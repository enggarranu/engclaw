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
from urllib.error import HTTPError, URLError

from .config import Config
from .gateway import Gateway
from .agent.planner import Session, plan_and_execute
from .rag_store import get_rag_store


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
        # Auto-save sesi
        self._auto_save_seconds: int = int(self.cfg.agent.get("auto_save_seconds", 60))
        self._last_saved: dict[int, float] = {}

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

    def _get_file_path(self, file_id: str) -> str | None:
        try:
            url = self.api_url("getFile") + f"?file_id={file_id}"
            with urlopen(url, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok") and data.get("result") and data["result"].get("file_path"):
                return data["result"]["file_path"]
        except Exception:
            return None
        return None

    def _download_file_bytes(self, file_path: str) -> bytes:
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        with urlopen(url, timeout=60) as resp:
            return resp.read()

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
            if len(text) > 3500:
                print(f"[telegram] warn: message length may exceed Telegram limits (len={len(text)})")
        try:
            with urlopen(req, timeout=30) as resp:
                data = resp.read().decode("utf-8", "ignore")
                status = resp.getcode()
                ok = True
                msg_id = None
                try:
                    j = json.loads(data)
                    ok = bool(j.get("ok", True))
                    msg_id = ((j.get("result") or {}).get("message_id"))
                except Exception:
                    pass
                if self.verbose:
                    print(f"[telegram] send_message resp status={status} ok={ok} msg_id={msg_id}")
        except HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", "ignore")
            except Exception:
                pass
            print(f"[telegram] send_message error status={e.code} body={err_body[:500]}")
        except URLError as e:
            print(f"[telegram] send_message network error: {e}")
        except Exception as e:
            print(f"[telegram] send_message unexpected error: {e}")

    @staticmethod
    def _escape_html(s: str) -> str:
        # Meng-escape karakter HTML agar aman dalam parse_mode=HTML
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _pre(self, s: str) -> str:
        # Membungkus teks ke blok <pre> untuk tampilan monospaced rapi
        return f"<pre>{self._escape_html(s)}</pre>"

    def _is_code(self, s: str) -> bool:
        # Heuristik: anggap code hanya jika ada pola khas kode/log, bukan sekadar teks multi-baris
        if ("RC=" in s) or ("OUT=" in s) or ("ERR=" in s) or ("```" in s):
            return True
        # Cek beberapa baris awal untuk tanda khas shell/kode
        check_prefixes = ("$ ", "import ", "from ", "def ", "class ", "SELECT ", "INSERT ", "UPDATE ", "CREATE ", "#!/")
        for line in s.splitlines()[:5]:
            t = line.lstrip()
            if t.startswith(check_prefixes) or t.endswith(";"):
                return True
        return False

    def _send_text_or_code(self, chat_id: int, s: str) -> None:
        # Kirim sebagai teks biasa kecuali terdeteksi sebagai blok code
        if self._is_code(s):
            self.send_message(chat_id, self._pre(s))
        else:
            self.send_message(chat_id, s)

    def _start_session(self, chat_id: int, name: Optional[str] = None) -> None:
        self.sessions[chat_id] = True
        if name:
            opts = self.session_opts.setdefault(chat_id, {})
            opts["name"] = name
            # Prefill persona name jika belum ada
            persona = opts.get("persona") or {}
            if not persona.get("name"):
                persona["name"] = name
            opts["persona"] = persona
        if self.verbose:
            print(f"[session] start chat={chat_id} name={name or ''}")
        if self._persona_missing(chat_id):
            self._start_persona_wizard(chat_id)
        else:
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
        # Simpan juga nama ke opts agar ketika load, sesi mengetahui namanya
        opts = dict(self.session_opts.get(chat_id, {}))
        opts["name"] = name
        data = {
            "name": name,
            "chat_id": chat_id,
            "history": self.history.get(chat_id, []),
            "opts": opts,
        }
        path = sessions_dir / f"{name}.json"
        path.write_text(json.dumps(data, indent=2))
        self._send_text_or_code(chat_id, f"Sesi disimpan: {path}")

    def _write_session_named(self, chat_id: int, name: str) -> None:
        """
        Menulis sesi ke berkas TANPA mengirim pesan chat (dipakai autosave).
        """
        sessions_dir = self.cfg.workspace_dir / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        opts = dict(self.session_opts.get(chat_id, {}))
        opts["name"] = name
        data = {
            "name": name,
            "chat_id": chat_id,
            "history": self.history.get(chat_id, []),
            "opts": opts,
        }
        path = sessions_dir / f"{name}.json"
        path.write_text(json.dumps(data, indent=2))

    def _load_session_named(self, chat_id: int, name: str) -> None:
        sessions_dir = self.cfg.workspace_dir / "sessions"
        path = sessions_dir / f"{name}.json"
        if not path.exists():
            self._send_text_or_code(chat_id, f"Sesi '{name}' tidak ditemukan.")
            return
        data = json.loads(path.read_text())
        self.history[chat_id] = list(data.get("history") or [])
        self.session_opts[chat_id] = dict(data.get("opts") or {})
        # Pastikan nama terisi di session_opts untuk autosave
        self.session_opts[chat_id]["name"] = data.get("name", name)
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
        if opts.get("model"):
            info.append(f"Model teks (override sesi): {opts.get('model')}")
        if opts.get("vision_model"):
            info.append(f"Model visi (override sesi): {opts.get('vision_model')}")
        if opts.get("cwd"):
            info.append(f"CWD (override sesi): {opts.get('cwd')}")
        if "context_window" in opts:
            info.append(f"Jendela konteks (override sesi): {opts.get('context_window')}")
        if "allow_shell" in opts:
            info.append(f"Izin shell (override sesi): {bool(opts.get('allow_shell'))}")
        if "auto_save_seconds" in opts:
            info.append(f"Autosave (override sesi): {opts.get('auto_save_seconds')} detik")
        cfg = self.cfg
        info.append("Konfigurasi terapan:")
        info.append(f"Channels: {', '.join(cfg.channels)}")
        ag = cfg.agent or {}
        info.append(f"Agent.allow_shell={bool(ag.get('allow_shell', True))}")
        info.append(f"Agent.cwd={ag.get('cwd', str(cfg.workspace_dir))}")
        info.append(f"Agent.stream={bool(ag.get('stream', True))}")
        info.append(f"Agent.planner={ag.get('planner', 'ndjson')}")
        info.append(f"Agent.temperature={ag.get('temperature', 0.0)}")
        info.append(f"Agent.auto_save_seconds={ag.get('auto_save_seconds', 60)}")
        oll = (cfg.integrations.get("ollama") or {})
        if oll:
            if oll.get("endpoint"):
                info.append(f"Ollama.endpoint={oll.get('endpoint')}")
            if oll.get("default_model"):
                info.append(f"Ollama.default_model={oll.get('default_model')}")
            if oll.get("vision_model"):
                info.append(f"Ollama.vision_model={oll.get('vision_model')}")
        rag = getattr(cfg, "rag", {}) or {}
        if rag:
            info.append(f"RAG.enabled={bool(rag.get('enabled', False))}")
            if rag.get("vector_store"):
                info.append(f"RAG.vector_store={rag.get('vector_store')}")
            if rag.get("embedding_model"):
                info.append(f"RAG.embedding_model={rag.get('embedding_model')}")
            for k in ("chunk_size_tokens", "chunk_overlap_tokens", "top_k", "mmr_lambda", "auto_index_interval_seconds"):
                if k in rag:
                    info.append(f"RAG.{k}={rag.get(k)}")
            srcs = rag.get("sources") or []
            if srcs:
                info.append(f"RAG.sources={', '.join(srcs)}")
            tidb = rag.get("tidb") or {}
            if tidb:
                if tidb.get("host"):
                    info.append(f"TiDB.host={tidb.get('host')}")
                if "port" in tidb:
                    info.append(f"TiDB.port={tidb.get('port')}")
                if tidb.get("database"):
                    info.append(f"TiDB.database={tidb.get('database')}")
                if "ssl" in tidb:
                    info.append("TiDB.ssl=enabled")
        mcp = getattr(cfg, "mcp", {}) or {}
        if mcp:
            info.append(f"MCP.mode={mcp.get('mode', 'client')}")
            servers = mcp.get("servers") or []
            if servers:
                names = [str(s.get("name")) for s in servers if s.get("name")]
                info.append(f"MCP.servers={len(servers)} ({', '.join(names)})")
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

    def _persona_missing(self, chat_id: int) -> bool:
        opts = self.session_opts.get(chat_id, {})
        p = opts.get("persona") or {}
        # Wajib: name, alias (panggilan user), role
        return not (p.get("name") and p.get("alias") and p.get("role"))

    def _persona_prompt(self, chat_id: int) -> str:
        name_hint = self.session_opts.get(chat_id, {}).get("name") or "<nama_agen>"
        return (
            "Sesi dimulai. Lengkapi persona terlebih dahulu (WAJIB).\n"
            "Isi minimal: name, alias (panggilan Anda), role. Contoh:\n"
            f"/ask persona name={name_hint} alias=maseng role=\"asisten pribadi\" style=\"santai\" traits=\"sopan, tegas\"\n"
            "Setelah terisi, kirim pesan biasa untuk mulai chat."
        )

    def _wizard_active(self, chat_id: int) -> bool:
        return bool(self.session_opts.get(chat_id, {}).get("persona_wizard"))

    def _wizard_next_field(self, chat_id: int) -> Optional[str]:
        required = ["name", "alias", "role", "style", "traits", "system"]
        p = (self.session_opts.get(chat_id, {}) or {}).get("persona") or {}
        for key in required:
            if not p.get(key):
                return key
        return None

    def _start_persona_wizard(self, chat_id: int) -> None:
        self.session_opts.setdefault(chat_id, {})["persona_wizard"] = {"step": self._wizard_next_field(chat_id) or "name"}
        self._send_text_or_code(chat_id, self._wizard_prompt(chat_id))

    def _wizard_prompt(self, chat_id: int) -> str:
        step = (self.session_opts.get(chat_id, {}).get("persona_wizard") or {}).get("step") or "name"
        prompts = {
            "name": "Nama agen? contoh: jon",
            "alias": "Saya memanggil Anda sebagai? contoh: maseng",
            "role": "Peran agen? contoh: asisten pribadi",
            "style": "Gaya jawaban (opsional)? contoh: santai — kirim '-' untuk lewati",
            "traits": "Sifat (opsional)? contoh: sopan, tegas — kirim '-' untuk lewati",
            "system": "Instruksi sistem tambahan (opsional)? — kirim '-' untuk lewati",
        }
        return prompts.get(step, "Lengkapi persona.")

    def _wizard_handle_answer(self, chat_id: int, text: str) -> None:
        step = (self.session_opts.get(chat_id, {}).get("persona_wizard") or {}).get("step") or "name"
        val = text.strip()
        if val != "-":
            p = (self.session_opts.setdefault(chat_id, {}).setdefault("persona", {}))
            p[step] = val
        nxt = self._wizard_next_field(chat_id)
        if nxt is None:
            self.session_opts.get(chat_id, {}).pop("persona_wizard", None)
            self._send_text_or_code(chat_id, "Persona lengkap. Silakan mulai percakapan.")
        else:
            self.session_opts.get(chat_id, {}).setdefault("persona_wizard", {})["step"] = nxt
            self._send_text_or_code(chat_id, self._wizard_prompt(chat_id))

    def _help_text(self) -> str:
        return (
            "Perintah tersedia:\n"
            "- /ask start [nama] — mulai sesi (opsional beri nama)\n"
            "- /ask stop — hentikan sesi aktif\n"
            "- /ask save [nama] — simpan sesi ke workspace_dir/sessions\n"
            "- /ask load <nama> — muat sesi tersimpan dan aktifkan\n"
            "- /ask list — daftar sesi tersimpan\n"
            "- /ask info — ringkasan status & persona sesi\n"
            "- /ask set model=<model> — atur model teks per sesi\n"
            "- /ask set vision=<model> — atur model visi per sesi\n"
            "- /ask set cwd=<path> — atur working directory per sesi\n"
            "- /ask set window=<n> — atur jendela konteks percakapan\n"
            "- /ask set allow_shell=<true|false> — izinkan/larang perintah shell\n"
            "- /ask models — daftar model Ollama terpasang\n"
            "- /ask persona key=value … | teks — set identitas (name, alias, role, style, traits, system)\n"
            "- run <skill> — jalankan skill JSON di workspace\n"
            "- exec <command> — jalankan perintah shell (jika diizinkan)\n\n"
            "Mode chat natural: setelah sesi aktif, ketik pesan biasa. Agen akan merencanakan, dapat memakai tool seperti shell/skill dan operasi file dalam workspace."
        )

    def _ollama_base(self) -> str:
        ep = (self.cfg.integrations.get("ollama") or {}).get("endpoint", "http://localhost:11434/api/generate")
        if "/api/" in ep:
            return ep.split("/api/", 1)[0]
        return ep.rstrip("/")

    def _list_ollama_models(self) -> str:
        import datetime
        url = self._ollama_base() + "/api/tags"
        try:
            if self.verbose:
                print(f"[ollama] GET {url}")
            with urlopen(url, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            models = data.get("models") or []
            if not models:
                return "Tidak ada model Ollama terdeteksi."
            lines = ["NAME\tSIZE\tMODIFIED"]
            for m in models:
                name = m.get("name", "-")
                size = m.get("size") or m.get("size_bytes") or "-"
                if isinstance(size, int):
                    gb = size / (1024**3)
                    size_s = f"{gb:.1f} GB" if gb >= 1 else f"{size/1024**2:.0f} MB"
                else:
                    size_s = str(size)
                mod = m.get("modified") or m.get("modified_at") or "-"
                lines.append(f"{name}\t{size_s}\t{mod}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error mengambil daftar model: {e}"

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
        import threading
        import time as _time
        # Jalankan thread autosave
        if self._auto_save_seconds and self._auto_save_seconds > 0:
            def _autosave_loop():
                while True:
                    try:
                        now = _time.time()
                        for chat_id, active in list(self.sessions.items()):
                            if not active:
                                continue
                            secs = int(self.session_opts.get(chat_id, {}).get("auto_save_seconds", self._auto_save_seconds))
                            if secs <= 0:
                                continue
                            last = self._last_saved.get(chat_id, 0)
                            if now - last >= secs:
                                # Simpan ke nama sesi jika tersedia; jika belum, pakai default 'jon'
                                name = self.session_opts.get(chat_id, {}).get("name") or "jon"
                                try:
                                    self._write_session_named(chat_id, name)
                                    self._last_saved[chat_id] = now
                                    if self.verbose:
                                        print(f"[autosave] chat={chat_id} name={name}")
                                except Exception as e:
                                    if self.verbose:
                                        print(f"[autosave] error: {e}")
                    except Exception:
                        pass
                    _time.sleep(max(5, int(self._auto_save_seconds)))
            threading.Thread(target=_autosave_loop, daemon=True).start()

        # Jalankan thread indexing RAG jika diaktifkan
        try:
            rag_cfg = self.cfg.rag or {}
        except Exception:
            rag_cfg = {}
        if bool(rag_cfg.get("enabled", False)):
            interval = int(rag_cfg.get("auto_index_interval_seconds", 300))
            if interval > 0:
                def _rag_loop():
                    import time as _t
                    while True:
                        try:
                            store = get_rag_store(self.cfg.workspace_dir, rag_cfg, self.cfg.integrations)
                            total = store.index_workspace() + store.index_readme()
                            if self.verbose and total:
                                print(f"[rag] indexed {total} chunks")
                        except Exception as e:
                            if self.verbose:
                                print(f"[rag] error: {e}")
                        _t.sleep(interval)
                threading.Thread(target=_rag_loop, daemon=True).start()
                if self.verbose:
                    print(f"[rag] scheduler started interval={interval}s enabled=true store={rag_cfg.get('vector_store')} model={rag_cfg.get('embedding_model')}")

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
                        continue
                    # Tangani gambar (photo/document image)
                    photo = (msg.get("photo") or [])
                    document = msg.get("document") or {}
                    caption = msg.get("caption") or ""
                    handled_image = False
                    if photo:
                        # Ambil ukuran terbesar
                        file_id = sorted(photo, key=lambda x: x.get("file_size", 0))[-1].get("file_id")
                        handled_image = self.handle_image(chat_id, file_id, caption)
                    elif document and str(document.get("mime_type", "")).startswith("image/"):
                        file_id = document.get("file_id")
                        handled_image = self.handle_image(chat_id, file_id, caption)
                    elif document:
                        # Tangani dokumen umum: simpan ke workspace dan indeks ke RAG jika diminta
                        try:
                            self.handle_document(chat_id, document, caption)
                            continue
                        except Exception as e:
                            if self.verbose:
                                print(f"[telegram] document error: {e}")
                            self._send_text_or_code(chat_id, f"Error dokumen: {e}")
                            continue
                    if handled_image:
                        continue
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
            if self.verbose:
                print(f"[ask] tail='{tail}' raw='{raw_tail}'")
            if tail == "" or tail.startswith("start"):
                parts = tail.split()
                # Default nama sesi ke 'jon' jika tidak diberikan
                name = parts[1] if len(parts) >= 2 else "jon"
                if self.verbose:
                    print(f"[ask] start name='{name}'")
                self._start_session(chat_id, name)
                return
            if tail.startswith("stop") or tail.startswith("end"):
                if self.verbose:
                    print("[ask] stop")
                self._stop_session(chat_id)
                return
            if tail in ("help", "?", "h"):
                if self.verbose:
                    print("[ask] help requested")
                self._send_text_or_code(chat_id, self._help_text())
                return
            if tail.startswith("models"):
                if self.verbose:
                    print("[ask] models")
                txt = self._list_ollama_models()
                self._send_text_or_code(chat_id, txt)
                return
            if tail.startswith("set"):
                # /ask set key=value ...  (mendukung model, vision, cwd, window, allow_shell)
                content = raw_tail[3:].strip()
                kvs = list(re.finditer(r"([a-zA-Z_]+)\s*=\s*(\"([^\"]*)\"|'([^']*)'|[^\s]+)", content))
                if not kvs:
                    self._send_text_or_code(chat_id, "Format: /ask set vision=<model> | model=<model> | cwd=<path> | window=<n> | allow_shell=<true|false> | autosave=<seconds>")
                    return
                dst = self.session_opts.setdefault(chat_id, {})
                for m2 in kvs:
                    key = m2.group(1)
                    val = m2.group(3) or m2.group(4) or m2.group(2)
                    if key in ("vision", "vision_model"):
                        dst["vision_model"] = val
                    elif key in ("model",):
                        dst["model"] = val
                    elif key == "cwd":
                        dst["cwd"] = val
                    elif key in ("window", "context_window"):
                        try:
                            dst["context_window"] = int(val)
                        except Exception:
                            pass
                    elif key == "allow_shell":
                        dst["allow_shell"] = str(val).lower() in ("1", "true", "yes", "y")
                    elif key in ("autosave", "auto_save_seconds"):
                        try:
                            dst["auto_save_seconds"] = int(val)
                        except Exception:
                            pass
                self._send_text_or_code(chat_id, "Setelan sesi diperbarui.")
                return
            if tail.startswith("save"):
                if self.verbose:
                    print("[ask] save")
                parts = tail.split()
                if len(parts) >= 2:
                    self._save_session_named(chat_id, parts[1])
                else:
                    self._save_session(chat_id)
                return
            if tail.startswith("load"):
                if self.verbose:
                    print("[ask] load")
                parts = tail.split()
                if len(parts) >= 2:
                    self._load_session_named(chat_id, parts[1])
                else:
                    self._send_text_or_code(chat_id, "Format: /ask load <nama>")
                return
            if tail.startswith("list"):
                if self.verbose:
                    print("[ask] list")
                self._list_sessions(chat_id)
                return
            if tail.startswith("info"):
                if self.verbose:
                    print("[ask] info")
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
                if self._persona_missing(chat_id):
                    missing = []
                    p = self.session_opts.get(chat_id, {}).get("persona") or {}
                    if not p.get("name"): missing.append("name")
                    if not p.get("alias"): missing.append("alias")
                    if not p.get("role"): missing.append("role")
                    self._send_text_or_code(chat_id, "Persona diperbarui sebagian. Masih kurang: " + ", ".join(missing))
                else:
                    self._send_text_or_code(chat_id, "Persona lengkap. Silakan mulai percakapan.")
                return
            if self.verbose:
                print(f"[ask] unknown tail '{tail}' -> help")
            self._send_text_or_code(chat_id, self._help_text())
            return
        # Alias sederhana untuk memulai: /start
        if tl.strip() in ("/start", "start"):
            # Gunakan 'jon' sebagai nama default saat memulai sesi cepat
            default_name = self.session_opts.get(chat_id, {}).get("name") or "jon"
            self._start_session(chat_id, default_name)
            return

        # Perintah RAG
        m_rag = re.match(r"^/rag(?:@\w+)?(?:\s+(.*))?$", t, flags=re.IGNORECASE)
        if m_rag:
            tail = (m_rag.group(1) or "").strip()
            rag_cfg = self.cfg.rag or {}
            store = get_rag_store(self.cfg.workspace_dir, rag_cfg, self.cfg.integrations)
            if tail.lower().startswith("index") or tail == "":
                total = store.index_workspace() + store.index_readme()
                self._send_text_or_code(chat_id, f"RAG diindeks: {total} chunks")
                return
            if tail.lower().startswith("search "):
                q = tail.split(" ", 1)[1].strip()
                hits = store.search(q, top_k=int(rag_cfg.get("top_k", 5)))
                if not hits:
                    self._send_text_or_code(chat_id, "Tidak ada hasil.")
                    return
                lines = [f"Hasil: {len(hits)}"]
                for h in hits:
                    lines.append(f"- {h['doc_id']}#{h['chunk_id']} score={h['score']:.3f}\n{h['text']}")
                self._send_text_or_code(chat_id, "\n".join(lines))
                return
            if tail.lower().startswith("add "):
                parts = tail.split()
                if len(parts) >= 3:
                    type_ = parts[1]
                    uri = " ".join(parts[2:])
                    store.add_source(type_, uri)
                    self._send_text_or_code(chat_id, "Sumber ditambahkan.")
                else:
                    self._send_text_or_code(chat_id, "Format: /rag add <type> <uri>")
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
            pend = (self.session_opts.get(chat_id, {}) or {}).get("pending_file")
            if pend:
                ans = tl
                if ans in ("ya", "yes", "y", "lanjut", "ok", "analisa") or ans.startswith("/confirm") or ans == pend.get("suggest_name", "").lower():
                    p = Path(pend.get("suggest_path"))
                    if p.exists():
                        try:
                            content = p.read_text(errors="ignore")
                        except Exception as e:
                            self._send_text_or_code(chat_id, f"Error baca file: {e}")
                            self.session_opts.get(chat_id, {}).pop("pending_file", None)
                            return
                        self.session_opts.get(chat_id, {}).pop("pending_file", None)
                        self._send_text_or_code(chat_id, f"Konfirmasi: {p.name}. Analisa dimulai.")
                        opts = self.session_opts.get(chat_id, {})
                        cwd = Path(opts.get("cwd") or self.cfg.agent.get("cwd", self.cfg.workspace_dir))
                        allow_shell = bool(opts.get("allow_shell", self.cfg.agent.get("allow_shell", True)))
                        sess = Session(cwd=cwd, allow_shell=allow_shell)
                        def emit(s: str) -> None:
                            self.history.setdefault(chat_id, []).append(f"A: {s}")
                            self._send_text_or_code(chat_id, s)
                        use_stream = bool(self.cfg.agent.get("stream", True))
                        persona_text = self._build_context(chat_id)
                        planner_type = str(self.cfg.agent.get("planner", "ndjson")).lower()
                        temperature = float(self.cfg.agent.get("temperature", 0.2))
                        default_text_model = (self.cfg.integrations.get("ollama") or {}).get("default_model", "llama3")
                        mem = f"Target: {p}\nIsi:\n" + (content[:6000])
                        try:
                            if planner_type == "langchain":
                                from .agent.langchain_planner import plan_and_execute_lc
                                model = default_text_model
                                if self.verbose:
                                    print(f"[planner] text via LangChain model={model} temp={temperature} stream={use_stream}")
                                plan_and_execute_lc(raw, self.gw, sess, emit, model=model, system_text=((persona_text or "") + "\n" + mem), temperature=temperature)
                            else:
                                if self.verbose:
                                    print(f"[planner] text via NDJSON model={default_text_model} temp={temperature} stream={use_stream}")
                                plan_and_execute(
                                    raw, self.gw, sess, emit,
                                    debug=self.verbose,
                                    use_stream=use_stream,
                                    memory=((persona_text or "") + "\n" + mem),
                                    model_options={"temperature": temperature}
                                )
                        except Exception as e:
                            self._send_text_or_code(chat_id, f"Error: {e}")
                        return
                elif ans in ("tidak", "no", "n"):
                    self.session_opts.get(chat_id, {}).pop("pending_file", None)
                    self._send_text_or_code(chat_id, "Dibatalkan. File tidak dikonfirmasi.")
                    return
            if self._wizard_active(chat_id):
                self._wizard_handle_answer(chat_id, t)
                return
            # Wajib persona lengkap sebelum menjalankan planner
            if self._persona_missing(chat_id):
                self._start_persona_wizard(chat_id)
                return
            # Ambil opsi sesi jika ada; fallback ke config
            opts = self.session_opts.get(chat_id, {})
            cwd = Path(opts.get("cwd") or self.cfg.agent.get("cwd", self.cfg.workspace_dir))
            allow_shell = bool(opts.get("allow_shell", self.cfg.agent.get("allow_shell", True)))
            sess = Session(cwd=cwd, allow_shell=allow_shell)
            # Deteksi permintaan file/path yang tidak lengkap lalu konfirmasi
            token = None
            parts = t.split()
            for w in reversed(parts):
                if ("/" in w) or ("." in w) or (w.lower().startswith("readme") or w.lower().endswith("_engclaw")):
                    token = w
                    break
            if token:
                base = Path(cwd)
                cand = base / token if not Path(token).is_absolute() else Path(token)
                if self.verbose:
                    print(f"[file] probe token={token} base={base}")
                if cand.exists():
                    self.session_opts.setdefault(chat_id, {})["pending_file"] = {"suggest_path": str(cand), "suggest_name": cand.name}
                    self._send_text_or_code(chat_id, f"File ditemukan: {cand}. Analisa sekarang? Balas 'ya' atau 'tidak'.")
                    return
                # cari kandidat
                picks = []
                name = Path(token).name
                exts = [".md", ".txt", ".json", ".py"]
                for ext in exts:
                    p = base / (name + ext)
                    if p.exists():
                        picks.append(p)
                if not picks:
                    for p in base.rglob("*"):
                        if p.is_file() and (name.lower() in p.name.lower() or p.stem.lower() == name.lower()):
                            picks.append(p)
                            if len(picks) >= 10:
                                break
                if picks:
                    choice = sorted(picks, key=lambda x: (0 if x.suffix in (".md", ".txt") else 1, len(x.name)))[0]
                    self.session_opts.setdefault(chat_id, {})["pending_file"] = {"suggest_path": str(choice), "suggest_name": choice.name}
                    extras = ", ".join(p.name for p in picks[:5])
                    msg = f"File '{name}' tidak ditemukan. Maksud Anda '{choice.name}'? Balas 'ya' untuk konfirmasi atau 'tidak' untuk batal."
                    if len(picks) > 1:
                        msg += f"\nKandidat lain: {extras}"
                    self._send_text_or_code(chat_id, msg)
                    return
                else:
                    self._send_text_or_code(chat_id, f"File '{name}' tidak dapat diidentifikasi di workspace.")
                    return
            m_file = re.search(r"\b(file|berkas)\s+([A-Za-z0-9_.\-]+)\b", t, flags=re.IGNORECASE)
            if m_file:
                name = m_file.group(2)
                base = Path(cwd)
                candidate = base / name
                if not candidate.exists():
                    exts = [".md", ".txt", ".json", ".py"]
                    picks = []
                    for ext in exts:
                        p = base / (name + ext)
                        if p.exists():
                            picks.append(p)
                    if not picks:
                        for p in base.rglob("*"):
                            if p.is_file() and (name.lower() in p.name.lower() or p.stem.lower() == name.lower()):
                                picks.append(p)
                    if picks:
                        choice = sorted(picks, key=lambda x: (0 if x.suffix in (".md", ".txt") else 1, len(x.name)))[0]
                        self.session_opts.setdefault(chat_id, {})["pending_file"] = {"suggest_path": str(choice), "suggest_name": choice.name}
                        self._send_text_or_code(chat_id, f"File '{name}' tidak ditemukan. Maksud Anda '{choice.name}'? Balas 'ya' untuk konfirmasi atau 'tidak' untuk batal.")
                        return
                    else:
                        self._send_text_or_code(chat_id, f"File '{name}' tidak dapat diidentifikasi di workspace.")
                        return
            def emit(s: str) -> None:
                self.history.setdefault(chat_id, []).append(f"A: {s}")
                self._send_text_or_code(chat_id, s)
            use_stream = bool(self.cfg.agent.get("stream", True))
            # Persona + konteks ringkas
            persona_text = self._build_context(chat_id)
            planner_type = str(self.cfg.agent.get("planner", "ndjson")).lower()
            temperature = float(self.cfg.agent.get("temperature", 0.2))
            # Tentukan model teks untuk logging (mengikuti default NDJSON)
            default_text_model = (self.cfg.integrations.get("ollama") or {}).get("default_model", "llama3")
            try:
                if planner_type == "langchain":
                    from .agent.langchain_planner import plan_and_execute_lc
                    model = default_text_model
                    if self.verbose:
                        print(f"[planner] text via LangChain model={model} temp={temperature} stream={use_stream}")
                    plan_and_execute_lc(raw, self.gw, sess, emit, model=model, system_text=persona_text or None, temperature=temperature)
                else:
                    if self.verbose:
                        print(f"[planner] text via NDJSON model={default_text_model} temp={temperature} stream={use_stream}")
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
        # Jika belum ada sesi, mulai otomatis dengan nama default 'jon'
        self._start_session(chat_id, "jon")
        return

    def handle_image(self, chat_id: int, file_id: str | None, caption: str | None) -> bool:
        if file_id is None:
            return False
        fp = self._get_file_path(file_id)
        if not fp:
            return False
        try:
            content = self._download_file_bytes(fp)
        except Exception as e:
            self._send_text_or_code(chat_id, f"Error unduh gambar: {e}")
            return True
        import base64
        b64 = base64.b64encode(content).decode("utf-8")
        prompt = caption or "Jelaskan isi gambar ini secara ringkas."
        # Pastikan kanal ollama ada
        if "ollama" not in self.gw.channels:
            self._send_text_or_code(chat_id, "Kanal 'ollama' belum aktif untuk pemrosesan gambar.")
            return True
        # Pilih model visi: prioritas sesi → config.ollama.vision_model → fallback llava
        sess_vm = (self.session_opts.get(chat_id, {}) or {}).get("vision_model")
        cfg_vm = (self.cfg.integrations.get("ollama") or {}).get("vision_model")
        model = sess_vm or cfg_vm or "llava:latest"
        if self.verbose:
            src = "session" if sess_vm else ("config" if cfg_vm else "fallback")
            print(f"[planner] vision image model={model} source={src} caption_len={len(prompt)}")
        opts = {"temperature": float(self.cfg.agent.get("temperature", 0.2))}
        resp = self.gw.channels["ollama"].send({"model": model, "prompt": prompt, "images": [b64], "options": opts})
        if resp.get("ok"):
            self._send_text_or_code(chat_id, resp.get("response", ""))
        else:
            self._send_text_or_code(chat_id, f"Error: {resp.get('error')}")
        return True

    def handle_document(self, chat_id: int, document: dict, caption: str | str | None) -> None:
        file_id = document.get("file_id")
        file_name = document.get("file_name") or f"upload_{file_id or 'unknown'}"
        mime = str(document.get("mime_type", ""))
        if self.verbose:
            print(f"[doc] recv file_id={file_id} name={file_name} mime={mime} caption_len={len(caption or '')}")
        fp = self._get_file_path(file_id)
        if not fp:
            raise RuntimeError("file path tidak ditemukan dari Telegram")
        content = self._download_file_bytes(fp)
        # Simpan ke workspace/docs
        target_dir = self.cfg.workspace_dir / "docs"
        target_dir.mkdir(parents=True, exist_ok=True)
        dst = target_dir / file_name
        dst.write_bytes(content)
        if self.verbose:
            print(f"[doc] saved to {dst} bytes={len(content)}")
        # Heuristik: jika caption mengandung kata kunci, lakukan indeks RAG
        want_index = False
        c = (caption or "").lower()
        for kw in ("rag", "index", "simpan di rag", "pelajari"):
            if kw in c:
                want_index = True
                break
        if want_index:
            rag_cfg = self.cfg.rag or {}
            store = get_rag_store(self.cfg.workspace_dir, rag_cfg, self.cfg.integrations)
            added = store.index_file(dst)
            self._send_text_or_code(chat_id, f"Dokumen disimpan: {dst}\nRAG diindeks: {added} chunks")
        else:
            self._send_text_or_code(chat_id, f"Dokumen disimpan: {dst}")


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
    try:
        rag_cfg = cfg.rag or {}
    except Exception:
        rag_cfg = {}
    try:
        mcp_cfg = cfg.mcp or {}
    except Exception:
        mcp_cfg = {}
    print(f"[init] channels={cfg.channels}")
    print(f"[init] rag.enabled={bool(rag_cfg.get('enabled', False))} store={rag_cfg.get('vector_store')} embed_model={rag_cfg.get('embedding_model')}")
    print(f"[init] mcp.mode={mcp_cfg.get('mode', 'client')} servers={len(mcp_cfg.get('servers', []))}")
    TelegramBridge(token, cfg, verbose=verbose).loop()
    return 0
