"""Task queue and database service."""

import logging
from datetime import datetime, timezone

from bot.db import db

logger = logging.getLogger(__name__)

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

async def enqueue_task(thread_id: int, chat_id: int, project_id: int, prompt: str, mode: str = "code", model: str | None = None, parent_task_id: int | None = None, retry_of_task_id: int | None = None) -> int:
    """Add a new task to the queue and return its ID."""
    now = _now()
    cur = await db.conn.execute(
        "INSERT INTO tasks (chat_id, thread_id, project_id, prompt, status, mode, model, created_at, started_at, finished_at, error, parent_task_id, retry_of_task_id) "
        "VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, NULL, NULL, NULL, ?, ?)",
        (chat_id, thread_id, project_id, prompt, mode, model, now, parent_task_id, retry_of_task_id)
    )
    await db.conn.commit()
    return cur.lastrowid

async def pop_next_task(thread_id: int) -> dict | None:
    """Atomically pop the next queued task and mark it as running."""
    # Find next queued task
    cur = await db.conn.execute(
        "SELECT * FROM tasks WHERE thread_id = ? AND status = 'queued' ORDER BY id ASC LIMIT 1",
        (thread_id,)
    )
    row = await cur.fetchone()
    if not row:
        return None
        
    task_id = row["id"]
    # Update to running
    await db.conn.execute(
        "UPDATE tasks SET status = 'running', started_at = ? WHERE id = ?",
        (_now(), task_id)
    )
    await db.conn.commit()
    
    # Fetch updated row
    cur = await db.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    updated = await cur.fetchone()
    return dict(updated) if updated else None

async def cancel_task(task_id: int) -> None:
    """Mark a task as cancelled."""
    await db.conn.execute(
        "UPDATE tasks SET status = 'cancelled', finished_at = ? WHERE id = ?",
        (_now(), task_id)
    )
    await db.conn.commit()

async def cancel_queue(thread_id: int) -> None:
    """Cancel all queued and running tasks for a thread."""
    await db.conn.execute(
        "UPDATE tasks SET status = 'cancelled', finished_at = ? WHERE thread_id = ? AND status IN ('queued', 'running')",
        (_now(), thread_id)
    )
    await db.conn.commit()

async def get_active_task(thread_id: int) -> dict | None:
    """Get the currently running task for a thread."""
    cur = await db.conn.execute(
        "SELECT * FROM tasks WHERE thread_id = ? AND status = 'running' ORDER BY id DESC LIMIT 1",
        (thread_id,)
    )
    row = await cur.fetchone()
    return dict(row) if row else None

async def get_task(task_id: int) -> dict | None:
    """Get task by ID."""
    cur = await db.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = await cur.fetchone()
    return dict(row) if row else None

async def get_queued_count(thread_id: int) -> int:
    """Get number of queued tasks for a thread."""
    cur = await db.conn.execute(
        "SELECT COUNT(*) as c FROM tasks WHERE thread_id = ? AND status = 'queued'",
        (thread_id,)
    )
    row = await cur.fetchone()
    return row["c"] if row else 0

async def finish_task(task_id: int, status: str, error: str | None = None, result_summary: str | None = None) -> None:
    """Mark a task as done or failed."""
    await db.conn.execute(
        "UPDATE tasks SET status = ?, finished_at = ?, error = ?, result_summary = ? WHERE id = ?",
        (status, _now(), error, result_summary, task_id)
    )
    await db.conn.commit()

async def recovery_interrupted_tasks() -> None:
    """Mark all running tasks as interrupted (e.g. after bot restart)."""
    assert db.conn
    await db.conn.execute(
        "UPDATE tasks SET status = 'interrupted', result_summary = 'Bot restarted' WHERE status = 'running'"
    )
    await db.conn.commit()

async def log_task_event(task_id: int, level: str, message: str, data: str | None = None) -> None:
    """Add a log entry for a task."""
    await db.conn.execute(
        "INSERT INTO task_logs (task_id, timestamp, level, message, data) VALUES (?, ?, ?, ?, ?)",
        (task_id, _now(), level, message, data)
    )
    await db.conn.commit()

async def log_task_events_bulk(task_id: int, events: list[tuple[str, str, str | None]]) -> None:
    """Add multiple log entries for a task in a single SQLite commit."""
    if not events:
        return
    now = _now()
    await db.conn.executemany(
        "INSERT INTO task_logs (task_id, timestamp, level, message, data) VALUES (?, ?, ?, ?, ?)",
        [(task_id, now, level, message, data) for level, message, data in events],
    )
    await db.conn.commit()
