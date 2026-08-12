"""Regression tests for the fail-closed AGY Bubblewrap worker."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "antigravity-bot"))
os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")

from bot.config import settings
from bot.services import worker_sandbox


def _worker_identity() -> tuple[int, int]:
    """Use the test process identity; production validation rejects root."""
    return os.geteuid(), os.getegid()


def _configure_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    uid, gid = _worker_identity()
    home = tmp_path / "worker-home"
    (home / ".gemini/config").mkdir(parents=True)
    (home / ".gemini/antigravity-cli/skills").mkdir(parents=True)
    (home / ".gemini/config/mcp_config.json").write_text("{}\n", encoding="utf-8")
    if os.stat(home).st_uid != uid or os.stat(home).st_gid != gid:
        os.chown(home, uid, gid)
    for path in home.rglob("*"):
        if path.is_symlink():
            continue
        if path.stat().st_uid != uid or path.stat().st_gid != gid:
            os.chown(path, uid, gid)

    skills = tmp_path / "skills"
    skills.mkdir()
    task_root = tmp_path / "task-workspaces"
    task_root.mkdir()
    capability_root = tmp_path / "capability"
    capability_root.mkdir()

    monkeypatch.setattr(settings, "agy_sandbox_binary", "/usr/bin/true")
    monkeypatch.setattr(settings, "agy_path", "/usr/bin/true")
    monkeypatch.setattr(settings, "agy_sandbox_python_path", "/usr/bin/python3")
    monkeypatch.setattr(settings, "agy_worker_home", str(home))
    monkeypatch.setattr(settings, "agy_worker_runtime_dir", str(tmp_path / "runtime"))
    monkeypatch.setattr(settings, "agy_worker_uid", uid)
    monkeypatch.setattr(settings, "agy_worker_gid", gid)
    # The test container is root in a restricted user namespace and cannot
    # chown to 65534. Exercise the launch builder with its current identity;
    # production still reaches the real non-root validation in _worker_ids.
    monkeypatch.setattr(worker_sandbox, "_worker_ids", lambda: (uid, gid))
    monkeypatch.setattr(settings, "agy_global_skills_dir", str(skills))
    monkeypatch.setattr(settings, "task_workspaces_dir", str(task_root))
    return task_root, capability_root


def _ro_bind_index(command: list[str], source: str, destination: str) -> int:
    for index in range(len(command) - 2):
        if command[index : index + 3] == ["--ro-bind", source, destination]:
            return index
    raise AssertionError(f"missing read-only bind {source} -> {destination}")


def test_sandbox_launch_strips_bot_environment_and_exposes_only_capabilities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_root, capability_root = _configure_worker(tmp_path, monkeypatch)
    workspace = task_root / "task-42"
    workspace.mkdir()
    (workspace / "project.txt").write_text("safe task copy", encoding="utf-8")
    monkeypatch.setenv("BOT_TOKEN", "do-not-leak-this-token")
    monkeypatch.setenv("AGY_BOT_DB_PATH", "/private/bot.db")

    launch = worker_sandbox.build_sandbox_launch(
        agy_args=["/usr/bin/true", "--print", "inspect"],
        execution_dir=str(workspace),
        capability_dir=capability_root,
        capability_token="per-task-secret",
        thread_id=42,
    )

    command = list(launch.command)
    assert "--unshare-user" in command
    assert "--disable-userns" in command
    assert "--unshare-pid" in command
    assert "--clearenv" in command
    assert "--bind" in command
    assert str(workspace) in command
    assert "/workspace" in command
    assert str(capability_root) in command
    assert "/run/ag-capabilities" in command
    assert command[command.index("--symlink") + 1 : command.index("--symlink") + 3] == [
        "/opt/ag-worker-tools/capability_client.py",
        "/opt/ag-worker-tools/ag-ssh",
    ]
    path_index = command.index("PATH")
    assert command[path_index + 1].startswith("/opt/ag-worker-tools:")
    assert "AGY_CAPABILITY_TOKEN" in command
    assert "per-task-secret" in command
    assert "AGY_BOT_ROOT" not in command
    assert "AGY_BOT_DB_PATH" not in command
    assert str(ROOT / "antigravity-bot") not in command
    assert _ro_bind_index(command, str(capability_root / "etc" / "passwd"), "/etc/passwd")
    assert "/etc/hosts" in command

    # The outer process does not inherit service secrets either; bwrap clears
    # the inner environment once more before it launches AGY.
    assert "BOT_TOKEN" not in launch.env
    assert "AGY_BOT_DB_PATH" not in launch.env
    assert "do-not-leak-this-token" not in launch.env.values()
    assert "per-task-secret" not in launch.env.values()

    command_start = command.index("--") + 1
    assert str(workspace) not in command[command_start:]

    mcp_config = json.loads((capability_root / "mcp_config.json").read_text())
    server = mcp_config["mcpServers"]["ag-telegram-memory"]
    assert server["args"] == [
        "/opt/ag-worker-tools/capability_client.py",
        "mcp-memory",
    ]
    assert "db-path" not in json.dumps(mcp_config)
    assert "bot.services" not in json.dumps(mcp_config)
    assert (capability_root / "etc" / "hosts").read_text(encoding="utf-8") == (
        "127.0.0.1 localhost\n::1 localhost\n"
    )
    assert "root:" not in (capability_root / "etc" / "passwd").read_text(
        encoding="utf-8"
    )


def test_sandbox_refuses_an_unmanaged_execution_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _task_root, capability_root = _configure_worker(tmp_path, monkeypatch)
    unmanaged = tmp_path / "not-managed-by-bot"
    unmanaged.mkdir()

    with pytest.raises(worker_sandbox.WorkerSandboxError, match="outside"):
        worker_sandbox.build_sandbox_launch(
            agy_args=["/usr/bin/true", "--print", "inspect"],
            execution_dir=str(unmanaged),
            capability_dir=capability_root,
            capability_token="token",
            thread_id=None,
        )


def test_sandbox_rejects_cli_outside_dedicated_runtime_or_usr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _task_root, _capability_root = _configure_worker(tmp_path, monkeypatch)
    outside = tmp_path / "root-like-home" / "agy"
    outside.parent.mkdir()
    outside.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    outside.chmod(0o755)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(settings, "agy_path", str(outside))
    monkeypatch.setattr(settings, "agy_worker_runtime_dir", str(runtime))

    with pytest.raises(worker_sandbox.WorkerSandboxError, match="AGY_PATH must be inside"):
        worker_sandbox.validate_sandbox_configuration()


def test_sandbox_mounts_a_dedicated_cli_runtime_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_root, capability_root = _configure_worker(tmp_path, monkeypatch)
    runtime = tmp_path / "runtime"
    executable = runtime / "bin" / "agy"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    workspace = task_root / "task-13"
    workspace.mkdir()
    monkeypatch.setattr(settings, "agy_path", str(executable))
    monkeypatch.setattr(settings, "agy_worker_runtime_dir", str(runtime))

    launch = worker_sandbox.build_sandbox_launch(
        agy_args=[str(executable), "--print", "inspect"],
        execution_dir=str(workspace),
        capability_dir=capability_root,
        capability_token="token",
        thread_id=13,
    )

    command = list(launch.command)
    _ro_bind_index(command, str(runtime.resolve()), "/opt/ag-worker/runtime")
    assert command[-3:] == ["/opt/ag-worker/runtime/bin/agy", "--print", "inspect"]


def test_sandbox_rewrites_add_dir_to_its_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_root, capability_root = _configure_worker(tmp_path, monkeypatch)
    workspace = task_root / "task-14"
    workspace.mkdir()

    launch = worker_sandbox.build_sandbox_launch(
        agy_args=["/usr/bin/true", "--add-dir", str(workspace), "--print", "inspect"],
        execution_dir=str(workspace),
        capability_dir=capability_root,
        capability_token="token",
        thread_id=14,
    )

    command = list(launch.command)
    command_start = command.index("--") + 1
    inner_command = command[command_start:]
    assert inner_command == [
        "/usr/bin/true",
        "--add-dir",
        "/workspace",
        "--print",
        "inspect",
    ]


def test_production_worker_identity_cannot_be_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "agy_worker_uid", 0)
    monkeypatch.setattr(settings, "agy_worker_gid", 0)

    with pytest.raises(worker_sandbox.WorkerSandboxError, match="non-root"):
        worker_sandbox._worker_ids()
