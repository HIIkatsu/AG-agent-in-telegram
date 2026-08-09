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
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    workdir TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    default_model TEXT,
    default_mode TEXT DEFAULT 'code',
    test_command TEXT,
    run_command TEXT,
    deploy_command TEXT,
    server_profile_id INTEGER
);

CREATE TABLE IF NOT EXISTS thread_sessions (
    thread_id    INTEGER PRIMARY KEY,
    project_id   INTEGER REFERENCES projects(id),
    uuid         TEXT    NOT NULL UNIQUE,
    workdir      TEXT    NOT NULL,
    is_mounted   INTEGER NOT NULL DEFAULT 0,
    web_search   TEXT    NOT NULL DEFAULT 'off',
    mode         TEXT    NOT NULL DEFAULT 'code',
    model        TEXT    NOT NULL DEFAULT '',
    topic_name   TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL,
    last_used_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER,
    thread_id    INTEGER NOT NULL,
    project_id   INTEGER,
    prompt       TEXT    NOT NULL,
    status       TEXT    NOT NULL,
    mode         TEXT    NOT NULL DEFAULT 'code',
    model        TEXT,
    created_at   TEXT,
    started_at   TEXT,
    finished_at  TEXT,
    error        TEXT,
    result_summary TEXT,
    full_response_path TEXT,
    parent_task_id INTEGER,
    retry_of_task_id INTEGER
);

CREATE TABLE IF NOT EXISTS task_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT
);

CREATE TABLE IF NOT EXISTS task_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    filename TEXT NOT NULL,
    content_path TEXT NOT NULL,
    created_at TEXT NOT NULL
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

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not connected"
        return self._conn

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(_CREATE_TABLES)
        
        # Auto-migrate running tasks to interrupted on bot startup
        await self._conn.execute(
            "UPDATE tasks SET status = 'interrupted' WHERE status = 'running'"
        )
        
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

        if thread_id != 0:
            glob = await self.get_global_settings()
            def_model = glob.get("model", "")
            def_web = glob.get("web_search", "off")
            def_mode = glob.get("mode", "code")
        else:
            def_model = ""
            def_web = "off"
            def_mode = "code"
            
        session_uuid = str(uuid.uuid4())
        now = _now()
        workdir = _default_workdir(thread_id)
        
        # 1. Create default project for this session
        project_name = f"Project {thread_id}" if thread_id != 0 else "Global Settings"
        cur_proj = await self._conn.execute(
            "INSERT INTO projects (name, workdir, created_at, last_used_at, default_model, default_mode) VALUES (?, ?, ?, ?, ?, ?)",
            (project_name, workdir, now, now, def_model, def_mode)
        )
        project_id = cur_proj.lastrowid
        
        # 2. Insert session
        await self._conn.execute(
            "INSERT INTO thread_sessions "
            "(thread_id, project_id, uuid, workdir, is_mounted, web_search, mode, model, topic_name, created_at, last_used_at) "
            "VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
            (thread_id, project_id, session_uuid, workdir, def_web, def_mode, def_model, "", now, now),
        )
        await self._conn.commit()
        return {
            "thread_id": thread_id,
            "project_id": project_id,
            "uuid": session_uuid,
            "workdir": workdir,
            "is_mounted": 0,
            "web_search": def_web,
            "mode": def_mode,
            "model": def_model,
            "created_at": now,
            "last_used_at": now,
        }

    async def get_session(self, thread_id: int) -> dict | None:
        """Get session data for a thread."""
        async with self.conn.execute(
            "SELECT uuid, workdir, is_mounted, web_search, mode, model, topic_name, project_id FROM thread_sessions WHERE thread_id = ?",
            (thread_id,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return {
                    "uuid": row[0],
                    "workdir": row[1],
                    "is_mounted": bool(row[2]),
                    "web_search": row[3],
                    "mode": row[4],
                    "model": row[5],
                    "topic_name": row[6] or "",
                    "project_id": row[7],
                    "thread_id": thread_id
                }
            return None

    async def get_global_settings(self) -> dict:
        """Get global default settings from thread_id = 0."""
        s = await self.get_session(0)
        if not s:
            await self.get_or_create_session(0)
            s = await self.get_session(0)
        return s or {"model": "", "web_search": "off", "mode": "code"}

    async def update_global_settings(self, model: str | None = None, web_search: str | None = None, mode: str | None = None) -> None:
        """Update global default settings."""
        if model is not None:
            await self.set_model(0, model)
        if web_search is not None:
            await self.set_web_search(0, web_search)
        if mode is not None:
            await self.set_mode(0, mode)

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

    async def set_web_search(self, thread_id: int, web_search: str) -> None:
        """Set web_search mode ('off', 'auto', 'required')."""
        assert self._conn
        await self._conn.execute(
            "UPDATE thread_sessions SET web_search = ? WHERE thread_id = ?",
            (web_search, thread_id),
        )
        await self._conn.commit()

    async def set_mode(self, thread_id: int, mode: str) -> None:
        """Set agent mode."""
        assert self._conn
        await self._conn.execute(
            "UPDATE thread_sessions SET mode = ? WHERE thread_id = ?",
            (mode, thread_id),
        )
        await self._conn.commit()

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
