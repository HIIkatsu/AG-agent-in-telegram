"""Bot entry point — logging, DB, native command menu, polling (Forum Topics edition)."""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand

from bot.config import settings
from bot.db import db
from bot.middleware import AuthMiddleware

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/opt/antigravity-bot/logs/bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


async def _set_commands(bot: Bot) -> None:
    """Register native Telegram /command menu."""
    commands = [
        BotCommand(command="mount", description="Привязать директорию проекта"),
        BotCommand(command="pwd", description="Показать рабочую директорию"),
        BotCommand(command="settings", description="Настройки ветки"),
        BotCommand(command="web", description="Вкл/выкл веб-поиск"),
        BotCommand(command="model", description="Выбор модели"),
        BotCommand(command="stats", description="Статистика сессий"),
        BotCommand(command="help", description="Справка"),
        BotCommand(command="cleanup", description="Очистить битые сессии CLI"),
    ]
    await bot.set_my_commands(commands)
    logger.info("Bot commands registered")


async def main() -> None:
    logger.info("Connecting to database ...")
    await db.connect()

    # Purge broken CLI sessions (e.g. old invalid-UUID dirs) at startup
    from bot.handlers.chats import purge_stale_cli_sessions
    purged = purge_stale_cli_sessions()
    if purged:
        logger.info("Startup cleanup: purged %d broken CLI sessions", purged)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=None),
    )

    dp = Dispatcher()

    # Apply auth middleware to both messages and callbacks
    auth = AuthMiddleware()
    dp.message.middleware(auth)
    dp.callback_query.middleware(auth)

    # Register native /command menu
    await _set_commands(bot)

    # Import and include routers (order matters!)
    from bot.handlers.start import router as start_router
    from bot.handlers.chats import router as chats_router
    from bot.handlers.callbacks import router as callbacks_router
    from bot.handlers.message import router as message_router

    dp.include_router(start_router)
    dp.include_router(chats_router)
    dp.include_router(callbacks_router)
    dp.include_router(message_router)  # catch-all MUST be last

    logger.info(
        "Bot starting (polling) ... FORUM_GROUP_ID=%s",
        settings.forum_group_id or "NOT SET",
    )
    try:
        await dp.start_polling(bot)
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
