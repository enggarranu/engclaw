"""
Abstraksi kanal untuk Pyclaw.

Kanal adalah antarmuka IO yang bisa dipakai oleh skill, misalnya:
- TerminalChannel: menjalankan perintah shell di sistem pengguna.
- HTTPChannel: melakukan permintaan HTTP sederhana.

Semua kanal mengikuti protokol minimal `send` dan `receive` sesuai kebutuhan.
"""

from __future__ import annotations

from typing import Protocol, Any


class Channel(Protocol):
    """
    Protokol kanal minimal.

    Method:
    - send(payload): kirim data/aksi ke kanal.
    - receive(): opsional, baca data dari kanal jika kanal mendukung.
    """

    def send(self, payload: Any) -> Any:  # pragma: no cover - antarmuka
        ...

    def receive(self) -> Any:  # pragma: no cover - antarmuka
        ...

