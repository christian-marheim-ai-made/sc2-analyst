"""W6b · Siege tanks, the aggregation seam's first repository - per game, one player's tanks.

The measures below are PROPOSED (ruled by the owner; built as the default until he does). Each is
counted from the record; none is a judgement:

  tanks              tanks built (units that started as SiegeTank)
  siege_ups          times a tank went into siege mode (a type change to SiegeTankSieged)
  tank_seconds       seconds tanks were alive; sieged_seconds, of those, in siege mode
  fights_with_tanks  fights in which at least one of the player's tanks was alive at the start
  sieged_at_start    of the tank-fights: tanks already sieged when the fight's first death came
  sieged_in_fight    tanks that sieged during a fight, not before it (late)
  never_sieged       tanks alive at a fight's start that were not sieged at any moment of it
  lost_unsieged      tanks the player lost in fights while in tank mode; lost_sieged in siege mode
  killed_sieged      priced value its sieged tanks killed; killed_unsieged in tank mode
  siege_spots        each siege-up placed where the tank was last seen, when that position is at
                     most 15 s old: at a ramp / at a base / open ground; older ones are "unknown"

What it does NOT prove: that a siege was well timed or well placed. A tank sieged at a fight's start
may have sieged at the wrong spot; a tank lost unsieged may have been moving on purpose. Positions
come every 10.7 s at best (S2); where a tank sieged is the place it was last seen, aged. Who ordered
the siege (the selection) is not in the record (gap F5).
"""
from __future__ import annotations

import collections

from . import aggregate as AG, fights as F, positions as P, terrain as TR

SIEGED = "SiegeTankSieged"
TANK = "SiegeTank"
FRESH_S = 15.0
COUNTS = ("tanks", "siege_ups", "fights_with_tanks", "tank_fight_presences", "sieged_at_start",
          "sieged_in_fight", "never_sieged", "lost_sieged", "lost_unsieged", "killed_sieged",
          "killed_unsieged", "spot_ramp", "spot_base", "spot_open", "spot_unknown")


def intervals(u: dict) -> list:
    """[(from, to)] the tank was in siege mode (to = its death or None while alive at the end)."""
    out, start = [], None
    for t, name in u["types"]:
        if name == SIEGED and start is None:
            start = t
        elif name != SIEGED and start is not None:
            out.append((start, t))
            start = None
    if start is not None:
        out.append((start, u["died"]))
    return out


def _sieged_at(iv: list, t: float) -> bool:
    return any(a <= t and (b is None or t < b) for a, b in iv)


def per_game(game: dict, pid: int) -> dict:
    tanks = [u for u in game["units"] if u["owner"] == pid and u["types"][0][1] == TANK]
    out = {k: 0 for k in COUNTS}
    out["tank_seconds"] = out["sieged_seconds"] = 0.0
    out["tanks"] = len(tanks)
    if not tanks:
        return out
    end = game["end"]
    iv = {u["tag"]: intervals(u) for u in tanks}
    for u in tanks:
        life_end = u["died"] if u["died"] is not None else end
        out["tank_seconds"] += max(0.0, life_end - u["done"])
        out["sieged_seconds"] += sum(((b if b is not None else life_end) - a) for a, b in iv[u["tag"]])
        out["siege_ups"] += len(iv[u["tag"]])
    fs = F.fights(game)
    for f in fs:
        alive = [u for u in tanks if u["done"] <= f["start"] and (u["died"] is None or u["died"] > f["start"])]
        if not alive:
            continue
        out["fights_with_tanks"] += 1
        for u in alive:
            out["tank_fight_presences"] += 1
            if _sieged_at(iv[u["tag"]], f["start"]):
                out["sieged_at_start"] += 1
            elif any(f["start"] < a <= f["end"] for a, _ in iv[u["tag"]]):
                out["sieged_in_fight"] += 1
            else:
                out["never_sieged"] += 1
    for d in F.combat_deaths(game):
        if d["pid"] == pid and d["type"] in (TANK, SIEGED):
            if any(f["start"] <= d["t"] <= f["end"] for f in fs):
                out["lost_sieged" if d["type"] == SIEGED else "lost_unsieged"] += 1
        if d["kpid"] == pid and d["vk"] and d["ktype"] in (TANK, SIEGED):
            out["killed_sieged" if d["ktype"] == SIEGED else "killed_unsieged"] += d["value"]
    try:
        frame = TR.Frame(game)
        pos = P.Positions(game)
    except Exception:
        frame = pos = None
    for tag, spans in iv.items():
        for a, _ in spans:
            spot = "spot_unknown"
            if frame is not None and frame.bases:
                p = pos.at(tag, a)
                if p["x"] is not None and p["age"] is not None and p["age"] <= FRESH_S:
                    w = frame.describe(p["x"], p["y"], a)
                    spot = ("spot_ramp" if w.get("ramp", {}).get("at") else
                            "spot_base" if w.get("base") else "spot_open")
            out[spot] += 1
    return out


def total(rows: list) -> dict:
    t = collections.Counter()
    for r in rows:
        for k in COUNTS + ("tank_seconds", "sieged_seconds"):
            t[k] += r.get(k, 0)
    out = dict(t)
    out["tank_seconds"] = round(t["tank_seconds"])
    out["sieged_seconds"] = round(t["sieged_seconds"])
    out["games_with_tanks"] = sum(1 for r in rows if r.get("tanks"))
    pres = t["tank_fight_presences"]
    out["share_sieged_at_start"] = round(100 * t["sieged_at_start"] / pres, 1) if pres else None
    out["share_sieged_late"] = round(100 * t["sieged_in_fight"] / pres, 1) if pres else None
    out["share_never_sieged"] = round(100 * t["never_sieged"] / pres, 1) if pres else None
    lost = t["lost_sieged"] + t["lost_unsieged"]
    out["share_lost_unsieged"] = round(100 * t["lost_unsieged"] / lost, 1) if lost else None
    out["share_time_sieged"] = round(100 * t["sieged_seconds"] / t["tank_seconds"], 1) if t["tank_seconds"] else None
    known = t["spot_ramp"] + t["spot_base"] + t["spot_open"]
    out["share_spots_known"] = round(100 * known / t["siege_ups"], 1) if t["siege_ups"] else None
    return out


AG.register(AG.Repository("siege", "v1", per_game, total, __doc__.split("\n\n")[1]))
