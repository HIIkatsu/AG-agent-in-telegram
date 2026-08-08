"""Dynamic UI Task Tracker & Response Renderer with Git VCS Diff & Rollback Buttons."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.services.git_manager import git_manager
from bot.utils.formatting import chunk_text
from bot.utils.sanitizer import clean_telegram_markdown

logger = logging.getLogger(__name__)

# Реестр отправленных сообщений для каждого статусного сообщения,
# чтобы при откате (rollback) можно было их удалить.
rollback_registry: dict[int, list[int]] = {}

_SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


@dataclass
class StepInfo:
    tool_name: str
    label: str
    status: str = "IN_PROGRESS"  # IN_PROGRESS, DONE, ERROR, DENIED


def build_tracker_kb(thread_id: int, ws_dir: str = "", final: bool = False) -> InlineKeyboardMarkup | None:
    """Build tracker keyboard with cancel button during generation and diff/accept/rollback buttons on completion."""
    has_changes = bool(ws_dir and git_manager.has_changes(ws_dir))

    if final:
        if has_changes:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="👀 Посмотреть Diff", callback_data=f"view_diff:{thread_id}"),
                        InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_diff:{thread_id}"),
                    ],
                    [InlineKeyboardButton(text="⏪ Откатить", callback_data=f"rollback:{thread_id}")],
                ]
            )
        return None

    buttons = [[InlineKeyboardButton(text="✕ Отмена", callback_data="cancel_gen")]]
    if has_changes:
        buttons.append([InlineKeyboardButton(text="👀 Посмотреть Diff", callback_data=f"view_diff:{thread_id}")])
        buttons.append([InlineKeyboardButton(text="⏪ Откатить", callback_data=f"rollback:{thread_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


class TaskTracker:
    """Manages static header, active step spinner tree, and smooth text streaming without flickering."""

    def __init__(self, bot: Bot, thread_id: int, status_message: Message, ws_dir: str = "", debounce: float = 0.4):
        self.bot = bot
        self.thread_id = thread_id
        self.status_msg = status_message
        self.ws_dir = ws_dir
        self.debounce = debounce

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

    async def on_tool_start(self, tool_name: str, label: str) -> None:
        """Lifecycle hook when a tool starts executing."""
        async with self._lock:
            if self._current_step_idx is not None and self._current_step_idx < len(self.steps):
                if self.steps[self._current_step_idx].status == "IN_PROGRESS":
                    self.steps[self._current_step_idx].status = "DONE"

            step = StepInfo(tool_name=tool_name, label=label, status="IN_PROGRESS")
            self.steps.append(step)
            self._current_step_idx = len(self.steps) - 1

        await self.render(force=True)

    async def on_tool_end(self, tool_name: str, status: str = "DONE") -> None:
        """Lifecycle hook when a tool finishes executing."""
        async with self._lock:
            if self._current_step_idx is not None and self._current_step_idx < len(self.steps):
                self.steps[self._current_step_idx].status = status

        await self.render(force=True)

    async def cancel(self) -> None:
        self._cancelled = True
        self._finished = True
        self._finish_status = "CANCELLED"
        if self._render_task and not self._render_task.done():
            self._render_task.cancel()
        try:
            await self.status_msg.edit_text("Генерация отменена.")
        except Exception:
            pass

    async def finish(self, status: str = "DONE") -> None:
        self._finished = True
        self._finish_status = status
        if self._render_task and not self._render_task.done():
            self._render_task.cancel()
            try:
                await self._render_task
            except asyncio.CancelledError:
                pass
        await self.render(force=True, final=True)

    async def _render_loop(self) -> None:
        while not self._finished:
            await asyncio.sleep(self.debounce)
            self._spinner_idx = (self._spinner_idx + 1) % len(_SPINNER)
            await self.render()

    async def render(self, force: bool = False, final: bool = False) -> None:
        async with self._lock:
            buffer_copy = self._buffer
            steps_copy = list(self.steps)

        spinner = _SPINNER[self._spinner_idx % len(_SPINNER)]

        if final:
            buffer_copy = await self._extract_large_code_blocks(buffer_copy)

        if final:
            if self._finish_status == "DONE":
                header = "<b>✅ Задача завершена</b>\n"
            elif self._finish_status == "ERROR":
                header = "<b>❌ Ошибка выполнения</b>\n"
            elif self._finish_status == "CANCELLED":
                header = "<b>⏹ Задача отменена</b>\n"
            elif self._finish_status == "TIMEOUT":
                header = "<b>⏱ Время ожидания вышло</b>\n"
            else:
                header = f"<b>ℹ️ {self._finish_status}</b>\n"
        else:
            header = "<b>Агент работает...</b>\n"

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
                step_lines.append(f"└─ [⏳] Оформление ответа...")
            elif steps_copy:
                step_lines[-1] = step_lines[-1].replace("├─", "└─")

        clean_text = clean_telegram_markdown(buffer_copy.strip())
        kb = build_tracker_kb(self.thread_id, ws_dir=self.ws_dir, final=final)

        if final:
            sent_msg_ids = []
            
            if not steps_copy and clean_text:
                # No tools used (simple answer). Transform the status message directly into the final answer.
                chunks = chunk_text(clean_text, max_len=4000)
                try:
                    await self._safe_edit(self.status_msg, chunks[0], reply_markup=kb)
                    # If there are more chunks, send them as replies
                    if len(chunks) > 1:
                        reply_id = self.status_msg.reply_to_message.message_id if self.status_msg.reply_to_message else None
                        for i, chunk in enumerate(chunks[1:]):
                            part_text = f"<i>(Часть {i+2}/{len(chunks)})</i>\n\n{chunk}"
                            msg = await self.bot.send_message(
                                chat_id=self.status_msg.chat.id,
                                text=part_text,
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                                message_thread_id=self.thread_id if self.thread_id else None,
                                reply_to_message_id=reply_id
                            )
                            sent_msg_ids.append(msg.message_id)
                except Exception as e:
                    logger.debug("Render final simple reply error: %s", e)
                
                rollback_registry[self.status_msg.message_id] = sent_msg_ids
                return

            # Complex task (tools used)
            loading_text = header + "\n".join(step_lines)
            if not clean_text:
                loading_text += "\n└─ [ℹ️] Пустой ответ"
            
            await self._safe_edit(self.status_msg, loading_text, reply_markup=kb)
            self._last_rendered_text = loading_text
            
            if clean_text:
                try:
                    chunks = chunk_text(clean_text, max_len=4000)
                    if len(clean_text) > 12000:
                        import tempfile
                        import os
                        from aiogram.types import FSInputFile
                        
                        reply_id = self.status_msg.reply_to_message.message_id if self.status_msg.reply_to_message else None
                        msg1 = await self.bot.send_message(
                            chat_id=self.status_msg.chat.id,
                            text=chunks[0] + "\n\n<i>[Ответ слишком длинный, см. файл ниже]</i>",
                            parse_mode="HTML",
                            message_thread_id=self.thread_id if self.thread_id else None,
                            reply_to_message_id=reply_id
                        )
                        sent_msg_ids.append(msg1.message_id)
                        
                        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".md", prefix="response_")
                        try:
                            with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
                                f.write(buffer_copy.strip())
                            msg2 = await self.bot.send_document(
                                self.status_msg.chat.id, 
                                FSInputFile(tmp_path, filename="full_response.md"),
                                message_thread_id=self.thread_id if self.thread_id else None
                            )
                            sent_msg_ids.append(msg2.message_id)
                        finally:
                            os.remove(tmp_path)
                    else:
                        reply_id = self.status_msg.reply_to_message.message_id if self.status_msg.reply_to_message else None
                        for i, chunk in enumerate(chunks):
                            text_to_send = chunk if i == 0 else f"<i>(Часть {i+1}/{len(chunks)})</i>\n\n{chunk}"
                            msg = await self.bot.send_message(
                                chat_id=self.status_msg.chat.id,
                                text=text_to_send,
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                                message_thread_id=self.thread_id if self.thread_id else None,
                                reply_to_message_id=reply_id
                            )
                            sent_msg_ids.append(msg.message_id)
                except Exception as e:
                    logger.debug("Render final reply error: %s", e)
            
            rollback_registry[self.status_msg.message_id] = sent_msg_ids
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
            await self._safe_edit(self.status_msg, chunks[0], reply_markup=kb)
            self._last_rendered_text = rendered
        except Exception as e:
            logger.debug("Render edit error: %s", e)

    async def _safe_edit(self, msg: Message, html_text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        try:
            await msg.edit_text(html_text, parse_mode="HTML", reply_markup=reply_markup, disable_web_page_preview=True)
        except Exception as exc:
            err = str(exc)
            if "message is not modified" in err:
                return
            if "Too Many Requests" in err or "retry after" in err.lower():
                m = re.search(r"retry after (\d+)", err, re.IGNORECASE)
                await asyncio.sleep(int(m.group(1)) if m else 3)
                try:
                    await msg.edit_text(html_text, parse_mode="HTML", reply_markup=reply_markup, disable_web_page_preview=True)
                except Exception:
                    pass
            elif "can't parse entities" in err.lower():
                try:
                    await msg.edit_text(re.sub(r"<[^>]+>", "", html_text), reply_markup=reply_markup)
                except Exception:
                    pass

    async def _extract_large_code_blocks(self, text: str) -> str:
        """Extract code blocks > 1500 chars or > 50 lines to files and send to Telegram."""
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
        
        for m in matches:
            lang = m.group(1).strip().lower()
            code = m.group(2)
            lines_count = len(code.split("\n"))
            
            if len(code) > 1500 or lines_count > 50:
                snippets_count += 1
                ext = ext_map.get(lang, ".txt") if lang else ".txt"
                filename = f"snippet_{snippets_count}{ext}"
                file_path = os.path.join(self.ws_dir, filename)
                
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(code)
                
                try:
                    await self.bot.send_document(
                        self.status_msg.chat.id,
                        FSInputFile(file_path, filename=filename),
                        message_thread_id=self.thread_id if self.thread_id else None
                    )
                except Exception as e:
                    logger.error("Failed to send snippet %s: %s", filename, e)
                
                placeholder = f"📄 [Сгенерирован файл: {filename} | {lines_count} строк]"
                
                result.append(text[offset:m.start()])
                result.append(placeholder)
                offset = m.end()
            else:
                result.append(text[offset:m.end()])
                offset = m.end()
                
        result.append(text[offset:])
        return "".join(result)
