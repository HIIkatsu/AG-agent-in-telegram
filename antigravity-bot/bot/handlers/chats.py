"""Forum-topic commands: /mount, /pwd, /settings, /web, /model + topic lifecycle.

General topic = Master control panel (system commands, session overview).
Topic threads  = Isolated agent sessions.
"""

from __future__ import annotations

import logging
import os
import shutil

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    ForumTopicClosed,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.config import settings
from bot.db import db
from bot.utils.keyboards import thread_settings_keyboard

logger = logging.getLogger(__name__)

router = Router(name="chats")


# ── Helpers ──────────────────────────────────────────────────────────────

def _model_keyboard(thread_id: int) -> InlineKeyboardMarkup:
    """Generate model selection keyboard from config.json, scoped to thread_id."""
    models = settings.get_available_models()
    rows = []
    for m in models:
        model_id = m.get("id", "")
        model_name = m.get("name", model_id)
        rows.append([InlineKeyboardButton(text=model_name, callback_data=f"model:{model_id}:{thread_id}")])
    rows.append([InlineKeyboardButton(text="Сбросить (По умолчанию)", callback_data=f"model::{thread_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _get_thread_id(message: Message) -> int | None:
    """Extract thread_id; returns None for General topic."""
    return message.message_thread_id


def _is_general(message: Message) -> bool:
    """True when message is in the General topic (no thread_id)."""
    return message.message_thread_id is None


async def purge_dead_topics(bot: Bot) -> int:
    """Check all active sessions and remove those where topic was deleted."""
    sessions = await db.list_all_sessions()
    dead_count = 0
    from aiogram.enums import ChatAction
    from aiogram.exceptions import TelegramBadRequest
    for s in sessions:
        tid = s["thread_id"]
        if tid == 0:
            continue
        try:
            await bot.send_chat_action(
                chat_id=settings.forum_group_id,
                action=ChatAction.TYPING,
                message_thread_id=tid
            )
        except TelegramBadRequest as e:
            err_str = str(e).lower()
            if "message thread not found" in err_str or "topic deleted" in err_str or "thread not found" in err_str:
                await db.delete_session(tid)
                dead_count += 1
                import shutil
                workdir = s.get("workdir", "")
                if workdir and workdir.startswith(settings.workspaces_dir):
                    try:
                        shutil.rmtree(workdir, ignore_errors=True)
                    except Exception:
                        pass
            else:
                import logging
                logging.error(f"purge_dead_topics TelegramBadRequest: {e}")
        except Exception as e:
            import logging
            logging.error(f"purge_dead_topics Exception: {e}")
    return dead_count


async def build_master_panel() -> tuple[str, InlineKeyboardMarkup]:
    """Build text and keyboard for Master Panel."""
    sessions = await db.list_all_sessions()
    sessions = [s for s in sessions if s["thread_id"] != 0]

    glob = await db.get_global_settings()
    glob_model = glob.get("model") or "Gemini 3.6 flash (high)"
    glob_mode = glob.get("mode") or "code"
    glob_web = glob.get("web_search") or "off"

    text = (
        "🌍 <b>Глобальные настройки (По умолчанию)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 Дефолтная Модель: <b>{glob_model}</b>\n"
        f"🧠 Дефолтный Режим: <b>{glob_mode}</b>\n"
        f"🌐 Дефолтный Веб: <b>{glob_web}</b>\n\n"
        f"📊 Активных проектов: <b>{len(sessions)}</b>"
    )

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🤖 Дефолтная Модель", callback_data="set_menu:model:0"),
                InlineKeyboardButton(text="🧠 Дефолтный Режим", callback_data="set_menu:mode:0")
            ],
            [
                InlineKeyboardButton(text="🌐 Дефолтный Веб", callback_data="set_menu:web:0")
            ],
            [
                InlineKeyboardButton(text="🗑 Управление сессиями", callback_data="manage_sessions_menu:0"),
            ],
            [
                InlineKeyboardButton(text="🧹 Очистка сессий (Зависшие)", callback_data="purge_cli_sessions")
            ]
        ]
    )

    return text, kb


async def build_sessions_manage_panel() -> tuple[str, InlineKeyboardMarkup]:
    """Build a specific panel for deleting sessions."""
    sessions = await db.list_all_sessions()
    sessions = [s for s in sessions if s["thread_id"] != 0]

    lines = [
        "🗑 <b>Управление сессиями</b>\n",
        "Нажмите на ID сессии ниже, чтобы удалить её. Это полезно, если вы удалили ветку в Telegram, и бот не может убрать её автоматически.\n",
    ]

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    rows = []
    
    if sessions:
        current_row = []
        for s in sessions[:30]:
            tid = s["thread_id"]
            tname = s.get("topic_name") or f"Thread {tid}"
            # Shorten if too long
            if len(tname) > 15:
                tname = tname[:13] + ".."
            current_row.append(InlineKeyboardButton(text=f"🗑 {tname}", callback_data=f"kill_session:{tid}"))
            if len(current_row) == 2:
                rows.append(current_row)
                current_row = []
        if current_row:
            rows.append(current_row)
            
    rows.append([InlineKeyboardButton(text="🧹 Авто-очистка удаленных веток", callback_data="clean_dead_topics")])
    rows.append([InlineKeyboardButton(text="◀️ Назад в Master Panel", callback_data="back_to_master")])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    return "\n".join(lines), kb


async def _master_panel(message: Message) -> None:
    """Show Master control panel in General topic."""
    text, kb = await build_master_panel()
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ── /mount — bind a real project directory to this topic ─────────────────

