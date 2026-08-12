#!/usr/bin/env python3
"""Sandbox-side client for the task-scoped memory and SSH capability broker.

This module intentionally uses only the Python standard library.  It is mounted
read-only into the worker and has no path to the bot source, database or keys.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from collections.abc import Mapping, Sequence
from typing import Any

_MAX_REQUEST_BYTES = 16 * 1024
_MAX_RESPONSE_BYTES = 64 * 1024
_SOCKET_TIMEOUT_SECONDS = 180
_SERVER_NAME = "ag-telegram-capabilities"
_SERVER_VERSION = "1.1.0"
_DEFAULT_PROTOCOL_VERSION = "2025-06-18"


class BrokerClientError(RuntimeError):
    """A task-scoped broker request could not be fulfilled."""


def _endpoint() -> tuple[str, str]:
    socket_path = os.environ.get("AGY_CAPABILITY_SOCKET", "").strip()
    token = os.environ.get("AGY_CAPABILITY_TOKEN", "").strip()
    if not socket_path or not token:
        raise BrokerClientError("task capability broker is unavailable")
    return socket_path, token


def _read_line(connection: socket.socket) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = connection.recv(4096)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_RESPONSE_BYTES:
            raise BrokerClientError("capability broker response is too large")
        chunks.append(chunk)
        joined = b"".join(chunks)
        if b"\n" in joined:
            return joined.split(b"\n", 1)[0]
    return b"".join(chunks)


def broker_call(action: str, arguments: Mapping[str, object]) -> dict[str, object]:
    """Call exactly one allowed capability over the per-task Unix socket."""
    socket_path, token = _endpoint()
    request = json.dumps(
        {"token": token, "action": action, "arguments": dict(arguments)},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(request) > _MAX_REQUEST_BYTES:
        raise BrokerClientError("capability request is too large")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(_SOCKET_TIMEOUT_SECONDS)
            connection.connect(socket_path)
            connection.sendall(request + b"\n")
            raw_response = _read_line(connection)
    except OSError as exc:
        raise BrokerClientError("task capability broker is unavailable") from exc

    try:
        response = json.loads(raw_response)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrokerClientError("task capability broker returned an invalid response") from exc
    if not isinstance(response, dict):
        raise BrokerClientError("task capability broker returned an invalid response")
    if response.get("ok") is not True:
        error = response.get("error", "capability request failed")
        raise BrokerClientError(str(error))
    result = response.get("result")
    if not isinstance(result, dict):
        raise BrokerClientError("task capability broker returned an invalid result")
    return result


_MEMORY_TOOLS: list[dict[str, object]] = [
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

_IMAGE_TOOLS: list[dict[str, object]] = [
    {
        "name": "mistral_generate_image",
        "description": (
            "Generate one image through the configured Mistral image service. "
            "Use only when the user explicitly asks for an image. The result is "
            "saved directly to this task's Telegram artifact output; do not copy "
            "it, inspect host paths, or use the built-in generate_image tool."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Complete visual description for the requested image",
                }
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    }
]

_CAPABILITY_TOOLS = [*_MEMORY_TOOLS, *_IMAGE_TOOLS]


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
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "isError": is_error,
    }


def _capability_tool_result(params: Mapping[str, object]) -> dict[str, object]:
    name = params.get("name")
    arguments = params.get("arguments", {})
    if not isinstance(arguments, Mapping):
        return _tool_result({"error": "tool arguments must be an object"}, is_error=True)
    actions = {
        "save_memory": "memory.save",
        "list_memory": "memory.list",
        "delete_memory": "memory.delete",
        "mistral_generate_image": "image.generate",
    }
    action = actions.get(name) if isinstance(name, str) else None
    if action is None:
        return _tool_result({"error": f"unknown memory tool: {name}"}, is_error=True)
    try:
        return _tool_result(broker_call(action, arguments))
    except BrokerClientError as exc:
        return _tool_result({"error": str(exc)}, is_error=True)


def handle_memory_request(request: Mapping[str, object]) -> dict[str, object] | None:
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
            else _DEFAULT_PROTOCOL_VERSION
        )
        return _success(
            request_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": _SERVER_NAME, "version": _SERVER_VERSION},
            },
        )
    if method == "tools/list":
        return _success(request_id, {"tools": _CAPABILITY_TOOLS})
    if method == "tools/call":
        return _success(request_id, _capability_tool_result(params))
    if method == "ping":
        return _success(request_id, {})
    if request_id is None:
        return None
    return _error(request_id, -32601, f"method not found: {method}")


def run_memory_mcp() -> int:
    """Serve the native memory MCP protocol over stdin/stdout."""
    for raw_line in sys.stdin:
        try:
            request: Any = json.loads(raw_line)
        except json.JSONDecodeError:
            response = _error(None, -32700, "parse error")
        else:
            if not isinstance(request, Mapping):
                response = _error(None, -32600, "request must be an object")
            else:
                response = handle_memory_request(request)
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


def _ssh_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Task-scoped SSH capability client")
    subparsers = parser.add_subparsers(dest="action", required=True)
    exec_parser = subparsers.add_parser("exec", help="Run an approved command")
    exec_parser.add_argument("env_name", help="Configured environment name")
    exec_parser.add_argument("command", help="Command to execute remotely")
    exec_parser.add_argument("--cwd", help="Remote working directory")
    subparsers.add_parser("list", help="List configured environment names")
    subparsers.add_parser("pubkey", help="Show the bot public SSH key")
    return parser


def run_ssh(argv: Sequence[str] | None = None) -> int:
    args = _ssh_parser().parse_args(argv)
    try:
        if args.action == "list":
            result = broker_call("ssh.list", {})
            environments = result.get("environments", [])
            print("Available environments:")
            for environment in environments if isinstance(environments, list) else []:
                if isinstance(environment, Mapping) and isinstance(environment.get("name"), str):
                    print(f"- {environment['name']}")
            return 0
        if args.action == "pubkey":
            result = broker_call("ssh.pubkey", {})
            print(result.get("public_key", ""))
            return 0
        result = broker_call(
            "ssh.exec",
            {"environment": args.env_name, "command": args.command, "cwd": args.cwd},
        )
    except BrokerClientError as exc:
        print(f"SSH capability denied: {exc}", file=sys.stderr)
        return 2

    exit_code = result.get("exit_code", -1)
    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    print(f"--- SSH EXIT CODE: {exit_code} ---")
    if stdout:
        print("--- STDOUT ---")
        print(stdout)
    if stderr:
        print("--- STDERR ---")
        print(stderr)
    return 0 if exit_code == 0 else 1


def main(argv: Sequence[str] | None = None) -> int:
    values = list(argv if argv is not None else sys.argv[1:])
    if values and values[0] == "mcp-memory":
        return run_memory_mcp()
    return run_ssh(values)


if __name__ == "__main__":
    raise SystemExit(main())
