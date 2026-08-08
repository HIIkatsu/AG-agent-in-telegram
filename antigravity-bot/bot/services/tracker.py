"""Dynamic UI Task Tracker & Response Renderer with Git VCS Diff & Rollback Buttons."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.services.git_manager import git_manager
from bot.utils.formatting import chunk_text
from bot.utils.sanitizer import clean_telegram_markdown

logger = logging.getLogger(__name__)

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
        if self._render_task and not self._render_task.done():
            self._render_task.cancel()
        try:
            await self.status_msg.edit_text("Генерация отменена.")
        except Exception:
            pass

    async def finish(self) -> None:
        self._finished = True
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

        full_text_blocks = []
        if not final:
            full_text_blocks.append(header + "\n".join(step_lines))

        clean_text = clean_telegram_markdown(buffer_copy.strip())
        if clean_text:
            if not final and steps_copy:
                full_text_blocks.append("────────────────────")
            full_text_blocks.append(clean_text)

        rendered = "\n".join(full_text_blocks).strip()
        if not rendered:
            return

        if rendered == self._last_rendered_text and not force:
            return

        kb = build_tracker_kb(self.thread_id, ws_dir=self.ws_dir, final=final)
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
