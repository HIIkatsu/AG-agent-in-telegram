"""Regression tests for host-key verification in brokered SSH execution."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "antigravity-bot"))
os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")

from bot.config import settings
from bot.services import ssh_executor


def _environment(key_path: Path) -> dict[str, object]:
    return {
        "name": "production",
        "host": "example.invalid",
        "port": 2222,
        "username": "agent",
        "ssh_key_path": str(key_path),
    }


def test_ssh_refuses_unknown_hosts_before_connecting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_path = tmp_path / "id_ed25519"
    key_path.write_text("private", encoding="utf-8")
    missing_known_hosts = tmp_path / "known_hosts"
    monkeypatch.setattr(settings, "ssh_known_hosts_path", str(missing_known_hosts))
    monkeypatch.setattr(
        ssh_executor.db,
        "get_environment_by_name",
        AsyncMock(return_value=_environment(key_path)),
    )
    connect = AsyncMock()
    monkeypatch.setattr(ssh_executor.asyncssh, "connect", connect)
    monkeypatch.setattr(ssh_executor, "ensure_key_exists", lambda: str(key_path))

    result = asyncio.run(ssh_executor.execute_command("production", "hostname"))

    assert result[0] == -1
    assert "known_hosts file is missing" in result[2]
    connect.assert_not_called()


def test_ssh_uses_verified_known_hosts_and_quotes_remote_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_path = tmp_path / "id_ed25519"
    key_path.write_text("private", encoding="utf-8")
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("example.invalid ssh-ed25519 AAAA\n", encoding="utf-8")
    monkeypatch.setattr(settings, "ssh_known_hosts_path", str(known_hosts))
    monkeypatch.setattr(settings, "ssh_command_timeout_seconds", 5)
    monkeypatch.setattr(
        ssh_executor.db,
        "get_environment_by_name",
        AsyncMock(return_value=_environment(key_path)),
    )
    monkeypatch.setattr(ssh_executor, "ensure_key_exists", lambda: str(key_path))
    captured: dict[str, object] = {}

    class _Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def run(self, command: str, *, check: bool):
            captured["command"] = command
            assert check is False
            return type("Result", (), {"exit_status": 0, "stdout": "ok", "stderr": ""})()

    def fake_connect(**kwargs):
        captured["connect"] = kwargs
        return _Connection()

    monkeypatch.setattr(ssh_executor.asyncssh, "connect", fake_connect)

    result = asyncio.run(
        ssh_executor.execute_command("production", "pwd", "/srv/a path; unsafe")
    )

    assert result == (0, "ok", "")
    assert captured["connect"] == {
        "host": "example.invalid",
        "port": 2222,
        "username": "agent",
        "client_keys": [str(key_path)],
        "known_hosts": str(known_hosts.resolve()),
    }
    assert captured["command"] == "cd -- '/srv/a path; unsafe' && pwd"
