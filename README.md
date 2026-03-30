# Pyclaw

Pyclaw adalah proyek contoh asisten AI pribadi sederhana berbasis Python, terinspirasi oleh OpenClaw. Fokusnya adalah arsitektur yang mudah dipahami dengan komentar di setiap blok kode agar memudahkan belajar dan adaptasi.

## Fitur Utama
- Onboarding cepat untuk menyiapkan konfigurasi dan struktur workspace.
- Loader skill berbasis JSON yang mudah ditulis dan dibaca.
- Kanal bawaan: Terminal (eksekusi shell) dan HTTP (GET sederhana).
- Gateway ringan untuk menjalankan langkah-langkah skill dan mencatat log.
- Mode agentik + streaming: model Ollama memproduksi NDJSON berisi instruksi tool (shell/skill), dieksekusi langsung dan hasilnya di-stream ke Telegram.

## Struktur Direktori
```
pyclaw/
  cli.py            # CLI: onboard, list-skills, run
  config.py         # Konfigurasi: baca/tulis config JSON
  workspace.py      # Workspace: memastikan folder skills/logs/data
  gateway.py        # Orkestrator eksekusi skill
  channels/
    base.py         # Antarmuka/protokol kanal
    terminal.py     # Kanal perintah shell
    http.py         # Kanal permintaan HTTP GET
examples/
  hello.json        # Skill contoh: print pesan
  fetch.json        # Skill contoh: HTTP GET ke example.com
  shell.json        # Skill contoh: jalankan ls -la
```

## Instalasi & Jalankan
Default tanpa dependensi eksternal — gunakan Python bawaan.

- Onboard dan buat workspace:
```
python -m pyclaw.cli onboard --workspace ./workspace
```
- Lihat daftar skill:
```
python -m pyclaw.cli list-skills
```
- Jalankan skill:
```
python -m pyclaw.cli run hello
python -m pyclaw.cli run fetch
python -m pyclaw.cli run shell
```

Jika ingin memakai planner berbasis LangChain, instal dependensi opsional:
```
pip install -r requirements.txt
```

### Integrasi Telegram
- Simpan token di config: jalankan onboard dulu, lalu edit `pyclaw.config.json` dan tambahkan:
```
{
  "integrations": {
    "telegram_token": "123456:ABCDEF..."
  }
}
```
- Atau berikan token via argumen:
```
python -m pyclaw.cli telegram-bot --token 123456:ABCDEF...
```
- Perintah di chat Telegram:
  - `run <skill>` — contoh: `run hello`
  - `exec <command>` — contoh: `exec ls -la`
  - `ask <prompt>` — mode agentik: model merencanakan, bisa memanggil shell/skill, hasil di-stream rapi.

### Integrasi Ollama
- Pastikan Ollama berjalan lokal (`ollama serve`) dan model tersedia (mis. `ollama pull llama3`).
- Aktifkan kanal `ollama` dengan memastikan `channels` di config berisi `"ollama"` atau jalankan Telegram bot yang akan menambahkannya otomatis.
- Contoh skill meminta LLM: `examples/ask.json` dan jalankan:
```
python -m pyclaw.cli run ask
```

#### Konfigurasi model dan endpoint di `pyclaw.config.json`
Anda bisa mengatur model default dan endpoint Ollama agar fleksibel:
```
{
  "channels": ["terminal", "http", "ollama"],
  "integrations": {
    "ollama": {
      "endpoint": "http://localhost:11434/api/generate",
      "default_model": "qwen2:1.5b"
    }
  },
  "agent": {
    "allow_shell": true,
    "cwd": "./",
    "stream": false,
    "planner": "langchain",
    "temperature": 0.2
  }
}
```
- `agent.allow_shell`: izinkan planner mengeksekusi perintah shell dari model.
- `agent.cwd`: direktori kerja default untuk perintah shell.
- `agent.stream`: aktifkan streaming untuk planner NDJSON (non-stream disarankan untuk debugging).
- `agent.planner`: pilih `ndjson` (internal) atau `langchain` (opsional, butuh dependensi).
- `agent.temperature`: atur kreativitas model (0.0 deterministik, lebih tinggi lebih kreatif).

### Mode Agentik & Streaming
- Planner: `pyclaw/agent/planner.py` menunggu NDJSON dari model: `{ "say": ... }` atau `{ "tool": "shell", "command": ... }` atau `{ "tool": "skill", "name": ... }`.
- Kanal Ollama streaming: `pyclaw/channels/ollama.py` menyediakan `send_stream` untuk membaca NDJSON baris-demi-baris.
- Telegram bridge akan mengirim hasil secara bertahap dalam blok `<pre>` agar rapi dan mudah dibaca.
- Skill tanpa `model` akan memakai `default_model` di atas (contoh: `examples/ask_default.json`).
- Di Telegram, Anda bisa override per pesan: `ask llama3: tulis haiku` atau cukup `ask ...` untuk pakai default.

### Planner LangChain (opsional)
- Aktifkan dengan menambah konfigurasi berikut di `pyclaw.config.json`:
```
{
  "agent": {
    "planner": "langchain",
    "allow_shell": true,
    "cwd": "./",
    "stream": false
  }
}
```
- Instal paket: `pip install -r requirements.txt`.
- Tools yang tersedia di LangChain planner: `shell`, `skill`, `file_write`, `file_append`, `file_read`, `file_list`.

## Catatan
- Semua kode diberi komentar untuk menjelaskan fungsi tiap blok.
- Format skill menggunakan JSON sederhana agar mudah diadopsi.
- Ini adalah baseline yang bisa dikembangkan: tambah kanal baru, tambah jenis aksi baru, atau integrasi ke sistem lain.