@router.message(Command("mount"))
async def cmd_mount(message: Message) -> None:
    thread_id = _get_thread_id(message)
    if thread_id is None:
        await message.answer("⚠️ Команда /mount работает только внутри ветки (не в General).")
        return

    raw_path = message.text.partition(" ")[2].strip() if message.text else ""
    if not raw_path:
        await message.answer("Использование: <code>/mount /абсолютный/путь/к/проекту</code>", parse_mode="HTML")
        return

    abs_path = os.path.abspath(raw_path)
    if not os.path.isdir(abs_path):
        await message.answer(f"❌ Директория не найдена:\n<code>{abs_path}</code>", parse_mode="HTML")
        return

    session = await db.get_or_create_session(thread_id)
    await db.set_workdir(thread_id, abs_path, is_mounted=True)

    await message.answer(
        f"✅ <b>Проект смонтирован</b>\n\n"
        f"📂 <code>{abs_path}</code>\n\n"
        f"Все команды, git-диффы и откаты в этой ветке теперь привязаны к указанной директории.",
        parse_mode="HTML",
    )
    logger.info("Thread %s mounted to %s", thread_id, abs_path)


# ── /pwd — show current workdir ─────────────────────────────────────────

@router.message(Command("pwd"))
async def cmd_pwd(message: Message) -> None:
    thread_id = _get_thread_id(message)
    if thread_id is None:
        await message.answer("ℹ️ General-ветка. Рабочая директория не назначена.")
        return

    session = await db.get_session(thread_id)
    if not session:
        await message.answer("Сессия ещё не создана. Отправьте любое сообщение в эту ветку.")
        return

    mounted = "✅ (смонтировано через /mount)" if session["is_mounted"] else "📁 (по умолчанию)"
    await message.answer(
        f"📂 <b>Рабочая директория:</b>\n<code>{session['workdir']}</code>\n{mounted}",
        parse_mode="HTML",
    )





# ── /web — toggle web search ────────────────────────────────────────────

@router.message(Command("web"))
async def cmd_web(message: Message) -> None:
    thread_id = _get_thread_id(message)
    if thread_id is None:
        await message.answer("⚠️ Команда /web работает только внутри ветки.")
        return

    await db.get_or_create_session(thread_id)
    new_val = await db.toggle_web_search(thread_id)
    await message.answer(f"Веб-поиск: <b>{'ВКЛ' if new_val else 'ВЫКЛ'}</b>", parse_mode="HTML")


# ── /model — select model ───────────────────────────────────────────────

@router.message(Command("model"))
async def cmd_model(message: Message) -> None:
    thread_id = _get_thread_id(message)
    if thread_id is None:
        await message.answer("⚠️ Команда /model работает только внутри ветки.")
        return

    session = await db.get_or_create_session(thread_id)
    current = session.get("model", "") or "по умолчанию"
    await message.answer(
        f"Текущая модель: <b>{current}</b>\n\nВыберите модель из списка:",
        parse_mode="HTML",
        reply_markup=_model_keyboard(thread_id),
    )


# ── Forum Topic Closed — Deep Cleanup ───────────────────────────────────

@router.message(F.forum_topic_closed)
async def on_topic_closed(message: Message) -> None:
    """Handle forum topic closure: clean up DB + tmp workspace."""
    thread_id = _get_thread_id(message)
    if thread_id is None:
        return

    session = await db.delete_session(thread_id)
    if not session:
        logger.info("Topic %s closed but no session found in DB", thread_id)
        return

    is_mounted = bool(session.get("is_mounted", 0))
    workdir = session.get("workdir", "")

    if not is_mounted and workdir and workdir.startswith(settings.workspaces_dir):
        # Safe to remove tmp workspace
        if os.path.isdir(workdir):
            try:
                shutil.rmtree(workdir, ignore_errors=True)
                logger.info("Cleaned up tmp workspace for thread %s: %s", thread_id, workdir)
            except Exception as e:
                logger.warning("Failed to remove workspace %s: %s", workdir, e)
    else:
        logger.info(
            "Thread %s was mounted to %s — skipping filesystem cleanup",
            thread_id, workdir,
        )

    logger.info("Deep cleanup complete for thread %s (mounted=%s)", thread_id, is_mounted)


# ── /cleanup — purge broken CLI sessions ────────────────────────────────

def purge_stale_cli_sessions() -> int:
    """Remove broken / stale CLI session dirs from ~/.gemini/antigravity-cli/.

    Returns the number of purged entries.
    """
    cli_dir = os.path.expanduser("~/.gemini/antigravity-cli")
    if not os.path.isdir(cli_dir):
        return 0

    # Sessions live in brain/<uuid> subdirs — scan there
    import uuid as _uuid
    _SYSTEM_DIRS = {"builtin", "brain", "knowledge", "updater", "bin",
                    "scratch", "cache", "log", "implicit", "presence",
                    "conversations", "crashes", "config"}
    purged = 0
    for entry in os.listdir(cli_dir):
        entry_path = os.path.join(cli_dir, entry)
        if not os.path.isdir(entry_path):
            continue
        # Never touch known system directories
        if entry in _SYSTEM_DIRS:
            continue
        # Try to parse as valid UUID — if it fails, it's a broken session
        try:
            _uuid.UUID(entry)
        except ValueError:
            try:
                shutil.rmtree(entry_path, ignore_errors=True)
                purged += 1
                logger.info("Purged broken CLI session: %s", entry)
            except Exception as exc:
                logger.warning("Failed to purge %s: %s", entry_path, exc)
    return purged


@router.message(Command("cleanup"))
async def cmd_cleanup(message: Message) -> None:
    """Purge stale/broken CLI sessions from disk."""
    purged = purge_stale_cli_sessions()
    if purged:
        await message.answer(
            f"🧹 Очищено <b>{purged}</b> битых сессий CLI.",
            parse_mode="HTML",
        )
    else:
        await message.answer("✅ Битых сессий не обнаружено.")
