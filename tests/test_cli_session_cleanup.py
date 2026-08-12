"""Regression tests for safe AGY session cleanup."""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "antigravity-bot"))
os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")

from bot.handlers import chats
from bot.config import settings


def test_cleanup_only_touches_invalid_sessions_inside_brain(tmp_path: Path, monkeypatch) -> None:
    worker_home = tmp_path / "worker-home"
    cli_root = worker_home / ".gemini" / "antigravity-cli"
    skill = cli_root / "skills" / "global-memory" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("must survive", encoding="utf-8")
    root_directory = cli_root / "unrelated-user-data"
    root_directory.mkdir()

    brain = cli_root / "brain"
    valid_session = brain / str(uuid.uuid4())
    broken_session = brain / "not-a-session"
    valid_session.mkdir(parents=True)
    broken_session.mkdir(parents=True)

    monkeypatch.setattr(settings, "agy_worker_home", str(worker_home))

    assert chats.purge_stale_cli_sessions() == 1
    assert skill.read_text(encoding="utf-8") == "must survive"
    assert root_directory.is_dir()
    assert valid_session.is_dir()
    assert not broken_session.exists()
