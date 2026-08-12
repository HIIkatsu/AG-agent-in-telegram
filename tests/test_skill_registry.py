"""Regression tests for bot-owned global AGY skill registration."""

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

from bot.services.skill_registry import SkillRegistryError, ensure_global_skills

REQUIRED_SKILLS = {
    "api-and-interface-design",
    "browser-testing-with-devtools",
    "ci-cd-and-automation",
    "code-review-and-quality",
    "code-simplification",
    "context-engineering",
    "debugging-and-error-recovery",
    "deprecation-and-migration",
    "documentation-and-adrs",
    "doubt-driven-development",
    "frontend-ui-engineering",
    "git-workflow-and-versioning",
    "global-memory",
    "idea-refine",
    "incremental-implementation",
    "interview-me",
    "observability-and-instrumentation",
    "performance-optimization",
    "planning-and-task-breakdown",
    "remote-environments",
    "security-and-hardening",
    "shipping-and-launch",
    "source-driven-development",
    "spec-driven-development",
    "test-driven-development",
    "using-agent-skills",
}


def _create_skill(root: Path, name: str, marker: str = "content") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test\n---\n\n{marker}\n",
        encoding="utf-8",
    )
    return skill_dir


def test_all_required_bundled_skills_remain_native_skill_packages() -> None:
    source = ROOT / "antigravity-bot" / ".agents" / "skills"
    skill_names = {path.name for path in source.iterdir() if path.is_dir()}

    assert REQUIRED_SKILLS <= skill_names
    assert all((source / name / "SKILL.md").is_file() for name in skill_names)
    assert all(
        f"name: {name}" in (source / name / "SKILL.md").read_text(encoding="utf-8")
        for name in skill_names
    )


def test_global_memory_skill_uses_native_memory_tools() -> None:
    skill = (ROOT / "antigravity-bot" / ".agents" / "skills" / "global-memory" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "save_memory" in skill
    assert "list_memory" in skill
    assert "delete_memory" in skill
    assert "bot.services.memory_tools" not in skill


def test_registry_installs_the_complete_bundled_library(tmp_path: Path) -> None:
    source = ROOT / "antigravity-bot" / ".agents" / "skills"
    target = tmp_path / "global"
    skill_names = {path.name for path in source.iterdir() if path.is_dir()}

    report = ensure_global_skills(source, target)

    assert report.available_count == len(skill_names)
    assert all((target / name / "SKILL.md").is_file() for name in skill_names)
    assert all(not (target / name).is_symlink() for name in skill_names)


def test_registry_copies_every_skill_and_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "bundled"
    target = tmp_path / "global"
    alpha = _create_skill(source, "alpha")
    _create_skill(source, "beta")
    (alpha / "reference.txt").write_text("linked asset", encoding="utf-8")

    first = ensure_global_skills(source, target)
    second = ensure_global_skills(source, target)

    assert first.installed == ("alpha", "beta")
    assert first.available_count == 2
    assert second.unchanged == ("alpha", "beta")
    assert (target / "alpha").is_dir()
    assert not (target / "alpha").is_symlink()
    assert (target / "beta").is_dir()
    assert (target / "alpha" / "reference.txt").read_text() == "linked asset"
    manifest = json.loads(
        (target / ".ag-agent-in-telegram-skills.json").read_text(encoding="utf-8")
    )
    assert manifest["version"] == 2
    assert set(manifest["managed"]) == {"alpha", "beta"}


def test_registry_updates_owned_copy_after_source_relocation(tmp_path: Path) -> None:
    first_source = tmp_path / "first"
    second_source = tmp_path / "second"
    target = tmp_path / "global"
    _create_skill(first_source, "alpha", "first")
    relocated = _create_skill(second_source, "alpha", "second")

    ensure_global_skills(first_source, target)
    report = ensure_global_skills(second_source, target)

    assert report.updated == ("alpha",)
    assert (target / "alpha" / "SKILL.md").read_text(encoding="utf-8") == (
        relocated / "SKILL.md"
    ).read_text(encoding="utf-8")


def test_registry_never_overwrites_user_owned_skill(tmp_path: Path) -> None:
    source = tmp_path / "bundled"
    target = tmp_path / "global"
    _create_skill(source, "alpha")
    user_skill = _create_skill(target, "alpha", "user owned")

    with pytest.raises(SkillRegistryError, match="alpha"):
        ensure_global_skills(source, target)

    assert not user_skill.is_symlink()
    assert "user owned" in (user_skill / "SKILL.md").read_text(encoding="utf-8")


def test_registry_never_overwrites_a_modified_managed_skill(tmp_path: Path) -> None:
    source = tmp_path / "bundled"
    target = tmp_path / "global"
    _create_skill(source, "alpha")
    ensure_global_skills(source, target)
    managed_skill = target / "alpha" / "SKILL.md"
    managed_skill.write_text("local change", encoding="utf-8")

    with pytest.raises(SkillRegistryError, match="alpha"):
        ensure_global_skills(source, target)

    assert managed_skill.read_text(encoding="utf-8") == "local change"
