# Antigravity in Telegram 🚀

Telegram bot integration for **Antigravity CLI** agent execution, featuring multi-thread conversation persistence, voice message transcription, local Git checkpoints, and dynamic task tracking.

## Features

- 🤖 **Antigravity CLI Integration**: Run agent sessions via native `agy` CLI with stream-json output.
- 💬 **Forum Topics & Thread Isolation**: Each Telegram thread maps to an isolated workspace with project session persistence.
- 🎤 **Voice Support**: Instant voice message transcription via cloud STT (Groq with Wit.ai fallback).
- 🔄 **Git Checkpoints & Rollback**: Automatic workspace checkpoints before tasks with side-by-side diff viewing and one-click rollback.
- 🛡️ **Runtime Rules**: Combines tracked `INSTRUCTIONS.md` policy with optional private `INSTRUCTIONS.local.md` context without modifying mounted projects.
- 🧩 **Global Agent Skills**: Registers every bundled skill in AGY's user-level skill directory while leaving each mounted project's `.agents` directory untouched.
- 🧠 **Global Memory**: Injects a fresh, bounded snapshot of durable user facts into every task; the `global-memory` skill can list, save, and delete facts.

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
4. Install dependencies: `pip install -r antigravity-bot/requirements.txt`.

## IDE Workflow Commands

Inside a project forum topic the bot exposes a Telegram IDE workflow:

- `/project` — project dashboard with branch, changed files, model/mode/web and quick actions.
- `/files` — browse and open workspace files.
- `/search <query>` — ripgrep-based filename/content search with ignored build/cache directories.
- `/context` — show context; `/context add path`, `/context rm path`, `/context note text`, `/context clear` manage pinned task context.
- `/memory` — inspect and delete global user memory; agents can update it through the `global-memory` skill when explicitly asked.
- `/diff` — review changed files and open diff.html / patch / accept / rollback / tests.
- `/test` — auto-detect and run the project test command.
- `/run <command>` — run a managed command in the workspace and stream output.
- `/queue`, `/status`, `/task <id>`, `/cancel` — manage the task queue and task cards.

## Development Checks

```bash
pip install -r antigravity-bot/requirements.txt -r requirements-dev.txt
python -m compileall antigravity-bot/bot antigravity-bot/migrate_phase3.py
python -m pytest
ruff check antigravity-bot tests
```
