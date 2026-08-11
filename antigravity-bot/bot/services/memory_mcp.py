"""Native stdio MCP tools for AGY memory operations.

The server deliberately uses the small MCP JSON-RPC surface required by AGY
instead of exposing SQLite or a general shell to the model. It never writes
diagnostics to stdout because stdout is the MCP transport.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Mapping
from pathlib import Path

from bot.services.global_memory import MAX_FACT_CHARS

SERVER_NAME = "ag-telegram-memory"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
_VALID_SCOPES = {"global", "project"}

class MemoryToolError(ValueError):
    """A native memory request is invalid or cannot be safely fulfilled."""


def _normalize_text(text: object) -> str:
    if not isinstance(text, str):
        raise MemoryToolError("text must be a string")
    normalized = " ".join(text.split())
    if not normalized:
        raise MemoryToolError("text cannot be empty")
    if len(normalized) > MAX_FACT_CHARS:
        raise MemoryToolError(
            f"text exceeds the {MAX_FACT_CHARS}-character memory limit"
        )
    return normalized


def _normalize_scope(scope: object) -> str:
    if not isinstance(scope, str):
        raise MemoryToolError("scope must be 'global' or 'project'")
    normalized = scope.strip().lower()
    if normalized not in _VALID_SCOPES:
        raise MemoryToolError("scope must be 'global' or 'project'")
    return normalized


def _require_project_thread_id(thread_id: int | None) -> int:
    if isinstance(thread_id, bool) or not isinstance(thread_id, int) or thread_id <= 0:
        raise MemoryToolError(
            "project memory is available only inside a Telegram project topic"
        )
    return thread_id


def _open_database(db_path: str) -> sqlite3.Connection:
    if not str(db_path).strip():
        raise MemoryToolError("memory database path is not configured")
    target = Path(db_path).expanduser()
    if not target.is_file():
        raise MemoryToolError("memory database is unavailable")
    try:
        connection = sqlite3.connect(target, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection
    except sqlite3.Error as exc:
        raise MemoryToolError(f"memory database is unavailable: {exc}") from exc


def save_memory(
    text: str,
    scope: str = "global",
    *,
    db_path: str,
    thread_id: int | None = None,
) -> dict[str, object]:
    """Save one deduplicated fact without exposing database access to the agent."""
    value = _normalize_text(text)
    resolved_scope = _normalize_scope(scope)
    connection = _open_database(db_path)
    try:
        with connection:
            if resolved_scope == "global":
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO user_memory (fact) VALUES (?)", (value,)
                )
                row = connection.execute(
                    "SELECT id FROM user_memory WHERE fact = ?", (value,)
                ).fetchone()
            else:
                resolved_thread_id = _require_project_thread_id(thread_id)
                cursor = connection.execute(
                    """
                    INSERT INTO project_memory (thread_id, note, created_at)
                    SELECT ?, ?, CURRENT_TIMESTAMP
                    WHERE NOT EXISTS (
                        SELECT 1 FROM project_memory
                        WHERE thread_id = ? AND note = ?
                    )
                    """,
                    (resolved_thread_id, value, resolved_thread_id, value),
                )
                row = connection.execute(
                    """
                    SELECT id FROM project_memory
                    WHERE thread_id = ? AND note = ?
                    ORDER BY id ASC LIMIT 1
                    """,
                    (resolved_thread_id, value),
                ).fetchone()
        assert row is not None
        return {
            "scope": resolved_scope,
            "id": int(row["id"]),
            "status": "saved" if cursor.rowcount else "already_exists",
            "text": value,
        }
    except sqlite3.Error as exc:
        raise MemoryToolError(f"failed to save memory: {exc}") from exc
    finally:
        connection.close()


def list_memory(
    scope: str = "global",
    *,
    db_path: str,
    thread_id: int | None = None,
) -> dict[str, object]:
    """List only the facts available in the requested scope."""
    resolved_scope = _normalize_scope(scope)
    connection = _open_database(db_path)
    try:
        if resolved_scope == "global":
            rows = connection.execute(
                "SELECT id, fact AS text FROM user_memory ORDER BY id ASC"
            ).fetchall()
        else:
            resolved_thread_id = _require_project_thread_id(thread_id)
            rows = connection.execute(
                """
                SELECT id, note AS text FROM project_memory
                WHERE thread_id = ? ORDER BY id DESC
                """,
                (resolved_thread_id,),
            ).fetchall()
        return {
            "scope": resolved_scope,
            "entries": [dict(row) for row in rows],
        }
    except sqlite3.Error as exc:
        raise MemoryToolError(f"failed to list memory: {exc}") from exc
    finally:
        connection.close()


def delete_memory(
    memory_id: int,
    scope: str = "global",
    *,
    db_path: str,
    thread_id: int | None = None,
) -> dict[str, object]:
    """Delete a single fact in the allowed scope."""
    if isinstance(memory_id, bool) or not isinstance(memory_id, int) or memory_id <= 0:
        raise MemoryToolError("id must be a positive integer")
    resolved_scope = _normalize_scope(scope)
    connection = _open_database(db_path)
    try:
        with connection:
            if resolved_scope == "global":
                cursor = connection.execute(
                    "DELETE FROM user_memory WHERE id = ?", (memory_id,)
                )
            else:
                resolved_thread_id = _require_project_thread_id(thread_id)
                cursor = connection.execute(
                    "DELETE FROM project_memory WHERE id = ? AND thread_id = ?",
                    (memory_id, resolved_thread_id),
                )
        return {
            "scope": resolved_scope,
            "id": memory_id,
            "deleted": bool(cursor.rowcount),
        }
    except sqlite3.Error as exc:
        raise MemoryToolError(f"failed to delete memory: {exc}") from exc
    finally:
        connection.close()


_TOOLS: list[dict[str, object]] = [
    {
        "name": "save_memory",
        "description": (
            "Persist one user-provided fact. Use global for cross-project facts "
            "and project only for the current Telegram project topic."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Fact to remember"},
                "scope": {
                    "type": "string",
                    "enum": ["global", "project"],
                    "default": "global",
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_memory",
        "description": "List memory facts in the global or current project scope.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["global", "project"],
                    "default": "global",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_memory",
        "description": "Delete one memory fact by its ID in the requested scope.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "minimum": 1},
                "scope": {
                    "type": "string",
                    "enum": ["global", "project"],
                    "default": "global",
                },
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
]


def _success(request_id: object, result: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: object, code: int, message: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _tool_result(payload: dict[str, object], *, is_error: bool = False) -> dict[str, object]:
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
        ],
        "isError": is_error,
    }


def _handle_tool_call(
    params: Mapping[str, object],
    *,
    db_path: str,
    thread_id: int | None,
) -> dict[str, object]:
    name = params.get("name")
    arguments = params.get("arguments", {})
    if not isinstance(arguments, Mapping):
        return _tool_result({"error": "tool arguments must be an object"}, is_error=True)
    try:
        if name == "save_memory":
            output = save_memory(
                arguments.get("text", ""),
                arguments.get("scope", "global"),
                db_path=db_path,
                thread_id=thread_id,
            )
        elif name == "list_memory":
            output = list_memory(
                arguments.get("scope", "global"),
                db_path=db_path,
                thread_id=thread_id,
            )
        elif name == "delete_memory":
            output = delete_memory(
                arguments.get("id"),
                arguments.get("scope", "global"),
                db_path=db_path,
                thread_id=thread_id,
            )
        else:
            return _tool_result({"error": f"unknown memory tool: {name}"}, is_error=True)
    except MemoryToolError as exc:
        return _tool_result({"error": str(exc)}, is_error=True)
    return _tool_result(output)


def handle_request(
    request: Mapping[str, object],
    *,
    db_path: str,
    thread_id: int | None = None,
) -> dict[str, object] | None:
    """Handle one JSON-RPC request for a deterministic stdio MCP process."""
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params", {})
    if not isinstance(method, str):
        return _error(request_id, -32600, "method must be a string")
    if not isinstance(params, Mapping):
        return _error(request_id, -32602, "params must be an object")

    if method == "initialize":
        requested_version = params.get("protocolVersion")
        protocol_version = (
            requested_version
            if isinstance(requested_version, str) and requested_version
            else DEFAULT_PROTOCOL_VERSION
        )
        return _success(
            request_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if method == "tools/list":
        return _success(request_id, {"tools": _TOOLS})
    if method == "tools/call":
        return _success(
            request_id,
            _handle_tool_call(params, db_path=db_path, thread_id=thread_id),
        )
    if method == "ping":
        return _success(request_id, {})
    if request_id is None:
        # Notifications, including notifications/initialized, need no response.
        return None
    return _error(request_id, -32601, f"method not found: {method}")


def _thread_id_from_environment() -> int | None:
    raw = os.environ.get("AGY_TG_THREAD_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AGY native memory MCP server")
    parser.add_argument(
        "--db-path",
        default=os.environ.get("AGY_BOT_DB_PATH", ""),
        help="Bot SQLite database path (provided by the managed MCP config)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the native MCP server over stdin/stdout."""
    db_path = _build_parser().parse_args(argv).db_path.strip()
    if not db_path:
        print("memory database path is not configured", file=sys.stderr)
        return 2
    thread_id = _thread_id_from_environment()
    for raw_line in sys.stdin:
        try:
            request = json.loads(raw_line)
        except json.JSONDecodeError:
            response = _error(None, -32700, "parse error")
        else:
            if not isinstance(request, Mapping):
                response = _error(None, -32600, "request must be an object")
            else:
                response = handle_request(
                    request,
                    db_path=db_path,
                    thread_id=thread_id,
                )
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
