"""Durable, single-consumer task queue state transitions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from bot.db import Database, db

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    """The only persisted execution states for a task."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    INTERRUPTED = "interrupted"
    ERROR = "error"  # Legacy rows created by older bot versions.

    @classmethod
    def parse(cls, value: TaskStatus | str) -> TaskStatus:
        if isinstance(value, cls):
            return value
        try:
            return cls(value)
        except ValueError as exc:
            raise TaskStateError(f"Unknown task status: {value!r}") from exc


class TaskStateError(RuntimeError):
    """A requested task state transition is invalid or impossible."""


_FINISH_STATUSES = frozenset(
    {
        TaskStatus.DONE,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.TIMEOUT,
    }
)


@dataclass(frozen=True)
class TaskTransition:
    """The durable result of a requested state transition."""

    task_id: int
    status: TaskStatus
    changed: bool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskService:
    """Task storage with atomic claims and immutable terminal states."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def enqueue_task(
        self,
        *,
        thread_id: int,
        chat_id: int,
        project_id: int,
        prompt: str,
        mode: str = "code",
        model: str | None = None,
        parent_task_id: int | None = None,
        retry_of_task_id: int | None = None,
    ) -> int:
        """Add a task in the only state that can be claimed by a worker."""
        now = _now()
        cursor = await self._database.conn.execute(
            """
            INSERT INTO tasks (
                chat_id, thread_id, project_id, prompt, status, mode, model,
                created_at, started_at, finished_at, error, parent_task_id,
                retry_of_task_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
            """,
            (
                chat_id,
                thread_id,
                project_id,
                prompt,
                TaskStatus.QUEUED.value,
                mode,
                model,
                now,
                parent_task_id,
                retry_of_task_id,
            ),
        )
        await self._database.conn.commit()
        task_id = int(cursor.lastrowid)
        await self._record_event(task_id, "status", "Task queued")
        return task_id

    async def pop_next_task(self, thread_id: int) -> dict | None:
        """Claim one queued task with one atomic SQLite write statement.

        The partial unique index created by Database is a second durable guard.
        This is safe even if two bot processes briefly overlap during a
        deployment or restart.
        """
        started_at = _now()
        cursor = await self._database.conn.execute(
            """
            UPDATE tasks
            SET
                status = ?,
                started_at = ?,
                finished_at = NULL,
                error = NULL,
                result_summary = NULL
            WHERE id = (
                SELECT queued.id
                FROM tasks AS queued
                WHERE queued.thread_id = ?
                  AND queued.status = ?
                ORDER BY queued.id ASC
                LIMIT 1
            )
              AND status = ?
              AND NOT EXISTS (
                SELECT 1
                FROM tasks AS active
                WHERE active.thread_id = ?
                  AND active.status = ?
              )
            RETURNING *
            """,
            (
                TaskStatus.RUNNING.value,
                started_at,
                thread_id,
                TaskStatus.QUEUED.value,
                TaskStatus.QUEUED.value,
                thread_id,
                TaskStatus.RUNNING.value,
            ),
        )
        row = await cursor.fetchone()
        await self._database.conn.commit()
        if row is None:
            return None

        task = dict(row)
        await self._record_event(
            int(task["id"]),
            "status",
            "Task status: queued → running",
        )
        return task

    async def finish_task(
        self,
        task_id: int,
        status: TaskStatus,
        *,
        error: str | None = None,
        result_summary: str | None = None,
    ) -> TaskTransition:
        """Finish a running task without overwriting a concurrent cancellation."""
        target = TaskStatus.parse(status)
        if target not in _FINISH_STATUSES:
            raise TaskStateError(
                f"Task #{task_id} cannot finish with non-terminal status {target.value!r}"
            )

        cursor = await self._database.conn.execute(
            """
            UPDATE tasks
            SET status = ?, finished_at = ?, error = ?, result_summary = ?
            WHERE id = ? AND status = ?
            """,
            (
                target.value,
                _now(),
                error,
                result_summary,
                task_id,
                TaskStatus.RUNNING.value,
            ),
        )
        await self._database.conn.commit()
        changed = cursor.rowcount == 1
        if changed:
            await self._record_event(
                task_id,
                "status",
                f"Task status: running → {target.value}",
            )
            return TaskTransition(task_id=task_id, status=target, changed=True)

        current = await self._current_status(task_id)
        return TaskTransition(task_id=task_id, status=current, changed=False)

    async def cancel_task(
        self,
        task_id: int,
        *,
        thread_id: int | None = None,
    ) -> TaskTransition:
        """Cancel a queued or running task; terminal rows remain immutable."""
        where = "id = ? AND status IN (?, ?)"
        values: list[object] = [
            TaskStatus.CANCELLED.value,
            _now(),
            "Cancelled by user",
            task_id,
            TaskStatus.QUEUED.value,
            TaskStatus.RUNNING.value,
        ]
        if thread_id is not None:
            where += " AND thread_id = ?"
            values.append(thread_id)

        cursor = await self._database.conn.execute(
            f"""
            UPDATE tasks
            SET status = ?, finished_at = ?, error = ?
            WHERE {where}
            """,
            values,
        )
        await self._database.conn.commit()
        changed = cursor.rowcount == 1
        if changed:
            await self._record_event(task_id, "status", "Task cancelled by user")
            return TaskTransition(
                task_id=task_id,
                status=TaskStatus.CANCELLED,
                changed=True,
            )

        current = await self._current_status(task_id, thread_id=thread_id)
        return TaskTransition(task_id=task_id, status=current, changed=False)

    async def cancel_queue(self, thread_id: int) -> int:
        """Cancel only work that has not reached a terminal state."""
        cursor = await self._database.conn.execute(
            """
            UPDATE tasks
            SET status = ?, finished_at = ?, error = ?
            WHERE thread_id = ? AND status IN (?, ?)
            """,
            (
                TaskStatus.CANCELLED.value,
                _now(),
                "Cancelled by user",
                thread_id,
                TaskStatus.QUEUED.value,
                TaskStatus.RUNNING.value,
            ),
        )
        await self._database.conn.commit()
        return cursor.rowcount

    async def stop_task_and_queue(
        self,
        task_id: int,
        *,
        thread_id: int,
    ) -> TaskTransition:
        """Stop a running task and its queue without a claim-after-stop race.

        A ``Stop`` button is attached to a running task.  If that task is still
        running when this transaction begins, all active work in its project is
        cancelled in one SQLite write transaction before the subprocess is
        signalled.  A stale button for a queued task only cancels that task.
        """
        connection = self._database.conn
        await connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = await connection.execute(
                "SELECT status FROM tasks WHERE id = ? AND thread_id = ?",
                (task_id, thread_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise TaskStateError(f"Task #{task_id} was not found")

            current = TaskStatus.parse(str(row["status"]))
            if current not in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                await connection.rollback()
                return TaskTransition(task_id=task_id, status=current, changed=False)

            if current is TaskStatus.RUNNING:
                await connection.execute(
                    """
                    UPDATE tasks
                    SET status = ?, finished_at = ?, error = ?
                    WHERE thread_id = ? AND status IN (?, ?)
                    """,
                    (
                        TaskStatus.CANCELLED.value,
                        _now(),
                        "Cancelled by user",
                        thread_id,
                        TaskStatus.QUEUED.value,
                        TaskStatus.RUNNING.value,
                    ),
                )
            else:
                await connection.execute(
                    """
                    UPDATE tasks
                    SET status = ?, finished_at = ?, error = ?
                    WHERE id = ? AND thread_id = ? AND status = ?
                    """,
                    (
                        TaskStatus.CANCELLED.value,
                        _now(),
                        "Cancelled by user",
                        task_id,
                        thread_id,
                        TaskStatus.QUEUED.value,
                    ),
                )
            await connection.commit()
        except Exception:
            await connection.rollback()
            raise

        await self._record_event(task_id, "status", "Task cancelled by user")
        return TaskTransition(
            task_id=task_id,
            status=TaskStatus.CANCELLED,
            changed=True,
        )

    async def get_active_task(self, thread_id: int) -> dict | None:
        """Get the one running task allowed for a project thread."""
        cursor = await self._database.conn.execute(
            """
            SELECT * FROM tasks
            WHERE thread_id = ? AND status = ?
            ORDER BY id DESC LIMIT 1
            """,
            (thread_id, TaskStatus.RUNNING.value),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_task(
        self,
        task_id: int,
        *,
        thread_id: int | None = None,
    ) -> dict | None:
        """Return one task, optionally constrained to its project thread."""
        query = "SELECT * FROM tasks WHERE id = ?"
        values: list[object] = [task_id]
        if thread_id is not None:
            query += " AND thread_id = ?"
            values.append(thread_id)
        cursor = await self._database.conn.execute(query, values)
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_queued_count(self, thread_id: int) -> int:
        """Return the number of tasks still waiting to be claimed."""
        cursor = await self._database.conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE thread_id = ? AND status = ?",
            (thread_id, TaskStatus.QUEUED.value),
        )
        row = await cursor.fetchone()
        return int(row["c"]) if row else 0

    async def recovery_interrupted_tasks(self) -> int:
        """Mark stale running work as interrupted after the bot has restarted."""
        cursor = await self._database.conn.execute(
            """
            UPDATE tasks
            SET
                status = ?,
                finished_at = COALESCE(finished_at, ?),
                error = COALESCE(error, ?),
                result_summary = COALESCE(result_summary, ?)
            WHERE status = ?
            """,
            (
                TaskStatus.INTERRUPTED.value,
                _now(),
                "Bot restarted while task was running",
                "Bot restarted",
                TaskStatus.RUNNING.value,
            ),
        )
        await self._database.conn.commit()
        return cursor.rowcount

    async def _current_status(
        self,
        task_id: int,
        *,
        thread_id: int | None = None,
    ) -> TaskStatus:
        where = "id = ?"
        values: list[object] = [task_id]
        if thread_id is not None:
            where += " AND thread_id = ?"
            values.append(thread_id)
        cursor = await self._database.conn.execute(
            f"SELECT status FROM tasks WHERE {where}",
            values,
        )
        row = await cursor.fetchone()
        if row is None:
            raise TaskStateError(f"Task #{task_id} was not found")
        return TaskStatus.parse(str(row["status"]))

    async def _record_event(self, task_id: int, level: str, message: str) -> None:
        """Keep state durable even when optional diagnostic logging fails."""
        try:
            await self._database.conn.execute(
                """
                INSERT INTO task_logs (task_id, timestamp, level, message, data)
                VALUES (?, ?, ?, ?, NULL)
                """,
                (task_id, _now(), level, message),
            )
            await self._database.conn.commit()
        except Exception:
            logger.exception("Failed to record task event for task #%s", task_id)


task_service = TaskService(db)


async def enqueue_task(
    thread_id: int,
    chat_id: int,
    project_id: int,
    prompt: str,
    mode: str = "code",
    model: str | None = None,
    parent_task_id: int | None = None,
    retry_of_task_id: int | None = None,
) -> int:
    """Compatibility wrapper for the application-level task service."""
    return await task_service.enqueue_task(
        thread_id=thread_id,
        chat_id=chat_id,
        project_id=project_id,
        prompt=prompt,
        mode=mode,
        model=model,
        parent_task_id=parent_task_id,
        retry_of_task_id=retry_of_task_id,
    )


async def pop_next_task(thread_id: int) -> dict | None:
    return await task_service.pop_next_task(thread_id)


async def cancel_task(
    task_id: int,
    *,
    thread_id: int | None = None,
) -> TaskTransition:
    return await task_service.cancel_task(task_id, thread_id=thread_id)


async def cancel_queue(thread_id: int) -> int:
    return await task_service.cancel_queue(thread_id)


async def stop_task_and_queue(
    task_id: int,
    *,
    thread_id: int,
) -> TaskTransition:
    return await task_service.stop_task_and_queue(task_id, thread_id=thread_id)


async def get_active_task(thread_id: int) -> dict | None:
    return await task_service.get_active_task(thread_id)


async def get_task(
    task_id: int,
    *,
    thread_id: int | None = None,
) -> dict | None:
    return await task_service.get_task(task_id, thread_id=thread_id)


async def get_queued_count(thread_id: int) -> int:
    return await task_service.get_queued_count(thread_id)


async def finish_task(
    task_id: int,
    status: TaskStatus,
    error: str | None = None,
    result_summary: str | None = None,
) -> TaskTransition:
    return await task_service.finish_task(
        task_id,
        status,
        error=error,
        result_summary=result_summary,
    )


async def recovery_interrupted_tasks() -> int:
    return await task_service.recovery_interrupted_tasks()


async def log_task_event(
    task_id: int,
    level: str,
    message: str,
    data: str | None = None,
) -> None:
    """Add a diagnostic task log entry."""
    await db.conn.execute(
        """
        INSERT INTO task_logs (task_id, timestamp, level, message, data)
        VALUES (?, ?, ?, ?, ?)
        """,
        (task_id, _now(), level, message, data),
    )
    await db.conn.commit()


async def log_task_events_bulk(
    task_id: int,
    events: list[tuple[str, str, str | None]],
) -> None:
    """Add multiple diagnostic events in one SQLite commit."""
    if not events:
        return
    now = _now()
    await db.conn.executemany(
        """
        INSERT INTO task_logs (task_id, timestamp, level, message, data)
        VALUES (?, ?, ?, ?, ?)
        """,
        [(task_id, now, level, message, data) for level, message, data in events],
    )
    await db.conn.commit()
