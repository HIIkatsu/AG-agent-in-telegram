# Antigravity in Telegram 🚀

Telegram bot integration for **Antigravity CLI** agent execution, featuring multi-thread conversation persistence, voice message transcription, isolated code-task workspaces, and dynamic task tracking.

## Features

- 🤖 **Antigravity CLI Integration**: Run agent sessions via native `agy` CLI with stream-json output.
- 💬 **Forum Topics & Thread Isolation**: Each Telegram thread maps to an isolated workspace with project session persistence.
- 🎤 **Voice Support**: Instant voice message transcription via cloud STT (Groq with Wit.ai fallback).
- 🔄 **Task-scoped Git Safety**: Every code task runs in a private repository snapshot. Accept applies only that task's checked patch; Discard removes only its isolated workspace and never resets the mounted project.
- 🛡️ **Runtime Rules**: Combines tracked `INSTRUCTIONS.md` policy with optional private `INSTRUCTIONS.local.md` context without modifying mounted projects.
- 🧩 **Global Agent Skills**: Registers every bundled skill in AGY's user-level skill directory while leaving each mounted project's `.agents` directory untouched.
- 🧠 **Two-layer Memory**: Keeps global user facts and per-project notes separate; AGY uses native MCP tools to manage both without shell access to SQLite.
- 🔒 **Sandboxed Worker & Capability Broker**: Runs every AGY task in a separate Bubblewrap namespace with no bot `.env`, SQLite database or SSH key. Memory and remote access are exposed only through narrow, task-scoped capabilities.

## Project Structure

```
.
├── antigravity-bot/
│   ├── bot/
│   │   ├── handlers/       # Aiogram handlers (message, callbacks, start, chats)
│   │   ├── services/       # agy runner, git manager, voice, tracker
│   │   ├── utils/          # Formatting and keyboard helpers
│   │   ├── config.py       # Configuration settings
│   │   └── db.py           # SQLite session management
│   ├── .env.example
│   ├── INSTRUCTIONS.md
│   ├── INSTRUCTIONS.local.example.md
│   └── requirements.txt
└── README.md
```

## Setup

1. Copy `.env.example` to `.env` and fill in your Telegram Bot credentials.
2. Optionally copy `antigravity-bot/INSTRUCTIONS.local.example.md` to `antigravity-bot/INSTRUCTIONS.local.md` and add private personal or infrastructure context. The local file is ignored by Git; never put secrets in it. Restart the bot after changing it so a new instruction snapshot and SHA-256 are loaded.
3. Keep `AGY_GLOBAL_SKILLS_DIR` at AGY CLI's user-level default unless the service runs under a custom home directory. On startup the bot creates only manifest-managed skill copies there and refuses to overwrite user-owned or locally modified skills with the same names.
4. In normal production mode, the bot generates a per-task memory MCP configuration inside the worker sandbox. It does not read or modify the service account's `AGY_MCP_CONFIG_PATH`. That path is retained only for the explicit local `AGY_ALLOW_UNSANDBOXED_DEV=true` fallback.
5. Keep `TASK_WORKSPACES_DIR` outside mounted repositories. Tracked and non-ignored files are copied there for each code task; ignored secrets such as `.env` are excluded.
6. Install dependencies: `pip install -r antigravity-bot/requirements.txt`.

If you intentionally use the local unsandboxed development fallback and its MCP
config is invalid, the bot remains available but that fallback's native memory
tools are disabled. Do not delete or paste the config into chat. After updating
to a version that includes the repair command, run this explicit local operation
from the repository root:

    PYTHONPATH=antigravity-bot python -m bot.services.memory_mcp_config repair-invalid

It creates an owner-only (0600) backup of the unreadable original and writes a
minimal valid config containing only the bot's memory MCP server. Restart the bot
afterwards.

## Production worker sandbox and SSH setup

AGY tasks now fail closed unless a dedicated Bubblewrap worker is ready. This is
intentional: a prompt-injected repository or web page must not be able to read
the bot token, SQLite database, root home directory, or SSH private key.

The following is a one-time VPS setup. Use a real non-root account instead of
the example UID `65534`; put its numeric UID and GID into `.env` as
`AGY_WORKER_UID` and `AGY_WORKER_GID`.

