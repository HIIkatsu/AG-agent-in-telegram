"""Main Message Handler — router for text, voice, photos, documents.

Forum-topics architecture: routes by message_thread_id.
General topic (thread_id=None) ignores regular prompts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import urllib.parse
from dataclasses import dataclass, field

from aiogram import Bot, F, Router
from aiogram.types import Message

from bot.db import db
from bot.services.agy_runner import run_agy
from bot.services.artifacts import (
    ArtifactError,
    cleanup_task_artifact_directory,
    collect_task_artifacts,
    deliver_task_artifacts,
    is_explicit_artifact_request,
    prepare_task_artifact_directory,
)
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

logger = logging.getLogger(__name__)

_queue_loops: set[int] = set()

_MEDIA_GROUP_DEBOUNCE_SECONDS = 0.8


@dataclass
class _MediaGroup:
    """Files received for one Telegram album while its debounce timer runs."""

    message: Message
    bot: Bot
    attachments: list[str] = field(default_factory=list)
    captions: list[str] = field(default_factory=list)
    timer: asyncio.Task[None] | None = None


# Include the chat in the key because Telegram only guarantees a media group ID
# within the context in which the album was sent.
_media_groups: dict[tuple[int, str], _MediaGroup] = {}

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


async def _flush_media_group(key: tuple[int, str]) -> None:
    """Wait for an album to settle, then enqueue it as one model task."""
    try:
        await asyncio.sleep(_MEDIA_GROUP_DEBOUNCE_SECONDS)
        group = _media_groups.pop(key, None)
        if group is None:
            return

        caption = next((value for value in group.captions if value.strip()), "")
        if not caption:
            caption = (
                f"Пользователь отправил альбом из {len(group.attachments)} вложений. "
                "Проанализируй все вложения вместе."
            )
        await _process(
            group.message,
            caption,
            group.bot,
            files=group.attachments,
        )
    except asyncio.CancelledError:
        # A new album item resets the quiet-period timer.
        return
    except Exception:
        # Background task exceptions otherwise have no handler to report them.
        logger.exception("Failed to flush Telegram media group %s", key[1])


def _buffer_media_group(
    message: Message,
    bot: Bot,
    attachment: str,
    caption: str | None,
) -> bool:
    """Buffer an album item and return whether immediate processing was deferred."""
    media_group_id = message.media_group_id
    if not media_group_id:
        return False

    key = (message.chat.id, media_group_id)
    group = _media_groups.get(key)
    if group is None:
        group = _MediaGroup(message=message, bot=bot)
        _media_groups[key] = group
    elif group.timer is not None:
        group.timer.cancel()

    group.attachments.append(attachment)
    if caption:
        group.captions.append(caption)
    group.timer = asyncio.create_task(_flush_media_group(key))
    return True


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
    
    # Start loop if not running. A pending isolated workspace is an explicit
    # review barrier; queued prompts resume after Accept or Discard.
    from bot.services.task_workspace import task_workspace_manager

    blocking = await task_workspace_manager.get_blocking(thread_id)
    if blocking and thread_id not in _queue_loops:
        pos = await get_queued_count(thread_id)
        try:
            msg = await message.reply(
                f"⏸ Задача поставлена в очередь (позиция {pos}). "
                f"Сначала примените или отбросьте изменения задачи #{blocking.task_id}.",
                disable_notification=True,
            )
            t_reg.append(msg.message_id)
        except Exception:
            pass
    elif not _start_queue_processing(thread_id, bot, chat_id):
        pos = await get_queued_count(thread_id)
        try:
            msg = await message.reply(f"⏳ Задача поставлена в очередь (позиция {pos})", disable_notification=True)
            t_reg.append(msg.message_id)
        except Exception:
            pass


# To store active tracker/agy_task per thread_id for cancellation
_active_tasks: dict[int, tuple[TaskTracker, asyncio.Task | None]] = {}


def _tracker_finish_status(status: object) -> str:
    """Map durable queue states to the task-card renderer's display states."""
    from bot.services.task_service import TaskStatus

    resolved = TaskStatus.parse(status)
    return {
        TaskStatus.DONE: "DONE",
        TaskStatus.FAILED: "ERROR",
        TaskStatus.ERROR: "ERROR",
        TaskStatus.CANCELLED: "CANCELLED",
        TaskStatus.TIMEOUT: "TIMEOUT",
        TaskStatus.INTERRUPTED: "INTERRUPTED",
    }.get(resolved, "ERROR")


def _start_queue_processing(thread_id: int, bot: Bot, chat_id: int) -> bool:
    """Start one queue runner per thread and report whether it was started."""
    if thread_id in _queue_loops:
        return False
    _queue_loops.add(thread_id)
    asyncio.create_task(_process_queue(thread_id, bot, chat_id))
    return True


