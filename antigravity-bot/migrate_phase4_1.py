import asyncio
import logging
from bot.db import Database

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("migrate_phase4_1")

async def main():
    db = Database()
    await db.connect()
    
    logger.info("Creating environments table...")
    await db.conn.execute("""
        CREATE TABLE IF NOT EXISTS environments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            host TEXT NOT NULL,
            port INTEGER NOT NULL DEFAULT 22,
            username TEXT NOT NULL,
            ssh_key_path TEXT NOT NULL
        )
    """)
    await db.conn.commit()
    logger.info("Migration 4.1 applied successfully.")

if __name__ == "__main__":
    asyncio.run(main())
