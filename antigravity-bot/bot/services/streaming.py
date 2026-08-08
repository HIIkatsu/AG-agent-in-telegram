"""Debounced streaming with animated spinner, TaskTracker, and cancel button."""

from __future__ import annotations

import asyncio
import logging
import re

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.utils.formatting import chunk_text
from bot.utils.sanitizer import clean_telegram_markdown

logger = logging.getLogger(__name__)

_SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_BARS = [
    "▰▱▱▱▱▱▱▱▱▱", "▰▰▱▱▱▱▱▱▱▱", "▰▰▰▱▱▱▱▱▱▱",
    "▰▰▰▰▱▱▱▱▱▱", "▰▰▰▰▰▱▱▱▱▱", "▰▰▰▰▰▰▱▱▱▱",
    "▰▰▰▰▰▰▰▱▱▱", "▰▰▰▰▰▰▰▰▱▱", "▰▰▰▰▰▰▰▰▰▱",
    "▰▰▰▰▰▰▰▰▰▰",
]
_STATUSES = [
    "Обрабатываю запрос", "Анализирую контекст", "Подбираю решение",
    "Генерирую ответ", "Формулирую мысль", "Собираю данные",
    "Обдумываю подход", "Пишу ответ", "Почти готово", "Финализирую",
]


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✕ Отмена", callback_data="cancel_gen")]]
    )


class StreamingUpdater:
    """Animated status -> streamed content with cancel support."""

    def __init__(self, bot: Bot, chat_id: int, status_message: Message, debounce: float = 1.3):
        self.bot = bot
        self.chat_id = chat_id
        self.debounce = debounce
        self._status_msg = status_message
        self._response_msgs: list[Message] = []
        self._buffer = ""
        self._last_sent = ""
        self._lock = asyncio.Lock()
        self._has_content = False
        self._finished = False
        self._cancelled = False
        self._status_task: asyncio.Task | None = None
        self._flush_task: asyncio.Task | None = None
        self._current_tool = ""

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    async def start(self) -> None:
        self._status_task = asyncio.create_task(self._status_loop())
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def feed(self, text: str) -> None:
        if self._cancelled:
            return
        async with self._lock:
            self._buffer += text
            if not self._has_content:
                self._has_content = True

    async def cancel(self) -> None:
        self._cancelled = True
        self._finished = True

    async def finish(self) -> None:
        self._finished = True
        for task in (self._status_task, self._flush_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._cancelled:
            try:
                await self._status_msg.edit_text("Генерация отменена.")
            except Exception:
                pass
            return

        await self._flush(final=True)
        if self._response_msgs:
            try:
                await self._status_msg.delete()
            except Exception:
                pass

    # -- Phase 1: animated spinner --
    async def _status_loop(self) -> None:
        idx = 0
        while not self._has_content and not self._finished:
            await asyncio.sleep(1.2)
            if self._has_content or self._finished:
                break
            idx = (idx + 1) % len(_SPINNER)
            label = self._current_tool or _STATUSES[idx % len(_STATUSES)]
            text = f"{_SPINNER[idx]} {label}...\n{_BARS[idx % len(_BARS)]}"
            try:
                await self._status_msg.edit_text(text, reply_markup=_cancel_kb())
            except Exception:
                pass

    # -- Phase 2: content streaming --
    async def _flush_loop(self) -> None:
        while not self._finished:
            await asyncio.sleep(self.debounce)
            if self._has_content and not self._cancelled:
                await self._flush()

    async def _flush(self, final: bool = False) -> None:
        async with self._lock:
            current = self._buffer
        if not current.strip():
            return
        if current == self._last_sent and not final:
            return

        # Sanitize Markdown & LaTeX before sending to Telegram
        display = clean_telegram_markdown(current.strip())
        if not display.strip():
            return

        chunks = chunk_text(display, max_len=4000)
        try:
            for i, chunk in enumerate(chunks):
                if not chunk.strip():
                    continue
                if i < len(self._response_msgs):
                    await self._safe_edit(self._response_msgs[i], chunk)
                else:
                    msg = await self.bot.send_message(
                        self.chat_id, chunk, parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    self._response_msgs.append(msg)
            self._last_sent = current
        except Exception:
            logger.exception("Flush error")

    async def _safe_edit(self, msg: Message, html_text: str) -> None:
        try:
            await msg.edit_text(html_text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as exc:
            err = str(exc)
            if "message is not modified" in err:
                return
            if "Too Many Requests" in err or "retry after" in err.lower():
                m = re.search(r"retry after (\d+)", err, re.IGNORECASE)
                await asyncio.sleep(int(m.group(1)) if m else 3)
                try:
                    await msg.edit_text(html_text, parse_mode="HTML", disable_web_page_preview=True)
                except Exception:
                    pass
            elif "can't parse entities" in err.lower():
                try:
                    await msg.edit_text(re.sub(r"<[^>]+>", "", html_text))
                except Exception:
                    pass
            else:
                logger.error("edit: %s", exc)
