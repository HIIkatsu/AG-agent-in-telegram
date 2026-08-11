"""Handlers for Personal Intelligence Memory."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.db import db

router = Router()

@router.callback_query(F.data.startswith("mem_show:"))
async def cq_mem_show(cq: CallbackQuery) -> None:
    """Callback to show memory from dashboard."""
    _, thread_id_str = cq.data.split(":", 1)
    thread_id = int(thread_id_str) if thread_id_str != "0" else None

    await _show_memory(cq, thread_id)


async def _show_memory(cq: CallbackQuery, thread_id: int | None) -> None:
    """Render memory without modifying the callback model."""
    facts = await db.get_all_user_memory()
    if not facts:
        text = "📭 <b>Глобальная память пуста</b>\n\nАгент пока не сохранил никаких фактов."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"set_menu:main:{thread_id or 0}")]
        ])
    else:
        text = "🧠 <b>Глобальная Память Агента (Personal Intelligence)</b>\n\nЭти факты агент использует во всех проектах:\n\n"
        kb_rows = []
        for i, row in enumerate(facts, start=1):
            fact_text = row['fact']
            if len(fact_text) > 100:
                fact_text = fact_text[:97] + "..."
            text += f"<b>{i}.</b> <code>{fact_text}</code>\n"
            kb_rows.append([
                InlineKeyboardButton(text=f"🗑 Удалить факт {i}", callback_data=f"mem_del:{row['id']}:{thread_id or 0}")
            ])
        kb_rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"set_menu:main:{thread_id or 0}")])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
        
    await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

@router.message(Command("memory"))
async def cmd_memory(message: Message) -> None:
    """Show global user memory."""
    thread_id = message.message_thread_id
    facts = await db.get_all_user_memory()
    if not facts:
        text = "📭 <b>Глобальная память пуста</b>\n\nАгент пока не сохранил никаких фактов."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"set_menu:main:{thread_id or 0}")]
        ])
    else:
        text = "🧠 <b>Глобальная Память Агента (Personal Intelligence)</b>\n\nЭти факты агент использует во всех проектах:\n\n"
        kb_rows = []
        for i, row in enumerate(facts, start=1):
            fact_text = row['fact']
            if len(fact_text) > 100:
                fact_text = fact_text[:97] + "..."
            text += f"<b>{i}.</b> <code>{fact_text}</code>\n"
            kb_rows.append([
                InlineKeyboardButton(text=f"🗑 Удалить факт {i}", callback_data=f"mem_del:{row['id']}:{thread_id or 0}")
            ])
        kb_rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"set_menu:main:{thread_id or 0}")])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
        
    await message.answer(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("mem_del:"))
async def cq_mem_del(cq: CallbackQuery) -> None:
    parts = cq.data.split(":")
    fact_id = int(parts[1])
    thread_id_str = parts[2]
    thread_id = int(thread_id_str) if thread_id_str != "0" else None
    
    await db.delete_user_memory(fact_id)
    await cq.answer("Факт удален из памяти.")
    
    await _show_memory(cq, thread_id)
