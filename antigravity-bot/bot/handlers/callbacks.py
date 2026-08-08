"""Inline callback handlers: permissions HITL, Git Diff, Rollback, model selection, web toggle, cancel."""

from __future__ import annotations

import asyncio
import os

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import settings
from bot.db import db
from bot.services.agy_runner import run_agy
from bot.services.diff_viewer import generate_diff_html_file
from bot.services.git_manager import git_manager
from bot.services.permissions import permission_handler
from bot.utils.keyboards import thread_settings_keyboard

router = Router(name="callbacks")


# ── HITL Permission Callbacks ────────────────────────────────────────────

@router.callback_query(F.data.startswith("perm:allow:"))
async def cb_perm_allow(cq: CallbackQuery, bot: Bot) -> None:
    req_id = cq.data.split(":")[2]  # type: ignore[union-attr]
    res = await permission_handler.handle_callback(req_id, allow=True, bot=bot)
    await cq.answer(res)


@router.callback_query(F.data.startswith("perm:deny:"))
async def cb_perm_deny(cq: CallbackQuery, bot: Bot) -> None:
    req_id = cq.data.split(":")[2]  # type: ignore[union-attr]
    res = await permission_handler.handle_callback(req_id, allow=False, bot=bot)
    await cq.answer(res)


# ── Git View Diff ────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("view_diff:"))
async def cb_view_diff(cq: CallbackQuery, bot: Bot) -> None:
    assert cq.from_user and cq.message
    thread_id = int(cq.data.split(":")[1])  # type: ignore[union-attr]
    session = await db.get_session(thread_id)
    if not session:
        await cq.answer("Сессия не найдена", show_alert=True)
        return

    ws = session["workdir"]
    raw_diff = git_manager.get_diff(ws)

    if not raw_diff or not raw_diff.strip():
        await cq.answer("Нет измененных файлов.", show_alert=True)
        return

    await cq.answer("Генерация diff.html...")
    diff_file_path = generate_diff_html_file(raw_diff, f"Thread {thread_id}")
    doc = FSInputFile(diff_file_path, filename="diff.html")
    await bot.send_document(
        cq.message.chat.id,
        doc,
        caption="👀 <b>Diff изменений (VS Code Side-by-Side View):</b>\nОткройте файл в браузере для удобного просмотра.",
        parse_mode="HTML",
        message_thread_id=thread_id,
    )


# ── Git Accept Diff ──────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("accept_diff:"))
async def cb_accept_diff(cq: CallbackQuery) -> None:
    assert cq.from_user and cq.message
    thread_id = int(cq.data.split(":")[1])  # type: ignore[union-attr]
    session = await db.get_session(thread_id)
    if session:
        ws = session["workdir"]
        git_manager.accept(ws)
        await cq.answer("Изменения приняты!")
        try:
            await cq.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    else:
        await cq.answer("Сессия не найдена", show_alert=True)


# ── Git Rollback & Agent Memory Sync ─────────────────────────────────────

@router.callback_query(F.data.startswith("rollback:"))
async def cb_rollback(cq: CallbackQuery, bot: Bot) -> None:
    assert cq.from_user and cq.message
    thread_id = int(cq.data.split(":")[1])  # type: ignore[union-attr]
    session = await db.get_session(thread_id)

    if not session:
        await cq.answer("Сессия не найдена", show_alert=True)
        return

    ws = session["workdir"]

    # 1. Execute Git Rollback (reset --hard HEAD && clean -fd)
    ok = git_manager.rollback(ws)

    if ok:
        await cq.answer("Изменения откатаны!", show_alert=True)

        # 2. Update Telegram status message
        try:
            await cq.message.edit_text(
                "⏪ <b>Изменения откатаны:</b>\nВсе файлы возвращены к исходному состоянию, новые файлы удалены.",
                parse_mode="HTML",
                reply_markup=None,
            )
        except Exception:
            pass

        import uuid as _uuid
        _NAMESPACE_TG = _uuid.UUID('6ba7b810-9ed0-11d1-80b4-00c04fd430c8')
        conv_id = str(_uuid.uuid5(_NAMESPACE_TG, f"thread-{thread_id}"))
        
        # 3. Synchronize agy Agent Memory — clean prompt, no [SYSTEM:] injection
        sync_prompt = (
            "Пользователь отменил твои последние изменения. "
            "Файлы откачены к исходному состоянию. Забудь последний шаг и жди указаний."
        )
        asyncio.create_task(
            run_agy(
                prompt=sync_prompt,
                conversation_id=conv_id,
                workspace_dir=ws,
                on_chunk=lambda _: asyncio.sleep(0),
                bot=bot,
                chat_id=cq.message.chat.id,
                thread_id=thread_id,
            )
        )
    else:
        await cq.answer("Ошибка отката изменений.", show_alert=True)


