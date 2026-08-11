"""Install the bot-owned native memory MCP server without clobbering user config."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MEMORY_MCP_SERVER_NAME = "ag-telegram-memory"
DEFAULT_MCP_CONFIG_PATH = Path("~/.gemini/config/mcp_config.json")
_MEMORY_MCP_MODULE = "bot.services.memory_mcp"


class MemoryMcpConfigError(RuntimeError):
    """The shared AGY MCP configuration cannot be safely updated."""


@dataclass(frozen=True)
class MemoryMcpConfigReport:
    """Result of ensuring the bot-owned MCP server entry."""

    config_path: Path
    state: str


def _expected_server(
    *,
    python_executable: Path,
    bot_root: Path,
    db_path: str,
) -> dict[str, object]:
    resolved_root = bot_root.expanduser().resolve()
    return {
        "command": str(python_executable.expanduser().resolve()),
        "args": [
            "-m",
            _MEMORY_MCP_MODULE,
            "--db-path",
            str(Path(db_path).expanduser().resolve()),
        ],
        "cwd": str(resolved_root),
    }


def _load_config(config_path: Path) -> dict[str, Any]:
    try:
        raw = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise MemoryMcpConfigError(
            f"Cannot read MCP config {config_path}: {exc}"
        ) from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MemoryMcpConfigError(
            f"MCP config is not valid JSON: {config_path}"
        ) from exc
    if not isinstance(payload, dict):
        raise MemoryMcpConfigError(
            f"MCP config must contain a JSON object: {config_path}"
        )
    servers = payload.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        raise MemoryMcpConfigError(
            f"MCP config field mcpServers must be an object: {config_path}"
        )
    return payload


def _is_bot_owned_server(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    args = value.get("args")
    return isinstance(args, list) and args[:2] == ["-m", _MEMORY_MCP_MODULE]


def _write_config(config_path: Path, payload: dict[str, Any]) -> None:
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=".ag-memory-mcp-",
            suffix=".json",
            dir=config_path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.chmod(temp_name, 0o600)
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, config_path)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise
    except OSError as exc:
        raise MemoryMcpConfigError(
            f"Cannot write MCP config {config_path}: {exc}"
        ) from exc


def ensure_memory_mcp_config(
    *,
    config_path: Path = DEFAULT_MCP_CONFIG_PATH,
    python_executable: Path,
    bot_root: Path,
    db_path: str,
) -> MemoryMcpConfigReport:
    """Merge a safe, bot-owned memory server into AGY's global MCP config.

    The function keeps all unrelated servers intact. An existing entry with the
    same name is updated only when it already launches this bot's server; any
    other entry is treated as user-owned and causes a safe startup failure.
    """
    target = config_path.expanduser()
    payload = _load_config(target)
    servers = payload.setdefault("mcpServers", {})
    assert isinstance(servers, dict)

    expected = _expected_server(
        python_executable=python_executable,
        bot_root=bot_root,
        db_path=db_path,
    )
    existing = servers.get(MEMORY_MCP_SERVER_NAME)
    if existing is not None and not _is_bot_owned_server(existing):
        raise MemoryMcpConfigError(
            "Refusing to overwrite user-owned MCP server "
            f"{MEMORY_MCP_SERVER_NAME!r} in {target}"
        )
    if existing == expected:
        return MemoryMcpConfigReport(config_path=target, state="unchanged")

    servers[MEMORY_MCP_SERVER_NAME] = expected
    _write_config(target, payload)
    state = "installed" if existing is None else "updated"
    return MemoryMcpConfigReport(config_path=target, state=state)
