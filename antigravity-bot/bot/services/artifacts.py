"""Safe, task-scoped delivery of AGY-generated artifacts.

An AGY task is allowed to write user-facing output only to its own directory
below ``TASK_ARTIFACTS_DIR``. This module deliberately never scans a shared
CLI scratch directory: such a scan can accidentally pick up another task's
file, a file created before the task, or a root-owned AGY artifact.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import FSInputFile

from bot.config import settings

logger = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
_MAX_ARTIFACT_DEPTH = 4
_MAX_ARTIFACT_PATH_LENGTH = 240

# Direct output has to be intentional. This keeps ordinary chat replies and
# incidental project files from being uploaded to Telegram merely because a
# model happened to create them.
_EXPLICIT_ARTIFACT_RE = re.compile(
    r"(?:"
    r"(?:(?:за|с)?генерир(?:уй|овать)|созд(?:ай|ать)|сделай|нарисуй|отправь|пришли|"
    r"подготовь|экспортируй|сохрани)\s+(?:мне\s+)?"
    r"(?:картинк\w*|изображени\w*|фото\w*|файл\w*|документ\w*|pdf|"
    r"презентаци\w*|таблиц\w*|архив\w*)"
    r"|(?:картинк\w*|изображени\w*|фото\w*)\s+(?:(?:за|с)?генерир(?:уй|овать)|"
    r"созд(?:ай|ать)|сделай|нарисуй)"
    r"|(?:(?:за|с)?генерир(?:уй|овать)|созд(?:ай|ать)|сделай|напиши|сверстай|"
    r"подготовь)\b[^\n]{0,100}?"
    r"(?:html(?:[- ]?файл)?|веб[- ]?страниц\w*|страниц\w*|странич\w*|сайт\w*|лендинг\w*|"
    r"web[- ]?page\w*|website\w*)"
    r"|(?:send|generate|create|export|save)\s+(?:an?\s+)?"
    r"(?:image|picture|photo|file|document|pdf|spreadsheet|presentation|archive|"
    r"html|web[- ]?page|website|landing[- ]?page)"
    r")",
    re.IGNORECASE,
)

_EXPLICIT_IMAGE_RE = re.compile(
    r"(?:"
    r"(?:(?:за|с)?генерир(?:уй|овать)|созд(?:ай|ать)|сделай|нарисуй)\b[^\n]{0,80}?"
    r"(?:картинк\w*|изображени\w*|фото\w*)"
    r"|(?:картинк\w*|изображени\w*|фото\w*)\s+(?:(?:за|с)?генерир(?:уй|овать)|"
    r"созд(?:ай|ать)|сделай|нарисуй)"
    r"|(?:send|generate|create|draw)\b[^\n]{0,80}?(?:image|picture|photo)"
    r")",
    re.IGNORECASE,
)


class ArtifactError(RuntimeError):
    """A task output directory or artifact is unsafe to use."""


@dataclass(frozen=True)
class TaskArtifact:
    """One regular file produced in the exact directory for a task."""

    path: Path
    relative_path: str
    size_bytes: int


@dataclass(frozen=True)
class ArtifactCollection:
    """Files safe to deliver and files intentionally left on the VPS."""

    files: tuple[TaskArtifact, ...]
    skipped: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArtifactDeliveryReport:
    """Outcome used to decide whether the task output can be cleaned up."""

    delivered: int
    failed: int


def _managed_root() -> Path:
    """Return the bot-owned output root, rejecting symlinked configuration."""
    configured = Path(settings.task_artifacts_dir).expanduser()
    try:
        existing = configured.lstat()
    except FileNotFoundError:
        try:
            configured.mkdir(parents=True, mode=0o700, exist_ok=False)
        except FileExistsError:
            # Two independent Telegram topics may begin their first task at
            # once. Re-inspect rather than failing one of the safe requests.
            pass
        existing = configured.lstat()
    except OSError as exc:
        raise ArtifactError(f"Cannot inspect TASK_ARTIFACTS_DIR: {exc}") from exc

    if stat.S_ISLNK(existing.st_mode):
        raise ArtifactError("TASK_ARTIFACTS_DIR must not be a symbolic link")
    if not stat.S_ISDIR(existing.st_mode):
        raise ArtifactError("TASK_ARTIFACTS_DIR must be a directory")
    if existing.st_uid != os.geteuid():
        raise ArtifactError("TASK_ARTIFACTS_DIR must be owned by the bot service user")
    mode = stat.S_IMODE(existing.st_mode)
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ArtifactError("TASK_ARTIFACTS_DIR must not be group- or world-writable")
    try:
        return configured.resolve(strict=True)
    except OSError as exc:
        raise ArtifactError(f"Cannot resolve TASK_ARTIFACTS_DIR: {exc}") from exc


def task_artifact_directory(task_id: int) -> Path:
    """Return the one exact output directory reserved for a positive task ID."""
    if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= 0:
        raise ArtifactError("Task artifact directory requires a positive task ID")
    root = _managed_root()
    target = root / f"task-{task_id}"
    try:
        target.relative_to(root)
    except ValueError as exc:  # Defensive even though task_id is numeric.
        raise ArtifactError("Task artifact directory escapes its managed root") from exc
    return target


def _require_exact_task_directory(task_id: int, *, must_exist: bool) -> Path:
    target = task_artifact_directory(task_id)
    if not target.exists() and not target.is_symlink():
        if must_exist:
            raise ArtifactError(f"Task artifact directory does not exist for task #{task_id}")
        return target
    try:
        metadata = target.lstat()
    except OSError as exc:
        raise ArtifactError(f"Cannot inspect task artifact directory: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ArtifactError("Task artifact directory must not be a symbolic link")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ArtifactError("Task artifact path must be a directory")
    return target


def prepare_task_artifact_directory(task_id: int) -> Path:
    """Allocate a fresh private directory writable only by the AGY worker.

    The service user owns the parent, while the worker owns only the task leaf.
    Therefore a compromised task cannot create sibling task paths or read their
    names. Reusing an existing directory is refused instead of risking stale
    output being delivered for a later task with the same ID.
    """
    target = _require_exact_task_directory(task_id, must_exist=False)
    if target.exists() or target.is_symlink():
        raise ArtifactError(f"Task artifact directory already exists for task #{task_id}")
    try:
        target.mkdir(mode=0o750)
        os.chown(target, settings.agy_worker_uid, settings.agy_worker_gid)
        os.chmod(target, 0o750)
    except OSError as exc:
        try:
            target.rmdir()
        except OSError:
            pass
        raise ArtifactError(f"Cannot prepare task artifact directory: {exc}") from exc
    return target


def cleanup_task_artifact_directory(task_id: int) -> None:
    """Remove only the exact private output directory for a completed task."""
    target = _require_exact_task_directory(task_id, must_exist=False)
    if not target.exists():
        return
    try:
        shutil.rmtree(target)
    except OSError as exc:
        raise ArtifactError(f"Cannot remove task artifact directory: {exc}") from exc


def is_explicit_artifact_request(text: str) -> bool:
    """Whether the user explicitly requested a deliverable in Telegram.

    A web page is a deliverable even when its wording does not literally say
    ``file``. The output remains confined to the exact task directory; this
    is not a watcher over a shared scratch folder.
    """
    return bool(_EXPLICIT_ARTIFACT_RE.search(text))


def is_explicit_image_request(text: str) -> bool:
    """Whether the request specifically requires an image file as its result."""
    return bool(_EXPLICIT_IMAGE_RE.search(text))


def has_image_artifact(artifacts: ArtifactCollection) -> bool:
    """Return whether collected output contains an image Telegram can deliver."""
    return any(item.path.suffix.lower() in _IMAGE_EXTENSIONS for item in artifacts.files)


def validate_requested_artifacts(
    prompt: str,
    artifacts: ArtifactCollection | None,
) -> str | None:
    """Return a user-safe terminal error when a promised deliverable is absent.

    The final answer may be fluent even when a generation tool failed.  This
    check is intentionally based on inspected files rather than model prose or
    a tool's reported state.
    """
    if not is_explicit_artifact_request(prompt):
        return None
    if artifacts is None or not artifacts.files:
        return (
            "❌ Итоговый файл не был создан, поэтому я не выдаю ложное "
            "подтверждение о готовом результате."
        )
    if is_explicit_image_request(prompt) and not has_image_artifact(artifacts):
        return (
            "❌ Изображение не было создано. Генерация не прошла, поэтому "
            "результат в Telegram не отправлен."
        )
    return None


def _limits() -> tuple[int, int]:
    max_files = max(1, int(settings.artifact_max_files))
    max_size = max(1, int(settings.artifact_max_size_mb)) * 1024 * 1024
    return max_files, max_size


def _collect_task_artifacts_sync(task_id: int) -> ArtifactCollection:
    """Walk only one output tree, never following symlinks or special files."""
    target = _require_exact_task_directory(task_id, must_exist=True)
    max_files, max_size = _limits()
    files: list[TaskArtifact] = []
    skipped: list[str] = []
    stack: list[tuple[Path, int]] = [(target, 0)]

    while stack:
        directory, depth = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            skipped.append(f"не удалось прочитать каталог: {exc}")
            continue
        for entry in entries:
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                skipped.append(f"не удалось проверить: {entry.name}")
                continue
            if stat.S_ISLNK(metadata.st_mode):
                skipped.append(f"пропущена символическая ссылка: {entry.name}")
                continue
            entry_path = Path(entry.path)
            if stat.S_ISDIR(metadata.st_mode):
                if depth >= _MAX_ARTIFACT_DEPTH:
                    skipped.append(f"слишком глубокий каталог: {entry.name}")
                else:
                    stack.append((entry_path, depth + 1))
                continue
            if not stat.S_ISREG(metadata.st_mode):
                skipped.append(f"пропущен не-обычный файл: {entry.name}")
                continue
            try:
                relative = entry_path.relative_to(target).as_posix()
            except ValueError:
                skipped.append(f"пропущен путь вне задачи: {entry.name}")
                continue
            if len(relative) > _MAX_ARTIFACT_PATH_LENGTH:
                skipped.append(f"слишком длинный путь: {entry.name}")
                continue
            if metadata.st_size > max_size:
                skipped.append(f"превышен размер файла: {relative}")
                continue
            if len(files) >= max_files:
                skipped.append("превышен лимит количества файлов")
                continue
            files.append(
                TaskArtifact(
                    path=entry_path,
                    relative_path=relative,
                    size_bytes=metadata.st_size,
                )
            )
    return ArtifactCollection(tuple(files), tuple(skipped))


async def collect_task_artifacts(task_id: int) -> ArtifactCollection:
    """Collect only the files produced by this task in a worker thread."""
    return await asyncio.to_thread(_collect_task_artifacts_sync, task_id)


async def deliver_task_artifacts(
    bot: Bot,
    chat_id: int,
    artifacts: ArtifactCollection,
    *,
    thread_id: int | None = None,
    rollback_list: list[int] | None = None,
) -> ArtifactDeliveryReport:
    """Send files as photos where appropriate and retain failed output safely."""
    delivered = 0
    failed = 0
    from bot.services.telegram_rate_limiter import telegram_rate_limiter

    for artifact in artifacts.files:
        path = artifact.path
        try:
            metadata = path.lstat()
        except OSError:
            failed += 1
            logger.warning("Artifact disappeared before Telegram delivery: %s", artifact.relative_path)
            continue
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            failed += 1
            logger.warning("Refusing unsafe artifact before Telegram delivery: %s", artifact.relative_path)
            continue

        message = None
        suffix = path.suffix.lower()
        try:
            if suffix in _IMAGE_EXTENSIONS:
                try:
                    async def send_photo():
                        return await bot.send_photo(
                            chat_id,
                            FSInputFile(str(path)),
                            caption=artifact.relative_path,
                            message_thread_id=thread_id,
                        )

                    message = await telegram_rate_limiter.request(
                        chat_id,
                        send_photo,
                        label=f"artifact photo {artifact.relative_path}",
                    )
                except TelegramRetryAfter:
                    raise
                except Exception:
                    # Telegram rejects some valid image files as photos (for
                    # example an unsupported animation or dimensions). A
                    # document fallback preserves the actual generated file.
                    logger.info(
                        "Photo delivery failed; retrying artifact as a document: %s",
                        artifact.relative_path,
                        exc_info=True,
                    )
                    async def send_document_fallback():
                        return await bot.send_document(
                            chat_id,
                            FSInputFile(str(path)),
                            caption=artifact.relative_path,
                            message_thread_id=thread_id,
                        )

                    message = await telegram_rate_limiter.request(
                        chat_id,
                        send_document_fallback,
                        label=f"artifact document fallback {artifact.relative_path}",
                    )
            else:
                async def send_document():
                    return await bot.send_document(
                        chat_id,
                        FSInputFile(str(path)),
                        caption=artifact.relative_path,
                        message_thread_id=thread_id,
                    )

                message = await telegram_rate_limiter.request(
                    chat_id,
                    send_document,
                    label=f"artifact document {artifact.relative_path}",
                )
        except Exception:
            failed += 1
            logger.exception("Failed to deliver task artifact to Telegram: %s", artifact.relative_path)
            continue

        delivered += 1
        if rollback_list is not None and message is not None:
            rollback_list.append(message.message_id)
        logger.info("Delivered task artifact to Telegram: %s", artifact.relative_path)
    return ArtifactDeliveryReport(delivered=delivered, failed=failed)


def is_managed_workspace_path(raw_path: str | Path) -> bool:
    """Return whether a path belongs to a source or isolated task workspace."""
    path = Path(raw_path).resolve(strict=False)
    roots = (
        Path(settings.workspaces_dir).expanduser().resolve(strict=False),
        Path(settings.task_workspaces_dir).expanduser().resolve(strict=False),
    )
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
