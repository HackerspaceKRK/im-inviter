"""Punkt wejścia: pętla pollingowa (Authentik → Matrix) + long polling Telegrama."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from typing import List

from .authentik import Authentik
from .matrix_sync import Matrix, run_matrix_cycle
from .state import State
from .telegram_sync import TelegramLogic

log = logging.getLogger("main")


def _csv(name: str) -> List[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


async def amain(dry_run: bool, once: bool) -> None:
    authentik = Authentik(os.environ["AUTHENTIK_URL"], os.environ["AUTHENTIK_TOKEN"])
    state = State(os.environ.get("DB_PATH", "/data/state.db"))
    interval = int(os.environ.get("POLL_INTERVAL", "60"))

    rooms = _csv("MATRIX_ROOMS")
    matrix = (
        Matrix(os.environ["MATRIX_HOMESERVER"], os.environ["MATRIX_TOKEN"]) if rooms else None
    )

    tg_logic = None
    tg_app = None
    if os.environ.get("TELEGRAM_TOKEN") and not dry_run:
        from telegram.ext import Application, CallbackQueryHandler, CommandHandler

        groups = [int(g) for g in _csv("TELEGRAM_GROUPS")]
        tg_logic = TelegramLogic(authentik, state, groups)
        tg_app = Application.builder().token(os.environ["TELEGRAM_TOKEN"]).build()
        tg_logic.bot = tg_app.bot
        tg_app.add_handler(CommandHandler("start", tg_logic.on_start))
        tg_app.add_handler(CallbackQueryHandler(tg_logic.on_button))
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling()
    elif os.environ.get("TELEGRAM_TOKEN"):
        log.info("dry-run: Telegram wyłączony")

    try:
        while True:
            try:
                if matrix:
                    log.info("matrix: %s", await run_matrix_cycle(authentik, matrix, state, rooms, dry_run))
                if tg_logic:
                    await tg_logic.poll_codes()
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
    a = p.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(message)s")
    asyncio.run(amain(a.dry_run, a.once))


if __name__ == "__main__":
    main()
