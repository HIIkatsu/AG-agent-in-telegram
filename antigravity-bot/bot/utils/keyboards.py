"""Inline keyboards for forum-topic architecture."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def thread_settings_keyboard(thread_id: int, web_search: bool) -> InlineKeyboardMarkup:
    """Settings keyboard shown inside a forum topic."""
    web_label = "🌐 Веб-поиск: ВКЛ" if web_search else "🌐 Веб-поиск: ВЫКЛ"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=web_label, callback_data=f"web_toggle:{thread_id}")],
            [InlineKeyboardButton(text="🤖 Сменить модель", callback_data=f"model_menu:{thread_id}")],
        ]
    )
