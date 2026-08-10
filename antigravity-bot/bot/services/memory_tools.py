import argparse
import asyncio
import sys
from pathlib import Path

# Add project root to path so we can import bot modules
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.db import db

async def save_fact(fact: str):
    success = await db.add_user_memory(fact)
    if success:
        print(f"✅ Fact saved successfully: {fact}")
    else:
        print(f"⚠️ Fact already exists in memory: {fact}")

async def list_facts():
    facts = await db.get_all_user_memory()
    if not facts:
        print("📭 Memory is empty.")
        return
    print("🧠 User Context (Global Memory):")
    for row in facts:
        print(f"[{row['id']}] {row['fact']}")

async def main():
    parser = argparse.ArgumentParser(description="AGY Personal Intelligence Memory Tool")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    save_parser = subparsers.add_parser("save", help="Save a new fact about the user")
    save_parser.add_argument("fact", type=str, help="The fact to remember")
    
    list_parser = subparsers.add_parser("list", help="List all remembered facts")
    
    args = parser.parse_args()
    
    if args.command == "save":
        await save_fact(args.fact)
    elif args.command == "list":
        await list_facts()

if __name__ == "__main__":
    asyncio.run(main())
