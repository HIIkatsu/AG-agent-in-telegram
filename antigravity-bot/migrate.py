import sqlite3

c = sqlite3.connect("/opt/antigravity-bot/data/bot.db")
try:
    c.execute("ALTER TABLE thread_sessions ADD COLUMN topic_name TEXT DEFAULT ''")
    c.commit()
    print("Migrated topic_name!")
except Exception as e:
    print("Ignored error:", e)
