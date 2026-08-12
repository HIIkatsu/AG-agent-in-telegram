"""Install the bot-owned native memory MCP server without clobbering user config."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MEMORY_MCP_SERVER_NAME = "ag-telegram-memory"
DEFAULT_MCP_CONFIG_PATH = Path("~/.gemini/config/mcp_config.json")
_MEMORY_MCP_MODULE = "bot.services.memory_mcp"


class MemoryMcpConfigError(RuntimeError):
    """The shared AGY MCP configuration cannot be safely updated."""


class InvalidMemoryMcpConfigError(MemoryMcpConfigError):
    """The MCP config cannot be parsed as JSON or JSONC."""


@dataclass(frozen=True)
class MemoryMcpConfigReport:
    """Result of ensuring the bot-owned MCP server entry."""

    config_path: Path
    state: str


@dataclass(frozen=True)
class MemoryMcpConfigRepairReport:
    """Result of explicitly repairing an unreadable MCP config."""

    config_path: Path
    backup_path: Path


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


def _strip_jsonc_comments(raw: str) -> str:
    """Remove JSONC comments without touching comment-like text in strings."""
    result: list[str] = []
    in_string = False
    escaped = False
    index = 0

    while index < len(raw):
        char = raw[index]
        next_char = raw[index + 1] if index + 1 < len(raw) else ""

        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue

        if char == "/" and next_char == "/":
            index += 2
            while index < len(raw) and raw[index] not in "\r\n":
                index += 1
            continue

        if char == "/" and next_char == "*":
            index += 2
            while index < len(raw):
                if raw[index] == "*" and index + 1 < len(raw) and raw[index + 1] == "/":
                    index += 2
                    break
                # Keep line boundaries so parser errors remain understandable.
                if raw[index] in "\r\n":
                    result.append(raw[index])
                index += 1
            else:
                raise ValueError("unterminated JSONC block comment")
            continue

        result.append(char)
        index += 1

    return "".join(result)


def _strip_jsonc_trailing_commas(raw: str) -> str:
    """Remove trailing commas outside strings, as allowed by JSONC."""
    result: list[str] = []
    in_string = False
    escaped = False
    index = 0

    while index < len(raw):
        char = raw[index]
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue

        if char == ",":
            lookahead = index + 1
            while lookahead < len(raw) and raw[lookahead].isspace():
                lookahead += 1
            if lookahead < len(raw) and raw[lookahead] in "]}":
                index += 1
                continue

        result.append(char)
        index += 1

    return "".join(result)


def _load_json_or_jsonc(raw: str, config_path: Path) -> object:
    """Load strict JSON first, then the JSONC form used by some AGY setups."""
    raw = raw.lstrip("\ufeff")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            normalized = _strip_jsonc_trailing_commas(_strip_jsonc_comments(raw))
            return json.loads(normalized)
        except (json.JSONDecodeError, ValueError) as jsonc_error:
            raise InvalidMemoryMcpConfigError(
                f"MCP config is not valid JSON or JSONC: {config_path}"
            ) from jsonc_error


def _load_config(config_path: Path) -> dict[str, Any]:
    try:
        raw = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except UnicodeError as exc:
        raise InvalidMemoryMcpConfigError(
            f"MCP config is not valid UTF-8: {config_path}"
        ) from exc
    except OSError as exc:
        raise MemoryMcpConfigError(
            f"Cannot read MCP config {config_path}: {exc}"
        ) from exc

    payload = _load_json_or_jsonc(raw, config_path)
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


def _invalid_backup_path(config_path: Path, raw: bytes) -> Path:
    """Return a unique local-only filename without exposing config contents."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    digest = hashlib.sha256(raw).hexdigest()[:12]
    return config_path.with_name(
        f"{config_path.name}.invalid-{stamp}-{digest}.bak"
    )


def _write_private_backup(backup_path: Path, raw: bytes) -> None:
    """Persist original invalid bytes atomically with owner-only permissions."""
    if backup_path.exists():
        raise MemoryMcpConfigError(
            f"Refusing to overwrite existing MCP config backup: {backup_path}"
        )
    try:
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=".ag-memory-mcp-backup-",
            suffix=".tmp",
            dir=backup_path.parent,
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                os.chmod(temp_name, 0o600)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, backup_path)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise
    except OSError as exc:
        raise MemoryMcpConfigError(
            f"Cannot create MCP config backup {backup_path}: {exc}"
        ) from exc


def repair_invalid_memory_mcp_config(
    *,
    config_path: Path = DEFAULT_MCP_CONFIG_PATH,
    python_executable: Path,
    bot_root: Path,
    db_path: str,
) -> MemoryMcpConfigRepairReport:
    """Back up and replace only a syntactically unreadable MCP config.

    This is intentionally an explicit operator action. Valid configurations,
    including those with an incompatible schema, are never replaced because
    they may contain unrelated user-owned MCP server definitions.
    """
    target = config_path.expanduser()
    if target.is_symlink():
        raise MemoryMcpConfigError(
            f"Refusing to repair MCP config symlink: {target}"
        )

    try:
        _load_config(target)
    except InvalidMemoryMcpConfigError:
        pass
    except MemoryMcpConfigError as exc:
        raise MemoryMcpConfigError(
            f"Refusing to replace a non-syntax MCP config error: {exc}"
        ) from exc
    else:
        raise MemoryMcpConfigError(
            f"MCP config is already valid; refusing repair: {target}"
        )

    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise MemoryMcpConfigError(
            f"Cannot read invalid MCP config {target}: {exc}"
        ) from exc

    backup_path = _invalid_backup_path(target, raw)
    _write_private_backup(backup_path, raw)
    _write_config(
        target,
        {
            "mcpServers": {
                MEMORY_MCP_SERVER_NAME: _expected_server(
                    python_executable=python_executable,
                    bot_root=bot_root,
                    db_path=db_path,
                )
            }
        },
    )
    return MemoryMcpConfigRepairReport(
        config_path=target,
        backup_path=backup_path,
    )


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


def main(argv: list[str] | None = None) -> int:
    """Run an explicit, no-content-exposure repair for a broken global config."""
    parser = argparse.ArgumentParser(
        description="Repair an invalid AGY MCP config without exposing its contents."
    )
    parser.add_argument(
        "command",
        choices=("repair-invalid",),
        help="back up a syntactically invalid config and install native memory MCP",
    )
    args = parser.parse_args(argv)

    if args.command == "repair-invalid":
        from bot.config import settings
        from bot.services.instructions import BOT_ROOT

        try:
            report = repair_invalid_memory_mcp_config(
                config_path=Path(settings.agy_mcp_config_path),
                python_executable=Path(sys.executable),
                bot_root=BOT_ROOT,
                db_path=settings.db_path,
            )
        except MemoryMcpConfigError as exc:
            print(f"MCP repair was not applied: {exc}", file=sys.stderr)
            return 1
        print(f"MCP config repaired: {report.config_path}")
        print(f"Original config backup (0600): {report.backup_path}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
