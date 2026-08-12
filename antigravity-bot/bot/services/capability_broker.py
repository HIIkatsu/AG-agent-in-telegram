"""Per-task broker for memory and approved SSH capabilities.

The AGY worker has no direct path to the bot database or SSH private keys.  It
can only reach this short-lived Unix socket, authenticated by a random token
created for the task.  SSH execution additionally requires a Telegram click.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot

from bot.config import settings
from bot.services import memory_mcp
from bot.services.permissions import permission_handler
from bot.services.ssh_executor import execute_command, get_public_key

logger = logging.getLogger(__name__)

_MAX_REQUEST_BYTES = 16 * 1024
_MAX_COMMAND_CHARS = 8_000
_MAX_CWD_CHARS = 2_000
_MAX_OUTPUT_CHARS = 24_000


class CapabilityBrokerError(RuntimeError):
    """A sandbox capability request is malformed, unauthorised or denied."""


@dataclass(frozen=True)
class CapabilityEndpoint:
    """The only per-task capability data passed into the sandbox."""

    mount_dir: Path
    token: str


SshExecutor = Callable[[str, str, str | None], Awaitable[tuple[int, str, str]]]


def _shorten(value: str) -> str:
    if len(value) <= _MAX_OUTPUT_CHARS:
        return value
    return value[:_MAX_OUTPUT_CHARS] + "\n… output truncated by capability broker …"


def _required_string(
    arguments: Mapping[str, object],
    name: str,
    *,
    maximum: int,
) -> str:
    value = arguments.get(name)
    if not isinstance(value, str):
        raise CapabilityBrokerError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise CapabilityBrokerError(f"{name} cannot be empty")
    if "\x00" in normalized or len(normalized) > maximum:
        raise CapabilityBrokerError(f"{name} is invalid")
    return normalized


def _optional_cwd(arguments: Mapping[str, object]) -> str | None:
    value = arguments.get("cwd")
    if value is None:
        return None
    if not isinstance(value, str):
        raise CapabilityBrokerError("cwd must be a string")
    normalized = value.strip()
    if not normalized:
        return None
    if "\x00" in normalized or len(normalized) > _MAX_CWD_CHARS:
        raise CapabilityBrokerError("cwd is invalid")
    return normalized


class TaskCapabilityBroker:
    """Expose a tiny, task-bound capability surface to one sandbox worker."""

    def __init__(
        self,
        *,
        bot: Bot,
        chat_id: int,
        thread_id: int | None,
        task_id: int | None,
        worker_uid: int,
        worker_gid: int,
        ssh_executor: SshExecutor = execute_command,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._thread_id = thread_id
        self._task_id = task_id
        self._worker_uid = worker_uid
        self._worker_gid = worker_gid
        self._ssh_executor = ssh_executor
        self._token = secrets.token_urlsafe(32)
        self._mount_dir: Path | None = None
        self._socket_path: Path | None = None
        self._server: asyncio.AbstractServer | None = None

    @property
    def endpoint(self) -> CapabilityEndpoint:
        if self._mount_dir is None:
            raise CapabilityBrokerError("Capability broker has not been started")
        return CapabilityEndpoint(mount_dir=self._mount_dir, token=self._token)

    async def start(self) -> CapabilityEndpoint:
        """Create one unlistable, token-protected socket endpoint for a worker."""
        if self._server is not None:
            return self.endpoint
        root = Path(settings.agy_capability_socket_dir).expanduser().resolve()
        try:
            # Bubblewrap is launched by the trusted root-owned bot service.
            # Keep the parent private: the non-root AGY process receives only
            # its already-mounted child endpoint, never this host directory.
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(root, 0o700)
            mount_dir = Path(tempfile.mkdtemp(prefix="task-capability-", dir=root))
            # The sandbox bind-mounts this child directly. The capability token
            # remains mandatory for every request; the random path is a second
            # containment layer and the worker never sees the host parent.
            os.chmod(mount_dir, 0o711)
            socket_path = mount_dir / "broker.sock"
            server = await asyncio.start_unix_server(self._handle_client, path=socket_path)
            os.chown(socket_path, self._worker_uid, self._worker_gid)
            os.chmod(socket_path, 0o666)
        except (OSError, PermissionError) as exc:
            if "mount_dir" in locals():
                shutil.rmtree(mount_dir, ignore_errors=True)
            raise CapabilityBrokerError(
                f"Cannot create task capability broker: {exc}"
            ) from exc
        self._mount_dir = mount_dir
        self._socket_path = socket_path
        self._server = server
        return self.endpoint

    async def close(self) -> None:
        """Remove the socket and its token-bearing task endpoint."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        if self._socket_path is not None:
            try:
                self._socket_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Cannot remove task capability socket %s", self._socket_path)
        if self._mount_dir is not None:
            shutil.rmtree(self._mount_dir, ignore_errors=True)
        self._socket_path = None
        self._mount_dir = None

    async def __aenter__(self) -> TaskCapabilityBroker:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=15)
            if not raw or len(raw) > _MAX_REQUEST_BYTES:
                raise CapabilityBrokerError("capability request is invalid")
            try:
                request = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CapabilityBrokerError("capability request is invalid") from exc
            if not isinstance(request, dict):
                raise CapabilityBrokerError("capability request is invalid")
            result = await self._dispatch(request)
            response: dict[str, object] = {"ok": True, "result": result}
        except CapabilityBrokerError as exc:
            response = {"ok": False, "error": str(exc)}
        except Exception:
            logger.exception("Task capability broker request failed")
            response = {"ok": False, "error": "capability request failed"}
        try:
            writer.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _dispatch(self, request: Mapping[str, object]) -> dict[str, object]:
        token = request.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token, self._token):
            raise CapabilityBrokerError("capability authentication failed")
        action = request.get("action")
        arguments = request.get("arguments", {})
        if not isinstance(action, str) or not isinstance(arguments, Mapping):
            raise CapabilityBrokerError("capability request is invalid")
        if action == "memory.save":
            return await self._memory_save(arguments)
        if action == "memory.list":
            return await self._memory_list(arguments)
        if action == "memory.delete":
            return await self._memory_delete(arguments)
        if action == "ssh.list":
            return await self._ssh_list()
        if action == "ssh.pubkey":
            return {"public_key": await get_public_key()}
        if action == "ssh.exec":
            return await self._ssh_exec(arguments)
        raise CapabilityBrokerError("capability is not available")

    async def _memory_save(self, arguments: Mapping[str, object]) -> dict[str, object]:
        try:
            result = await asyncio.to_thread(
                memory_mcp.save_memory,
                arguments.get("text", ""),
                arguments.get("scope", "global"),
                db_path=settings.db_path,
                thread_id=self._thread_id,
            )
        except memory_mcp.MemoryToolError as exc:
            raise CapabilityBrokerError(str(exc)) from exc
        await self._log_event("memory", "Memory fact saved through capability broker")
        return result

    async def _memory_list(self, arguments: Mapping[str, object]) -> dict[str, object]:
        try:
            return await asyncio.to_thread(
                memory_mcp.list_memory,
                arguments.get("scope", "global"),
                db_path=settings.db_path,
                thread_id=self._thread_id,
            )
        except memory_mcp.MemoryToolError as exc:
            raise CapabilityBrokerError(str(exc)) from exc

    async def _memory_delete(self, arguments: Mapping[str, object]) -> dict[str, object]:
        try:
            result = await asyncio.to_thread(
                memory_mcp.delete_memory,
                arguments.get("id"),
                arguments.get("scope", "global"),
                db_path=settings.db_path,
                thread_id=self._thread_id,
            )
        except memory_mcp.MemoryToolError as exc:
            raise CapabilityBrokerError(str(exc)) from exc
        if result.get("deleted"):
            await self._log_event("memory", "Memory fact deleted through capability broker")
        return result

    async def _ssh_list(self) -> dict[str, object]:
        from bot.db import db

        environments = await db.get_all_environments()
        return {"environments": [{"name": row["name"]} for row in environments]}

    async def _ssh_exec(self, arguments: Mapping[str, object]) -> dict[str, object]:
        environment = _required_string(arguments, "environment", maximum=128)
        command = _required_string(arguments, "command", maximum=_MAX_COMMAND_CHARS)
        cwd = _optional_cwd(arguments)
        approved = await permission_handler.handle_permission(
            bot=self._bot,
            chat_id=self._chat_id,
            tool_name="ssh_exec",
            parameters={"environment": environment, "command": command, "cwd": cwd},
            thread_id=self._thread_id,
            force_approval=True,
            timeout_seconds=settings.ssh_approval_timeout_seconds,
        )
        if not approved:
            await self._log_event("ssh", f"SSH command denied for environment {environment}")
            raise CapabilityBrokerError("SSH command was not approved")
        await self._log_event("ssh", f"SSH command approved for environment {environment}")
        exit_code, stdout, stderr = await self._ssh_executor(environment, command, cwd)
        return {
            "exit_code": exit_code,
            "stdout": _shorten(stdout),
            "stderr": _shorten(stderr),
        }

    async def _log_event(self, level: str, message: str) -> None:
        if not self._task_id:
            return
        try:
            from bot.services.task_service import log_task_event

            await log_task_event(self._task_id, level, message)
        except Exception:
            logger.exception("Cannot log capability event for task %s", self._task_id)
