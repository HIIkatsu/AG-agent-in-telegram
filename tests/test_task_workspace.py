"""Regression tests for task-scoped source isolation and safe Git finalization."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "antigravity-bot"))
os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")

from bot.config import settings
from bot.db import db
from bot.services.artifacts import is_managed_workspace_path
from bot.services.git_manager import GitManager, GitRepositoryRequired
from bot.services.task_workspace import (
    TaskWorkspace,
    TaskWorkspaceConflict,
    TaskWorkspaceError,
    _apply_patch,
    _build_patch,
    _create_snapshot,
    _remove_task_tree,
    task_workspace_manager,
)
from bot.services.tracker import build_tracker_kb


def _git(repo: Path, *args: str, input_data: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input_data,
        capture_output=True,
        check=True,
    ).stdout


def _init_repo(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "--quiet")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.invalid")
    (path / ".gitignore").write_text(".env\nignored/\n", encoding="utf-8")
    (path / "app.txt").write_text("base\n", encoding="utf-8")
    (path / "stable.txt").write_text("stable\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "--quiet", "-m", "base")


def _workspace(task_id: int, fields: dict[str, str]) -> TaskWorkspace:
    return TaskWorkspace(
        task_id=task_id,
        thread_id=42,
        state="active",
        created_at="now",
        **fields,
    )


def test_task_snapshot_and_apply_preserve_source_git_state(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _init_repo(source)
    original_head = _git(source, "rev-parse", "HEAD")

    # Pre-existing user work is the task baseline, including staged and
    # untracked files. Ignored secrets must never enter the task workspace.
    (source / "app.txt").write_text("user baseline\n", encoding="utf-8")
    _git(source, "add", "app.txt")
    (source / "notes.txt").write_text("user notes\n", encoding="utf-8")
    (source / ".env").write_text("TOKEN=super-secret\n", encoding="utf-8")
    source_index_before = (source / ".git" / "index").read_bytes()
    source_status_before = _git(
        source, "status", "--porcelain", "--untracked-files=all"
    )

    old_task_root = settings.task_workspaces_dir
    settings.task_workspaces_dir = str(tmp_path / "task-roots")
    try:
        fields = _create_snapshot(7, str(source), False)
        workspace = _workspace(7, fields)
        task_root = Path(workspace.task_root)

        assert (task_root / "app.txt").read_text(encoding="utf-8") == "user baseline\n"
        assert (task_root / "notes.txt").read_text(encoding="utf-8") == "user notes\n"
        assert not (task_root / ".env").exists()
        assert "remote" not in _git(task_root, "config", "--list").decode()
        assert _git(source, "rev-parse", "HEAD") == original_head
        assert (source / ".git" / "index").read_bytes() == source_index_before
        assert (
            _git(source, "status", "--porcelain", "--untracked-files=all")
            == source_status_before
        )

        # Both committed and uncommitted task work belong to the same delta.
        (task_root / "app.txt").write_text("agent result\n", encoding="utf-8")
        _git(task_root, "add", "app.txt")
        _git(task_root, "commit", "--quiet", "-m", "agent commit")
        (task_root / "created.py").write_text("print('task')\n", encoding="utf-8")

        # A concurrent, unrelated source edit must survive acceptance.
        (source / "later.txt").write_text("user later\n", encoding="utf-8")
        patch = _build_patch(workspace)
        assert b"agent result" in patch
        assert b"created.py" in patch
        assert _apply_patch(workspace, patch)

        assert (source / "app.txt").read_text(encoding="utf-8") == "agent result\n"
        assert _git(source, "show", ":app.txt") == b"user baseline\n"
        assert (source / "notes.txt").read_text(encoding="utf-8") == "user notes\n"
        assert (source / "later.txt").read_text(encoding="utf-8") == "user later\n"
        assert (source / "created.py").read_text(encoding="utf-8") == "print('task')\n"
        assert (source / ".env").read_text(encoding="utf-8") == "TOKEN=super-secret\n"
        assert _git(source, "rev-parse", "HEAD") == original_head

        # A retry after a crash between apply and DB finalization is idempotent.
        assert not _apply_patch(workspace, patch)

        _remove_task_tree(7, workspace.task_root)
        assert source.is_dir()
        assert not task_root.exists()
    finally:
        settings.task_workspaces_dir = old_task_root


def test_conflicting_source_edit_is_never_overwritten(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _init_repo(source)
    old_task_root = settings.task_workspaces_dir
    settings.task_workspaces_dir = str(tmp_path / "task-roots")
    try:
        workspace = _workspace(8, _create_snapshot(8, str(source), False))
        (Path(workspace.task_root) / "app.txt").write_text("agent\n", encoding="utf-8")
        patch = _build_patch(workspace)
        (source / "app.txt").write_text("user concurrent edit\n", encoding="utf-8")

        with pytest.raises(TaskWorkspaceConflict):
            _apply_patch(workspace, patch)

        assert (source / "app.txt").read_text(
            encoding="utf-8"
        ) == "user concurrent edit\n"
        assert Path(workspace.task_root).is_dir()
        _remove_task_tree(8, workspace.task_root)
    finally:
        settings.task_workspaces_dir = old_task_root


def test_binary_deletion_and_executable_mode_apply_from_one_patch(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _init_repo(source)
    (source / "image.bin").write_bytes(b"\x00old\xff")
    script = source / "tool.sh"
    script.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
    script.chmod(0o644)
    _git(source, "add", ".")
    _git(source, "commit", "--quiet", "-m", "binary fixtures")

    old_task_root = settings.task_workspaces_dir
    settings.task_workspaces_dir = str(tmp_path / "task-roots")
    try:
        workspace = _workspace(10, _create_snapshot(10, str(source), False))
        task_root = Path(workspace.task_root)
        (task_root / "image.bin").write_bytes(b"\x00new\xfe")
        (task_root / "stable.txt").unlink()
        (task_root / "tool.sh").chmod(0o755)

        patch = _build_patch(workspace)
        assert b"GIT binary patch" in patch
        assert _apply_patch(workspace, patch)

        assert (source / "image.bin").read_bytes() == b"\x00new\xfe"
        assert not (source / "stable.txt").exists()
        assert (source / "tool.sh").stat().st_mode & 0o111
        _remove_task_tree(10, workspace.task_root)
    finally:
        settings.task_workspaces_dir = old_task_root


def test_mounted_git_subdirectory_keeps_the_same_task_cwd(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _init_repo(source)
    mounted = source / "packages" / "app"
    mounted.mkdir(parents=True)
    (mounted / "main.py").write_text("before = True\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "--quiet", "-m", "nested project")

    old_task_root = settings.task_workspaces_dir
    settings.task_workspaces_dir = str(tmp_path / "task-roots")
    try:
        workspace = _workspace(11, _create_snapshot(11, str(mounted), False))
        assert workspace.source_subdir == "packages/app"
        assert Path(workspace.task_workdir).relative_to(workspace.task_root) == Path(
            "packages/app"
        )
        task_file = Path(workspace.task_workdir) / "main.py"
        task_file.write_text("after = True\n", encoding="utf-8")
        assert _apply_patch(workspace, _build_patch(workspace))
        assert (mounted / "main.py").read_text(encoding="utf-8") == "after = True\n"
        _remove_task_tree(11, workspace.task_root)
    finally:
        settings.task_workspaces_dir = old_task_root


def test_snapshot_rejects_symlinks_that_escape_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _init_repo(source)
    (source / "escape").symlink_to("../outside")
    _git(source, "add", "escape")
    _git(source, "commit", "--quiet", "-m", "unsafe symlink")
    old_task_root = settings.task_workspaces_dir
    settings.task_workspaces_dir = str(tmp_path / "task-roots")
    try:
        with pytest.raises(TaskWorkspaceError, match="escapes"):
            _create_snapshot(9, str(source), False)
        assert not any((tmp_path / "task-roots").glob("*/task-9"))
    finally:
        settings.task_workspaces_dir = old_task_root


def test_git_read_operations_do_not_initialize_or_mutate_workspace(
    tmp_path: Path,
) -> None:
    manager = GitManager()
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "keep.txt").write_text("mine\n", encoding="utf-8")

    with pytest.raises(GitRepositoryRequired):
        manager.status(str(plain))

    assert not (plain / ".git").exists()
    assert (plain / "keep.txt").read_text(encoding="utf-8") == "mine\n"
    assert not hasattr(manager, "rollback")
    assert not hasattr(manager, "rollback_to_commit")
    assert not hasattr(manager, "accept")

    source = tmp_path / "source"
    _init_repo(source)
    (source / "app.txt").write_text("staged\n", encoding="utf-8")
    _git(source, "add", "app.txt")
    (source / "untracked.txt").write_text("new\n", encoding="utf-8")
    index_before = (source / ".git" / "index").read_bytes()
    head_before = _git(source, "rev-parse", "HEAD")

    assert manager.status(str(source))
    assert "staged" in manager.get_diff(str(source))
    assert (source / ".git" / "index").read_bytes() == index_before
    assert _git(source, "rev-parse", "HEAD") == head_before


def test_review_buttons_are_scoped_to_task_id() -> None:
    keyboard = build_tracker_kb(
        thread_id=42,
        status="done",
        task_id=17,
        has_changes=True,
    )
    assert keyboard is not None
    callbacks = {
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    }
    assert callbacks == {"t:df:17", "t:ac:17", "t:rb:17"}
    assert all(not value.startswith("rollback:") for value in callbacks)


def test_managed_workspace_check_uses_path_boundaries(tmp_path: Path) -> None:
    old_source_root = settings.workspaces_dir
    old_task_root = settings.task_workspaces_dir
    settings.workspaces_dir = str(tmp_path / "sources")
    settings.task_workspaces_dir = str(tmp_path / "tasks")
    try:
        assert is_managed_workspace_path(tmp_path / "sources" / "project" / "file.txt")
        assert is_managed_workspace_path(tmp_path / "tasks" / "abc" / "task-1")
        assert not is_managed_workspace_path(tmp_path / "sources-evil" / "file.txt")
        assert not is_managed_workspace_path(tmp_path / "scratch" / "file.txt")
    finally:
        settings.workspaces_dir = old_source_root
        settings.task_workspaces_dir = old_task_root


def test_manager_persists_and_applies_only_one_task_delta(tmp_path: Path) -> None:
    async def exercise() -> None:
        source = tmp_path / "source"
        _init_repo(source)
        old_db_path = db._path
        old_task_root = settings.task_workspaces_dir
        db._path = str(tmp_path / "bot.db")
        settings.task_workspaces_dir = str(tmp_path / "task-roots")
        await db.connect()
        try:
            await db.conn.execute(
                "INSERT INTO tasks (id, thread_id, prompt, status, mode) "
                "VALUES (17, 42, 'edit app', 'running', 'code')"
            )
            await db.conn.commit()
            workspace = await task_workspace_manager.prepare(
                17,
                42,
                str(source),
                allow_initialize=False,
            )
            with pytest.raises(TaskWorkspaceError, match="no pending workspace"):
                await task_workspace_manager.accept(17)
            with pytest.raises(TaskWorkspaceError, match="already active"):
                await task_workspace_manager.discard(17)
            (Path(workspace.task_root) / "stable.txt").write_text(
                "changed by task\n", encoding="utf-8"
            )
            assert await task_workspace_manager.has_changes(17)
            await task_workspace_manager.mark_pending(17)
            assert (await task_workspace_manager.get_blocking(42)).task_id == 17  # type: ignore[union-attr]

            assert await task_workspace_manager.accept(17)
            finalized = await task_workspace_manager.get(17)
            assert finalized and finalized.state == "applied"
            assert finalized.finalized_at
            assert not Path(workspace.task_root).exists()
            assert (source / "stable.txt").read_text(
                encoding="utf-8"
            ) == "changed by task\n"
            assert await task_workspace_manager.get_blocking(42) is None
        finally:
            await db.close()
            db._path = old_db_path
            settings.task_workspaces_dir = old_task_root

    asyncio.run(exercise())


def test_restart_recovery_keeps_changed_workspace_and_cleans_unchanged(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = tmp_path / "source"
        _init_repo(source)
        old_db_path = db._path
        old_task_root = settings.task_workspaces_dir
        db._path = str(tmp_path / "recovery.db")
        settings.task_workspaces_dir = str(tmp_path / "task-roots")
        await db.connect()
        try:
            await db.conn.executemany(
                "INSERT INTO tasks (id, thread_id, prompt, status, mode) "
                "VALUES (?, ?, 'task', 'interrupted', 'code')",
                [(20, 50), (21, 51)],
            )
            await db.conn.commit()
            changed = await task_workspace_manager.prepare(
                20, 50, str(source), allow_initialize=False
            )
            unchanged = await task_workspace_manager.prepare(
                21, 51, str(source), allow_initialize=False
            )
            (Path(changed.task_root) / "app.txt").write_text(
                "interrupted change\n", encoding="utf-8"
            )

            await task_workspace_manager.recover()

            changed_after = await task_workspace_manager.get(20)
            unchanged_after = await task_workspace_manager.get(21)
            assert changed_after and changed_after.state == "pending"
            assert Path(changed.task_root).is_dir()
            assert unchanged_after and unchanged_after.state == "interrupted_unchanged"
            assert not Path(unchanged.task_root).exists()
            await task_workspace_manager.discard(20)
        finally:
            await db.close()
            db._path = old_db_path
            settings.task_workspaces_dir = old_task_root

    asyncio.run(exercise())
