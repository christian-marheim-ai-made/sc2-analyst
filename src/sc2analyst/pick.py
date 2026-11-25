"""Name a game by what a person sees - map and start time - never by its twin's file name."""
from __future__ import annotations

from pathlib import Path

from . import adapter_sc2, library as L


INDEX_VERSION = 2   # 2: the workbench's library columns (format, length, races, owner, result)


def _row(f: Path, g: dict) -> dict:
    owner = next((p for p in g["players"] if p["is_owner"]), None)
    teams = sorted({p["team"] for p in g["players"]})
    return {"file": f.name, "map": g["map"], "start_time": g["start_time"], "format": L.game_format(g),
            "end": g["end"], "build": g["build"],
            "races": {str(t): sorted(p["race"] for p in g["players"] if p["team"] == t) for t in teams},
            "owner_played": owner is not None,
            "owner_result": owner["result"] if owner is not None else None}


def index(folder: Path = L.RECORDS) -> list[dict]:
    """One row per record (file, map, start, format, length, races, the owner's result), cached in
    `_index.json` beside them while the record count is unchanged - finding one game never re-reads
    all of them."""
    import json
    files = L.record_files(folder)
    cache = folder / "_index.json"
    rows = []
    if cache.exists():
        got = json.loads(cache.read_text(encoding="utf-8"))
        if got.get("version") == INDEX_VERSION:
            if got.get("count") == len(files):
                return got["rows"]
            rows = got.get("rows", [])
    # read only the records the cache does not know (a new game added: one file, not 768)
    known = {r["file"] for r in rows}
    names = {f.name for f in files}
    rows = [r for r in rows if r["file"] in names]
    for f in files:
        if f.name in known:
            continue
        rows.append(_row(f, adapter_sc2.load(f)))
    cache.write_text(json.dumps({"version": INDEX_VERSION, "count": len(files), "rows": rows}),
                     encoding="utf-8")
    return rows


def find(map_name: str, start_prefix: str, folder: Path = L.RECORDS) -> tuple[Path, dict]:
    for r in index(folder):
        if r["map"] == map_name and r["start_time"].startswith(start_prefix):
            f = folder / r["file"]
            return f, adapter_sc2.load(f)
    raise LookupError("no record of %s at %s" % (map_name, start_prefix))


# the games the review tables and the page show (chosen 2026-10-26; any other works the same)
ONE_V_ONE = ("Old Sun Temple LE", "2026-07-16 09:14")
THREE_V_THREE = ("Sandstorm CE", "2026-09-23 16:07")


def load(which: tuple[str, str]) -> dict:
    return find(*which)[1]


def side_names(game: dict) -> dict:
    """pid -> what a page may show: the owner's handle, else Ally_2, Ally_3 ... / Enemy_1, Enemy_2 ...
    in pid order. No alias leaks.

    The numbering is the demo replays' (owner 2026-11-25, "Yes, match them"): the owner is number 1 of
    his team, so his allies start at 2; the enemies start at 1. Before 2026-11-25 it was 'Ally 1' /
    'Enemy 1' - saved reports from then still say so."""
    owner = next((p for p in game["players"] if p["is_owner"]), None)
    out, ally, enemy = {}, 1, 0
    for p in sorted(game["players"], key=lambda p: p["pid"]):
        if owner is not None and p["pid"] == owner["pid"]:
            out[p["pid"]] = p["name"]
        elif owner is not None and p["team"] == owner["team"]:
            ally += 1
            out[p["pid"]] = "Ally_%d" % ally
        else:
            enemy += 1
            out[p["pid"]] = "Enemy_%d" % enemy
    return out

