"""Regression tests for idempotent settings-screen edits."""

import importlib
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText


sys.path.insert(0, str(Path(__file__).parents[1] / "antigravity-bot"))


def _edit_settings_message():
    """Import the handler after each test has supplied required configuration."""
    return importlib.import_module("bot.handlers.settings")._edit_settings_message


def test_unchanged_settings_message_is_a_success(monkeypatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:testing")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    error = TelegramBadRequest(
        method=EditMessageText(text="unchanged", chat_id=1, message_id=1),
        message="Bad Request: message is not modified",
    )
    message = SimpleNamespace(edit_text=AsyncMock(side_effect=error))

    asyncio.run(
        _edit_settings_message()(SimpleNamespace(message=message), "unchanged", None)
    )


def test_other_edit_errors_are_not_hidden(monkeypatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:testing")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    error = TelegramBadRequest(
        method=EditMessageText(text="invalid", chat_id=1, message_id=1),
        message="Bad Request: message to edit not found",
    )
    message = SimpleNamespace(edit_text=AsyncMock(side_effect=error))

    with pytest.raises(TelegramBadRequest, match="message to edit not found"):
        asyncio.run(
            _edit_settings_message()(SimpleNamespace(message=message), "invalid", None)
        )
