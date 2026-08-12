"""Regression tests for voice requests during Telegram flood control."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "antigravity-bot"))
os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")

from bot.handlers import message as message_handler
from bot.services import telegram_rate_limiter


def _retry_after() -> TelegramRetryAfter:
    return TelegramRetryAfter(
        method=SendMessage(chat_id=-100, text="preview"),
        message="Too Many Requests: retry after 0",
        retry_after=0,
    )


def test_voice_task_is_enqueued_even_when_its_preview_is_rate_limited(monkeypatch) -> None:
    class StatusMessage:
        chat = SimpleNamespace(id=-100)
        edit_text = AsyncMock()
        delete = AsyncMock()

    status = StatusMessage()

    class IncomingMessage:
        voice = SimpleNamespace(file_id="voice-id")
        from_user = SimpleNamespace(id=1)
        message_thread_id = 77
        chat = SimpleNamespace(id=-100)

        def __init__(self) -> None:
            self.answer_calls = 0

        async def answer(self, *_args, **_kwargs):
            self.answer_calls += 1
            if self.answer_calls == 1:
                return status
            raise _retry_after()

    class Bot:
        async def get_file(self, _file_id):
            return SimpleNamespace(file_path="voice.ogg")

        async def download_file(self, _file_path, _destination):
            return None

    async def idle_typing(_bot, _chat_id, stop_event, _thread_id):
        await stop_event.wait()

    queued = AsyncMock()
    monkeypatch.setattr(
        telegram_rate_limiter,
        "telegram_rate_limiter",
        telegram_rate_limiter.TelegramRateLimiter(min_chat_interval=0),
    )
    monkeypatch.setattr(message_handler, "_typing_loop", idle_typing)
    monkeypatch.setattr(message_handler, "transcribe_voice", AsyncMock(return_value="сделай страницу"))
    monkeypatch.setattr(message_handler, "_process", queued)

    incoming = IncomingMessage()
    bot = Bot()
    asyncio.run(message_handler.on_voice(incoming, bot))

    queued.assert_awaited_once_with(incoming, "сделай страницу", bot)
    # One status answer + one non-blocking attempt for the failed preview.
    assert incoming.answer_calls == 2
