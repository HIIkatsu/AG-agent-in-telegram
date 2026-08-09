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


# ── Master Panel Callbacks ──────────────────────────────────────────────────

@router.callback_query(F.data == "manage_sessions_menu:0")
async def cb_manage_sessions_menu(cq: CallbackQuery, bot: Bot) -> None:
    from bot.handlers.chats import build_sessions_manage_panel
    text, kb = await build_sessions_manage_panel()
    try:
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass

@router.callback_query(F.data == "back_to_master")
async def cb_back_to_master(cq: CallbackQuery, bot: Bot) -> None:
    from bot.handlers.chats import build_master_panel
    text, kb = await build_master_panel()
    try:
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass

@router.callback_query(F.data == "clean_dead_topics")
async def cb_clean_dead_topics(cq: CallbackQuery, bot: Bot) -> None:
    await cq.answer("Запуск проверки... Это может занять несколько секунд.", show_alert=False)
    from bot.handlers.chats import purge_dead_topics, build_sessions_manage_panel
    dead_count = await purge_dead_topics(bot)
    
    text, kb = await build_sessions_manage_panel()
    try:
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    
    await cq.answer(f"Проверка завершена. Удалено сессий: {dead_count}", show_alert=True)


@router.callback_query(F.data == "purge_cli_sessions")
async def cb_purge_cli_sessions(cq: CallbackQuery) -> None:
    from bot.handlers.chats import purge_stale_cli_sessions
    purged = purge_stale_cli_sessions()
    await cq.answer(f"Удалено зависших процессов: {purged}", show_alert=True)


@router.callback_query(F.data.startswith("kill_session:"))
async def cb_kill_session(cq: CallbackQuery, bot: Bot) -> None:
    parts = cq.data.split(":")  # type: ignore[union-attr]
    if len(parts) < 2:
        return
    try:
        tid = int(parts[1])
    except ValueError:
        return

    # Delete session from DB
    session = await db.delete_session(tid)
    if not session:
        await cq.answer(f"Сессия {tid} уже удалена.", show_alert=True)
    else:
        # Cleanup files
        import shutil
        workdir = session.get("workdir", "")
        is_mounted = session.get("is_mounted", 0)
        if not is_mounted and workdir and workdir.startswith(settings.workspaces_dir):
            try:
                shutil.rmtree(workdir, ignore_errors=True)
            except Exception:
                pass
        await cq.answer(f"Сессия {tid} и её файлы удалены.", show_alert=False)

    # Refresh manage panel in place
    from bot.handlers.chats import build_sessions_manage_panel
    text, kb = await build_sessions_manage_panel()
    try:
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass


@router.callback_query(F.data == "purge_cli_sessions")
async def cb_purge_cli_sessions(cq: CallbackQuery, bot: Bot) -> None:
    from bot.handlers.chats import purge_stale_cli_sessions
    purged = purge_stale_cli_sessions()
    await cq.answer(f"Успешно очищено зависших фоновых процессов: {purged}", show_alert=True)


# ── Git View Diff ────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("t:df:") | F.data.startswith("view_diff:"))
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

@router.callback_query(F.data.startswith("t:ac:") | F.data.startswith("accept_diff:"))
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
    # Parse commit_hash if available
    parts = cq.data.split(":")
    commit_hash = parts[2] if len(parts) > 2 else None

    # 1. Execute Git Rollback
    if commit_hash:
        ok = git_manager.rollback_to_commit(ws, commit_hash)
    else:
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

        # 2.5 Delete telegram messages associated with this deep rollback
        from bot.services.tracker import thread_messages_registry
        t_reg = thread_messages_registry.get(thread_id, [])
        to_delete = [msg_id for msg_id in t_reg if msg_id > cq.message.message_id]
        
        if to_delete:
            # Clean up the registry
            thread_messages_registry[thread_id] = [msg_id for msg_id in t_reg if msg_id <= cq.message.message_id]
            
            # Delete in chunks of 100
            for i in range(0, len(to_delete), 100):
                chunk = to_delete[i:i+100]
                try:
                    await bot.delete_messages(cq.message.chat.id, chunk)
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning("Failed to delete deep rollback messages: %s", e)

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


# ── Task Management Callbacks ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("t:st:") | F.data.startswith("cancel_task:"))
async def cb_cancel_task(cq: CallbackQuery) -> None:
    assert cq.message
    from bot.handlers.message import _active_tasks
    from bot.services.task_service import cancel_task

    thread_id = cq.message.message_thread_id  # type: ignore[union-attr]
    if thread_id is None:
        await cq.answer("Нечего отменять")
        return

    task_id = int(cq.data.split(":")[1])
    
    # Check if running
    entry = _active_tasks.get(thread_id)
    if entry:
        tracker, agy_task = entry
        if tracker.task_id == task_id:
            await tracker.cancel()
            if agy_task and not agy_task.done():
                agy_task.cancel()
            await cq.answer("Генерация отменена!")
            return
            
    # If not running, just cancel in DB
    await cancel_task(task_id)
    await cq.answer("Задача отменена!")


