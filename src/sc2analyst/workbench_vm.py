"""W3 · The workbench's view models: pure functions from a game record to what each screen shows.

The thin-view rule (MVVM): every decision a screen needs - which series, in which order, labelled
how, coloured how, on which scale, with which provenance - is made here and tested with unittest.
The page (`workbench/paint.js`) paints what these return and decides nothing.

Screens: library · game (the gap chart, each player on their own, the fight strip, the events) ·
snapshot at a second (with the minimap) · fight (before / after / Δ, losses by class, both verdicts).
Normal view: minute steps and the game's own counts. Debug: one-second steps (or every sample), the
ledger's counts beside the game's, and every number with its provenance and age (DL-22).
"""
from __future__ import annotations

import bisect
import collections
import re

from . import benefit as BF, build as BU, fights as F, library as L, pick, render, timeline as T

GAP_LINES = [("standing", "Standing value"), ("collected", "Collected"), ("workers_game", "Workers"),
             ("army", "Army (the game's)"), ("economy", "Economy (the game's)"),
             ("technology", "Tech (the game's)"), ("income", "Income / min")]
GAP_DEBUG = [("workers", "Workers (ledger)"), ("army_value", "Army value (ledger)")]
GAP_COLOURS = ["#e6e9ef", "#7aa2ff", "#34d399", "#f87171", "#fbbf24", "#a78bfa", "#2dd4bf", "#f472b6", "#fb923c"]
PLAYER_METRICS = [("workers_game", "Workers"), ("army", "Army"), ("economy", "Economy"),
                  ("technology", "Tech"), ("bank", "Bank"), ("income", "Income / min")]
PLAYER_DEBUG = [("workers", "Workers (ledger)"), ("army_value", "Army value (ledger)"),
                ("off_board", "Off the board")]
SNAP_METRICS = PLAYER_METRICS + [("town_halls", "Town halls"), ("upgrades", "Upgrades"),
                                 ("standing", "Standing value")]
SNAP_DEBUG = PLAYER_DEBUG + [("army_unpriced", "Army unpriced (count)"), ("collected", "Collected"),
                             ("lost", "Lost")]
FIGHT_METRICS = [("workers_game", "Workers"), ("army", "Army"), ("economy", "Economy"),
                 ("technology", "Tech"), ("bank", "Bank"), ("income", "Income / min"),
                 ("town_halls", "Town halls"), ("standing", "Standing value")]
LOSS_CLASSES = ("worker", "town hall", "other structure", "army")


# --- naming and colour -----------------------------------------------------------------------------

def game_key(map_name: str, start_time: str) -> str:
    """What a URL names a game by: map and start minute, never its twin's file name."""
    return "%s_%s" % (re.sub(r"[^A-Za-z0-9]+", "_", map_name).strip("_"),
                      start_time[:16].replace(":", "").replace(" ", "_"))


def names(game: dict) -> dict:
    """pid -> what a page may show: Player_1 / Ally_2.. / Enemy_1.., or Team t · n without the owner."""
    if any(p["is_owner"] for p in game["players"]):
        return pick.side_names(game)
    out, n = {}, collections.Counter()
    for p in sorted(game["players"], key=lambda p: p["pid"]):
        n[p["team"]] += 1
        out[p["pid"]] = "Team %s · %d" % (p["team"], n[p["team"]])
    return out


def team_order(game: dict) -> list:
    """The owner's team first (team A on every gap chart), else by team number."""
    teams = sorted({p["team"] for p in game["players"]})
    owner = next((p for p in game["players"] if p["is_owner"]), None)
    if owner is not None:
        teams.remove(owner["team"])
        teams.insert(0, owner["team"])
    return teams


def team_label(game: dict, team) -> str:
    owner = next((p for p in game["players"] if p["is_owner"]), None)
    if owner is not None:
        return "Your team" if team == owner["team"] else "Enemy team"
    return "Team %s" % team


def colours(game: dict) -> tuple[dict, dict]:
    order = team_order(game)
    tc = {t: render.TEAM_COLOURS[i % len(render.TEAM_COLOURS)] for i, t in enumerate(order)}
    pids = sorted(p["pid"] for p in game["players"])
    pc = {pid: render.PLAYER_COLOURS[i % len(render.PLAYER_COLOURS)] for i, pid in enumerate(pids)}
    return tc, pc


