"""Fresh-database bootstrap regression tests."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "antigravity-bot"))
os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")

from bot.db import Database


def test_fresh_database_creates_environments_table(tmp_path: Path) -> None:
    async def exercise() -> None:
        database = Database()
        database._path = str(tmp_path / "fresh.db")
        await database.connect()
        try:
            await database.add_environment(
                "home",
                "host.example",
                22,
                "agent",
                "/tmp/test-key",
            )
            environments = await database.get_all_environments()
            assert environments == [
                {
                    "id": 1,
                    "name": "home",
                    "host": "host.example",
                    "port": 22,
                    "username": "agent",
                    "ssh_key_path": "/tmp/test-key",
                }
            ]
        finally:
            await database.close()

    asyncio.run(exercise())
