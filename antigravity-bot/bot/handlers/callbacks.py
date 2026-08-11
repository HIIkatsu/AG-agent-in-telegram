"""Inline callback handlers: permissions HITL, Git Diff, Rollback, model selection, web toggle, cancel."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import settings
from bot.db import db
from bot.services.diff_viewer import generate_diff_html_file
from bot.services.git_manager import git_manager
from bot.services.permissions import permission_handler
from bot.services.task_workspace import (
    TaskWorkspaceConflict,
    TaskWorkspaceError,
    task_workspace_manager,
)

router = Router(name="callbacks")
logger = logging.getLogger(__name__)


def _short_error(exc: BaseException, limit: int = 120) -> str:
    return str(exc).replace("\n", " ")[:limit]


async def _resolve_thread_from_callback(last_part: str) -> int | None:
    """Callbacks may carry either thread_id or task_id; resolve both safely."""
    try:
        value = int(last_part)
    except ValueError:
        return None
    session = await db.get_session(value)
    if session:
        return value
    from bot.services.task_service import get_task
    task = await get_task(value)
    return int(task["thread_id"]) if task else None


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
    parts = cq.data.split(":")  # type: ignore[union-attr]
    if cq.data.startswith("t:df:"):  # type: ignore[union-attr]
        try:
            task_id = int(parts[-1])
            workspace = await task_workspace_manager.get(task_id)
            if not workspace:
                await cq.answer("Изолированный workspace задачи не найден", show_alert=True)
                return
            thread_id = workspace.thread_id
            raw_diff = await task_workspace_manager.diff(task_id)
            title = f"Task {task_id}"
        except (ValueError, TaskWorkspaceError) as exc:
            await cq.answer(
                f"Не удалось прочитать diff: {_short_error(exc)}", show_alert=True
            )
            return
    else:
        thread_id = await _resolve_thread_from_callback(parts[-1])
        if thread_id is None:
            await cq.answer("Не удалось определить ветку", show_alert=True)
            return
        session = await db.get_session(thread_id)
        if not session:
            await cq.answer("Сессия не найдена", show_alert=True)
            return
        try:
            raw_diff = await git_manager.get_diff_async(session["workdir"])
        except Exception as exc:
            await cq.answer(
                f"Git diff недоступен: {_short_error(exc)}", show_alert=True
            )
            return
        title = f"Thread {thread_id}"

    if not raw_diff or not raw_diff.strip():
        await cq.answer("Нет измененных файлов.", show_alert=True)
        return

    await cq.answer("Генерация diff.html...")
    diff_file_path = generate_diff_html_file(raw_diff, title)
    doc = FSInputFile(diff_file_path, filename="diff.html")
    await bot.send_document(
        cq.message.chat.id,
        doc,
        caption="👀 <b>Diff изменений (VS Code Side-by-Side View):</b>\nОткройте файл в браузере для удобного просмотра.",
        parse_mode="HTML",
        message_thread_id=thread_id,
    )


# ── Git Accept Diff ──────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("t:ac:"))
async def cb_accept_diff(cq: CallbackQuery, bot: Bot) -> None:
    assert cq.from_user and cq.message
    try:
        task_id = int(cq.data.split(":")[-1])  # type: ignore[union-attr]
    except ValueError:
        await cq.answer("Некорректный ID задачи", show_alert=True)
        return
    workspace = await task_workspace_manager.get(task_id)
    if not workspace:
        await cq.answer("Изолированный workspace задачи не найден", show_alert=True)
        return
    try:
        changed = await task_workspace_manager.accept(task_id)
    except TaskWorkspaceConflict as exc:
        await cq.answer(
            "Конфликт: исходный проект изменился. Workspace сохранён. "
            f"{_short_error(exc, 90)}",
            show_alert=True,
        )
        return
    except TaskWorkspaceError as exc:
        await cq.answer(
            f"Не удалось применить изменения: {_short_error(exc)}", show_alert=True
        )
        return
    from bot.handlers.message import resume_queue_processing
    from bot.services.task_service import log_task_event

    await resume_queue_processing(workspace.thread_id, bot, cq.message.chat.id)
    try:
        await log_task_event(
            task_id,
            "git",
            "Task patch applied to source workspace" if changed else "Task had no changes",
        )
    except Exception:
        logger.exception("Failed to log acceptance of task #%s", task_id)
    await cq.answer("Изменения этой задачи применены")
    try:
        await cq.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# ── Task Workspace Discard ────────────────────────────────────────────────

@router.callback_query(F.data.startswith("t:rb:"))
async def cb_rollback(cq: CallbackQuery, bot: Bot) -> None:
    assert cq.from_user and cq.message
    try:
        task_id = int(cq.data.split(":")[-1])  # type: ignore[union-attr]
    except ValueError:
        await cq.answer("Некорректный ID задачи", show_alert=True)
        return
    workspace = await task_workspace_manager.get(task_id)
    if not workspace:
        await cq.answer("Изолированный workspace задачи не найден", show_alert=True)
        return
    try:
        await task_workspace_manager.discard(task_id)
    except TaskWorkspaceError as exc:
        await cq.answer(
            f"Не удалось отбросить изменения: {_short_error(exc)}", show_alert=True
        )
        return
    from bot.handlers.message import resume_queue_processing
    from bot.services.task_service import log_task_event

    await resume_queue_processing(workspace.thread_id, bot, cq.message.chat.id)
    try:
        await log_task_event(
            task_id, "git", "Task workspace discarded; source was untouched"
        )
    except Exception:
        logger.exception("Failed to log discard of task #%s", task_id)
    await cq.answer("Изменения задачи отброшены; исходный проект не изменялся")
    try:
        await cq.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data.startswith("accept_diff:") | F.data.startswith("rollback:"))
async def cb_unsafe_legacy_git_action(cq: CallbackQuery) -> None:
    """Refuse source-wide buttons left in messages created by older releases."""
    await cq.answer(
        "Массовый accept/rollback отключён: используйте кнопки конкретной задачи.",
        show_alert=True,
    )


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

    parts = cq.data.split(":")
    task_id = int(parts[-1])
    
    # Check if running
    entry = _active_tasks.get(thread_id)
    if entry:
        tracker, agy_task = entry
        if tracker.task_id == task_id:
            await tracker.cancel()
            if agy_task and not agy_task.done():
                agy_task.cancel()
                try:
                    await agy_task
                except asyncio.CancelledError:
                    pass
            from bot.services.task_service import cancel_queue
            await cancel_queue(thread_id)
            await cq.answer("Текущая генерация и вся очередь отменены!")
            return
            
    # If not running, just cancel in DB
    await cancel_task(task_id)
    await cq.answer("Задача отменена!")


@router.callback_query(F.data == "cancel_gen")
async def cb_cancel_gen_legacy(cq: CallbackQuery) -> None:
    await cq.answer("Используйте новую систему задач.", show_alert=True)


@router.callback_query(F.data.startswith("t:ss:") | F.data.startswith("task_status:"))
async def cb_task_status(cq: CallbackQuery) -> None:
    assert cq.message
    from bot.handlers.ide import build_task_card
    task_id = int(cq.data.split(":")[-1])  # type: ignore[union-attr]
    text, kb = await build_task_card(task_id)
    try:
        await cq.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await cq.answer("Статус обновлён")


@router.callback_query(F.data.startswith("t:rt:") | F.data.startswith("retry_task:"))
async def cb_retry_task(cq: CallbackQuery, bot: Bot) -> None:
    assert cq.message
    from bot.services.task_service import enqueue_task, get_task
    task_id = int(cq.data.split(":")[-1])  # type: ignore[union-attr]
    task = await get_task(task_id)
    if not task:
        await cq.answer("Задача не найдена", show_alert=True)
        return
    new_id = await enqueue_task(
        thread_id=task["thread_id"],
        chat_id=task["chat_id"],
        project_id=task.get("project_id") or 0,
        prompt=task["prompt"],
        mode=task.get("mode") or "code",
        model=task.get("model"),
        retry_of_task_id=task_id,
    )
    from bot.handlers.message import _start_queue_processing
    _start_queue_processing(task["thread_id"], bot, task["chat_id"])
    await cq.answer(f"Задача #{new_id} добавлена в очередь")


@router.callback_query(F.data.startswith("t:lg:") | F.data.startswith("view_logs:"))
async def cb_view_logs(cq: CallbackQuery) -> None:
    assert cq.message
    import html
    task_id = int(cq.data.split(":")[-1])  # type: ignore[union-attr]
    cur = await db.conn.execute("SELECT * FROM task_logs WHERE task_id = ? ORDER BY id DESC LIMIT 40", (task_id,))
    rows = [dict(r) for r in await cur.fetchall()]
    if not rows:
        await cq.answer("Логов пока нет", show_alert=True)
        return
    rows.reverse()
    text = "📄 <b>Логи задачи #{}</b>\n\n".format(task_id) + "\n".join(
        f"• <code>{html.escape(r['level'])}</code> {html.escape(r['message'][:250])}" for r in rows
    )
    await cq.message.answer(text, parse_mode="HTML")
    await cq.answer("Логи отправлены")


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
    await cq.answer(f"Веб-поиск: {new_val}")


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
