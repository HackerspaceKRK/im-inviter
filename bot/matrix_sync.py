"""Matrix: zapraszanie kont z atrybutu `matrixHandles` do pokoi z configu."""
from __future__ import annotations

import logging
import time
import uuid
from html import escape as html_escape
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from .handles import invite_action, normalize_matrix_handles, pending_age_label

log = logging.getLogger("matrix")


class Matrix:
    def __init__(self, homeserver: str, token: str, client: Optional[httpx.AsyncClient] = None,
                 msgtype: str = "m.text"):
        # m.text: zwykła wiadomość, mosty (mautrix → Telegram) ją przekazują, ale klienty powiadamiają.
        # m.notice: „bot", cichy, ale mosty domyślnie go pomijają.
        self.msgtype = msgtype
        self.base = homeserver.rstrip("/") + "/_matrix/client/v3"
        self.client = client or httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, timeout=20
        )
        self._rids: Dict[str, str] = {}

    async def rid(self, room: str) -> str:
        """Alias (`#pokoj:serwer`) → ID pokoju (`!abc:serwer`); ID zwraca bez zmian.

        API zapraszania/stanu/wysyłania przyjmuje tylko ID — z aliasem Synapse odpowiada mylącym
        „User … not in room". Wynik jest w pamięci do restartu bota.
        """
        if not room.startswith("#"):
            return room
        if room not in self._rids:
            r = await self.client.get(f"{self.base}/directory/room/{quote(room, safe='')}")
            r.raise_for_status()
            self._rids[room] = r.json()["room_id"]
        return self._rids[room]

    async def membership(self, room: str, mxid: str) -> Optional[str]:
        room = await self.rid(room)
        r = await self.client.get(
            f"{self.base}/rooms/{quote(room, safe='')}/state/m.room.member/{quote(mxid, safe='')}"
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("membership")

    async def room_name(self, room: str) -> Optional[str]:
        room = await self.rid(room)
        r = await self.client.get(f"{self.base}/rooms/{quote(room, safe='')}/state/m.room.name")
        return r.json().get("name") if r.status_code == 200 else None

    async def invite(self, room: str, mxid: str) -> None:
        room = await self.rid(room)
        r = await self.client.post(
            f"{self.base}/rooms/{quote(room, safe='')}/invite", json={"user_id": mxid}
        )
        r.raise_for_status()

    async def notice(self, room: str, text: str, html: Optional[str] = None) -> None:
        room = await self.rid(room)
        content: Dict[str, Any] = {"msgtype": self.msgtype, "body": text}
        if html:
            content.update({"format": "org.matrix.custom.html", "formatted_body": html})
        r = await self.client.put(
            f"{self.base}/rooms/{quote(room, safe='')}/send/m.room.message/{uuid.uuid4().hex}",
            json=content,
        )
        r.raise_for_status()


def user_link(mxid: str) -> str:
    """Handle jako link matrix.to do USERA: klient rysuje pigułkę użytkownika, a nie zgaduje
    z tekstu `@x:domena.pl`, że to adres pokoju/domeny."""
    return f'<a href="https://matrix.to/#/{quote(mxid, safe="@:")}">{html_escape(mxid)}</a>'


def _retry_after(e: Exception) -> float:
    """Sekundy z odpowiedzi 429 (retry_after_ms z Synapse'a albo nagłówek Retry-After); 0 gdy to nie 429."""
    if not (isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 429):
        return 0.0
    try:
        ms = e.response.json().get("retry_after_ms")
        if ms:
            return float(ms) / 1000
    except Exception:
        pass
    try:
        return float(e.response.headers.get("Retry-After", 60))
    except ValueError:
        return 60.0


def _server_error(e: Exception) -> bool:
    """Błąd po stronie serwera/połączenia (np. 502 przy zdalnym serwerze handle'a, timeout)."""
    return isinstance(e, httpx.RequestError) or (
        isinstance(e, httpx.HTTPStatusError) and e.response.status_code >= 500
    )


def _hhmm(ts: float) -> str:
    return time.strftime("%H:%M", time.gmtime(ts)) + " UTC"


def _err(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        try:
            err = e.response.json().get("error", "")
        except Exception:
            return str(e.response.status_code)
        if e.response.status_code == 403 and "not in room" in err:
            return "bot nie jest w tym pokoju albo nie ma w nim uprawnień (dodaj go i nadaj power level ≥ invite)"
        return f"{e.response.status_code} {err}".strip()
    return f"{type(e).__name__}: {e}" if str(e) == "" else str(e)


# Od najlepszego do najgorszego: agregat konta = najgorszy stan po wszystkich pokojach/przestrzeniach.
STATE_RANK = {"unknown": 0, "joined": 1, "invited": 2, "pending": 3, "left": 4, "banned": 5, "error": 6}
MAX_RESULTS = 5  # ile ostatnich wyników ręcznych zaproszeń zostaje w matrixInviteResults


def parse_requests(value: Any) -> List[Dict[str, Any]]:
    """`matrixInviteRequests`: lista {handle, by, ts}. Wpisy niepoprawnego kształtu są pomijane."""
    out: List[Dict[str, Any]] = []
    if not isinstance(value, list):
        return out
    for item in value:
        if isinstance(item, dict) and isinstance(item.get("handle"), str):
            out.append({"handle": item["handle"], "by": str(item.get("by") or "admin"), "ts": item.get("ts")})
    return out


def _req_key(req: Dict[str, Any]) -> tuple:
    return (req["handle"], req["by"], req["ts"])


def inconsistency_note(handles) -> str:
    """Komentarz zostawiany userowi w atrybucie `imInviterNote`, gdy bot wykryje nadpisanie swoich atrybutów."""
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    which = ", ".join(sorted(set(handles))) or "(brak)"
    return (f"{when}: wykryto niespójność. Atrybuty bota (matrixAccounts itd.) zniknęły z Authentika, mimo że bot je "
            f"wcześniej zapisał; prawdopodobnie nadpisało je inne narzędzie (np. zapis usera w usermanagerze, który podmienia "
            f"cały obiekt atrybutów). Bot wyczyścił swoją pamięć o kontach Matrix: {which} i policzył stan od nowa "
            f"(konta spoza pokoi dostaną zaproszenia ponownie). Sprawdź, czy telegramUsername/matrixHandles są nadal wpisane.")


def aggregate_account(entries: List[Tuple[str, str]]) -> Tuple[str, str]:
    """[(stan, szczegóły)] → (najgorszy stan, szczegóły wpisów o tym stanie, bez powtórzeń)."""
    if not entries:
        return "unknown", ""
    worst = max(STATE_RANK[st] for st, _ in entries)
    state = next(st for st, _ in entries if STATE_RANK[st] == worst)
    details: List[str] = []
    for st, d in entries:
        if st == state and d and d not in details:
            details.append(d)
    return state, "; ".join(details)


async def run_matrix_cycle(
    authentik: Any, matrix: Matrix, state: Any, rooms: List[str], dry_run: bool = False,
    spaces: Optional[List[str]] = None, notify_room: Optional[str] = None,
    pending_after_hours: float = 24,
) -> Dict[str, int]:
    """Jeden przebieg: dla każdego usera z handle'ami zaproś brakujących. Zwraca statystyki.

    rooms  — zaproszenie + wiadomość „Dodano …" w tym samym pokoju.
    pending_after_hours — po tylu godzinach nieprzyjęte zaproszenie trafia do matrixStatus (0 = wyłączone).
    spaces — samo zaproszenie do przestrzeni (pokoje-dzieci wpuszczają członków space same, o ile
             mają regułę „restricted"); wiadomość idzie do notify_room, a bez niego nie ma jej wcale.

    Na usera zapisujemy atrybuty JEDNYM wywołaniem: matrixStatus, matrixAccounts (stan każdego konta),
    znormalizowaną listę handle'i oraz wyniki/oczyszczoną kolejkę ręcznych zaproszeń (matrixInviteRequests,
    pisaną przez usermanager). Ręczne zaproszenie ignoruje pamięć „już zapraszałem" i odstęp po porażce
    (to świadoma decyzja admina), ale tylko dla handle'a, który jest na liście TEGO usera.
    """
    stats = {"invited": 0, "errors": 0, "manual": 0}
    spaces = spaces or []
    targets = [(r, False) for r in rooms] + [(s, True) for s in spaces if s not in rooms]
    names: Dict[str, str] = {}
    current: set = set()  # wszystkie handle'e wpisane teraz w Authentiku (do zapominania usuniętych)
    ctx = {"halt_until": 0.0}  # po 429: do tego momentu nie wysyłamy już zaproszeń (reszta w kolejnych przebiegach)

    async def label(room: str) -> str:
        """Nazwa pokoju/przestrzeni do komunikatów (cache na przebieg); fallback: ID."""
        if room not in names:
            names[room] = (await matrix.room_name(room)) or room
        return names[room]

    async def process_pair(
        handle: str, room: str, is_space: bool, force: bool, username: str, by: Optional[str], msgs: List[str]
    ) -> Tuple[Optional[str], str, str, bool]:
        """Para (handle, pokój). Zwraca (stan do matrixAccounts | None, szczegóły, wynik, przerwij pokoje).
        wynik: invited | skipped | deferred | failed."""
        action = invite_action(await matrix.membership(room, handle), state.was_invited(handle, room))
        if force and action == "left":
            action = "invite"  # admin kazał zaprosić ponownie mimo wcześniejszego odrzucenia
        if action != "invite":
            state.clear_failure(handle, room)  # np. ktoś zaprosił ręcznie / już jest w pokoju
        if action == "banned":
            lbl = await label(room)
            msgs.append(f"{handle} ma bana w {lbl}")
            if state.report_once(f"ban:{handle}:{room}"):
                log.warning("%s zbanowany w %s", handle, room)
            return "banned", f"ma bana w {lbl}", "failed", False
        if action == "left":
            # Odrzucone zaproszenie wygląda w Matrixie tak samo jak wyjście z pokoju (leave).
            # Nie ponawiamy; żeby zaprosić jeszcze raz: usuń handle z Authentika i dopisz z powrotem
            # albo użyj ręcznego zaproszenia w usermanagerze.
            lbl = await label(room)
            detail = f"odrzucił zaproszenie lub wyszedł z: {lbl}"
            msgs.append(f"{handle} {detail}")
            if state.report_once(f"left:{handle}:{room}"):
                log.info("%s odrzucił/wyszedł z %s (user %s)", handle, room, username)
            return "left", detail, "skipped", False
        if action == "pending":
            # Zaproszenie czeka na akceptację. Matrix go nie wygasza ani nie da się go „ponowić",
            # więc tylko pokazujemy, jak długo wisi (nagabywać możemy tylko własne zaproszenia).
            lbl = await label(room)
            detail = f"czeka na akceptację w: {lbl}"
            ts = state.invited_at(handle, room)
            if pending_after_hours and ts is not None and time.time() - ts >= pending_after_hours * 3600:
                age = pending_age_label(time.time() - ts)
                detail = f"nie przyjął zaproszenia do: {lbl} ({age})"
                msgs.append(f"{handle} {detail}")
                if state.report_once(f"pending:{handle}:{room}"):
                    log.info("%s nie przyjął zaproszenia do %s (%s)", handle, room, age)
            return "pending", detail, "skipped", False
        if action == "joined":
            return "joined", "", "skipped", False
        if action != "invite":
            return None, "", "skipped", False  # np. brak wpisu, ale zapraszane kiedyś: nic nie wiemy
        # --- action == "invite" ---
        if dry_run:
            log.info("[dry-run] zaprosiłbym %s do %s (user %s)", handle, room, username)
            return None, "", "skipped", False
        if not force:
            fail = state.failure(handle, room)
            if fail and fail[1] > time.time():  # odstęp po poprzedniej porażce jeszcze trwa
                detail = f"{await label(room)}: {fail[2]} (następna próba {_hhmm(fail[1])})"
                msgs.append(f"{handle} → {detail}")
                return "error", detail, "failed", False
        if time.time() < ctx["halt_until"]:  # limit zaproszeń (429): reszta w następnych przebiegach
            return None, "", "deferred", False
        try:
            await matrix.invite(room, handle)
        except Exception as e:
            err = _err(e)
            retry_after = _retry_after(e)
            nxt = state.record_failure(handle, room, err, min_delay=retry_after)
            if retry_after:
                ctx["halt_until"] = time.time() + retry_after
            stats["errors"] += 1
            detail = f"{await label(room)}: {err} (następna próba {_hhmm(nxt)})"
            msgs.append(f"{handle} → {detail}")
            if state.report_once(f"err:{handle}:{room}:{err}"):
                log.error("invite %s → %s: %s (następna próba %s)", handle, room, err, _hhmm(nxt))
            # serwer handle'a nieosiągalny: nie próbuj pozostałych pokoi w tym przebiegu
            return "error", detail, ("deferred" if retry_after else "failed"), _server_error(e)
        state.clear_failure(handle, room)
        state.mark_invited(handle, room)
        stats["invited"] += 1
        log.info("zaproszono %s do %s (user %s%s)", handle, room, username, f", ręcznie przez {by}" if force else "")
        # Wiadomość „Dodano…" jest dodatkiem: jej porażka (np. limit wiadomości) nie psuje
        # udanego zaproszenia ani nie jest ponawiana jako zaproszenie.
        suffix = f" (ręcznie, przez {by})" if force and by else ""
        html_suffix = f" (ręcznie, przez {html_escape(by)})" if suffix else ""
        try:
            if not is_space:
                await matrix.notice(
                    room, f"Dodano {handle} na prośbę {username}{suffix}",
                    f"Dodano {user_link(handle)} na prośbę {html_escape(username)}{html_suffix}",
                )
            elif notify_room:
                sp = await label(room)
                await matrix.notice(
                    notify_room,
                    f"Dodano {handle} do przestrzeni {sp} na prośbę {username}{suffix}",
                    f"Dodano {user_link(handle)} do przestrzeni {html_escape(sp)} "
                    f"na prośbę {html_escape(username)}{html_suffix}",
                )
        except Exception as e:
            log.warning("zaproszono %s do %s, ale wiadomość „Dodano” się nie wysłała: %s", handle, room, _err(e))
        return "invited", "", "invited", False

    all_users = await authentik.list_users()
    if not dry_run:
        # Authentik jest źródłem prawdy: pamięć o pokojach spoza konfiguracji i o userach, których nie ma, jest zbędna.
        dropped = state.drop_unconfigured_rooms([r for r, _ in targets])
        pruned = state.prune_written([u["pk"] for u in all_users])
        if dropped or pruned:
            log.info("spójność: usunięto %d wpisów o pokojach spoza konfiguracji i %d znaczników nieistniejących userów",
                     dropped, pruned)

    for user in all_users:
        attrs = user.get("attributes") or {}
        raw = attrs.get("matrixHandles")
        requests = parse_requests(attrs.get("matrixInviteRequests"))
        username = user["username"]
        # Czy ktoś nadpisał atrybuty bota? Bot pamięta, że zapisał matrixAccounts dla tego usera, a w Authentiku
        # ich nie ma. (Świadome usunięcie wszystkich handle'i zostawia matrixAccounts do czasu sprzątnięcia przez bota.)
        written = state.written_handles(user["pk"])
        wiped = written is not None and not attrs.get("matrixAccounts")
        if not raw and not requests:
            # Brak handle'i: nie zostawiamy starego statusu/stanów kont.
            if not dry_run and written is not None:
                if wiped:
                    log.warning("spójność: user %s stracił atrybuty bota (handle'e %s); czyszczę pamięć i zostawiam notatkę",
                                username, ", ".join(written) or "-")
                    state.forget_handles(written)
                    state.forget_user(user["pk"])
                    await authentik.update_attributes(user["pk"], {"imInviterNote": inconsistency_note(written)})
                else:
                    state.forget_user(user["pk"])  # świadome usunięcie handle'i: forget_missing posprząta resztę
            if not dry_run and (attrs.get("matrixStatus") or attrs.get("matrixAccounts")):
                await authentik.update_attributes(user["pk"], remove=["matrixStatus", "matrixAccounts"])
            continue
        handles, invalid = normalize_matrix_handles(raw)
        current.update(handles)
        updates: Dict[str, Any] = {}
        if wiped:
            log.warning("spójność: user %s stracił atrybuty bota (handle'e %s); czyszczę pamięć i liczę stan od nowa",
                        username, ", ".join(sorted(set(written) | set(handles))) or "-")
            if not dry_run:
                lost = set(written) | set(handles)
                state.forget_handles(lost)  # zaproszenia liczone od nowa wg PRAWDZIWEGO stanu w Matrixie
                state.forget_user(user["pk"])
                updates["imInviterNote"] = inconsistency_note(lost)
        msgs: List[str] = [f"niepoprawny handle: {h}" for h in invalid]

        # Użytkownik wpisuje textarea (string) — innym aplikacjom oddajemy porządną listę.
        if raw and raw != handles and not dry_run:
            updates["matrixHandles"] = handles

        # Ręczne prośby: tylko dla handle'i z listy TEGO usera (inaczej nie zapraszamy dowolnego konta).
        forced: Dict[str, List[Dict[str, Any]]] = {}
        consumed: List[Tuple[Dict[str, Any], str, int]] = []  # (żądanie, wynik, ile zaproszeń)
        for req in requests:
            found, _bad = normalize_matrix_handles(req["handle"])
            h = found[0] if found else None
            if h is None or h not in handles:
                consumed.append((req, "error: to konto nie należy do tego usera", 0))
            else:
                forced.setdefault(h, []).append(req)
        outcomes: Dict[str, Dict[str, Any]] = {
            h: {"invited": 0, "skipped": 0, "failed": [], "deferred": False} for h in forced
        }

        accounts: Dict[str, List[Tuple[str, str]]] = {h: [] for h in handles}
        for handle in handles:
            force = handle in forced
            by = forced[handle][0]["by"] if force else None
            for room, is_space in targets:
                try:
                    st, detail, outcome, stop = await process_pair(
                        handle, room, is_space, force, username, by, msgs
                    )
                except Exception as e:  # jeden zły handle/pokój nie wywala reszty
                    stats["errors"] += 1
                    err = _err(e)
                    msgs.append(f"{handle} → {room}: {err}")
                    if state.report_once(f"err:{handle}:{room}:{err}"):
                        log.error("invite %s → %s: %s", handle, room, err)
                    st, detail, outcome, stop = "error", f"{room}: {err}", "failed", False
                if st:
                    accounts[handle].append((st, detail))
                if force:
                    o = outcomes[handle]
                    if outcome == "invited":
                        o["invited"] += 1
                    elif outcome == "deferred":
                        o["deferred"] = True
                    elif outcome == "failed":
                        o["failed"].append(detail)
                    else:
                        o["skipped"] += 1
                if stop:
                    break

        if not dry_run:
            for h, reqs in forced.items():
                o = outcomes[h]
                if o["deferred"]:
                    continue  # limit (429): prośba zostaje w kolejce do następnego przebiegu
                if o["failed"]:
                    result = "error: " + "; ".join(dict.fromkeys(o["failed"]))
                else:
                    result = f"ok: zaproszono do {o['invited']}, pominięto {o['skipped']} (już w pokoju lub zaproszenie czeka)"
                stats["manual"] += 1
                for req in reqs:
                    consumed.append((req, result, o["invited"]))

        status = "; ".join(msgs) if msgs else "ok"
        if attrs.get("matrixStatus") != status:
            updates["matrixStatus"] = status

        now_ts = int(time.time())
        prev_accounts = attrs.get("matrixAccounts") if isinstance(attrs.get("matrixAccounts"), dict) else {}
        new_accounts: Dict[str, Dict[str, Any]] = {}
        for h in handles:
            st, detail = aggregate_account(accounts[h])
            prev = prev_accounts.get(h) if isinstance(prev_accounts.get(h), dict) else {}
            same = prev.get("state") == st and prev.get("detail") == detail and prev.get("updated")
            new_accounts[h] = {"state": st, "detail": detail, "updated": prev["updated"] if same else now_ts}
        if new_accounts != prev_accounts:
            updates["matrixAccounts"] = new_accounts

        if consumed:
            prev_results = attrs.get("matrixInviteResults")
            prev_results = prev_results if isinstance(prev_results, list) else []
            entries = [
                {"handle": req["handle"], "by": req["by"], "ts": now_ts, "result": result, "rooms": n}
                for req, result, n in consumed
            ]
            updates["matrixInviteResults"] = (prev_results + entries)[-MAX_RESULTS:]

        if not dry_run and (updates or consumed):
            done = {_req_key(r) for r, _res, _n in consumed}

            def drop_consumed(fresh: Dict[str, Any], done: set = done) -> None:
                """Na ŚWIEŻYCH atrybutach: nie gubimy próśb dopisanych przez admina w trakcie przebiegu."""
                if not done:
                    return
                left = [r for r in parse_requests(fresh.get("matrixInviteRequests")) if _req_key(r) not in done]
                if left:
                    fresh["matrixInviteRequests"] = left
                else:
                    fresh.pop("matrixInviteRequests", None)

            await authentik.update_attributes(user["pk"], updates, mutate=drop_consumed)
        if not dry_run and new_accounts:
            state.record_written(user["pk"], new_accounts)  # od teraz brak matrixAccounts w Authentiku = ktoś je nadpisał

    # Handle usunięty z Authentika = zapomnij o nim (skasuj i dopisz z powrotem → ponowne zaproszenie).
    # Dopiero po całej pętli, więc nie dotyczy przebiegu, który się wysypał w połowie.
    if not dry_run:
        gone = state.forget_missing(current)
        if gone:
            log.info("zapomniano handle'e usunięte z Authentika: %s", ", ".join(gone))
    return stats
