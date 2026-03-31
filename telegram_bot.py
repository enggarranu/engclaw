from __future__ import annotations

from pyclaw.telegram_bot import TelegramBridge as _Bridge  # type: ignore
from engclaw.config import Config


def run_bot_via_cli(token_arg: str | None, verbose: bool = False) -> int:
    cfg = Config.load()
    token = token_arg or cfg.integrations.get("telegram_token")
    if not token:
        print("Token Telegram tidak ditemukan. Berikan via --token atau simpan di config.integrations.telegram_token")
        return 1
    if "terminal" not in cfg.channels:
        cfg.channels.append("terminal")
    if "ollama" not in cfg.channels:
        cfg.channels.append("ollama")
    print("[telegram] starting bot loop (verbose=" + str(verbose) + ")")
    _Bridge(token, cfg, verbose=verbose).loop()
    return 0
