"""
Modul konfigurasi untuk Pyclaw.

Tanggung jawab:
- Membaca dan menulis berkas konfigurasi pengguna.
- Menyediakan lokasi default untuk workspace dan direktori skills.

Semua operasi menggunakan pustaka standar (`pathlib`, `json`) agar mudah dipakai
tanpa dependensi eksternal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


class Config:
    """
    Kelas ringan untuk memuat dan menyimpan konfigurasi pengguna.

    Struktur konfigurasi:
    - workspace_dir: direktori kerja utama untuk log, data, dan skills.
    - skills_dir: direktori tempat berkas skill berada.
    - channels: daftar nama kanal yang diaktifkan (misal: ["terminal", "http"]).
    """

    # Menentukan lokasi file konfigurasi default di direktori kerja saat ini
    # Agar mudah ditulis di lingkungan sandbox yang mungkin membatasi akses HOME
    DEFAULT_PATH = Path.cwd() / "pyclaw.config.json"

    def __init__(self, workspace_dir: Path, skills_dir: Path, channels: list[str] | None = None, integrations: Dict[str, Any] | None = None, agent: Dict[str, Any] | None = None):
        # Menyimpan path workspace dan skills sebagai atribut objek
        self.workspace_dir = workspace_dir
        self.skills_dir = skills_dir
        # Menggunakan daftar kanal default jika tidak diberikan
        self.channels = channels or ["terminal", "http"]
        # Menyimpan info integrasi eksternal (mis. token Telegram)
        self.integrations: Dict[str, Any] = integrations or {}
        # Pengaturan agent (mis. izin shell, cwd)
        self.agent: Dict[str, Any] = agent or {"allow_shell": True, "cwd": str(workspace_dir)}

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """
        Memuat konfigurasi dari berkas JSON.

        Jika berkas belum ada, method ini akan mengembalikan konfigurasi default
        dengan lokasi di `~/.pyclaw/workspace` dan `skills` di dalamnya.
        """
        # Menentukan path yang akan digunakan (argumen atau default)
        cfg_path = path or cls.DEFAULT_PATH
        if not cfg_path.exists():
            # Membuat struktur default ketika config belum ada
            default_ws = Path.cwd() / "workspace"
            default_skills = default_ws / "skills"
            return cls(workspace_dir=default_ws, skills_dir=default_skills)

        # Membaca isi file JSON dan membentuk objek Config
        data = json.loads(cfg_path.read_text())
        return cls(
            workspace_dir=Path(data["workspace_dir"]),
            skills_dir=Path(data["skills_dir"]),
            channels=list(data.get("channels") or ["terminal", "http"]),
            integrations=dict(data.get("integrations") or {}),
            agent=dict(data.get("agent") or {}),
        )

    def save(self, path: Path | None = None) -> None:
        """
        Menyimpan konfigurasi ke berkas JSON.

        Berkas akan dibuat beserta direktori induknya jika belum ada.
        """
        # Menentukan path target untuk penyimpanan
        cfg_path = path or self.DEFAULT_PATH
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        # Menulis data dalam format JSON yang mudah dibaca
        data: Dict[str, Any] = {
            "workspace_dir": str(self.workspace_dir),
            "skills_dir": str(self.skills_dir),
            "channels": self.channels,
            "integrations": self.integrations,
            "agent": self.agent,
        }
        cfg_path.write_text(json.dumps(data, indent=2))
