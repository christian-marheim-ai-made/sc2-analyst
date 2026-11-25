"""S4 · The all-HAVE repositories, each consumer-shaped, each with its evidence rule and its mirror.

  B4 trade efficiency     value lost vs killed, per player and per minute, against income
  B6 expansion timing     each town hall's start and finish; workers over time
  B7 reinforcement flow   what each player completed during each fight
  B8 death causes         what killed my stuff: (my unit type, killer type) by count and value
  B9 wall-offs            structures started at the player's main ramp, and when

The mirror (DL-5): every quantity with an opposing counterpart is computed from BOTH sides by
separate loops - the victim's ledger and the killer's ledger - and `mirror_check` asserts they agree
exactly. A disagreement is a defect in this code, never in the game.

Evidence rule common to all: values are sc2reader's costs per build (`vk` false = no cost in its data,
counted apart, never zero - DL-20); times are the in-game clock (DL-18); deaths are combat deaths
(DL-1).
"""
from __future__ import annotations

import collections
import json
import math
from pathlib import Path

from . import fights as F

REPO = Path(__file__).resolve().parents[2]
WALL_RADIUS = 6.0      # cells from the ramp's centre; a reporting parameter
WALL_TYPES = {"SupplyDepot", "SupplyDepotLowered", "Barracks", "Factory", "Bunker", "EngineeringBay",
              "Pylon", "Gateway", "WarpGate", "CyberneticsCore", "PhotonCannon", "ShieldBattery",
              "Forge", "SpineCrawler", "SporeCrawler", "EvolutionChamber", "SpawningPool",
              "RoachWarren", "MissileTurret"}


def _team(game):
    return {p["pid"]: p["team"] for p in game["players"]}


# --- B4 -----------------------------------------------------------------------------------------

