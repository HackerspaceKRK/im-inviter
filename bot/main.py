"""Punkt wejścia: pętla pollingowa (Authentik → Matrix) + long polling Telegrama."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from typing import List

from .authentik import Authentik
from .matrix_sync import Matrix, run_matrix_cycle
from .migrate import INVENTORY_EVERY, run_migration
from .state import State
from .telegram_sync import TelegramLogic, parse_tg_chats

log = logging.getLogger("main")


def _csv(name: str) -> List[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


async def amain(dry_run: bool, once: bool, audit: bool = False) -> None:
    authentik = Authentik(os.environ["AUTHENTIK_URL"], os.environ["AUTHENTIK_TOKEN"])
    state = State(os.environ.get("DB_PATH", "/data/state.db"))
    interval = int(os.environ.get("POLL_INTERVAL", "60"))

    rooms = _csv("MATRIX_ROOMS")
    spaces = _csv("MATRIX_SPACES")
    notify_room = os.environ.get("MATRIX_NOTIFY_ROOM", "").strip() or None
    pending_after = float(os.environ.get("MATRIX_PENDING_AFTER_HOURS", "24"))
    matrix = (
        Matrix(os.environ["MATRIX_HOMESERVER"], os.environ["MATRIX_TOKEN"],
               msgtype=os.environ.get("MATRIX_MSGTYPE", "m.text").strip() or "m.text")
        if rooms or spaces else None
    )

    tg_logic = None
    tg_app = None
    if os.environ.get("TELEGRAM_TOKEN") and not dry_run:
        from telegram.ext import (Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler, MessageHandler,
                                  filters)

        chats = parse_tg_chats(os.environ)
        log.info("Telegram: grupy members=%s (tagi), grupy publiczne=%s, kanały=%s", chats.members or "brak",
                 chats.public or "brak", chats.channels or "brak")
        if chats.legacy:
            log.warning("TELEGRAM_GROUPS jest przestarzałe (czaty %s traktuję jak publiczne: linki i komendy, BEZ tagów). "
                        "Przenieś je do TELEGRAM_MEMBERS_GROUPS / TELEGRAM_PUBLIC_GROUPS / TELEGRAM_CHANNELS.", chats.legacy)
        settings_url = os.environ["AUTHENTIK_URL"].rstrip("/") + "/if/user/#/settings"
        # Tag „Verified” przy nicku na grupach (puste TELEGRAM_MEMBER_TAG wyłącza). Max 16 znaków, bez emoji.
        member_tag = os.environ.get("TELEGRAM_MEMBER_TAG", "Verified")
        if len(member_tag.strip()) > 16:  # limit Telegrama; dłuższy tag tylko generowałby błędy
            log.error("TELEGRAM_MEMBER_TAG ma ponad 16 znaków: tagi wyłączone")
            member_tag = ""
        # Dopisek do tagu dla wygasłego/nieaktywnego konta (Verified*); pusty = wyłączony
        suffix = os.environ.get("TELEGRAM_EXPIRED_TAG_SUFFIX", "*")
        if len((member_tag + suffix).strip()) > 16:
            log.error("TELEGRAM_MEMBER_TAG + TELEGRAM_EXPIRED_TAG_SUFFIX ma ponad 16 znaków: dopisek wyłączony")
            suffix = ""
        tg_logic = TelegramLogic(authentik, state, chats.all, settings_url=settings_url, member_tag=member_tag,
                                 expired_suffix=suffix, tag_groups=chats.tag)
        tg_app = Application.builder().token(os.environ["TELEGRAM_TOKEN"]).build()
        tg_logic.bot = tg_app.bot
        tg_app.add_handler(CommandHandler("start", tg_logic.on_start))
        tg_app.add_handler(CommandHandler("grupy", tg_logic.on_groups))
        tg_app.add_handler(CommandHandler("verify", tg_logic.on_verify))
        tg_app.add_handler(CallbackQueryHandler(tg_logic.on_button))
        tg_app.add_handler(ChatMemberHandler(tg_logic.on_chat_member, ChatMemberHandler.CHAT_MEMBER))
        # group=1: działa obok komend (/start na grupie też jest wiadomością), nie zamiast nich
        tg_app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.UpdateType.MESSAGE & ~filters.StatusUpdate.ALL,
                                          tg_logic.on_group_message), group=1)
        await tg_app.initialize()
        await tg_app.bot.set_my_commands([
            ("start", "Połącz konto z Authentikiem"),
            ("grupy", "Wyślij mi świeże linki do grup"),
            ("verify", "Potwierdź swoje konto (bez linków)"),
        ])
        await tg_app.start()
        # chat_member (wejście do grupy) nie przychodzi domyślnie: trzeba o nie poprosić
        await tg_app.updater.start_polling(allowed_updates=["message", "callback_query", "chat_member"])
    elif os.environ.get("TELEGRAM_TOKEN"):
        log.info("dry-run: Telegram wyłączony")

    last_migration = 0.0
    try:
        while True:
            try:
                # Przegląd atrybutów (stare nazwy → camelCase, scalenie, sprzątanie): na starcie i co godzinę,
                # bo stary blueprint/flow może jeszcze zapisać stare klucze.
                if time.time() - last_migration >= INVENTORY_EVERY:
                    last_migration = time.time()
                    try:
                        await run_migration(authentik, dry_run, audit)
                    except Exception:
                        log.exception("migracja atrybutów nie powiodła się (bot działa dalej)")
                if matrix:
                    log.info("matrix: %s", await run_matrix_cycle(
                        authentik, matrix, state, rooms, dry_run, spaces, notify_room, pending_after))
                if tg_logic:
                    await tg_logic.poll_codes()
                    await tg_logic.poll_invites()
                    await tg_logic.maybe_review_chats()
                    await tg_logic.backfill_tags()
            except Exception:
                log.exception("przebieg nie powiódł się, próbuję ponownie za %ss", interval)
            if once:
                break
            await asyncio.sleep(interval)
    finally:
        if tg_app:
            await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="tylko loguj, nic nie wysyłaj ani nie zapisuj")
    p.add_argument("--once", action="store_true", help="jeden przebieg i wyjście")
    p.add_argument("--audit", action="store_true",
                   help="w logu migracji wypisz pełny inwentarz atrybutów (klucz×userów); tylko odczyt")
    a = p.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(message)s")
    # httpx loguje pełne URL-e na INFO, a w URL-ach Telegrama jest token bota: nigdy do logów.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    asyncio.run(amain(a.dry_run, a.once, a.audit))


if __name__ == "__main__":
    main()
