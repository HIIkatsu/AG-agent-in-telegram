"""Async SQLite database for forum-topic session management.

Each Telegram forum thread (topic) maps to exactly one session row,
keyed by `thread_id` (== message_thread_id from Telegram).
"""

from __future__ import annotations

import os
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

CREATE INDEX IF NOT EXISTS idx_tasks_thread_status_id
    ON tasks(thread_id, status, id);

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

CREATE TABLE IF NOT EXISTS task_workspaces (
    task_id INTEGER PRIMARY KEY REFERENCES tasks(id),
    thread_id INTEGER NOT NULL,
    source_workdir TEXT NOT NULL,
    source_root TEXT NOT NULL,
    source_subdir TEXT NOT NULL DEFAULT '',
    task_root TEXT NOT NULL UNIQUE,
    task_workdir TEXT NOT NULL UNIQUE,
    snapshot_commit TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    finalized_at TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_task_workspaces_thread_state
    ON task_workspaces(thread_id, state);

CREATE TABLE IF NOT EXISTS callback_paths (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS context_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(thread_id, path)
);

CREATE TABLE IF NOT EXISTS context_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER NOT NULL,
    note TEXT NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS background_processes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER NOT NULL,
    project_id INTEGER,
    type TEXT NOT NULL,
    pid INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    url TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS environments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL DEFAULT 22,
    username TEXT NOT NULL,
    ssh_key_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS command_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER NOT NULL,
    command TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    output TEXT
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
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(_CREATE_TABLES)
        await self._deduplicate_running_tasks()
        await self._conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_one_running_per_thread
                ON tasks(thread_id)
                WHERE status = 'running'
            """
        )
        await self._conn.commit()

    async def close(self) -> None:
        connection = self._conn
        self._conn = None
        if connection:
            await connection.close()

    async def _deduplicate_running_tasks(self) -> None:
        """Make old pre-constraint rows compatible with one active task per thread."""
        assert self._conn
        await self._conn.execute(
            """
            UPDATE tasks
            SET
                status = 'interrupted',
                finished_at = COALESCE(finished_at, ?),
                error = COALESCE(error, ?),
                result_summary = COALESCE(result_summary, ?)
            WHERE status = 'running'
              AND id NOT IN (
                SELECT MAX(id)
                FROM tasks
                WHERE status = 'running'
                GROUP BY thread_id
              )
            """,
            (
                _now(),
                "Duplicate running task recovered during startup",
                "Bot restarted",
            ),
        )

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
        """Set the binary web-search mode; ``required`` is a legacy alias for on."""
        assert self._conn
        web_search = "on" if web_search in {"on", "required"} else "off"
        await self._conn.execute(
            "UPDATE thread_sessions SET web_search = ? WHERE thread_id = ?",
            (web_search, thread_id),
        )
        await self._conn.commit()

    async def toggle_web_search(self, thread_id: int) -> str:
        """Toggle web search between off and on."""
        session = await self.get_or_create_session(thread_id)
        current = session.get("web_search", "off")
        next_mode = "off" if current in {"on", "required"} else "on"
        await self.set_web_search(thread_id, next_mode)
        return next_mode

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

    # ------------------------------------------------------------------
    # Phase 3: callback_paths
    # ------------------------------------------------------------------
    async def save_callback_path(self, path: str) -> int:
        """Save a long path and return its short ID."""
        assert self._conn
        # Try to insert or ignore if exists
        await self._conn.execute(
            "INSERT OR IGNORE INTO callback_paths (path) VALUES (?)",
            (path,)
        )
        await self._conn.commit()
        # Retrieve the ID
        cur = await self._conn.execute(
            "SELECT id FROM callback_paths WHERE path = ?",
            (path,)
        )
        row = await cur.fetchone()
        return row[0] if row else 0

    async def get_callback_path(self, path_id: int) -> str | None:
        """Get the long path by its ID."""
        assert self._conn
        cur = await self._conn.execute(
            "SELECT path FROM callback_paths WHERE id = ?",
            (path_id,)
        )
        row = await cur.fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------
    # Phase 3: IDE context and memory helpers
    # ------------------------------------------------------------------
    async def add_context_file(self, thread_id: int, path: str, pinned: bool = False) -> None:
        assert self._conn
        await self._conn.execute(
            "INSERT OR REPLACE INTO context_files (thread_id, path, pinned, created_at) VALUES (?, ?, ?, ?)",
            (thread_id, path, int(pinned), _now()),
        )
        await self._conn.commit()

    async def remove_context_file(self, thread_id: int, path: str) -> None:
        assert self._conn
        await self._conn.execute("DELETE FROM context_files WHERE thread_id = ? AND path = ?", (thread_id, path))
        await self._conn.commit()

    async def clear_context(self, thread_id: int) -> None:
        assert self._conn
        await self._conn.execute("DELETE FROM context_files WHERE thread_id = ?", (thread_id,))
        await self._conn.execute("DELETE FROM context_notes WHERE thread_id = ?", (thread_id,))
        await self._conn.commit()

    async def list_context_files(self, thread_id: int) -> list[dict]:
        assert self._conn
        cur = await self._conn.execute("SELECT * FROM context_files WHERE thread_id = ? ORDER BY pinned DESC, path", (thread_id,))
        return [dict(r) for r in await cur.fetchall()]

    async def add_context_note(self, thread_id: int, note: str, pinned: bool = False) -> int:
        assert self._conn
        cur = await self._conn.execute(
            "INSERT INTO context_notes (thread_id, note, pinned, created_at) VALUES (?, ?, ?, ?)",
            (thread_id, note, int(pinned), _now()),
        )
        await self._conn.commit()
        return cur.lastrowid

    async def list_context_notes(self, thread_id: int) -> list[dict]:
        assert self._conn
        cur = await self._conn.execute("SELECT * FROM context_notes WHERE thread_id = ? ORDER BY pinned DESC, id DESC", (thread_id,))
        return [dict(r) for r in await cur.fetchall()]

    async def add_memory_note(self, thread_id: int, note: str) -> int:
        assert self._conn
        cur = await self._conn.execute(
            "INSERT INTO project_memory (thread_id, note, created_at) VALUES (?, ?, ?)",
            (thread_id, note, _now()),
        )
        await self._conn.commit()
        return cur.lastrowid

    async def delete_memory_note(self, note_id: int, thread_id: int) -> None:
        assert self._conn
        await self._conn.execute("DELETE FROM project_memory WHERE id = ? AND thread_id = ?", (note_id, thread_id))
        await self._conn.commit()

    async def list_memory_notes(self, thread_id: int) -> list[dict]:
        assert self._conn
        cur = await self._conn.execute("SELECT * FROM project_memory WHERE thread_id = ? ORDER BY id DESC", (thread_id,))
        return [dict(r) for r in await cur.fetchall()]

    async def create_command_run(self, thread_id: int, command: str) -> int:
        assert self._conn
        cur = await self._conn.execute(
            "INSERT INTO command_runs (thread_id, command, status, started_at) VALUES (?, ?, 'running', ?)",
            (thread_id, command, _now()),
        )
        await self._conn.commit()
        return cur.lastrowid

    async def finish_command_run(self, run_id: int, status: str, output: str) -> None:
        assert self._conn
        await self._conn.execute(
            "UPDATE command_runs SET status = ?, finished_at = ?, output = ? WHERE id = ?",
            (status, _now(), output, run_id),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Phase 4: Background Processes
    # ------------------------------------------------------------------
    async def create_background_process(self, thread_id: int, project_id: int | None, type_: str, pid: int) -> int:
        assert self._conn
        cur = await self._conn.execute(
            "INSERT INTO background_processes (thread_id, project_id, type, pid) VALUES (?, ?, ?, ?)",
            (thread_id, project_id, type_, pid),
        )
        await self._conn.commit()
        return cur.lastrowid

    async def update_background_process(self, process_id: int, status: str, url: str | None = None) -> None:
        assert self._conn
        if url is not None:
            await self._conn.execute(
                "UPDATE background_processes SET status = ?, url = ? WHERE id = ?",
                (status, url, process_id),
            )
        else:
            await self._conn.execute(
                "UPDATE background_processes SET status = ? WHERE id = ?",
                (status, process_id),
            )
        await self._conn.commit()

    async def get_background_process(self, process_id: int) -> dict | None:
        assert self._conn
        cur = await self._conn.execute("SELECT * FROM background_processes WHERE id = ?", (process_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_background_processes(self, thread_id: int, include_stopped: bool = False) -> list[dict]:
        assert self._conn
        if include_stopped:
            cur = await self._conn.execute("SELECT * FROM background_processes WHERE thread_id = ? ORDER BY id DESC", (thread_id,))
        else:
            cur = await self._conn.execute("SELECT * FROM background_processes WHERE thread_id = ? AND status = 'running' ORDER BY id DESC", (thread_id,))
        return [dict(r) for r in await cur.fetchall()]

    async def remove_background_process(self, process_id: int) -> None:
        assert self._conn
        await self._conn.execute("DELETE FROM background_processes WHERE id = ?", (process_id,))
        await self._conn.commit()


    # ------------------------------------------------------------------
    # Personal Intelligence (Global User Memory)
    # ------------------------------------------------------------------
    async def add_user_memory(self, fact: str) -> bool:
        assert self._conn
        try:
            await self._conn.execute(
                "INSERT INTO user_memory (fact) VALUES (?)",
                (fact,),
            )
            await self._conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False  # Already exists

    async def get_all_user_memory(self) -> list[dict]:
        assert self._conn
        cur = await self._conn.execute("SELECT * FROM user_memory ORDER BY id ASC")
        return [dict(r) for r in await cur.fetchall()]

    async def get_all_environments(self) -> list[dict]:
        assert self._conn
        cur = await self._conn.execute("SELECT * FROM environments ORDER BY id ASC")
        return [dict(r) for r in await cur.fetchall()]
        
    async def get_environment_by_name(self, name: str) -> dict | None:
        assert self._conn
        cur = await self._conn.execute("SELECT * FROM environments WHERE name = ?", (name,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_environment_by_id(self, env_id: int) -> dict | None:
        assert self._conn
        cur = await self._conn.execute("SELECT * FROM environments WHERE id = ?", (env_id,))
        row = await cur.fetchone()
        return dict(row) if row else None
        
    async def add_environment(self, name: str, host: str, port: int, username: str, ssh_key_path: str) -> None:
        assert self._conn
        await self._conn.execute(
            "INSERT OR REPLACE INTO environments (name, host, port, username, ssh_key_path) VALUES (?, ?, ?, ?, ?)",
            (name, host, port, username, ssh_key_path)
        )
        await self._conn.commit()

    async def delete_environment(self, env_id: int) -> None:
        assert self._conn
        await self._conn.execute("DELETE FROM environments WHERE id = ?", (env_id,))
        await self._conn.commit()

    async def delete_user_memory(self, fact_id: int) -> bool:
        assert self._conn
        cur = await self._conn.execute("DELETE FROM user_memory WHERE id = ?", (fact_id,))
        await self._conn.commit()
        return cur.rowcount > 0


db = Database()