def _openings(game: dict) -> dict:
    """pid -> its opening (DL-26), or {} when the map layer or the thresholds are missing."""
    try:
        from . import openings as O
        th = O.load_thresholds()
        return O.classify(game, th) if th else {}
    except Exception:
        return {}


def header(game: dict) -> dict:
    nm = names(game)
    tc, pc = colours(game)
    op = _openings(game)
    teams = []
    for t in team_order(game):
        ps = [p for p in game["players"] if p["team"] == t]
        teams.append({"team": t, "label": team_label(game, t), "colour": tc[t],
                      "result": ps[0]["result"] if ps else None,
                      "players": [{"pid": p["pid"], "name": nm[p["pid"]], "race": p["race"],
                                   "colour": pc[p["pid"]], "owner": p["is_owner"],
                                   "opening": op.get(p["pid"], {}).get("class"),
                                   "opening_note": _opening_note(op.get(p["pid"]))} for p in ps]})
    return {"map": game["map"], "start": game["start_time"][:16], "format": L.game_format(game),
            "end": game["end"], "end_clock": L.clock(game["end"]), "build": game["build"], "teams": teams}


def _opening_note(o: dict | None) -> str | None:
    if not o:
        return None
    parts = ["first expansion %s%s" % (L.clock(o["first_expansion"]), " (flown)" if o["via"] == "landed" else "")
             if o["first_expansion"] is not None else "no expansion"]
    if o["attack_value"]:
        parts.append("killed %d at the other team's bases before it" % o["attack_value"])
    return " · ".join(parts)


def _range(columns) -> list:
    vals = [x for col in columns for x in col if x is not None]
    if not vals:
        return [0, 1]
    lo, hi = min(vals), max(vals)
    return [lo, hi if hi > lo else lo + 1]


# --- library -----------------------------------------------------------------------------------------

def library_vm(rows: list, q: dict | None = None) -> dict:
    q = q or {}
    games, seen = [], collections.Counter()
    for r in sorted(rows, key=lambda r: r["start_time"], reverse=True):
        key = game_key(r["map"], r["start_time"])
        seen[key] += 1
        if seen[key] > 1:
            key = "%s-%d" % (key, seen[key])
        races = sorted({x for v in r.get("races", {}).values() for x in v})
        games.append({"key": key, "file": r["file"], "map": r["map"], "start": r["start_time"][:16],
                      "format": r.get("format"), "length": L.clock(r.get("end")),
                      "races": " v ".join("/".join(v) for _, v in sorted(r.get("races", {}).items())),
                      "race_set": races, "owner_played": r.get("owner_played", False),
                      "result": r.get("owner_result"), "openings": r.get("openings", [])})
    filters = {"maps": sorted({g["map"] for g in games}),
               "formats": sorted({g["format"] for g in games if g["format"]}),
               "races": sorted({x for g in games for x in g["race_set"]}),
               "openings": [c for c in ("ONE-BASE ATTACK", "FAST EXPAND", "STANDARD", "NO EXPANSION")
                            if any(c in g["openings"] for g in games)]}
    shown = [g for g in games
             if (not q.get("map") or g["map"] == q["map"])
             and (not q.get("format") or g["format"] == q["format"])
             and (not q.get("race") or q["race"] in g["race_set"])
             and (not q.get("result") or g["result"] == q["result"])
             and (not q.get("opening") or q["opening"] in g["openings"])
             and (not q.get("owner") or g["owner_played"])]
    return {"count": len(games), "shown": len(shown), "filters": filters,
            "games": [{k: v for k, v in g.items() if k not in ("file", "race_set")} for g in shown],
            "files": {g["key"]: g["file"] for g in games}}


# --- game --------------------------------------------------------------------------------------------

def _class_of(kinds) -> str:
    k = set(kinds)
    if "worker" in k:
        return "worker"
    if "townhall" in k:
        return "town hall"
    if "structure" in k:
        return "other structure"
    return "army"


