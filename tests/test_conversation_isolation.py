"""Regression tests for AGY project isolation per Telegram topic."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "antigravity-bot"))
os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")

from bot.db import telegram_topic_key  # noqa: E402
from bot.handlers import message as message_handler  # noqa: E402


def test_conversation_id_uses_chat_and_topic_but_not_task_id() -> None:
    source = Path(message_handler.__file__).read_text(encoding="utf-8")

    assert 'f"telegram-topic:{chat_id}:{telegram_thread_id}"' in source
    assert 'f"session-{session_uuid}-task-{task_id}"' not in source


def test_telegram_topic_key_is_composite() -> None:
    assert telegram_topic_key(100, 7) == telegram_topic_key(100, 7)
    assert telegram_topic_key(100, 7) != telegram_topic_key(101, 7)
    assert telegram_topic_key(100, 7) != telegram_topic_key(100, 8)
    assert telegram_topic_key(100, None) == 0
