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


def _configure_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path]:
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

    task_root = tmp_path / "task-workspaces"
    task_root.mkdir()
    artifacts_root = tmp_path / "task-artifacts"
    artifacts_root.mkdir()
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
    # CI normally runs as an unprivileged account.  Unit tests exercise the
    # command builder, while production explicitly requires the root-owned bot
    # service to construct the mount namespace before it drops to agyworker.
    monkeypatch.setattr(worker_sandbox, "_require_root_launcher", lambda: None)
    monkeypatch.setattr(settings, "task_workspaces_dir", str(task_root))
    monkeypatch.setattr(settings, "task_artifacts_dir", str(artifacts_root))
    return task_root, capability_root, artifacts_root


def _artifact_dir(root: Path, name: str) -> Path:
    target = root / name
    target.mkdir()
    return target


def _ro_bind_index(command: list[str], source: str, destination: str) -> int:
    for index in range(len(command) - 2):
        if command[index : index + 3] == ["--ro-bind", source, destination]:
            return index
    raise AssertionError(f"missing read-only bind {source} -> {destination}")


def test_sandbox_launch_strips_bot_environment_and_exposes_only_capabilities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_root, capability_root, artifacts_root = _configure_worker(tmp_path, monkeypatch)
    workspace = task_root / "task-42"
    workspace.mkdir()
    artifact_dir = _artifact_dir(artifacts_root, "task-42")
    (workspace / "project.txt").write_text("safe task copy", encoding="utf-8")
    monkeypatch.setenv("BOT_TOKEN", "do-not-leak-this-token")
    monkeypatch.setenv("AGY_BOT_DB_PATH", "/private/bot.db")

    launch = worker_sandbox.build_sandbox_launch(
        agy_args=["/usr/bin/true", "--print", "inspect"],
        execution_dir=str(workspace),
        artifact_dir=artifact_dir,
        capability_dir=capability_root,
        capability_token="per-task-secret",
        thread_id=42,
    )

    command = list(launch.command)
    assert "--unshare-user" not in command
    assert "--disable-userns" not in command
    assert "--unshare-pid" in command
    assert "--clearenv" in command
    assert "--uid" not in command
    assert "--gid" not in command
    assert "--bind" in command
    assert str(workspace) in command
    assert "/workspace" in command
    assert str(capability_root) in command
    assert "/run/ag-capabilities" in command
    assert str(artifact_dir) in command
    assert "/run/ag-artifacts" in command
    assert "/home/agy/.gemini/antigravity-cli/scratch" in command
    assert command[command.index("--symlink") + 1 : command.index("--symlink") + 3] == [
        "/opt/ag-worker-tools/capability_client.py",
        "/opt/ag-worker-tools/ag-ssh",
    ]
    path_index = command.index("PATH")
    assert command[path_index + 1].startswith("/opt/ag-worker-tools:")
    assert "AGY_CAPABILITY_TOKEN" in command
    assert "per-task-secret" in command
    assert command[command.index("AGY_ARTIFACT_DIR") + 1] == "/run/ag-artifacts"
    assert command[command.index("TMPDIR") + 1] == "/run/ag-artifacts"
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
    assert not hasattr(launch, "preexec_fn")

    command_start = command.index("--") + 1
    assert str(workspace) not in command[command_start:]
    uid, gid = _worker_identity()
    assert command[command_start : command_start + 9] == [
        "/usr/bin/setpriv",
        f"--reuid={uid}",
        f"--regid={gid}",
        "--clear-groups",
        "--inh-caps=-all",
        "--ambient-caps=-all",
        "--bounding-set=-all",
        "--no-new-privs",
        "--",
    ]

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
    assert f"agyworker:x:{uid}:{gid}:" in (
        capability_root / "etc" / "passwd"
    ).read_text(encoding="utf-8")


def test_sandbox_refuses_an_unmanaged_execution_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _task_root, capability_root, artifacts_root = _configure_worker(tmp_path, monkeypatch)
    unmanaged = tmp_path / "not-managed-by-bot"
    unmanaged.mkdir()
    artifact_dir = _artifact_dir(artifacts_root, "task-43")

    with pytest.raises(worker_sandbox.WorkerSandboxError, match="outside"):
        worker_sandbox.build_sandbox_launch(
            agy_args=["/usr/bin/true", "--print", "inspect"],
            execution_dir=str(unmanaged),
            artifact_dir=artifact_dir,
            capability_dir=capability_root,
            capability_token="token",
            thread_id=None,
        )