async def resume_queue_processing(thread_id: int, bot: Bot, chat_id: int) -> None:
    """Resume queued work after a task workspace has been finalized."""
    while thread_id in _queue_loops:
        await asyncio.sleep(0.05)
    _start_queue_processing(thread_id, bot, chat_id)

async def _process_queue(thread_id: int, bot: Bot, chat_id: int) -> None:
    from bot.services.task_service import TaskStatus, finish_task, pop_next_task
    from bot.services.task_service import log_task_event
    from bot.services.task_workspace import task_workspace_manager
    from bot.services.tracker import thread_messages_registry
    
    try:
        while True:
            if await task_workspace_manager.has_blocking(thread_id):
                break
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
                await finish_task(
                    task_id,
                    TaskStatus.FAILED,
                    error="Project session was removed before task execution",
                )
                continue
                
            ws = session["workdir"]
            if not os.path.isdir(ws) and not session.get("is_mounted"):
                os.makedirs(ws, exist_ok=True)
            from bot.modes import get_mode_config
            mode_config = get_mode_config(mode)
            web_search = effective_web_policy(session.get("web_search"), mode_config["web"])

            # Deterministic valid UUIDv5 based on thread_id
            import uuid as _uuid
            _NAMESPACE_TG = _uuid.UUID('6ba7b810-9ed0-11d1-80b4-00c04fd430c8')
            conversation_id = str(_uuid.uuid5(_NAMESPACE_TG, f"thread-{thread_id}"))

            try:
                status_msg = await bot.send_message(
                    chat_id,
                    f"<b>{'⚡ Chat' if execution_profile == 'chat' else '🛠 Code task'}</b>\n└─ [⠋] Обработка...",
                    parse_mode="HTML",
                    message_thread_id=thread_id,
                )
            except Exception as exc:
                logger.exception("Failed to create status message for task #%s", task_id)
                await finish_task(
                    task_id,
                    TaskStatus.FAILED,
                    error=f"Could not create Telegram task status message: {exc}",
                )
                continue

            stop_typing = asyncio.Event()
            typing_task = asyncio.create_task(
                _typing_loop(bot, chat_id, stop_typing, thread_id)
            )
            
            t_reg = thread_messages_registry.setdefault(thread_id, [])
            t_reg.append(status_msg.message_id)
            
            tracker = TaskTracker(
                bot, thread_id, status_msg, ws_dir=ws, 
                commit_hash=None, task_id=task_id, 
                model=model, mode="⚡ Chat" if execution_profile == "chat" else "🛠 Code task"
            )
            await tracker.start()
            # Make Stop effective even while the isolated snapshot is being
            # prepared (before the AGY subprocess exists).
            _active_tasks[thread_id] = (tracker, None)

            agent_ws = ws
            if execution_profile == "code":
                await tracker.on_tool_start("task_workspace", "Создаю изолированный workspace")
                try:
                    workspace = await task_workspace_manager.prepare(
                        task_id,
                        thread_id,
                        ws,
                        allow_initialize=not bool(session.get("is_mounted")),
                    )
                    agent_ws = workspace.task_workdir
                    await log_task_event(
                        task_id,
                        "git",
                        "Isolated task workspace created",
                        workspace.snapshot_commit,
                    )
                    await tracker.on_tool_end("task_workspace", "DONE")
                except Exception as exc:
                    logger.exception("Failed to prepare isolated workspace for task #%s", task_id)
                    await tracker.on_tool_end("task_workspace", "ERROR")
                    final_status = await finish_task(
                        task_id,
                        TaskStatus.FAILED,
                        error=str(exc),
                    )
                    stop_typing.set()
                    typing_task.cancel()
                    _active_tasks.pop(thread_id, None)
                    await tracker.finish(_tracker_finish_status(final_status.status))
                    await asyncio.sleep(0.5)
                    continue
                if tracker.cancelled:
                    final_status = await finish_task(
                        task_id,
                        TaskStatus.CANCELLED,
                        error="Cancelled by user",
                    )
                    stop_typing.set()
                    typing_task.cancel()
                    _active_tasks.pop(thread_id, None)
                    try:
                        await task_workspace_manager.discard(
                            task_id,
                            state="cancelled_before_run",
                            allow_active=True,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to clean cancelled task workspace #%s", task_id
                        )
                    await tracker.finish(_tracker_finish_status(final_status.status))
                    await asyncio.sleep(0.5)
                    continue
            try:
                artifact_dir = await asyncio.to_thread(
                    prepare_task_artifact_directory,
                    task_id,
                )
            except ArtifactError as exc:
                logger.exception("Failed to prepare artifact directory for task #%s", task_id)
                final_status = await finish_task(
                    task_id,
                    TaskStatus.FAILED,
                    error=str(exc),
                )
                if execution_profile == "code":
                    try:
                        await task_workspace_manager.discard(
                            task_id,
                            state="artifact_setup_failed",
                            allow_active=True,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to clean workspace after artifact setup failure for task #%s",
                            task_id,
                        )
                stop_typing.set()
                typing_task.cancel()
                _active_tasks.pop(thread_id, None)
                await tracker.finish(_tracker_finish_status(final_status.status))
                await asyncio.sleep(0.5)
                continue
            agy_task: asyncio.Task | None = None

            try:
                agy_task = asyncio.create_task(run_agy(
                    prompt=prompt,
                    conversation_id=conversation_id,
                    workspace_dir=agent_ws,
                    on_chunk=tracker.feed_text,
                    bot=bot,
                    chat_id=chat_id,
                    tracker=tracker,
                    web_search=web_search,
                    model=model,
                    mode=mode,
                    execution_profile=execution_profile,
                    thread_id=thread_id,
                    artifact_dir=artifact_dir,
                ))
                _active_tasks[thread_id] = (tracker, agy_task)
                full_response = await agy_task
                if "Превышен таймаут" in full_response:
                    target_status = TaskStatus.TIMEOUT
                elif (
                    "❌ **Ошибка выполнения" in full_response
                    or "❌ <b>Песочница AGY не готова." in full_response
                ):
                    target_status = TaskStatus.FAILED
                else:
                    target_status = TaskStatus.DONE
                task_status = (
                    await finish_task(
                        task_id,
                        target_status,
                        result_summary=full_response[:1000],
                    )
                ).status
            except asyncio.CancelledError:
                full_response = ""
                task_status = (
                    await finish_task(
                        task_id,
                        TaskStatus.CANCELLED,
                        error="Cancelled by user",
                    )
                ).status
            except Exception as exc:
                full_response = ""
                task_status = (
                    await finish_task(
                        task_id,
                        TaskStatus.FAILED,
                        error=str(exc),
                    )
                ).status
            finally:
                stop_typing.set()
                typing_task.cancel()
                _active_tasks.pop(thread_id, None)

            if execution_profile == "code":
                try:
                    tracker.has_changes_after_finish = await task_workspace_manager.has_changes(task_id)
                    if tracker.has_changes_after_finish:
                        await task_workspace_manager.mark_pending(task_id)
                        await log_task_event(task_id, "git", "Task changes are pending review")
                except Exception as exc:
                    # A broken task Git state must block the queue, not silently fall
                    # back to destructive source-wide operations.
                    tracker.has_changes_after_finish = True
                    await task_workspace_manager.mark_pending(task_id)
                    await log_task_event(task_id, "error", "Could not render task diff", str(exc))

            await tracker.finish(_tracker_finish_status(task_status))

            artifact_cleanup_safe = True
            try:
                task_artifacts = await collect_task_artifacts(task_id)
            except ArtifactError:
                # Do not attempt cleanup after a failed safety inspection: the
                # directory is intentionally retained for an operator to
                # inspect rather than following a surprising path.
                artifact_cleanup_safe = False
                task_artifacts = None
                logger.exception("Failed to collect task artifacts for task #%s", task_id)

            if (
                task_artifacts is not None
                and TaskStatus.parse(task_status) == TaskStatus.DONE
                and is_explicit_artifact_request(prompt)
            ):
                from bot.services.tracker import rollback_registry

                rlist = rollback_registry.setdefault(status_msg.message_id, [])
                report = await deliver_task_artifacts(
                    bot,
                    chat_id,
                    task_artifacts,
                    thread_id=thread_id,
                    rollback_list=rlist,
                )
                if report.failed or task_artifacts.skipped:
                    artifact_cleanup_safe = False
                    logger.warning(
                        "Task artifact delivery incomplete for task #%s: delivered=%d failed=%d skipped=%d",
                        task_id,
                        report.delivered,
                        report.failed,
                        len(task_artifacts.skipped),
                    )
                    try:
                        notice = await bot.send_message(
                            chat_id,
                            "⚠️ Не все созданные файлы удалось отправить. "
                            "Они сохранены на сервере для безопасной проверки.",
                            message_thread_id=thread_id,
                        )
                        rlist.append(notice.message_id)
                    except Exception:
                        logger.exception(
                            "Failed to report incomplete artifact delivery for task #%s",
                            task_id,
                        )

            if artifact_cleanup_safe:
                try:
                    await asyncio.to_thread(cleanup_task_artifact_directory, task_id)
                except ArtifactError:
                    logger.exception("Failed to clean task artifact directory for task #%s", task_id)

            if tracker.has_changes_after_finish:
                # Do not let the next task branch from a stale source snapshot.
                break

            if execution_profile == "chat":
                await asyncio.sleep(0.5)
                continue

            try:
                await task_workspace_manager.discard(
                    task_id,
                    state="unchanged",
                    allow_active=True,
                )
            except Exception:
                logger.exception("Failed to clean unchanged task workspace #%s", task_id)
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
    if _buffer_media_group(message, bot, rel_path, message.caption):
        return
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
    if _buffer_media_group(message, bot, rel_path, message.caption):
        return
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
