"""Deploy Forum Topics architecture to VPS — full file list."""

import io
import os
import sys
import paramiko

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("87.58.205.235", port=22, username="root", password="sJjuXb4MB3fz3", timeout=15)
sftp = client.open_sftp()

base = r"c:\Users\hiika\Desktop\Gemini_bot\antigravity-bot"
files = [
    # Config + env
    (r".env", "/opt/antigravity-bot/.env"),
    (r"config.json", "/opt/antigravity-bot/config.json"),
    # Core
    (r"bot\config.py", "/opt/antigravity-bot/bot/config.py"),
    (r"bot\db.py", "/opt/antigravity-bot/bot/db.py"),
    (r"bot\middleware.py", "/opt/antigravity-bot/bot/middleware.py"),
    (r"bot\__main__.py", "/opt/antigravity-bot/bot/__main__.py"),
    # Handlers
    (r"bot\handlers\start.py", "/opt/antigravity-bot/bot/handlers/start.py"),
    (r"bot\handlers\chats.py", "/opt/antigravity-bot/bot/handlers/chats.py"),
    (r"bot\handlers\callbacks.py", "/opt/antigravity-bot/bot/handlers/callbacks.py"),
    (r"bot\handlers\message.py", "/opt/antigravity-bot/bot/handlers/message.py"),
    # Services
    (r"bot\services\agy_runner.py", "/opt/antigravity-bot/bot/services/agy_runner.py"),
    (r"bot\services\artifacts.py", "/opt/antigravity-bot/bot/services/artifacts.py"),
    (r"bot\services\permissions.py", "/opt/antigravity-bot/bot/services/permissions.py"),
    (r"bot\services\tracker.py", "/opt/antigravity-bot/bot/services/tracker.py"),
    (r"bot\services\git_manager.py", "/opt/antigravity-bot/bot/services/git_manager.py"),
    (r"bot\services\diff_viewer.py", "/opt/antigravity-bot/bot/services/diff_viewer.py"),
    (r"bot\services\backups.py", "/opt/antigravity-bot/bot/services/backups.py"),
    (r"bot\services\streaming.py", "/opt/antigravity-bot/bot/services/streaming.py"),
    # Utils
    (r"bot\utils\keyboards.py", "/opt/antigravity-bot/bot/utils/keyboards.py"),
    (r"bot\utils\sanitizer.py", "/opt/antigravity-bot/bot/utils/sanitizer.py"),
    (r"bot\utils\formatting.py", "/opt/antigravity-bot/bot/utils/formatting.py"),
]

for local_rel, remote in files:
    local_path = os.path.join(base, local_rel)
    sftp.put(local_path, remote)
    print(f"  [PUT] {remote}")

sftp.close()
print(f"\nUploaded {len(files)} files")

# Clear pyc cache + restart service
stdin, stdout, stderr = client.exec_command(
    "find /opt/antigravity-bot -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; "
    "systemctl restart antigravity-bot"
)
stdout.read()
err = stderr.read().decode(errors="replace")
print("Restarted service ✓")

# Quick status check
import time
time.sleep(3)
stdin, stdout, stderr = client.exec_command(
    "systemctl is-active antigravity-bot; journalctl -u antigravity-bot -n 10 --no-pager"
)
print(stdout.read().decode(errors="replace"))
client.close()
