"""Tests for batching Telegram albums into a single task."""

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "antigravity-bot"))
os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")

from bot.handlers import message as message_handler


@pytest.fixture(autouse=True)
def clear_media_groups():
    message_handler._media_groups.clear()
    yield
    for group in message_handler._media_groups.values():
        if group.timer is not None:
            group.timer.cancel()
    message_handler._media_groups.clear()


def test_media_group_is_enqueued_once_with_every_attachment(monkeypatch):
    process = AsyncMock()
    monkeypatch.setattr(message_handler, "_process", process)
    monkeypatch.setattr(message_handler, "_MEDIA_GROUP_DEBOUNCE_SECONDS", 0.01)

    async def exercise_debouncer():
        bot = object()
        first = SimpleNamespace(media_group_id="album-1", chat=SimpleNamespace(id=10))
        second = SimpleNamespace(media_group_id="album-1", chat=SimpleNamespace(id=10))

        assert message_handler._buffer_media_group(
            first, bot, "uploads/a.jpg", "Общий заголовок"
        )
        assert message_handler._buffer_media_group(
            second, bot, "uploads/b.jpg", None
        )

        await asyncio.sleep(0.03)

        process.assert_awaited_once_with(
            first,
            "Общий заголовок",
            bot,
            files=["uploads/a.jpg", "uploads/b.jpg"],
        )
        assert message_handler._media_groups == {}

    asyncio.run(exercise_debouncer())


def test_message_without_media_group_keeps_fast_path():
    message = SimpleNamespace(media_group_id=None, chat=SimpleNamespace(id=10))

    assert not message_handler._buffer_media_group(
        message, object(), "uploads/single.jpg", None
    )
    assert message_handler._media_groups == {}
