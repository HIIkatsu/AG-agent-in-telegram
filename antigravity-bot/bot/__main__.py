"""Bot entry point — logging, DB, native command menu, polling (Forum Topics edition)."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import uvloop
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand

from bot.config import settings
from bot.db import db
from bot.middleware import AuthMiddleware

log_handlers = [logging.StreamHandler(sys.stdout)]
log_file_path = "/opt/antigravity-bot/logs/bot.log"
try:
    if not os.path.exists(os.path.dirname(log_file_path)):
        os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
    log_handlers.append(logging.FileHandler(log_file_path, encoding="utf-8"))
except OSError:
    pass

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=log_handlers,
)
logger = logging.getLogger(__name__)


def _configure_native_memory_tools() -> bool:
    """Register native memory tools without making the bot unavailable."""
    from pathlib import Path

    from bot.services.instructions import BOT_ROOT
    from bot.services.memory_mcp_config import (
        MemoryMcpConfigError,
        ensure_memory_mcp_config,
    )

    try:
        memory_mcp_report = ensure_memory_mcp_config(
            config_path=Path(settings.agy_mcp_config_path),
            python_executable=Path(sys.executable),
            bot_root=BOT_ROOT,
            db_path=settings.db_path,
        )
    except MemoryMcpConfigError as exc:
        logger.warning(
            "AGY native memory tools unavailable; bot will continue without them: %s",
            exc,
        )
        return False

    logger.info(
        "AGY native memory tools ready: state=%s config=%s",
        memory_mcp_report.state,
        memory_mcp_report.config_path,
    )
    return True


def check_startup() -> None:
    """Validate environment before starting."""
    import shutil
    
    # Check bot token
    if not settings.bot_token or settings.bot_token == "YOUR_BOT_TOKEN_HERE":
        logger.error("bot_token is not set or invalid in .env")
        sys.exit(1)
        
    # Check agy CLI
    agy_path = settings.agy_path
    if not os.path.exists(agy_path):
        if not shutil.which("agy"):
            logger.error("agy CLI not found at %s and not in PATH.", agy_path)
            sys.exit(1)
        else:
            logger.info("agy found in PATH")
            
    # Register bundled skills in AGY's user-level global skill directory.
    # This deliberately never writes into a mounted project's .agents folder.
    from pathlib import Path

    from bot.services.skill_registry import SkillRegistryError, ensure_global_skills

    try:
        skill_report = ensure_global_skills(
            target_dir=Path(settings.agy_global_skills_dir),
        )
    except SkillRegistryError as exc:
        logger.error("Cannot register bundled AGY skills: %s", exc)
        sys.exit(1)
    logger.info(
        "Bundled AGY skills ready: available=%d installed=%d updated=%d removed=%d",
        skill_report.available_count,
        len(skill_report.installed),
        len(skill_report.updated),
        len(skill_report.removed),
    )

    # This integration is optional: an invalid third-party MCP config must not
    # take down the Telegram bot or risk being overwritten during startup.
    _configure_native_memory_tools()

    # Check source and isolated task workspace roots.
    for workspace_root in (settings.workspaces_dir, settings.task_workspaces_dir):
        try:
            os.makedirs(workspace_root, exist_ok=True)
        except OSError as e:
            logger.error("Cannot create workspace directory %s: %s", workspace_root, e)
            sys.exit(1)


async def _set_commands(bot: Bot) -> None:
    """Register native Telegram /command menu."""
    commands = [
        BotCommand(command="project", description="Дашборд и настройки проекта"),
        BotCommand(command="files", description="Файловый менеджер проекта"),
        BotCommand(command="search", description="Поиск по проекту"),
        BotCommand(command="context", description="Контекст задачи"),
        BotCommand(command="memory", description="Память: проект и глобальная"),
        BotCommand(command="diff", description="Diff workflow"),
        BotCommand(command="test", description="Запустить тесты"),
        BotCommand(command="run", description="Запустить команду"),
        BotCommand(command="queue", description="Очередь задач"),
        BotCommand(command="status", description="Статус задачи"),
        BotCommand(command="cancel", description="Отменить текущую задачу"),
        BotCommand(command="git", description="История коммитов"),
        BotCommand(command="stats", description="Подробная статистика и лимиты"),
        BotCommand(command="settings", description="Глобальные настройки (Мастер-панель)"),
    ]
    await bot.set_my_commands(commands)
    logger.info("Bot commands registered")


async def main() -> None:
    check_startup()
    logger.info("Connecting to database ...")
    await db.connect()

    # Purge broken CLI sessions (e.g. old invalid-UUID dirs) at startup.
    # The cleanup is restricted to the CLI's brain directory and never touches
    # global skills or other AGY state.
    from bot.handlers.chats import purge_stale_cli_sessions
    purged = purge_stale_cli_sessions()
    if purged:
        logger.info("Startup cleanup: purged %d broken CLI sessions", purged)
        
    from bot.services.task_service import recovery_interrupted_tasks
    await recovery_interrupted_tasks()
    from bot.services.task_workspace import task_workspace_manager
    await task_workspace_manager.recover()

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
    from bot.handlers.callbacks import router as callbacks_router
    from bot.handlers.chats import router as chats_router
    from bot.handlers.dashboard import router as dashboard_router
    from bot.handlers.environments import router as environments_router
    from bot.handlers.files import router as files_router
    from bot.handlers.git_ui import router as git_router
    from bot.handlers.ide import router as ide_router
    from bot.handlers.memory import router as memory_router
    from bot.handlers.message import router as message_router
    from bot.handlers.settings import router as settings_router
    from bot.handlers.start import router as start_router

    dp.include_router(start_router)
    dp.include_router(chats_router)
    dp.include_router(dashboard_router)
    dp.include_router(settings_router)
    dp.include_router(files_router)
    dp.include_router(git_router)
    dp.include_router(ide_router)
    dp.include_router(memory_router)
    dp.include_router(environments_router)
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
    uvloop.install()
    asyncio.run(main())
