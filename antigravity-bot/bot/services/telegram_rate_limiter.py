"""Small Telegram edit rate limiter for task status messages."""

from __future__ import annotations

import asyncio
import time


class TelegramRateLimiter:
    """Per chat/message throttle with retry-after cooldown support."""

    def __init__(self, min_interval: float = 1.5) -> None:
        self.min_interval = min_interval
        self._last_edit: dict[tuple[int, int], float] = {}
        self._cooldown_until: dict[tuple[int, int], float] = {}
        self._lock = asyncio.Lock()

    async def allow_edit(self, chat_id: int, message_id: int, *, final: bool = False, force: bool = False) -> bool:
        key = (chat_id, message_id)
        async with self._lock:
            now = time.monotonic()
            cooldown = self._cooldown_until.get(key, 0.0)
            if now < cooldown:
                if final or force:
                    await asyncio.sleep(cooldown - now)
                else:
                    return False
            last = self._last_edit.get(key, 0.0)
            if not final and now - last < self.min_interval:
                return False
            self._last_edit[key] = time.monotonic()
            return True

    async def set_retry_after(self, chat_id: int, message_id: int, retry_after: float) -> None:
        async with self._lock:
            self._cooldown_until[(chat_id, message_id)] = time.monotonic() + retry_after


telegram_rate_limiter = TelegramRateLimiter()
