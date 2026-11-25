"""W4 · Openings and attack fights, per player (DL-26).

An *expansion* is a town hall at a base other than the player's main. Where a town hall is: where
it was started, then every spot it LANDED. Terran halls lift off often (445 of 1,097 Command
Centers in 150 games, measured 2026-10-28) - built in the main, flown to the natural. The landing
spot is not in the tracker; the player's `LandCommandCenter` / `LandOrbitalCommand` command carries
it, and the last such command before the hall lands is taken as its spot. An expansion counts from
the hall's START (the money is committed then), with its landing time beside it. A hall at no base
is a *macro hall*, listed apart.

An *attack* for a player is priced value it killed AT a base held by the other team - each death
placed on the map (`terrain.Frame.describe`), the base's holder at that second, or an enemy main.

The class, from `data/measured/openings.json` (thresholds measured over the library by
`tools/measure_openings.py`, frozen there):

  ONE-BASE ATTACK  killed >= attack_min at the other team's bases before starting its first
                   expansion, and before cut_s
  FAST EXPAND      not that; its first expansion started at or before its race's threshold
  STANDARD         not those; expanded before cut_s
  NO EXPANSION     none of the above

What it does not prove: intent. A player classed ONE-BASE ATTACK killed things at an enemy base
before expanding; whether that was the plan or a defence that spilled over is not in the replay.
A hall that landed twice is placed by the land commands' order, which is right when one hall flies
at a time.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from . import fights as F, library as L, terrain as TR

REPO = Path(__file__).resolve().parents[2]
THRESHOLDS = REPO / "data/measured/openings.json"
CLASSES = ("ONE-BASE ATTACK", "FAST EXPAND", "STANDARD", "NO EXPANSION")
DEFAULTS = {"attack_min": 300, "cut_s": 480.0, "fe_quantile": 0.33}


def load_thresholds(path: Path = THRESHOLDS) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _base_at(frame, x, y):
    best = None
    for b in frame.bases:
        d = math.hypot(x - b["center"]["x"], y - b["center"]["y"])
        if d <= TR.HALL_R and (best is None or d < best[0]):
            best = (d, b)
    return best[1] if best else None


def town_halls(game: dict, frame=None) -> dict:
    """pid -> [{started, done, base, kind: start|main|expansion|macro, landed, via}] in start order."""
    frame = frame or TR.Frame(game)
    out = {p["pid"]: [] for p in game["players"]}
    for u in sorted((u for u in game["units"] if "townhall" in u["kinds"] and u["owner"] in out),
                    key=lambda u: u["born"]):
        spots = frame.spots.get(u["tag"]) or TR.hall_spots(game, u)
        kind, base, landed, via = None, None, None, None
        for t, x, y, how in spots:
            b = _base_at(frame, x, y)
            if b is not None and frame.mains.get(b["id"]) != u["owner"]:
                kind, base, landed, via = "expansion", b["id"], (t if how == "landed" else None), how
                break
            if kind is None:
                kind = "main" if b is not None else "macro"
                base = b["id"] if b is not None else None
        if u["born"] == 0 and u["how"] == "born" and kind != "expansion":
            kind = "start"
        out[u["owner"]].append({"started": u["born"], "done": u["done"], "died": u["died"], "base": base,
                                "kind": kind, "landed": landed, "via": via, "type": u["types"][0][1]})
    return out


def attack_kills(game: dict, frame=None) -> dict:
    """pid -> [(t, value, fight id, victim pid)]: priced value it killed at a base held by the other team."""
    frame = frame or TR.Frame(game)
    team = {p["pid"]: p["team"] for p in game["players"]}
    fid = {}
    for f in F.fights(game):
        fid[(f["start"], f["end"])] = f["id"]
    spans = sorted(fid)
    out = {pid: [] for pid in team}
    for d in F.combat_deaths(game):
        if not d["vk"] or d["x"] is None or d["kpid"] not in team:
            continue
        where = frame.describe(d["x"], d["y"], d["t"])
        b = where.get("base")
        if not b:
            continue
        holder_team = team.get(b["holder"]) if b["holder"] is not None else None
        main_team = team.get(b["main_of"]) if b["main_of"] is not None else None
        enemy = team[d["kpid"]]
        if (holder_team is not None and holder_team != enemy) or \
                (holder_team is None and main_team is not None and main_team != enemy):
            f = next((fid[s] for s in spans if s[0] <= d["t"] <= s[1]), None)
            out[d["kpid"]].append((d["t"], d["value"], f, d["pid"]))
    return out


def threshold_for(th: dict, race: str, fmt: str):
    by = th["fe_threshold_s"].get(race, {})
    return by.get(fmt, by.get("all"))


def classify(game: dict, th: dict | None = None, attack_min=None, cut_s=None, fe_q=None, parts=None) -> dict:
    """pid -> {class, first_expansion, landed, attack_value, first_attack, attack_fights, macro_halls}.
    Thresholds from `th` (openings.json) unless given; fe_q picks another measured quantile. `parts` =
    (town_halls, attack_kills) already computed, so the variants of one game cost one map pass."""
    th = th if th is not None else (load_thresholds() or {"fe_threshold_s": {}, **DEFAULTS})
    attack_min = th.get("attack_min", DEFAULTS["attack_min"]) if attack_min is None else attack_min
    cut_s = th.get("cut_s", DEFAULTS["cut_s"]) if cut_s is None else cut_s
    if parts is None:
        frame = TR.Frame(game)
        parts = (town_halls(game, frame), attack_kills(game, frame))
    halls, kills = parts
    fmt = L.game_format(game)
    out = {}
    for p in game["players"]:
        pid = p["pid"]
        exps = [h for h in halls[pid] if h["kind"] == "expansion"]
        first = exps[0] if exps else None
        until = min(first["started"] if first else float("inf"), cut_s)
        before = [k for k in kills[pid] if k[0] < until]
        value = sum(k[1] for k in before)
        if fe_q is None:
            fe = threshold_for(th, p["race"], fmt)
        else:
            fe = (th.get("quantiles", {}).get(p["race"], {}).get(fmt) or
                  th.get("quantiles", {}).get(p["race"], {}).get("all") or {}).get(str(fe_q))
        if value >= attack_min:
            cls = "ONE-BASE ATTACK"
        elif first and fe is not None and first["started"] <= fe:
            cls = "FAST EXPAND"
        elif first and first["started"] < cut_s:
            cls = "STANDARD"
        else:
            cls = "NO EXPANSION"
        out[pid] = {"class": cls, "race": p["race"], "team": p["team"],
                    "first_expansion": first["started"] if first else None,
                    "landed": first["landed"] if first else None, "via": first["via"] if first else None,
                    "fe_threshold": fe, "attack_value": value,
                    "first_attack": before[0][0] if before else None,
                    "attack_fights": sorted({k[2] for k in before if k[2] is not None}),
                    "macro_halls": sum(1 for h in halls[pid] if h["kind"] == "macro")}
    return out
