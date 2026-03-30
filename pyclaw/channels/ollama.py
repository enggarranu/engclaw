"""
Kanal Ollama: berkomunikasi dengan model LLM lokal via Ollama API.

Endpoint default: http://localhost:11434/api/generate

Payload `send` minimal:
  {"model": "llama3", "prompt": "Tuliskan haiku."}

Mengembalikan dict {"ok": bool, "response": str, "error": str|None}
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


class OllamaChannel:
    """
    Implementasi kanal sederhana untuk Ollama.
    """

    def __init__(self, endpoint: str = "http://localhost:11434/api/generate"):
        # Menyimpan endpoint API Ollama
        self.endpoint = endpoint

    def send(self, payload: Any) -> Dict[str, Any]:
        # Mengambil model dan prompt dari payload
        model = payload.get("model")
        prompt = payload.get("prompt")
        if not model or not prompt:
            return {"ok": False, "error": "model/prompt wajib", "response": ""}

        try:
            # Menyiapkan permintaan HTTP POST ke Ollama
            body_obj: Dict[str, Any] = {"model": model, "prompt": prompt}
            # Teruskan opsi model seperti temperature jika tersedia
            if payload.get("options"):
                body_obj["options"] = payload.get("options")
            # Dukungan visi: daftar base64 image strings
            images: List[str] | None = payload.get("images")
            if images:
                body_obj["images"] = images
            body = json.dumps(body_obj).encode("utf-8")
            req = Request(self.endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            # Respon Ollama berupa NDJSON; gabungkan field "response"
            text_chunks = []
            for line in raw.splitlines():
                try:
                    obj = json.loads(line)
                    chunk = obj.get("response")
                    if chunk:
                        text_chunks.append(chunk)
                except json.JSONDecodeError:
                    continue
            return {"ok": True, "response": "".join(text_chunks), "error": None}
        except (URLError, HTTPError) as e:
            # Menangani koneksi gagal atau server tidak tersedia
            return {"ok": False, "response": "", "error": str(e)}

    def send_stream(self, payload: Any) -> Iterator[Dict[str, Any]]:
        # Versi streaming: mengembalikan iterator objek NDJSON dari Ollama
        model = payload.get("model")
        prompt = payload.get("prompt")
        if not model or not prompt:
            yield {"response": "", "error": "model/prompt wajib"}
            return
        body_obj: Dict[str, Any] = {"model": model, "prompt": prompt}
        if payload.get("options"):
            body_obj["options"] = payload.get("options")
        images: List[str] | None = payload.get("images")
        if images:
            body_obj["images"] = images
        body = json.dumps(body_obj).encode("utf-8")
        req = Request(self.endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=30) as resp:
                # Membaca baris demi baris (NDJSON)
                for raw_line in iter(resp.readline, b""):
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        obj = {"response": line}
                    yield obj
        except (URLError, HTTPError) as e:
            yield {"response": "", "error": str(e)}
