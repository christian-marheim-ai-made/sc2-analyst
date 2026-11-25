"""The library's game records on the data home, and the small statistics the review tables use.

Review tables carry aggregates and map names only - never a twin's file name (some name a
handle) and never a player's name.
"""
from __future__ import annotations

import collections
from pathlib import Path

from . import adapter_sc2, settings

DATA_HOME = settings.data_home()   # config/analyst*.toml or SC2A_DATA_HOME
RECORDS = DATA_HOME / "derived" / adapter_sc2.ADAPTER_VERSION


def record_files(folder: Path = RECORDS) -> list[Path]:
    return sorted(folder.glob("*.json.gz"))


def iter_records(folder: Path = RECORDS):
    for f in record_files(folder):
        yield f, adapter_sc2.load(f)


def game_format(game: dict) -> str:
    sizes = collections.Counter(p["team"] for p in game["players"])
    return "v".join(str(sizes[t]) for t in sorted(sizes))


def owner_pid(game: dict) -> int | None:
    for p in game["players"]:
        if p["is_owner"]:
            return p["pid"]
    return None


def quantiles(values, qs=(0.1, 0.5, 0.9)) -> list:
    v = sorted(values)
    if not v:
        return [None for _ in qs]
    return [v[min(len(v) - 1, int(round(q * (len(v) - 1))))] for q in qs]


def md_table(header: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for r in rows:
        out.append("| " + " | ".join("" if c is None else str(c) for c in r) + " |")
    return "\n".join(out)


def clock(t: float | None) -> str:
    if t is None:
        return "-"
    return "%d:%02d" % (int(t) // 60, int(t) % 60)
