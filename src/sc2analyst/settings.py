"""The machine's own settings: where the data home is. One home for it, never a path in the code.

`config/analyst.local.toml` (gitignored, your machine) wins over `config/analyst.toml` (tracked, the
public default); the environment variable SC2A_DATA_HOME wins over both. A relative path is taken
from the repository root.

    # config/analyst.toml
    data_home = "data-home"
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONFIG = REPO / "config"


def _file() -> dict:
    for name in ("analyst.local.toml", "analyst.toml"):
        p = CONFIG / name
        if p.exists():
            return tomllib.loads(p.read_text(encoding="utf-8"))
    return {}


def data_home() -> Path:
    raw = os.environ.get("SC2A_DATA_HOME") or _file().get("data_home") or "data-home"
    p = Path(raw)
    return p if p.is_absolute() else REPO / p
