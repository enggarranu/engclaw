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
    # Instruksi sistem untuk memandu LLM agar mengeluarkan NDJSON yang valid
    return (
        "Anda adalah planner yang hanya mengeluarkan baris NDJSON. Setiap baris harus JSON valid. "
        "Gunakan salah satu bentuk: {\"say\": \"teks\"} atau {\"tool\": \"shell\", \"command\": \"...\"} atau {\"tool\": \"skill\", \"name\": \"...\"}. "
        "Jangan keluarkan teks lain selain baris NDJSON. Selesaikan dengan beberapa baris {\"say\": \"...\"}."
    )


def plan_and_execute(prompt: str, gateway, session: Session, on_output: Callable[[str], None], debug: bool = False, use_stream: bool = True) -> None:
    # Membuat prompt gabungan dengan instruksi sistem
    full_prompt = system_instructions() + "\n" + prompt
    # Memastikan kanal ollama tersedia
    if "ollama" not in gateway.channels:
        from ..channels.ollama import OllamaChannel
        gateway.channels["ollama"] = OllamaChannel()
    ch = gateway.channels["ollama"]

    if use_stream:
        # Memproses stream NDJSON dari Ollama; gabungkan response per baris
        buffer = ""
        for obj in ch.send_stream({"model": (gateway.integrations.get("ollama") or {}).get("default_model", "llama3"), "prompt": full_prompt}):
            if debug:
                print(f"[stream] {obj}")
            # Objek dari Ollama punya kunci `response` per token; tambahkan ke buffer
            chunk = obj.get("response") or ""
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
                # Menangani `say`
                if "say" in data:
                    on_output(str(data["say"]))
                    continue
                # Menangani `tool`
                tool = data.get("tool")
                if tool == "shell":
                    if not session.allow_shell:
                        on_output("[ditolak] shell tidak diizinkan oleh konfigurasi")
                        continue
                    cmd = str(data.get("command") or "")
                    rc, out, err = gateway.channels["terminal"].send({"command": cmd, "cwd": str(session.cwd)})
                    on_output(f"$ {cmd}\nRC={rc}\nOUT=\n{out}\nERR=\n{err}")
                elif tool == "skill":
                    name = str(data.get("name") or "")
                    result = gateway.run_skill(name)
                    on_output(f"[skill {name}] ok={result.get('ok')} log={result.get('log')}")
    else:
        # Non-stream: ambil satu respons penuh, lalu proses per-baris
        resp = ch.send({"model": (gateway.integrations.get("ollama") or {}).get("default_model", "llama3"), "prompt": full_prompt})
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
            if "say" in data:
                on_output(str(data["say"]))
                continue
            tool = data.get("tool")
            if tool == "shell":
                if not session.allow_shell:
                    on_output("[ditolak] shell tidak diizinkan oleh konfigurasi")
                    continue
                cmd = str(data.get("command") or "")
                rc, out, err = gateway.channels["terminal"].send({"command": cmd, "cwd": str(session.cwd)})
                on_output(f"$ {cmd}\nRC={rc}\nOUT=\n{out}\nERR=\n{err}")
            elif tool == "skill":
                name = str(data.get("name") or "")
                result = gateway.run_skill(name)
                on_output(f"[skill {name}] ok={result.get('ok')} log={result.get('log')}")
