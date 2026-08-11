"""Regression tests for the remote-environments CLI adapter."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "antigravity-bot"))
os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")

from bot.services import ssh_tool


def test_ssh_tool_lists_environments_and_closes_database(monkeypatch, capsys) -> None:
    connect = AsyncMock()
    close = AsyncMock()
    get_environments = AsyncMock(
        return_value=[
            {
                "name": "home",
                "username": "agent",
                "host": "host.example",
                "port": 2222,
            }
        ]
    )
    monkeypatch.setattr(ssh_tool.db, "connect", connect)
    monkeypatch.setattr(ssh_tool.db, "close", close)
    monkeypatch.setattr(ssh_tool.db, "get_all_environments", get_environments)

    result = asyncio.run(ssh_tool.main(["list"]))

    assert result == 0
    assert "agent@host.example:2222" in capsys.readouterr().out
    connect.assert_awaited_once_with()
    close.assert_awaited_once_with()


def test_ssh_tool_returns_failure_code_and_still_closes_database(
    monkeypatch, capsys
) -> None:
    connect = AsyncMock()
    close = AsyncMock()
    execute = AsyncMock(return_value=(-1, "", "offline"))
    monkeypatch.setattr(ssh_tool.db, "connect", connect)
    monkeypatch.setattr(ssh_tool.db, "close", close)
    monkeypatch.setattr(ssh_tool, "execute_command", execute)

    result = asyncio.run(ssh_tool.main(["exec", "home", "pwd"]))

    assert result == 1
    assert "offline" in capsys.readouterr().out
    execute.assert_awaited_once_with("home", "pwd", None)
    close.assert_awaited_once_with()
