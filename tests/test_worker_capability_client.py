"""Regression tests for the dependency-free client mounted in the worker."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
CLIENT_PATH = ROOT / "antigravity-bot" / "worker_tools" / "capability_client.py"


def _load_client_module():
    spec = importlib.util.spec_from_file_location("worker_capability_client", CLIENT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_worker_memory_mcp_routes_to_broker_without_bot_imports(monkeypatch) -> None:
    client = _load_client_module()
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_call(action: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append((action, dict(arguments)))
        return {"scope": "global", "id": 5, "status": "saved", "text": "fact"}

    monkeypatch.setattr(client, "broker_call", fake_call)
    response = client.handle_memory_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "save_memory",
                "arguments": {"text": "fact", "scope": "global"},
            },
        }
    )

    assert response is not None
    assert response["result"]["isError"] is False
    assert calls == [("memory.save", {"text": "fact", "scope": "global"})]
    source = CLIENT_PATH.read_text(encoding="utf-8")
    assert "from bot" not in source
    assert "sqlite3" not in source
    assert "AGY_BOT_DB_PATH" not in source


def test_worker_mistral_tool_routes_only_a_structured_image_request(monkeypatch) -> None:
    client = _load_client_module()
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_call(action: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append((action, dict(arguments)))
        return {
            "status": "saved_for_telegram",
            "files": ["mistral-image-1.png"],
            "count": 1,
        }

    monkeypatch.setattr(client, "broker_call", fake_call)
    listed = client.handle_memory_request(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    )
    response = client.handle_memory_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "mistral_generate_image",
                "arguments": {"prompt": "Летающий город"},
            },
        }
    )

    assert listed is not None
    tool_names = {tool["name"] for tool in listed["result"]["tools"]}
    assert "mistral_generate_image" in tool_names
    assert response is not None
    assert response["result"]["isError"] is False
    assert calls == [("image.generate", {"prompt": "Летающий город"})]


def test_worker_ag_ssh_client_only_submits_a_structured_broker_request(
    monkeypatch, capsys
) -> None:
    client = _load_client_module()
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_call(action: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append((action, dict(arguments)))
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(client, "broker_call", fake_call)

    assert client.run_ssh(["exec", "production", "pwd", "--cwd", "/srv/app"]) == 0
    assert calls == [
        (
            "ssh.exec",
            {"environment": "production", "command": "pwd", "cwd": "/srv/app"},
        )
    ]
    assert "SSH EXIT CODE: 0" in capsys.readouterr().out
