"""CLI Tool for the Agent to execute SSH commands via bot's internal ssh_executor."""

import argparse
import asyncio
import sys

from bot.db import db
from bot.services.ssh_executor import execute_command, get_public_key

async def main():
    parser = argparse.ArgumentParser(description="Agent SSH Execution Tool")
    subparsers = parser.add_subparsers(dest="action", required=True)

    # exec subcommand
    exec_p = subparsers.add_parser("exec", help="Execute command on remote environment")
    exec_p.add_argument("env_name", help="Name of the environment (e.g. 'Home PC')")
    exec_p.add_argument("command", help="Command to execute")
    exec_p.add_argument("--cwd", help="Working directory", default=None)

    # pubkey subcommand
    subparsers.add_parser("pubkey", help="Show bot's public SSH key")
    
    # list subcommand
    subparsers.add_parser("list", help="List available environments")

    args = parser.parse_args()

    await db.connect()

    if args.action == "pubkey":
        pub = await get_public_key()
        print(pub)
        return

    if args.action == "list":
        envs = await db.get_all_environments()
        if not envs:
            print("No environments configured.")
            return
        print("Available environments:")
        for env in envs:
            print(f"- '{env['name']}' ({env['username']}@{env['host']}:{env.get('port', 22)})")
        return

    if args.action == "exec":
        ret, stdout, stderr = await execute_command(args.env_name, args.command, args.cwd)
        print(f"--- SSH EXIT CODE: {ret} ---")
        if stdout:
            print("--- STDOUT ---")
            print(stdout)
        if stderr:
            print("--- STDERR ---")
            print(stderr)
        
        sys.exit(0 if ret == 0 else 1)

if __name__ == "__main__":
    asyncio.run(main())
