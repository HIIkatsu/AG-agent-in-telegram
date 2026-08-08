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
