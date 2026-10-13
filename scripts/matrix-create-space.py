#!/usr/bin/env python3
"""Zakłada prywatną przestrzeń Matrix z jednym pokojem-dzieckiem (do testów albo na start).

Dziecko ma regułę join „restricted": członkowie przestrzeni wchodzą do niego sami, więc bot
musi zaprosić tylko do przestrzeni. Użycie (zmienne z .env: MATRIX_HOMESERVER, MATRIX_TOKEN):

    set -a; . ./.env; set +a
    python3 scripts/matrix-create-space.py "im-sync test" "im-sync test - pokój"

Wypisuje ID przestrzeni (do MATRIX_SPACES) i dziecka. Skasować: wyjść z obu (Element → Leave).
"""
import os
import sys
import urllib.parse

import httpx

name, child_name = (sys.argv[1:3] + ["", ""])[:2]
if not name or not child_name:
    sys.exit(__doc__)

base = os.environ["MATRIX_HOMESERVER"].rstrip("/") + "/_matrix/client/v3"
c = httpx.Client(headers={"Authorization": "Bearer " + os.environ["MATRIX_TOKEN"]}, timeout=30)


def ok(r):
    if r.status_code != 200:
        sys.exit(f"BŁĄD HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


me = ok(c.get(base + "/account/whoami"))["user_id"]
server = me.split(":", 1)[1]

space = ok(c.post(base + "/createRoom", json={
    "name": name, "visibility": "private", "preset": "private_chat",
    "creation_content": {"type": "m.space"},
    "power_level_content_override": {"events_default": 100, "invite": 50},
}))["room_id"]

child = ok(c.post(base + "/createRoom", json={
    "name": child_name, "visibility": "private", "preset": "private_chat", "room_version": "10",
    "initial_state": [
        {"type": "m.room.join_rules", "state_key": "",
         "content": {"join_rule": "restricted", "allow": [{"type": "m.room_membership", "room_id": space}]}},
        {"type": "m.space.parent", "state_key": space, "content": {"via": [server], "canonical": True}},
    ],
}))["room_id"]

ok(c.put(f"{base}/rooms/{urllib.parse.quote(space, safe='')}/state/m.space.child/{urllib.parse.quote(child, safe='')}",
         json={"via": [server]}))

print(f"MATRIX_SPACES={space}")
print(f"# dziecko (restricted do tej przestrzeni): {child}")
