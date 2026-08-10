import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import settings  # noqa: E402


def run_migration():
    """Add user_memory table for Personal Intelligence."""
    db_path = settings.db_path
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    print(f"Migrating database at: {db_path}")

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.executescript('''
    CREATE TABLE IF NOT EXISTS user_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fact TEXT NOT NULL UNIQUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    ''')

    conn.commit()
    conn.close()
    print("Migration Memory complete!")


if __name__ == "__main__":
    run_migration()
