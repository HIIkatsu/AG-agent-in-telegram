"""Local Git Version Control Engine for Workspace Snapshots, Diffs, and Rollback."""

from __future__ import annotations

import logging
import os
import subprocess

logger = logging.getLogger(__name__)


def _run_git(ws_dir: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Execute a git command in the workspace directory."""
    cmd = ["git"] + list(args)
    return subprocess.run(
        cmd,
        cwd=ws_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )


class GitManager:
    """Manages local git repo per chat workspace for instant diffs and clean rollbacks."""

    def init_workspace(self, ws_dir: str) -> None:
        """Initialize git repo in workspace if not already present."""
        os.makedirs(ws_dir, exist_ok=True)
        git_dir = os.path.join(ws_dir, ".git")
        if not os.path.isdir(git_dir):
            logger.info("Initializing git workspace: %s", ws_dir)
            # Create .gitignore to exclude bot system files from tracking
            gitignore_path = os.path.join(ws_dir, ".gitignore")
            if not os.path.exists(gitignore_path):
                with open(gitignore_path, "w", encoding="utf-8") as f:
                    f.write(".agyrules\n.agents/\n__pycache__/\n")
            _run_git(ws_dir, "init")
            _run_git(ws_dir, "config", "user.name", "AntigravityBot")
            _run_git(ws_dir, "config", "user.email", "bot@antigravity.local")
            _run_git(ws_dir, "add", ".")
            _run_git(ws_dir, "commit", "-m", "Initial commit", "--allow-empty")

    def create_checkpoint(self, ws_dir: str, label: str = "checkpoint") -> str:
        """Create a commit snapshot before starting a new task and return its commit hash."""
        self.init_workspace(ws_dir)
        # Ensure .agyrules is always in .gitignore (even for pre-existing workspaces)
        gitignore_path = os.path.join(ws_dir, ".gitignore")
        _needed = {".agyrules", ".agents/"}
        if os.path.exists(gitignore_path):
            existing = set(open(gitignore_path, encoding="utf-8").read().splitlines())
            missing = _needed - existing
            if missing:
                with open(gitignore_path, "a", encoding="utf-8") as f:
                    for m in missing:
                        f.write(f"\n{m}")
        _run_git(ws_dir, "add", ".")
        _run_git(ws_dir, "commit", "-m", f"Pre-task: {label}", "--allow-empty")
        res = _run_git(ws_dir, "rev-parse", "HEAD")
        commit_hash = res.stdout.strip()
        logger.debug("Git checkpoint created in %s: %s", ws_dir, commit_hash)
        return commit_hash

    def has_changes(self, ws_dir: str) -> bool:
        """Return True if workspace has uncommitted changes or new untracked files."""
        self.init_workspace(ws_dir)
        _run_git(ws_dir, "add", "-N", ".")
        res = _run_git(ws_dir, "status", "--porcelain")
        return bool(res.stdout.strip())

    def get_diff(self, ws_dir: str) -> str:
        """Generate git diff against HEAD. Uses 'git add -N .' to register new untracked files."""
        self.init_workspace(ws_dir)
        # CRITICAL: Intent-to-add registers new files so they appear in git diff HEAD
        _run_git(ws_dir, "add", "-N", ".")
        res = _run_git(ws_dir, "diff", "HEAD")
        return res.stdout

    def rollback(self, ws_dir: str) -> bool:
        """Revert all modified files to HEAD and clean untracked files."""
        self.init_workspace(ws_dir)
        res_reset = _run_git(ws_dir, "reset", "--hard", "HEAD")
        res_clean = _run_git(ws_dir, "clean", "-fd")
        logger.info("Git rollback in %s: reset=%s, clean=%s", ws_dir, res_reset.stdout.strip(), res_clean.stdout.strip())
        return res_reset.returncode == 0

    def rollback_to_commit(self, ws_dir: str, commit_hash: str) -> bool:
        """Revert workspace to a specific commit and clean untracked files."""
        self.init_workspace(ws_dir)
        res_reset = _run_git(ws_dir, "reset", "--hard", commit_hash)
        res_clean = _run_git(ws_dir, "clean", "-fd")
        logger.info("Git rollback to %s in %s: reset=%s, clean=%s", commit_hash, ws_dir, res_reset.stdout.strip(), res_clean.stdout.strip())
        return res_reset.returncode == 0

    def accept(self, ws_dir: str) -> None:
        """Commit current changes to accept the task modifications."""
        self.init_workspace(ws_dir)
        _run_git(ws_dir, "add", ".")
        _run_git(ws_dir, "commit", "-m", "Accepted task modifications", "--allow-empty")


git_manager = GitManager()
