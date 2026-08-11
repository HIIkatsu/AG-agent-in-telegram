"""Handlers for durable project and global user memory."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.db import db
from bot.services.global_memory import MAX_FACT_CHARS

router = Router()
_MAX_VISIBLE_PROJECT_NOTES = 12
_MAX_VISIBLE_GLOBAL_FACTS = 12


def _normalize_project_note(raw: str) -> str:
    note = " ".join(raw.split())
    if not note:
        raise ValueError("Пустую заметку сохранить нельзя.")
    if len(note) > MAX_FACT_CHARS:
        raise ValueError(
            f"Заметка длиннее допустимого лимита ({MAX_FACT_CHARS} символов)."
        )
    return note


def _entry_lines(rows: list[dict], text_key: str, limit: int) -> list[str]:
    lines = [
        f"<b>#{row['id']}</b> <code>{html.escape(str(row[text_key]))}</code>"
        for row in rows[:limit]
    ]
    if len(rows) > limit:
        lines.append(f"<i>… ещё {len(rows) - limit}</i>")
    return lines or ["— пусто"]


async def _build_memory_view(
    thread_id: int | None,
    *,
    include_back: bool,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build the same two-layer memory view for a command and dashboard."""
    facts = await db.get_all_user_memory()
    project_notes = await db.list_memory_notes(thread_id) if thread_id is not None else []

    lines = ["🧠 <b>Память проекта</b>", ""]
    if thread_id is None:
        lines.append("— доступна внутри темы проекта")
    else:
        lines.extend(_entry_lines(project_notes, "note", _MAX_VISIBLE_PROJECT_NOTES))

    lines.extend(["", "🌐 <b>Глобальная память</b>", ""])
    lines.extend(_entry_lines(facts, "fact", _MAX_VISIBLE_GLOBAL_FACTS))
    lines.extend(
        [
            "",
            "<code>/memory add текст</code> · <code>/memory rm id</code> — память текущего проекта.",
            "Глобальные факты агент сохраняет только по прямой просьбе пользователя.",
        ]
    )

    buttons: list[list[InlineKeyboardButton]] = []
    if thread_id is not None:
        for row in project_notes[:_MAX_VISIBLE_PROJECT_NOTES]:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"🗑 Удалить заметку #{row['id']}",
                        callback_data=f"mem_pdel:{row['id']}:{thread_id}",
                    )
                ]
            )
    for row in facts[:_MAX_VISIBLE_GLOBAL_FACTS]:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 Удалить глобальный факт #{row['id']}",
                    callback_data=f"mem_del:{row['id']}:{thread_id or 0}",
                )
            ]
        )
    if include_back:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=f"set_menu:main:{thread_id or 0}",
                )
            ]
        )

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    return "\n".join(lines), keyboard


async def _show_memory(cq: CallbackQuery, thread_id: int | None) -> None:
    """Render memory from the dashboard without modifying callback state."""
    if cq.message is None:
        await cq.answer("Сообщение с памятью больше недоступно.", show_alert=True)
        return
    text, keyboard = await _build_memory_view(thread_id, include_back=True)
    await cq.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("mem_show:"))
async def cq_mem_show(cq: CallbackQuery) -> None:
    """Show both memory scopes from the dashboard."""
    _, thread_id_str = cq.data.split(":", 1)
    try:
        thread_id = int(thread_id_str) if thread_id_str != "0" else None
    except ValueError:
        await cq.answer("Некорректный проект.", show_alert=True)
        return
    await _show_memory(cq, thread_id)


async def _handle_project_memory_command(message: Message, args: str) -> None:
    thread_id = message.message_thread_id
    if thread_id is None:
        await message.reply("Локальная память доступна только внутри темы проекта.")
        return

    if args.startswith("add "):
        try:
            note = _normalize_project_note(args[4:])
        except ValueError as exc:
            await message.reply(str(exc))
            return
        notes = await db.list_memory_notes(thread_id)
        if any(row["note"] == note for row in notes):
            await message.answer("ℹ️ Такая заметка уже есть в памяти проекта.")
            return
        await db.add_memory_note(thread_id, note)
        await message.answer("🧠 Сохранил в память текущего проекта.")
        return

    if args.startswith("rm "):
        try:
            note_id = int(args[3:].strip())
        except ValueError:
            await message.reply("Использование: /memory rm <id>")
            return
        await db.delete_memory_note(note_id, thread_id)
        await message.answer("🧹 Заметка удалена из памяти проекта.")
        return

    await message.reply(
        "Использование: /memory · /memory add <текст> · /memory rm <id>"
    )


@router.message(Command("memory"))
async def cmd_memory(message: Message, command: CommandObject) -> None:
    """Show both project-local and global durable memory."""
    args = (command.args or "").strip()
    if args:
        await _handle_project_memory_command(message, args)
        return
    text, keyboard = await _build_memory_view(
        message.message_thread_id,
        include_back=False,
    )
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("mem_del:"))
async def cq_mem_del(cq: CallbackQuery) -> None:
    """Delete one global fact and keep the current project context visible."""
    parts = cq.data.split(":")
    try:
        fact_id = int(parts[1])
        thread_id = int(parts[2]) if parts[2] != "0" else None
    except (IndexError, ValueError):
        await cq.answer("Некорректный факт памяти.", show_alert=True)
        return
    deleted = await db.delete_user_memory(fact_id)
    await cq.answer(
        "Факт удалён из глобальной памяти." if deleted else "Факт уже удалён."
    )
    await _show_memory(cq, thread_id)


@router.callback_query(F.data.startswith("mem_pdel:"))
async def cq_project_memory_del(cq: CallbackQuery) -> None:
    """Delete a project-local note from its own Telegram topic only."""
    parts = cq.data.split(":")
    try:
        note_id = int(parts[1])
        thread_id = int(parts[2])
    except (IndexError, ValueError):
        await cq.answer("Некорректная заметка памяти.", show_alert=True)
        return
    await db.delete_memory_note(note_id, thread_id)
    await cq.answer("Заметка удалена из памяти проекта.")
    await _show_memory(cq, thread_id)
