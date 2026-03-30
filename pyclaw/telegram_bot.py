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
from typing import Optional, Any
import re
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
        # Manajemen sesi per chat
        self.sessions: dict[int, bool] = {}
        self.history: dict[int, list[str]] = {}
        self.session_opts: dict[int, dict[str, Any]] = {}

    def _persona_text(self, chat_id: int) -> str:
        # Membentuk teks persona untuk dijadikan memory sistem
        opts = self.session_opts.get(chat_id, {})
        p = opts.get("persona") or {}
        parts = []
        sys = p.get("system")
        if sys:
            parts.append(str(sys))
        name = p.get("name")
        role = p.get("role")
        alias = p.get("alias")
        style = p.get("style")
        traits = p.get("traits")
        if name or role or alias:
            descr = []
            if name:
                descr.append(f"Nama agen: {name}")
            if role:
                descr.append(f"Peran: {role}")
            if alias:
                descr.append(f"Panggil pengguna: {alias}")
            parts.append("; ".join(descr))
        if style:
            parts.append(f"Gaya: {style}")
        if traits:
            parts.append(f"Sifat: {traits}")
        return "\n".join(parts)

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

    def _is_code(self, s: str) -> bool:
        # Heuristik sederhana: blok berbaris atau pola RC/OUT/ERR atau baris shell
        return ("\n" in s) or ("RC=" in s) or ("OUT=" in s) or ("ERR=" in s) or s.startswith("$ ")

    def _send_text_or_code(self, chat_id: int, s: str) -> None:
        # Kirim sebagai teks biasa kecuali terdeteksi sebagai blok code
        if self._is_code(s):
            self.send_message(chat_id, self._pre(s))
        else:
            self.send_message(chat_id, s)

    def _start_session(self, chat_id: int, name: Optional[str] = None) -> None:
        self.sessions[chat_id] = True
        if name:
            self.session_opts.setdefault(chat_id, {})["name"] = name
        if self.verbose:
            print(f"[session] start chat={chat_id} name={name or ''}")
        self._send_text_or_code(chat_id, "Sesi dimulai. Kirim pesan biasa untuk bertanya.")

    def _stop_session(self, chat_id: int) -> None:
        self.sessions[chat_id] = False
        if self.verbose:
            print(f"[session] stop chat={chat_id}")
        self._send_text_or_code(chat_id, "Sesi dihentikan.")

    def _save_session(self, chat_id: int) -> None:
        # Simpan riwayat ke workspace logs
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        sessions_dir = self.cfg.workspace_dir / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        name = f"chat_{chat_id}_{ts}"
        data = {
            "name": name,
            "chat_id": chat_id,
            "history": self.history.get(chat_id, []),
            "opts": self.session_opts.get(chat_id, {}),
        }
        path = sessions_dir / f"{name}.json"
        path.write_text(json.dumps(data, indent=2))
        self._send_text_or_code(chat_id, f"Sesi disimpan: {path}")

    def _save_session_named(self, chat_id: int, name: str) -> None:
        sessions_dir = self.cfg.workspace_dir / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "name": name,
            "chat_id": chat_id,
            "history": self.history.get(chat_id, []),
            "opts": self.session_opts.get(chat_id, {}),
        }
        path = sessions_dir / f"{name}.json"
        path.write_text(json.dumps(data, indent=2))
        self._send_text_or_code(chat_id, f"Sesi disimpan: {path}")

    def _load_session_named(self, chat_id: int, name: str) -> None:
        sessions_dir = self.cfg.workspace_dir / "sessions"
        path = sessions_dir / f"{name}.json"
        if not path.exists():
            self._send_text_or_code(chat_id, f"Sesi '{name}' tidak ditemukan.")
            return
        data = json.loads(path.read_text())
        self.history[chat_id] = list(data.get("history") or [])
        self.session_opts[chat_id] = dict(data.get("opts") or {})
        self.sessions[chat_id] = True
        # Kirim ringkasan konteks setelah load agar pengguna tahu persona aktif
        persona_txt = self._persona_text(chat_id)
        summary = [f"Sesi '{name}' dimuat dan diaktifkan."]
        if persona_txt:
            summary.append("Konteks:")
            summary.append(persona_txt)
        self._send_text_or_code(chat_id, "\n".join(summary))

    def _session_info(self, chat_id: int) -> str:
        info = ["Status sesi: aktif" if self.sessions.get(chat_id) else "Status sesi: non-aktif"]
        opts = self.session_opts.get(chat_id, {})
        if opts.get("name"):
            info.append(f"Nama sesi: {opts.get('name')}")
        ptxt = self._persona_text(chat_id)
        if ptxt:
            info.append("Persona:")
            info.append(ptxt)
        return "\n".join(info)

    def _build_context(self, chat_id: int) -> str:
        # Bangun konteks ringkas dari persona + jendela percakapan terakhir
        opts = self.session_opts.get(chat_id, {})
        win = int(opts.get("context_window", 8))
        persona = self._persona_text(chat_id)
        # Ambil hanya baris U:/A: yang single-line dan bukan blok kode RC/OUT/ERR
        lines = []
        for s in self.history.get(chat_id, [])[-(win*2):]:
            t = s.strip()
            if t.startswith("U:") or t.startswith("A:"):
                if ("\n" not in t) and ("RC=" not in t) and ("OUT=" not in t) and ("ERR=" not in t):
                    lines.append(t)
        ctx = "\n".join(lines)
        if persona and ctx:
            return persona + "\n" + ctx
        return persona or ctx

    def _list_sessions(self, chat_id: int) -> None:
        sessions_dir = self.cfg.workspace_dir / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        items = sorted(p.stem for p in sessions_dir.glob("*.json"))
        if not items:
            self._send_text_or_code(chat_id, "Tidak ada sesi tersimpan.")
            return
        self._send_text_or_code(chat_id, "Daftar sesi:\n" + "\n".join(items))

    def handle_text(self, chat_id: int, text: str) -> None:
        # Delegasi ke implementasi yang lebih rapi
        self.handle_text2(chat_id, text)

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
                        self.handle_text2(chat_id, text)
                    else:
                        if self.verbose:
                            print("[telegram] skip update: no text message")
            except Exception:
                time.sleep(2)

    def handle_text2(self, chat_id: int, text: str) -> None:
        # Versi rapi dengan dukungan sesi natural
        t = text.strip()
        tl = t.lower()
        # Perintah sesi via /ask
        m = re.match(r"^/ask(?:@\w+)?(?:\s+(.*))?$", t, flags=re.IGNORECASE)
        if m:
            raw_tail = (m.group(1) or "").strip()
            tail = raw_tail.lower()
            if tail == "" or tail.startswith("start"):
                parts = tail.split()
                name = parts[1] if len(parts) >= 2 else None
                self._start_session(chat_id, name)
                return
            if tail.startswith("stop") or tail.startswith("end"):
                self._stop_session(chat_id)
                return
            if tail.startswith("save"):
                parts = tail.split()
                if len(parts) >= 2:
                    self._save_session_named(chat_id, parts[1])
                else:
                    self._save_session(chat_id)
                return
            if tail.startswith("load"):
                parts = tail.split()
                if len(parts) >= 2:
                    self._load_session_named(chat_id, parts[1])
                else:
                    self._send_text_or_code(chat_id, "Format: /ask load <nama>")
                return
            if tail.startswith("list"):
                self._list_sessions(chat_id)
                return
            if tail.startswith("info"):
                self._send_text_or_code(chat_id, self._session_info(chat_id))
                return
            if tail.startswith("persona"):
                # Set persona dari bentuk: key=value atau teks bebas
                content = raw_tail[7:].strip()  # setelah 'persona'
                persona: dict[str, Any] = dict(self.session_opts.get(chat_id, {}).get("persona") or {})
                kvs = list(re.finditer(r"([a-zA-Z_]+)\s*=\s*(\"([^\"]*)\"|'([^']*)'|[^\s]+)", content))
                if kvs:
                    for m2 in kvs:
                        key = m2.group(1)
                        val = m2.group(3) or m2.group(4) or m2.group(2)
                        persona[key] = val
                elif content:
                    persona["system"] = content
                self.session_opts.setdefault(chat_id, {})["persona"] = persona
                self._send_text_or_code(chat_id, "Persona diperbarui.")
                return
        # Aksi eksplisit: run
        if tl.startswith("run ") or tl.startswith("/run "):
            name = t.split(" ", 1)[1].strip() if " " in t else ""
            result = self.gw.run_skill(name)
            if result.get("ok"):
                try:
                    log_path = Path(result["log"]).resolve()
                    snippet = log_path.read_text()
                except Exception:
                    snippet = f"Skill '{name}' selesai. Log: {result['log']}"
                self._send_text_or_code(chat_id, snippet)
            else:
                self._send_text_or_code(chat_id, f"Gagal: {result.get('error')}")
            return
        # Aksi eksplisit: exec
        if tl.startswith("exec ") or tl.startswith("/exec "):
            cmd = t.split(" ", 1)[1].strip() if " " in t else ""
            rc, out, err = self.gw.channels["terminal"].send({"command": cmd})
            block = f"RC={rc}\nOUT=\n{out}\nERR=\n{err}"
            self._send_text_or_code(chat_id, block)
            return
        # Natural chat (sesi aktif)
        if self.sessions.get(chat_id, False):
            raw = t
            # Simpan input pengguna untuk konteks
            self.history.setdefault(chat_id, []).append(f"U: {t}")
            # Ambil opsi sesi jika ada; fallback ke config
            opts = self.session_opts.get(chat_id, {})
            cwd = Path(opts.get("cwd") or self.cfg.agent.get("cwd", self.cfg.workspace_dir))
            allow_shell = bool(opts.get("allow_shell", self.cfg.agent.get("allow_shell", True)))
            sess = Session(cwd=cwd, allow_shell=allow_shell)
            def emit(s: str) -> None:
                self.history.setdefault(chat_id, []).append(f"A: {s}")
                self._send_text_or_code(chat_id, s)
            use_stream = bool(self.cfg.agent.get("stream", True))
            # Persona + konteks ringkas
            persona_text = self._build_context(chat_id)
            planner_type = str(self.cfg.agent.get("planner", "ndjson")).lower()
            temperature = float(self.cfg.agent.get("temperature", 0.2))
            try:
                if planner_type == "langchain":
                    from .agent.langchain_planner import plan_and_execute_lc
                    model = (self.cfg.integrations.get("ollama") or {}).get("default_model", "llama3")
                    plan_and_execute_lc(raw, self.gw, sess, emit, model=model, system_text=persona_text or None, temperature=temperature)
                else:
                    plan_and_execute(
                        raw, self.gw, sess, emit,
                        debug=self.verbose,
                        use_stream=use_stream,
                        memory=persona_text or None,
                        model_options={"temperature": temperature}
                    )
            except Exception as e:
                self._send_text_or_code(chat_id, f"Error: {e}")
            return
        # Default bantuan
        self._send_text_or_code(chat_id, "Gunakan /ask start untuk memulai sesi percakapan.")


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
