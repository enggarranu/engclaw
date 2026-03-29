"""
Pemuat skill berbasis berkas JSON sederhana.

Format berkas contoh (hello.json):
{
  "name": "hello",
  "description": "Mencetak pesan ke konsol",
  "steps": [
    {"action": "print", "message": "Halo dari Pyclaw!"}
  ]
}

Setiap langkah (`steps`) dapat berupa:
- action: "print" -> mencetak pesan
- action: "http_get" -> menggunakan HTTPChannel
- action: "shell" -> menggunakan TerminalChannel
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Any, List


class Skill:
    """
    Representasi skill yang dapat dijalankan.
    """

    def __init__(self, name: str, description: str, steps: List[Dict[str, Any]]):
        # Menyimpan atribut inti skill
        self.name = name
        self.description = description
        self.steps = steps

    def __repr__(self) -> str:
        # Representasi ringkas untuk debugging
        return f"Skill(name={self.name!r}, steps={len(self.steps)})"


class SkillLoader:
    """
    Memuat skill dari direktori berkas JSON.
    """

    def __init__(self, skills_dir: Path):
        # Menyimpan lokasi direktori skills
        self.skills_dir = skills_dir

    def list_skill_files(self) -> List[Path]:
        """
        Daftar berkas *.json di direktori skills.
        """
        # Menghasilkan daftar berkas json, kosong jika direktori belum ada
        if not self.skills_dir.exists():
            return []
        return sorted(self.skills_dir.glob("*.json"))

    def load_skill(self, name: str) -> Skill | None:
        """
        Memuat satu skill berdasarkan nama berkas tanpa ekstensi.
        """
        # Menentukan path berkas skill dan memeriksa keberadaannya
        path = self.skills_dir / f"{name}.json"
        if not path.exists():
            return None
        # Membaca dan mem-parsing JSON menjadi objek Skill
        data = json.loads(path.read_text())
        return Skill(
            name=data.get("name", name),
            description=data.get("description", ""),
            steps=list(data.get("steps") or []),
        )