def _verdict_label(game: dict, b: dict | None) -> str | None:
    if b is None:
        return None
    if b["verdict"] == "even":
        return "even"
    if b["verdict"] == "unknown":
        return "not known"
    return team_label(game, b["benefited"]) + (" (at the game's end)" if b["at_end"] else "")


def fight_rows(game: dict, rows: dict | None = None, where: dict | None = None) -> list:
    """Every fight with both verdicts (DL-29 and lost-less-value), for the strip and the fight screen."""
    rows = rows if rows is not None else T.sampled_rows(game)
    order = team_order(game)
    tc, _ = colours(game)
    out = []
    for f in F.fights(game):
        b = BF.fight_benefit(game, rows, f, order[0], order[1]) if len(order) == 2 else None
        out.append({"id": f["id"], "start": f["start"], "end": f["end"], "clock": L.clock(f["start"]),
                    "value": f["value"], "deaths": f["deaths"], "where": (where or {}).get(f["id"]),
                    "lost_value_by_team": f["lost_value_by_team"],
                    "benefited": None if b is None else b["benefited"],
                    "benefited_label": _verdict_label(game, b),
                    "at_end": bool(b and b["at_end"]),
                    "benefited_colour": None if not b or b["benefited"] is None else tc[b["benefited"]],
                    "lost_less_label": None if not b or b["lost_less"] is None else team_label(game, b["lost_less"]),
                    "disagree": bool(b and b["disagree"]),
                    "dN": None if b is None else b["dN"], "dr": None if b is None else b["dr"],
                    "benefit_5min": None if b is None else b["benefit_5min"]})
    return out


def _places(game: dict) -> dict:
    """fight id -> its place label; empty when the map layer is missing."""
    try:
        from . import terrain
        return {p["fight"]: p["where"] for p in terrain.place_fights(game, names(game))}
    except Exception:
        return {}


def events(game: dict) -> list:
    nm = names(game)
    out = []
    for u in game["units"]:
        if "townhall" in u["kinds"] and u["how"] == "init" and u["owner"] in nm:
            out.append({"t": u["born"], "pid": u["owner"], "kind": "town hall",
                        "label": "%s · %s started" % (nm[u["owner"]], u["types"][0][1])})
    for t, pid, name in game.get("upgrades", []):
        if t > 1 and pid in nm:
            out.append({"t": t, "pid": pid, "kind": "upgrade", "label": "%s · %s" % (nm[pid], name)})
    for p in game["players"]:
        for blk in BU.supply_blocks(game, p["pid"]):
            if blk["at_least_s"] >= 7:
                out.append({"t": blk["from"], "pid": p["pid"], "kind": "supply block",
                            "label": "%s · supply blocked at %d for ≥ %g s" % (nm[p["pid"]], blk["supply"],
                                                                           blk["at_least_s"])})
    return sorted(out, key=lambda e: (e["t"], e["pid"]))


def game_vm(game: dict, debug: bool = False, key: str | None = None) -> dict:
    step = 1 if debug else 60
    tl = T.timeline(game, step)
    order = team_order(game)
    _, pc = colours(game)
    nm = names(game)
    gap = None
    if len(order) == 2:
        lines = [{"key": k, "label": label, "values": T.gap(tl, order[0], order[1], k),
                  "provenance": T.PROVENANCE[k], "default": k == "standing",
                  "colour": GAP_COLOURS[i % len(GAP_COLOURS)]}
                 for i, (k, label) in enumerate(GAP_LINES + (GAP_DEBUG if debug else []))]
        gap = {"a": team_label(game, order[0]), "b": team_label(game, order[1]), "lines": lines,
               "ranges": {ln["key"]: _range([ln["values"]]) for ln in lines}}
    metrics = PLAYER_METRICS + (PLAYER_DEBUG if debug else [])
    players = []
    for t in order:
        for p in sorted((p for p in game["players"] if p["team"] == t), key=lambda p: p["pid"]):
            s = tl["players"][p["pid"]]
            players.append({"pid": p["pid"], "name": nm[p["pid"]], "colour": pc[p["pid"]], "team": t,
                            "metrics": {k: {"values": s[k], "provenance": T.PROVENANCE[k]} for k, _ in metrics}})
    ranges = {k: _range([pl["metrics"][k]["values"] for pl in players]) for k, _ in metrics}
    return {"key": key, "header": header(game), "debug": debug, "step": step, "t": tl["t"],
            "gap": gap, "metrics": [{"key": k, "label": label} for k, label in metrics],
            "players": players, "ranges": ranges,
            "fights": fight_rows(game, T.sampled_rows(game), _places(game)), "events": events(game)}


