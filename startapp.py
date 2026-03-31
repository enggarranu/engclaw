#!/usr/bin/env python3
import argparse
from pyclaw.telegram_bot import run_bot_via_cli


def main():
    p = argparse.ArgumentParser(prog="startapp", description="Start Pyclaw Telegram bot")
    p.add_argument("--token", help="Telegram bot token (fallback to config)")
    p.add_argument("--verbose", action="store_true", default=True, help="Enable verbose logs")
    p.add_argument("--no-verbose", action="store_false", dest="verbose", help="Disable verbose logs")
    args = p.parse_args()
    run_bot_via_cli(args.token, verbose=args.verbose)


if __name__ == "__main__":
    main()
