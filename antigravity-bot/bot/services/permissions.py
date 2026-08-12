"""Human-in-the-Loop Interceptor & Process Signaling for Dangerous Commands."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import uuid
from dataclasses import dataclass
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

# Commands strictly requiring user confirmation via Telegram buttons
_STRICT_DANGEROUS_PATTERNS = (
    "rm ", "git checkout", "git reset", "systemctl", "mv ", ">", ">>"
)

# Blocking server commands that block stdout indefinitely in headless execution
_BLOCKING_SERVER_PATTERNS = (
    "http.server", "http-server", "npm start", "yarn start", "vite", "ng serve", "live-server", "python -m http", "python3 -m http"
)


@dataclass
class PermissionRequest:
    req_id: str
    chat_id: int
    tool_name: str
    details: str
    event: asyncio.Event
    approved: bool = False
    message_id: int | None = None
    proc: Any | None = None  # asyncio.subprocess.Process
    thread_id: int | None = None  # forum topic thread ID


class PermissionHandler:
    """Manages Telegram Inline confirmation and Linux process SIGSTOP/SIGCONT signaling for dangerous commands."""

    def __init__(self) -> None:
        self._requests: dict[str, PermissionRequest] = {}
        self._lock = asyncio.Lock()

    def is_blocking_server(self, tool_name: str, parameters: dict[str, Any]) -> bool:
        """Check if command tries to launch an indefinite blocking web server."""
        if tool_name == "run_command":
            cmd = str(parameters.get("CommandLine", "")).strip()
            if any(p in cmd for p in _BLOCKING_SERVER_PATTERNS) and not cmd.endswith("&"):
                return True
        return False

    def is_strictly_dangerous(self, tool_name: str, parameters: dict[str, Any]) -> bool:
        """Check if tool execution matches strictly dangerous commands requiring Telegram prompt."""
        if tool_name == "run_command":
            cmd = str(parameters.get("CommandLine", "")).strip()
            if any(pattern in cmd for pattern in _STRICT_DANGEROUS_PATTERNS):
                return True
            return False

        return False

    async def handle_permission(
        self,
        bot: Bot,
        chat_id: int,
        tool_name: str,
        parameters: dict[str, Any],
        proc: Any | None = None,
        thread_id: int | None = None,
        force_approval: bool = False,
        timeout_seconds: float | None = None,
    ) -> bool:
        """Handle tool permission. Auto-approves routine tools immediately.
        
        Hard-denies blocking foreground servers to prevent hangs.
        Freezes dangerous processes via SIGSTOP until approved (SIGCONT) or denied (proc.kill()).
        """
        # Immediately deny blocking foreground web servers to prevent process hanging
        if self.is_blocking_server(tool_name, parameters):
            logger.info("Auto-denying blocking web server command to prevent hanging: %s", parameters)
            return False

        if not force_approval and not self.is_strictly_dangerous(tool_name, parameters):
            return True

        req_id = str(uuid.uuid4())[:8]
        details = self._format_details(tool_name, parameters)

        event = asyncio.Event()
        req = PermissionRequest(
            req_id=req_id,
            chat_id=chat_id,
            tool_name=tool_name,
            details=details,
            event=event,
            proc=proc,
            thread_id=thread_id,
        )

        async with self._lock:
            self._requests[req_id] = req

        # 1. Freeze process via SIGSTOP if running on Linux
        if proc and hasattr(proc, "pid") and proc.pid:
            try:
                os.kill(proc.pid, signal.SIGSTOP)
                logger.info("Process %d frozen (SIGSTOP) for permission %s", proc.pid, req_id)
            except Exception as e:
                logger.warning("Failed to SIGSTOP process %s: %s", proc.pid, e)

        # 2. Send Telegram confirmation message
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Разрешить", callback_data=f"perm:allow:{req_id}"),
                    InlineKeyboardButton(text="❌ Отклонить", callback_data=f"perm:deny:{req_id}"),
                ]
            ]
        )

        import html
        escaped_details = html.escape(details)
        text = (
            "⚠️ <b>Опасное действие! Требуется подтверждение:</b>\n\n"
            f"<b>Инструмент:</b> <code>{tool_name}</code>\n"
            f"<b>Команда:</b>\n<pre><code>{escaped_details}</code></pre>"
        )

        try:
            msg = await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb, message_thread_id=thread_id)
            req.message_id = msg.message_id
        except Exception:
            logger.exception("Failed to send permission message")
            async with self._lock:
                self._requests.pop(req_id, None)
            if proc and hasattr(proc, "pid") and proc.pid:
                try:
                    os.kill(proc.pid, signal.SIGCONT)
                except Exception:
                    pass
            return False

        # 3. Wait for user decision via CallbackQuery. Capability operations
        # such as SSH use a bounded wait so a killed sandbox client cannot leave
        # a permanent approval request behind.
        try:
            if timeout_seconds is None:
                await event.wait()
            else:
                await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
        except TimeoutError:
            async with self._lock:
                self._requests.pop(req_id, None)
            if req.message_id:
                try:
                    await bot.edit_message_text(
                        f"⌛ <b>Время подтверждения истекло</b> (<code>{req.tool_name}</code>)",
                        chat_id=req.chat_id,
                        message_id=req.message_id,
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            return False

        async with self._lock:
            self._requests.pop(req_id, None)

        return req.approved

    async def handle_callback(self, req_id: str, allow: bool, bot: Bot) -> str:
        """Called when user clicks [Разрешить] or [Отклонить] inline button."""
        async with self._lock:
            req = self._requests.get(req_id)

        if not req:
            return "Запрос устарел или не найден."

        req.approved = allow
        proc = req.proc

        if allow:
            # Unfreeze process via SIGCONT
            if proc and hasattr(proc, "pid") and proc.pid:
                try:
                    os.kill(proc.pid, signal.SIGCONT)
                    logger.info("Process %d resumed (SIGCONT) for permission %s", proc.pid, req_id)
                except Exception as e:
                    logger.warning("Failed to SIGCONT process %s: %s", proc.pid, e)

            if req.message_id:
                try:
                    await bot.edit_message_text(
                        f"✅ <b>Действие разрешено</b> (<code>{req.tool_name}</code>)",
                        chat_id=req.chat_id,
                        message_id=req.message_id,
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            req.event.set()
            return "Разрешено."
        else:
            # Kill process on denial
            if proc:
                try:
                    proc.kill()
                    logger.info("Process killed on permission denial %s", req_id)
                except Exception as e:
                    logger.warning("Failed to kill process %s: %s", req_id, e)

            if req.message_id:
                try:
                    await bot.edit_message_text(
                        f"❌ <b>Действие отклонено пользователем</b> (<code>{req.tool_name}</code>)",
                        chat_id=req.chat_id,
                        message_id=req.message_id,
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            req.event.set()
            return "Отклонено."

    def _format_details(self, tool_name: str, parameters: dict[str, Any]) -> str:
        if tool_name == "run_command":
            return str(parameters.get("CommandLine", ""))
        if tool_name == "ssh_exec":
            environment = parameters.get("environment", "")
            command = parameters.get("command", "")
            cwd = parameters.get("cwd")
            cwd_line = f"\nКаталог: {cwd}" if cwd else ""
            return f"SSH environment: {environment}\nКоманда: {command}{cwd_line}"
        if tool_name in ("write_to_file", "replace_file_content", "multi_replace_file_content"):
            return f"Файл: {parameters.get('TargetFile', parameters.get('path', ''))}"
        return str(parameters)[:300]


permission_handler = PermissionHandler()
