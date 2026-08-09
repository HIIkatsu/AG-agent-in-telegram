"""Git History and Diff UI."""

import html
import subprocess
import os

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.filters import Command

from bot.db import db

router = Router(name="git_ui")


def run_git_command(ws: str, cmd: list[str]) -> str:
    """Helper to run git commands in workspace."""
    try:
        res = subprocess.run(
            ["git"] + cmd,
            cwd=ws,
            capture_output=True,
            text=True,
            check=True
        )
        return res.stdout.strip()
    except subprocess.CalledProcessError as e:
        return f"Git error: {e.stderr.strip()}"
    except Exception as e:
        return f"Error: {e}"


async def build_git_history(thread_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Build the Git History UI for the last commits."""
    session = await db.get_session(thread_id)
    if not session:
        return "Сессия не найдена.", InlineKeyboardMarkup(inline_keyboard=[])

    ws = session["workdir"]
    if not os.path.exists(os.path.join(ws, ".git")):
        return "Git не инициализирован в проекте.", InlineKeyboardMarkup(inline_keyboard=[])

    # Get last 10 commits: hash|author|message
    log_output = run_git_command(ws, ["log", "-n", "10", "--pretty=format:%h|%an|%s"])
    
    if "Git error" in log_output or "Error:" in log_output:
        if "does not have any commits yet" in log_output:
            return "Коммитов пока нет.", InlineKeyboardMarkup(inline_keyboard=[])
        return log_output, InlineKeyboardMarkup(inline_keyboard=[])

    if not log_output:
        return "Коммитов пока нет.", InlineKeyboardMarkup(inline_keyboard=[])

    lines = log_output.splitlines()
    text_lines = ["🌿 <b>Git History (Последние 10 коммитов)</b>\n"]
    
    keyboard = []
    
    for line in lines:
        parts = line.split("|", 2)
        if len(parts) == 3:
            short_hash, author, msg = parts
            text_lines.append(f"<code>{short_hash}</code> <b>{html.escape(author)}</b>: <i>{html.escape(msg)}</i>")
            keyboard.append([InlineKeyboardButton(text=f"📜 Посмотреть Diff {short_hash}", callback_data=f"g:c:{short_hash}:{thread_id}")])

    text_lines.append("\nНажмите на кнопку ниже, чтобы посмотреть изменения в коммите.")
    
    # Back to dashboard button
    keyboard.append([InlineKeyboardButton(text="🔙 Назад в Dashboard", callback_data=f"project_settings:{thread_id}")])

    return "\n".join(text_lines), InlineKeyboardMarkup(inline_keyboard=keyboard)


@router.callback_query(F.data.startswith("git_history:"))
async def cb_git_history(cq: CallbackQuery) -> None:
    """Entry point from dashboard 'Git History' button."""
    thread_id = int(cq.data.split(":")[1])  # type: ignore[union-attr]
    text, kb = await build_git_history(thread_id)
    try:
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb)  # type: ignore[union-attr]
    except Exception:
        pass
    await cq.answer()


@router.callback_query(F.data.startswith("g:c:"))
async def cb_show_commit(cq: CallbackQuery, bot: Bot) -> None:
    """Show diff for a specific commit."""
    parts = cq.data.split(":")  # type: ignore[union-attr]
    short_hash = parts[2]
    thread_id = int(parts[3])

    session = await db.get_session(thread_id)
    if not session:
        await cq.answer("Сессия не найдена.", show_alert=True)
        return

    ws = session["workdir"]
    await cq.answer("Генерация diff...")

    # Get diff for the commit
    # 'git diff {hash}^!' shows the changes introduced by {hash}
    diff_output = run_git_command(ws, ["diff", f"{short_hash}^!"])
    
    if "Git error" in diff_output or not diff_output:
        # Maybe it's the root commit, try git show
        diff_output = run_git_command(ws, ["show", short_hash, "--format="])

    if "Git error" in diff_output or not diff_output:
        await cq.answer("Не удалось получить изменения.", show_alert=True)
        return

    if len(diff_output) > 3000:
        # Send as patch file
        import tempfile
        patch_path = os.path.join(tempfile.gettempdir(), f"commit_{short_hash}.diff")
        with open(patch_path, "w", encoding="utf-8") as f:
            f.write(diff_output)
            
        doc = FSInputFile(patch_path, filename=f"{short_hash}.diff")
        await bot.send_document(
            cq.message.chat.id,  # type: ignore[union-attr]
            doc,
            caption=f"🌿 <b>Diff коммита <code>{short_hash}</code></b>",
            parse_mode="HTML",
            message_thread_id=thread_id if thread_id != 0 else None
        )
        return

    # Send as text message
    escaped_diff = html.escape(diff_output)
    text = (
        f"🌿 <b>Изменения в коммите <code>{short_hash}</code>:</b>\n"
        f"<pre><code class=\"language-diff\">{escaped_diff}</code></pre>"
    )
    
    if len(text) > 4000:
        # Fallback if wrapping it makes it too long
        import tempfile
        patch_path = os.path.join(tempfile.gettempdir(), f"commit_{short_hash}.diff")
        with open(patch_path, "w", encoding="utf-8") as f:
            f.write(diff_output)
            
        doc = FSInputFile(patch_path, filename=f"{short_hash}.diff")
        await bot.send_document(
            cq.message.chat.id,  # type: ignore[union-attr]
            doc,
            caption=f"🌿 <b>Diff коммита <code>{short_hash}</code></b>",
            parse_mode="HTML",
            message_thread_id=thread_id if thread_id != 0 else None
        )
        return

    await bot.send_message(
        cq.message.chat.id,  # type: ignore[union-attr]
        text,
        parse_mode="HTML",
        message_thread_id=thread_id if thread_id != 0 else None
    )

@router.message(Command("git"))
async def cmd_git(message: Message, bot: Bot) -> None:
    """Open Git History."""
    thread_id = message.message_thread_id
    if thread_id is None:
        await message.reply("Доступно только в топике проекта.")
        return

    text, kb = await build_git_history(thread_id)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)
