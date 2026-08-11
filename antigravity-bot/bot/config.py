"""Configuration loaded from .env and config.json."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # GitHub
    github_token: str | None = None
    groq_api_key: str | None = None
    wit_ai_token: str | None = None

    model_config = SettingsConfigDict(
        env_file=(".env", "./antigravity-bot/.env", "/opt/antigravity-bot/.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str
    allowed_user_ids: str  # comma-separated
    forum_group_id: int = 0  # Telegram chat ID of the forum group
    agy_path: str = "/root/.local/bin/agy"
    workspaces_dir: str = "/tmp/workspaces"
    db_path: str = "/opt/antigravity-bot/data/bot.db"
    log_level: str = "INFO"
    config_json_path: str = "/opt/antigravity-bot/config.json"

    # Timeouts
    task_timeout_seconds: int = 600
    agy_print_timeout: str = "10m0s"
    permissions_mode: str = "skip"  # skip, ask, deny-dangerous
    dangerously_skip_permissions: bool = True

    @property
    def allowed_ids(self) -> list[int]:
        return [int(x.strip()) for x in self.allowed_user_ids.split(",") if x.strip()]

    def get_available_models(self) -> list[dict[str, str]]:
        """Read available models strictly from config.json."""
        path = self.config_json_path
        if not os.path.exists(path):
            path = str(Path(__file__).resolve().parent.parent / "config.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("models", [])
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            return [
                {"id": "gemini-3.6-flash", "name": "Gemini 3.6 Flash (Быстрый)"},
                {"id": "gemini-3.1-pro", "name": "Gemini 3.1 Pro (Умный)"},
            ]


settings = Settings()  # type: ignore[call-arg]
