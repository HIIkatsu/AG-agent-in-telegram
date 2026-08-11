"""Read-only Git inspection plus explicit initialization of bot-owned projects."""

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


class GitRepositoryRequired(RuntimeError):
    """Raised when a read operation is requested for a non-Git directory."""


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
    """Inspect repositories without changing their index, worktree, or refs."""

    def _require_repository(self, ws_dir: str, timeout: float) -> None:
        result = _run_git(ws_dir, "rev-parse", "--is-inside-work-tree", timeout=timeout)
        if result.returncode != 0 or result.stdout.strip() != "true":
            raise GitRepositoryRequired(f"Workspace is not a Git repository: {ws_dir}")

    def init_workspace(self, ws_dir: str, timeout: float = 10) -> None:
        """Initialize an explicitly bot-owned workspace.

        Callers must never use this for an arbitrary mounted directory.
        """
        os.makedirs(ws_dir, exist_ok=True)
        git_dir = os.path.join(ws_dir, ".git")
        if not os.path.isdir(git_dir):
            logger.info("Initializing git workspace: %s", ws_dir)
            gitignore_path = os.path.join(ws_dir, ".gitignore")
            if not os.path.exists(gitignore_path):
                with open(gitignore_path, "w", encoding="utf-8") as f:
                    f.write(".agyrules\n.agents/\n__pycache__/\n")
            commands = (
                ("init",),
                ("config", "user.name", "AntigravityBot"),
                ("config", "user.email", "bot@antigravity.local"),
                ("add", "."),
                ("commit", "-m", "Initial commit", "--allow-empty"),
            )
            for command in commands:
                result = _run_git(ws_dir, *command, timeout=timeout)
                if result.returncode != 0:
                    detail = result.stderr.strip() or result.stdout.strip()
                    raise RuntimeError(f"git {' '.join(command)} failed: {detail}")

    async def init_workspace_async(self, ws_dir: str, timeout: float = 10) -> None:
        await _to_thread(self.init_workspace, ws_dir, timeout=timeout, wait_timeout=timeout)

    def get_current_branch(self, ws_dir: str, timeout: float = 5) -> str:
        self._require_repository(ws_dir, timeout)
        res = _run_git(ws_dir, "rev-parse", "--abbrev-ref", "HEAD", timeout=timeout)
        if res.returncode != 0:
            raise GitRepositoryRequired(res.stderr.strip() or "Git HEAD is unavailable")
        return res.stdout.strip()

    async def get_current_branch_async(self, ws_dir: str, timeout: float = 5) -> str:
        return await _to_thread(self.get_current_branch, ws_dir, timeout=timeout, wait_timeout=timeout)

    def status(self, ws_dir: str, timeout: float = 5) -> list[str]:
        self._require_repository(ws_dir, timeout)
        res = _run_git(
            ws_dir,
            "status",
            "--porcelain",
            "--untracked-files=all",
            timeout=timeout,
        )
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or "git status failed")
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

    def has_changes(self, ws_dir: str, timeout: float = 5) -> bool:
        return bool(self.status(ws_dir, timeout=timeout))

    async def has_changes_async(self, ws_dir: str, timeout: float = 5) -> bool:
        return await _to_thread(self.has_changes, ws_dir, timeout=timeout, wait_timeout=timeout)

    def get_diff(self, ws_dir: str, timeout: float = 5) -> str:
        self._require_repository(ws_dir, timeout)
        res = _run_git(
            ws_dir,
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            "HEAD",
            "--",
            timeout=timeout,
        )
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or "git diff failed")
        untracked = _run_git(
            ws_dir,
            "ls-files",
            "--others",
            "--exclude-standard",
            timeout=timeout,
        )
        if untracked.returncode != 0:
            raise RuntimeError(untracked.stderr.strip() or "git ls-files failed")
        names = [name for name in untracked.stdout.splitlines() if name]
        if not names:
            return res.stdout
        summary = "\n".join(f"# untracked: {name}" for name in names)
        return f"{res.stdout}\n{summary}\n"

    async def get_diff_async(self, ws_dir: str, timeout: float = 5) -> str:
        return await _to_thread(self.get_diff, ws_dir, timeout=timeout, wait_timeout=timeout)

git_manager = GitManager()
