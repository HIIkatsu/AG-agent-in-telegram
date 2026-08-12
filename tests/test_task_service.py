"""Regression tests for durable task queue claims and status transitions."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "antigravity-bot"))
os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")

from bot.db import Database
from bot.services.task_service import (
    TaskService,
    TaskStateError,
    TaskStatus,
)


async def _new_database(path: Path) -> Database:
    database = Database()
    database._path = str(path)
    await database.connect()
    return database


async def _enqueue(service: TaskService, thread_id: int = 42) -> int:
    return await service.enqueue_task(
        thread_id=thread_id,
        chat_id=-100,
        project_id=1,
        prompt="task",
    )


def test_competing_workers_claim_one_task_once(tmp_path: Path) -> None:
    async def exercise() -> None:
        path = tmp_path / "queue.db"
        first = await _new_database(path)
        second = await _new_database(path)
        first_service = TaskService(first)
        second_service = TaskService(second)
        try:
            task_id = await _enqueue(first_service)
            claims = await asyncio.gather(
                first_service.pop_next_task(42),
                second_service.pop_next_task(42),
            )

            claimed_ids = [int(task["id"]) for task in claims if task is not None]
            assert claimed_ids == [task_id]
            assert await first_service.pop_next_task(42) is None
            active = await second_service.get_active_task(42)
            assert active is not None
            assert active["id"] == task_id
            assert active["status"] == TaskStatus.RUNNING.value
        finally:
            await second.close()
            await first.close()

    asyncio.run(exercise())


def test_cancelled_task_cannot_be_overwritten_by_late_completion(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        database = await _new_database(tmp_path / "queue.db")
        service = TaskService(database)
        try:
            task_id = await _enqueue(service)
            assert (await service.pop_next_task(42))["id"] == task_id

            cancelled = await service.cancel_task(task_id, thread_id=42)
            late_completion = await service.finish_task(
                task_id,
                TaskStatus.DONE,
                result_summary="must not overwrite cancellation",
            )
            stored = await service.get_task(task_id)

            assert cancelled.changed is True
            assert cancelled.status is TaskStatus.CANCELLED
            assert late_completion.changed is False
            assert late_completion.status is TaskStatus.CANCELLED
            assert stored is not None
            assert stored["status"] == TaskStatus.CANCELLED.value
            assert stored["result_summary"] is None
        finally:
            await database.close()

    asyncio.run(exercise())


def test_stopping_running_task_cancels_its_queue_atomically(tmp_path: Path) -> None:
    async def exercise() -> None:
        database = await _new_database(tmp_path / "queue.db")
        service = TaskService(database)
        try:
            running_id = await _enqueue(service)
            queued_id = await _enqueue(service)
            assert (await service.pop_next_task(42))["id"] == running_id

            stopped = await service.stop_task_and_queue(
                running_id,
                thread_id=42,
            )
            late_completion = await service.finish_task(
                running_id,
                TaskStatus.DONE,
                result_summary="must not overwrite stop",
            )
            running = await service.get_task(running_id)
            queued = await service.get_task(queued_id)

            assert stopped.changed is True
            assert stopped.status is TaskStatus.CANCELLED
            assert late_completion.changed is False
            assert late_completion.status is TaskStatus.CANCELLED
            assert running is not None
            assert queued is not None
            assert running["status"] == TaskStatus.CANCELLED.value
            assert queued["status"] == TaskStatus.CANCELLED.value
            assert await service.pop_next_task(42) is None
        finally:
            await database.close()

    asyncio.run(exercise())


def test_task_transitions_are_thread_scoped_and_typed(tmp_path: Path) -> None:
    async def exercise() -> None:
        database = await _new_database(tmp_path / "queue.db")
        service = TaskService(database)
        try:
            task_id = await _enqueue(service, thread_id=42)
            with pytest.raises(TaskStateError, match="was not found"):
                await service.cancel_task(task_id, thread_id=43)

            queued = await service.get_task(task_id)
            assert queued is not None
            assert queued["status"] == TaskStatus.QUEUED.value
            assert await service.get_task(task_id, thread_id=43) is None
            assert (await service.pop_next_task(42))["id"] == task_id
            with pytest.raises(TaskStateError, match="non-terminal"):
                await service.finish_task(task_id, TaskStatus.RUNNING)
        finally:
            await database.close()

    asyncio.run(exercise())


def test_restart_recovery_marks_running_task_interrupted(tmp_path: Path) -> None:
    async def exercise() -> None:
        database = await _new_database(tmp_path / "queue.db")
        service = TaskService(database)
        try:
            task_id = await _enqueue(service)
            assert await service.pop_next_task(42) is not None

            assert await service.recovery_interrupted_tasks() == 1
            recovered = await service.get_task(task_id)
            assert recovered is not None
            assert recovered["status"] == TaskStatus.INTERRUPTED.value
            assert recovered["finished_at"]
            assert recovered["error"] == "Bot restarted while task was running"
        finally:
            await database.close()

    asyncio.run(exercise())


def test_database_upgrades_legacy_duplicate_running_rows(tmp_path: Path) -> None:
    async def exercise() -> None:
        path = tmp_path / "legacy.db"
        initial = await _new_database(path)
        await initial.close()

        legacy = sqlite3.connect(path)
        try:
            legacy.execute("DROP INDEX idx_tasks_one_running_per_thread")
            legacy.executemany(
                "INSERT INTO tasks (thread_id, prompt, status, mode) VALUES (?, ?, ?, ?)",
                [
                    (77, "old task", TaskStatus.RUNNING.value, "code"),
                    (77, "new task", TaskStatus.RUNNING.value, "code"),
                ],
            )
            legacy.commit()
        finally:
            legacy.close()

        upgraded = await _new_database(path)
        try:
            cursor = await upgraded.conn.execute(
                "SELECT id, status FROM tasks WHERE thread_id = ? ORDER BY id",
                (77,),
            )
            rows = [dict(row) for row in await cursor.fetchall()]
            assert [row["status"] for row in rows] == [
                TaskStatus.INTERRUPTED.value,
                TaskStatus.RUNNING.value,
            ]
            cursor = await upgraded.conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'index' AND name = 'idx_tasks_one_running_per_thread'
                """
            )
            assert await cursor.fetchone() is not None
        finally:
            await upgraded.close()

    asyncio.run(exercise())