# ── Cancel generation ────────────────────────────────────────────────────

@router.callback_query(F.data == "cancel_gen")
async def cb_cancel_gen(cq: CallbackQuery) -> None:
    assert cq.message
    from bot.handlers.message import _active

    # Determine thread_id from the message context
    thread_id = cq.message.message_thread_id  # type: ignore[union-attr]
    if thread_id is None:
        await cq.answer("Нечего отменять")
        return

    entry = _active.get(thread_id)
    if entry:
        tracker, agy_task = entry
        await tracker.cancel()
        if agy_task and not agy_task.done():
            agy_task.cancel()
        await cq.answer("Отменено")
    else:
        await cq.answer("Нечего отменять")


# ── Web search toggle ────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("web_toggle:"))
async def cb_web_toggle(cq: CallbackQuery) -> None:
    assert cq.from_user and cq.message
    thread_id = int(cq.data.split(":")[1])  # type: ignore[union-attr]
    new_val = await db.toggle_web_search(thread_id)
    session = await db.get_session(thread_id)
    if session:
        await cq.message.edit_reply_markup(  # type: ignore[union-attr]
            reply_markup=thread_settings_keyboard(thread_id, new_val),
        )
    await cq.answer(f"Веб-поиск: {'ВКЛ' if new_val else 'ВЫКЛ'}")


# ── Model selection menu ─────────────────────────────────────────────────

@router.callback_query(F.data.startswith("model_menu:"))
async def cb_model_menu(cq: CallbackQuery) -> None:
    assert cq.message
    thread_id = int(cq.data.split(":")[1])  # type: ignore[union-attr]
    session = await db.get_session(thread_id)
    if not session:
        await cq.answer("Сессия не найдена", show_alert=True)
        return

    models = settings.get_available_models()
    rows = []
    for m in models:
        model_id = m.get("id", "")
        model_name = m.get("name", model_id)
        rows.append([InlineKeyboardButton(text=model_name, callback_data=f"model:{model_id}:{thread_id}")])
    rows.append([InlineKeyboardButton(text="Сбросить (По умолчанию)", callback_data=f"model::{thread_id}")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    current = session.get("model", "") or "по умолчанию"
    await cq.message.edit_text(  # type: ignore[union-attr]
        f"Текущая модель: <b>{current}</b>\n\nВыберите модель:",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await cq.answer()


# ── Model selection ──────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("model:"))
async def cb_model(cq: CallbackQuery) -> None:
    assert cq.from_user and cq.message
    parts = cq.data.split(":", 2)  # type: ignore[union-attr]

    # Format: model:<model_id>:<thread_id>  (from model_menu)
    # or      model:<model_id>              (from /model command inline)
    if len(parts) == 3:
        model = parts[1]
        thread_id = int(parts[2]) if parts[2] else None
    else:
        model = parts[1] if len(parts) > 1 else ""
        # Try to get thread_id from message context
        thread_id = cq.message.message_thread_id  # type: ignore[union-attr]

    if thread_id is None:
        await cq.answer("Не удалось определить ветку", show_alert=True)
        return

    session = await db.get_session(thread_id)
    if session:
        await db.set_model(thread_id, model)
        label = model or "по умолчанию"
        await cq.message.edit_text(f"Модель изменена на: <b>{label}</b>", parse_mode="HTML")  # type: ignore[union-attr]
        await cq.answer(f"Модель: {label}")
    else:
        await cq.answer("Сессия не найдена", show_alert=True)


# ── Cancel (generic dismiss) ─────────────────────────────────────────────

@router.callback_query(F.data == "cancel")
async def cb_cancel(cq: CallbackQuery) -> None:
    assert cq.message
    try:
        await cq.message.delete()  # type: ignore[union-attr]
    except Exception:
        pass
    await cq.answer()
