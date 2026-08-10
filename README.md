# Antigravity in Telegram 🚀

Telegram bot integration for **Antigravity CLI** agent execution, featuring multi-thread conversation persistence, voice message transcription, local Git checkpoints, and dynamic task tracking.

## Features

- 🤖 **Antigravity CLI Integration**: Run agent sessions via native `agy` CLI with stream-json output.
- 💬 **Forum Topics & Thread Isolation**: Each Telegram thread maps to an isolated workspace with project session persistence.
- 🎤 **Voice Support**: Instant voice message transcription via `faster-whisper`.
- 🔄 **Git Checkpoints & Rollback**: Automatic workspace checkpoints before tasks with side-by-side diff viewing and one-click rollback.
- 🛡️ **Native Rules**: Uses `.agents/AGENTS.md` rules for agent behavior control.
- 🚀 **One-Click Deploy**: Automated SSH deployment script for Linux VPS.

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
│   └── requirements.txt
├── deploy.py               # VPS deployment script
├── quick_deploy.py         # Fast SSH sync script
└── README.md
```

## Setup & Deployment

1. Copy `.env.example` to `.env` and fill in your Telegram Bot credentials.
2. Install dependencies: `pip install -r antigravity-bot/requirements.txt`.
3. Deploy to VPS using `python quick_deploy.py`.

## IDE Workflow Commands

Inside a project forum topic the bot exposes a Telegram IDE workflow:

- `/project` — project dashboard with branch, changed files, model/mode/web and quick actions.
- `/files` — browse and open workspace files.
- `/search <query>` — ripgrep-based filename/content search with ignored build/cache directories.
- `/context` — show context; `/context add path`, `/context rm path`, `/context note text`, `/context clear` manage pinned task context.
- `/memory` — show project memory; `/memory add text`, `/memory rm id` manage persistent project notes.
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
