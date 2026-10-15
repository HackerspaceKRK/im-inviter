"""Migracja atrybutów usera: stare nazwy (snake_case) → camelCase, scalanie duplikatów, sprzątanie.

Uruchamiana przy starcie bota (i potem co godzinę, bo stary blueprint/flow może jeszcze zapisać stare klucze).
Jest idempotentna: user, który ma już nowy format, nie jest zapisywany. Zmienia WYŁĄCZNIE atrybuty bota;
nieznanych kluczy (np. cudzych narzędzi) nie rusza, tylko zgłasza je w ostrzeżeniu do ręcznego przejrzenia
(pełny inwentarz kluczy tylko z `--audit`).
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from .handles import append_tg_id, normalize_matrix_handles, normalize_tg_ids

log = logging.getLogger("migrate")

# Dane wpisywane przez usera/admina: zmieniamy nazwę (i scalamy z nowym kluczem, jeśli już istnieje).
RENAMES = {
    "matrix_handles": "matrixHandles",
    "telegram_username": "telegramUsername",
}
# Stare znaczniki „zweryfikowane ID": źródłem prawdy jest wyłącznie `telegramIDs` (wiele kont na usera),
# więc ich wartość dopisujemy do `telegramIDs` (w jego formacie), a klucz usuwamy.
ID_LEGACY = ("telegram_id", "telegramVerifiedId")
# Wartości pochodne albo jednorazowe: bot policzy je od nowa, więc zmiana nazwy nie ma sensu — usuwamy.
DROP = (
    "matrix_status", "matrix_accounts", "matrix_invite_requests", "matrix_invite_results",
    "telegram_code", "telegram_status",
)
# Flow ustawień zapisuje puste stringi dla nietkniętych pól — nie trzymamy ich jako atrybutów.
DROP_WHEN_EMPTY = ("matrixHandles", "telegramUsername", "telegramCode")

# Klucze, które znamy (bot, usermanager, Authentik) — reszta w inwentarzu to „nieznane".
KNOWN_KEYS = {
    "matrixHandles", "matrixStatus", "matrixAccounts", "matrixInviteRequests", "matrixInviteResults", "imInviterNote",
    "telegramInviteRequests", "telegramInviteResults", "telegramReachableIDs", "telegramAccounts",
    "telegramUsername", "telegramCode", "telegramStatus", "telegramIDs",
    "mifareCardId", "bankAccountNumber", "membershipExpirationTimestamp", "foundUsBy", "settings",
}
_EMPTY = (None, "", [], {})
INVENTORY_EVERY = 3600  # s: ponowne uruchomienie migracji w pętli głównej


def _is_empty(v: Any) -> bool:
    return v in _EMPTY if isinstance(v, (str, list, dict, type(None))) else False


def plan_migration(attrs: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """(co ustawić, co usunąć, opis zmian). Puste wyniki = nic do roboty."""
    set_: Dict[str, Any] = {}
    remove: List[str] = []
    notes: List[str] = []

    if "matrix_handles" in attrs:
        merged, invalid = normalize_matrix_handles([attrs.get("matrixHandles"), attrs.get("matrix_handles")])
        if merged:
            set_["matrixHandles"] = merged
        remove.append("matrix_handles")
        dropped = f"; odrzucono niepoprawne wpisy: {', '.join(invalid)}" if invalid else ""
        notes.append(f"matrix_handles → matrixHandles ({len(merged)} kont po scaleniu{dropped})")

    if "telegram_username" in attrs:
        already_linked = any(not _is_empty(attrs.get(k)) for k in ID_LEGACY)
        if already_linked and not _is_empty(attrs["telegram_username"]):
            # Stary bot zostawiał spełniony wniosek w atrybucie. Konto jest już połączone, więc nie przenosimy go
            # jako „oczekującego" (inaczej usermanager pokazywałby „waiting for /start" w nieskończoność).
            notes.append("telegram_username: usunięto (wniosek spełniony, konto już połączone)")
        elif _is_empty(attrs.get("telegramUsername")) and not _is_empty(attrs["telegram_username"]):
            set_["telegramUsername"] = attrs["telegram_username"]
            notes.append("telegram_username → telegramUsername")
        else:
            notes.append("telegram_username: usunięto (nowy klucz już ma wartość lub stary jest pusty)")
        remove.append("telegram_username")

    # telegramIDs to pole WSPÓŁDZIELONE z usermanagerem i innymi narzędziami: NIE zmieniamy formatu zapisu
    # (lista zostaje listą, tekst „1, 2" zostaje tekstem). Jedyna zmiana to dopisanie ID ze starych znaczników
    # (telegram_id, telegramVerifiedId), jeśli ich jeszcze nie ma, w tym samym formacie co istniejąca wartość.
    current = attrs.get("telegramIDs")
    legacy_present = [k for k in ID_LEGACY if k in attrs]
    merged = current
    appended = []
    for token in normalize_tg_ids(*(attrs.get(k) for k in legacy_present)):
        updated = append_tg_id(merged, token)
        if updated != merged:
            appended.append(token)
            merged = updated
    if appended:
        set_["telegramIDs"] = merged
        notes.append(f"telegramIDs: dopisano ze starych kluczy: {', '.join(appended)}")
    for k in legacy_present:
        remove.append(k)
        notes.append(f"{k}: usunięto (wartość scalona do telegramIDs)")

    for key in DROP:
        if key in attrs:
            remove.append(key)
            notes.append(f"{key}: usunięto (bot policzy od nowa)")

    for key in DROP_WHEN_EMPTY:
        if key in attrs and key not in set_ and _is_empty(attrs[key]):
            remove.append(key)
            notes.append(f"{key}: pusta wartość usunięta")

    return set_, remove, notes


async def run_migration(authentik: Any, dry_run: bool = False, audit: bool = False) -> Dict[str, Any]:
    """Przegląda WSZYSTKICH userów (także nieaktywnych) i naprawia atrybuty bota. Zwraca podsumowanie.

    audit=True dodatkowo wypisuje pełny inwentarz kluczy (klucz×userów). Domyślnie log pokazuje tylko to, co bot
    zmienia, i ostrzeżenie o nieznanych kluczach; inwentarz jest wyłącznie do odczytu, niczego nie zmienia."""
    users = await authentik.list_users(include_inactive=True)
    inventory: Counter = Counter()
    migrated = 0
    for user in users:
        attrs = user.get("attributes") or {}
        inventory.update(attrs.keys())
        set_, remove, notes = plan_migration(attrs)
        if not (set_ or remove):
            continue
        migrated += 1
        log.info("%s migracja atrybutów usera %s: %s", "[dry-run]" if dry_run else "", user["username"], "; ".join(notes))
        if dry_run:
            continue

        def apply(fresh: Dict[str, Any]) -> None:
            """Na ŚWIEŻYCH atrybutach: plan liczony ponownie tuż przed zapisem (nie nadpisujemy cudzych zmian)."""
            s, r, _n = plan_migration(fresh)
            fresh.update(s)
            for k in r:
                fresh.pop(k, None)

        await authentik.update_attributes(user["pk"], mutate=apply)

    handled = KNOWN_KEYS | set(RENAMES) | set(DROP) | set(ID_LEGACY)  # także stare klucze, które właśnie migrujemy
    unknown = {k: n for k, n in inventory.items() if k not in handled and not k.startswith("goauthentik.io/")}
    log.info("migracja: userów %d, zmieniono %d", len(users), migrated)
    if audit:
        log.info("atrybuty w Authentiku (klucz×userów): %s",
                 ", ".join(f"{k}×{n}" for k, n in sorted(inventory.items())) or "brak")
    if unknown:
        log.warning("nieznane atrybuty (do ręcznego przejrzenia, bot ich nie rusza): %s",
                    ", ".join(f"{k}×{n}" for k, n in sorted(unknown.items())))
    return {"users": len(users), "migrated": migrated, "unknown": unknown, "inventory": dict(inventory)}
