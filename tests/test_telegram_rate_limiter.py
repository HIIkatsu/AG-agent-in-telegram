"""Regression tests for Telegram flood-control recovery."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage

from bot.services.telegram_rate_limiter import TelegramRateLimiter
from bot.services.tracker import TaskTracker


def _retry_after() -> TelegramRetryAfter:
    return TelegramRetryAfter(
        method=SendMessage(chat_id=1, text="status"),
        message="Too Many Requests: retry after 0",
        retry_after=0,
    )


def test_request_retries_a_telegram_429_without_losing_the_operation() -> None:
    limiter = TelegramRateLimiter(min_chat_interval=0)
    calls = 0

    async def send() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _retry_after()
        return "sent"

    assert asyncio.run(limiter.request(100, send, label="test status")) == "sent"
    assert calls == 2


def test_nonfinal_edit_is_suppressed_during_its_message_cooldown() -> None:
    limiter = TelegramRateLimiter(min_interval=60, min_chat_interval=0)

    async def exercise() -> tuple[bool, bool]:
        first = await limiter.allow_edit(100, 5)
        second = await limiter.allow_edit(100, 5)
        return first, second

    assert asyncio.run(exercise()) == (True, False)


def test_tracker_can_finish_without_a_status_card() -> None:
    """A Telegram UI failure must not make the durable task runner crash."""

    async def exercise() -> None:
        tracker = TaskTracker(
            SimpleNamespace(),
            thread_id=5,
            status_message=None,
            task_id=None,
        )
        await tracker.start()
        await tracker.feed_text("Ответ сохранён в журнале задачи")
        await tracker.on_tool_start("run_command", "Выполнение")
        await tracker.finish("DONE")

    asyncio.run(exercise())
