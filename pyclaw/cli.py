"""
CLI Pyclaw: antarmuka baris perintah untuk onboarding dan eksekusi skill.

Perintah yang didukung:
- onboard: menyiapkan konfigurasi dan workspace awal.
- list-skills: menampilkan daftar skill yang tersedia.
- run <nama>: menjalankan skill berdasarkan nama berkas JSON.

Contoh penggunaan:
  python -m pyclaw.cli onboard --workspace ./workspace
  python -m pyclaw.cli list-skills
  python -m pyclaw.cli run hello
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import List

from .config import Config
from .gateway import Gateway
from .workspace import Workspace
from .telegram_bot import run_bot_via_cli


def cmd_onboard(args: argparse.Namespace) -> None:
    """
    Menangani perintah `onboard` untuk menyiapkan konfigurasi pengguna.
    """
    # Menentukan lokasi workspace dari argumen atau default
    workspace_root = Path(args.workspace) if args.workspace else (Path.home() / ".pyclaw" / "workspace")
    ws = Workspace(workspace_root)
    ws.ensure()

    # Membuat objek konfigurasi dan menyimpannya ke file default
    cfg = Config(workspace_dir=ws.root, skills_dir=ws.skills_dir)
    cfg.save()

    # Menyalin skill contoh ke direktori skills
    examples = [
        Path(__file__).parent / ".." / "examples" / "hello.json",
        Path(__file__).parent / ".." / "examples" / "fetch.json",
        Path(__file__).parent / ".." / "examples" / "shell.json",
        Path(__file__).parent / ".." / "examples" / "ask.json",
        Path(__file__).parent / ".." / "examples" / "ask_default.json",
    ]
    # Memfilter hanya berkas yang ada agar aman di lingkungan berbeda
    examples = [p.resolve() for p in examples if p.resolve().exists()]
    ws.copy_examples(examples)

    # Memberi umpan balik sukses kepada pengguna
    print(f"Onboard selesai. Workspace: {ws.root}")
    print(f"Skill contoh tersalin ke: {ws.skills_dir}")


def cmd_list_skills(args: argparse.Namespace) -> None:
    """
    Menangani perintah `list-skills` untuk menampilkan daftar skill.
    """
    # Memuat konfigurasi pengguna untuk menemukan direktori skills
    cfg = Config.load()
    gw = Gateway(cfg.workspace_dir, cfg.skills_dir, cfg.channels, cfg.integrations)
    # Mengiterasi berkas skill dan mencetak nama tanpa ekstensi
    files: List[Path] = gw.loader.list_skill_files()
    if not files:
        print("Tidak ada skill ditemukan.")
        return
    for f in files:
        print(f.stem)


def cmd_run(args: argparse.Namespace) -> None:
    """
    Menangani perintah `run` untuk mengeksekusi sebuah skill.
    """
    # Memuat konfigurasi pengguna untuk menjalankan skill
    cfg = Config.load()
    gw = Gateway(cfg.workspace_dir, cfg.skills_dir, cfg.channels, cfg.integrations)
    result = gw.run_skill(args.name)
    # Mencetak ringkasan hasil eksekusi ke konsol
    if result.get("ok"):
        print(f"Skill '{result['skill']}' selesai. Log: {result['log']}")
    else:
        print(f"Gagal: {result.get('error')}")


def build_parser() -> argparse.ArgumentParser:
    """
    Membuat parser argumen CLI.
    """
    # Membuat parser utama dengan deskripsi ringkas
    p = argparse.ArgumentParser(prog="pyclaw", description="CLI Pyclaw")
    sub = p.add_subparsers(dest="cmd", required=True)

    # Subperintah `onboard` dengan argumen opsional workspace
    sp = sub.add_parser("onboard", help="Siapkan konfigurasi & workspace")
    sp.add_argument("--workspace", help="Path workspace (default ~/.pyclaw/workspace)")
    sp.set_defaults(fn=cmd_onboard)

    # Subperintah `list-skills` untuk melihat daftar skill
    sp = sub.add_parser("list-skills", help="Daftar skill yang tersedia")
    sp.set_defaults(fn=cmd_list_skills)

    # Subperintah `run` untuk menjalankan sebuah skill
    sp = sub.add_parser("run", help="Jalankan skill berdasarkan nama")
    sp.add_argument("name", help="Nama skill (nama berkas JSON tanpa .json)")
    sp.set_defaults(fn=cmd_run)

    # Subperintah `telegram-bot` untuk menjalankan bridge Telegram
    sp = sub.add_parser("telegram-bot", help="Jalankan bridge Telegram ke Pyclaw")
    sp.add_argument("--token", help="Token Bot Telegram (fallback ke config.integrations.telegram_token)")
    sp.add_argument("--verbose", action="store_true", help="Tampilkan log verbose saat berjalan")
    def _fn(args):
        # Menjalankan bot; exit code tidak dipakai di CLI ini
        run_bot_via_cli(args.token, verbose=bool(getattr(args, "verbose", False)))
    sp.set_defaults(fn=_fn)

    return p


def main() -> None:
    """
    Titik masuk utama CLI.
    """
    # Membangun parser dan membaca argumen dari baris perintah
    parser = build_parser()
    args = parser.parse_args()
    # Menjalankan fungsi handler sesuai subperintah yang dipilih
    fn = getattr(args, "fn", None)
    if fn is None:
        parser.print_help()
        return
    fn(args)


if __name__ == "__main__":
    # Memanggil main ketika modul dijalankan sebagai skrip
    main()
