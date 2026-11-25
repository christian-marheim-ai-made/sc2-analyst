"""Where one game sits against the owner's library: per-player-game metrics and their distribution.

Each metric is computed the same way for the game on the page and for every game in the library, so
"this game" and "your typical" are comparable by construction. Grouped by the player's race and the
game's format; a group with fewer than MIN_GROUP games falls back to the race alone.
"""
from __future__ import annotations

import bisect
import collections
import json
from pathlib import Path

from . import build as B, library as L, repositories as R

REPO = Path(__file__).resolve().parents[2]
FILE = REPO / "data/measured/baselines.json"
MIN_GROUP = 20

METRICS = {
    "idle_townhall_share": ("town halls' idle share of life", "lower"),
    "idle_production_share": ("production structures' idle share of life", "lower"),
    "supply_blocked_s": ("seconds supply blocked (lower bound)", "lower"),
    "workers_at_6": ("workers at 6:00", "higher"),
    "t_44_workers": ("time to 44 workers (s)", "lower"),
    "killed_lost_ratio": ("value killed / value lost", "higher"),
    "lost_share_of_income": ("value lost / income", "lower"),
    "larva_banked_share": ("hatcheries' share of life with 3+ larva", "lower"),
}


def metrics(game: dict, pid: int, bt: dict) -> dict:
    out = {}
    idle = [r for r in B.idle_production(game, pid, bt) if r["to"] - r["from"] >= 60]
    for cls, key in (("townhall", "idle_townhall_share"), ("production", "idle_production_share")):
        xs = [r for r in idle if r["class"] == cls]
        life = sum(r["to"] - r["from"] for r in xs)
        out[key] = round(sum(r["idle_s"] for r in xs) / life, 3) if life else None
    out["supply_blocked_s"] = round(sum(b["at_least_s"] for b in B.supply_blocks(game, pid)), 1)
    ex = R.expansions(game)[str(pid)]
    wm = dict(ex["workers_by_minute"])
    out["workers_at_6"] = wm.get(6) if game["end"] >= 6 * 60 + 30 else None
    out["t_44_workers"] = ex["workers_first_reached"]["44"]
    tr = R.trade(game)[str(pid)]
    out["killed_lost_ratio"] = tr["ratio"]
    out["lost_share_of_income"] = tr["lost_share_of_income"]
    lv = [r for r in B.larva_bank(game, pid) if r["life_s"] >= 60]
    life = sum(r["life_s"] for r in lv)
    out["larva_banked_share"] = round(sum(r["banked_s"] for r in lv) / life, 3) if life else None
    return out


def build(games, bt: dict) -> dict:
    groups = collections.defaultdict(lambda: collections.defaultdict(list))
    for g in games:
        if g["end"] < 180:
            continue
        for p in g["players"]:
            if not p["is_owner"]:
                continue
            m = metrics(g, p["pid"], bt)
            for key in (p["race"] + "|" + L.game_format(g), p["race"]):
                for k, v in m.items():
                    if v is not None:
                        groups[key][k].append(v)
    return {key: {k: sorted(v) for k, v in ms.items()} for key, ms in groups.items()}


def load() -> dict:
    return json.loads(FILE.read_text(encoding="utf-8"))


def compare(game: dict, pid: int, bt: dict, base: dict) -> list[dict]:
    race = next(p["race"] for p in game["players"] if p["pid"] == pid)
    key = race + "|" + L.game_format(game)
    m = metrics(game, pid, bt)
    rows = []
    for k, (label, better) in METRICS.items():
        v = m.get(k)
        if v is None:
            continue
        dist = base["groups"].get(key, {}).get(k, [])
        group = key
        if len(dist) < MIN_GROUP:
            dist = base["groups"].get(race, {}).get(k, [])
            group = race
        if len(dist) < MIN_GROUP:
            continue
        pct = round(100 * bisect.bisect_left(dist, v) / len(dist))
        rows.append({"metric": k, "label": label, "better": better, "value": v,
                     "p50": dist[len(dist) // 2], "p10": dist[int(0.1 * (len(dist) - 1))],
                     "p90": dist[int(0.9 * (len(dist) - 1))], "percentile": pct,
                     "group": group, "n": len(dist)})
    return rows
