"""S6 · Focus maps (A4 player, A5 team): where each player's camera was around a fight.

Camera != attention (the old `attention_rule` contract): a camera dwell says where the screen was,
not what the player saw or thought.

A dwell is a run of camera positions within DWELL_MOVE cells of the run's first position; dwells are
numbered 1, 2, 3 ... in the fight's window [start - LEAD_S, end + TAIL_S]. A dwell is NEAR the fight
when its centre is within NEAR cells of any of the fight's places. One screen's extent is unmeasured
(DL-3): NEAR is a reporting parameter.
"""
from __future__ import annotations

import math

from . import fights as F

DWELL_MOVE = 12.0
NEAR = 20.0
LEAD_S = 10.0
TAIL_S = 5.0


def dwells(cam: list, t0: float, t1: float) -> list[dict]:
    """cam: [[t, x, y]] for one player, sorted. The position at t0 is the last one before it."""
    before = [c for c in cam if c[0] < t0]
    inside = [c for c in cam if t0 <= c[0] <= t1]
    pts = ([[t0, before[-1][1], before[-1][2]]] if before else []) + inside
    out = []
    for t, x, y in pts:
        if out and math.hypot(x - out[-1]["ax"], y - out[-1]["ay"]) <= DWELL_MOVE:
            o = out[-1]
            o["n_pts"] += 1
            o["sx"] += x
            o["sy"] += y
        else:
            out.append({"start": t, "ax": x, "ay": y, "sx": x, "sy": y, "n_pts": 1})
    for i, o in enumerate(out):
        o["end"] = out[i + 1]["start"] if i + 1 < len(out) else t1
        o["x"] = round(o["sx"] / o["n_pts"], 1)
        o["y"] = round(o["sy"] / o["n_pts"], 1)
        o["n"] = i + 1
        o["seconds"] = round(o["end"] - o["start"], 2)
        for k in ("ax", "ay", "sx", "sy", "n_pts"):
            del o[k]
    return out


def focus(game: dict, fight: dict) -> dict:
    t0, t1 = fight["start"] - LEAD_S, fight["end"] + TAIL_S
    places = fight["place_centres"]
    out = {}
    for p in game["players"]:
        cam = sorted([c[0], c[2], c[3]] for c in game["camera"] if c[1] == p["pid"])
        ds = dwells(cam, t0, t1)
        for d in ds:
            d["dist"] = round(min(math.hypot(d["x"] - x, d["y"] - y) for x, y, _ in places), 1)
            d["near"] = d["dist"] <= NEAR
        total = t1 - t0
        near_s = sum(d["seconds"] for d in ds if d["near"])
        away = [d for d in ds if not d["near"]]
        longest = max(away, key=lambda d: d["seconds"]) if away else None
        at_start = next((d for d in reversed(ds) if d["start"] <= fight["start"]), None)
        out[str(p["pid"])] = {
            "dwells": ds, "window": [round(t0, 2), round(t1, 2)],
            "near_share": round(near_s / total, 3) if total > 0 and ds else None,
            "switches": max(0, len(ds) - 1),
            "longest_away": None if longest is None else
            {"n": longest["n"], "seconds": longest["seconds"], "x": longest["x"], "y": longest["y"]},
            "near_at_start": None if at_start is None else at_start["near"]}
    return out


def team_view(game: dict, fight: dict, per_player: dict) -> dict:
    out = {}
    for team in sorted({p["team"] for p in game["players"]}):
        ps = [p["pid"] for p in game["players"] if p["team"] == team]
        near = [per_player[str(pid)]["near_at_start"] for pid in ps]
        n = sum(1 for x in near if x)
        kind = ("all on the fight" if n == len(ps) else "none on the fight" if n == 0
                else "split: %d of %d on the fight" % (n, len(ps)))
        out[str(team)] = {"players": ps, "near_at_start": near, "shape": kind}
    return out


def big_fights(game: dict, top: int = 5) -> list[dict]:
    fs = sorted(F.fights(game), key=lambda f: -f["value"])[:top]
    return sorted(fs, key=lambda f: f["start"])