# --- snapshot at a second ------------------------------------------------------------------------------

def _cell(series: dict, k: str, i: int, age) -> dict:
    prov = T.PROVENANCE[k]
    return {"v": series[k][i], "provenance": prov, "age": age if prov != "exact" and prov != "exact (count)" else 0}


def minimap_at(game: dict, t: float, positions, width: int = 420) -> str | None:
    """Army and structures alive at t, where they were last seen, faded by the position's age."""
    try:
        svg = render.MinimapSVG(game["map"], width=width)
    except Exception:
        return None
    _, pc = colours(game)
    for u in game["units"]:
        if u["owner"] not in pc or u["done"] is None or u["done"] > t:
            continue
        if u["died"] is not None and u["died"] <= t:
            continue
        k = set(u["kinds"])
        if k & {"transient", "marker", "neutral", "worker"}:
            continue
        if not k & {"army", "structure"}:
            continue
        pos = positions.at(u["tag"], t)
        if pos["x"] is None:
            continue
        faded = pos["location_confidence"] == "stale"
        svg.circle(pos["x"], pos["y"], 3.2 if "structure" in k else 2.2, pc[u["owner"]],
                   opacity=0.3 if faded else 0.9,
                   title="%s · seen %g s ago" % (u["types"][-1][1], pos["age"]))
    return svg.svg("units at %s" % L.clock(t))


def snapshot_vm(game: dict, t: float, debug: bool = False, positions=None) -> dict:
    tl = T.timeline(game, 1)
    i = T.at(tl, t)
    order = team_order(game)
    tc, pc = colours(game)
    nm = names(game)
    metrics = SNAP_METRICS + (SNAP_DEBUG if debug else [])
    players = []
    for team in order:
        for p in sorted((p for p in game["players"] if p["team"] == team), key=lambda p: p["pid"]):
            s, age = tl["players"][p["pid"]], tl["age"][p["pid"]][i]
            players.append({"pid": p["pid"], "name": nm[p["pid"]], "colour": pc[p["pid"]], "team": team,
                            "cells": {k: _cell(s, k, i, age) for k, _ in metrics}})
    teams = [{"team": team, "label": team_label(game, team), "colour": tc[team],
              "cells": {k: {"v": tl["teams"][team][k][i], "provenance": T.PROVENANCE[k]} for k, _ in metrics}}
             for team in order]
    mm = None
    if positions is not None:
        mm = minimap_at(game, tl["t"][i], positions)
    return {"t": tl["t"][i], "clock": L.clock(tl["t"][i]), "debug": debug,
            "metrics": [{"key": k, "label": label} for k, label in metrics],
            "players": players, "teams": teams, "minimap": mm,
            "minimap_note": None if mm else "no minimap: the map layer or the positions are missing"}


# --- fight ---------------------------------------------------------------------------------------------

def _denied(game: dict, fight: dict, rows: dict) -> str:
    """DL-28's ledger lower bound for each victim that lost workers, as one sentence."""
    from . import econ as E
    d = E.denied_income(game, fight, rows)
    if not d:
        return "no workers lost, so nothing beyond the value (towns halls show only in the observed rate)"
    nm = names(game)
    parts = []
    for pid, x in sorted(d.items()):
        if x["denied"] is None:
            parts.append("%s lost %d workers (no income sample before the fight)" % (nm.get(pid, "?"), x["workers_lost"]))
            continue
        parts.append("%s at least %d (%d workers lost; %g worker-seconds short in the next %d s at %g a minute each%s)"
                     % (nm.get(pid, "?"), round(x["denied"]), x["workers_lost"], x["worker_seconds"],
                        E.HORIZON_S, x["per_worker_min"],
                        "; back to its count at %s" % L.clock(x["back_at"]) if x["back_at"] else "; not back within it"))
    return " · ".join(parts)

