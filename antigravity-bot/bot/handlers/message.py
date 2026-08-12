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
    validate_requested_artifacts,
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
_TEXT_DEBOUNCE_SECONDS = 0.9
_TEXT_JOIN_SEPARATOR = "\n"


@dataclass
class _PendingText:
    """Consecutive Telegram text fragments that belong to one user request."""

    message: Message
    bot: Bot
    parts: list[str] = field(default_factory=list)
    timer: asyncio.Task[None] | None = None


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
_text_groups: dict[tuple[int, int | None, int], _PendingText] = {}

_VOICE_FRAMES = ["🎙 ⠋ Распознаю...", "🎙 ⠙ Распознаю...", "🎙 ⠹ Распознаю...", "🎙 ⠸ Распознаю..."]


async def _safe_message_answer(
    message: Message,
    text: str,
    *,
    label: str,
    retries: int = 2,
    **kwargs,
) -> Message | None:
    """Best-effort Telegram reply that can never discard an incoming task.

    A transcription preview is helpful UI, not a prerequisite for putting the
    recognised user request into the durable queue.  Telegram may return a
    temporary ``429`` while the bot is already updating a task card.
    """
    from bot.services.telegram_rate_limiter import telegram_rate_limiter

    async def answer() -> Message:
        return await message.answer(text, **kwargs)

    try:
        return await telegram_rate_limiter.request(
            message.chat.id,
            answer,
            label=label,
            retries=retries,
        )
    except Exception:
        logger.warning("Telegram reply unavailable (%s)", label, exc_info=True)
        return None


async def _safe_message_reply(
    message: Message,
    text: str,
    *,
    label: str,
    retries: int = 2,
    **kwargs,
) -> Message | None:
    """Best-effort reply variant used for non-essential queue notices."""
    from bot.services.telegram_rate_limiter import telegram_rate_limiter

    async def reply() -> Message:
        return await message.reply(text, **kwargs)

    try:
        return await telegram_rate_limiter.request(
            message.chat.id,
            reply,
            label=label,
            retries=retries,
        )
    except Exception:
        logger.warning("Telegram queue notice unavailable (%s)", label, exc_info=True)
        return None


async def _safe_message_edit(
    message: Message,
    text: str,
    *,
    label: str,
    retries: int = 2,
    **kwargs,
) -> bool:
    """Best-effort status edit; errors are logged instead of escaping a handler."""
    from bot.services.telegram_rate_limiter import telegram_rate_limiter

    async def edit() -> object:
        return await message.edit_text(text, **kwargs)

    try:
        await telegram_rate_limiter.request(
            message.chat.id,
            edit,
            label=label,
            retries=retries,
        )
    except Exception:
        logger.debug("Telegram status edit unavailable (%s)", label, exc_info=True)
        return False
    return True


async def _safe_message_delete(message: Message, *, label: str) -> bool:
    """Best-effort status deletion that respects the per-chat cooldown."""
    from bot.services.telegram_rate_limiter import telegram_rate_limiter

    async def delete() -> object:
        return await message.delete()

    try:
        await telegram_rate_limiter.request(
            message.chat.id,
            delete,
            label=label,
            retries=0,
        )
    except Exception:
        logger.debug("Telegram status deletion unavailable (%s)", label, exc_info=True)
        return False
    return True


async def _safe_bot_send_message(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    label: str,
    **kwargs,
) -> Message | None:
    """Send task UI without coupling task execution to Telegram availability."""
    from bot.services.telegram_rate_limiter import telegram_rate_limiter

    async def send() -> Message:
        return await bot.send_message(chat_id, text, **kwargs)

    try:
        return await telegram_rate_limiter.request(chat_id, send, label=label)
    except Exception:
        logger.warning("Telegram task UI unavailable (%s)", label, exc_info=True)
        return None

def _normalize_filename(name: str) -> str:
    """Normalize uploaded filename to a safe basename with bounded length."""
    name = os.path.basename(urllib.parse.unquote(name))
    name = re.sub(r'[^\w\.-]', '_', name)
    name = re.sub(r'_+', '_', name).strip('._')
    if not name:
        return "uploaded_file"
    stem, ext = os.path.splitext(name)
    return f"{stem[:80]}{ext[:16]}"


async def _flush_text_group(key: tuple[int, int | None, int]) -> None:
    """Wait for consecutive Telegram text chunks, then enqueue one task."""
    try:
        await asyncio.sleep(_TEXT_DEBOUNCE_SECONDS)
        group = _text_groups.pop(key, None)
        if group is None:
            return
        text = _TEXT_JOIN_SEPARATOR.join(part for part in group.parts if part)
        if text.strip():
            await _process(group.message, text, group.bot)
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("Failed to flush Telegram text group for chat/thread/user %s", key)


