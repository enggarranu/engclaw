"""
Gateway Pyclaw: orkestrator sederhana untuk menjalankan skill.

Tanggung jawab:
- Menginisialisasi kanal yang diaktifkan dari konfigurasi.
- Menjalankan langkah-langkah skill secara berurutan.
- Menulis log hasil eksekusi ke workspace.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from .channels.http import HTTPChannel
from .channels.terminal import TerminalChannel
from .skills.loader import Skill, SkillLoader
from .channels.ollama import OllamaChannel


class Gateway:
    """
    Orkestrator eksekusi skill.
    """

    def __init__(self, workspace_dir: Path, skills_dir: Path, channels: list[str], integrations: Dict[str, Any] | None = None):
        # Menyimpan referensi lokasi workspace dan skills
        self.workspace_dir = workspace_dir
        self.skills_dir = skills_dir
        # Menyimpan konfigurasi integrasi eksternal (mis. Ollama)
        self.integrations = integrations or {}
        # Membuat map kanal berdasarkan nama yang diaktifkan
        self.channels: Dict[str, Any] = {}
        if "http" in channels:
            self.channels["http"] = HTTPChannel()
        if "terminal" in channels:
            self.channels["terminal"] = TerminalChannel()
        if "ollama" in channels:
            ep = (self.integrations.get("ollama") or {}).get("endpoint")
            self.channels["ollama"] = OllamaChannel(endpoint=ep or "http://localhost:11434/api/generate")
        # Menyiapkan pemuat skill untuk mengambil definisi skill
        self.loader = SkillLoader(skills_dir)

    def run_skill(self, name: str) -> Dict[str, Any]:
        """
        Menjalankan sebuah skill berdasarkan namanya.

        Mengembalikan dict hasil eksekusi berisi ringkasan langkah dan output.
        """
        # Memuat skill dari berkas JSON
        skill = self.loader.load_skill(name)
        if not skill:
            return {"ok": False, "error": f"Skill '{name}' tidak ditemukan"}

        # Menjalankan tiap langkah dan mengumpulkan hasil
        results = []
        for i, step in enumerate(skill.steps, start=1):
            action = step.get("action")
            # Menangani aksi "print" untuk mencetak pesan
            if action == "print":
                msg = step.get("message", "")
                results.append({"step": i, "action": "print", "output": msg})
            # Menangani aksi "http_get" menggunakan HTTPChannel
            elif action == "http_get":
                url = step.get("url")
                resp = self.channels["http"].send({"url": url})
                results.append({"step": i, "action": "http_get", "status": resp["status"]})
            # Menangani aksi "shell" menggunakan TerminalChannel
            elif action == "shell":
                cmd = step.get("command")
                rc, out, err = self.channels["terminal"].send({"command": cmd})
                results.append({"step": i, "action": "shell", "returncode": rc, "stdout": out, "stderr": err})
            # Menangani aksi "ollama_prompt" untuk meminta respons LLM
            elif action == "ollama_prompt":
                default_model = (self.integrations.get("ollama") or {}).get("default_model", "llama3")
                model = step.get("model") or default_model
                prompt = step.get("prompt")
                if "ollama" not in self.channels:
                    ep = (self.integrations.get("ollama") or {}).get("endpoint")
                    self.channels["ollama"] = OllamaChannel(endpoint=ep or "http://localhost:11434/api/generate")
                resp = self.channels["ollama"].send({"model": model, "prompt": prompt})
                results.append({"step": i, "action": "ollama_prompt", **resp})
            else:
                # Mengumpulkan info jika aksi tidak dikenal
                results.append({"step": i, "action": action, "error": "aksi tidak dikenal"})

        # Menulis log eksekusi ke file dalam workspace
        log_dir = self.workspace_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_file = log_dir / f"skill_{skill.name}_{ts}.log"
        log_lines = [f"Skill: {skill.name}", f"Description: {skill.description}"]
        for r in results:
            log_lines.append(str(r))
        log_file.write_text("\n".join(log_lines))

        # Mengembalikan ringkasan eksekusi ke pemanggil
        return {"ok": True, "skill": skill.name, "results": results, "log": str(log_file)}
