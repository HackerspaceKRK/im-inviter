"""Mały stan w SQLite: kogo już zaproszono, co już zaraportowano, jednorazowe kody Telegrama.

Prawdą o członkostwie jest Matrix; to jest tylko pamięć "to już robiłem", żeby nie
zapraszać w kółko kogoś, kto wyszedł, i nie spamować tym samym błędem.
Kody Telegrama trzymamy jako hash (SHA-256), nigdy jawnie.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from typing import Optional

CODE_TTL_SECONDS = 15 * 60
MAX_CODES_PER_ACCOUNT = 5
# Odstępy między próbami nieudanego zaproszenia (s): 5 min, 15 min, 1 h, 6 h, 24 h (i tak już co 24 h).
INVITE_BACKOFF = (300, 900, 3600, 21600, 86400)
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
            CREATE TABLE IF NOT EXISTS failures(
                handle TEXT, room TEXT, attempts INTEGER, next_retry REAL, last_error TEXT,
                PRIMARY KEY(handle, room));
            -- Co bot ostatnio zapisał do Authentika dla usera (jego handle'e). Pozwala wykryć, że ktoś INNY
            -- nadpisał atrybuty (np. zapis w usermanagerze): bot pamięta, że zapisał matrixAccounts, a tam pusto.
            CREATE TABLE IF NOT EXISTS written(pk INTEGER PRIMARY KEY, handles TEXT, ts INTEGER);
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

    # --- nieudane zaproszenia: odstęp rośnie, żeby nie młócić homeservera (limit zaproszeń → 429) ---
    def failure(self, handle: str, room: str):
        """(próby, kolejna próba (unix), ostatni błąd) albo None."""
        return self.db.execute(
            "SELECT attempts, next_retry, last_error FROM failures WHERE handle=? AND room=?", (handle, room)
        ).fetchone()

    def record_failure(self, handle: str, room: str, error: str, min_delay: float = 0,
                       now: Optional[float] = None) -> float:
        """Zapisuje porażkę; zwraca czas kolejnej próby. min_delay = np. retry_after z 429."""
        now = time.time() if now is None else now
        prev = self.failure(handle, room)
        attempts = (prev[0] if prev else 0) + 1
        delay = max(INVITE_BACKOFF[min(attempts - 1, len(INVITE_BACKOFF) - 1)], min_delay)
        self.db.execute("INSERT OR REPLACE INTO failures VALUES(?,?,?,?,?)",
                        (handle, room, attempts, now + delay, error))
        self.db.commit()
        return now + delay

    def clear_failure(self, handle: str, room: str) -> None:
        self.db.execute("DELETE FROM failures WHERE handle=? AND room=?", (handle, room))
        self.db.commit()

    def invited_at(self, handle: str, room: str) -> Optional[int]:
        """Kiedy bot zaprosił tę parę (unix ts) albo None, gdy nie zapraszał (np. zaprosił ktoś inny)."""
        r = self.db.execute("SELECT ts FROM invited WHERE handle=? AND room=?", (handle, room)).fetchone()
        return r[0] if r else None

    # --- spójność z Authentikiem (źródło prawdy): pamięć bota nie może przeżyć danych, z których wynikała ---
    def forget_handles(self, handles) -> None:
        """Kasuje całą pamięć o podanych handle'ach: zaproszenia, odstępy po porażkach, zgłoszone błędy."""
        for h in set(handles):
            self.db.execute("DELETE FROM invited WHERE handle=?", (h,))
            self.db.execute("DELETE FROM failures WHERE handle=?", (h,))
            for prefix in (f"ban:{h}:", f"err:{h}:", f"left:{h}:", f"pending:{h}:"):
                self.db.execute("DELETE FROM reported WHERE substr(key,1,?)=?", (len(prefix), prefix))
        self.db.commit()

    def record_written(self, pk: int, handles) -> None:
        """Zapisuje, że bot właśnie zapisał stan kont (matrixAccounts) usera `pk` dla tych handle'i."""
        enc = json.dumps(sorted(set(handles)))
        row = self.db.execute("SELECT handles FROM written WHERE pk=?", (pk,)).fetchone()
        if row is None or row[0] != enc:
            self.db.execute("INSERT OR REPLACE INTO written VALUES(?,?,?)", (pk, enc, int(time.time())))
            self.db.commit()

    def written_handles(self, pk: int) -> Optional[list]:
        """Handle'e, dla których bot ostatnio zapisał stan usera; None, gdy bot jeszcze nigdy tego nie zapisał."""
        row = self.db.execute("SELECT handles FROM written WHERE pk=?", (pk,)).fetchone()
        return json.loads(row[0]) if row else None

    def forget_user(self, pk: int) -> None:
        self.db.execute("DELETE FROM written WHERE pk=?", (pk,))
        self.db.commit()

    def prune_written(self, existing_pks) -> int:
        """Usuwa znaczniki userów, których już nie ma w Authentiku."""
        known = {r[0] for r in self.db.execute("SELECT pk FROM written")}
        gone = known - set(existing_pks)
        for pk in gone:
            self.db.execute("DELETE FROM written WHERE pk=?", (pk,))
        if gone:
            self.db.commit()
        return len(gone)

    def drop_unconfigured_rooms(self, rooms) -> int:
        """Kasuje pamięć o pokojach, których nie ma już w konfiguracji (MATRIX_ROOMS/MATRIX_SPACES). Zwraca liczbę wierszy."""
        rooms = list(rooms)
        if not rooms:
            return 0  # brak konfiguracji Matrixa: niczego nie ruszamy
        marks = ",".join("?" * len(rooms))
        n = 0
        for table in ("invited", "failures"):
            n += self.db.execute(f"DELETE FROM {table} WHERE room NOT IN ({marks})", rooms).rowcount
        if n:
            self.db.commit()
        return n

    def forget_missing(self, current_handles) -> list:
        """Zapomina handle'e, których nikt już nie ma wpisanych w Authentiku (zwraca ich listę).

        Dzięki temu skasowanie handle'a i dopisanie go z powrotem = ponowne zaproszenie
        (np. po odrzuconym zaproszeniu). Czyści też zapamiętane „już zgłoszone" błędy i bany.
        """
        known = {r[0] for r in self.db.execute("SELECT DISTINCT handle FROM invited")}
        gone = sorted(known - set(current_handles))
        self.forget_handles(gone)
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