def trade(game: dict) -> dict:
    team = _team(game)
    deaths = F.combat_deaths(game)
    lost = collections.defaultdict(lambda: collections.Counter())    # victim -> killer -> value
    for d in deaths:                                                   # the victim's ledger
        lost[d["pid"]][d["kpid"]] += d["value"]
    killed = collections.defaultdict(lambda: collections.Counter())  # killer -> victim -> value
    for u in game["units"]:                                            # the killer's ledger
        if u["died"] is None or u["kpid"] not in team or u["owner"] not in team:
            continue
        if team[u["kpid"]] == team[u["owner"]]:
            continue
        killed[u["kpid"]][u["owner"]] += u["m"] + u["g"]
    unknown = collections.Counter(d["pid"] for d in deaths if not d["vk"])
    income = _income(game)
    per_min = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0]))
    for d in deaths:
        m = int(d["t"] // 60)
        per_min[d["pid"]][m][0] += d["value"]
        per_min[d["kpid"]][m][1] += d["value"]
    out = {}
    for pid in team:
        L, K = sum(lost[pid].values()), sum(killed[pid].values())
        out[str(pid)] = {
            "lost": L, "killed": K, "ratio": round(K / L, 2) if L else None,
            "income": income.get(pid), "lost_share_of_income": round(L / income[pid], 3)
            if income.get(pid) else None,
            "deaths_value_unknown": unknown[pid],
            "lost_to": {str(k): v for k, v in sorted(lost[pid].items())},
            "killed_of": {str(k): v for k, v in sorted(killed[pid].items())},
            "per_minute": [[m, v[0], v[1]] for m, v in sorted(per_min[pid].items())],
        }
    return out


def _income(game):
    """Resources collected: the stats' collection rate (per minute) integrated over its samples."""
    out = collections.Counter()
    last = {}
    for s in sorted(game["stats"], key=lambda s: (s["pid"], s["t"])):
        p = s["pid"]
        if p in last:
            dt = s["t"] - last[p]["t"]
            out[p] += (last[p]["m_rate"] + last[p]["g_rate"]) * dt / 60
        last[p] = s
    return {k: int(v) for k, v in out.items()}


def mirror_check(game: dict) -> list[str]:
    """Empty when every lost/killed pair agrees exactly from both ledgers."""
    t = trade(game)
    bad = []
    for a, ra in t.items():
        for b, v in ra["lost_to"].items():
            other = t.get(b, {}).get("killed_of", {}).get(a, 0)
            if other != v:
                bad.append("P%s lost %d to P%s, P%s killed %d of P%s" % (a, v, b, b, other, a))
    for f in F.fights(game):
        if sum(f["lost_value_by_team"].values()) != sum(p["killed_value"] for p in f["by_player"].values()):
            bad.append("fight %d lost != killed" % f["id"])
    return bad


# --- B6 -----------------------------------------------------------------------------------------

def expansions(game: dict) -> dict:
    out = {}
    stats = collections.defaultdict(list)
    for s in game["stats"]:
        stats[s["pid"]].append(s)
    for p in game["players"]:
        pid = p["pid"]
        halls = sorted((u for u in game["units"] if u["owner"] == pid and "townhall" in u["kinds"]),
                       key=lambda u: u["born"])
        rows = [{"n": i + 1, "type": u["types"][-1][1], "started": u["born"], "done": u["done"],
                 "died": u["died"], "x": u["x"], "y": u["y"]} for i, u in enumerate(halls)]
        ws = sorted(stats[pid], key=lambda s: s["t"])
        first = {}
        for n in (22, 44, 66):
            first[str(n)] = next((s["t"] for s in ws if s["workers"] >= n), None)
        out[str(pid)] = {"town_halls": rows, "workers_first_reached": first,
                         "workers_max": max((s["workers"] for s in ws), default=None),
                         # the last sample in each minute
                         "workers_by_minute": sorted({int(s["t"] // 60): s["workers"]
                                                      for s in ws}.items())}
    return out


# --- B7 -----------------------------------------------------------------------------------------

def reinforcement(game: dict, min_deaths: int = 6) -> list[dict]:
    """Units each player COMPLETED while a fight ran (S1 fights of `min_deaths` or more)."""
    team = _team(game)
    made = [u for u in game["units"] if u["owner"] in team and "army" in u["kinds"] and u["born"] > 0]
    out = []
    for f in F.fights(game):
        if f["deaths"] < min_deaths:
            continue
        per = collections.defaultdict(lambda: {"units": 0, "value": 0})
        for u in made:
            t = u["done"] if u["done"] is not None else u["born"]
            if f["start"] <= t <= f["end"]:
                per[str(u["owner"])]["units"] += 1
                per[str(u["owner"])]["value"] += u["m"] + u["g"]
        out.append({"fight": f["id"], "start": f["start"], "end": f["end"],
                    "completed_during": dict(per)})
    return out


# --- B8 -----------------------------------------------------------------------------------------

def death_causes(game: dict) -> dict:
    out = {}
    deaths = F.combat_deaths(game)
    for p in game["players"]:
        pid = p["pid"]
        c = collections.defaultdict(lambda: [0, 0])
        no_unit = 0
        for d in deaths:
            if d["pid"] != pid:
                continue
            if d["ktype"] is None:
                no_unit += 1
                continue
            k = (d["type"], d["ktype"])
            c[k][0] += 1
            c[k][1] += d["value"]
        by_killer = collections.defaultdict(lambda: [0, 0])
        for (_, kt), (n, v) in c.items():
            by_killer[kt][0] += n
            by_killer[kt][1] += v
        out[str(pid)] = {
            "by_killer_type": sorted(([k, n, v] for k, (n, v) in by_killer.items()),
                                     key=lambda r: -r[2]),
            "pairs": sorted(([a, b, n, v] for (a, b), (n, v) in c.items()), key=lambda r: -r[3])[:30],
            "deaths_without_killer_unit": no_unit,
        }
    return out


# --- B9 -----------------------------------------------------------------------------------------

def map_features(map_name: str) -> dict | None:
    f = REPO / "data/maps" / (map_name.replace(" ", "_") + ".features.json")
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def spawn(game: dict, pid: int):
    th = [u for u in game["units"] if u["owner"] == pid and "townhall" in u["kinds"] and u["born"] == 0]
    return (th[0]["x"], th[0]["y"]) if th else None


def main_ramp(features: dict, xy) -> dict | None:
    if not features or not xy or not features.get("ramps"):
        return None
    return min(features["ramps"], key=lambda r: math.hypot(r["base_center"]["x"] - xy[0],
                                                           r["base_center"]["y"] - xy[1]))


def walls(game: dict) -> dict:
    feats = map_features(game["map"])
    out = {}
    for p in game["players"]:
        pid = p["pid"]
        sp = spawn(game, pid)
        ramp = main_ramp(feats, sp)
        if ramp is None:
            out[str(pid)] = {"ramp": None, "why": "no map features or no spawn"}
            continue
        rx, ry = ramp["base_center"]["x"], ramp["base_center"]["y"]
        at = [{"type": u["types"][0][1], "started": u["born"], "done": u["done"], "died": u["died"],
               "dist": round(math.hypot(u["x"] - rx, u["y"] - ry), 1)}
              for u in game["units"] if u["owner"] == pid and u["how"] == "init"
              and "structure" in u["kinds"] and u["types"][0][1] in WALL_TYPES
              and math.hypot(u["x"] - rx, u["y"] - ry) <= WALL_RADIUS]
        at.sort(key=lambda r: r["started"])
        out[str(pid)] = {"ramp": {"id": ramp["id"], "x": rx, "y": ry, "hi": ramp["hi"], "lo": ramp["lo"],
                                  "from_spawn": round(math.hypot(rx - sp[0], ry - sp[1]), 1)},
                         "structures": at,
                         "wall_candidate": len(at) >= 2,
                         "complete_at": max((r["done"] for r in at[:3] if r["done"]), default=None)
                         if len(at) >= 2 else None}
    return out


def all_repositories(game: dict) -> dict:
    return {"trade": trade(game), "expansions": expansions(game),
            "reinforcement": reinforcement(game), "death_causes": death_causes(game),
            "walls": walls(game), "mirror_problems": mirror_check(game)}
