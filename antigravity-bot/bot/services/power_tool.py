"""CLI Tool for the Agent to manage hardware power via bot's internal power_manager."""

import argparse
import asyncio
import sys

from bot.db import db
from bot.services.power_manager import soft_shutdown, hard_wake_up, list_devices

async def main():
    parser = argparse.ArgumentParser(description="Agent Hardware Power Tool")
    subparsers = parser.add_subparsers(dest="action", required=True)

    # shutdown subcommand
    shutdown_p = subparsers.add_parser("shutdown", help="Soft shutdown remote PC via SSH")
    shutdown_p.add_argument("env_name", help="Name of the environment (e.g. 'Home PC')")

    # wakeup subcommand
    wakeup_p = subparsers.add_parser("wakeup", help="Hard wake up PC via Xiaomi Smart Plug")
    wakeup_p.add_argument("device_name", help="Name of the plug in Mi Home")

    # list subcommand
    subparsers.add_parser("list", help="List available Xiaomi devices")

    args = parser.parse_args()

    # We need DB for shutdown command because it queries env_name
    await db.connect()

    if args.action == "list":
        try:
            res = await list_devices()
            print(res)
        except Exception as e:
            print(f"Error listing devices: {e}")
            sys.exit(1)
        return

    if args.action == "shutdown":
        ret, stdout, stderr = await soft_shutdown(args.env_name)
        print(f"--- SSH EXIT CODE: {ret} ---")
        if stdout:
            print("--- STDOUT ---")
            print(stdout)
        if stderr:
            print("--- STDERR ---")
            print(stderr)
        
        sys.exit(0 if ret == 0 else 1)

    if args.action == "wakeup":
        try:
            res = await hard_wake_up(args.device_name)
            print(res)
        except Exception as e:
            print(f"Error waking up device: {e}")
            sys.exit(1)
        return

if __name__ == "__main__":
    asyncio.run(main())
