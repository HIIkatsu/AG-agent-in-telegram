"""Async SQLite database for forum-topic session management.

Each Telegram forum thread (topic) maps to exactly one session row,
keyed by `thread_id` (== message_thread_id from Telegram).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import aiosqlite

from bot.config import settings

_CREATE_TABLES = """\
CREATE TABLE IF NOT EXISTS thread_sessions (
    thread_id    INTEGER PRIMARY KEY,
    uuid         TEXT    NOT NULL UNIQUE,
    workdir      TEXT    NOT NULL,
    is_mounted   INTEGER NOT NULL DEFAULT 0,
    web_search   INTEGER NOT NULL DEFAULT 0,
    model        TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL,
    last_used_at TEXT    NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_workdir(thread_id: int) -> str:
    """Default workspace path for a thread that hasn't been /mount-ed."""
    return f"{settings.workspaces_dir}/{thread_id}"


class Database:
    """Thin async wrapper around an SQLite file — forum-topics edition."""

    def __init__(self) -> None:
        self._path = settings.db_path
        self._conn: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(_CREATE_TABLES)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    # ------------------------------------------------------------------
    # session CRUD
    # ------------------------------------------------------------------
    async def get_or_create_session(self, thread_id: int) -> dict:
        """Lazily create a session for *thread_id* on first message."""
        assert self._conn
        cur = await self._conn.execute(
            "SELECT * FROM thread_sessions WHERE thread_id = ?", (thread_id,)
        )
        row = await cur.fetchone()
        if row:
            return dict(row)

        session_uuid = str(uuid.uuid4())
        now = _now()
        workdir = _default_workdir(thread_id)
        await self._conn.execute(
            "INSERT INTO thread_sessions "
            "(thread_id, uuid, workdir, is_mounted, web_search, model, created_at, last_used_at) "
            "VALUES (?, ?, ?, 0, 0, '', ?, ?)",
            (thread_id, session_uuid, workdir, now, now),
        )
        await self._conn.commit()
        return {
            "thread_id": thread_id,
            "uuid": session_uuid,
            "workdir": workdir,
            "is_mounted": 0,
            "web_search": 0,
            "model": "",
            "created_at": now,
            "last_used_at": now,
        }

    async def get_session(self, thread_id: int) -> dict | None:
        """Return session dict or None."""
        assert self._conn
        cur = await self._conn.execute(
            "SELECT * FROM thread_sessions WHERE thread_id = ?", (thread_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def delete_session(self, thread_id: int) -> dict | None:
        """Delete a session. Returns the deleted row (for cleanup logic) or None."""
        assert self._conn
        cur = await self._conn.execute(
            "SELECT * FROM thread_sessions WHERE thread_id = ?", (thread_id,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        session = dict(row)
        await self._conn.execute(
            "DELETE FROM thread_sessions WHERE thread_id = ?", (thread_id,)
        )
        await self._conn.commit()
        return session

    async def set_workdir(self, thread_id: int, path: str, is_mounted: bool = True) -> None:
        """Update workdir for a thread (used by /mount)."""
        assert self._conn
        await self._conn.execute(
            "UPDATE thread_sessions SET workdir = ?, is_mounted = ? WHERE thread_id = ?",
            (path, int(is_mounted), thread_id),
        )
        await self._conn.commit()

    async def toggle_web_search(self, thread_id: int) -> bool:
        """Toggle web_search flag. Returns the *new* value."""
        assert self._conn
        cur = await self._conn.execute(
            "SELECT web_search FROM thread_sessions WHERE thread_id = ?", (thread_id,)
        )
        row = await cur.fetchone()
        if not row:
            return False
        new_val = 0 if row["web_search"] else 1
        await self._conn.execute(
            "UPDATE thread_sessions SET web_search = ? WHERE thread_id = ?",
            (new_val, thread_id),
        )
        await self._conn.commit()
        return bool(new_val)

    async def set_model(self, thread_id: int, model: str) -> None:
        assert self._conn
        await self._conn.execute(
            "UPDATE thread_sessions SET model = ? WHERE thread_id = ?",
            (model, thread_id),
        )
        await self._conn.commit()

    async def update_last_used(self, thread_id: int) -> None:
        assert self._conn
        await self._conn.execute(
            "UPDATE thread_sessions SET last_used_at = ? WHERE thread_id = ?",
            (_now(), thread_id),
        )
        await self._conn.commit()

    async def list_all_sessions(self) -> list[dict]:
        """Return all sessions (for /stats)."""
        assert self._conn
        cur = await self._conn.execute(
            "SELECT * FROM thread_sessions ORDER BY last_used_at DESC"
        )
        return [dict(r) for r in await cur.fetchall()]


db = Database()
