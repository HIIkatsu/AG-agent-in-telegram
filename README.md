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
4. The bot registers its `ag-telegram-memory` MCP server in `AGY_MCP_CONFIG_PATH` on startup. Other MCP servers in that file are preserved; a conflicting server with that exact name is refused rather than overwritten.
5. Keep `TASK_WORKSPACES_DIR` outside mounted repositories. Tracked and non-ignored files are copied there for each code task; ignored secrets such as `.env` are excluded.
6. Install dependencies: `pip install -r antigravity-bot/requirements.txt`.

If the startup log says that the MCP config is invalid, the bot remains available
but native memory tools are disabled. Do not delete or paste the config into chat.
After updating to a version that includes the repair command, run this explicit
local operation from the repository root:

    PYTHONPATH=antigravity-bot python -m bot.services.memory_mcp_config repair-invalid

It creates an owner-only (0600) backup of the unreadable original and writes a
minimal valid config containing only the bot's memory MCP server. Restart the bot
afterwards.

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
