import sqlite3

def run():
    c = sqlite3.connect("/opt/antigravity-bot/data/bot.db")
    c.row_factory = sqlite3.Row
    
    try:
        # Start transaction
        c.execute("BEGIN TRANSACTION;")
        
        # 1. New Tables
        c.execute("""
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
            )
        """)
        
        c.execute("""
            CREATE TABLE IF NOT EXISTS task_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                data TEXT
            )
        """)
        
        c.execute("""
            CREATE TABLE IF NOT EXISTS task_artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                content_path TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        
        # 2. Auto-create projects from existing distinct workdirs
        c.execute("""
            INSERT OR IGNORE INTO projects (name, workdir, created_at)
            SELECT DISTINCT 
                COALESCE(NULLIF(replace(workdir, rtrim(workdir, replace(workdir, '/', '')), ''), ''), 'Default'),
                workdir,
                datetime('now')
            FROM thread_sessions WHERE workdir IS NOT NULL
        """)
        
        # 3. Migrate thread_sessions (SQLite safe schema change)
        c.execute("""
            CREATE TABLE thread_sessions_new (
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
            )
        """)
        
        c.execute("""
            INSERT INTO thread_sessions_new 
            (thread_id, project_id, uuid, workdir, is_mounted, web_search, mode, model, topic_name, created_at, last_used_at)
            SELECT 
                ts.thread_id,
                p.id,
                ts.uuid,
                ts.workdir,
                ts.is_mounted,
                CASE WHEN ts.web_search = 1 THEN 'auto' ELSE 'off' END,
                'code',
                ts.model,
                ts.topic_name,
                ts.created_at,
                ts.last_used_at
            FROM thread_sessions ts
            LEFT JOIN projects p ON ts.workdir = p.workdir
        """)
        
        c.execute("DROP TABLE thread_sessions")
        c.execute("ALTER TABLE thread_sessions_new RENAME TO thread_sessions")
        
        # 4. Migrate tasks table (SQLite safe schema change)
        c.execute("""
            CREATE TABLE tasks_new (
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
            )
        """)
        
        c.execute("""
            INSERT INTO tasks_new
            (id, thread_id, prompt, status, mode, model, started_at, finished_at, error, created_at)
            SELECT 
                id, thread_id, prompt, status, mode, model, started_at, finished_at, error, started_at
            FROM tasks
        """)
        
        # Try to backfill project_id for existing tasks using thread_sessions
        c.execute("""
            UPDATE tasks_new
            SET project_id = (
                SELECT project_id FROM thread_sessions WHERE thread_id = tasks_new.thread_id
            )
            WHERE project_id IS NULL
        """)
        
        c.execute("DROP TABLE tasks")
        c.execute("ALTER TABLE tasks_new RENAME TO tasks")
        
        c.execute("COMMIT;")
        print("Migration Phase 2 completed successfully!")
        
    except Exception as e:
        c.execute("ROLLBACK;")
        print(f"Migration failed: {e}")
    finally:
        c.close()

if __name__ == "__main__":
    run()
