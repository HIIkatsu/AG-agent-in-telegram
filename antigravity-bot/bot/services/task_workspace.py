"""Isolated, task-scoped Git workspaces.

Code tasks never run in the mounted project itself.  A private repository is
created from the project's tracked and non-ignored files, and only the delta
from that immutable snapshot can later be applied to the source workspace.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from bot.config import settings
from bot.db import db

logger = logging.getLogger(__name__)

_BLOCKING_STATES = ("active", "pending", "conflict")
_FINALIZABLE_STATES = ("pending", "conflict")
_MAX_SNAPSHOT_FILES = 100_000
_MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024 * 1024


class TaskWorkspaceError(RuntimeError):
    """A task workspace could not be created or inspected safely."""


class TaskWorkspaceConflict(TaskWorkspaceError):
    """The task patch no longer applies cleanly to the source workspace."""


@dataclass(frozen=True)
class TaskWorkspace:
    task_id: int
    thread_id: int
    source_workdir: str
    source_root: str
    source_subdir: str
    task_root: str
    task_workdir: str
    snapshot_commit: str
    state: str
    created_at: str
    finalized_at: str | None = None
    error: str | None = None

    @classmethod
    def from_row(cls, row: object) -> TaskWorkspace:
        return cls(**dict(row))  # type: ignore[arg-type]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace").strip()


def _run_git(
    cwd: Path,
    *args: str,
    input_data: bytes | None = None,
    check: bool = True,
    timeout: float = 60,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            input=input_data,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise TaskWorkspaceError(
            f"git {' '.join(args)} timed out after {timeout:g}s"
        ) from exc
    if check and result.returncode != 0:
        detail = _decode(result.stderr) or _decode(result.stdout) or "unknown git error"
        raise TaskWorkspaceError(f"git {' '.join(args)} failed: {detail[:2000]}")
    return result


def _ensure_within(path: Path, root: Path, label: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise TaskWorkspaceError(f"{label} escapes its managed root: {path}") from exc


def _task_path(task_id: int, source_root: Path) -> Path:
    base = Path(settings.task_workspaces_dir).expanduser().resolve()
    source_key = hashlib.sha256(os.fsencode(source_root)).hexdigest()[:16]
    target = (base / source_key / f"task-{task_id}").resolve()
    _ensure_within(target, base, "Task workspace")
    return target


def _remove_task_tree(task_id: int, raw_path: str) -> None:
    """Delete only the exact bot-owned directory allocated to *task_id*."""
    base = Path(settings.task_workspaces_dir).expanduser().resolve()
    target = Path(raw_path).resolve()
    _ensure_within(target, base, "Task workspace")
    if target == base or target.name != f"task-{task_id}":
        raise TaskWorkspaceError(f"Refusing unsafe task workspace cleanup: {target}")
    if target.exists():
        if target.is_symlink() or not target.is_dir():
            raise TaskWorkspaceError(
                f"Refusing unexpected task workspace type: {target}"
            )
        shutil.rmtree(target)
    try:
        target.parent.rmdir()
    except OSError:
        pass


def _copy_entry(source_root: Path, task_root: Path, relative: str) -> int:
    rel_path = Path(relative)
    if rel_path.is_absolute() or ".." in rel_path.parts:
        raise TaskWorkspaceError(f"Unsafe Git path in source repository: {relative!r}")

    source = source_root / rel_path
    destination = task_root / rel_path
    try:
        metadata = source.lstat()
    except FileNotFoundError:
        # A tracked deletion is intentionally absent from the snapshot.
        return 0

    destination.parent.mkdir(parents=True, exist_ok=True)
    if stat.S_ISLNK(metadata.st_mode):
        link_target = os.readlink(source)
        if os.path.isabs(link_target):
            raise TaskWorkspaceError(f"Absolute symlink is not allowed: {relative}")
        resolved_target = (source.parent / link_target).resolve(strict=False)
        _ensure_within(resolved_target, source_root, f"Symlink {relative}")
        os.symlink(link_target, destination)
        return len(os.fsencode(link_target))
    if stat.S_ISREG(metadata.st_mode):
        resolved_source = source.resolve()
        _ensure_within(resolved_source, source_root, f"File {relative}")
        shutil.copy2(source, destination)
        return metadata.st_size
    if stat.S_ISDIR(metadata.st_mode):
        raise TaskWorkspaceError(
            f"Git submodules are not supported in isolated task workspaces: {relative}"
        )
    raise TaskWorkspaceError(f"Unsupported source file type: {relative}")


def _create_snapshot(
    task_id: int,
    source_workdir_raw: str,
    allow_initialize: bool,
) -> dict[str, str]:
    source_workdir = Path(source_workdir_raw).expanduser()
    if not source_workdir.exists() and allow_initialize:
        source_workdir.mkdir(parents=True)
    if not source_workdir.is_dir():
        raise TaskWorkspaceError(
            f"Source workspace is not a directory: {source_workdir}"
        )
    source_workdir = source_workdir.resolve()

    root_result = _run_git(
        source_workdir,
        "rev-parse",
        "--show-toplevel",
        check=False,
        timeout=10,
    )
    if root_result.returncode != 0 and allow_initialize:
        from bot.services.git_manager import git_manager

        git_manager.init_workspace(str(source_workdir), timeout=15)
        root_result = _run_git(
            source_workdir,
            "rev-parse",
            "--show-toplevel",
            check=False,
            timeout=10,
        )
    if root_result.returncode != 0:
        raise TaskWorkspaceError(
            "Mounted code workspace must already be a Git repository; "
            "the bot will not initialize or rewrite it."
        )

    source_root = Path(os.fsdecode(root_result.stdout).strip()).resolve()
    _ensure_within(source_workdir, source_root, "Mounted workspace")
    source_subdir = source_workdir.relative_to(source_root)

    task_root = _task_path(task_id, source_root)
    managed_base = Path(settings.task_workspaces_dir).expanduser().resolve()
    try:
        managed_base.relative_to(source_root)
    except ValueError:
        pass
    else:
        raise TaskWorkspaceError(
            "TASK_WORKSPACES_DIR must not be located inside the source repository"
        )
    try:
        source_root.relative_to(managed_base)
    except ValueError:
        pass
    else:
        raise TaskWorkspaceError(
            "A source repository cannot be located inside TASK_WORKSPACES_DIR"
        )
    if task_root.exists():
        raise TaskWorkspaceError(f"Task workspace already exists: {task_root}")

    listed = _run_git(
        source_root,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        timeout=30,
    ).stdout
    entries = list(
        dict.fromkeys(os.fsdecode(item) for item in listed.split(b"\0") if item)
    )
    if len(entries) > _MAX_SNAPSHOT_FILES:
        raise TaskWorkspaceError(
            f"Repository snapshot contains too many files ({len(entries):,})"
        )

    task_root.parent.mkdir(parents=True, exist_ok=True)
    task_root.mkdir()
    try:
        total_bytes = 0
        for relative in entries:
            total_bytes += _copy_entry(source_root, task_root, relative)
            if total_bytes > _MAX_SNAPSHOT_BYTES:
                raise TaskWorkspaceError(
                    "Repository snapshot exceeds the 2 GiB safety limit"
                )

        task_workdir = task_root / source_subdir
        task_workdir.mkdir(parents=True, exist_ok=True)
        _run_git(task_root, "init", "--quiet", timeout=15)
        _run_git(task_root, "config", "user.name", "AntigravityBot", timeout=10)
        _run_git(task_root, "config", "user.email", "bot@antigravity.local", timeout=10)
        _run_git(task_root, "config", "core.autocrlf", "false", timeout=10)
        # The source list already excludes ignored *untracked* files. Force is
        # required here so files tracked by the source despite .gitignore stay
        # part of the snapshot.
        _run_git(task_root, "add", "--all", "--force", timeout=60)
        _run_git(
            task_root,
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            f"Task {task_id} source snapshot",
            timeout=60,
        )
        snapshot_commit = _decode(
            _run_git(task_root, "rev-parse", "HEAD", timeout=10).stdout
        )
    except Exception:
        _remove_task_tree(task_id, str(task_root))
        raise

    return {
        "source_workdir": str(source_workdir),
        "source_root": str(source_root),
        "source_subdir": source_subdir.as_posix() if source_subdir.parts else "",
        "task_root": str(task_root),
        "task_workdir": str(task_workdir),
        "snapshot_commit": snapshot_commit,
    }


def _build_patch(workspace: TaskWorkspace) -> bytes:
    task_root = Path(workspace.task_root)
    if not task_root.is_dir():
        raise TaskWorkspaceError(f"Task workspace is missing: {task_root}")
    _run_git(task_root, "add", "--intent-to-add", "--", ".", timeout=30)
    return _run_git(
        task_root,
        "diff",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-textconv",
        workspace.snapshot_commit,
        "--",
        timeout=60,
    ).stdout


def _apply_patch(workspace: TaskWorkspace, patch: bytes) -> bool:
    source_root = Path(workspace.source_root)
    if not source_root.is_dir():
        raise TaskWorkspaceConflict(f"Source repository is missing: {source_root}")
    current_root = _run_git(
        source_root,
        "rev-parse",
        "--show-toplevel",
        check=False,
        timeout=10,
    )
    if (
        current_root.returncode != 0
        or Path(os.fsdecode(current_root.stdout).strip()).resolve() != source_root
    ):
        raise TaskWorkspaceConflict(
            "The source path is no longer the same Git repository"
        )

    args = ("apply", "--binary", "--whitespace=nowarn", "-")
    checked = _run_git(
        source_root,
        "apply",
        "--check",
        "--binary",
        "--whitespace=nowarn",
        "-",
        input_data=patch,
        check=False,
        timeout=60,
    )
    if checked.returncode != 0:
        # Acceptance is retry-safe across a crash after git apply but before
        # the SQLite state update. If the exact patch is already present,
        # finalize it instead of applying it twice or reporting a conflict.
        reversed_check = _run_git(
            source_root,
            "apply",
            "--check",
            "--reverse",
            "--binary",
            "--whitespace=nowarn",
            "-",
            input_data=patch,
            check=False,
            timeout=60,
        )
        if reversed_check.returncode == 0:
            return False
        detail = (
            _decode(checked.stderr) or "source files changed since the task started"
        )
        raise TaskWorkspaceConflict(
            f"Task patch conflicts with the source: {detail[:2000]}"
        )
    applied = _run_git(
        source_root,
        *args,
        input_data=patch,
        check=False,
        timeout=60,
    )
    if applied.returncode != 0:
        detail = _decode(applied.stderr) or "git apply failed"
        raise TaskWorkspaceConflict(f"Task patch could not be applied: {detail[:2000]}")
    return True


class TaskWorkspaceManager:
    """Persist and finalize isolated workspaces for code tasks."""

    async def get(self, task_id: int) -> TaskWorkspace | None:
        cursor = await db.conn.execute(
            "SELECT * FROM task_workspaces WHERE task_id = ?",
            (task_id,),
        )
        row = await cursor.fetchone()
        return TaskWorkspace.from_row(row) if row else None

    async def get_blocking(self, thread_id: int) -> TaskWorkspace | None:
        placeholders = ",".join("?" for _ in _BLOCKING_STATES)
        cursor = await db.conn.execute(
            f"SELECT * FROM task_workspaces WHERE thread_id = ? "
            f"AND state IN ({placeholders}) ORDER BY task_id LIMIT 1",
            (thread_id, *_BLOCKING_STATES),
        )
        row = await cursor.fetchone()
        return TaskWorkspace.from_row(row) if row else None

    async def has_blocking(self, thread_id: int) -> bool:
        return await self.get_blocking(thread_id) is not None

    async def prepare(
        self,
        task_id: int,
        thread_id: int,
        source_workdir: str,
        *,
        allow_initialize: bool,
    ) -> TaskWorkspace:
        existing = await self.get(task_id)
        if existing:
            if existing.state in _BLOCKING_STATES and Path(existing.task_root).is_dir():
                return existing
            raise TaskWorkspaceError(
                f"Task #{task_id} already has a finalized workspace ({existing.state})"
            )
        blocking = await self.get_blocking(thread_id)
        if blocking:
            raise TaskWorkspaceError(
                f"Task #{blocking.task_id} must be accepted or discarded first"
            )

        fields = await asyncio.to_thread(
            _create_snapshot,
            task_id,
            source_workdir,
            allow_initialize,
        )
        created_at = _now()
        try:
            await db.conn.execute(
                "INSERT INTO task_workspaces "
                "(task_id, thread_id, source_workdir, source_root, source_subdir, "
                "task_root, task_workdir, snapshot_commit, state, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                (
                    task_id,
                    thread_id,
                    fields["source_workdir"],
                    fields["source_root"],
                    fields["source_subdir"],
                    fields["task_root"],
                    fields["task_workdir"],
                    fields["snapshot_commit"],
                    created_at,
                ),
            )
            await db.conn.commit()
        except Exception:
            await asyncio.to_thread(_remove_task_tree, task_id, fields["task_root"])
            raise
        workspace = await self.get(task_id)
        assert workspace is not None
        return workspace

    async def _set_state(
        self,
        task_id: int,
        state: str,
        *,
        finalized: bool = False,
        error: str | None = None,
    ) -> None:
        await db.conn.execute(
            "UPDATE task_workspaces SET state = ?, finalized_at = ?, error = ? "
            "WHERE task_id = ?",
            (
                state,
                _now() if finalized else None,
                error[:2000] if error else None,
                task_id,
            ),
        )
        await db.conn.commit()

    async def patch_bytes(self, task_id: int) -> bytes:
        workspace = await self.get(task_id)
        if not workspace:
            raise TaskWorkspaceError(f"Task workspace #{task_id} was not found")
        return await asyncio.to_thread(_build_patch, workspace)

    async def diff(self, task_id: int) -> str:
        return (await self.patch_bytes(task_id)).decode("utf-8", errors="replace")

    async def has_changes(self, task_id: int) -> bool:
        return bool(await self.patch_bytes(task_id))

    async def mark_pending(self, task_id: int) -> None:
        await self._set_state(task_id, "pending")

    async def accept(self, task_id: int) -> bool:
        workspace = await self.get(task_id)
        if not workspace or workspace.state not in _FINALIZABLE_STATES:
            raise TaskWorkspaceError(f"Task #{task_id} has no pending workspace")
        patch = await asyncio.to_thread(_build_patch, workspace)
        if patch:
            try:
                await asyncio.to_thread(_apply_patch, workspace, patch)
            except TaskWorkspaceConflict as exc:
                await self._set_state(task_id, "conflict", error=str(exc))
                raise
        await self._set_state(task_id, "applied", finalized=True)
        try:
            await asyncio.to_thread(_remove_task_tree, task_id, workspace.task_root)
        except Exception:
            # The source and durable state are finalized already. Cleanup must
            # not leave the queue blocked or invite a second apply attempt.
            logger.exception("Failed to remove applied task workspace #%s", task_id)
        return bool(patch)

    async def discard(
        self,
        task_id: int,
        *,
        state: str = "discarded",
        allow_active: bool = False,
    ) -> None:
        workspace = await self.get(task_id)
        if not workspace:
            raise TaskWorkspaceError(f"Task workspace #{task_id} was not found")
        allowed_states = _BLOCKING_STATES if allow_active else _FINALIZABLE_STATES
        if workspace.state not in allowed_states:
            raise TaskWorkspaceError(
                f"Task #{task_id} workspace is already {workspace.state}"
            )
        await asyncio.to_thread(_remove_task_tree, task_id, workspace.task_root)
        await self._set_state(task_id, state, finalized=True)

    async def finalize_if_unchanged(self, task_id: int) -> bool:
        if await self.has_changes(task_id):
            await self.mark_pending(task_id)
            return False
        await self.discard(task_id, state="unchanged", allow_active=True)
        return True

    async def recover(self) -> None:
        placeholders = ",".join("?" for _ in _BLOCKING_STATES)
        cursor = await db.conn.execute(
            f"SELECT * FROM task_workspaces WHERE state IN ({placeholders})",
            _BLOCKING_STATES,
        )
        rows = [TaskWorkspace.from_row(row) for row in await cursor.fetchall()]
        for workspace in rows:
            if not Path(workspace.task_root).is_dir():
                await self._set_state(
                    workspace.task_id,
                    "lost",
                    finalized=True,
                    error="Task workspace is missing after restart",
                )
                continue
            if workspace.state != "active":
                continue
            try:
                if await self.has_changes(workspace.task_id):
                    await self.mark_pending(workspace.task_id)
                else:
                    await self.discard(
                        workspace.task_id,
                        state="interrupted_unchanged",
                        allow_active=True,
                    )
            except Exception as exc:
                logger.exception(
                    "Failed to recover task workspace #%s", workspace.task_id
                )
                await self._set_state(workspace.task_id, "conflict", error=str(exc))


task_workspace_manager = TaskWorkspaceManager()
