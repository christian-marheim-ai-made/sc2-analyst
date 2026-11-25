"""W6b · The aggregation seam: one question over a chosen set of games (ratified 2026-10-29).

The owner's example: *"an analysis of my seige tank usage and decision for the last 30 games"*. The
seam has three parts, each replaceable alone:

  select     which games and whose play: the owner's last N games by start time, narrowed by map,
             format, his race, the enemy's races, his opening (W4) - `select(rows, ...)`
  repository one per-game measurement, registered by name: `REPOSITORIES[name] = Repository(...)`;
             `per_game(game, pid) -> dict` of numbers, `total(rows) -> dict` adds them up (shares
             are computed from the sums, never averaged from per-game shares)
  runner     `run(name, picked)` - one pass, Pool(4) at below-normal priority, one row per game and
             the summed table, written versioned under <data home>/aggregates/
             <repository>/<selection>/<version>/table.json, with `reviewed.json` beside it once the
             owner has reviewed it (R-M: the model reads an aggregate only then)

R-0 holds: the code counts and sums; the model, later, assesses the summed table and never adds.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from multiprocessing import Pool
from pathlib import Path
from typing import Callable

from . import adapter_sc2 as A, library as L

AGGREGATES = L.DATA_HOME / "aggregates"


@dataclass
class Repository:
    name: str
    version: str
    per_game: Callable          # (game, pid) -> dict
    total: Callable             # (rows) -> dict
    about: str                  # what it measures and what it does not prove


REPOSITORIES: dict = {}
KNOWN = ("siege",)          # the repository modules; each registers itself when imported


def load_repositories() -> dict:
    """Import every known repository. Called by the runner in each worker: a worker process started
    on Windows imports only what its parent's main module imported (measured W7: the workbench's
    workers had no `siege`)."""
    import importlib
    for name in KNOWN:
        importlib.import_module("%s.%s" % (__package__, name))
    return REPOSITORIES


def register(repo: Repository) -> Repository:
    REPOSITORIES[repo.name] = repo
    return repo


def select(rows: list, map_name=None, fmt=None, race=None, vs=None,
           opening=None, openings: dict | None = None) -> list:
    """The owner's games, newest first, narrowed (`run` cuts to the last N). `rows` =
    pick.index(); `openings` = the W4 classes by record file ({file: {pid: {class}}}); `race`, `vs`
    and `opening` need the owner's pid, which the index does not hold, so they are applied by
    `run` once each game is loaded (marked here as wanted)."""
    out = [r for r in rows if r.get("owner_played")]
    if map_name:
        out = [r for r in out if r["map"] == map_name]
    if fmt:
        out = [r for r in out if r.get("format") == fmt]
    out.sort(key=lambda r: r["start_time"], reverse=True)
    picked = []
    for r in out:
        picked.append(dict(r, want={"race": race, "vs": vs, "opening": opening},
                           openings=(openings or {}).get(r["file"])))
    return picked


def slug(**sel) -> str:
    parts = ["%s-%s" % (k, re.sub(r"[^A-Za-z0-9]+", "", str(v))) for k, v in sorted(sel.items()) if v]
    return "_".join(parts) or "all"


def _owner_matches(game: dict, want: dict, openings: dict | None):
    me = next((p for p in game["players"] if p["is_owner"]), None)
    if me is None:
        return None
    if want.get("race") and me["race"] != want["race"]:
        return None
    if want.get("vs"):
        enemy = {p["race"] for p in game["players"] if p["team"] != me["team"]}
        if want["vs"] not in enemy:
            return None
    if want.get("opening"):
        cls = (openings or {}).get(str(me["pid"]), {}).get("class")
        if cls != want["opening"]:
            return None
    return me


def _one(args):
    name, path, want, openings = args
    try:
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
    except Exception:
        pass
    load_repositories()
    g = A.load(Path(path))
    me = _owner_matches(g, want, openings)
    if me is None:
        return None
    row = REPOSITORIES[name].per_game(g, me["pid"])
    return dict(row, game=g["map"] + " " + g["start_time"][:16], result=me["result"], race=me["race"])


def run(name: str, picked: list, last: int | None = None, records: Path = L.RECORDS, jobs: int = 4) -> dict:
    """One row per game (newest first, at most `last` once the race / vs / opening filters held) and
    the summed table."""
    repo = load_repositories()[name]
    args = [(name, str(records / r["file"]), r["want"], r["openings"]) for r in picked]
    rows = []
    with Pool(jobs) as pool:
        for row in pool.imap(_one, args, chunksize=4):   # in order: the newest games first
            if row is not None:
                rows.append(row)
                if last is not None and len(rows) >= last:
                    pool.terminate()
                    break
    return {"repository": name, "version": repo.version, "games": len(rows), "rows": rows,
            "total": repo.total(rows), "about": repo.about}


def write(result: dict, selection: str) -> Path:
    d = AGGREGATES / result["repository"] / selection / result["version"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "table.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    return d


def reviewed(result_dir: Path) -> bool:
    return (result_dir / "reviewed.json").exists()
