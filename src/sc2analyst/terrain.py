"""S5 · The map as the frame (R-P, Q5): every fight and every base placed into the map layer.

The map layer (`data/maps/<map>.features.json`) gives bases (resource clusters by mineral content,
DL-12), mineral walls, ramps (with their cliff levels hi/lo), Xel'Naga towers, destructibles. A point
on the map is described by what is there:
  - the base it is at (within BASE_R of a base's centre), and who held that base at that moment (a
    town hall finished, not yet dead, within HALL_R of the base's centre) - "main" when it is a
    player's starting base;
  - the nearest ramp (and whether the point is AT it, within RAMP_R);
  - a tower or a mineral wall nearby.
The radii are reporting parameters, not rules of the game.

Killzone CANDIDATES (P-6, his to decide): grid cells of KILLZONE_CELL where deaths fell in two or
more different fights; ranked by value lost there. Reported, never decided.

Not here: cliff level of open ground. The map layer reads ramps' levels only; the old join read
t3SyncCliffLevel (archive/reference/extractor/white_chamber_position_warfare.RUNNABLE.py), which is
not ported. A fight off a ramp has no level.
"""
from __future__ import annotations

import collections
import math

from . import fights as F, repositories as R

BASE_R = 18.0
HALL_R = 10.0
RAMP_R = 8.0
NEAR_R = 10.0
KILLZONE_CELL = 8.0


LAND = ("LandCommandCenter", "LandOrbitalCommand")


def _d(ax, ay, bx, by):
    return math.hypot(ax - bx, ay - by)


def hall_spots(game: dict, u: dict) -> list:
    """[(t, x, y, how)] - where a town hall stood from t on: where it was started, then each spot it
    LANDED. The tracker does not carry a landing spot; the owner's last `LandCommandCenter` /
    `LandOrbitalCommand` before the hall lands does (amended W4, 2026-10-28: 445 of 1,097 Command
    Centers in 150 games lift off - before this, a hall built in the main and flown to the natural
    held the main, never the natural)."""
    spots = [(u["born"], u["x"], u["y"], "started")]
    lands = sorted((c for c in game.get("commands", []) if c[1] == u["owner"] and c[2] in LAND
                    and c[3] is not None), key=lambda c: c[0])
    lifted = None
    used = set()
    types = u["types"]
    for (_t0, a), (t1, b) in zip(types, types[1:]):
        if not a.endswith("Flying") and b.endswith("Flying"):
            lifted = t1
        elif a.endswith("Flying") and not b.endswith("Flying"):
            cands = [i for i, c in enumerate(lands) if i not in used and c[0] <= t1 + 0.5
                     and (lifted is None or c[0] >= lifted)]
            if cands:
                used.add(cands[-1])
                spots.append((t1, lands[cands[-1]][3], lands[cands[-1]][4], "landed"))
    return spots


class Frame:
    def __init__(self, game: dict, names: dict | None = None):
        self.game = game
        self.names = names or {p["pid"]: "P%d" % p["pid"] for p in game["players"]}
        self.f = R.map_features(game["map"]) or {}
        self.bases = [b for b in self.f.get("resource_clusters", []) if b.get("kind") == "base"]
        self.halls = [u for u in game["units"] if "townhall" in u["kinds"] and u["owner"]]
        self.spots = {u["tag"]: hall_spots(game, u) for u in self.halls}
        self.mains = {}
        for p in game["players"]:
            sp = R.spawn(game, p["pid"])
            if sp and self.bases:
                b = min(self.bases, key=lambda b: _d(sp[0], sp[1], b["center"]["x"], b["center"]["y"]))
                self.mains[b["id"]] = p["pid"]

    def where_at(self, u: dict, t: float):
        """The hall's spot at t: the last start or landing at or before t."""
        spot = None
        for st, x, y, _how in self.spots.get(u["tag"], [(u["born"], u["x"], u["y"], "started")]):
            if st <= t:
                spot = (x, y)
        return spot

    def holder(self, base: dict, t: float):
        cx, cy = base["center"]["x"], base["center"]["y"]
        for u in self.halls:
            if (u["done"] or u["born"]) > t or (u["died"] is not None and u["died"] <= t):
                continue
            spot = self.where_at(u, t)
            if spot and _d(spot[0], spot[1], cx, cy) <= HALL_R:
                return u["owner"]
        return None

    def describe(self, x: float, y: float, t: float) -> dict:
        out = {"x": x, "y": y}
        if self.bases:
            b = min(self.bases, key=lambda b: _d(x, y, b["center"]["x"], b["center"]["y"]))
            dist = _d(x, y, b["center"]["x"], b["center"]["y"])
            if dist <= BASE_R:
                out["base"] = {"id": b["id"], "dist": round(dist, 1), "holder": self.holder(b, t),
                               "main_of": self.mains.get(b["id"]),
                               "minerals_at_start": b.get("minerals_at_start")}
        ramps = self.f.get("ramps", [])
        if ramps:
            r = min(ramps, key=lambda r: _d(x, y, r["base_center"]["x"], r["base_center"]["y"]))
            dist = _d(x, y, r["base_center"]["x"], r["base_center"]["y"])
            out["ramp"] = {"id": r["id"], "dist": round(dist, 1), "hi": r["hi"], "lo": r["lo"],
                           "at": dist <= RAMP_R}
        for tw in self.f.get("xelnaga_towers", []):
            if _d(x, y, tw["position"]["x"], tw["position"]["y"]) <= NEAR_R:
                out["tower"] = True
        for w in self.f.get("mineral_walls", []):
            if _d(x, y, w["center"]["x"], w["center"]["y"]) <= NEAR_R:
                out["mineral_wall"] = True
        out["label"] = label(out, self.names)
        return out


