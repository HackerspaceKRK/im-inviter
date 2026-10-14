"""Czysta logika: normalizacja handle'i i decyzje "czy zapraszać". Bez sieci, łatwa do testów."""
from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple

MXID_RE = re.compile(r"^@[a-z0-9._=/+\-]+:[a-z0-9.\-]+(?::\d{1,5})?$")
TG_USERNAME_RE = re.compile(r"^[a-z0-9_]{5,32}$")


def _tokens(value: Any) -> List[str]:
    """Rozbija tekst (nowe linie, spacje, przecinki, średniki) albo listę na kawałki."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out: List[str] = []
        for item in value:
            out.extend(_tokens(item))
        return out
    return [t for t in re.split(r"[\s,;]+", str(value)) if t]


def normalize_matrix_handles(value: Any) -> Tuple[List[str], List[str]]:
    """Zwraca (poprawne bez duplikatów w kolejności wpisania, niepoprawne)."""
    valid: List[str] = []
    invalid: List[str] = []
    for tok in _tokens(value):
        h = tok.lower()
        if not h.startswith("@"):
            h = "@" + h
        if MXID_RE.match(h):
            if h not in valid:
                valid.append(h)
        else:
            invalid.append(tok)
    return valid, invalid


def normalize_tg_username(value: Any) -> Optional[str]:
    """'@Jan_Kowalski' / 't.me/Jan_Kowalski' -> 'jan_kowalski'; None gdy to nie username."""
    toks = _tokens(value)
    if len(toks) != 1:
        return None
    s = toks[0].lower()
    s = re.sub(r"^(https?://)?(t\.me/)", "", s)
    s = s.lstrip("@")
    return s if TG_USERNAME_RE.match(s) else None


def invite_action(membership: Optional[str], already_invited: bool) -> str:
    """Co zrobić z parą (handle, pokój): 'invite' | 'joined' | 'skip' | 'banned' | 'left' | 'pending'.

    membership = wartość z m.room.member (join/invite/leave/ban) albo None gdy brak wpisu.
    already_invited = bot już kiedyś to zaprosił (SQLite). Dzięki temu ktoś, kto świadomie
    wyszedł z pokoju (leave), nie jest zapraszany w kółko.
    """
    if membership == "ban":
        return "banned"
    if membership == "join":
        return "joined"
    if membership == "invite":
        return "pending"  # zaproszony, ale jeszcze nie przyjął; zaproszenia w Matrixie nie wygasają
    if membership == "leave" and already_invited:
        return "left"  # odrzucił zaproszenie albo wyszedł — nie ponawiamy, ale zgłaszamy w statusie
    return "skip" if already_invited else "invite"


def pending_age_label(age_seconds: float) -> str:
    """Gruby opis wieku zaproszenia: status w Authentiku zmienia się najwyżej raz na dobę (nie co godzinę)."""
    days = int(age_seconds // 86400)
    if days >= 2:
        return f"ponad {days} dni"
    hours = int(age_seconds // 3600)
    return "ponad dobę" if hours >= 24 else f"ponad {max(hours, 1)} h"


def normalize_tg_ids(*values: Any) -> List[str]:
    """ID Telegrama z dowolnej formy zapisu → lista tekstów (kolejność zachowana, bez duplikatów i pustych).

    Przyjmuje tablicę, napis „288245411, 111111111" (przecinki/spacje/średniki) albo liczby; `usermanager`
    zapisuje `telegramIDs` jako tablicę tekstów, ale starsze dane i ręczne edycje bywają inne.
    """
    out: List[str] = []
    for v in values:
        for tok in _tokens(v):
            if tok not in out:
                out.append(tok)
    return out


def find_pending_tg_users(users: List[dict], tg_username: str) -> List[dict]:
    """Userzy Authentika, którzy wpisali ten @username jako wniosek o połączenie.

    Czy to konto jest już połączone, rozstrzyga wcześniej `find_linked_user` (po ID na liście `telegramIDs`)."""
    want = normalize_tg_username(tg_username)
    if not want:
        return []
    return [u for u in users if normalize_tg_username((u.get("attributes") or {}).get("telegramUsername")) == want]
