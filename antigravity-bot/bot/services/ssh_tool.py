"""CLI used by AGY's remote-environments skill for configured SSH access."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from bot.db import db
from bot.services.ssh_executor import execute_command, get_public_key


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent SSH execution tool")
    subparsers = parser.add_subparsers(dest="action", required=True)

    exec_parser = subparsers.add_parser(
        "exec",
        help="Execute a command on a configured remote environment",
    )
    exec_parser.add_argument("env_name", help="Configured environment name")
    exec_parser.add_argument("command", help="Command to execute")
    exec_parser.add_argument("--cwd", help="Remote working directory")

    subparsers.add_parser("pubkey", help="Show the bot's public SSH key")
    subparsers.add_parser("list", help="List configured remote environments")
    return parser


async def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    await db.connect()
    try:
        if args.action == "pubkey":
            print(await get_public_key())
            return 0

        if args.action == "list":
            environments = await db.get_all_environments()
            if not environments:
                print("No environments configured.")
                return 0
            print("Available environments:")
            for environment in environments:
                print(
                    f"- '{environment['name']}' "
                    f"({environment['username']}@{environment['host']}:"
                    f"{environment.get('port', 22)})"
                )
            return 0

        return_code, stdout, stderr = await execute_command(
            args.env_name,
            args.command,
            args.cwd,
        )
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
