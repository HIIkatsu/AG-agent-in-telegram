"""File Backup & Rollback System for File Modifications."""

from __future__ import annotations

import logging
import os
import shutil
import time

logger = logging.getLogger(__name__)


class BackupManager:
    """Manages file backups (.bak) before file modifications and handles rollback."""

    def __init__(self) -> None:
        self._backups: dict[int, list[tuple[str, str]]] = {}  # chat_id -> [(original_path, backup_path)]

    def has_backups(self, chat_id: int) -> bool:
        """Return True if chat_id has active file backups."""
        return bool(self._backups.get(chat_id))

    def create_backup(self, chat_id: int, target_file_path: str) -> str | None:
        """Create a backup copy (.bak) of target_file_path if it exists."""
        if not target_file_path or not os.path.isfile(target_file_path):
            return None

        try:
            timestamp = int(time.time() * 1000)
            backup_path = f"{target_file_path}.bak_{timestamp}"
            shutil.copy2(target_file_path, backup_path)
            
            if chat_id not in self._backups:
                self._backups[chat_id] = []
            self._backups[chat_id].append((target_file_path, backup_path))

            logger.info("Created backup for chat %d: %s -> %s", chat_id, target_file_path, backup_path)
            return backup_path
        except Exception as e:
            logger.warning("Failed to create backup for %s: %s", target_file_path, e)
            return None

    def rollback(self, chat_id: int) -> list[str]:
        """Restore all backed up files for chat_id and remove backup files."""
        restored: list[str] = []
        entries = self._backups.pop(chat_id, [])

        for original_path, backup_path in entries:
            try:
                if os.path.isfile(backup_path):
                    shutil.copy2(backup_path, original_path)
                    os.remove(backup_path)
                    restored.append(os.path.basename(original_path))
                    logger.info("Restored file from backup: %s", original_path)
            except Exception as e:
                logger.error("Failed to restore backup %s -> %s: %s", backup_path, original_path, e)

        return restored

    def clear(self, chat_id: int) -> None:
        """Clear backup entries for a chat after successful completion without deleting backups."""
        self._backups.pop(chat_id, None)


backup_manager = BackupManager()
