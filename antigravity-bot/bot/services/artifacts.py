"""Artifact Post-Tool Delivery & Cleanup across Workspaces & Scratchpad."""

from __future__ import annotations

import logging
import os

from aiogram import Bot
from aiogram.types import FSInputFile

from bot.config import settings

logger = logging.getLogger(__name__)

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

_TRACK_EXT = (
    _IMAGE_EXT
    | {
        ".html", ".css", ".js", ".py", ".ts", ".tsx", ".jsx",
        ".pptx", ".docx", ".xlsx", ".pdf", ".svg",
        ".zip", ".tar", ".gz", ".rar", ".7z",
        ".json", ".yaml", ".yml", ".toml", ".xml",
        ".md", ".txt", ".csv", ".sh", ".bat", ".sql",
    }
)

_DEFAULT_SCRATCH_DIRS = [
    "/root/.gemini/antigravity-cli/scratch",
    "/root/.gemini/antigravity-ide/scratch",
]


def snapshot_workspaces(target_dirs: list[str]) -> dict[str, float]:
    """Return ``{absolute_path: mtime}`` for trackable files across workspace and scratchpad directories."""
    snap: dict[str, float] = {}
    all_dirs = list(target_dirs) + _DEFAULT_SCRATCH_DIRS

    for d in all_dirs:
        if not os.path.isdir(d):
            try:
                os.makedirs(d, exist_ok=True)
            except Exception:
                continue
        for root, dirs, files in os.walk(d):
            dirs[:] = [sub for sub in dirs if not sub.startswith(".")]
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext in _TRACK_EXT:
                    fpath = os.path.join(root, fname)
                    try:
                        snap[fpath] = os.path.getmtime(fpath)
                    except OSError:
                        pass
    return snap


def diff_snapshots(
    before: dict[str, float], after: dict[str, float]
) -> list[str]:
    """Return paths that are new or modified, deduplicated by filename."""
    changed_paths = [
        p for p, mt in after.items()
        if p not in before or before[p] < mt
    ]

    # Deduplicate by basename (prefer workspace dir over temp scratchpad)
    seen_names: set[str] = set()
    deduped: list[str] = []

    # Sort workspace paths first
    changed_paths.sort(key=lambda p: (0 if settings.workspaces_dir in p else 1, p))

    for p in changed_paths:
        name = os.path.basename(p)
        if name not in seen_names:
            seen_names.add(name)
            deduped.append(p)

    return deduped


async def deliver_and_cleanup_artifacts(bot: Bot, chat_id: int, files: list[str], thread_id: int | None = None) -> None:
    """Send detected artifacts as documents to Telegram without deleting workspace project files."""
    for fpath in files:
        if not os.path.exists(fpath):
            continue

        name = os.path.basename(fpath)
        ext = os.path.splitext(fpath)[1].lower()
        inp = FSInputFile(fpath)

        # Skip sending standalone background images (e.g. bg.jpg, hero_bg.jpg) as photos to avoid photo spam
        if ext in _IMAGE_EXT and ("bg" in name.lower() or "background" in name.lower() or "hero" in name.lower()):
            logger.info("Skipping standalone background image telegram photo spam: %s", name)
            continue

        try:
            # Deliver all artifacts (including HTML, CSS, JS, Images) as clean document files
            await bot.send_document(chat_id, inp, caption=name, message_thread_id=thread_id)
            logger.info("Delivered artifact to Telegram: %s", fpath)
        except Exception:
            logger.exception("Failed to deliver artifact %s to Telegram", fpath)
        finally:
            # ONLY clean up temporary scratchpad files. NEVER delete user's workspace files!
            if settings.workspaces_dir not in fpath and os.path.exists(fpath):
                try:
                    os.remove(fpath)
                    logger.info("Cleaned up scratchpad file from disk: %s", fpath)
                except Exception as e:
                    logger.warning("Failed to remove scratch file %s: %s", fpath, e)
