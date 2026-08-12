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
    agy_mcp_config_path: str = "~/.gemini/config/mcp_config.json"
    # AGY always runs in a fail-closed Bubblewrap worker. The dedicated worker
    # home may contain only the CLI's own authentication/runtime state; bot
    # tokens, SQLite data and SSH keys are never mounted there.
    agy_sandbox_binary: str = "/usr/bin/bwrap"
    agy_sandbox_python_path: str = "/usr/bin/python3"
    agy_worker_home: str = "/opt/antigravity-bot/agy-worker-home"
    # A dedicated, read-only CLI runtime. AGY_PATH must either live below this
    # directory or be a system executable below /usr; never point it into the
    # bot service account's home directory when the sandbox is enabled.
    agy_worker_runtime_dir: str = "/opt/antigravity-bot/agy-worker-runtime"
    agy_worker_uid: int = 65534
    agy_worker_gid: int = 65534
    agy_capability_socket_dir: str = "/tmp/antigravity-capabilities"
    agy_allow_unsandboxed_dev: bool = False
    workspaces_dir: str = "/tmp/workspaces"
    task_workspaces_dir: str = "/tmp/antigravity-task-workspaces"
    # Every AGY invocation gets one private output directory. It is mounted at
    # the CLI scratch path and is the only location from which generated files
    # are auto-delivered to Telegram.
    task_artifacts_dir: str = "/tmp/antigravity-task-artifacts"
    artifact_max_files: int = 20
    artifact_max_size_mb: int = 45
    db_path: str = "/opt/antigravity-bot/data/bot.db"
    log_level: str = "INFO"
    config_json_path: str = "/opt/antigravity-bot/config.json"

    # Timeouts
    task_timeout_seconds: int = 600
    agy_print_timeout: str = "10m0s"
    permissions_mode: str = "skip"  # skip, ask, deny-dangerous
    dangerously_skip_permissions: bool = True
    ssh_key_path: str = "/opt/antigravity-bot/.ssh/bot_ed25519"
    ssh_known_hosts_path: str = "/opt/antigravity-bot/.ssh/known_hosts"
    ssh_command_timeout_seconds: int = 120
    ssh_approval_timeout_seconds: int = 120

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
