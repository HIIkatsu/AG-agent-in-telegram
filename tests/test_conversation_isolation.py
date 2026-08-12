"""Regression tests for AGY project isolation per Telegram task."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "antigravity-bot"))
os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")

from bot.handlers import message as message_handler  # noqa: E402


def test_conversation_id_uses_session_uuid_and_task_id() -> None:
    # Guard the source-level invariant that prevents stale AGY --continue state
    # from one failed task/topic being replayed into later Telegram messages.
    source = Path(message_handler.__file__).read_text(encoding="utf-8")

    assert 'session_uuid = str(session.get("uuid") or f"thread-{thread_id}")' in source
    assert 'f"session-{session_uuid}-task-{task_id}"' in source
    assert 'f"thread-{thread_id}"\n                        if execution_profile == "chat"' not in source
