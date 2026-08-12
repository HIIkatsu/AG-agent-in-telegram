"""Regression tests for task-scoped Telegram artifact delivery."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "antigravity-bot"))
os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")

from bot.config import settings
from bot.services import artifacts


def _configure_artifact_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "task-artifacts"
    monkeypatch.setattr(settings, "task_artifacts_dir", str(root))
    monkeypatch.setattr(settings, "agy_worker_uid", os.geteuid())
    monkeypatch.setattr(settings, "agy_worker_gid", os.getegid())
    monkeypatch.setattr(settings, "artifact_max_files", 20)
    monkeypatch.setattr(settings, "artifact_max_size_mb", 1)
    return root


def test_task_output_is_private_and_exactly_scoped(tmp_path: Path, monkeypatch) -> None:
    root = _configure_artifact_root(tmp_path, monkeypatch)

    output = artifacts.prepare_task_artifact_directory(71)

    assert output == root / "task-71"
    assert stat.S_IMODE(root.stat().st_mode) & 0o022 == 0
    assert stat.S_IMODE(output.stat().st_mode) == 0o750
    assert output.stat().st_uid == os.geteuid()
    with pytest.raises(artifacts.ArtifactError, match="already exists"):
        artifacts.prepare_task_artifact_directory(71)

    artifacts.cleanup_task_artifact_directory(71)
    assert not output.exists()


def test_collection_never_follows_links_and_does_not_dedupe_names(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _configure_artifact_root(tmp_path, monkeypatch)
    output = artifacts.prepare_task_artifact_directory(72)
    nested = output / "nested"
    nested.mkdir()
    (output / "result.txt").write_text("one", encoding="utf-8")
    (nested / "result.txt").write_text("two", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    (output / "outside-link.txt").symlink_to(outside)

    collection = asyncio.run(artifacts.collect_task_artifacts(72))

    assert [item.relative_path for item in collection.files] == [
        "result.txt",
        "nested/result.txt",
    ]
    assert any("символическая ссылка" in item for item in collection.skipped)


def test_explicit_image_request_is_detected_but_an_analysis_request_is_not() -> None:
    assert artifacts.is_explicit_artifact_request("Загенерируй картинку Фурии")
    assert artifacts.is_explicit_artifact_request("Please generate an image of a cat")
    assert not artifacts.is_explicit_artifact_request("Проанализируй эту картинку")


def test_image_uses_document_fallback_and_failed_file_is_not_deleted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _configure_artifact_root(tmp_path, monkeypatch)
    output = artifacts.prepare_task_artifact_directory(73)
    image = output / "fury.png"
    image.write_bytes(b"not-a-real-png")
    collection = asyncio.run(artifacts.collect_task_artifacts(73))

    class _Bot:
        def __init__(self) -> None:
            self.photos = 0
            self.documents = 0

        async def send_photo(self, *_args, **_kwargs):
            self.photos += 1
            raise RuntimeError("Telegram rejected image dimensions")

        async def send_document(self, *_args, **_kwargs):
            self.documents += 1
            return SimpleNamespace(message_id=44)

    bot = _Bot()
    rollback: list[int] = []
    report = asyncio.run(
        artifacts.deliver_task_artifacts(
            bot,
            1,
            collection,
            thread_id=5,
            rollback_list=rollback,
        )
    )

    assert report.delivered == 1
    assert report.failed == 0
    assert bot.photos == 1
    assert bot.documents == 1
    assert rollback == [44]
    assert image.exists()


def test_delivery_failure_preserves_output_for_safe_recovery(tmp_path: Path, monkeypatch) -> None:
    _configure_artifact_root(tmp_path, monkeypatch)
    output = artifacts.prepare_task_artifact_directory(74)
    document = output / "report.pdf"
    document.write_bytes(b"pdf")
    collection = asyncio.run(artifacts.collect_task_artifacts(74))

    class _Bot:
        async def send_document(self, *_args, **_kwargs):
            raise RuntimeError("network failure")

    report = asyncio.run(artifacts.deliver_task_artifacts(_Bot(), 1, collection))

    assert report.delivered == 0
    assert report.failed == 1
    assert document.exists()
