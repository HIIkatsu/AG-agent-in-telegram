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
from typing import Any
from collections import defaultdict

from aiogram import Bot, F, Router
from aiogram.types import Message

from bot.config import settings
from bot.db import db
from bot.services.agy_runner import run_agy
from bot.services.artifacts import (
    deliver_and_cleanup_artifacts,
    diff_snapshots,
    snapshot_workspaces,
)

from bot.services.git_manager import git_manager
from bot.services.tracker import TaskTracker
from bot.services.voice import transcribe_voice

router = Router(name="message")

# Active task tracking per thread_id: thread_id -> (tracker, agy_task)
_active: dict[int, tuple[TaskTracker, asyncio.Task | None]] = {}

# Message queue per thread_id
_queue: dict[int, list[tuple[Message, str, list[str] | None]]] = defaultdict(list)

_VOICE_FRAMES = ["🎙 ⠋ Распознаю...", "🎙 ⠙ Распознаю...", "🎙 ⠹ Распознаю...", "🎙 ⠸ Распознаю..."]

def _normalize_filename(name: str) -> str:
    """Normalize uploaded filename to be safe."""
    name = urllib.parse.unquote(name)
    name = re.sub(r'[^\w\.-]', '_', name)
    return name.strip('_') or "uploaded_file"


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

    # Prevent concurrent duplicate executions for the same thread
    if thread_id in _active:
        _, active_task = _active[thread_id]
        if active_task and not active_task.done():
            _queue[thread_id].append((message, text, files))
            pos = len(_queue[thread_id])
            try:
                await message.reply(f"⏳ Задача поставлена в очередь (позиция {pos})", disable_notification=True)
            except Exception:
                pass
            return

    session = await db.get_or_create_session(thread_id)

    # Deterministic valid UUIDv5 based on thread_id so agy CLI accepts it
    import uuid as _uuid
    _NAMESPACE_TG = _uuid.UUID('6ba7b810-9ed0-11d1-80b4-00c04fd430c8')
    conversation_id = str(_uuid.uuid5(_NAMESPACE_TG, f"thread-{thread_id}"))

    web_search: bool = bool(session.get("web_search", 0))
    model: str = session.get("model", "") or ""
    ws = session["workdir"]
    os.makedirs(ws, exist_ok=True)

    await db.update_last_used(thread_id)

    # If files were attached, add them to prompt
    prompt = text
    if files:
        file_list = "\n".join(f"- {f}" for f in files)
        prompt = f"{text}\n\n[Прикрепленные файлы в рабочей директории:\n{file_list}]"

    # Register messages for deep rollback tracking
    from bot.services.tracker import thread_messages_registry
    t_reg = thread_messages_registry.setdefault(thread_id, [])
    t_reg.append(message.message_id)

    # Ensure git checkpoint is created before task execution
    commit_hash = git_manager.create_checkpoint(ws, label=text[:25])

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(bot, chat_id, stop_typing, thread_id))

    status_msg = await message.answer("<b>Агент работает...</b>\n└─ [⠋] Обработка...", parse_mode="HTML")
    t_reg.append(status_msg.message_id)
    tracker = TaskTracker(bot, thread_id, status_msg, ws_dir=ws, commit_hash=commit_hash)
    await tracker.start()

    # Register active execution for cancel & duplicate prevention
    _active[thread_id] = (tracker, None)

    # Take snapshot of both workspace and agy scratchpad
    snap_before = snapshot_workspaces([ws])
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
            thread_id=thread_id,
        ))
        _active[thread_id] = (tracker, agy_task)
        full_response = await agy_task
    except asyncio.CancelledError:
        full_response = ""
    finally:
        stop_typing.set()
        typing_task.cancel()
        _active.pop(thread_id, None)

    # Artifact post-tool processing across workspace + scratchpad
    snap_after = snapshot_workspaces([ws])
    new_files = diff_snapshots(snap_before, snap_after)

    # Sync any newly created files from scratchpad into workspace so Git VCS tracks them
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

    # Finish tracker now so git_manager.has_changes(ws) checks workspace with all files present!
    await tracker.finish()

    if synced_files:
        from bot.services.tracker import rollback_registry
        rlist = rollback_registry.setdefault(status_msg.message_id, [])
        await deliver_and_cleanup_artifacts(bot, chat_id, synced_files, thread_id=thread_id, rollback_list=rlist)

    # Process next in queue
    if _queue[thread_id]:
        next_msg, next_text, next_files = _queue[thread_id].pop(0)
        # Give a tiny breath between tasks
        await asyncio.sleep(0.5)
        asyncio.create_task(_process(next_msg, next_text, bot, next_files))


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
    await _process(message, prompt, bot, files=[local_path])


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
    await _process(message, prompt, bot, files=[local_path])


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