def fight_vm(game: dict, fight_id: int, debug: bool = False) -> dict:
    fs = F.fights(game)
    f = next((x for x in fs if x["id"] == fight_id), None)
    if f is None:
        raise KeyError("no fight %s" % fight_id)
    rows = T.sampled_rows(game)
    where = _places(game)
    fr = next(x for x in fight_rows(game, rows, where) if x["id"] == fight_id)
    tl = T.timeline(game, "sample")
    ts = tl["t"]
    bi = T.at(tl, f["start"])
    ai = min(bisect.bisect_left(ts, f["end"]), len(ts) - 1)
    order = team_order(game)
    tc, pc = colours(game)
    nm = names(game)

    def table(series):
        out = {}
        for k, _ in FIGHT_METRICS:
            b, a = series[k][bi], series[k][ai]
            out[k] = {"before": b, "after": a,
                      "delta": None if a is None or b is None else round(a - b, 1),
                      "provenance": T.PROVENANCE[k]}
        return out

    deaths = [d for d in F.combat_deaths(game) if f["start"] <= d["t"] <= f["end"]]
    losses = collections.defaultdict(lambda: {c: {"value": 0, "count": 0} for c in LOSS_CLASSES})
    unpriced = collections.Counter()
    for d in deaths:
        c = _class_of(d["kinds"])
        losses[d["pid"]][c]["count"] += 1
        if d["vk"]:
            losses[d["pid"]][c]["value"] += d["value"]
        else:
            unpriced[d["pid"]] += 1
    players = []
    for team in order:
        for p in sorted((p for p in game["players"] if p["team"] == team), key=lambda p: p["pid"]):
            players.append({"pid": p["pid"], "name": nm[p["pid"]], "colour": pc[p["pid"]], "team": team,
                            "table": table(tl["players"][p["pid"]]),
                            "losses": losses[p["pid"]], "unpriced": unpriced[p["pid"]]})
    teams = [{"team": team, "label": team_label(game, team), "colour": tc[team],
              "table": table(tl["teams"][team])} for team in order]
    mm = None
    try:
        svg = render.MinimapSVG(game["map"], width=420)
        for d in deaths:
            if d["x"] is not None:
                svg.circle(d["x"], d["y"], 2.4, pc.get(d["pid"], "#999"),
                           title="%s lost %s" % (nm.get(d["pid"], "?"), d["type"]))
        mm = svg.svg("deaths in fight %d" % fight_id)
    except Exception:
        mm = None
    out = {"id": fight_id, "clock": "%s–%s" % (L.clock(f["start"]), L.clock(f["end"])),
           "start": f["start"], "end": f["end"], "before_t": ts[bi], "after_t": ts[ai],
           "where": fr["where"], "value": f["value"], "deaths": f["deaths"],
           "metrics": [{"key": k, "label": label} for k, label in FIGHT_METRICS],
           "players": players, "teams": teams, "loss_classes": list(LOSS_CLASSES),
           "verdict": {"benefited": fr["benefited_label"], "benefited_colour": fr["benefited_colour"],
                       "at_end": fr["at_end"],
                       "lost_less": fr["lost_less_label"], "disagree": fr["disagree"],
                       "dN": fr["dN"], "dr": fr["dr"], "benefit_5min": fr["benefit_5min"],
                       "team_a": team_label(game, order[0]) if order else None},
           "denied_income": _denied(game, f, rows), "minimap": mm, "debug": debug,
           "prev": fight_id - 1 if fight_id > 1 else None, "next": fight_id + 1 if fight_id < len(fs) else None}
    if debug:
        out["death_rows"] = [{"clock": L.clock(d["t"]), "victim": nm.get(d["pid"], "?"), "type": d["type"],
                              "killer": nm.get(d["kpid"], "?"), "killer_type": d["ktype"],
                              "value": d["value"] if d["vk"] else None, "class": _class_of(d["kinds"])}
                             for d in deaths]
    return out
