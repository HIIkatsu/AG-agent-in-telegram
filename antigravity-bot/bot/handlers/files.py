"""File Explorer and Viewer UI."""

import html
import os

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.filters import Command

from bot.db import db

router = Router(name="files")

IGNORED_DIRS = {".git", "node_modules", ".venv", "__pycache__", "dist", ".agents", ".antigravity"}


async def build_file_explorer(thread_id: int, current_path: str) -> tuple[str, InlineKeyboardMarkup]:
    """Build the File Explorer UI for a given path."""
    session = await db.get_session(thread_id)
    if not session:
        return "Сессия не найдена.", InlineKeyboardMarkup(inline_keyboard=[])

    ws = session["workdir"]
    full_path = os.path.join(ws, current_path) if current_path else ws
    full_path = os.path.abspath(full_path)

    # Security check: ensure path is within workdir
    if not full_path.startswith(os.path.abspath(ws)):
        full_path = ws
        current_path = ""

    if not os.path.exists(full_path):
        return f"Путь не найден: {current_path}", InlineKeyboardMarkup(inline_keyboard=[])

    is_dir = os.path.isdir(full_path)
    
    # If it's a file, we shouldn't be building a directory UI, but just in case
    if not is_dir:
        return f"Это файл: {current_path}", InlineKeyboardMarkup(inline_keyboard=[])

    try:
        entries = list(os.scandir(full_path))
    except Exception as e:
        return f"Ошибка чтения директории: {e}", InlineKeyboardMarkup(inline_keyboard=[])

    # Sort: folders first, then files, alphabetically
    folders = []
    files = []
    for entry in entries:
        if entry.name in IGNORED_DIRS:
            continue
        if entry.is_dir():
            folders.append(entry.name)
        else:
            files.append(entry.name)

    folders.sort()
    files.sort()

    keyboard = []
    
    # Add parent dir button if not in root
    if current_path and current_path != ".":
        parent_path = os.path.dirname(current_path.rstrip("/"))
        if parent_path == current_path:
            parent_path = ""
        parent_id = await db.save_callback_path(parent_path)
        root_id = await db.save_callback_path("")
        keyboard.append([
            InlineKeyboardButton(text="🔙 Назад", callback_data=f"f:dir:{thread_id}:{parent_id}"),
            InlineKeyboardButton(text="🏠 В корень", callback_data=f"f:dir:{thread_id}:{root_id}")
        ])

    # Build folder buttons
    for folder in folders:
        rel_path = os.path.join(current_path, folder).replace("\\", "/")
        path_id = await db.save_callback_path(rel_path)
        keyboard.append([InlineKeyboardButton(text=f"📁 {folder}", callback_data=f"f:dir:{thread_id}:{path_id}")])

    # Build file buttons
    for file in files:
        rel_path = os.path.join(current_path, file).replace("\\", "/")
        path_id = await db.save_callback_path(rel_path)
        keyboard.append([InlineKeyboardButton(text=f"📄 {file}", callback_data=f"f:open:{thread_id}:{path_id}")])

    kb = InlineKeyboardMarkup(inline_keyboard=keyboard)
    display_path = current_path if current_path else "/"
    text = f"📁 <b>File Explorer</b>\n\nТекущая папка: <code>{display_path}</code>"
    return text, kb


@router.callback_query(F.data.startswith("view_files:"))
async def cb_view_files_root(cq: CallbackQuery) -> None:
    """Entry point from the dashboard 'Files' button."""
    thread_id = int(cq.data.split(":")[1])  # type: ignore[union-attr]
    text, kb = await build_file_explorer(thread_id, "")
    try:
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb)  # type: ignore[union-attr]
    except Exception:
        pass
    await cq.answer()


@router.callback_query(F.data.startswith("f:dir:"))
async def cb_open_dir(cq: CallbackQuery) -> None:
    """Navigate to a directory."""
    parts = cq.data.split(":")  # type: ignore[union-attr]
    thread_id = int(parts[2])
    path_id = int(parts[3])
    
    path = await db.get_callback_path(path_id)
    if path is None:
        await cq.answer("Путь устарел, вернитесь в корень.", show_alert=True)
        return

    text, kb = await build_file_explorer(thread_id, path)
    try:
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb)  # type: ignore[union-attr]
    except Exception:
        pass
    await cq.answer()


