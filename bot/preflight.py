"""Sprawdza całą układankę przed pierwszym uruchomieniem: `python -m bot.preflight`.

Nic nie zapisuje. Każdy test wypisuje OK/BŁĄD z podpowiedzią, co poprawić.
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import List
from urllib.parse import quote

import httpx

REQUIRED_AUTHENTIK_PERMS = ("authentik_core.view_user", "authentik_core.change_user")


def _csv(name: str) -> List[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


def say(ok: bool, what: str, hint: str = "") -> bool:
    print(("OK    " if ok else "BŁĄD ") + what + ("" if ok or not hint else f"\n      → {hint}"))
    return ok


async def check_authentik(c: httpx.AsyncClient) -> bool:
    url, ok = os.environ.get("AUTHENTIK_URL", "").rstrip("/"), True
    if not url or not os.environ.get("AUTHENTIK_TOKEN"):
        return say(False, "Authentik: brak AUTHENTIK_URL / AUTHENTIK_TOKEN w .env")
    if "example.com" in url or "DOMENA" in url:
        return say(False, "Authentik: AUTHENTIK_URL to jeszcze placeholder z szablonu",
                   "wpisz prawdziwy adres Authentika w .env")
    h = {"Authorization": f"Bearer {os.environ['AUTHENTIK_TOKEN']}"}
    r = await c.get(f"{url}/api/v3/core/users/me/", headers=h)
    if not say(r.status_code == 200, f"Authentik: token działa (HTTP {r.status_code})",
               "token nieznany albo wygasły. Musi mieć Intent = API Token (Directory → Tokens and "
               "App passwords); token wyświetlony przy tworzeniu service-accounta to App password i nie działa"):
        return False
    who = r.json()["user"]
    say(True, f"Authentik: zalogowany jako {who['username']}")
    r = await c.get(f"{url}/api/v3/core/users/", headers=h, params={"page_size": 1})
    ok &= say(r.status_code == 200, f"Authentik: odczyt listy userów (HTTP {r.status_code})",
              "nadaj service-accountowi uprawnienie 'View user'")
    # Co zobaczy user w ustawieniach? Uruchamiamy flow z brandu i czytamy pola pierwszego etapu.
    r = await c.get(f"{url}/api/v3/core/brands/current/", headers=h)
    slug = r.json().get("flow_user_settings") if r.status_code == 200 else None
    if not slug:
        ok &= say(False, "Authentik: brand nie ma ustawionego flow user settings", "Admin → System → Brands")
        return ok
    r = await c.get(f"{url}/api/v3/flows/executor/{slug}/", headers=h)
    body = r.json() if r.status_code == 200 else {}
    if body.get("component") == "ak-stage-access-denied":
        ok &= say(False, f"Authentik: flow {slug} jest zepsuty — „{body.get('error_message')}”",
                  "ustawienia usera są teraz niedostępne dla wszystkich! Szybkie wyjście: Admin → System → Brands → "
                  "User settings flow = default-user-settings-flow. Potem sprawdź Admin → Flows → "
                  f"{slug} → Stage Bindings (ma być prompt order 20 i user write order 100)")
        return ok
    fields = [f.get("field_key") for f in (body.get("fields") or [])]
    wanted = ["attributes.matrix_handles", "attributes.telegram_username", "attributes.telegram_code"]
    missing = [w for w in wanted if w not in fields]
    ok &= say(not missing, f"Authentik: pola IM widoczne w ustawieniach usera (flow {slug})",
              "brak: " + ", ".join(missing) + " — zaaplikuj blueprint (Admin → Customization → Blueprints) "
              "albo dodaj prompty ręcznie (docs/DEPLOY.md, krok 2)")
    return ok


async def check_matrix(c: httpx.AsyncClient) -> bool:
    rooms, spaces = _csv("MATRIX_ROOMS"), _csv("MATRIX_SPACES")
    notify = os.environ.get("MATRIX_NOTIFY_ROOM", "").strip()
    if not rooms and not spaces:
        print("      (MATRIX_ROOMS i MATRIX_SPACES puste — Matrix pominięty)")
        return True
    base = os.environ.get("MATRIX_HOMESERVER", "").rstrip("/") + "/_matrix/client/v3"
    h = {"Authorization": f"Bearer {os.environ.get('MATRIX_TOKEN', '')}"}
    r = await c.get(f"{base}/account/whoami", headers=h)
    if not say(r.status_code == 200, f"Matrix: token działa (HTTP {r.status_code})",
               "sprawdź MATRIX_HOMESERVER i token (scripts/matrix-login.sh)"):
        return False
    me, ok = r.json()["user_id"], True
    say(True, f"Matrix: bot to {me}")
    for room in list(dict.fromkeys(rooms + spaces + ([notify] if notify else []))):  # bez duplikatów
        if room.startswith("#"):  # alias → ID (API stanu przyjmuje tylko ID)
            ra = await c.get(f"{base}/directory/room/{quote(room, safe='')}", headers=h)
            if ra.status_code != 200:
                ok &= say(False, f"Matrix {room}: nie da się rozwiązać aliasu (HTTP {ra.status_code})",
                          "sprawdź alias albo podaj ID pokoju (!abc:serwer)")
                continue
            say(True, f"Matrix {room}: alias → {ra.json()['room_id']}")
            room = ra.json()["room_id"]
        rq = quote(room, safe="")
        kind = "przestrzeń" if room in spaces else ("pokój powiadomień" if room == notify else "pokój")
        r = await c.get(f"{base}/rooms/{rq}/state/m.room.member/{quote(me, safe='')}", headers=h)
        joined = r.status_code == 200 and r.json().get("membership") == "join"
        ok &= say(joined, f"Matrix {room}: bot jest w ({kind})", "wejdź botem (zaproś go i zaakceptuj)")
        if not joined:
            continue
        if room in spaces:
            r = await c.get(f"{base}/rooms/{rq}/state/m.room.create", headers=h)
            is_space = r.status_code == 200 and r.json().get("type") == "m.space"
            ok &= say(is_space, f"Matrix {room}: to jest przestrzeń (m.space)",
                      "w MATRIX_SPACES podaj ID PRZESTRZENI, zwykły pokój daj do MATRIX_ROOMS")
        if room == notify and room not in rooms + spaces:
            r = await c.get(f"{base}/rooms/{rq}/state/m.room.power_levels", headers=h)
            pl = r.json() if r.status_code == 200 else {}
            mine = pl.get("users", {}).get(me, pl.get("users_default", 0))
            ok &= say(mine >= pl.get("events", {}).get("m.room.message", pl.get("events_default", 0)),
                      f"Matrix {room}: bot może pisać w pokoju powiadomień", "podnieś power level bota")
            continue
        r = await c.get(f"{base}/rooms/{rq}/state/m.room.power_levels", headers=h)
        pl = r.json() if r.status_code == 200 else {}
        mine = pl.get("users", {}).get(me, pl.get("users_default", 0))
        need = pl.get("invite", 0)
        ok &= say(mine >= need, f"Matrix {room}: power level bota {mine} ≥ wymagany do invite {need}",
                  "podnieś bota (np. Moderator) w ustawieniach pokoju")
        r = await c.get(f"{base}/rooms/{rq}/state/m.room.encryption", headers=h)
        say(r.status_code == 404, f"Matrix {room}: pokój niezaszyfrowany" if r.status_code == 404
            else f"Matrix {room}: pokój JEST zaszyfrowany — bot nie wyśle wiadomości 'Dodano…' (invite zadziała)")
    return ok


async def check_telegram(c: httpx.AsyncClient) -> bool:
    tok = os.environ.get("TELEGRAM_TOKEN")
    if not tok:
        print("      (TELEGRAM_TOKEN puste — Telegram pominięty)")
        return True
    base = f"https://api.telegram.org/bot{tok}"
    r = await c.get(f"{base}/getMe")
    if not say(r.status_code == 200, f"Telegram: token działa (HTTP {r.status_code})", "weź token od @BotFather"):
        return False
    me, ok = r.json()["result"], True
    say(True, f"Telegram: bot @{me['username']}")
    for g in _csv("TELEGRAM_GROUPS"):
        r = await c.get(f"{base}/getChatMember", params={"chat_id": g, "user_id": me["id"]})
        res = r.json().get("result", {}) if r.status_code == 200 else {}
        can = res.get("status") == "creator" or (res.get("status") == "administrator" and res.get("can_invite_users"))
        ok &= say(bool(can), f"Telegram {g}: bot jest adminem z prawem zapraszania",
                  "dodaj bota do grupy jako admina z 'Invite users'")
    return ok


async def _safe(name: str, check, c: httpx.AsyncClient) -> bool:
    """Błąd sieci w jednym teście nie zabija reszty i nie sypie tracebackiem."""
    try:
        return await check(c)
    except httpx.RequestError as e:
        return say(False, f"{name}: nie mogę się połączyć ({type(e).__name__}: {e})",
                   "sprawdź adres w .env (placeholder z szablonu? literówka? DNS?)")


async def amain() -> int:
    async with httpx.AsyncClient(timeout=20) as c:
        results = [
            await _safe("Authentik", check_authentik, c),
            await _safe("Matrix", check_matrix, c),
            await _safe("Telegram", check_telegram, c),
        ]
    print("\nWszystko gotowe." if all(results) else "\nSą błędy — popraw i uruchom ponownie.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
