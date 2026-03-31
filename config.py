from __future__ import annotations

from pathlib import Path

from pyclaw.config import Config as _BaseConfig


class Config(_BaseConfig):
    # Use engclaw-specific default config filename
    DEFAULT_PATH = Path.cwd() / "engclaw.config.json"

