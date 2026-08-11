"""Rendering regression tests for the unified /memory command."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "antigravity-bot"))
os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")

from bot.handlers import memory


class _FakeMemoryDatabase:
    async def get_all_user_memory(self) -> list[dict]:
        return [{"id": 5, "fact": "Глобальный <факт>"}]

    async def list_memory_notes(self, thread_id: int) -> list[dict]:
        assert thread_id == 42
        return [{"id": 9, "note": "Локальная <заметка>"}]


def test_memory_view_shows_project_and_global_blocks(monkeypatch) -> None:
    monkeypatch.setattr(memory, "db", _FakeMemoryDatabase())

    text, keyboard = asyncio.run(
        memory._build_memory_view(42, include_back=False)
    )

    assert "🧠 <b>Память проекта</b>" in text
    assert "🌐 <b>Глобальная память</b>" in text
    assert "Локальная &lt;заметка&gt;" in text
    assert "Глобальный &lt;факт&gt;" in text
    assert "/memory add текст" in text
    assert keyboard is not None
    callback_data = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert "mem_pdel:9:42" in callback_data
    assert "mem_del:5:42" in callback_data


def test_memory_view_in_general_topic_keeps_global_memory_available(monkeypatch) -> None:
    monkeypatch.setattr(memory, "db", _FakeMemoryDatabase())

    text, _ = asyncio.run(memory._build_memory_view(None, include_back=False))

    assert "— доступна внутри темы проекта" in text
    assert "Глобальный &lt;факт&gt;" in text


def test_ide_router_no_longer_shadows_the_unified_memory_command() -> None:
    ide_source = (ROOT / "antigravity-bot" / "bot" / "handlers" / "ide.py").read_text(
        encoding="utf-8"
    )

    assert 'Command("memory")' not in ide_source


def test_native_command_menu_has_one_unified_memory_entry() -> None:
    main_source = (ROOT / "antigravity-bot" / "bot" / "__main__.py").read_text(
        encoding="utf-8"
    )

    assert main_source.count('BotCommand(command="memory"') == 1
