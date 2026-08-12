"""Register bot-bundled skills in Antigravity CLI's global skill directory."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

BOT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BUNDLED_SKILLS_DIR = BOT_ROOT / ".agents" / "skills"
DEFAULT_GLOBAL_SKILLS_DIR = Path.home() / ".gemini" / "antigravity-cli" / "skills"
_MANIFEST_NAME = ".ag-agent-in-telegram-skills.json"


class SkillRegistryError(RuntimeError):
    """Raised when bundled skills cannot be registered without overwriting user data."""


@dataclass(frozen=True)
class SkillSyncReport:
    """Result of synchronizing the bot-owned global skill copies."""

    installed: tuple[str, ...]
    updated: tuple[str, ...]
    unchanged: tuple[str, ...]
    removed: tuple[str, ...]

    @property
    def available_count(self) -> int:
        return len(self.installed) + len(self.updated) + len(self.unchanged)


@dataclass(frozen=True)
class _ManagedSkill:
    source: str
    sha256: str


def _discover_skills(source_dir: Path) -> dict[str, Path]:
    if not source_dir.is_dir():
        raise SkillRegistryError(
            f"Bundled skills directory does not exist: {source_dir}"
        )

    directories = sorted(
        entry
        for entry in source_dir.iterdir()
        if entry.is_dir() and not entry.name.startswith(".")
    )
    invalid = [
        entry.name
        for entry in directories
        if entry.is_symlink() or not (entry / "SKILL.md").is_file()
    ]
    if invalid:
        raise SkillRegistryError(
            "Invalid bundled skill packages (SKILL.md required): " + ", ".join(invalid)
        )
    if not directories:
        raise SkillRegistryError(f"No valid bundled skills found in: {source_dir}")
    return {entry.name: entry.resolve() for entry in directories}


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()

    def visit(directory: Path) -> None:
        for entry in sorted(directory.iterdir(), key=lambda path: path.name):
            relative = entry.relative_to(root).as_posix().encode("utf-8")
            if entry.is_symlink():
                digest.update(b"L\0" + relative + b"\0")
                digest.update(os.readlink(entry).encode("utf-8"))
            elif entry.is_dir():
                digest.update(b"D\0" + relative + b"\0")
                visit(entry)
            elif entry.is_file():
                executable = entry.stat().st_mode & 0o111
                digest.update(
                    b"F\0" + relative + b"\0" + str(executable).encode() + b"\0"
                )
                with entry.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(128 * 1024), b""):
                        digest.update(chunk)
            else:
                raise SkillRegistryError(f"Unsupported entry in bundled skill: {entry}")

    try:
        visit(root)
    except OSError as exc:
        raise SkillRegistryError(f"Cannot inspect skill package {root}: {exc}") from exc
    return digest.hexdigest()


def _load_manifest(target_dir: Path) -> dict[str, _ManagedSkill]:
    manifest_path = target_dir / _MANIFEST_NAME
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SkillRegistryError(f"Cannot read managed skills manifest: {exc}") from exc

    if not isinstance(payload, dict):
        raise SkillRegistryError(f"Invalid managed skills manifest: {manifest_path}")
    managed = payload.get("managed", {})
    if payload.get("version") != 2 or not isinstance(managed, dict):
        raise SkillRegistryError(f"Invalid managed skills manifest: {manifest_path}")

    parsed: dict[str, _ManagedSkill] = {}
    for name, metadata in managed.items():
        if not isinstance(name, str) or not isinstance(metadata, dict):
            raise SkillRegistryError(
                f"Invalid managed skills manifest: {manifest_path}"
            )
        source = metadata.get("source")
        sha256 = metadata.get("sha256")
        if (
            not isinstance(source, str)
            or not isinstance(sha256, str)
            or len(sha256) != 64
        ):
            raise SkillRegistryError(
                f"Invalid managed skills manifest: {manifest_path}"
            )
        parsed[name] = _ManagedSkill(source=source, sha256=sha256)
    return parsed


def _manifest_payload(skills: dict[str, Path], digests: dict[str, str]) -> str:
    payload = {
        "version": 2,
        "managed": {
            name: {"source": str(path), "sha256": digests[name]}
            for name, path in skills.items()
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _remove_tree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def ensure_global_skills(
    source_dir: Path = DEFAULT_BUNDLED_SKILLS_DIR,
    target_dir: Path = DEFAULT_GLOBAL_SKILLS_DIR,
) -> SkillSyncReport:
    """Copy every bundled skill globally without modifying a target workspace.

    Only directories recorded in the registry manifest and unchanged since the last
    sync are replaced or removed. User-owned and user-modified paths are preserved.
    Real directories are used because AGY does not document directory-symlink discovery.
    """
    source_dir = source_dir.expanduser().resolve()
    raw_target_dir = target_dir.expanduser()
    try:
        target_metadata = raw_target_dir.lstat()
    except FileNotFoundError:
        target_metadata = None
    except OSError as exc:
        raise SkillRegistryError(f"Cannot inspect global skills directory: {exc}") from exc
    if target_metadata is not None and raw_target_dir.is_symlink():
        raise SkillRegistryError(
            f"Global skills directory must not be a symbolic link: {raw_target_dir}"
        )
    target_dir = raw_target_dir.resolve()
    if source_dir == target_dir:
        raise SkillRegistryError("Bundled and global skills directories must differ")

    skills = _discover_skills(source_dir)
    digests = {name: _tree_digest(path) for name, path in skills.items()}
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SkillRegistryError(
            f"Cannot create global skills directory: {exc}"
        ) from exc

    previous = _load_manifest(target_dir)
    actions: dict[str, str] = {}
    conflicts: list[str] = []

    for name in skills:
        destination = target_dir / name
        prior = previous.get(name)
        if prior is None:
            if destination.exists() or destination.is_symlink():
                conflicts.append(name)
            else:
                actions[name] = "installed"
            continue

        if not destination.exists() and not destination.is_symlink():
            actions[name] = "updated"
        elif (
            destination.is_symlink()
            or not destination.is_dir()
            or _tree_digest(destination) != prior.sha256
        ):
            conflicts.append(name)
        elif digests[name] == prior.sha256:
            actions[name] = "unchanged"
        else:
            actions[name] = "updated"

    removable_stale: list[str] = []
    for name in sorted(set(previous) - set(skills)):
        destination = target_dir / name
        if not destination.exists() and not destination.is_symlink():
            continue
        if (
            not destination.is_symlink()
            and destination.is_dir()
            and _tree_digest(destination) == previous[name].sha256
        ):
            removable_stale.append(name)
        else:
            conflicts.append(name)

    if conflicts:
        joined = ", ".join(sorted(set(conflicts)))
        raise SkillRegistryError(
            "Refusing to overwrite user-owned or modified global skill paths: " + joined
        )

    staging_root = Path(tempfile.mkdtemp(prefix=".ag-skill-stage-", dir=target_dir))
    backup_root = Path(tempfile.mkdtemp(prefix=".ag-skill-backup-", dir=target_dir))
    moved_backups: list[str] = []
    installed_destinations: list[str] = []
    try:
        for name, action in actions.items():
            if action == "unchanged":
                continue
            staged = staging_root / name
            shutil.copytree(skills[name], staged, symlinks=True)
            if _tree_digest(staged) != digests[name]:
                raise SkillRegistryError(
                    f"Staged skill copy failed integrity check: {name}"
                )

        staged_manifest = staging_root / _MANIFEST_NAME
        staged_manifest.write_text(_manifest_payload(skills, digests), encoding="utf-8")

        replace_names = [
            name for name, action in actions.items() if action == "updated"
        ]
        for name in [*replace_names, *removable_stale]:
            destination = target_dir / name
            if destination.exists() or destination.is_symlink():
                os.replace(destination, backup_root / name)
                moved_backups.append(name)

        for name, action in actions.items():
            if action == "unchanged":
                continue
            os.replace(staging_root / name, target_dir / name)
            installed_destinations.append(name)

        os.replace(staged_manifest, target_dir / _MANIFEST_NAME)
    except (OSError, shutil.Error, SkillRegistryError) as exc:
        for name in reversed(installed_destinations):
            _remove_tree(target_dir / name)
        for name in reversed(moved_backups):
            backup = backup_root / name
            if backup.exists() or backup.is_symlink():
                os.replace(backup, target_dir / name)
        if isinstance(exc, SkillRegistryError):
            raise
        raise SkillRegistryError(f"Cannot synchronize global skills: {exc}") from exc
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
        shutil.rmtree(backup_root, ignore_errors=True)

    return SkillSyncReport(
        installed=tuple(
            name for name, action in actions.items() if action == "installed"
        ),
        updated=tuple(name for name, action in actions.items() if action == "updated"),
        unchanged=tuple(
            name for name, action in actions.items() if action == "unchanged"
        ),
        removed=tuple(removable_stale),
    )
