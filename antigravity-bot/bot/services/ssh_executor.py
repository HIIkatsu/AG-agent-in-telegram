"""SSH execution manager using asyncssh."""

import asyncio
import logging
import os
import shlex
import subprocess
from pathlib import Path

import asyncssh

from bot.config import settings
from bot.db import db

logger = logging.getLogger(__name__)


def _key_path() -> Path:
    return Path(settings.ssh_key_path).expanduser().resolve()


def _known_hosts_path() -> Path:
    return Path(settings.ssh_known_hosts_path).expanduser().resolve()

def ensure_key_exists() -> str:
    """Ensure the default SSH key exists, generate if not. Return path to key."""
    key_path = _key_path()
    if not key_path.exists():
        key_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(key_path.parent, 0o700)
        logger.info("Generating new SSH key at %s", key_path)
        # Run ssh-keygen synchronously
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key_path), "-q"],
            check=True,
        )
        os.chmod(key_path, 0o600)
        logger.info("SSH key generated successfully.")
    return str(key_path)

async def get_public_key() -> str:
    """Return the content of the public key."""
    key_path = ensure_key_exists()
    pub_path = f"{key_path}.pub"
    if os.path.exists(pub_path):
        with open(pub_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return "Error: Public key not found."

async def execute_command(env_name: str, command: str, cwd: str | None = None) -> tuple[int, str, str]:
    """Execute an approved command on a configured remote environment via SSH."""
    env = await db.get_environment_by_name(env_name)
    if not env:
        return -1, "", f"Environment '{env_name}' not found in database."

    ensure_key_exists()
    ssh_key_path = Path(env["ssh_key_path"] or _key_path()).expanduser()
    known_hosts_path = _known_hosts_path()
    if not ssh_key_path.is_file():
        return -1, "", f"SSH key not found at {ssh_key_path}"
    if not known_hosts_path.is_file():
        return -1, "", (
            "SSH known_hosts file is missing. Add and verify the server host key "
            f"at {known_hosts_path}."
        )

    try:
        async def _connect_and_run():
            async with asyncssh.connect(
                host=env["host"],
                port=env.get("port", 22),
                username=env["username"],
                client_keys=[str(ssh_key_path)],
                known_hosts=str(known_hosts_path),
            ) as conn:
                exec_cmd = command
                if cwd:
                    exec_cmd = f"cd -- {shlex.quote(cwd)} && {command}"
                result = await conn.run(exec_cmd, check=False)
                return result.exit_status, result.stdout, result.stderr

        return await asyncio.wait_for(
            _connect_and_run(), timeout=settings.ssh_command_timeout_seconds
        )

    except asyncio.TimeoutError:
        logger.error("SSH connection to %s timed out.", env_name)
        return -1, "", (
            f"Timeout: SSH command on {env_name} exceeded "
            f"{settings.ssh_command_timeout_seconds} seconds."
        )
    except Exception as exc:
        logger.error("SSH execution error on %s: %s", env_name, exc)
        return -1, "", str(exc)
