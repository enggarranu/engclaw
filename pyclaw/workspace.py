"""
Modul workspace untuk Pyclaw.

Tanggung jawab:
- Menyediakan utilitas untuk memastikan direktori workspace siap dipakai.
- Mengatur lokasi log dan data sederhana.
- Menyalin contoh skills saat onboarding.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable


class Workspace:
    """
    Abstraksi direktori kerja Pyclaw.

    Workspace berisi struktur:
    - skills/: berkas definisi skill (*.json)
    - logs/: berkas log teks
    - data/: data sederhana yang dihasilkan skill
    """

    def __init__(self, root: Path):
        # Menyimpan akar workspace sebagai atribut
        self.root = root
        # Menentukan subdirektori standar di dalam workspace
        self.skills_dir = root / "skills"
        self.logs_dir = root / "logs"
        self.data_dir = root / "data"

    def ensure(self) -> None:
        """
        Membuat struktur direktori workspace jika belum ada.
        """
        # Membuat seluruh direktori secara rekursif dan idempotent
        for d in (self.root, self.skills_dir, self.logs_dir, self.data_dir):
            d.mkdir(parents=True, exist_ok=True)

    def copy_examples(self, examples: Iterable[Path]) -> None:
        """
        Menyalin berkas contoh skill ke direktori `skills/`.
        """
        # Memastikan direktori skills tersedia sebelum menyalin
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        for src in examples:
            dst = self.skills_dir / src.name
            # Hanya menyalin jika berkas tujuan belum ada agar tidak menimpa
            if not dst.exists():
                dst.write_text(src.read_text())

