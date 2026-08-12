"""Dynamic UI Task Tracker & Response Renderer with Git VCS Diff & Rollback Buttons."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime

from aiogram import Bot
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.utils.telegram_renderer import chunk_text, part_label, render_markdown, render_markdown_chunks, strip_telegram_html

logger = logging.getLogger(__name__)

# Реестр всех отправленных и полученных сообщений в каждом треде (thread_id -> list[message_id])
# Используется для безопасного удаления сообщений при ГЛУБОКОМ откате.
thread_messages_registry: dict[int, list[int]] = {}

# Реестр отправленных сообщений для каждого статусного сообщения (оставляем для совместимости)
rollback_registry: dict[int, list[int]] = {}

_SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


@dataclass
class StepInfo:
    tool_name: str
    label: str
    status: str = "IN_PROGRESS"  # IN_PROGRESS, DONE, ERROR, DENIED


def build_tracker_kb(thread_id: int, ws_dir: str = "", status: str = "running", commit_hash: str | None = None, task_id: int | None = None, has_changes: bool = False) -> InlineKeyboardMarkup | None:
    """Build tracker keyboard based on task status without doing git I/O in render."""
    buttons = []

    if status == "running":
        buttons.append([
            InlineKeyboardButton(text="⏹ Стоп", callback_data=f"t:st:{task_id}" if task_id else "cancel_gen"),
            InlineKeyboardButton(text="📌 Статус", callback_data=f"t:ss:{task_id}" if task_id else "task_status:0")
        ])
    elif status in ("failed", "interrupted", "error", "timeout", "cancelled"):
        buttons.append([
            InlineKeyboardButton(text="🔁 Retry", callback_data=f"t:rt:{task_id}" if task_id else "retry_task:0"),
            InlineKeyboardButton(text="📄 Logs", callback_data=f"t:lg:{task_id}" if task_id else "view_logs:0")
        ])
    if status != "running" and has_changes and task_id:
        buttons.append([
            InlineKeyboardButton(text="👀 Diff", callback_data=f"t:df:{task_id}"),
            InlineKeyboardButton(text="✅ Применить", callback_data=f"t:ac:{task_id}"),
        ])
        buttons.append([
            InlineKeyboardButton(text="🗑 Отбросить", callback_data=f"t:rb:{task_id}")
        ])

    return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None


class TaskTracker:
    """Manages static header, active step spinner tree, and smooth text streaming without flickering."""

    def __init__(
        self, bot: Bot, thread_id: int, status_message: Message | None, ws_dir: str = "",
        debounce: float = 0.4, commit_hash: str | None = None, task_id: int | None = None,
        model: str = "", mode: str = "code", started_at: datetime | None = None
    ):
        self.bot = bot
        self.thread_id = thread_id
        self.status_msg = status_message
        self.ws_dir = ws_dir
        self.debounce = max(debounce, 0.8)
        self.commit_hash = commit_hash
        self.task_id = task_id
        self.model = model
        self.mode = mode
        self.started_at = started_at or datetime.now()

        self.steps: list[StepInfo] = []
        self._current_step_idx: int | None = None
        self._buffer = ""
        self._last_rendered_text = ""
        self._lock = asyncio.Lock()

        self._finished = False
        self._cancelled = False
        self._spinner_idx = 0
        self._finish_status = "DONE"

        self._render_task: asyncio.Task | None = None
        self.has_changes_after_finish = False
        self._pending_log_events: list[tuple[str, str, str | None]] = []
        self._background_tasks: set[asyncio.Task] = set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    async def start(self) -> None:
        self._render_task = asyncio.create_task(self._render_loop())

    async def feed_text(self, text: str) -> None:
        if self._cancelled:
            return
        async with self._lock:
            self._buffer += text

    async def replace_text(self, text: str) -> None:
        """Replace streamed prose when an independently verified result disagrees.

        Tool output is not evidence that a requested image or file exists.  The
        queue uses this at the terminal boundary so a model cannot claim that a
        failed generation was delivered successfully.
        """
        async with self._lock:
            self._buffer = text

    async def on_tool_start(self, tool_name: str, label: str) -> None:
        """Lifecycle hook when a tool starts executing."""
        async with self._lock:
            if self._current_step_idx is not None and self._current_step_idx < len(self.steps):
                if self.steps[self._current_step_idx].status == "IN_PROGRESS":
                    self.steps[self._current_step_idx].status = "DONE"

            step = StepInfo(tool_name=tool_name, label=label, status="IN_PROGRESS")
            self.steps.append(step)
            self._current_step_idx = len(self.steps) - 1

        if self.task_id:
            self._pending_log_events.append(("tool_start", label, tool_name))

    async def on_tool_end(self, tool_name: str, status: str = "DONE") -> None:
        """Lifecycle hook when a tool finishes executing."""
        async with self._lock:
            if self._current_step_idx is not None and self._current_step_idx < len(self.steps):
                self.steps[self._current_step_idx].status = status

        if self.task_id:
            self._pending_log_events.append((f"tool_{status.lower()}", tool_name, None))

    async def cancel(self) -> None:
        self._cancelled = True
        self._finished = True
        self._finish_status = "CANCELLED"
        if self._render_task and not self._render_task.done():
            self._render_task.cancel()
        if self.task_id:
            self._pending_log_events.append(("cancelled", "Задача отменена пользователем", None))
            await self._flush_log_events()
        if self.status_msg is not None:
            await self._safe_edit(self.status_msg, "Генерация отменена.", final=True)

    async def finish(self, status: str = "DONE") -> None:
        self._finished = True
        self._finish_status = status
        if self._render_task and not self._render_task.done():
            self._render_task.cancel()
            try:
                await self._render_task
            except asyncio.CancelledError:
                pass
        self._schedule_log_flush()
        await self.render(force=True, final=True)

    async def _render_loop(self) -> None:
        while not self._finished:
            await asyncio.sleep(self.debounce)
            self._spinner_idx = (self._spinner_idx + 1) % len(_SPINNER)
            await self._flush_log_events()
            await self.render()

    async def render(self, force: bool = False, final: bool = False) -> None:
        async with self._lock:
            buffer_copy = self._buffer
            steps_copy = list(self.steps)

        # Telegram delivery is intentionally auxiliary to the durable task
        # queue.  If Telegram is temporarily unavailable at task start, the
        # task still runs and its DB log remains available for recovery.
        if self.status_msg is None:
            return

        spinner = _SPINNER[self._spinner_idx % len(_SPINNER)]

        if final:
            buffer_copy = await self._extract_large_code_blocks(buffer_copy)

        # Calculate elapsed time
        elapsed_sec = int((datetime.now() - self.started_at).total_seconds())
        m, s = divmod(elapsed_sec, 60)
        elapsed = f"{m:02d}:{s:02d}"
        
        project_name = os.path.basename(self.ws_dir)
        header = f"🧠 Задача #{self.task_id or '?'}\n"
        header += f"Проект: <b>{project_name}</b>\n"
        header += f"⏱ Время: {elapsed} | Модель: <i>{self.model or 'default'}</i> | Режим: <i>{self.mode}</i>\n\n"
        
        if final:
            if self._finish_status == "DONE":
                header += "<b>✅ Завершено</b>\n"
            elif self._finish_status == "ERROR":
                header += "<b>❌ Ошибка</b>\n"
            elif self._finish_status == "CANCELLED":
                header += "<b>⏹ Отменено</b>\n"
            elif self._finish_status == "TIMEOUT":
                header += "<b>⏱ Timeout</b>\n"
            else:
                header += f"<b>ℹ️ {self._finish_status}</b>\n"
        else:
            header += "<b>Статус:</b> running\n"

        step_lines = []

        if not steps_copy and not final:
            step_lines.append(f"└─ [{spinner}] Обработка...")
        else:
            for i, step in enumerate(steps_copy):
                prefix = "├─"
                if step.status == "DONE":
                    icon = "✅"
                elif step.status == "IN_PROGRESS":
                    icon = spinner
                elif step.status == "ERROR":
                    icon = "❌"
                elif step.status == "DENIED":
                    icon = "🚫"
                else:
                    icon = "⏳"

                step_lines.append(f"{prefix} [{icon}] {step.label}")

            if not final:
                # This heartbeat is intentionally animated. A model can spend
                # several seconds composing its response after the last tool
                # finished; a static line looks like a frozen Telegram task.
                step_lines.append(f"└─ [{spinner}] Формирую и отправляю итог...")
            elif steps_copy:
                step_lines[-1] = step_lines[-1].replace("├─", "└─")

        if final:
            rendered_response = render_markdown(buffer_copy.strip())
            clean_text = rendered_response.html
        else:
            preview = buffer_copy.strip()
            if len(preview) > 1000:
                preview = "…" + preview[-1000:]
            import html as _html
            clean_text = _html.escape(preview)
        status = self._finish_status.lower() if final else "running"
        kb = build_tracker_kb(self.thread_id, ws_dir=self.ws_dir, status=status, commit_hash=self.commit_hash, task_id=self.task_id, has_changes=self.has_changes_after_finish)

        if final:
            sent_msg_ids = []
            
            if not steps_copy and clean_text:
                # No tools used (simple answer). Transform the status message directly into the final answer.
                chunks = render_markdown_chunks(buffer_copy.strip(), max_len=3600)
                try:
                    await self._safe_edit(self.status_msg, chunks[0].html, reply_markup=kb, final=True)
                    # If there are more chunks, send them as replies
                    if len(chunks) > 1:
                        reply_id = self.status_msg.reply_to_message.message_id if self.status_msg.reply_to_message else None
                        for i, chunk in enumerate(chunks[1:]):
                            part_text = f"{part_label(i + 2, len(chunks))}{chunk.html}"
                            msg = await self._send_message(
                                part_text,
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                                message_thread_id=self.thread_id if self.thread_id else None,
                                reply_to_message_id=reply_id,
                            )
                            sent_msg_ids.append(msg.message_id)
                except Exception as e:
                    logger.debug("Render final simple reply error: %s", e)
                
                rollback_registry.setdefault(self.status_msg.message_id, []).extend(sent_msg_ids)
                if self.thread_id is not None:
                    thread_messages_registry.setdefault(self.thread_id, []).extend(sent_msg_ids)
                return

            # Complex task (tools used)
            loading_text = header + "\n".join(step_lines)
            if not clean_text:
                loading_text += "\n└─ [ℹ️] Пустой ответ"
            
            await self._safe_edit(self.status_msg, loading_text, reply_markup=kb, final=True)
            self._last_rendered_text = loading_text
            
            if clean_text:
                try:
                    chunks = render_markdown_chunks(buffer_copy.strip(), max_len=3600)
                    if len(clean_text) > 12000:
                        reply_id = self.status_msg.reply_to_message.message_id if self.status_msg.reply_to_message else None
                        msg1 = await self._send_message(
                            chunks[0].html + "\n\n<i>[Ответ слишком длинный, см. файл ниже]</i>",
                            parse_mode="HTML",
                            message_thread_id=self.thread_id if self.thread_id else None,
                            reply_to_message_id=reply_id,
                        )
                        sent_msg_ids.append(msg1.message_id)
                        self._schedule_background_task(
                            self._send_full_response_document(buffer_copy.strip()),
                            label="send_full_response_document",
                        )
                    else:
                        reply_id = self.status_msg.reply_to_message.message_id if self.status_msg.reply_to_message else None
                        for i, chunk in enumerate(chunks):
                            text_to_send = f"{part_label(i + 1, len(chunks))}{chunk.html}"
                            msg = await self._send_message(
                                text_to_send,
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                                message_thread_id=self.thread_id if self.thread_id else None,
                                reply_to_message_id=reply_id,
                            )
                            sent_msg_ids.append(msg.message_id)
                except Exception as e:
                    logger.debug("Render final reply error: %s", e)
            
            rollback_registry.setdefault(self.status_msg.message_id, []).extend(sent_msg_ids)
            if self.thread_id is not None:
                thread_messages_registry.setdefault(self.thread_id, []).extend(sent_msg_ids)
            return

        # Not final -> we combine them to avoid spam
        full_text_blocks = []
        full_text_blocks.append(header + "\n".join(step_lines))
        if clean_text:
            if steps_copy:
                full_text_blocks.append("────────────────────")
            full_text_blocks.append(clean_text)
            
        rendered = "\n".join(full_text_blocks).strip()
        if not rendered:
            return

        if rendered == self._last_rendered_text and not force:
            return

        try:
            chunks = chunk_text(rendered, max_len=4000)
            await self._safe_edit(self.status_msg, chunks[0], reply_markup=kb, final=final, force=force)
            self._last_rendered_text = rendered
        except Exception as e:
            logger.debug("Render edit error: %s", e)

    async def _flush_log_events(self) -> None:
        if not self.task_id or not self._pending_log_events:
            return
        events = self._pending_log_events
        self._pending_log_events = []
        from bot.services.task_service import log_task_events_bulk
        await log_task_events_bulk(self.task_id, events)

    def _schedule_background_task(self, coro, *, label: str) -> None:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)

        def _done_callback(done_task: asyncio.Task) -> None:
            self._background_tasks.discard(done_task)
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug("Background tracker task %s failed: %s", label, exc)

        task.add_done_callback(_done_callback)

    def _schedule_log_flush(self) -> None:
        if self.task_id and self._pending_log_events:
            self._schedule_background_task(self._flush_log_events(), label="log_flush")

    async def _send_message(self, text: str, **kwargs) -> Message:
        """Send a task response through the chat-wide Telegram limiter."""
        if self.status_msg is None:
            raise RuntimeError("Task tracker has no Telegram status message")
        from bot.services.telegram_rate_limiter import telegram_rate_limiter

        async def send() -> Message:
            return await self.bot.send_message(
                chat_id=self.status_msg.chat.id,
                text=text,
                **kwargs,
            )

        return await telegram_rate_limiter.request(
            self.status_msg.chat.id,
            send,
            label="task response",
        )

    async def _safe_edit(self, msg: Message, html_text: str, reply_markup: InlineKeyboardMarkup | None = None, final: bool = False, force: bool = False) -> None:
        from bot.services.telegram_rate_limiter import telegram_rate_limiter
        if not await telegram_rate_limiter.allow_edit(msg.chat.id, msg.message_id, final=final, force=force):
            return
        timeout = 12.0 if final or force else 4.0

        async def edit(text: str, *, markup: InlineKeyboardMarkup | None) -> object:
            return await asyncio.wait_for(
                msg.edit_text(
                    text,
                    parse_mode="HTML",
                    reply_markup=markup,
                    disable_web_page_preview=True,
                ),
                timeout=timeout,
            )

        try:
            await telegram_rate_limiter.request(
                msg.chat.id,
                lambda: edit(html_text, markup=reply_markup),
                label="task status edit",
            )
        except asyncio.TimeoutError:
            logger.debug("Telegram edit timed out after %.1fs (final=%s, force=%s)", timeout, final, force)
            return
        except Exception as exc:
            err = str(exc)
            if "message is not modified" in err:
                return
            if "can't parse entities" in err.lower():
                try:
                    await telegram_rate_limiter.request(
                        msg.chat.id,
                        lambda: edit(
                            strip_telegram_html(html_text),
                            markup=reply_markup,
                        ),
                        label="task status plain-text fallback",
                    )
                except Exception:
                    pass
            else:
                logger.debug("Task status edit failed: %s", exc)

    async def _extract_large_code_blocks(self, text: str) -> str:
        """Extract large code blocks to files and schedule Telegram delivery in background."""
        pattern = re.compile(r"```([a-zA-Z0-9_\-\+]*)\n(.*?)```", re.DOTALL)
        matches = list(pattern.finditer(text))
        
        if not matches:
            return text
            
        ext_map = {
            "python": ".py", "py": ".py", "javascript": ".js", "js": ".js",
            "typescript": ".ts", "ts": ".ts", "html": ".html", "css": ".css",
            "bash": ".sh", "sh": ".sh", "json": ".json", "yaml": ".yaml",
            "yml": ".yml", "sql": ".sql", "xml": ".xml", "markdown": ".md",
            "md": ".md", "cpp": ".cpp", "c": ".c", "java": ".java", "go": ".go",
            "rust": ".rs", "rs": ".rs", "php": ".php", "ruby": ".rb", "rb": ".rb"
        }
        
        offset = 0
        result = []
        snippets_count = 0
        snippets_to_send: list[tuple[str, str]] = []
        
        for m in matches:
            lang = m.group(1).strip().lower()
            code = m.group(2)
            lines_count = len(code.split("\n"))
            
            if len(code) > 1500 or lines_count > 50:
                snippets_count += 1
                ext = ext_map.get(lang, ".txt") if lang else ".txt"
                filename = f"snippet_{snippets_count}{ext}"
                temp_dir = tempfile.mkdtemp(prefix="agy-snippets-")
                file_path = os.path.join(temp_dir, filename)
                
                await asyncio.to_thread(self._write_text_file, file_path, code)
                snippets_to_send.append((file_path, filename))
                
                placeholder = f"📄 [Код отправлен файлом: {filename} | {lines_count} строк]"
                
                result.append(text[offset:m.start()])
                result.append(placeholder)
                offset = m.end()
            else:
                result.append(text[offset:m.end()])
                offset = m.end()
                
        result.append(text[offset:])
        if snippets_to_send:
            self._schedule_background_task(
                self._send_snippet_documents(snippets_to_send),
                label="send_snippet_documents",
            )
        return "".join(result)

    @staticmethod
    def _write_text_file(file_path: str, content: str) -> None:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

    async def _send_snippet_documents(self, snippets: list[tuple[str, str]]) -> None:
        sent_msg_ids = []
        if self.status_msg is None:
            for directory in {os.path.dirname(path) for path, _ in snippets}:
                await asyncio.to_thread(shutil.rmtree, directory, True)
            return
        from bot.services.telegram_rate_limiter import telegram_rate_limiter

        try:
            for file_path, filename in snippets:
                try:
                    async def send_document() -> Message:
                        return await self.bot.send_document(
                            self.status_msg.chat.id,
                            FSInputFile(file_path, filename=filename),
                            message_thread_id=self.thread_id if self.thread_id else None,
                        )

                    msg = await telegram_rate_limiter.request(
                        self.status_msg.chat.id,
                        send_document,
                        label=f"code snippet {filename}",
                    )
                    sent_msg_ids.append(msg.message_id)
                except Exception as e:
                    logger.error("Failed to send snippet %s: %s", filename, e)
        finally:
            for directory in {os.path.dirname(path) for path, _ in snippets}:
                await asyncio.to_thread(shutil.rmtree, directory, True)

        if sent_msg_ids:
            rollback_registry.setdefault(self.status_msg.message_id, []).extend(sent_msg_ids)
            if self.thread_id is not None:
                thread_messages_registry.setdefault(self.thread_id, []).extend(sent_msg_ids)

    async def _send_full_response_document(self, content: str) -> None:
        import tempfile

        if self.status_msg is None:
            return
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".md", prefix="response_")
        try:
            await asyncio.to_thread(self._write_fd_text_file, tmp_fd, content)
            from bot.services.telegram_rate_limiter import telegram_rate_limiter

            async def send_document() -> Message:
                return await self.bot.send_document(
                    self.status_msg.chat.id,
                    FSInputFile(tmp_path, filename="full_response.md"),
                    message_thread_id=self.thread_id if self.thread_id else None,
                )

            msg = await telegram_rate_limiter.request(
                self.status_msg.chat.id,
                send_document,
                label="full task response document",
            )
            rollback_registry.setdefault(self.status_msg.message_id, []).append(msg.message_id)
            if self.thread_id is not None:
                thread_messages_registry.setdefault(self.thread_id, []).append(msg.message_id)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    @staticmethod
    def _write_fd_text_file(fd: int, content: str) -> None:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