def label(d: dict, names: dict) -> str:
    if "base" in d:
        b = d["base"]
        if b["main_of"] is not None:
            where = "%s's main" % names[b["main_of"]]
        else:
            where = "base %d" % b["id"]
        held = "held by %s" % names[b["holder"]] if b["holder"] is not None else "empty"
        return "%s (%s)" % (where, held)
    if d.get("ramp", {}).get("at"):
        return "at ramp %d (level %d/%d)" % (d["ramp"]["id"], d["ramp"]["hi"], d["ramp"]["lo"])
    if d.get("tower"):
        return "at a Xel'Naga tower"
    if d.get("mineral_wall"):
        return "at a mineral wall"
    return "open ground"


def place_fights(game: dict, names: dict | None = None) -> list[dict]:
    fr = Frame(game, names)
    out = []
    for f in F.fights(game):
        places = [dict(fr.describe(x, y, f["start"]), deaths=n) for x, y, n in f["place_centres"]]
        out.append({"fight": f["id"], "start": f["start"], "end": f["end"], "deaths": f["deaths"],
                    "value": f["value"], "places": places,
                    "where": places[0]["label"] if places else None})
    return out


def killzones(game: dict, top: int = 5, names: dict | None = None) -> list[dict]:
    cells = collections.defaultdict(lambda: {"fights": set(), "lost": collections.Counter(), "deaths": 0})
    deaths = F.combat_deaths(game)
    runs = F.segment(deaths)
    for i, run in enumerate(runs):
        for d in run:
            k = (int(d["x"] // KILLZONE_CELL), int(d["y"] // KILLZONE_CELL))
            c = cells[k]
            c["fights"].add(i)
            c["lost"][d["pid"]] += d["value"]
            c["deaths"] += 1
    fr = Frame(game, names)
    rows = []
    for (i, j), c in cells.items():
        if len(c["fights"]) < 2:
            continue
        x, y = (i + 0.5) * KILLZONE_CELL, (j + 0.5) * KILLZONE_CELL
        rows.append({"x": x, "y": y, "fights": len(c["fights"]), "deaths": c["deaths"],
                     "lost_by_player": {str(k): v for k, v in sorted(c["lost"].items())},
                     "value": sum(c["lost"].values()), "where": fr.describe(x, y, game["end"])["label"]})
    rows.sort(key=lambda r: -r["value"])
    return rows[:top]


def base_timeline(game: dict) -> list[dict]:
    fr = Frame(game)
    out = []
    for b in fr.bases:
        holders = []
        for u in fr.halls:
            if _d(u["x"], u["y"], b["center"]["x"], b["center"]["y"]) <= HALL_R:
                holders.append({"pid": u["owner"], "from": u["done"] or u["born"], "to": u["died"]})
        out.append({"id": b["id"], "x": b["center"]["x"], "y": b["center"]["y"],
                    "main_of": fr.mains.get(b["id"]), "minerals_at_start": b.get("minerals_at_start"),
                    "held": sorted(holders, key=lambda h: h["from"])})
    return out