def _buffer_text_message(message: Message, bot: Bot, text: str) -> None:
    """Debounce adjacent text messages from one user into a single model task.

    Telegram clients split pasted text that exceeds the message limit. Without
    this quiet-period buffer each fragment becomes an independent queued task,
    so the agent sees incomplete instructions. Commands are excluded by the
    router and every user/thread has an independent buffer.
    """
    assert message.from_user
    key = (message.chat.id, message.message_thread_id, message.from_user.id)
    group = _text_groups.get(key)
    if group is None:
        group = _PendingText(message=message, bot=bot)
        _text_groups[key] = group
    elif group.timer is not None:
        group.timer.cancel()
    group.parts.append(text)
    group.timer = asyncio.create_task(_flush_text_group(key))


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
        await _safe_message_edit(
            msg,
            _VOICE_FRAMES[idx],
            label="voice transcription animation",
        )
        try:
            # The chat-wide limiter already protects Telegram, and this avoids
            # creating needless edit attempts between its permitted updates.
            await asyncio.wait_for(stop_event.wait(), timeout=1.6)
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
        msg = await _safe_message_reply(
            message,
            f"⏸ Задача поставлена в очередь (позиция {pos}). "
            f"Сначала примените или отбросьте изменения задачи #{blocking.task_id}.",
            label="blocked task queue notice",
            disable_notification=True,
        )
        if msg is not None:
            t_reg.append(msg.message_id)
    elif not _start_queue_processing(thread_id, bot, chat_id):
        pos = await get_queued_count(thread_id)
        msg = await _safe_message_reply(
            message,
            f"⏳ Задача поставлена в очередь (позиция {pos})",
            label="task queue notice",
            disable_notification=True,
        )
        if msg is not None:
            t_reg.append(msg.message_id)


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
            conversation_id = str(
                _uuid.uuid5(
                    _NAMESPACE_TG,
                    (
                        f"thread-{thread_id}"
                        if execution_profile == "chat"
                        else f"thread-{thread_id}-task-{task_id}"
                    ),
                )
            )

            status_msg = await _safe_bot_send_message(
                bot,
                chat_id,
                f"<b>{'⚡ Chat' if execution_profile == 'chat' else '🛠 Code task'}</b>\n└─ [⠋] Обработка...",
                label=f"task #{task_id} status card",
                parse_mode="HTML",
                message_thread_id=thread_id,
            )
            if status_msg is None:
                # The durable queue and the worker do not depend on an
                # optional status card.  A temporary Telegram outage used to
                # discard a claimed task at this point.
                logger.warning(
                    "Running task #%s without Telegram status card; UI will recover on later requests",
                    task_id,
                )

            stop_typing = asyncio.Event()
            typing_task = asyncio.create_task(
                _typing_loop(bot, chat_id, stop_typing, thread_id)
            )
            
            t_reg = thread_messages_registry.setdefault(thread_id, [])
            if status_msg is not None:
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
            full_response = ""
            terminal_error: str | None = None

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
            except asyncio.CancelledError:
                full_response = ""
                target_status = TaskStatus.CANCELLED
                terminal_error = "Cancelled by user"
            except Exception as exc:
                full_response = ""
                target_status = TaskStatus.FAILED
                terminal_error = str(exc)
                logger.exception("Task #%s failed before its final response", task_id)
            finally:
                stop_typing.set()
                typing_task.cancel()
                _active_tasks.pop(thread_id, None)

            # Verify deliverables before announcing a successful task.  The
            # model's prose and a tool lifecycle event are not proof that an
            # image/file was created.  This only inspects the exact, private
            # directory allocated to this task; it never scans a shared CLI
            # scratch folder.
            artifact_cleanup_safe = True
            task_artifacts = None
            await tracker.on_tool_start(
                "artifact_inspection",
                "Проверяю итоговые файлы",
            )
            try:
                task_artifacts = await collect_task_artifacts(task_id)
            except ArtifactError as exc:
                artifact_cleanup_safe = False
                await tracker.on_tool_end("artifact_inspection", "ERROR")
                logger.exception("Failed to collect task artifacts for task #%s", task_id)
                if target_status == TaskStatus.DONE and is_explicit_artifact_request(prompt):
                    target_status = TaskStatus.FAILED
                    terminal_error = str(exc)
                    full_response = (
                        "❌ Не удалось безопасно проверить итоговый файл, поэтому "
                        "результат не считается созданным."
                    )
                    await tracker.replace_text(full_response)
            else:
                await tracker.on_tool_end("artifact_inspection", "DONE")

            if target_status == TaskStatus.DONE:
                artifact_failure = validate_requested_artifacts(prompt, task_artifacts)
                if artifact_failure:
                    target_status = TaskStatus.FAILED
                    terminal_error = "Requested Telegram deliverable was not produced"
                    full_response = artifact_failure
                    await tracker.replace_text(full_response)

            # The output directory is task-scoped and otherwise empty by
            # contract. Deliver every verified file it contains rather than
            # trying to infer a user's intent from Russian word morphology.
            # The previous regex gate silently dropped real index.html and
            # image files for valid requests such as "сделать" or "скинуть".
            if (
                task_artifacts is not None
                and task_artifacts.files
                and target_status == TaskStatus.DONE
            ):
                from bot.services.tracker import rollback_registry

                await tracker.on_tool_start(
                    "artifact_delivery",
                    f"Отправляю в Telegram: {len(task_artifacts.files)} файл(а)",
                )
                rlist = (
                    rollback_registry.setdefault(status_msg.message_id, [])
                    if status_msg is not None
                    else None
                )
                report = await deliver_task_artifacts(
                    bot,
                    chat_id,
                    task_artifacts,
                    thread_id=thread_id,
                    rollback_list=rlist,
                )
                if report.failed or task_artifacts.skipped:
                    artifact_cleanup_safe = False
                    target_status = TaskStatus.FAILED
                    terminal_error = "Telegram artifact delivery failed"
                    full_response = (
                        full_response.rstrip()
                        + "\n\n⚠️ Итоговый файл создан, но Telegram не подтвердил "
                        "его отправку. Он сохранён на сервере; задача отмечена "
                        "ошибкой, чтобы её можно было безопасно повторить."
                    )
                    await tracker.replace_text(full_response)
                    await tracker.on_tool_end("artifact_delivery", "ERROR")
                    logger.warning(
                        "Task artifact delivery incomplete for task #%s: delivered=%d failed=%d skipped=%d",
                        task_id,
                        report.delivered,
                        report.failed,
                        len(task_artifacts.skipped),
                    )
                else:
                    await tracker.on_tool_end("artifact_delivery", "DONE")

            task_status = (
                await finish_task(
                    task_id,
                    target_status,
                    error=terminal_error,
                    result_summary=full_response[:1000],
                )
            ).status

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
    _buffer_text_message(message, bot, message.text)


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

    status_msg = await _safe_message_answer(
        message,
        _VOICE_FRAMES[0],
        label="voice transcription status",
        retries=0,
    )
    stop_anim = asyncio.Event()
    anim_task = (
        asyncio.create_task(_animate_voice(status_msg, stop_anim))
        if status_msg is not None
        else None
    )

    try:
        file = await bot.get_file(message.voice.file_id)
        assert file.file_path
        tmp = tempfile.mktemp(suffix=".ogg", dir="/tmp")
        await bot.download_file(file.file_path, tmp)

        text = await transcribe_voice(tmp)
        stop_anim.set()
        if anim_task is not None:
            anim_task.cancel()

        if not text or not text.strip():
            if status_msg is not None:
                await _safe_message_edit(
                    status_msg,
                    "Не удалось распознать речь.",
                    label="empty voice transcription",
                )
            else:
                await _safe_message_answer(
                    message,
                    "Не удалось распознать речь.",
                    label="empty voice transcription fallback",
                )
            return

        if status_msg is not None:
            await _safe_message_delete(status_msg, label="voice transcription status cleanup")
        preview = text[:500] + "..." if len(text) > 500 else text
        # This preview is optional.  It must never block the durable enqueue
        # below when Telegram is temporarily rate-limiting the group.
        await _safe_message_answer(
            message,
            f"<b>Распознано:</b>\n\n<blockquote>{preview}</blockquote>",
            label="voice transcription preview",
            retries=0,
            parse_mode="HTML",
        )

        await _process(message, text, bot)
    except Exception:
        logger.exception("Voice message processing failed")
        stop_anim.set()
        if anim_task is not None:
            anim_task.cancel()
        error_text = "Ошибка голосового сообщения. Попробуйте отправить его ещё раз."
        if status_msg is not None:
            edited = await _safe_message_edit(
                status_msg,
                error_text,
                label="voice transcription error",
            )
            if edited:
                return
        await _safe_message_answer(
            message,
            error_text,
            label="voice transcription error fallback",
        )
    finally:
        stop_typing.set()
        typing_task.cancel()