@router.callback_query(F.data.startswith("f:open:"))
async def cb_open_file(cq: CallbackQuery, bot: Bot) -> None:
    """Open a file."""
    parts = cq.data.split(":")  # type: ignore[union-attr]
    thread_id = int(parts[2])
    path_id = int(parts[3])
    
    path = await db.get_callback_path(path_id)
    if path is None:
        await cq.answer("Путь устарел, вернитесь в корень.", show_alert=True)
        return

    session = await db.get_session(thread_id)
    if not session:
        await cq.answer("Сессия не найдена.", show_alert=True)
        return

    ws = session["workdir"]
    full_path = os.path.abspath(os.path.join(ws, path))

    if not full_path.startswith(os.path.abspath(ws)) or not os.path.isfile(full_path):
        await cq.answer("Файл не найден.", show_alert=True)
        return

    await cq.answer("Загрузка файла...")

    # Define binary extensions
    binary_exts = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".tar", ".gz", ".mp3", ".mp4", ".ico"}
    ext = os.path.splitext(full_path)[1].lower()
    
    file_size = os.path.getsize(full_path)
    
    if ext in binary_exts or file_size > 1024 * 1024:  # If binary or > 1MB
        doc = FSInputFile(full_path, filename=os.path.basename(full_path))
        path_id = await db.save_callback_path(path)
        await bot.send_document(
            cq.message.chat.id,  # type: ignore[union-attr]
            doc,
            caption=f"📄 <b>{os.path.basename(full_path)}</b>",
            parse_mode="HTML",
            message_thread_id=thread_id if thread_id != 0 else None,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🧠 В контекст", callback_data=f"ctx:add:{thread_id}:{path_id}")]])
        )
        return

    # Text file reading
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        # Fallback for binary looking files
        doc = FSInputFile(full_path, filename=os.path.basename(full_path))
        await bot.send_document(
            cq.message.chat.id,  # type: ignore[union-attr]
            doc,
            caption=f"📄 <b>{os.path.basename(full_path)}</b> (Binary)",
            parse_mode="HTML",
            message_thread_id=thread_id if thread_id != 0 else None
        )
        return

    # 3000 chars limit for sending as message
    if len(content) > 3000:
        doc = FSInputFile(full_path, filename=os.path.basename(full_path))
        await bot.send_document(
            cq.message.chat.id,  # type: ignore[union-attr]
            doc,
            caption=f"📄 <b>{os.path.basename(full_path)}</b> (Слишком большой для текста)",
            parse_mode="HTML",
            message_thread_id=thread_id if thread_id != 0 else None
        )
        return

    # Send as formatted code block
    lang = ext.lstrip(".") if ext else "text"
    escaped_content = html.escape(content)
    
    text = (
        f"📄 <b>{os.path.basename(full_path)}</b>:\n"
        f"<pre><code class=\"language-{lang}\">{escaped_content}</code></pre>"
    )
    
    # Check text length against TG limit (4096)
    if len(text) > 4000:
        doc = FSInputFile(full_path, filename=os.path.basename(full_path))
        await bot.send_document(
            cq.message.chat.id,  # type: ignore[union-attr]
            doc,
            caption=f"📄 <b>{os.path.basename(full_path)}</b>",
            parse_mode="HTML",
            message_thread_id=thread_id if thread_id != 0 else None
        )
        return

    path_id = await db.save_callback_path(path)
    await bot.send_message(
        cq.message.chat.id,  # type: ignore[union-attr]
        text,
        parse_mode="HTML",
        message_thread_id=thread_id if thread_id != 0 else None,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🧠 В контекст", callback_data=f"ctx:add:{thread_id}:{path_id}")]])
    )

@router.message(Command("files"))
async def cmd_files(message: Message, bot: Bot) -> None:
    """Open File Explorer."""
    thread_id = message.message_thread_id
    if thread_id is None:
        await message.reply("Доступно только в топике проекта.")
        return

    text, kb = await build_file_explorer(thread_id, "")
    await message.answer(text, parse_mode="HTML", reply_markup=kb)
