"""Explicit remote power-off operations over configured SSH environments."""

from __future__ import annotations

import logging

from bot.services.ssh_executor import execute_command

logger = logging.getLogger(__name__)


async def soft_shutdown(env_name: str) -> tuple[int, str, str]:
    """Shut down a configured remote machine after an explicit user request."""
    logger.info("Initiating explicit soft shutdown for %s", env_name)
    command = "shutdown /s /t 0 || sudo poweroff || poweroff"
    return await execute_command(env_name, command)
