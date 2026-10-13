"""Matrix: zapraszanie kont z atrybutu `matrix_handles` do pokoi z configu."""
from __future__ import annotations

import logging
import uuid
from html import escape as html_escape
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from .handles import invite_action, normalize_matrix_handles

log = logging.getLogger("matrix")


class Matrix:
    def __init__(self, homeserver: str, token: str, client: Optional[httpx.AsyncClient] = None):
        self.base = homeserver.rstrip("/") + "/_matrix/client/v3"
        self.client = client or httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, timeout=20
        )

    async def membership(self, room: str, mxid: str) -> Optional[str]:
        r = await self.client.get(
            f"{self.base}/rooms/{quote(room, safe='')}/state/m.room.member/{quote(mxid, safe='')}"
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("membership")

    async def room_name(self, room: str) -> Optional[str]:
        r = await self.client.get(f"{self.base}/rooms/{quote(room, safe='')}/state/m.room.name")
        return r.json().get("name") if r.status_code == 200 else None

    async def invite(self, room: str, mxid: str) -> None:
        r = await self.client.post(
            f"{self.base}/rooms/{quote(room, safe='')}/invite", json={"user_id": mxid}
        )
        r.raise_for_status()

    async def notice(self, room: str, text: str, html: Optional[str] = None) -> None:
        content: Dict[str, Any] = {"msgtype": "m.notice", "body": text}
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


def _err(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        try:
            return f"{e.response.status_code} {e.response.json().get('error', '')}".strip()
        except Exception:
            return str(e.response.status_code)
    return str(e)


async def run_matrix_cycle(
    authentik: Any, matrix: Matrix, state: Any, rooms: List[str], dry_run: bool = False,
    spaces: Optional[List[str]] = None, notify_room: Optional[str] = None,
) -> Dict[str, int]:
    """Jeden przebieg: dla każdego usera z handle'ami zaproś brakujących. Zwraca statystyki.

    rooms  — zaproszenie + wiadomość „Dodano …" w tym samym pokoju.
    spaces — samo zaproszenie do przestrzeni (pokoje-dzieci wpuszczają członków space same, o ile
             mają regułę „restricted"); wiadomość idzie do notify_room, a bez niego nie ma jej wcale.
    """
    stats = {"invited": 0, "errors": 0}
    spaces = spaces or []
    targets = [(r, False) for r in rooms] + [(s, True) for s in spaces if s not in rooms]
    space_names: Dict[str, str] = {}
    current: set = set()  # wszystkie handle'e wpisane teraz w Authentiku (do zapominania usuniętych)

    async def label(room: str) -> str:
        """Nazwa pokoju/przestrzeni do komunikatów (cache na przebieg); fallback: ID."""
        if room not in space_names:
            space_names[room] = (await matrix.room_name(room)) or room
        return space_names[room]

    for user in await authentik.list_users():
        attrs = user.get("attributes") or {}
        raw = attrs.get("matrix_handles")
        if not raw:
            continue
        handles, invalid = normalize_matrix_handles(raw)
        current.update(handles)
        username = user["username"]
        msgs: List[str] = [f"niepoprawny handle: {h}" for h in invalid]

        # Użytkownik wpisuje textarea (string) — innym aplikacjom oddajemy porządną listę.
        if raw != handles and not dry_run:
            await authentik.update_attributes(user["pk"], {"matrix_handles": handles})

        for handle in handles:
            for room, is_space in targets:
                try:
                    action = invite_action(
                        await matrix.membership(room, handle), state.was_invited(handle, room)
                    )
                    if action == "banned":
                        msgs.append(f"{handle} ma bana w {await label(room)}")
                        if state.report_once(f"ban:{handle}:{room}"):
                            log.warning("%s zbanowany w %s", handle, room)
                    elif action == "left":
                        # Odrzucone zaproszenie wygląda w Matrixie tak samo jak wyjście z pokoju (leave).
                        # Nie ponawiamy; żeby zaprosić jeszcze raz: usuń handle z Authentika i dopisz z powrotem.
                        msgs.append(f"{handle} odrzucił zaproszenie lub wyszedł z: {await label(room)}")
                        if state.report_once(f"left:{handle}:{room}"):
                            log.info("%s odrzucił/wyszedł z %s (user %s)", handle, room, username)
                    elif action == "invite":
                        if dry_run:
                            log.info("[dry-run] zaprosiłbym %s do %s (user %s)", handle, room, username)
                            continue
                        await matrix.invite(room, handle)
                        state.mark_invited(handle, room)
                        stats["invited"] += 1
                        log.info("zaproszono %s do %s (user %s)", handle, room, username)
                        if not is_space:
                            await matrix.notice(
                                room, f"Dodano {handle} na prośbę {username}",
                                f"Dodano {user_link(handle)} na prośbę {html_escape(username)}",
                            )
                        elif notify_room:
                            sp = await label(room)
                            await matrix.notice(
                                notify_room,
                                f"Dodano {handle} do przestrzeni {sp} na prośbę {username}",
                                f"Dodano {user_link(handle)} do przestrzeni {html_escape(sp)} "
                                f"na prośbę {html_escape(username)}",
                            )
                except Exception as e:  # jeden zły handle/pokój nie wywala reszty
                    stats["errors"] += 1
                    msgs.append(f"{handle} → {room}: {_err(e)}")
                    if state.report_once(f"err:{handle}:{room}:{_err(e)}"):
                        log.error("invite %s → %s: %s", handle, room, _err(e))

        status = "; ".join(msgs) if msgs else "ok"
        if attrs.get("matrix_status") != status and not dry_run:
            await authentik.update_attributes(user["pk"], {"matrix_status": status})

    # Handle usunięty z Authentika = zapomnij o nim (skasuj i dopisz z powrotem → ponowne zaproszenie).
    # Dopiero po całej pętli, więc nie dotyczy przebiegu, który się wysypał w połowie.
    if not dry_run:
        gone = state.forget_missing(current)
        if gone:
            log.info("zapomniano handle'e usunięte z Authentika: %s", ", ".join(gone))
    return stats
