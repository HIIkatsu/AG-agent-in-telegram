"""Dashboard commands and project summary view."""

import os
from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.db import db
from bot.services.git_manager import git_manager
from bot.services.task_service import get_queued_count

router = Router(name="dashboard")


@router.message(Command("project", "dashboard"))
async def cmd_project(message: Message, bot: Bot) -> None:
    """Show the project dashboard with statistics and settings."""
    thread_id = message.message_thread_id
    if thread_id is None:
        await message.reply("Эта команда работает только внутри топика-проекта.")
        return

    session = await db.get_or_create_session(thread_id)
    ws = session["workdir"]
    model = session.get("model", "") or "default"
    web_search_enabled = bool(session.get("web_search", 0))

    project_name = os.path.basename(ws)
    if not os.path.exists(ws):
        os.makedirs(ws, exist_ok=True)
        git_manager.init(ws)

    # Git stats
    branch = git_manager.get_current_branch(ws) or "N/A"
    try:
        changes = git_manager.get_diff(ws)
        changed_files_count = len(changes.splitlines()) if changes else 0
    except Exception:
        changed_files_count = 0

    # Task stats
    queued_tasks = await get_queued_count(thread_id)
    
    cur = await db.conn.execute("SELECT COUNT(*) as c FROM tasks WHERE thread_id = ? AND status = 'done'", (thread_id,))
    row = await cur.fetchone()
    completed_tasks = row["c"] if row else 0

    text = (
        f"📊 <b>Project Dashboard</b>\n"
        f"📁 <b>{project_name}</b>\n\n"
        f"🌿 Ветка: <code>{branch}</code>\n"
        f"📝 Изменено файлов: {changed_files_count}\n\n"
        f"⚙️ <b>Конфигурация</b>\n"
        f"Модель: <i>{model}</i>\n"
        f"Режим: <i>code</i>\n"
        f"Веб-поиск: {'✅ Включен' if web_search_enabled else '❌ Выключен'}\n\n"
        f"📈 <b>Статистика</b>\n"
        f"Задач в очереди: {queued_tasks}\n"
        f"Выполнено задач: {completed_tasks}\n"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💬 Режим", callback_data="change_mode"),
                InlineKeyboardButton(text="🤖 Модель", callback_data="change_model"),
                InlineKeyboardButton(text="🌐 Веб-поиск", callback_data="toggle_web_search")
            ],
            [
                InlineKeyboardButton(text="📋 Очистить очередь", callback_data=f"clear_queue:{thread_id}"),
                InlineKeyboardButton(text="🔄 Обновить", callback_data=f"refresh_dashboard:{thread_id}")
            ]
        ]
    )

    await message.answer(text, parse_mode="HTML", reply_markup=kb)