def test_sandbox_rejects_cli_outside_dedicated_runtime_or_usr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _task_root, _capability_root, _artifacts_root = _configure_worker(tmp_path, monkeypatch)
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
    task_root, capability_root, artifacts_root = _configure_worker(tmp_path, monkeypatch)
    runtime = tmp_path / "runtime"
    executable = runtime / "bin" / "agy"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    workspace = task_root / "task-13"
    workspace.mkdir()
    artifact_dir = _artifact_dir(artifacts_root, "task-13")
    monkeypatch.setattr(settings, "agy_path", str(executable))
    monkeypatch.setattr(settings, "agy_worker_runtime_dir", str(runtime))

    launch = worker_sandbox.build_sandbox_launch(
        agy_args=[str(executable), "--print", "inspect"],
        execution_dir=str(workspace),
        artifact_dir=artifact_dir,
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
    task_root, capability_root, artifacts_root = _configure_worker(tmp_path, monkeypatch)
    workspace = task_root / "task-14"
    workspace.mkdir()
    artifact_dir = _artifact_dir(artifacts_root, "task-14")

    launch = worker_sandbox.build_sandbox_launch(
        agy_args=["/usr/bin/true", "--add-dir", str(workspace), "--print", "inspect"],
        execution_dir=str(workspace),
        artifact_dir=artifact_dir,
        capability_dir=capability_root,
        capability_token="token",
        thread_id=14,
    )

    command = list(launch.command)
    command_start = command.index("--") + 1
    inner_command = command[command_start:]
    assert inner_command[-5:] == [
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


def test_root_service_is_required_to_construct_the_bubblewrap_mount_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_sandbox.os, "geteuid", lambda: 999)

    with pytest.raises(worker_sandbox.WorkerSandboxError, match="must run as root"):
        worker_sandbox._require_root_launcher()


def test_privilege_drop_command_clears_worker_privileges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_sandbox, "_worker_ids", lambda: (999, 988))

    assert worker_sandbox._worker_privilege_drop_command() == [
        "/usr/bin/setpriv",
        "--reuid=999",
        "--regid=988",
        "--clear-groups",
        "--inh-caps=-all",
        "--ambient-caps=-all",
        "--bounding-set=-all",
        "--no-new-privs",
        "--",
    ]


def test_worker_home_rejects_root_owned_internal_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_worker(tmp_path, monkeypatch)
    home = Path(settings.agy_worker_home)
    broken_directory = home / ".gemini" / "antigravity-cli"
    real_lstat = Path.lstat

    def lstat_with_root_owned_cli(path: Path) -> os.stat_result:
        metadata = real_lstat(path)
        if path == broken_directory:
            values = list(metadata)
            values[4] = metadata.st_uid + 1
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(Path, "lstat", lstat_with_root_owned_cli)

    with pytest.raises(worker_sandbox.WorkerSandboxError, match="must be owned"):
        worker_sandbox._worker_home()


def test_worker_home_rejects_non_writable_internal_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_worker(tmp_path, monkeypatch)
    home = Path(settings.agy_worker_home)
    (home / ".gemini" / "antigravity-cli").chmod(0o550)

    with pytest.raises(worker_sandbox.WorkerSandboxError, match="must grant"):
        worker_sandbox._worker_home()


def test_bubblewrap_probe_includes_the_dynamic_loader_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_worker(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "agy_sandbox_binary", "/usr/bin/true")

    command = worker_sandbox._bubblewrap_probe_command()

    assert command[:3] == ["/usr/bin/true", "--die-with-parent", "--new-session"]
    assert "--unshare-user" not in command
    assert "--uid" not in command
    assert "--gid" not in command
    assert "--clearenv" in command
    assert _ro_bind_index(command, "/usr", "/usr")
    assert _ro_bind_index(command, "/bin", "/bin")
    assert _ro_bind_index(command, "/lib", "/lib")
    assert _ro_bind_index(command, "/lib64", "/lib64")
    assert any(
        command[index : index + 3]
        == ["--bind", str(Path(settings.agy_worker_home)), "/home/agy"]
        for index in range(len(command) - 2)
    )
    assert "/usr/bin/setpriv" in command
    assert command[-2:] == ["/usr/bin/id", "-u"]


def test_runtime_probe_rejects_a_process_that_did_not_drop_to_worker_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_sandbox, "_worker_ids", lambda: (999, 988))
    monkeypatch.setattr(worker_sandbox, "_bubblewrap_probe_command", lambda: ["probe"])
    monkeypatch.setattr(
        worker_sandbox.subprocess,
        "run",
        lambda *_args, **_kwargs: worker_sandbox.subprocess.CompletedProcess(
            ["probe"], 0, stdout="0\n", stderr=""
        ),
    )

    with pytest.raises(worker_sandbox.WorkerSandboxError, match="did not drop"):
        worker_sandbox._verify_bubblewrap_runtime()
