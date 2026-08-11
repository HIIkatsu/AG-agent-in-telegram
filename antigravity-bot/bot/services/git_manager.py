"""Local Git Version Control Engine for Workspace Snapshots, Diffs, and Rollback."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from functools import partial
from typing import Any

logger = logging.getLogger(__name__)


class GitCommandTimeout(RuntimeError):
    """Raised when a git command exceeds its configured timeout."""


def _run_git(ws_dir: str, *args: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Execute a git command in the workspace directory."""
    cmd = ["git"] + list(args)
    try:
        return subprocess.run(
            cmd,
            cwd=ws_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("git command timed out in %s after %ss: %s", ws_dir, timeout, " ".join(cmd))
        raise GitCommandTimeout(f"git {' '.join(args)} timed out after {timeout}s") from exc


async def _to_thread(fn: Any, /, *args: Any, wait_timeout: float | None = None, **kwargs: Any) -> Any:
    """Run blocking git work in a worker thread with an outer asyncio timeout."""
    call = partial(fn, *args, **kwargs)
    if wait_timeout is None:
        return await asyncio.to_thread(call)
    async with asyncio.timeout(wait_timeout + 1):
        return await asyncio.to_thread(call)


class GitManager:
    """Manages local git repo per chat workspace for instant diffs and clean rollbacks."""

    def init_workspace(self, ws_dir: str, timeout: float = 10) -> None:
        """Initialize git repo in workspace if not already present."""
        os.makedirs(ws_dir, exist_ok=True)
        git_dir = os.path.join(ws_dir, ".git")
        if not os.path.isdir(git_dir):
            logger.info("Initializing git workspace: %s", ws_dir)
            gitignore_path = os.path.join(ws_dir, ".gitignore")
            if not os.path.exists(gitignore_path):
                with open(gitignore_path, "w", encoding="utf-8") as f:
                    f.write(".agyrules\n.agents/\n__pycache__/\n")
            _run_git(ws_dir, "init", timeout=timeout)
            _run_git(ws_dir, "config", "user.name", "AntigravityBot", timeout=timeout)
            _run_git(ws_dir, "config", "user.email", "bot@antigravity.local", timeout=timeout)
            _run_git(ws_dir, "add", ".", timeout=timeout)
            _run_git(ws_dir, "commit", "-m", "Initial commit", "--allow-empty", timeout=timeout)

    async def init_workspace_async(self, ws_dir: str, timeout: float = 10) -> None:
        await _to_thread(self.init_workspace, ws_dir, timeout=timeout, wait_timeout=timeout)

    def get_current_branch(self, ws_dir: str, timeout: float = 5) -> str:
        self.init_workspace(ws_dir, timeout=timeout)
        res = _run_git(ws_dir, "rev-parse", "--abbrev-ref", "HEAD", timeout=timeout)
        return res.stdout.strip()

    async def get_current_branch_async(self, ws_dir: str, timeout: float = 5) -> str:
        return await _to_thread(self.get_current_branch, ws_dir, timeout=timeout, wait_timeout=timeout)

    def status(self, ws_dir: str, timeout: float = 5) -> list[str]:
        self.init_workspace(ws_dir, timeout=timeout)
        _run_git(ws_dir, "add", "-N", ".", timeout=timeout)
        res = _run_git(ws_dir, "status", "--porcelain", timeout=timeout)
        return [line for line in res.stdout.splitlines() if line.strip()]

    async def status_async(self, ws_dir: str, timeout: float = 5) -> list[str]:
        return await _to_thread(self.status, ws_dir, timeout=timeout, wait_timeout=timeout)

    def changed_files(self, ws_dir: str, timeout: float = 5) -> list[str]:
        """Return changed file paths using git status porcelain without rendering full diffs."""
        files: list[str] = []
        for line in self.status(ws_dir, timeout=timeout):
            path = line[3:] if len(line) > 3 else line.strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            files.append(os.path.join(ws_dir, path))
        return files

    async def changed_files_async(self, ws_dir: str, timeout: float = 5) -> list[str]:
        return await _to_thread(self.changed_files, ws_dir, timeout=timeout, wait_timeout=timeout)

    def create_checkpoint(self, ws_dir: str, label: str = "checkpoint", timeout: float = 15) -> str:
        self.init_workspace(ws_dir, timeout=timeout)
        gitignore_path = os.path.join(ws_dir, ".gitignore")
        _needed = {".agyrules", ".agents/"}
        if os.path.exists(gitignore_path):
            existing = set(open(gitignore_path, encoding="utf-8").read().splitlines())
            missing = _needed - existing
            if missing:
                with open(gitignore_path, "a", encoding="utf-8") as f:
                    for m in missing:
                        f.write(f"\n{m}")
        _run_git(ws_dir, "add", ".", timeout=timeout)
        _run_git(ws_dir, "commit", "-m", f"Pre-task: {label}", "--allow-empty", timeout=timeout)
        res = _run_git(ws_dir, "rev-parse", "HEAD", timeout=timeout)
        return res.stdout.strip()

    async def create_checkpoint_async(self, ws_dir: str, label: str = "checkpoint", timeout: float = 15) -> str:
        return await _to_thread(self.create_checkpoint, ws_dir, label, timeout=timeout, wait_timeout=timeout)

    def has_changes(self, ws_dir: str, timeout: float = 5) -> bool:
        return bool(self.status(ws_dir, timeout=timeout))

    async def has_changes_async(self, ws_dir: str, timeout: float = 5) -> bool:
        return await _to_thread(self.has_changes, ws_dir, timeout=timeout, wait_timeout=timeout)

    def get_diff(self, ws_dir: str, timeout: float = 5) -> str:
        self.init_workspace(ws_dir, timeout=timeout)
        _run_git(ws_dir, "add", "-N", ".", timeout=timeout)
        res = _run_git(ws_dir, "diff", "HEAD", timeout=timeout)
        return res.stdout

    async def get_diff_async(self, ws_dir: str, timeout: float = 5) -> str:
        return await _to_thread(self.get_diff, ws_dir, timeout=timeout, wait_timeout=timeout)

    def rollback(self, ws_dir: str, timeout: float = 15) -> bool:
        self.init_workspace(ws_dir, timeout=timeout)
        res_reset = _run_git(ws_dir, "reset", "--hard", "HEAD", timeout=timeout)
        res_clean = _run_git(ws_dir, "clean", "-fd", timeout=timeout)
        logger.info("Git rollback in %s: reset=%s, clean=%s", ws_dir, res_reset.stdout.strip(), res_clean.stdout.strip())
        return res_reset.returncode == 0

    async def rollback_async(self, ws_dir: str, timeout: float = 15) -> bool:
        return await _to_thread(self.rollback, ws_dir, timeout=timeout, wait_timeout=timeout)

    def rollback_to_commit(self, ws_dir: str, commit_hash: str, timeout: float = 15) -> bool:
        self.init_workspace(ws_dir, timeout=timeout)
        res_reset = _run_git(ws_dir, "reset", "--hard", commit_hash, timeout=timeout)
        res_clean = _run_git(ws_dir, "clean", "-fd", timeout=timeout)
        logger.info("Git rollback to %s in %s: reset=%s, clean=%s", commit_hash, ws_dir, res_reset.stdout.strip(), res_clean.stdout.strip())
        return res_reset.returncode == 0

    async def rollback_to_commit_async(self, ws_dir: str, commit_hash: str, timeout: float = 15) -> bool:
        return await _to_thread(self.rollback_to_commit, ws_dir, commit_hash, timeout=timeout, wait_timeout=timeout)

    def accept(self, ws_dir: str, timeout: float = 15) -> None:
        self.init_workspace(ws_dir, timeout=timeout)
        _run_git(ws_dir, "add", ".", timeout=timeout)
        _run_git(ws_dir, "commit", "-m", "Accepted task modifications", "--allow-empty", timeout=timeout)

    async def accept_async(self, ws_dir: str, timeout: float = 15) -> None:
        await _to_thread(self.accept, ws_dir, timeout=timeout, wait_timeout=timeout)


git_manager = GitManager()
