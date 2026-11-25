"""S1 · Fights (DL-1, R-P): a fight ends when the fighting stops for more than 10 seconds.

The owner's words (Q4): *"When the fighting stops for more than 10 seconds"*. The observable is
the COMBAT DEATH: a player's unit killed by a player of another team. Measured 2026-10-26 on the
twins: a death with no killer is a unit becoming something else (larva used, a drone becoming a
building, two templar merging); a death killed by its own owner is a self-destruct (a baneling's
blast, a salvage) or, far more often, a summon timing out (broodlings, MULEs, locusts, the
spray's invisible dummy). Neither is fighting, so neither keeps a fight alive or starts one.

Time-only, as ruled. Two fights at once in different places merge under the rule; the instrument
reports how often a fight's deaths fall into separate places (`places`), and splitting by place is
the owner's call (DL-1), never done here.

What a fight does NOT prove: who fought. A death needs contact; units that shot and lost nothing
leave no mark, and a fight with no deaths does not exist here. Positions are where units died.
"""
from __future__ import annotations

import math

GAP_S = 10.0
PLACE_LINK = 20.0  # cells; a reporting parameter for `places`, never a rule (DL-3 unmeasured)


def combat_deaths(game: dict) -> list[dict]:
    team = {p["pid"]: p["team"] for p in game["players"]}
    out = []
    for u in game["units"]:
        if u["died"] is None or u["owner"] not in team or u["kpid"] not in team:
            continue
        if team[u["kpid"]] == team[u["owner"]]:
            continue
        out.append({"t": u["died"], "x": u["dx"], "y": u["dy"], "pid": u["owner"],
                    "team": team[u["owner"]], "kpid": u["kpid"], "type": u["types"][-1][1],
                    "ktype": u["ktype"], "value": u["m"] + u["g"], "vk": u.get("vk", True),
                    "kinds": u["kinds"]})
    out.sort(key=lambda d: (d["t"], d["pid"], d["type"]))
    return out


def death_categories(game: dict) -> dict:
    team = {p["pid"]: p["team"] for p in game["players"]}
    c = {"enemy": 0, "no_killer": 0, "self": 0, "ally": 0, "neutral_victim": 0, "other": 0}
    for u in game["units"]:
        if u["died"] is None:
            continue
        if u["owner"] not in team:
            c["neutral_victim"] += 1
        elif u["kpid"] is None:
            c["no_killer"] += 1
        elif u["kpid"] == u["owner"]:
            c["self"] += 1
        elif team.get(u["kpid"]) == team[u["owner"]]:
            c["ally"] += 1
        elif u["kpid"] in team:
            c["enemy"] += 1
        else:
            c["other"] += 1
    return c


def segment(deaths: list[dict], gap_s: float = GAP_S) -> list[list[dict]]:
    """Maximal runs of deaths, consecutive deaths at most `gap_s` apart. Sorted input."""
    runs: list[list[dict]] = []
    for d in deaths:
        if runs and d["t"] - runs[-1][-1]["t"] <= gap_s:
            runs[-1].append(d)
        else:
            runs.append([d])
    return runs


def places(deaths: list[dict], link: float = PLACE_LINK) -> list[list[dict]]:
    """Single-linkage groups of death positions: two deaths within `link` cells share a place."""
    n = len(deaths)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if math.hypot(deaths[i]["x"] - deaths[j]["x"], deaths[i]["y"] - deaths[j]["y"]) <= link:
                parent[find(i)] = find(j)
    groups: dict[int, list] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(deaths[i])
    return sorted(groups.values(), key=len, reverse=True)


def fights(game: dict, gap_s: float = GAP_S) -> list[dict]:
    teams = sorted({p["team"] for p in game["players"]})
    out = []
    for i, run in enumerate(segment(combat_deaths(game), gap_s)):
        lost = {t: 0 for t in teams}
        dead = {t: 0 for t in teams}
        by_player: dict[int, dict] = {}
        for d in run:
            lost[d["team"]] += d["value"]
            dead[d["team"]] += 1
            p = by_player.setdefault(d["pid"], {"lost_value": 0, "deaths": 0,
                                                 "killed_value": 0, "kills": 0})
            p["lost_value"] += d["value"]
            p["deaths"] += 1
            k = by_player.setdefault(d["kpid"], {"lost_value": 0, "deaths": 0,
                                                  "killed_value": 0, "kills": 0})
            k["killed_value"] += d["value"]
            k["kills"] += 1
        cx = sum(d["x"] for d in run) / len(run)
        cy = sum(d["y"] for d in run) / len(run)
        pl = places(run)
        out.append({
            "id": i + 1,
            "start": run[0]["t"], "end": run[-1]["t"],
            "duration": round(run[-1]["t"] - run[0]["t"], 2),
            "deaths": len(run),
            "deaths_by_team": {str(t): dead[t] for t in teams},
            "lost_value_by_team": {str(t): lost[t] for t in teams},
            "by_player": {str(k): v for k, v in sorted(by_player.items())},
            "value": sum(lost.values()),
            "deaths_value_unknown": sum(1 for d in run if not d["vk"]),
            "centre": [round(cx, 1), round(cy, 1)],
            "spread": round(max(math.hypot(d["x"] - cx, d["y"] - cy) for d in run), 1),
            "places": len(pl),
            "places_of_3plus": sum(1 for g in pl if len(g) >= 3),
            "place_centres": [[round(sum(d["x"] for d in g) / len(g), 1),
                               round(sum(d["y"] for d in g) / len(g), 1), len(g)] for g in pl],
        })
    return out
