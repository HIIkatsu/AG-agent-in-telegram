"""Regression tests for the per-task memory and SSH capability broker."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "antigravity-bot"))
os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")

from bot.config import settings
from bot.services import capability_broker


def test_broker_rejects_wrong_token_and_handles_memory_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, object, str, int | None]] = []

    def fake_save_memory(
        text: object,
        scope: object,
        *,
        db_path: str,
        thread_id: int | None,
    ) -> dict[str, object]:
        calls.append((text, scope, db_path, thread_id))
        return {"scope": scope, "id": 7, "status": "saved", "text": text}

    monkeypatch.setattr(capability_broker.memory_mcp, "save_memory", fake_save_memory)
    monkeypatch.setattr(settings, "agy_capability_socket_dir", str(tmp_path / "sockets"))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "private.db"))
    broker = capability_broker.TaskCapabilityBroker(
        bot=SimpleNamespace(),
        chat_id=1,
        thread_id=77,
        task_id=None,
        worker_uid=os.geteuid(),
        worker_gid=os.getegid(),
    )

    async def exercise() -> dict[str, object]:
        with pytest.raises(
            capability_broker.CapabilityBrokerError,
            match="authentication failed",
        ):
            await broker._dispatch(
                {"token": "wrong", "action": "memory.list", "arguments": {}}
            )
        return await broker._dispatch(
            {
                "token": broker._token,
                "action": "memory.save",
                "arguments": {"text": "remember this", "scope": "project"},
            }
        )

    accepted = asyncio.run(exercise())

    assert accepted == {
        "scope": "project",
        "id": 7,
        "status": "saved",
        "text": "remember this",
    }
    assert calls == [("remember this", "project", str(tmp_path / "private.db"), 77)]


def test_ssh_capability_always_requires_telegram_approval_and_bounds_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    permission = SimpleNamespace(handle_permission=AsyncMock(return_value=True))
    monkeypatch.setattr(capability_broker, "permission_handler", permission)

    async def fake_ssh(
        environment: str,
        command: str,
        cwd: str | None,
    ) -> tuple[int, str, str]:
        assert (environment, command, cwd) == ("vps", "pwd", "/srv/app")
        return 0, "x" * 25_000, ""

    broker = capability_broker.TaskCapabilityBroker(
        bot=SimpleNamespace(),
        chat_id=123,
        thread_id=456,
        task_id=None,
        worker_uid=os.geteuid(),
        worker_gid=os.getegid(),
        ssh_executor=fake_ssh,
    )

    result = asyncio.run(
        broker._dispatch(
            {
                "token": broker._token,
                "action": "ssh.exec",
                "arguments": {
                    "environment": "vps",
                    "command": "pwd",
                    "cwd": "/srv/app",
                },
            }
        )
    )

    assert result["exit_code"] == 0
    assert str(result["stdout"]).endswith("output truncated by capability broker …")
    permission.handle_permission.assert_awaited_once_with(
        bot=broker._bot,
        chat_id=123,
        tool_name="ssh_exec",
        parameters={"environment": "vps", "command": "pwd", "cwd": "/srv/app"},
        thread_id=456,
        force_approval=True,
        timeout_seconds=settings.ssh_approval_timeout_seconds,
    )


def test_ssh_capability_cannot_execute_after_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    permission = SimpleNamespace(handle_permission=AsyncMock(return_value=False))
    execute = AsyncMock()
    monkeypatch.setattr(capability_broker, "permission_handler", permission)
    broker = capability_broker.TaskCapabilityBroker(
        bot=SimpleNamespace(),
        chat_id=1,
        thread_id=None,
        task_id=None,
        worker_uid=os.geteuid(),
        worker_gid=os.getegid(),
        ssh_executor=execute,
    )

    with pytest.raises(capability_broker.CapabilityBrokerError, match="not approved"):
        asyncio.run(
            broker._dispatch(
                {
                    "token": broker._token,
                    "action": "ssh.exec",
                    "arguments": {"environment": "vps", "command": "id"},
                }
            )
        )

    execute.assert_not_awaited()
