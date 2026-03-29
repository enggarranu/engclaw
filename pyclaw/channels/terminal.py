"""
Kanal terminal: mengeksekusi perintah shell melalui `subprocess`.

Digunakan oleh skill untuk menjalankan tugas sistem seperti `ls`, `echo`, dsb.
"""

from __future__ import annotations

import subprocess
from typing import Any


class TerminalChannel:
    """
    Implementasi kanal terminal sederhana.

    `send` menerima payload berupa objek dengan kunci `command` (string)
    dan opsional `cwd`. Mengembalikan tuple `(returncode, stdout, stderr)`.
    """

    def send(self, payload: Any) -> Any:
        # Mengambil perintah dan direktori kerja dari payload
        command = payload.get("command")
        cwd = payload.get("cwd")
        # Menjalankan perintah secara terpisah dengan menangkap output
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        # Mengembalikan hasil eksekusi untuk digunakan skill
        return proc.returncode, proc.stdout, proc.stderr

