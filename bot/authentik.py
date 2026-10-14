"""Klient Authentik API: lista userów i zapis atrybutów."""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional

import httpx


class Authentik:
    def __init__(self, base_url: str, token: str, client: Optional[httpx.AsyncClient] = None):
        self.base = base_url.rstrip("/")
        self.client = client or httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, timeout=20
        )

    async def list_users(self, include_inactive: bool = False) -> List[Dict[str, Any]]:
        users: List[Dict[str, Any]] = []
        page = 1
        while page:
            r = await self.client.get(
                f"{self.base}/api/v3/core/users/",
                params={"page": page, "page_size": 200, "include_groups": "false"},
            )
            r.raise_for_status()
            body = r.json()
            users.extend(u for u in body["results"] if include_inactive or u.get("is_active", True))
            page = body.get("pagination", {}).get("next", 0)
        return users

    async def update_attributes(
        self, pk: int, set_: Optional[Dict[str, Any]] = None, remove: Iterable[str] = (),
        mutate: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        """PATCH na /core/users/ podmienia CAŁE `attributes`, więc najpierw pobieramy świeżą
        wersję i scalamy — inaczej zgubilibyśmy atrybuty innych aplikacji.

        mutate(attrs) działa na świeżo wczytanych atrybutach tuż przed zapisem. Używaj go do list, które
        piszą też inni (np. `matrixInviteRequests` dopisywane przez usermanager w trakcie przebiegu bota):
        wartość policzona ze starej kopii nadpisałaby cudze zmiany."""
        url = f"{self.base}/api/v3/core/users/{pk}/"
        r = await self.client.get(url)
        r.raise_for_status()
        attrs = dict(r.json().get("attributes") or {})
        attrs.update(set_ or {})
        for key in remove:
            attrs.pop(key, None)
        if mutate:
            mutate(attrs)
        r = await self.client.patch(url, json={"attributes": attrs})
        r.raise_for_status()