@router.callback_query(F.data == "cancel_gen")
async def cb_cancel_gen_legacy(cq: CallbackQuery) -> None:
    await cq.answer("Используйте новую систему задач.", show_alert=True)


@router.callback_query(F.data.startswith("t:ss:") | F.data.startswith("task_status:"))
async def cb_task_status(cq: CallbackQuery) -> None:
    await cq.answer("Задача выполняется, пожалуйста подождите...", show_alert=True)


@router.callback_query(F.data.startswith("t:rt:") | F.data.startswith("retry_task:"))
async def cb_retry_task(cq: CallbackQuery) -> None:
    await cq.answer("Повтор задачи пока не реализован.", show_alert=True)


@router.callback_query(F.data.startswith("t:lg:") | F.data.startswith("view_logs:"))
async def cb_view_logs(cq: CallbackQuery) -> None:
    await cq.answer("Логи пока недоступны.", show_alert=True)


@router.callback_query(F.data.startswith("clear_queue:"))
async def cb_clear_queue(cq: CallbackQuery) -> None:
    from bot.services.task_service import cancel_queue
    thread_id = int(cq.data.split(":")[1])
    await cancel_queue(thread_id)
    await cq.answer("Очередь очищена!")


@router.callback_query(F.data.startswith("refresh_dashboard:"))
async def cb_refresh_dashboard(cq: CallbackQuery, bot: Bot) -> None:
    from bot.handlers.dashboard import cmd_project
    try:
        await cq.message.delete()
    except Exception:
        pass
    await cmd_project(cq.message, bot)
    await cq.answer("Обновлено")


@router.callback_query(F.data.startswith("mount_info:"))
async def cb_mount_info(cq: CallbackQuery) -> None:
    await cq.answer(
        "Чтобы привязать локальную папку сервера к этому топику, введите команду: /mount <абсолютный_путь>",
        show_alert=True
    )

@router.callback_query(F.data.startswith("set_mode:"))
async def cb_set_mode_quick(cq: CallbackQuery) -> None:
    parts = cq.data.split(":")
    mode = parts[1]
    thread_id = int(parts[2])
    await db.set_mode(thread_id, mode)
    await cq.answer(f"Режим переключен на: {mode}")
    # Refresh dashboard
    from bot.handlers.dashboard import build_dashboard_content
    text, kb = await build_dashboard_content(thread_id)
    try:
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass

@router.callback_query(F.data.startswith("view_files:") | F.data.startswith("run_tests:") | F.data.startswith("run_deploy:") | F.data.startswith("server_panel:") | F.data.startswith("view_context:"))
async def cb_placeholder_action(cq: CallbackQuery) -> None:
    await cq.answer("Функция будет доступна в следующем релизе!", show_alert=True)


# ── Web search toggle ────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("web_toggle:"))
async def cb_web_toggle(cq: CallbackQuery) -> None:
    assert cq.from_user and cq.message
    thread_id = int(cq.data.split(":")[1])  # type: ignore[union-attr]
    new_val = await db.toggle_web_search(thread_id)
    session = await db.get_session(thread_id)
    if session:
        if thread_id == 0:
            from bot.handlers.chats import build_master_panel
            text, kb = await build_master_panel()
        else:
            from bot.handlers.dashboard import build_dashboard_content
            text, kb = await build_dashboard_content(thread_id)
        
        try:
            await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
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
    
    rows.append([InlineKeyboardButton(text="Сбросить (Gemini 3.6 flash high)", callback_data=f"model::{thread_id}")])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"back_to_dash:{thread_id}")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    current = session.get("model", "") or "Gemini 3.6 flash (high)"
    await cq.message.edit_text(  # type: ignore[union-attr]
        f"Текущая модель: <b>{current}</b>\n\nВыберите модель:",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await cq.answer()


# ── Model selection ──────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("back_to_dash:"))
async def cb_back_to_dash(cq: CallbackQuery) -> None:
    assert cq.message
    thread_id = int(cq.data.split(":")[1])  # type: ignore[union-attr]
    if thread_id == 0:
        from bot.handlers.chats import build_master_panel
        text, kb = await build_master_panel()
    else:
        from bot.handlers.dashboard import build_dashboard_content
        text, kb = await build_dashboard_content(thread_id)
        
    try:
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await cq.answer()


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
        model = parts[1]
        thread_id = None

    if not thread_id:
        thread_id = cq.message.message_thread_id  # type: ignore[union-attr]
        if thread_id is None:
            await cq.answer("Не удалось определить ветку.")
    await db.set_model(thread_id, model)
    
    # Refresh UI
    if thread_id == 0:
        from bot.handlers.chats import build_master_panel
        text, kb = await build_master_panel()
    else:
        from bot.handlers.dashboard import build_dashboard_content
        text, kb = await build_dashboard_content(thread_id)
        
    try:
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    
    await cq.answer("Изменения сохранены!")


# ── Cancel (generic dismiss) ─────────────────────────────────────────────

@router.callback_query(F.data == "cancel")
async def cb_cancel(cq: CallbackQuery) -> None:
    assert cq.message
    try:
        await cq.message.delete()  # type: ignore[union-attr]
    except Exception:
        pass
    await cq.answer()
