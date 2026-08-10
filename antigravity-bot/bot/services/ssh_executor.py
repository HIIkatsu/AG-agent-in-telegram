"""SSH execution manager using asyncssh."""

import asyncio
import os
import subprocess
import logging
import asyncssh

from bot.db import db

logger = logging.getLogger(__name__)

KEY_PATH = "/opt/antigravity-bot/.ssh/bot_ed25519"

def ensure_key_exists() -> str:
    """Ensure the default SSH key exists, generate if not. Return path to key."""
    if not os.path.exists(KEY_PATH):
        os.makedirs(os.path.dirname(KEY_PATH), exist_ok=True)
        logger.info(f"Generating new SSH key at {KEY_PATH}")
        # Run ssh-keygen synchronously
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", KEY_PATH, "-q"],
            check=True
        )
        logger.info("SSH key generated successfully.")
    return KEY_PATH

async def get_public_key() -> str:
    """Return the content of the public key."""
    key_path = ensure_key_exists()
    pub_path = f"{key_path}.pub"
    if os.path.exists(pub_path):
        with open(pub_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return "Error: Public key not found."

async def execute_command(env_name: str, command: str, cwd: str | None = None) -> tuple[int, str, str]:
    """Execute command on a remote environment via SSH."""
    env = await db.get_environment_by_name(env_name)
    if not env:
        return -1, "", f"Environment '{env_name}' not found in database."

    ensure_key_exists()
    
    ssh_key_path = env["ssh_key_path"] if env.get("ssh_key_path") else KEY_PATH
    if not os.path.exists(ssh_key_path):
        return -1, "", f"SSH key not found at {ssh_key_path}"

    try:
        async with asyncssh.connect(
            host=env["host"],
            port=env.get("port", 22),
            username=env["username"],
            client_keys=[ssh_key_path],
            known_hosts=None  # Disable known_hosts check for simplicity in internal mesh
        ) as conn:
            
            exec_cmd = command
            if cwd:
                exec_cmd = f"cd {cwd} && {command}"
                
            result = await conn.run(exec_cmd, check=False)
            return result.exit_status, result.stdout, result.stderr

    except Exception as e:
        logger.error(f"SSH Execution error on {env_name}: {e}")
        return -1, "", str(e)
