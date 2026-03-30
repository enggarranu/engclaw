"""
Planner agentik: membaca keluaran streaming dari Ollama dan mengeksekusi tool.

Format yang diharapkan dari model:
  NDJSON baris demi baris (teks), tiap baris berupa objek JSON:
  - {"say": "teks"}
  - {"tool": "shell", "command": "ls -la"}
  - {"tool": "skill", "name": "hello"}

Planner akan:
  - Mengirim hasil `say` ke callback output.
  - Menjalankan perintah `shell` via TerminalChannel (opsional cwd dari config).
  - Menjalankan skill via Gateway.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional


@dataclass
class Session:
    # Menyimpan keadaan sesi sederhana (mis. working directory)
    cwd: Path
    allow_shell: bool


def system_instructions() -> str:
    # Instruksi sistem untuk memandu LLM agar mengeluarkan NDJSON yang valid (ketat)
    return (
        "OUTPUT STRICT NDJSON ONLY. Each line is a standalone valid JSON object. "
        "Allowed forms: {\"say\":\"text\"} | {\"tool\":\"shell\",\"command\":\"...\"} | {\"tool\":\"skill\",\"name\":\"...\"} | "
        "{\"tool\":\"file_write\",\"path\":\"relative/path\",\"content\":\"...\"} | {\"tool\":\"file_append\",\"path\":\"...\",\"content\":\"...\"} | "
        "{\"tool\":\"file_read\",\"path\":\"...\"} | {\"tool\":\"file_list\",\"path\":\".\"}. "
        "All file paths MUST be relative to the session cwd and must not escape the base directory. "
        "No prose, no explanations, no markdown, no code fences. End with one or more {\"say\":\"...\"} lines."
    )


def _execute_data(data: dict, gateway, session: Session, on_output: Callable[[str], None]) -> bool:
    # Mengembalikan True jika ada aksi yang dijalankan
    if "say" in data:
        on_output(str(data["say"]))
        return False
    tool = data.get("tool")
    if tool == "shell":
        if not session.allow_shell:
            on_output("[ditolak] shell tidak diizinkan oleh konfigurasi")
            return False
        cmd = str(data.get("command") or "")
        rc, out, err = gateway.channels["terminal"].send({"command": cmd, "cwd": str(session.cwd)})
        on_output(f"$ {cmd}\nRC={rc}\nOUT=\n{out}\nERR=\n{err}")
        return True
    if tool == "skill":
        name = str(data.get("name") or "")
        result = gateway.run_skill(name)
        on_output(f"[skill {name}] ok={result.get('ok')} log={result.get('log')}")
        return True
    # File utilities
    def _safe(p: str) -> Path:
        base = Path(session.cwd).resolve()
        target = (base / p).resolve()
        if base not in target.parents and target != base:
            raise ValueError("path di luar workspace tidak diizinkan")
        return target

    try:
        if tool == "file_write":
            path = _safe(str(data.get("path") or ""))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(data.get("content") or ""))
            on_output(f"[file_write] {path} bytes={path.stat().st_size}")
            return True
        if tool == "file_append":
            path = _safe(str(data.get("path") or ""))
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(str(data.get("content") or ""))
            on_output(f"[file_append] {path} bytes={path.stat().st_size}")
            return True
        if tool == "file_read":
            path = _safe(str(data.get("path") or ""))
            content = path.read_text() if path.exists() else ""
            on_output(f"[file_read] {path}\n{content}")
            return True
        if tool == "file_list":
            path = _safe(str(data.get("path") or "."))
            if path.is_dir():
                items = "\n".join(sorted(p.name for p in path.iterdir()))
            else:
                items = path.name
            on_output(f"[file_list] {path}\n{items}")
            return True
    except Exception as e:
        on_output(f"Error: {e}")
        return False
    return False


def plan_and_execute(prompt: str, gateway, session: Session, on_output: Callable[[str], None], debug: bool = False, use_stream: bool = True, memory: Optional[str] = None, model_options: Optional[dict] = None) -> None:
    # Membuat prompt gabungan dengan instruksi sistem
    pre = system_instructions()
    if memory:
        pre += "\n" + str(memory)
    full_prompt = pre + "\n" + prompt
    # Memastikan kanal ollama tersedia
    if "ollama" not in gateway.channels:
        from ..channels.ollama import OllamaChannel
        gateway.channels["ollama"] = OllamaChannel()
    ch = gateway.channels["ollama"]

    executed = 0
    if use_stream:
        # Memproses stream NDJSON dari Ollama; gabungkan response per baris
        buffer = ""
        for obj in ch.send_stream({"model": (gateway.integrations.get("ollama") or {}).get("default_model", "llama3"), "prompt": full_prompt, "options": model_options or {}}):
            if debug:
                print(f"[stream] {obj}")
            # Objek dari Ollama punya kunci `response` per token; tambahkan ke buffer
            chunk = obj.get("response") or ""
            if obj.get("error"):
                on_output(f"Error: {obj.get('error')}")
                break
            if not chunk:
                continue
            buffer += chunk
            # Memproses baris lengkap saja
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    # Jika bukan JSON, kirim sebagai teks biasa
                    if debug:
                        print(f"[planner] non-json line: {line}")
                    on_output(line)
                    continue
                if _execute_data(data, gateway, session, on_output):
                    executed += 1
    else:
        # Non-stream: ambil satu respons penuh, lalu proses per-baris
        resp = ch.send({"model": (gateway.integrations.get("ollama") or {}).get("default_model", "llama3"), "prompt": full_prompt, "options": model_options or {}})
        text = resp.get("response", "")
        if debug:
            print(f"[non-stream] len={len(text)} error={resp.get('error')}")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                if debug:
                    print(f"[planner] non-json line: {line}")
                on_output(line)
                continue
            if _execute_data(data, gateway, session, on_output):
                executed += 1

    # Retry ketat jika tidak ada aksi yang dijalankan
    if executed == 0:
        strict = (
            system_instructions() +
            "\nJANGAN balas selain NDJSON. Jika tidak perlu tool, keluarkan minimal satu {\"say\":\"...\"}."
        )
        full_prompt2 = (strict + "\n" + prompt)
        resp2 = ch.send({"model": (gateway.integrations.get("ollama") or {}).get("default_model", "llama3"), "prompt": full_prompt2, "options": model_options or {}})
        text2 = resp2.get("response", "")
        for line in text2.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                on_output(line)
                continue
            _execute_data(data, gateway, session, on_output)
