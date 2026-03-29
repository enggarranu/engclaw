"""
Kanal HTTP: melakukan permintaan HTTP sederhana menggunakan pustaka standar.

Digunakan oleh skill untuk mengambil data dari internet (GET saja).
"""

from __future__ import annotations

from typing import Any
from urllib.request import urlopen


class HTTPChannel:
    """
    Implementasi kanal HTTP sederhana hanya untuk GET.

    `send` menerima payload dengan kunci `url` dan opsional `timeout`.
    Mengembalikan dict berisi `status`, `headers`, dan `body` (teks).
    """

    def send(self, payload: Any) -> Any:
        # Mengambil URL dan timeout dari payload
        url = payload.get("url")
        timeout = payload.get("timeout", 10)
        # Melakukan permintaan HTTP GET
        with urlopen(url, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            headers = dict(resp.getheaders())
            body = resp.read().decode("utf-8", errors="replace")
        # Mengembalikan detail respons untuk dipakai skill
        return {"status": status, "headers": headers, "body": body}

