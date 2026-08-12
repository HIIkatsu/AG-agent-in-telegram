"""Bounded, per-chat Telegram request throttling and retry handling.

The bot renders a number of small status updates while a task is running.  A
``429 retry_after`` must never make an incoming message disappear or turn an
otherwise valid task into a failed one.  All task-facing sends and edits go
through this limiter so one chat observes Telegram's requested cooldown as a
single stream rather than several competing coroutines.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from aiogram.exceptions import TelegramRetryAfter


T = TypeVar("T")


class TelegramRateLimiter:
    """Per-chat request queue plus a per-message edit throttle."""

    def __init__(
        self,
        min_interval: float = 1.5,
        min_chat_interval: float = 0.75,
    ) -> None:
        self.min_interval = min_interval
        self.min_chat_interval = min_chat_interval
        self._last_edit: dict[tuple[int, int], float] = {}
        self._cooldown_until: dict[tuple[int, int], float] = {}
        self._chat_cooldown_until: dict[int, float] = {}
        self._last_chat_request: dict[int, float] = {}
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._lock = asyncio.Lock()

    def _chat_lock(self, chat_id: int) -> asyncio.Lock:
        """Return the one serialisation lock for a Telegram chat."""
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
        return lock

    async def request(
        self,
        chat_id: int,
        operation: Callable[[], Awaitable[T]],
        *,
        label: str,
        retries: int = 2,
    ) -> T:
        """Run one Telegram request, respecting and retrying ``retry_after``.

        The lock deliberately covers the cooldown sleep.  Telegram applies a
        flood limit to the chat, not merely to the individual status message;
        allowing a second coroutine through during that interval only creates
        another 429 and can lose a user update.
        """
        if retries < 0:
            raise ValueError("retries must not be negative")

        async with self._chat_lock(chat_id):
            attempt = 0
            while True:
                now = time.monotonic()
                delay = max(
                    self._chat_cooldown_until.get(chat_id, 0.0) - now,
                    self._last_chat_request.get(chat_id, 0.0)
                    + self.min_chat_interval
                    - now,
                )
                if delay > 0:
                    await asyncio.sleep(delay)

                try:
                    result = await operation()
                    self._last_chat_request[chat_id] = time.monotonic()
                    return result
                except TelegramRetryAfter as exc:
                    retry_after = max(0.0, float(exc.retry_after))
                    self._chat_cooldown_until[chat_id] = time.monotonic() + retry_after
                    self._last_chat_request[chat_id] = time.monotonic()
                    if attempt >= retries:
                        raise
                    attempt += 1
                    # The next iteration sleeps while still holding the chat
                    # lock, preventing another task UI update from racing it.

    async def _wait_for_message_cooldown(
        self,
        key: tuple[int, int],
        *,
        final: bool,
        force: bool,
    ) -> bool:
        """Apply the lightweight per-message edit throttle without lock sleeps."""
        async with self._lock:
            now = time.monotonic()
            cooldown = self._cooldown_until.get(key, 0.0)
            # ``0.0`` is a real-looking timestamp while a process is young:
            # on a fresh CI runner ``time.monotonic()`` can still be below the
            # configured interval.  Only throttle an edit when this message
            # has actually been edited before.
            last = self._last_edit.get(key)
            wait_for = max(0.0, cooldown - now)
            if not final and not force:
                if wait_for > 0 or (
                    last is not None and now - last < self.min_interval
                ):
                    return False

        if wait_for > 0:
            await asyncio.sleep(wait_for)

        async with self._lock:
            self._last_edit[key] = time.monotonic()
        return True

    async def allow_edit(self, chat_id: int, message_id: int, *, final: bool = False, force: bool = False) -> bool:
        return await self._wait_for_message_cooldown(
            (chat_id, message_id),
            final=final,
            force=force,
        )

    async def set_retry_after(self, chat_id: int, message_id: int, retry_after: float) -> None:
        async with self._lock:
            deadline = time.monotonic() + max(0.0, retry_after)
            self._cooldown_until[(chat_id, message_id)] = deadline
            self._chat_cooldown_until[chat_id] = deadline


telegram_rate_limiter = TelegramRateLimiter()
