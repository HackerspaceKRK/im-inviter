"""Matrix: zapraszanie kont z atrybutu `matrix_handles` do pokoi z configu."""
from __future__ import annotations

import logging
import uuid
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

    async def invite(self, room: str, mxid: str) -> None:
        r = await self.client.post(
            f"{self.base}/rooms/{quote(room, safe='')}/invite", json={"user_id": mxid}
        )
        r.raise_for_status()

    async def notice(self, room: str, text: str) -> None:
        r = await self.client.put(
            f"{self.base}/rooms/{quote(room, safe='')}/send/m.room.message/{uuid.uuid4().hex}",
            json={"msgtype": "m.notice", "body": text},
        )
        r.raise_for_status()


def _err(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        try:
            return f"{e.response.status_code} {e.response.json().get('error', '')}".strip()
        except Exception:
            return str(e.response.status_code)
    return str(e)


async def run_matrix_cycle(
    authentik: Any, matrix: Matrix, state: Any, rooms: List[str], dry_run: bool = False
) -> Dict[str, int]:
    """Jeden przebieg: dla każdego usera z handle'ami zaproś brakujących. Zwraca statystyki."""
    stats = {"invited": 0, "errors": 0}
    for user in await authentik.list_users():
        attrs = user.get("attributes") or {}
        raw = attrs.get("matrix_handles")
        if not raw:
            continue
        handles, invalid = normalize_matrix_handles(raw)
        username = user["username"]
        msgs: List[str] = [f"niepoprawny handle: {h}" for h in invalid]

        # Użytkownik wpisuje textarea (string) — innym aplikacjom oddajemy porządną listę.
        if raw != handles and not dry_run:
            await authentik.update_attributes(user["pk"], {"matrix_handles": handles})

        for handle in handles:
            for room in rooms:
                try:
                    action = invite_action(
                        await matrix.membership(room, handle), state.was_invited(handle, room)
                    )
                    if action == "banned":
                        msgs.append(f"{handle} ma bana w {room}")
                        if state.report_once(f"ban:{handle}:{room}"):
                            log.warning("%s zbanowany w %s", handle, room)
                    elif action == "invite":
                        if dry_run:
                            log.info("[dry-run] zaprosiłbym %s do %s (user %s)", handle, room, username)
                            continue
                        await matrix.invite(room, handle)
                        state.mark_invited(handle, room)
                        stats["invited"] += 1
                        log.info("zaproszono %s do %s (user %s)", handle, room, username)
                        await matrix.notice(room, f"Dodano {handle} na prośbę {username}")
                except Exception as e:  # jeden zły handle/pokój nie wywala reszty
                    stats["errors"] += 1
                    msgs.append(f"{handle} → {room}: {_err(e)}")
                    if state.report_once(f"err:{handle}:{room}:{_err(e)}"):
                        log.error("invite %s → %s: %s", handle, room, _err(e))

        status = "; ".join(msgs) if msgs else "ok"
        if attrs.get("matrix_status") != status and not dry_run:
            await authentik.update_attributes(user["pk"], {"matrix_status": status})
    return stats
