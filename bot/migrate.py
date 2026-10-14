"""Migracja atrybutów usera: stare nazwy (snake_case) → camelCase, scalanie duplikatów, sprzątanie.

Uruchamiana przy starcie bota (i potem co godzinę, bo stary blueprint/flow może jeszcze zapisać stare klucze).
Jest idempotentna: user, który ma już nowy format, nie jest zapisywany. Zmienia WYŁĄCZNIE atrybuty bota;
nieznanych kluczy (np. cudzych narzędzi) nie rusza, tylko zgłasza je w inwentarzu do ręcznego przejrzenia.
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from .handles import normalize_matrix_handles, normalize_tg_ids

log = logging.getLogger("migrate")

# Dane wpisywane przez usera/admina: zmieniamy nazwę (i scalamy z nowym kluczem, jeśli już istnieje).
RENAMES = {
    "matrix_handles": "matrixHandles",
    "telegram_username": "telegramUsername",
}
# Stare znaczniki „zweryfikowane ID": źródłem prawdy jest wyłącznie lista `telegramIDs` (wiele kont na usera),
# więc ich wartość scalamy do `telegramIDs`, a klucz usuwamy.
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
    "matrixHandles", "matrixStatus", "matrixAccounts", "matrixInviteRequests", "matrixInviteResults",
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
        merged, _invalid = normalize_matrix_handles([attrs.get("matrixHandles"), attrs.get("matrix_handles")])
        if merged:
            set_["matrixHandles"] = merged
        remove.append("matrix_handles")
        notes.append(f"matrix_handles → matrixHandles ({len(merged)} kont po scaleniu)")

    if "telegram_username" in attrs:
        if _is_empty(attrs.get("telegramUsername")) and not _is_empty(attrs["telegram_username"]):
            set_["telegramUsername"] = attrs["telegram_username"]
            notes.append("telegram_username → telegramUsername")
        else:
            notes.append("telegram_username: usunięto (nowy klucz już ma wartość lub stary jest pusty)")
        remove.append("telegram_username")

    # telegramIDs: scal stare znaczniki i ujednolić zapis do listy tekstów (tablica, „1, 2" albo liczby → ["1","2"]).
    legacy_present = [k for k in ID_LEGACY if k in attrs]
    if legacy_present or "telegramIDs" in attrs:
        merged = normalize_tg_ids(attrs.get("telegramIDs"), *(attrs.get(k) for k in legacy_present))
        if merged != attrs.get("telegramIDs") and (merged or "telegramIDs" in attrs):
            set_["telegramIDs"] = merged
            notes.append("telegramIDs: " + ("scalone ze starymi kluczami i " if legacy_present else "")
                         + f"ujednolicone do listy tekstów ({len(merged)} kont)")
        for k in legacy_present:
            remove.append(k)
            notes.append(f"{k} → telegramIDs (klucz usunięty)")

    for key in DROP:
        if key in attrs:
            remove.append(key)
            notes.append(f"{key}: usunięto (bot policzy od nowa)")

    for key in DROP_WHEN_EMPTY:
        if key in attrs and key not in set_ and _is_empty(attrs[key]):
            remove.append(key)
            notes.append(f"{key}: pusta wartość usunięta")

    return set_, remove, notes


async def run_migration(authentik: Any, dry_run: bool = False) -> Dict[str, Any]:
    """Przegląda WSZYSTKICH userów (także nieaktywnych) i naprawia atrybuty bota. Zwraca podsumowanie."""
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
    log.info("atrybuty w Authentiku (klucz×userów): %s",
             ", ".join(f"{k}×{n}" for k, n in sorted(inventory.items())) or "brak")
    if unknown:
        log.warning("nieznane atrybuty (do ręcznego przejrzenia, bot ich nie rusza): %s",
                    ", ".join(f"{k}×{n}" for k, n in sorted(unknown.items())))
    return {"users": len(users), "migrated": migrated, "unknown": unknown, "inventory": dict(inventory)}
