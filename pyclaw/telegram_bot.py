"""
Bridge Telegram untuk Pyclaw.

Fungsi:
- Long polling `getUpdates` dari Bot API.
- Menerima perintah teks sederhana dan mengarahkan ke Gateway.
- Mendukung pola: `run <skill>`, `exec <command>`, `ask <prompt>`.

Konfigurasi token:
- Prioritas: argumen CLI --token
- Fallback: Config.integrations["telegram_token"]
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional
from urllib.request import urlopen, Request

from .config import Config
from .gateway import Gateway
from .agent.planner import Session, plan_and_execute


class TelegramBridge:
    """
    Loop long-polling yang menghubungkan Telegram dengan Gateway.
    """

    def __init__(self, token: str, cfg: Config, verbose: bool = False):
        # Menyimpan token dan membangun Gateway
        self.token = token
        self.cfg = cfg
        self.gw = Gateway(cfg.workspace_dir, cfg.skills_dir, cfg.channels, cfg.integrations)
        # Menyimpan offset update agar tidak memproses pesan lama berulang
        self.offset: Optional[int] = None
        # Flag verbose untuk logging ke stdout
        self.verbose = verbose

    def api_url(self, method: str) -> str:
        # Membangun URL Bot API berdasarkan metode
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def get_updates(self) -> list[dict]:
        # Melakukan long-polling dengan timeout 20 detik
        query = {"timeout": 20}
        if self.offset is not None:
            query["offset"] = self.offset
        url = self.api_url("getUpdates") + ("?" + "&".join(f"{k}={v}" for k, v in query.items()))
        if self.verbose:
            print("[telegram] polling getUpdates")
        with urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if self.verbose:
            print(f"[telegram] updates: {len(data.get('result', []))} ok={data.get('ok', True)}")
        return data.get("result", [])

    def send_message(self, chat_id: int, text: str) -> None:
        # Mengirim pesan balik ke chat dengan mode HTML agar bisa <pre> blok monospaced
        body = json.dumps({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }).encode("utf-8")
        req = Request(self.api_url("sendMessage"), data=body, headers={"Content-Type": "application/json"}, method="POST")
        if self.verbose:
            print(f"[telegram] send_message to {chat_id}: {len(text)} chars")
        with urlopen(req, timeout=30) as resp:
            resp.read()

    @staticmethod
    def _escape_html(s: str) -> str:
        # Meng-escape karakter HTML agar aman dalam parse_mode=HTML
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _pre(self, s: str) -> str:
        # Membungkus teks ke blok <pre> untuk tampilan monospaced rapi
        return f"<pre>{self._escape_html(s)}</pre>"

    def handle_text(self, chat_id: int, text: str) -> None:
        # Memproses perintah teks dan memanggil Gateway sesuai pola
        t = text.strip()
        tl = t.lower()
        if self.verbose:
            print(f"[telegram] recv text: {t}")
        if tl.startswith("run ") or tl.startswith("/run "):
            name = t[4:].strip()
            result = self.gw.run_skill(name)
            if result.get("ok"):
                # Membaca ringkasan log dan kirim dalam blok <pre>
                try:
                    log_path = Path(result["log"]).resolve()
                    snippet = log_path.read_text()
                except Exception:
                    snippet = f"Skill '{name}' selesai. Log: {result['log']}"
                self.send_message(chat_id, self._pre(snippet))
            else:
                self.send_message(chat_id, f"Gagal: {result.get('error')}")
        elif tl.startswith("exec ") or tl.startswith("/exec "):
            cmd = t[5:].strip()
            if self.verbose:
                print(f"[telegram] exec: {cmd}")
            rc, out, err = self.gw.channels["terminal"].send({"command": cmd})
            block = f"RC={rc}\nOUT=\n{out}\nERR=\n{err}"
            self.send_message(chat_id, self._pre(block))
        elif tl.startswith("ask ") or tl.startswith("/ask "):
            # Mode agentik: biarkan model merencanakan dan memanggil tool
            raw = t.split(" ", 1)[1] if " " in t else ""
            sess = Session(cwd=Path(self.cfg.agent.get("cwd", self.cfg.workspace_dir)), allow_shell=bool(self.cfg.agent.get("allow_shell", True)))
            def emit(s: str) -> None:
                self.send_message(chat_id, self._pre(s))
            try:
                if self.verbose:
                    print(f"[planner] start with prompt: {raw}")
                use_stream = bool(self.cfg.agent.get("stream", True))
                plan_and_execute(raw, self.gw, sess, emit, debug=self.verbose, use_stream=use_stream)
                if self.verbose:
                    print(f"[planner] done")
            except Exception as e:
                self.send_message(chat_id, f"Error: {e}")
        else:
            # Bantuan sederhana jika pola tidak cocok
            self.send_message(chat_id, "Perintah: run <skill> | exec <command> | ask <prompt>")

    def loop(self) -> None:
        # Loop utama: ambil update dan proses pesan
        while True:
            try:
                updates = self.get_updates()
                for upd in updates:
                    self.offset = upd.get("update_id", 0) + 1
                    if self.verbose:
                        try:
                            print(f"[telegram] raw update: {json.dumps(upd)[:200]}...")
                        except Exception:
                            pass
                    msg = upd.get("message") or upd.get("edited_message") or upd.get("channel_post") or {}
                    chat = msg.get("chat") or {}
                    chat_id = chat.get("id")
                    text = msg.get("text")
                    if chat_id is not None and text:
                        self.handle_text(chat_id, text)
                    else:
                        if self.verbose:
                            print("[telegram] skip update: no text message")
            except Exception as e:
                # Tunggu sebentar saat error sebelum mencoba lagi
                time.sleep(2)


def run_bot_via_cli(token_arg: str | None, verbose: bool = False) -> int:
    # Memuat konfigurasi; ambil token dari argumen atau config.integrations
    cfg = Config.load()
    token = token_arg or cfg.integrations.get("telegram_token")
    if not token:
        print("Token Telegram tidak ditemukan. Berikan via --token atau simpan di config.integrations.telegram_token")
        return 1
    # Memastikan kanal yang diperlukan aktif
    if "terminal" not in cfg.channels:
        cfg.channels.append("terminal")
    if "ollama" not in cfg.channels:
        cfg.channels.append("ollama")
    # Menjalankan loop bot
    print("[telegram] starting bot loop (verbose=" + str(verbose) + ")")
    TelegramBridge(token, cfg, verbose=verbose).loop()
    return 0
