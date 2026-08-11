"""Handlers for managing SSH Environments."""

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot.db import db
from bot.services.ssh_executor import get_public_key

router = Router(name="environments")

class AddEnvState(StatesGroup):
    waiting_for_name = State()
    waiting_for_host = State()
    waiting_for_port = State()
    waiting_for_username = State()

@router.callback_query(F.data.startswith("env_manage_menu:"))
async def cq_env_manage_menu(cq: CallbackQuery, state: FSMContext) -> None:
    """Show list of environments."""
    await state.clear()
    thread_id = int(cq.data.split(":")[1])

    await _show_env_manage_menu(cq, thread_id)


async def _show_env_manage_menu(cq: CallbackQuery, thread_id: int) -> None:
    """Render the environment list without modifying the callback model."""
    envs = await db.get_all_environments()
    
    text = "🖥 <b>Управление Серверами (SSH)</b>\n\n"
    text += "Агент может подключаться к этим серверам для выполнения bash-команд (используя встроенный ssh-keygen ключ).\n\n"
    
    kb_rows = []
    
    if not envs:
        text += "<i>Список пуст.</i>\n"
    else:
        for env in envs:
            text += f"🔹 <b>{env['name']}</b> ({env['username']}@{env['host']}:{env.get('port', 22)})\n"
            kb_rows.append([
                InlineKeyboardButton(text=f"🗑 Удалить: {env['name']}", callback_data=f"env_del:{env['id']}:{thread_id}")
            ])
            
    kb_rows.append([
        InlineKeyboardButton(text="➕ Добавить сервер", callback_data=f"env_add:{thread_id}"),
        InlineKeyboardButton(text="🔑 Показать PubKey", callback_data=f"env_pubkey:{thread_id}")
    ])
    kb_rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"set_menu:main:{thread_id}")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("env_pubkey:"))
async def cq_env_pubkey(cq: CallbackQuery) -> None:
    """Show public key for authorized_keys."""
    thread_id = int(cq.data.split(":")[1])
    pub = await get_public_key()
    
    text = "🔑 <b>Публичный ключ бота</b>\n\nСкопируй этот ключ и добавь в файл `~/.ssh/authorized_keys` на целевом сервере:\n\n"
    text += f"<pre>{pub}</pre>"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Назад", callback_data=f"env_manage_menu:{thread_id}")
    ]])
    await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("env_del:"))
async def cq_env_del(cq: CallbackQuery) -> None:
    """Delete an environment."""
    _, env_id_str, thread_id_str = cq.data.split(":")
    await db.delete_environment(int(env_id_str))
    await cq.answer("Сервер удален!")
    
    await _show_env_manage_menu(cq, int(thread_id_str))

@router.callback_query(F.data.startswith("env_add:"))
async def cq_env_add(cq: CallbackQuery, state: FSMContext) -> None:
    """Start FSM to add a new environment."""
    thread_id = int(cq.data.split(":")[1])
    await state.update_data(thread_id=thread_id)
    await state.set_state(AddEnvState.waiting_for_name)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Отмена", callback_data=f"env_manage_menu:{thread_id}")
    ]])
    await cq.message.edit_text("Введите <b>Имя</b> сервера (например, Home PC):", parse_mode="HTML", reply_markup=kb)

@router.message(AddEnvState.waiting_for_name)
async def env_add_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=message.text.strip())
    await state.set_state(AddEnvState.waiting_for_host)
    await message.reply("Введите <b>IP адрес или хост</b> (например, 100.x.x.x):", parse_mode="HTML")

@router.message(AddEnvState.waiting_for_host)
async def env_add_host(message: Message, state: FSMContext) -> None:
    await state.update_data(host=message.text.strip())
    await state.set_state(AddEnvState.waiting_for_port)
    await message.reply("Введите <b>SSH порт</b> (по умолчанию 22):", parse_mode="HTML")

@router.message(AddEnvState.waiting_for_port)
async def env_add_port(message: Message, state: FSMContext) -> None:
    try:
        port = int(message.text.strip())
    except ValueError:
        await message.reply("Порт должен быть числом! Попробуй еще раз.")
        return
        
    await state.update_data(port=port)
    await state.set_state(AddEnvState.waiting_for_username)
    await message.reply("Введите <b>имя пользователя</b> (например, root или ubuntu):", parse_mode="HTML")

@router.message(AddEnvState.waiting_for_username)
async def env_add_username(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    name = data['name']
    host = data['host']
    port = data['port']
    username = message.text.strip()
    thread_id = data.get('thread_id', 0)
    
    await db.add_environment(name, host, port, username, "/opt/antigravity-bot/.ssh/bot_ed25519")
    await state.clear()
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ К списку серверов", callback_data=f"env_manage_menu:{thread_id}")
    ]])
    await message.reply(f"✅ Сервер <b>{name}</b> успешно добавлен!\nНе забудь добавить публичный ключ бота на этот сервер.", parse_mode="HTML", reply_markup=kb)
