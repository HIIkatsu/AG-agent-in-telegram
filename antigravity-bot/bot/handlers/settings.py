"""Settings router and screens."""

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import settings
from bot.db import db
from bot.modes import MODES

router = Router(name="settings")


async def _edit_settings_message(
    cq: CallbackQuery, text: str, kb: InlineKeyboardMarkup
) -> None:
    """Edit a settings screen, ignoring Telegram's harmless no-op response."""
    try:
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest as exc:
        # Repeated taps can render exactly the screen already shown. Telegram
        # reports that idempotent edit as an error even though the UI is valid.
        if "message is not modified" not in exc.message.lower():
            raise


async def build_settings_main(thread_id: int) -> tuple[str, InlineKeyboardMarkup]:
    await db.get_or_create_session(thread_id)
    text = (
        "⚙️ <b>Настройки проекта</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Выберите раздел для настройки:"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🤖 Модель", callback_data=f"set_menu:model:{thread_id}"),
                InlineKeyboardButton(text="🧠 Режим", callback_data=f"set_menu:mode:{thread_id}")
            ],
            [
                InlineKeyboardButton(text="🌐 Веб-поиск", callback_data=f"set_menu:web:{thread_id}"),
                InlineKeyboardButton(text="⏱ Тайм-ауты", callback_data=f"set_menu:timeout:{thread_id}")
            ],
            [
                InlineKeyboardButton(text="📦 Артефакты", callback_data=f"set_menu:artifacts:{thread_id}"),
                InlineKeyboardButton(text="🖥 Сервер", callback_data=f"set_menu:server:{thread_id}")
            ],
            [
                InlineKeyboardButton(text="◀️ Назад в Dashboard", callback_data=f"refresh_dashboard:{thread_id}")
            ]
        ]
    )
    return text, kb


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    thread_id = message.message_thread_id
    if thread_id is None:
        from bot.handlers.chats import _master_panel
        await _master_panel(message)
        return
        
    await message.reply("Настройки проекта теперь перенесены в дашборд. Пожалуйста, используйте команду /project")


@router.callback_query(F.data.startswith("project_settings:"))
async def cb_project_settings(cq: CallbackQuery) -> None:
    thread_id = int(cq.data.split(":")[1])
    text, kb = await build_settings_main(thread_id)
    await _edit_settings_message(cq, text, kb)


@router.callback_query(F.data.startswith("set_menu:"))
async def cb_set_menu(cq: CallbackQuery) -> None:
    parts = cq.data.split(":")
    menu = parts[1]
    thread_id = int(parts[2])

    await _show_settings_menu(cq, menu, thread_id)


async def _show_settings_menu(cq: CallbackQuery, menu: str, thread_id: int) -> None:
    """Render a settings submenu without modifying the callback model."""
    session = await db.get_or_create_session(thread_id)
    
    if menu == "model":
        text = f"🤖 <b>Текущая модель:</b> {session.get('model', 'default')}\nВыберите новую:"
        
        models = settings.get_available_models()
        rows = []
        for m in models:
            model_id = m.get("id", "")
            model_name = m.get("name", model_id)
            rows.append([InlineKeyboardButton(text=model_name, callback_data=f"set_val:model:{model_id}:{thread_id}")])
            
        rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"project_settings:{thread_id}")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
    elif menu == "mode":
        text = f"🧠 <b>Текущий режим:</b> {session.get('mode', 'code')}\nВыберите новый:"
        buttons = []
        row = []
        for key, val in MODES.items():
            row.append(InlineKeyboardButton(text=val["name"], callback_data=f"set_val:mode:{key}:{thread_id}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"project_settings:{thread_id}")])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    elif menu == "web":
        enabled = session.get("web_search", "off") in {"on", "required"}
        text = "🌐 <b>Веб-поиск</b>\nПоиск включён." if enabled else "🌐 <b>Веб-поиск</b>\nПоиск выключен."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"🌐 Веб-поиск: {'Вкл' if enabled else 'Выкл'}",
                callback_data=f"set_val:web:{'off' if enabled else 'on'}:{thread_id}",
            )],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"project_settings:{thread_id}")]
        ])
    else:
        text = f"Раздел {menu} пока в разработке."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"project_settings:{thread_id}")]
        ])

    await _edit_settings_message(cq, text, kb)


@router.callback_query(F.data.startswith("set_val:"))
async def cb_set_val(cq: CallbackQuery) -> None:
    parts = cq.data.split(":")
    menu = parts[1]
    val = parts[2]
    thread_id = int(parts[3])
    
    if menu == "model":
        # Find the actual name based on the id
        models = settings.get_available_models()
        model_name = val
        for m in models:
            if m.get("id") == val:
                model_name = m.get("name", val)
                break
                
        if thread_id == 0:
            await db.update_global_settings(model=model_name)
        else:
            await db.set_model(thread_id, model_name)
    elif menu == "mode":
        if thread_id == 0:
            await db.update_global_settings(mode=val)
        else:
            await db.set_mode(thread_id, val)
    elif menu == "web":
        if thread_id == 0:
            await db.update_global_settings(web_search=val)
        else:
            await db.set_web_search(thread_id, val)

    await cq.answer("Настройка сохранена!")
    
    # Refresh the selected menu. CallbackQuery is a frozen Pydantic model in
    # aiogram 3, so pass the parsed values explicitly instead of rewriting data.
    await _show_settings_menu(cq, menu, thread_id)
