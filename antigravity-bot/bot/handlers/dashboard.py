"""Dashboard commands and project summary view."""

import os
import asyncio
from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import settings
from bot.db import db
from bot.services.git_manager import git_manager
from bot.services.task_service import get_queued_count

router = Router(name="dashboard")


async def build_dashboard_content(thread_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Build the dashboard text and keyboard."""
    session = await db.get_or_create_session(thread_id)
    ws = session["workdir"]
    model = session.get("model", "") or "Gemini 3.6 flash (high)"
    web_search = session.get("web_search", "off")
    mode = session.get("mode", "code")

    project_name = os.path.basename(ws)
    if not os.path.exists(ws):
        os.makedirs(ws, exist_ok=True)
        git_manager.init_workspace(ws)

    # Git stats
    branch = git_manager.get_current_branch(ws) or "N/A"
    try:
        changed_files_count = len(git_manager.status(ws))
    except Exception:
        changed_files_count = 0

    # Task stats
    queued_tasks = await get_queued_count(thread_id)
    
    cur = await db.conn.execute("SELECT * FROM tasks WHERE thread_id = ? ORDER BY id DESC LIMIT 1", (thread_id,))
    last_task = await cur.fetchone()
    
    active_task_str = "None"
    last_error_str = "None"
    
    if last_task:
        if last_task["status"] == "running":
            active_task_str = f"#{last_task['id']} - {last_task['prompt'][:20]}..."
        elif last_task["error"]:
            last_error_str = last_task["error"][:30] + "..."

    text = (
        f"📊 <b>Project Dashboard</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📁 <b>{project_name}</b>\n\n"
        f"🌿 Ветка: <code>{branch}</code>\n"
        f"📝 Изменено файлов: {changed_files_count}\n\n"
        f"⚙️ <b>Конфигурация</b>\n"
        f"🤖 Модель: <i>{model}</i>\n"
        f"🧠 Режим: <i>{mode}</i>\n"
        f"🌐 Веб-поиск: <i>{web_search}</i>\n\n"
        f"📋 <b>Задачи</b>\n"
        f"▶️ Активная: <i>{active_task_str}</i>\n"
        f"⏳ В очереди: <i>{queued_tasks}</i>\n"
        f"❌ Последняя ошибка: <i>{last_error_str}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚙️ Настройки проекта", callback_data=f"project_settings:{thread_id}"),
                InlineKeyboardButton(text="🔄 Обновить", callback_data=f"refresh_dashboard:{thread_id}")
            ],
            [
                InlineKeyboardButton(text="📁 Files", callback_data=f"view_files:{thread_id}"),
                InlineKeyboardButton(text="👀 Diff", callback_data=f"open_diff:{thread_id}"),
            ],
            [
                InlineKeyboardButton(text="🧠 Context", callback_data=f"open_context:{thread_id}"),
                InlineKeyboardButton(text="🧪 Tests", callback_data=f"run_tests:{thread_id}"),
            ],
            [
                InlineKeyboardButton(text="🌿 Git History", callback_data=f"git_history:{thread_id}"),
                InlineKeyboardButton(text="🖥 Терминал (/run)", callback_data=f"prompt_run:{thread_id}"),
            ]
        ]
    )
    return text, kb


@router.message(Command("project", "dashboard"))
async def cmd_project(message: Message, bot: Bot) -> None:
    """Show the project dashboard with statistics and settings."""
    thread_id = message.message_thread_id
    if thread_id is None:
        from bot.handlers.chats import _master_panel
        await _master_panel(message)
        return

    text, kb = await build_dashboard_content(thread_id)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def get_cli_usage_stats() -> str:
    """Fetch and parse quota usage from agy CLI."""
    try:
        proc = await asyncio.create_subprocess_exec(
            settings.agy_path, "--print", "/usage",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return f"<i>Ошибка CLI: {stderr.decode('utf-8', errors='ignore').strip()}</i>"
        
        output = stdout.decode("utf-8").strip()
        if not output:
            return "<i>Нет данных о лимитах.</i>"

        lines = output.splitlines()
        groups: dict[str, list[str]] = {}
        for line in lines:
            parts = line.split('\t')
            if len(parts) >= 3:
                group = parts[0]
                limit_type = parts[1]
                val = parts[2]
                if group not in groups:
                    groups[group] = []
                
                # Make a progress bar
                bar_len = 10
                if val.endswith('%'):
                    try:
                        pct = float(val.strip('%'))
                        filled = int((pct / 100) * bar_len)
                        bar = "█" * filled + "░" * (bar_len - filled)
                    except ValueError:
                        bar = "░" * bar_len
                elif val.lower() == 'disabled':
                    bar = "░" * bar_len
                else:
                    bar = "░" * bar_len

                groups[group].append(f"• {limit_type}: <b>{val}</b>\n  <code>[{bar}]</code>")

        res = []
        for g, items in groups.items():
            res.append(f"<b>{g.upper()}</b>\n" + "\n".join(items) + "\n")
        return "\n".join(res)
    except Exception as e:
        return f"<i>Не удалось получить лимиты ({e})</i>"


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    """Show detailed project statistics and API limits."""
    thread_id = message.message_thread_id
    if thread_id is None:
        await message.reply("Статистика доступна только внутри топиков-проектов.")
        return

    # Task stats
    cur = await db.conn.execute("SELECT COUNT(*) as c FROM tasks WHERE thread_id = ?", (thread_id,))
    row = await cur.fetchone()
    total_tasks = row["c"] if row else 0

    cur = await db.conn.execute("SELECT COUNT(*) as c FROM tasks WHERE thread_id = ? AND status = 'done'", (thread_id,))
    row = await cur.fetchone()
    done_tasks = row["c"] if row else 0

    cur = await db.conn.execute("SELECT COUNT(*) as c FROM tasks WHERE thread_id = ? AND status IN ('failed', 'error', 'cancelled')", (thread_id,))
    row = await cur.fetchone()
    failed_tasks = row["c"] if row else 0

    cli_stats = await get_cli_usage_stats()

    text = (
        f"📊 <b>Подробная статистика проекта</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📈 <b>Задачи:</b>\n"
        f"Всего создано: <b>{total_tasks}</b>\n"
        f"✅ Успешно завершено: <b>{done_tasks}</b>\n"
        f"❌ Ошибок/Отмен: <b>{failed_tasks}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ <b>Лимиты Antigravity CLI</b>\n\n"
        f"{cli_stats}"
    )
    
    await message.answer(text, parse_mode="HTML")