```bash
apt-get update && apt-get install -y bubblewrap
useradd --system --create-home \
  --home-dir /opt/antigravity-bot/agy-worker-home \
  --shell /usr/sbin/nologin agyworker

install -d -o agyworker -g agyworker -m 0750 \
  /opt/antigravity-bot/agy-worker-home \
  /opt/antigravity-bot/agy-worker-home/.gemini \
  /opt/antigravity-bot/agy-worker-home/.gemini/config \
  /opt/antigravity-bot/agy-worker-home/.gemini/antigravity-cli \
  /opt/antigravity-bot/agy-worker-home/.gemini/antigravity-cli/skills \
  /opt/antigravity-bot/agy-worker-home/.config \
  /opt/antigravity-bot/agy-worker-home/.cache \
  /opt/antigravity-bot/agy-worker-home/.local \
  /opt/antigravity-bot/agy-worker-home/.local/share
printf '{}\n' > /opt/antigravity-bot/agy-worker-home/.gemini/config/mcp_config.json
chown agyworker:agyworker \
  /opt/antigravity-bot/agy-worker-home/.gemini/config/mcp_config.json
chmod 0640 /opt/antigravity-bot/agy-worker-home/.gemini/config/mcp_config.json

install -d -o root -g root -m 0755 /opt/antigravity-bot/agy-worker-runtime
id -u agyworker
id -g agyworker
```

Install the AGY executable and every non-system file it needs in
`AGY_WORKER_RUNTIME_DIR`, then set `AGY_PATH` to its executable there. A CLI
launcher below `/root/.local` is deliberately rejected: mounting `/root` would
reintroduce the exact secrets isolation is meant to remove. A system-wide CLI
below `/usr` is also accepted. Configure the AGY account/login separately with
`HOME=/opt/antigravity-bot/agy-worker-home`; this home may contain only the
CLI's own authentication and session data, never bot secrets.

The bot keeps its bundled skills as skills: it syncs them to
`AGY_GLOBAL_SKILLS_DIR` and mounts that directory read-only into every worker.
The worker receives a generated, per-task MCP configuration, so native
`save_memory`, `list_memory`, and `delete_memory` still work without exposing a
database path. The `remote-environments` skill uses `ag-ssh`; its only possible
operations are listing configured names, showing the public key, and submitting
one remote command for an explicit Telegram approval.

The short-lived capability socket is reachable only through that task's mounted
directory and every request carries a fresh 256-bit token. This is deliberate:
Bubblewrap can remap user IDs, so the token—not a fragile host UID mapping—is
the authorization boundary. The token disappears together with the task.

Add trusted host keys before enabling a remote environment. Verify each server
fingerprint against a trusted provider console or an administrator first; do
not blindly trust an `ssh-keyscan` result from an untrusted network.

```bash
install -d -m 0700 /opt/antigravity-bot/.ssh
install -m 0600 /dev/null /opt/antigravity-bot/.ssh/known_hosts
# After independently verifying the fingerprint:
ssh-keyscan -H -p 22 example-host >> /opt/antigravity-bot/.ssh/known_hosts
ssh-keygen -F example-host -f /opt/antigravity-bot/.ssh/known_hosts
```

Verify the namespace before restarting the service:

```bash
PYTHONPATH=/opt/antigravity-bot/antigravity-bot \
  /opt/antigravity-bot/venv/bin/python \
  -m bot.services.worker_sandbox check --verify-runtime
```

Keep `AGY_ALLOW_UNSANDBOXED_DEV=false` on the VPS. Setting it to `true` is a
temporary local debugging escape hatch only and intentionally produces a
critical startup warning. The worker retains outbound network access because
the AGY CLI needs its model provider; use host-level egress rules as an
additional boundary if that is a concern.

## IDE Workflow Commands

Inside a project forum topic the bot exposes a Telegram IDE workflow:

- `/project` — project dashboard with branch, changed files, model/mode/web and quick actions.
- `/files` — browse and open workspace files.
- `/search <query>` — ripgrep-based filename/content search with ignored build/cache directories.
- `/context` — show context; `/context add path`, `/context rm path`, `/context note text`, `/context clear` manage pinned task context.
- `/memory` — shows two blocks: notes for the current project and global user memory. `/memory add <text>` and `/memory rm <id>` manage the current project's notes; the agent manages global facts through native memory tools when explicitly asked.
- `/diff` — inspect the mounted project's current Git state without changing its index or refs.
- `/test` — auto-detect and run the project test command.
- `/run <command>` — run a managed command in the workspace and stream output.
- `/queue`, `/status`, `/task <id>`, `/cancel` — manage the task queue and task cards.

When a code task produces changes, the queue pauses at a review barrier. Use the task's **Diff**, **Применить**, or **Отбросить** buttons. Applying a conflicting patch is refused and leaves the task workspace intact for inspection.

## Development Checks

```bash
pip install -r antigravity-bot/requirements.txt -r requirements-dev.txt
python -m compileall antigravity-bot/bot antigravity-bot/migrate_phase3.py
python -m pytest
ruff check antigravity-bot tests
```
