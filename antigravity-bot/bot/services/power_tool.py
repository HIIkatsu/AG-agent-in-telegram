"""CLI tool for an explicit remote shutdown over a configured SSH environment."""

from __future__ import annotations

import argparse
import asyncio

from bot.db import db
from bot.services.power_manager import soft_shutdown


async def main() -> int:
    parser = argparse.ArgumentParser(description="Agent remote power tool")
    subparsers = parser.add_subparsers(dest="action", required=True)

    shutdown_parser = subparsers.add_parser(
        "shutdown",
        help="Soft-shutdown a configured remote machine over SSH",
    )
    shutdown_parser.add_argument("env_name", help="Configured SSH environment name")
    args = parser.parse_args()

    await db.connect()
    try:
        return_code, stdout, stderr = await soft_shutdown(args.env_name)
        print(f"--- SSH EXIT CODE: {return_code} ---")
        if stdout:
            print("--- STDOUT ---")
            print(stdout)
        if stderr:
            print("--- STDERR ---")
            print(stderr)
        return 0 if return_code == 0 else 1
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
