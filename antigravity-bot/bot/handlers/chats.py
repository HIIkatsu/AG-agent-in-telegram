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


async def _master_panel(message: Message) -> None:
    """Show Master control panel in General topic."""
    sessions = await db.list_all_sessions()

    lines = [
        "🏠 <b>Master Panel — Antigravity AI</b>\n",
        f"📊 Активных сессий: <b>{len(sessions)}</b>\n",
    ]

    if sessions:
        lines.append("─────────────────────")
        for s in sessions[:15]:
            mounted = "📌" if s.get("is_mounted") else "📁"
            model = s.get("model") or "default"
            web = "🌐" if s.get("web_search") else ""
            workdir_short = s["workdir"][-35:] if len(s["workdir"]) > 35 else s["workdir"]
            lines.append(
                f"{mounted} <b>Thread {s['thread_id']}</b> | {model} {web}\n"
                f"   └ <code>{workdir_short}</code>"
            )
        lines.append("─────────────────────")
    else:
        lines.append("\n<i>Создайте новую ветку (Topic) в группе, чтобы начать.</i>")

    lines.append(
        "\n💡 <b>Команды:</b>\n"
        "• <code>/settings</code> — эта панель\n"
        "• <code>/stats</code> — статистика\n"
        "• <code>/help</code> — справка\n\n"
        "⚡ Для работы с агентом — пишите в ветки."
    )

    await message.answer("\n".join(lines), parse_mode="HTML")


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


# ── /settings — show config (Master panel in General, topic config in threads) ──

@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    thread_id = _get_thread_id(message)

    # General → Master Panel
    if thread_id is None:
        await _master_panel(message)
        return

    # Topic → per-topic settings
    session = await db.get_or_create_session(thread_id)
    web = "ВКЛ" if session.get("web_search") else "ВЫКЛ"
    model = session.get("model") or "По умолчанию"
    workdir = session.get("workdir", "—")
    mounted = "✅ mount" if session.get("is_mounted") else "tmp"
    # Compute the same deterministic UUIDv5 used for CLI conversations
    import uuid as _uuid
    _NAMESPACE_TG = _uuid.UUID('6ba7b810-9ed0-11d1-80b4-00c04fd430c8')
    conv_id = str(_uuid.uuid5(_NAMESPACE_TG, f"thread-{thread_id}"))

    text = (
        f"⚙️ <b>Настройки ветки</b>\n\n"
        f"• <b>Модель:</b> {model}\n"
        f"• <b>Веб-поиск:</b> {web}\n"
        f"• <b>Директория:</b> <code>{workdir}</code> ({mounted})\n"
        f"• <b>Сессия agy:</b> <code>{conv_id[:18]}…</code>"
    )
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=thread_settings_keyboard(thread_id, bool(session.get("web_search"))),
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
