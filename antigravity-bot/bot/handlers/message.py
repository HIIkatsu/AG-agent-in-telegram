"""Main Message Handler — router for text, voice, photos, documents.

Forum-topics architecture: routes by message_thread_id.
General topic (thread_id=None) ignores regular prompts.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import urllib.parse

from aiogram import Bot, F, Router
from aiogram.types import Message

from bot.config import settings
from bot.db import db
from bot.services.agy_runner import run_agy
from bot.services.artifacts import collect_task_artifacts, deliver_and_cleanup_artifacts

from bot.services.git_manager import git_manager
from bot.services.execution_profiles import (
    CHAT_MEMORY_CHAR_BUDGET,
    CODE_MEMORY_CHAR_BUDGET,
    classify_execution_profile,
    effective_mode,
    effective_web_policy,
    select_relevant_notes,
)
from bot.services.tracker import TaskTracker
from bot.services.voice import transcribe_voice

router = Router(name="message")

_queue_loops: set[int] = set()

_VOICE_FRAMES = ["🎙 ⠋ Распознаю...", "🎙 ⠙ Распознаю...", "🎙 ⠹ Распознаю...", "🎙 ⠸ Распознаю..."]

def _normalize_filename(name: str) -> str:
    """Normalize uploaded filename to a safe basename with bounded length."""
    name = os.path.basename(urllib.parse.unquote(name))
    name = re.sub(r'[^\w\.-]', '_', name)
    name = re.sub(r'_+', '_', name).strip('._')
    if not name:
        return "uploaded_file"
    stem, ext = os.path.splitext(name)
    return f"{stem[:80]}{ext[:16]}"


@router.message(F.forum_topic_created)
async def topic_created_handler(message: Message) -> None:
    """Handle new topic creation to save its name."""
    thread_id = message.message_thread_id
    if not thread_id:
        return
    topic_name = message.forum_topic_created.name if message.forum_topic_created else "Новая ветка"
    # Just update the DB if it exists, or create if not
    await db.get_or_create_session(thread_id)
    await db.conn.execute("UPDATE thread_sessions SET topic_name = ? WHERE thread_id = ?", (topic_name, thread_id))
    await db.conn.commit()


@router.message(F.forum_topic_edited)
async def topic_edited_handler(message: Message) -> None:
    """Handle topic rename."""
    thread_id = message.message_thread_id
    if not thread_id or not message.forum_topic_edited:
        return
    new_name = message.forum_topic_edited.name
    if new_name:
        await db.conn.execute("UPDATE thread_sessions SET topic_name = ? WHERE thread_id = ?", (new_name, thread_id))
        await db.conn.commit()


async def _typing_loop(bot: Bot, chat_id: int, stop_event: asyncio.Event, thread_id: int | None = None) -> None:
    """Send typing action periodically until stop_event is set."""
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id, "typing", message_thread_id=thread_id)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass


async def _animate_voice(msg: Message, stop_event: asyncio.Event) -> None:
    """Animate voice status message cleanly."""
    idx = 0
    while not stop_event.is_set():
        idx = (idx + 1) % len(_VOICE_FRAMES)
        try:
            await msg.edit_text(_VOICE_FRAMES[idx])
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=0.6)
        except asyncio.TimeoutError:
            pass


async def _process(
    message: Message,
    text: str,
    bot: Bot,
    files: list[str] | None = None,
) -> None:
    assert message.from_user
    thread_id = message.message_thread_id

    # General topic → ignore regular prompts
    if thread_id is None:
        return

    chat_id = message.chat.id  # group chat ID for Telegram API
    
    # Register messages for deep rollback tracking
    from bot.services.tracker import thread_messages_registry
    t_reg = thread_messages_registry.setdefault(thread_id, [])
    t_reg.append(message.message_id)

    session = await db.get_or_create_session(thread_id)
    await db.update_last_used(thread_id)

    session_mode = str(session.get("mode", "code"))
    execution_profile = classify_execution_profile(text, has_attachments=bool(files))
    mode = effective_mode(session_mode, execution_profile)

    # Build a profile-aware prompt. Fast chat gets relevant bounded memory only;
    # project paths are deliberately omitted so they do not provoke file tools.
    prompt = text
    prompt_sections: list[str] = []
    if files:
        file_list = "\n".join(f"- {f}" for f in files)
        prompt_sections.append(f"Прикрепленные файлы в рабочей директории:\n{file_list}")

    if execution_profile == "code":
        context_files = await db.list_context_files(thread_id)
        if context_files:
            ctx = "\n".join(f"- {row['path']}" for row in context_files[:30])
            prompt_sections.append(f"pinned_files:\n{ctx}")

    memory_notes = await db.list_memory_notes(thread_id)
    if memory_notes:
        budget = (
            CHAT_MEMORY_CHAR_BUDGET
            if execution_profile == "chat"
            else CODE_MEMORY_CHAR_BUDGET
        )
        relevant_notes = select_relevant_notes(memory_notes, text, char_budget=budget)
        if relevant_notes:
            mem = "\n".join(f"- {note}" for note in relevant_notes)
            prompt_sections.append(f"relevant_notes:\n{mem}")

    if prompt_sections:
        prompt = f"{text}\n\n[" + "\n\n".join(prompt_sections) + "]"

    model: str = session.get("model", "") or "Gemini 3.6 flash (high)"
    project_id: int = session.get("project_id", 0) or 0
    
    # Enqueue task to database
    from bot.services.task_service import enqueue_task, get_queued_count
    await enqueue_task(
        thread_id=thread_id, 
        chat_id=chat_id, 
        project_id=project_id, 
        prompt=prompt, 
        mode=mode, 
        model=model
    )
    
    # Start loop if not running
    if thread_id not in _queue_loops:
        _queue_loops.add(thread_id)
        asyncio.create_task(_process_queue(thread_id, bot, chat_id))
    else:
        pos = await get_queued_count(thread_id)
        try:
            msg = await message.reply(f"⏳ Задача поставлена в очередь (позиция {pos})", disable_notification=True)
            t_reg.append(msg.message_id)
        except Exception:
            pass


# To store active tracker/agy_task per thread_id for cancellation
_active_tasks: dict[int, tuple[TaskTracker, asyncio.Task | None]] = {}

async def _process_queue(thread_id: int, bot: Bot, chat_id: int) -> None:
    from bot.services.task_service import pop_next_task, finish_task
    from bot.services.tracker import thread_messages_registry
    
    try:
        while True:
            task = await pop_next_task(thread_id)
            if not task:
                break
            
            task_id = task["id"]
            prompt = task["prompt"]
            model = task["model"]
            mode = task.get("mode", "code")
            execution_profile = "chat" if mode == "chat" else "code"
            
            session = await db.get_session(thread_id)
            if not session:
                break
                
            ws = session["workdir"]
            os.makedirs(ws, exist_ok=True)
            from bot.modes import get_mode_config
            mode_config = get_mode_config(mode)
            web_search = effective_web_policy(session.get("web_search"), mode_config["web"])

            # Deterministic valid UUIDv5 based on thread_id
            import uuid as _uuid
            _NAMESPACE_TG = _uuid.UUID('6ba7b810-9ed0-11d1-80b4-00c04fd430c8')
            conversation_id = str(_uuid.uuid5(_NAMESPACE_TG, f"thread-{thread_id}"))

            stop_typing = asyncio.Event()
            typing_task = asyncio.create_task(_typing_loop(bot, chat_id, stop_typing, thread_id))

            status_msg = await bot.send_message(
                chat_id, 
                f"<b>{'⚡ Chat' if execution_profile == 'chat' else '🛠 Code task'}</b>\n└─ [⠋] Обработка...",
                parse_mode="HTML",
                message_thread_id=thread_id
            )
            
            t_reg = thread_messages_registry.setdefault(thread_id, [])
            t_reg.append(status_msg.message_id)
            
            tracker = TaskTracker(
                bot, thread_id, status_msg, ws_dir=ws, 
                commit_hash=None, task_id=task_id, 
                model=model, mode="⚡ Chat" if execution_profile == "chat" else "🛠 Code task"
            )
            await tracker.start()

            import time as _time
            artifacts_started_at = _time.time()
            commit_hash: str | None = None
            if execution_profile == "code":
                await tracker.on_tool_start("git_checkpoint", "Создаю checkpoint")
                try:
                    commit_hash = await git_manager.create_checkpoint_async(ws, label=prompt[:25], timeout=15)
                    tracker.commit_hash = commit_hash
                    await tracker.on_tool_end("git_checkpoint", "DONE")
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning("Git checkpoint unavailable for %s", ws, exc_info=True)
                    await tracker.on_tool_end("git_checkpoint", "ERROR")
            agy_task: asyncio.Task | None = None

            try:
                agy_task = asyncio.create_task(run_agy(
                    prompt=prompt,
                    conversation_id=conversation_id,
                    workspace_dir=ws,
                    on_chunk=tracker.feed_text,
                    bot=bot,
                    chat_id=chat_id,
                    tracker=tracker,
                    web_search=web_search,
                    model=model,
                    mode=mode,
                    execution_profile=execution_profile,
                    thread_id=thread_id,
                ))
                _active_tasks[thread_id] = (tracker, agy_task)
                full_response = await agy_task
                task_status = "timeout" if "Превышен таймаут" in full_response else "done"
                await finish_task(task_id, task_status, result_summary=full_response[:1000])
            except asyncio.CancelledError:
                full_response = ""
                task_status = "cancelled"
                await finish_task(task_id, "cancelled", error="Cancelled by user")
            except Exception as exc:
                full_response = ""
                task_status = "failed"
                await finish_task(task_id, "failed", error=str(exc))
            finally:
                stop_typing.set()
                typing_task.cancel()
                _active_tasks.pop(thread_id, None)

            # Finalize the visible answer before any artifact scanning/diff work so the
            # user does not wait on slower post-processing after the model is done.
            if execution_profile == "code":
                try:
                    tracker.has_changes_after_finish = await git_manager.has_changes_async(ws, timeout=5)
                except Exception:
                    tracker.has_changes_after_finish = False

            await tracker.finish("TIMEOUT" if task_status == "timeout" else "ERROR" if task_status == "failed" else "CANCELLED" if task_status == "cancelled" else "DONE")

            if execution_profile == "chat" or task_status == "cancelled":
                await asyncio.sleep(0.5)
                continue

            # Artifact post-tool processing: git changed files + bounded scratchpad scan.
            new_files = await collect_task_artifacts(ws, artifacts_started_at)

            import shutil
            synced_files: list[str] = []
            for fpath in new_files:
                if settings.workspaces_dir not in fpath and os.path.exists(fpath):
                    dst = os.path.join(ws, os.path.basename(fpath))
                    try:
                        shutil.copy2(fpath, dst)
                        synced_files.append(dst)
                    except Exception:
                        synced_files.append(fpath)
                else:
                    synced_files.append(fpath)

            if synced_files:
                from bot.services.tracker import rollback_registry
                rlist = rollback_registry.setdefault(status_msg.message_id, [])
                await deliver_and_cleanup_artifacts(bot, chat_id, synced_files, thread_id=thread_id, rollback_list=rlist)

            await asyncio.sleep(0.5)
            
    finally:
        _queue_loops.discard(thread_id)


# -- Text --
@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, bot: Bot) -> None:
    assert message.text
    await _process(message, message.text, bot)


# -- Photo --
@router.message(F.photo)
async def on_photo(message: Message, bot: Bot) -> None:
    assert message.from_user
    thread_id = message.message_thread_id
    if thread_id is None:
        return

    session = await db.get_or_create_session(thread_id)
    ws = session["workdir"]
    uploads_dir = os.path.join(ws, "uploads")
    os.makedirs(uploads_dir, exist_ok=True)

    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    assert file.file_path
    ext = os.path.splitext(file.file_path)[1] or ".jpg"
    filename = _normalize_filename(f"photo_{photo.file_unique_id}{ext}")
    local_path = os.path.join(uploads_dir, filename)
    await bot.download_file(file.file_path, local_path)

    caption = message.caption or "Пользователь отправил фото. Проанализируй его."
    rel_path = f"uploads/{filename}"
    prompt = f"{caption}\n\n[Фото сохранено: {rel_path}]"
    await _process(message, prompt, bot, files=[rel_path])


# -- Document / File --
@router.message(F.document)
async def on_document(message: Message, bot: Bot) -> None:
    assert message.from_user and message.document
    thread_id = message.message_thread_id
    if thread_id is None:
        return

    session = await db.get_or_create_session(thread_id)
    ws = session["workdir"]
    uploads_dir = os.path.join(ws, "uploads")
    os.makedirs(uploads_dir, exist_ok=True)

    doc = message.document
    file = await bot.get_file(doc.file_id)
    assert file.file_path
    filename = doc.file_name or f"file_{doc.file_unique_id}"
    filename = _normalize_filename(filename)
    
    # Handle duplicates
    base, ext = os.path.splitext(filename)
    counter = 1
    local_path = os.path.join(uploads_dir, filename)
    while os.path.exists(local_path):
        filename = f"{base}_{counter}{ext}"
        local_path = os.path.join(uploads_dir, filename)
        counter += 1
        
    await bot.download_file(file.file_path, local_path)

    caption = message.caption or f"Пользователь отправил файл: {filename}"
    rel_path = f"uploads/{filename}"
    prompt = f"{caption}\n\n[Файл сохранен: {rel_path}]"
    await _process(message, prompt, bot, files=[rel_path])


# -- Voice --
@router.message(F.voice)
async def on_voice(message: Message, bot: Bot) -> None:
    assert message.voice and message.from_user
    thread_id = message.message_thread_id
    if thread_id is None:
        return

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(bot, message.chat.id, stop_typing, thread_id))

    status_msg = await message.answer(_VOICE_FRAMES[0])
    stop_anim = asyncio.Event()
    anim_task = asyncio.create_task(_animate_voice(status_msg, stop_anim))

    try:
        file = await bot.get_file(message.voice.file_id)
        assert file.file_path
        tmp = tempfile.mktemp(suffix=".ogg", dir="/tmp")
        await bot.download_file(file.file_path, tmp)

        text = await transcribe_voice(tmp)
        stop_anim.set()
        anim_task.cancel()

        if not text or not text.strip():
            await status_msg.edit_text("Не удалось распознать речь.")
            return

        try:
            await status_msg.delete()
        except Exception:
            pass
        preview = text[:500] + "..." if len(text) > 500 else text
        await message.answer(
            f"<b>Распознано:</b>\n\n<blockquote>{preview}</blockquote>",
            parse_mode="HTML",
        )

        await _process(message, text, bot)
    except Exception as e:
        stop_anim.set()
        anim_task.cancel()
        try:
            await status_msg.edit_text(f"Ошибка голосового сообщения: {e}")
        except Exception:
            await message.answer(f"Ошибка голосового сообщения: {e}")
    finally:
        stop_typing.set()
        typing_task.cancel()
