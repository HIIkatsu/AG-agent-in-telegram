"""/start, /help, /stats handlers (Forum Topics edition)."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message


router = Router(name="start")

_WELCOME = (
    "👋 <b>Добро пожаловать в Antigravity AI!</b>\n\n"
    "Я автономный ИИ-помощник, работающий в режиме <b>Forum Topics</b>.\n\n"
    "<b>✨ Как это работает:</b>\n"
    "• Каждая <b>ветка (Topic)</b> — изолированная сессия агента с собственной памятью\n"
    "• Создайте новую ветку → бот автоматически создаст сессию\n"
    "• <b>General</b> — мастер-панель (обзор, настройки, статистика)\n\n"
    "<b>📂 Монтирование проектов:</b>\n"
    "• <code>/mount /path/to/project</code> — привяжите реальную директорию к ветке\n"
    "• <code>/pwd</code> — покажет текущую рабочую директорию\n\n"
    "<b>🚀 Просто напишите в ветку</b> — текст, голосовое, фото или файл."
)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(_WELCOME, parse_mode="HTML")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    is_general = message.message_thread_id is None

    if is_general:
        await message.answer(
            "<b>📖 Справка — Antigravity AI</b>\n\n"
            "<b>🏠 General (мастер-панель):</b>\n"
            "/settings — обзор всех сессий\n"
            "/stats — статистика\n"
            "/help — эта справка\n\n"
            "<b>💬 Внутри ветки (Topic):</b>\n"
            "/mount &lt;путь&gt; — привязать директорию проекта\n"
            "/pwd — показать рабочую директорию\n"
            "/settings — настройки этой ветки\n"
            "/web — вкл/выкл веб-поиск\n"
            "/model — выбор модели\n\n"
            "<b>📌 Принцип работы:</b>\n"
            "Каждая ветка = отдельная сессия агента со своей памятью, "
            "рабочей директорией, моделью и настройками.",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "<b>📖 Команды ветки:</b>\n\n"
            "/mount &lt;путь&gt; — привязать директорию проекта\n"
            "/pwd — показать рабочую директорию\n"
            "/settings — настройки ветки\n"
            "/web — вкл/выкл веб-поиск\n"
            "/model — выбор модели\n"
            "/help — справка",
            parse_mode="HTML",
        )



