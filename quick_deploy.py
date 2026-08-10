"""Deploy Forum Topics architecture to VPS — recursive file list."""

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

def upload_dir(local_dir, remote_dir):
    try:
        sftp.mkdir(remote_dir)
    except IOError:
        pass
    for item in os.listdir(local_dir):
        if item in ('.git', '__pycache__', 'venv', 'node_modules', '.idea', '.vscode', '.env.example'):
            continue
        local_path = os.path.join(local_dir, item)
        remote_path = remote_dir + "/" + item
        if os.path.isfile(local_path):
            sftp.put(local_path, remote_path)
            print(f"  [PUT] {remote_path}")
        elif os.path.isdir(local_path):
            upload_dir(local_path, remote_path)

print("Starting recursive upload...")
upload_dir(base, "/opt/antigravity-bot")

sftp.close()
print(f"\nUpload complete.")

# Clear pyc cache, run migration + restart service
print("Running migrations and restarting service...")
stdin, stdout, stderr = client.exec_command(
    "find /opt/antigravity-bot -name '*.pyc' -delete && "
    "/opt/antigravity-bot/venv/bin/python /opt/antigravity-bot/migrate_phase3.py && "
    "/opt/antigravity-bot/venv/bin/python /opt/antigravity-bot/migrate_phase4.py && "
    "/opt/antigravity-bot/venv/bin/python /opt/antigravity-bot/migrate_phase4_1.py && "
    "/opt/antigravity-bot/venv/bin/python /opt/antigravity-bot/migrate_memory.py && "
    "/opt/antigravity-bot/venv/bin/pip install -r /opt/antigravity-bot/requirements.txt && "
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
