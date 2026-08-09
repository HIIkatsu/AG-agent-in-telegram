import asyncio
import os
import sqlite3

def run_migration():
    """Add callback_paths table for Phase 3."""
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bot.db")
    print(f"Migrating database at: {db_path}")

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute('''
    CREATE TABLE IF NOT EXISTS callback_paths (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL UNIQUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    conn.commit()
    conn.close()
    print("Migration Phase 3 complete!")

if __name__ == "__main__":
    run_migration()
