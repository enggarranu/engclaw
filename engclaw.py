#!/usr/bin/env python3
import sys


def main():
    # Delegasikan ke pyclaw.cli agar perintah tetap konsisten
    try:
        from pyclaw import cli as py_cli
    except Exception as e:
        print(f"Gagal memuat pyclaw.cli: {e}")
        sys.exit(1)

    argv = sys.argv[1:]
    if not argv:
        print("Usage: python -m engclaw [cli] <subcommand> [args]\nContoh: python -m engclaw telegram-bot --verbose")
        sys.exit(0)

    # Izinkan bentuk: python -m engclaw cli <...> dan python -m engclaw <subcommand>
    if argv[0] == "cli":
        sys.argv = ["pyclaw.cli"] + argv[1:]
    else:
        sys.argv = ["pyclaw.cli"] + argv

    py_cli.main()


if __name__ == "__main__":
    main()
