"""Artifact Post-Tool Delivery & Cleanup across Workspaces & Scratchpad."""

from __future__ import annotations

import logging
import os
from pathlib import Path

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

_DELIVER_EXT = {
    ".pdf", ".zip", ".tar", ".gz", ".rar", ".7z",
    ".pptx", ".docx", ".xlsx", ".html"
}

_DEFAULT_SCRATCH_DIRS = [
    "/root/.gemini/antigravity-cli/scratch",
    "/root/.gemini/antigravity-ide/scratch",
]

def _is_valid_dir(d: Path) -> bool:
    name = d.name
    if name.startswith("."):
        return False
    if name in {"node_modules", "__pycache__", "venv", ".venv"}:
        return False
    return True

def snapshot_workspaces(target_dirs: list[str]) -> dict[str, float]:
    """Return ``{absolute_path: mtime}`` for trackable files across workspace and scratchpad directories."""
    snap: dict[str, float] = {}
    all_dirs = [Path(d).resolve() for d in target_dirs + _DEFAULT_SCRATCH_DIRS]

    for d in all_dirs:
        if not d.is_dir():
            try:
                d.mkdir(parents=True, exist_ok=True)
            except Exception:
                continue
                
        for root, dirs, files in os.walk(d):
            root_path = Path(root)
            dirs[:] = [sub for sub in dirs if _is_valid_dir(Path(sub))]
            for fname in files:
                ext = Path(fname).suffix.lower()
                if ext in _TRACK_EXT:
                    fpath = root_path / fname
                    try:
                        snap[str(fpath)] = fpath.stat().st_mtime
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

    seen_names: set[str] = set()
    deduped: list[str] = []

    # Sort workspace paths first
    ws_dir = str(Path(settings.workspaces_dir).resolve())
    changed_paths.sort(key=lambda p: (0 if p.startswith(ws_dir) else 1, p))

    for p in changed_paths:
        name = os.path.basename(p)
        if name not in seen_names:
            seen_names.add(name)
            deduped.append(p)

    return deduped


def should_deliver(fpath: Path) -> bool:
    """Check if the file should be delivered to Telegram."""
    ext = fpath.suffix.lower()
    
    # Always deliver explicitly requested file types
    if ext in _DELIVER_EXT:
        return True
        
    # Always deliver images, but filter out background images later
    if ext in _IMAGE_EXT:
        return True
        
    # Deliver HTML, CSV, etc ONLY if they are in 'artifacts' or 'output' folders
    parts = fpath.parts
    if "artifacts" in parts or "output" in parts or "outputs" in parts:
        return True
        
    return False


async def deliver_and_cleanup_artifacts(
    bot: Bot, 
    chat_id: int, 
    files: list[str], 
    thread_id: int | None = None,
    rollback_list: list[int] | None = None,
) -> None:
    """Send detected artifacts as documents to Telegram without deleting workspace project files."""
    ws_dir = str(Path(settings.workspaces_dir).resolve())
    
    for fpath_str in files:
        fpath = Path(fpath_str)
        if not fpath.exists():
            continue

        name = fpath.name
        ext = fpath.suffix.lower()
        
        # Decide whether to send to Telegram
        if should_deliver(fpath):
            # Skip sending standalone background images to avoid photo spam
            if ext in _IMAGE_EXT and any(kw in name.lower() for kw in ("bg", "background", "hero")):
                logger.info("Skipping standalone background image telegram photo spam: %s", name)
            else:
                try:
                    inp = FSInputFile(str(fpath))
                    msg = await bot.send_document(chat_id, inp, caption=name, message_thread_id=thread_id)
                    if rollback_list is not None:
                        rollback_list.append(msg.message_id)
                    logger.info("Delivered artifact to Telegram: %s", fpath)
                except Exception:
                    logger.exception("Failed to deliver artifact %s to Telegram", fpath)

        # ONLY clean up temporary scratchpad files. NEVER delete user's workspace files!
        if not str(fpath).startswith(ws_dir):
            try:
                fpath.unlink(missing_ok=True)
                logger.info("Cleaned up scratchpad file from disk: %s", fpath)
            except Exception as e:
                logger.warning("Failed to remove scratch file %s: %s", fpath, e)


_IGNORE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "dist", "build", ".cache",
    "__pycache__", ".agents", ".antigravity", "vendor", "target",
}


def _is_ignored_dir(path: Path) -> bool:
    return path.name in _IGNORE_DIRS or path.name.startswith(".") and path.name not in {"."}


def _collect_recent_scratch_files_sync(
    started_at: float,
    max_depth: int = 3,
    max_files: int = 200,
    deadline_seconds: float = 2.0,
) -> list[str]:
    """Collect recent scratchpad artifacts with tight depth/count/time limits."""
    import time

    deadline = time.monotonic() + deadline_seconds
    found: list[str] = []
    stack: list[tuple[Path, int]] = [(Path(d), 0) for d in _DEFAULT_SCRATCH_DIRS]

    while stack and len(found) < max_files and time.monotonic() < deadline:
        root, depth = stack.pop()
        if not root.is_dir():
            continue
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if len(found) >= max_files or time.monotonic() >= deadline:
                break
            try:
                if entry.is_dir():
                    if depth < max_depth and not _is_ignored_dir(entry):
                        stack.append((entry, depth + 1))
                    continue
                if entry.suffix.lower() not in _TRACK_EXT:
                    continue
                if entry.stat().st_mtime >= started_at:
                    found.append(str(entry.resolve()))
            except OSError:
                continue
    return found


async def collect_task_artifacts(ws_dir: str, started_at: float) -> list[str]:
    """Collect task artifacts without full workspace os.walk on the event loop.

    Workspace changes come from git status; scratchpad is scanned in a bounded
    worker-thread pass by mtime/depth/count/time limits.
    """
    import asyncio

    from bot.services.git_manager import GitCommandTimeout, git_manager

    workspace_files: list[str] = []
    try:
        workspace_files = await git_manager.changed_files_async(ws_dir, timeout=5)
    except (GitCommandTimeout, TimeoutError):
        logger.warning("Git changed-files collection timed out for %s", ws_dir)
    except Exception:
        logger.exception("Failed to collect git changed files for %s", ws_dir)

    try:
        scratch_files = await asyncio.to_thread(_collect_recent_scratch_files_sync, started_at)
    except Exception:
        logger.exception("Failed to collect scratch artifacts")
        scratch_files = []

    deduped: list[str] = []
    seen: set[str] = set()
    for fpath in workspace_files + scratch_files:
        name = os.path.basename(fpath)
        if name not in seen:
            seen.add(name)
            deduped.append(fpath)
    return deduped
