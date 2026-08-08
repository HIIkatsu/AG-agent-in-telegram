"""Deploy antigravity-bot to VPS via paramiko SFTP + SSH."""

import io
import os
import stat
import sys
import time

import paramiko

# Fix Windows console encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

HOST = "87.58.205.235"
PORT = 22
USER = "root"
PASS = "sJjuXb4MB3fz3"
REMOTE_DIR = "/opt/antigravity-bot"

LOCAL_DIR = os.path.join(os.path.dirname(__file__), "antigravity-bot")

SYSTEMD_UNIT = f"""\
[Unit]
Description=Antigravity Telegram Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory={REMOTE_DIR}
ExecStart={REMOTE_DIR}/venv/bin/python -m bot
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
"""


def ssh_exec(client: paramiko.SSHClient, cmd: str, timeout: int = 120) -> str:
    print(f"  [SSH] {cmd}")
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    rc = stdout.channel.recv_exit_status()
    if out.strip():
        print(f"  [OUT] {out.strip()[:500]}")
    if err.strip():
        print(f"  [ERR] {err.strip()[:500]}")
    if rc != 0:
        print(f"  [RC]  {rc}")
    return out


def upload_tree(sftp: paramiko.SFTPClient, local: str, remote: str) -> int:
    """Recursively upload a directory tree. Returns file count."""
    count = 0
    for root, dirs, files in os.walk(local):
        # Skip __pycache__, .git, venv, etc.
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "venv", ".mypy_cache")]
        rel = os.path.relpath(root, local).replace("\\", "/")
        rdir = f"{remote}/{rel}" if rel != "." else remote
        try:
            sftp.stat(rdir)
        except FileNotFoundError:
            sftp.mkdir(rdir)
            print(f"  [MKDIR] {rdir}")
        for fname in files:
            local_path = os.path.join(root, fname)
            remote_path = f"{rdir}/{fname}"
            sftp.put(local_path, remote_path)
            count += 1
            print(f"  [PUT]   {remote_path}")
    return count


def main() -> None:
    print("=" * 60)
    print("  Antigravity Bot — Deploy to VPS")
    print("=" * 60)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    print(f"\n[1/7] Connecting to {HOST}:{PORT} …")
    client.connect(HOST, port=PORT, username=USER, password=PASS, timeout=15)
    print("  Connected ✓")

    # ── Step 2: System packages ──────────────────────────────────────
    print("\n[2/7] Installing system packages (ffmpeg, python3-pip, python3-venv) …")
    ssh_exec(client, "apt-get update -qq")
    ssh_exec(client, "apt-get install -y -qq ffmpeg python3-pip python3-venv", timeout=180)

    # ── Step 3: Create directory structure ────────────────────────────
    print("\n[3/7] Creating project directories …")
    ssh_exec(client, f"mkdir -p {REMOTE_DIR}/{{data,logs}}")

    # ── Step 4: Upload files ─────────────────────────────────────────
    print("\n[4/7] Uploading project files …")
    sftp = client.open_sftp()
    n = upload_tree(sftp, LOCAL_DIR, REMOTE_DIR)
    sftp.close()
    print(f"  Uploaded {n} files ✓")

    # ── Step 5: Create venv & install deps ───────────────────────────
    print("\n[5/7] Setting up Python venv & installing dependencies …")
    ssh_exec(client, f"python3 -m venv {REMOTE_DIR}/venv")
    ssh_exec(
        client,
        f"{REMOTE_DIR}/venv/bin/pip install --upgrade pip setuptools wheel -q",
        timeout=120,
    )
    ssh_exec(
        client,
        f"{REMOTE_DIR}/venv/bin/pip install -r {REMOTE_DIR}/requirements.txt -q",
        timeout=300,
    )

    # ── Step 6: Systemd service ──────────────────────────────────────
    print("\n[6/7] Configuring systemd service …")
    # Write unit file via sftp
    sftp = client.open_sftp()
    unit_path = "/etc/systemd/system/antigravity-bot.service"
    with sftp.open(unit_path, "w") as f:
        f.write(SYSTEMD_UNIT)
    sftp.close()
    print(f"  Written {unit_path}")

    ssh_exec(client, "systemctl daemon-reload")
    ssh_exec(client, "systemctl enable antigravity-bot")
    ssh_exec(client, "systemctl restart antigravity-bot")
    print("  Service started ✓")

    # ── Step 7: Verify ───────────────────────────────────────────────
    print("\n[7/7] Verifying …")
    time.sleep(3)
    status = ssh_exec(client, "systemctl status antigravity-bot --no-pager -l")
    logs = ssh_exec(client, "journalctl -u antigravity-bot -n 30 --no-pager")

    client.close()

    print("\n" + "=" * 60)
    print("  Deploy complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
