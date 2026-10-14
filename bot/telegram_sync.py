"""Telegram: weryfikacja konta i zapis `telegram_id` do atrybutów usera w Authentiku.

Dwie ścieżki (Authentik nie umie pokazać usera tokenu wygenerowanego przez bota, więc
kierunek jest: bot → user → Authentik):

A) User wpisał w Authentiku `telegram_username`, potem pisze do bota /start. Telegram sam
   gwarantuje, że username jest jego, więc bot pyta tylko "czy to konto Authentik X to Ty?"
   (przycisk — chroni przed tym, że ktoś wpisał CUDZY @handle) i zapisuje id.
B) Brak username albo brak wniosku: bot daje jednorazowy kod (15 min). User wkleja go w polu
   `telegram_code` w Authentiku; przy najbliższym przebiegu bot dopasowuje kod i zapisuje id.

Po weryfikacji bot wysyła w DM jednorazowe linki do grup (bot nie może dodać usera do
grupy po id — to ograniczenie API Telegrama).
"""
from __future__ import annotations

import logging
import time
from typing import Any, List, Optional, Tuple

from .handles import find_pending_tg_users

log = logging.getLogger("telegram")


class TelegramLogic:
    def __init__(self, authentik: Any, state: Any, groups: List[int], bot: Any = None,
                 settings_url: str = ""):
        self.authentik = authentik
        self.state = state
        self.groups = groups
        self.bot = bot  # telegram.Bot, ustawiany po zbudowaniu Application
        self.settings_url = settings_url  # np. https://auth.example.com/if/user/#/settings

    def _settings_line(self, prefix: str = "Ustawienia w Authentiku: ") -> str:
        return f"\n{prefix}{self.settings_url}" if self.settings_url else ""

    def group_help(self, bot_username: Optional[str] = None) -> str:
        """Odpowiedź na komendę wysłaną na grupie: komendy działają tylko w prywatnej rozmowie z botem."""
        me = f"@{bot_username}" if bot_username else "mnie"
        return (
            f"Moje komendy działają tylko w prywatnej rozmowie. Napisz do {me} na priv:\n"
            "• /start — łączy Twoje konto Telegram z kontem w Authentiku (najpierw wpisz swój @username "
            "w ustawieniach Authentika), a potem wysyła Ci jednorazowe linki do grup.\n"
            "• /grupy — wysyła świeże, jednorazowe linki do grup (tylko dla kont już połączonych przez /start)."
            + self._settings_line()
        )
    # --- czysta decyzja dla /start (testowalna bez Telegrama) ---
    async def start_reply(self, tg_id: int, tg_username: Optional[str]) -> Tuple[str, Optional[int]]:
        """Zwraca (tekst, pk usera do potwierdzenia albo None)."""
        users = await self.authentik.list_users()
        for u in users:
            if str((u.get("attributes") or {}).get("telegram_id")) == str(tg_id):
                return f"To konto Telegram jest już połączone z kontem Authentik „{u['username']}”.", None
        if tg_username:
            matches = find_pending_tg_users(users, tg_username)
            if len(matches) == 1:
                u = matches[0]
                return (
                    f"Konto Authentik „{u['username']}” prosi o połączenie z tym kontem Telegram "
                    f"(@{tg_username}). To Ty?",
                    u["pk"],
                )
            if len(matches) > 1:
                return "Ten @username wpisało kilka kont Authentik — napisz do admina.", None
        code = self.state.new_tg_code(tg_id)
        return (
            "Nie widzę oczekującego wniosku dla tego konta.\n"
            "Opcja 1: wpisz swój @username w Authentiku i wyślij mi /start jeszcze raz.\n"
            f"Opcja 2 (gdy nie masz username): wklej w Authentiku w polu „Kod z Telegrama” kod "
            f"{code} (ważny 15 minut)." + self._settings_line(),
            None,
        )

    async def link(self, pk: int, tg_id: int, users: Optional[List[dict]] = None) -> Tuple[bool, str]:
        """Zapisuje telegram_id (jeśli to id nie należy już do kogoś innego) i czyści kod."""
        users = users if users is not None else await self.authentik.list_users()
        for u in users:
            if u["pk"] != pk and str((u.get("attributes") or {}).get("telegram_id")) == str(tg_id):
                await self.authentik.update_attributes(
                    pk, {"telegram_status": "to konto Telegram jest już użyte przez innego usera"},
                    remove=["telegram_code"],
                )
                return False, "To konto Telegram jest już połączone z innym użytkownikiem."
        await self.authentik.update_attributes(
            pk, {"telegram_id": int(tg_id), "telegram_status": "ok"}, remove=["telegram_code"]
        )
        log.info("połączono authentik pk=%s z telegram id=%s", pk, tg_id)
        return True, "Połączone ✅"

    async def send_group_links(self, tg_id: int) -> int:
        """Wysyła w DM „Nazwa grupy: link" (jednorazowe, 24 h). Zwraca liczbę wysłanych linków.

        Jedna grupa, która zawiedzie (bot bez uprawnień, grupa usunięta), nie blokuje reszty;
        user dostaje listę grup, do których linku nie udało się wygenerować.
        """
        if not self.bot or not self.groups:
            return 0
        lines, failed = [], []
        for chat in self.groups:
            try:
                title = (await self.bot.get_chat(chat)).title or str(chat)
            except Exception:
                title = str(chat)
            try:
                link = await self.bot.create_chat_invite_link(
                    chat, member_limit=1, expire_date=int(time.time()) + 24 * 3600
                )
                lines.append(f"• {title}: {link.invite_link}")
            except Exception as e:
                log.error("invite link dla grupy %s: %s", chat, e)
                failed.append(title)
        text = ""
        if lines:
            text = ("Linki do grup (jednorazowe, ważne 24 h):\n" + "\n".join(lines)
                    + "\n\nNowe linki w każdej chwili: /grupy")
        if failed:
            text += ("\n\n" if text else "") + "Nie udało się wygenerować linku do: " + ", ".join(failed) + \
                    ". Napisz do admina."
        if text:
            await self.bot.send_message(tg_id, text)
        return len(lines)

    async def groups_reply(self, tg_id: int) -> Tuple[bool, str]:
        """/grupy: (czy wysłać linki, tekst gdy nie). Tylko dla połączonych userów."""
        users = await self.authentik.list_users()
        if not any(str((u.get("attributes") or {}).get("telegram_id")) == str(tg_id) for u in users):
            return False, ("To konto Telegram nie jest jeszcze połączone z Authentikiem. Wpisz swój @username "
                           "w ustawieniach Authentika, a potem wyślij mi /start." + self._settings_line())
        if not self.groups:
            return False, "Nie ma skonfigurowanych grup."
        return True, ""

    async def poll_codes(self) -> int:
        """Ścieżka B: userzy, którzy wkleili kod w Authentiku. Zwraca liczbę połączonych."""
        users = await self.authentik.list_users()
        linked = 0
        for u in users:
            attrs = u.get("attributes") or {}
            code = attrs.get("telegram_code")
            if not code:
                continue
            if attrs.get("telegram_id"):
                # Jedno konto Telegram na usera. Bez tego komunikatu kod leżałby w polu bez śladu.
                await self.authentik.update_attributes(
                    u["pk"],
                    {"telegram_status": "to konto Authentik ma już połączony Telegram — poproś admina o odłączenie, potem wklej kod ponownie"},
                    remove=["telegram_code"],
                )
                continue
            tg_id = self.state.consume_tg_code(str(code))
            log.info("kod od usera %s (authentik pk=%s): %s", u["username"], u["pk"],
                     f"pasuje do tg id={tg_id}" if tg_id is not None else "nieznany albo wygasły")
            if tg_id is None:
                await self.authentik.update_attributes(
                    u["pk"], {"telegram_status": "zły lub wygasły kod"}, remove=["telegram_code"]
                )
                continue
            ok, msg = await self.link(u["pk"], tg_id, users)
            if ok:
                linked += 1
                if self.bot:
                    await self.bot.send_message(tg_id, f"Połączono z kontem Authentik „{u['username']}” ✅")
                    await self.send_group_links(tg_id)
        return linked

    # --- handlery python-telegram-bot (cienkie) ---
    async def on_start(self, update: Any, context: Any) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        if update.effective_chat.type != "private":
            await self._reply_in_group(update, context, "/start")
            return
        u = update.effective_user
        text, pk = await self.start_reply(u.id, u.username)
        log.info("/start od tg id=%s username=%s -> %s", u.id, u.username,
                 f"pytanie o potwierdzenie (authentik pk={pk})" if pk is not None else "bez dopasowania")
        markup = None
        if pk is not None:
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton("Tak, to ja", callback_data=f"link:{pk}"),
                  InlineKeyboardButton("Nie", callback_data="cancel")]]
            )
        await update.message.reply_text(text, reply_markup=markup)

    async def _reply_in_group(self, update: Any, context: Any, cmd: str) -> None:
        """Komenda na grupie (także /komenda@nazwabota): wyjaśnij, że trzeba pisać na priv.

        Tylko na grupach z TELEGRAM_GROUPS. Na grupie, do której ktoś losowo dodał bota, milczymy
        (żadnej instrukcji, żadnego śladu poza logiem), żeby nie zdradzać, do czego służy.
        """
        if update.effective_chat.id not in self.groups:
            log.debug("%s na obcej grupie %s: ignoruję", cmd, update.effective_chat.id)
            return
        log.info("%s na czacie %s (%s) od tg id=%s: odsyłam instrukcję", cmd, update.effective_chat.id,
                 update.effective_chat.type, getattr(update.effective_user, "id", None))
        await update.message.reply_text(self.group_help(getattr(context.bot, "username", None)))

    async def on_groups(self, update: Any, context: Any) -> None:
        if update.effective_chat.type != "private":
            await self._reply_in_group(update, context, "/grupy")
            return
        u = update.effective_user
        send, text = await self.groups_reply(u.id)
        log.info("/grupy od tg id=%s -> %s", u.id, "wysyłam linki" if send else "odmowa")
        if send:
            await self.send_group_links(u.id)
        else:
            await update.message.reply_text(text)

    async def on_button(self, update: Any, context: Any) -> None:
        q = update.callback_query
        await q.answer()
        if not q.data.startswith("link:"):
            await q.edit_message_text("Anulowano.")
            return
        pk = int(q.data.split(":", 1)[1])
        u = q.from_user
        log.info("potwierdzenie od tg id=%s username=%s dla authentik pk=%s", u.id, u.username, pk)
        users = await self.authentik.list_users()
        # Powtórna weryfikacja w chwili kliknięcia: wniosek nadal istnieje i pasuje do username.
        pending = [m for m in find_pending_tg_users(users, u.username or "") if m["pk"] == pk]
        if not pending:
            await q.edit_message_text("Ten wniosek już nie istnieje albo nie pasuje do Twojego konta.")
            return
        ok, msg = await self.link(pk, u.id, users)
        await q.edit_message_text(msg)
        if ok:
            await self.send_group_links(u.id)
