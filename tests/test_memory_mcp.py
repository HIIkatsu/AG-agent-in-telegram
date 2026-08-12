"""Regression tests for AGY's native two-scope memory MCP server."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "antigravity-bot"))
os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")

from bot.db import Database
from bot.services import memory_mcp
from bot.services.memory_mcp_config import (
    MEMORY_MCP_SERVER_NAME,
    MemoryMcpConfigError,
    ensure_memory_mcp_config,
    repair_invalid_memory_mcp_config,
)
from bot.services.memory_mcp_config import main as memory_mcp_config_main


def _bootstrap_database(path: Path) -> None:
    async def bootstrap() -> None:
        database = Database()
        database._path = str(path)
        await database.connect()
        await database.close()

    asyncio.run(bootstrap())


def test_native_memory_tools_keep_global_and_project_scopes_separate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "memory.db"
    _bootstrap_database(path)
    db_path = str(path)

    global_fact = memory_mcp.save_memory(
        "  Пользователь  любит подробные ответы  ", db_path=db_path
    )
    duplicate = memory_mcp.save_memory(
        "Пользователь любит подробные ответы", db_path=db_path
    )
    project_note = memory_mcp.save_memory(
        "Использовать aiogram 3", scope="project", db_path=db_path, thread_id=42
    )

    assert global_fact == {
        "scope": "global",
        "id": 1,
        "status": "saved",
        "text": "Пользователь любит подробные ответы",
    }
    assert duplicate["status"] == "already_exists"
    assert project_note["scope"] == "project"
    assert project_note["status"] == "saved"
    assert memory_mcp.list_memory(db_path=db_path) == {
        "scope": "global",
        "entries": [{"id": 1, "text": "Пользователь любит подробные ответы"}],
    }
    assert memory_mcp.list_memory(
        "project", db_path=db_path, thread_id=42
    ) == {
        "scope": "project",
        "entries": [{"id": 1, "text": "Использовать aiogram 3"}],
    }
    assert memory_mcp.list_memory(
        "project", db_path=db_path, thread_id=43
    ) == {"scope": "project", "entries": []}

    assert memory_mcp.delete_memory(1, "project", db_path=db_path, thread_id=43) == {
        "scope": "project",
        "id": 1,
        "deleted": False,
    }
    assert memory_mcp.delete_memory(1, "project", db_path=db_path, thread_id=42) == {
        "scope": "project",
        "id": 1,
        "deleted": True,
    }


def test_native_server_advertises_save_memory_and_rejects_project_without_topic(
    tmp_path: Path,
) -> None:
    path = tmp_path / "memory.db"
    _bootstrap_database(path)
    db_path = str(path)

    initialize = memory_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
        db_path=db_path,
    )
    assert initialize is not None
    assert initialize["result"]["serverInfo"]["name"] == memory_mcp.SERVER_NAME

    tool_list = memory_mcp.handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, db_path=db_path
    )
    assert tool_list is not None
    tools = tool_list["result"]["tools"]
    save_tool = next(tool for tool in tools if tool["name"] == "save_memory")
    assert save_tool["inputSchema"]["required"] == ["text"]
    assert save_tool["inputSchema"]["properties"]["scope"]["default"] == "global"

    response = memory_mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "save_memory",
                "arguments": {"text": "только проект", "scope": "project"},
            },
        },
        db_path=db_path,
    )
    assert response is not None
    result = response["result"]
    assert result["isError"] is True
    assert "Telegram project topic" in result["content"][0]["text"]


def test_memory_mcp_stdio_protocol_uses_only_json_on_stdout(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    _bootstrap_database(db_path)
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "save_memory",
                "arguments": {"text": "проектный факт", "scope": "project"},
            },
        },
    ]
    environment = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "antigravity-bot"),
        "AGY_BOT_DB_PATH": str(db_path),
        "AGY_TG_THREAD_ID": "77",
    }

    completed = subprocess.run(
        [sys.executable, "-m", "bot.services.memory_mcp"],
        input="".join(json.dumps(request) + "\n" for request in requests),
        text=True,
        capture_output=True,
        env=environment,
        check=True,
        timeout=10,
    )

    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [response["id"] for response in responses] == [1, 2, 3]
    saved = json.loads(responses[2]["result"]["content"][0]["text"])
    assert saved["scope"] == "project"
    assert saved["status"] == "saved"


def test_native_memory_tool_can_write_while_bot_database_is_connected(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        database = Database()
        database._path = str(tmp_path / "memory.db")
        await database.connect()
        try:
            saved = memory_mcp.save_memory(
                "Факт записан без shell-команды",
                db_path=database._path,
            )
            assert saved["status"] == "saved"
            facts = await database.get_all_user_memory()
            assert len(facts) == 1
            assert facts[0]["id"] == 1
            assert facts[0]["fact"] == "Факт записан без shell-команды"
        finally:
            await database.close()

    asyncio.run(exercise())


def test_mcp_config_merges_the_bot_server_without_losing_other_servers(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "mcp_config.json"
    config_path.write_text(
        json.dumps({"mcpServers": {"other": {"command": "node", "args": ["x"]}}}),
        encoding="utf-8",
    )
    bot_root = ROOT / "antigravity-bot"

    report = ensure_memory_mcp_config(
        config_path=config_path,
        python_executable=Path(sys.executable),
        bot_root=bot_root,
        db_path="/tmp/bot.db",
    )
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    server = payload["mcpServers"][MEMORY_MCP_SERVER_NAME]

    assert report.state == "installed"
    assert payload["mcpServers"]["other"] == {"command": "node", "args": ["x"]}
    assert server["args"] == [
        "-m",
        "bot.services.memory_mcp",
        "--db-path",
        "/tmp/bot.db",
    ]
    assert ensure_memory_mcp_config(
        config_path=config_path,
        python_executable=Path(sys.executable),
        bot_root=bot_root,
        db_path="/tmp/bot.db",
    ).state == "unchanged"


def test_mcp_config_accepts_jsonc_without_losing_other_servers(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "mcp_config.json"
    config_path.write_text(
        """{
  // Kept server configured by the user.
  "mcpServers": {
    "other": {
      "command": "node",
      "args": ["x",],
    },
  },
}
""",
        encoding="utf-8",
    )

    report = ensure_memory_mcp_config(
        config_path=config_path,
        python_executable=Path(sys.executable),
        bot_root=ROOT / "antigravity-bot",
        db_path="/tmp/bot.db",
    )
    payload = json.loads(config_path.read_text(encoding="utf-8"))

    assert report.state == "installed"
    assert payload["mcpServers"]["other"] == {"command": "node", "args": ["x"]}
    assert MEMORY_MCP_SERVER_NAME in payload["mcpServers"]


def test_invalid_mcp_config_is_not_overwritten(tmp_path: Path) -> None:
    config_path = tmp_path / "mcp_config.json"
    original = '{"mcpServers": {"other": '
    config_path.write_text(original, encoding="utf-8")

    with pytest.raises(MemoryMcpConfigError, match="not valid JSON or JSONC"):
        ensure_memory_mcp_config(
            config_path=config_path,
            python_executable=Path(sys.executable),
            bot_root=ROOT / "antigravity-bot",
            db_path="/tmp/bot.db",
        )

    assert config_path.read_text(encoding="utf-8") == original


def test_repair_invalid_mcp_config_preserves_private_backup(tmp_path: Path) -> None:
    config_path = tmp_path / "mcp_config.json"
    original = b'{"mcpServers": {"other": '
    config_path.write_bytes(original)

    report = repair_invalid_memory_mcp_config(
        config_path=config_path,
        python_executable=Path(sys.executable),
        bot_root=ROOT / "antigravity-bot",
        db_path="/tmp/bot.db",
    )

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    server = payload["mcpServers"][MEMORY_MCP_SERVER_NAME]
    assert report.config_path == config_path
    assert report.backup_path.read_bytes() == original
    assert stat.S_IMODE(report.backup_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert server["command"] == str(Path(sys.executable).resolve())
    assert server["args"][-2:] == ["--db-path", "/tmp/bot.db"]


def test_repair_invalid_mcp_config_refuses_valid_user_config(tmp_path: Path) -> None:
    config_path = tmp_path / "mcp_config.json"
    original = json.dumps(
        {"mcpServers": {"other": {"command": "node", "args": ["x"]}}}
    )
    config_path.write_text(original, encoding="utf-8")

    with pytest.raises(MemoryMcpConfigError, match="already valid"):
        repair_invalid_memory_mcp_config(
            config_path=config_path,
            python_executable=Path(sys.executable),
            bot_root=ROOT / "antigravity-bot",
            db_path="/tmp/bot.db",
        )

    assert config_path.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("mcp_config.json.invalid-*.bak"))


def test_repair_invalid_mcp_config_does_not_hide_schema_error(tmp_path: Path) -> None:
    config_path = tmp_path / "mcp_config.json"
    original = json.dumps({"mcpServers": []})
    config_path.write_text(original, encoding="utf-8")

    with pytest.raises(MemoryMcpConfigError, match="non-syntax"):
        repair_invalid_memory_mcp_config(
            config_path=config_path,
            python_executable=Path(sys.executable),
            bot_root=ROOT / "antigravity-bot",
            db_path="/tmp/bot.db",
        )

    assert config_path.read_text(encoding="utf-8") == original


def test_repair_invalid_mcp_config_cli_uses_configured_runtime_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from bot.config import settings

    config_path = tmp_path / "mcp_config.json"
    config_path.write_text('{"mcpServers":', encoding="utf-8")
    monkeypatch.setattr(settings, "agy_mcp_config_path", str(config_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "bot.db"))

    assert memory_mcp_config_main(["repair-invalid"]) == 0
    output = capsys.readouterr().out

    assert "MCP config repaired:" in output
    assert "Original config backup (0600):" in output
    assert MEMORY_MCP_SERVER_NAME in json.loads(
        config_path.read_text(encoding="utf-8")
    )["mcpServers"]


def test_memory_mcp_configuration_failure_does_not_stop_the_bot(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from bot import __main__ as bot_main
    from bot.config import settings
    from bot.services import memory_mcp_config

    def fail_configuration(**_: object) -> None:
        raise MemoryMcpConfigError("invalid user config")

    monkeypatch.setattr(memory_mcp_config, "ensure_memory_mcp_config", fail_configuration)
    monkeypatch.setattr(settings, "agy_allow_unsandboxed_dev", True)

    with caplog.at_level(logging.WARNING, logger=bot_main.logger.name):
        assert bot_main._configure_native_memory_tools() is False

    assert "bot will continue without them" in caplog.text


def test_production_uses_the_task_scoped_memory_mcp_without_touching_user_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bot import __main__ as bot_main
    from bot.config import settings
    from bot.services import memory_mcp_config

    # A production worker owns a generated task config, so the historical
    # service-home config must not be opened or repaired on bot startup.
    monkeypatch.setattr(settings, "agy_allow_unsandboxed_dev", False)

    def must_not_be_called(**_: object) -> None:
        raise AssertionError("production must not configure the service-home MCP file")

    monkeypatch.setattr(memory_mcp_config, "ensure_memory_mcp_config", must_not_be_called)

    assert bot_main._configure_native_memory_tools() is True


def test_mcp_config_refuses_to_overwrite_a_user_owned_server(tmp_path: Path) -> None:
    config_path = tmp_path / "mcp_config.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    MEMORY_MCP_SERVER_NAME: {"command": "node", "args": ["other.js"]}
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(MemoryMcpConfigError, match="Refusing to overwrite"):
        ensure_memory_mcp_config(
            config_path=config_path,
            python_executable=Path(sys.executable),
            bot_root=ROOT / "antigravity-bot",
            db_path="/tmp/bot.db",
        )
