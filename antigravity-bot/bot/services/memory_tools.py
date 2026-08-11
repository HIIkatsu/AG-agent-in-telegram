"""CLI used by AGY's global-memory skill to manage durable user facts."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING

from bot.services.global_memory import MAX_FACT_CHARS

if TYPE_CHECKING:
    from bot.db import Database


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AGY global user-memory tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    save_parser = subparsers.add_parser(
        "save", help="Save a durable fact about the user"
    )
    save_parser.add_argument("fact", help="Fact to remember")

    subparsers.add_parser("list", help="List all durable user facts")

    delete_parser = subparsers.add_parser("delete", help="Delete a fact by numeric ID")
    delete_parser.add_argument(
        "fact_id", type=int, help="Fact ID shown by the list command"
    )
    return parser


async def _save_fact(database: Database, fact: str) -> int:
    fact = fact.strip()
    if not fact:
        print("ERROR: Memory fact cannot be empty.")
        return 2
    if len(fact) > MAX_FACT_CHARS:
        print(f"ERROR: Memory fact exceeds the {MAX_FACT_CHARS}-character limit.")
        return 2

    if await database.add_user_memory(fact):
        print(f"Saved global memory fact: {fact}")
    else:
        print(f"Global memory fact already exists: {fact}")
    return 0


async def _list_facts(database: Database) -> int:
    facts = await database.get_all_user_memory()
    if not facts:
        print("Global memory is empty.")
        return 0

    print("Global user memory:")
    for row in facts:
        print(f"[{row['id']}] {row['fact']}")
    return 0


async def _delete_fact(database: Database, fact_id: int) -> int:
    if await database.delete_user_memory(fact_id):
        print(f"Deleted global memory fact [{fact_id}].")
        return 0
    print(f"ERROR: Global memory fact [{fact_id}] does not exist.")
    return 1


async def main(
    argv: Sequence[str] | None = None,
    *,
    database: Database | None = None,
) -> int:
    """Run one memory operation using its own bounded database connection."""
    args = _build_parser().parse_args(argv)
    if database is None:
        from bot.db import Database

        database = Database()

    await database.connect()
    try:
        if args.command == "save":
            return await _save_fact(database, args.fact)
        if args.command == "list":
            return await _list_facts(database)
        return await _delete_fact(database, args.fact_id)
    finally:
        await database.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
