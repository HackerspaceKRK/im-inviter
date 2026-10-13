"""Mały stan w SQLite: kogo już zaproszono, co już zaraportowano, jednorazowe kody Telegrama.

Prawdą o członkostwie jest Matrix; to jest tylko pamięć "to już robiłem", żeby nie
zapraszać w kółko kogoś, kto wyszedł, i nie spamować tym samym błędem.
Kody Telegrama trzymamy jako hash (SHA-256), nigdy jawnie.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from typing import Optional

CODE_TTL_SECONDS = 15 * 60
MAX_CODES_PER_ACCOUNT = 5
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # bez 0/O/1/I


def hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def generate_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(8))


class State:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS invited(
                handle TEXT, room TEXT, ts INTEGER, PRIMARY KEY(handle, room));
            CREATE TABLE IF NOT EXISTS reported(key TEXT PRIMARY KEY, ts INTEGER);
            CREATE TABLE IF NOT EXISTS tgcodes(
                hash TEXT PRIMARY KEY, tg_id INTEGER, expires INTEGER);
            """
        )
        self.db.commit()

    # --- Matrix ---
    def was_invited(self, handle: str, room: str) -> bool:
        r = self.db.execute("SELECT 1 FROM invited WHERE handle=? AND room=?", (handle, room))
        return r.fetchone() is not None

    def mark_invited(self, handle: str, room: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO invited VALUES(?,?,?)", (handle, room, int(time.time()))
        )
        self.db.commit()

    def forget_missing(self, current_handles) -> list:
        """Zapomina handle'e, których nikt już nie ma wpisanych w Authentiku (zwraca ich listę).

        Dzięki temu skasowanie handle'a i dopisanie go z powrotem = ponowne zaproszenie
        (np. po odrzuconym zaproszeniu). Czyści też zapamiętane „już zgłoszone" błędy i bany.
        """
        known = {r[0] for r in self.db.execute("SELECT DISTINCT handle FROM invited")}
        gone = sorted(known - set(current_handles))
        for h in gone:
            self.db.execute("DELETE FROM invited WHERE handle=?", (h,))
            for prefix in (f"ban:{h}:", f"err:{h}:"):
                self.db.execute("DELETE FROM reported WHERE substr(key,1,?)=?", (len(prefix), prefix))
        if gone:
            self.db.commit()
        return gone

    def report_once(self, key: str) -> bool:
        """True przy pierwszym wywołaniu dla danego klucza (czyli: warto zalogować/zgłosić)."""
        cur = self.db.execute("INSERT OR IGNORE INTO reported VALUES(?,?)", (key, int(time.time())))
        self.db.commit()
        return cur.rowcount == 1

    # --- Telegram ---
    def new_tg_code(self, tg_id: int, now: Optional[float] = None) -> str:
        now = time.time() if now is None else now
        # Poprzednie kody tego konta zostają ważne do wygaśnięcia: bot sprawdza kody co ~60 s, więc
        # ponowny /start nie może unieważniać kodu, który user właśnie wkleił. Cap = nie rośnie w nieskończoność.
        self.db.execute("DELETE FROM tgcodes WHERE expires<?", (now,))
        self.db.execute(
            "DELETE FROM tgcodes WHERE tg_id=? AND hash NOT IN "
            "(SELECT hash FROM tgcodes WHERE tg_id=? ORDER BY expires DESC LIMIT ?)",
            (tg_id, tg_id, MAX_CODES_PER_ACCOUNT - 1),
        )
        code = generate_code()
        self.db.execute(
            "INSERT INTO tgcodes VALUES(?,?,?)", (hash_code(code), tg_id, int(now) + CODE_TTL_SECONDS)
        )
        self.db.commit()
        return code

    def consume_tg_code(self, code: str, now: Optional[float] = None) -> Optional[int]:
        """Zwraca tg_id jeśli kod istnieje i nie wygasł; kod jest jednorazowy (kasowany)."""
        now = time.time() if now is None else now
        h = hash_code(code)
        row = self.db.execute("SELECT tg_id, expires FROM tgcodes WHERE hash=?", (h,)).fetchone()
        if row is None:
            return None
        self.db.execute("DELETE FROM tgcodes WHERE hash=?", (h,))
        self.db.commit()
        return row[0] if row[1] >= now else None
