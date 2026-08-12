"""Build a fail-closed Bubblewrap launch for an AGY task worker.

The Telegram bot itself retains credentials, the SQLite database and SSH keys.
The model process sees a fresh mount namespace with only its task workspace,
the dedicated AGY worker home, read-only bundled skills and a per-task
capability socket.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from bot.config import settings

BOT_ROOT = Path(__file__).resolve().parents[2]
WORKER_TOOLS_DIR = BOT_ROOT / "worker_tools"
CAPABILITY_CLIENT = WORKER_TOOLS_DIR / "capability_client.py"

_SANDBOX_RUNTIME_DIR = "/opt/ag-worker/runtime"
_SANDBOX_HOME = "/home/agy"
_SANDBOX_WORKSPACE = "/workspace"
_SANDBOX_CAPABILITIES = "/run/ag-capabilities"
_SANDBOX_CLIENT_DIR = "/opt/ag-worker-tools"
_SANDBOX_CLIENT_PATH = "/opt/ag-worker-tools/capability_client.py"
_SANDBOX_MCP_CONFIG = f"{_SANDBOX_HOME}/.gemini/config/mcp_config.json"


class WorkerSandboxError(RuntimeError):
    """The secure AGY worker cannot be prepared safely."""


@dataclass(frozen=True)
class SandboxLaunch:
    """A complete command and the deliberately small parent environment."""

    command: tuple[str, ...]
    env: dict[str, str]
    cwd: str


def _resolved_existing(path: str | Path, label: str, *, executable: bool = False) -> Path:
    target = Path(path).expanduser().resolve()
    if not target.exists():
        raise WorkerSandboxError(f"{label} does not exist: {target}")
    if executable and (not target.is_file() or not os.access(target, os.X_OK)):
        raise WorkerSandboxError(f"{label} is not an executable file: {target}")
    return target


def _require_directory(path: str | Path, label: str) -> Path:
    target = _resolved_existing(path, label)
    if not target.is_dir():
        raise WorkerSandboxError(f"{label} is not a directory: {target}")
    return target


def _worker_ids() -> tuple[int, int]:
    uid = settings.agy_worker_uid
    gid = settings.agy_worker_gid
    if uid <= 0 or gid <= 0:
        raise WorkerSandboxError("AGY worker UID and GID must be non-root positive IDs")
    return uid, gid


def _worker_home() -> Path:
    uid, gid = _worker_ids()
    home = _require_directory(settings.agy_worker_home, "AGY worker home")
    metadata = home.stat()
    if metadata.st_uid != uid or metadata.st_gid != gid:
        raise WorkerSandboxError(
            "AGY worker home must be owned by AGY_WORKER_UID:AGY_WORKER_GID"
        )
    if stat.S_IMODE(metadata.st_mode) & stat.S_IWOTH:
        raise WorkerSandboxError("AGY worker home must not be world-writable")
    for relative in (
        ".gemini",
        ".gemini/config",
        ".gemini/antigravity-cli",
        ".gemini/antigravity-cli/skills",
    ):
        target = home / relative
        if not target.is_dir():
            raise WorkerSandboxError(
                f"AGY worker home is missing required directory: {target}"
            )
    mcp_config = home / ".gemini/config/mcp_config.json"
    if not mcp_config.is_file():
        raise WorkerSandboxError(
            "AGY worker home is missing required MCP config placeholder: "
            f"{mcp_config}"
        )
    return home


def _skills_dir() -> Path:
    return _require_directory(settings.agy_global_skills_dir, "AGY global skills directory")


def _worker_python() -> Path:
    python_path = _resolved_existing(
        settings.agy_sandbox_python_path,
        "AGY sandbox Python",
        executable=True,
    )
    try:
        python_path.relative_to("/usr")
    except ValueError as exc:
        raise WorkerSandboxError(
            "AGY_SANDBOX_PYTHON_PATH must be inside /usr so it is mounted read-only"
        ) from exc
    return python_path


def _agy_source_and_sandbox_path() -> tuple[Path, str, Path | None]:
    """Return a safe source executable and the corresponding sandbox path.

    A CLI installed below /usr can use the already read-only system runtime.
    Other installations must live in a dedicated worker runtime directory; this
    prevents accidentally mounting a root home directory just to make a CLI
    launcher or its Node/Python dependencies work.
    """
    agy_path = _resolved_existing(settings.agy_path, "AGY executable", executable=True)
    try:
        agy_path.relative_to("/usr")
    except ValueError:
        runtime = _require_directory(
            settings.agy_worker_runtime_dir,
            "AGY worker runtime directory",
        )
        runtime_metadata = runtime.stat()
        if stat.S_IMODE(runtime_metadata.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
            raise WorkerSandboxError(
                "AGY worker runtime directory must not be group- or world-writable"
            )
        try:
            relative_path = agy_path.relative_to(runtime)
        except ValueError as exc:
            raise WorkerSandboxError(
                "AGY_PATH must be inside AGY_WORKER_RUNTIME_DIR or /usr when the "
                "sandbox is enabled"
            ) from exc
        return agy_path, f"{_SANDBOX_RUNTIME_DIR}/{relative_path}", runtime
    return agy_path, str(agy_path), None


def _assert_execution_dir(execution_dir: str) -> Path:
    target = _require_directory(execution_dir, "Task execution directory")
    roots = (
        Path(settings.task_workspaces_dir).expanduser().resolve(),
        (Path(tempfile.gettempdir()) / "antigravity-chat").resolve(),
    )
    for root in roots:
        try:
            target.relative_to(root)
            return target
        except ValueError:
            continue
    raise WorkerSandboxError(
        "Task execution directory is outside the bot-managed worker roots"
    )


def prepare_execution_directory(execution_dir: str) -> Path:
    """Give the unprivileged worker ownership only of its isolated workspace."""
    target = _assert_execution_dir(execution_dir)
    uid, gid = _worker_ids()
    try:
        for root, directories, files in os.walk(target, followlinks=False):
            root_path = Path(root)
            os.chown(root_path, uid, gid)
            os.chmod(root_path, stat.S_IMODE(root_path.stat().st_mode) | stat.S_IRWXU)
            for name in [*directories, *files]:
                entry = root_path / name
                metadata = entry.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    os.lchown(entry, uid, gid)
                    continue
                os.chown(entry, uid, gid)
                mode = stat.S_IMODE(metadata.st_mode) | stat.S_IRUSR | stat.S_IWUSR
                if stat.S_ISDIR(metadata.st_mode):
                    mode |= stat.S_IXUSR
                os.chmod(entry, mode)
    except OSError as exc:
        raise WorkerSandboxError(
            f"Cannot grant worker access to isolated task workspace: {exc}"
        ) from exc
    return target


def validate_sandbox_configuration() -> None:
    """Validate prerequisites without starting an AGY task or reading secrets."""
    _resolved_existing(settings.agy_sandbox_binary, "Bubblewrap executable", executable=True)
    _agy_source_and_sandbox_path()
    _worker_home()
    _skills_dir()
    _worker_python()
    if not CAPABILITY_CLIENT.is_file():
        raise WorkerSandboxError(f"Capability client is missing: {CAPABILITY_CLIENT}")


def _write_mcp_config(capability_dir: Path) -> Path:
    """Create the worker-only MCP config with no database path or bot module."""
    target = capability_dir / "mcp_config.json"
    payload = {
        "mcpServers": {
            "ag-telegram-memory": {
                "command": str(_worker_python()),
                "args": [_SANDBOX_CLIENT_PATH, "mcp-memory"],
                "cwd": _SANDBOX_WORKSPACE,
            }
        }
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(target, 0o644)
    return target


def _write_sandbox_etc(capability_dir: Path, uid: int, gid: int) -> Path:
    """Create the few non-secret /etc files the isolated runtime needs."""
    target = capability_dir / "etc"
    target.mkdir(mode=0o755, exist_ok=True)
    files = {
        "passwd": f"agyworker:x:{uid}:{gid}:AGY worker:{_SANDBOX_HOME}:/usr/sbin/nologin\n",
        "group": f"agyworker:x:{gid}:\n",
        "hosts": "127.0.0.1 localhost\n::1 localhost\n",
        "nsswitch.conf": "passwd: files\ngroup: files\nhosts: files dns\n",
    }
    for name, content in files.items():
        path = target / name
        path.write_text(content, encoding="utf-8")
        os.chmod(path, 0o644)
    return target


def _read_only_mounts() -> list[tuple[str, str]]:
    """Return the minimal host runtime that an AGY binary normally needs."""
    mounts: list[tuple[str, str]] = []
    for raw_path in (
        "/usr",
        "/bin",
        "/lib",
        "/lib64",
        "/etc/ssl/certs",
        "/etc/resolv.conf",
    ):
        source = Path(raw_path)
        if source.exists() or source.is_symlink():
            mounts.append((raw_path, raw_path))
    return mounts


def _sandbox_environment(thread_id: int | None) -> dict[str, str]:
    """Environment for Bubblewrap itself; it intentionally inherits nothing."""
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "dumb",
        "NO_COLOR": "1",
        "PYTHONIOENCODING": "utf-8",
        "AGY_TG_THREAD_ID": str(thread_id) if thread_id is not None else "",
    }


def _sandboxify_agy_arguments(agy_args: list[str]) -> list[str]:
    """Replace host-only AGY path arguments with their sandbox counterpart."""
    result = list(agy_args)
    for index, value in enumerate(result[:-1]):
        if value == "--add-dir":
            result[index + 1] = _SANDBOX_WORKSPACE
    return result


def build_sandbox_launch(
    *,
    agy_args: list[str],
    execution_dir: str,
    capability_dir: Path,
    capability_token: str,
    thread_id: int | None,
) -> SandboxLaunch:
    """Build the fail-closed Bubblewrap command for exactly one AGY task."""
    if not agy_args:
        raise WorkerSandboxError("AGY command cannot be empty")
    validate_sandbox_configuration()
    workspace = prepare_execution_directory(execution_dir)
    worker_home = _worker_home()
    skills_dir = _skills_dir()
    _agy_path, sandbox_agy_path, agy_runtime_dir = _agy_source_and_sandbox_path()
    capability_root = _require_directory(capability_dir, "Capability socket directory")
    mcp_config = _write_mcp_config(capability_root)
    uid, gid = _worker_ids()
    sandbox_etc = _write_sandbox_etc(capability_root, uid, gid)
    sandbox_agy_args = _sandboxify_agy_arguments(agy_args)

    command: list[str] = [
        str(_resolved_existing(settings.agy_sandbox_binary, "Bubblewrap executable", executable=True)),
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--uid",
        str(uid),
        "--gid",
        str(gid),
        "--disable-userns",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--hostname",
        "agy-worker",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/workspace",
        "--bind",
        str(workspace),
        _SANDBOX_WORKSPACE,
        "--dir",
        "/home",
        "--bind",
        str(worker_home),
        _SANDBOX_HOME,
        "--dir",
        "/run",
        "--ro-bind",
        str(capability_root),
        _SANDBOX_CAPABILITIES,
        "--dir",
        "/opt",
        "--dir",
        _SANDBOX_CLIENT_DIR,
        "--ro-bind",
        str(CAPABILITY_CLIENT.resolve()),
        _SANDBOX_CLIENT_PATH,
        "--symlink",
        _SANDBOX_CLIENT_PATH,
        f"{_SANDBOX_CLIENT_DIR}/ag-ssh",
        "--ro-bind",
        str(skills_dir),
        f"{_SANDBOX_HOME}/.gemini/antigravity-cli/skills",
        "--ro-bind",
        str(mcp_config),
        _SANDBOX_MCP_CONFIG,
    ]
    if agy_runtime_dir is not None:
        command.extend(
            [
                "--dir",
                "/opt/ag-worker",
                "--ro-bind",
                str(agy_runtime_dir),
                _SANDBOX_RUNTIME_DIR,
            ]
        )
    command.extend(
        [
            "--dir",
            "/etc",
        "--dir",
        "/etc/ssl",
        "--ro-bind",
        str(sandbox_etc / "passwd"),
        "/etc/passwd",
        "--ro-bind",
        str(sandbox_etc / "group"),
        "/etc/group",
        "--ro-bind",
        str(sandbox_etc / "hosts"),
        "/etc/hosts",
        "--ro-bind",
        str(sandbox_etc / "nsswitch.conf"),
        "/etc/nsswitch.conf",
        ]
    )
    for source, destination in _read_only_mounts():
        command.extend(["--ro-bind", source, destination])
    socket_path = f"{_SANDBOX_CAPABILITIES}/broker.sock"
    command.extend(
        [
            "--clearenv",
            "--setenv",
            "HOME",
            _SANDBOX_HOME,
            "--setenv",
            "XDG_CONFIG_HOME",
            f"{_SANDBOX_HOME}/.config",
            "--setenv",
            "XDG_CACHE_HOME",
            f"{_SANDBOX_HOME}/.cache",
            "--setenv",
            "XDG_DATA_HOME",
            f"{_SANDBOX_HOME}/.local/share",
            "--setenv",
            "PATH",
            f"{_SANDBOX_CLIENT_DIR}:/usr/bin:/bin",
            "--setenv",
            "TERM",
            "dumb",
            "--setenv",
            "NO_COLOR",
            "1",
            "--setenv",
            "LANG",
            "C.UTF-8",
            "--setenv",
            "LC_ALL",
            "C.UTF-8",
            "--setenv",
            "PYTHONIOENCODING",
            "utf-8",
            "--setenv",
            "AGY_CAPABILITY_SOCKET",
            socket_path,
            "--setenv",
            "AGY_CAPABILITY_TOKEN",
            capability_token,
            "--setenv",
            "AGY_TG_THREAD_ID",
            str(thread_id) if thread_id is not None else "",
            "--chdir",
            _SANDBOX_WORKSPACE,
            "--",
            sandbox_agy_path,
            *sandbox_agy_args[1:],
        ]
    )
    return SandboxLaunch(
        command=tuple(command),
        env=_sandbox_environment(thread_id),
        cwd=str(workspace),
    )


def build_unsandboxed_development_launch(
    *,
    agy_args: list[str],
    execution_dir: str,
    thread_id: int | None,
) -> SandboxLaunch:
    """Explicit emergency-only path with a stripped environment, never automatic."""
    if not agy_args:
        raise WorkerSandboxError("AGY command cannot be empty")
    env = _sandbox_environment(thread_id)
    env["HOME"] = os.environ.get("HOME", "/root")
    return SandboxLaunch(command=tuple(agy_args), env=env, cwd=execution_dir)


def _bubblewrap_probe_command() -> list[str]:
    """Build a minimal process that exercises the worker's runtime mounts.

    ``/usr/bin/true`` is dynamically linked on normal Linux distributions.
    Mounting only ``/usr`` makes ``execvp`` misleadingly report that it cannot
    find the binary because its dynamic loader under ``/lib`` or ``/lib64`` is
    absent. Keep this probe aligned with the runtime mounts used by an AGY task.
    """
    binary = _resolved_existing(
        settings.agy_sandbox_binary,
        "Bubblewrap executable",
        executable=True,
    )
    probe = [
        str(binary),
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--uid",
        str(settings.agy_worker_uid),
        "--gid",
        str(settings.agy_worker_gid),
        "--disable-userns",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--hostname",
        "agy-worker-probe",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
    ]
    for raw_path in ("/usr", "/bin", "/lib", "/lib64"):
        source = Path(raw_path)
        if source.exists() or source.is_symlink():
            probe.extend(["--ro-bind", raw_path, raw_path])
    probe.extend(
        [
            "--clearenv",
            "--setenv",
            "PATH",
            "/usr/bin:/bin",
            "--",
            "/usr/bin/true",
        ]
    )
    return probe


def _verify_bubblewrap_runtime() -> None:
    """Run a harmless namespace probe for deployment preflight."""
    probe = _bubblewrap_probe_command()
    try:
        result = subprocess.run(probe, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkerSandboxError(f"Bubblewrap probe failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown Bubblewrap error").strip()
        raise WorkerSandboxError(f"Bubblewrap namespace probe failed: {detail[:500]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the AGY worker sandbox")
    parser.add_argument("check", nargs="?", choices=["check"], default="check")
    parser.add_argument(
        "--verify-runtime",
        action="store_true",
        help="also run a harmless Bubblewrap namespace probe",
    )
    args = parser.parse_args(argv)
    try:
        validate_sandbox_configuration()
        if args.verify_runtime:
            _verify_bubblewrap_runtime()
    except WorkerSandboxError as exc:
        print(f"AGY sandbox is not ready: {exc}")
        return 2
    print("AGY sandbox configuration is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
