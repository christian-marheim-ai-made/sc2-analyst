"""W2 · The timeline: every player and every team at any second (DL-22, DL-25 amended).

Two clocks, never mixed (DL-22):

  exact    from the unit ledger. A unit counts at second t when it has taken its first lasting form
           (larva, eggs and cocoons are transient), `done <= t < died`, under the owner it had at t.
           Workers, army value and supply, structures, town halls, upgrades done.
  sample   from the game's stats sample (record v2, every 7.14 s): the value at t is the LAST sample
           at or before t, with its age; never interpolated, None before the first sample.

Collected and standing (DL-25 as amended by W1): collected C(t) = what the player held at its
first sample (the starting endowment: bank + board + lost) plus the game's collection rate
integrated over the samples (each interval at its first sample's rate, `repositories._income`'s
rule). Standing N = C - lost. The board = bank + used now + in progress by the game's categories.
Off the board = N - bank - the board: research paid for, and whatever else the counters miss
(`docs/review/W1-record-v2.md` §2) - its own series, never folded into a category.

What it does not prove: a morph counts at its final value from its first lasting form (a zergling
that became a baneling counts as the baneling's value while it was a zergling); a unit with no
price in sc2reader's data adds to `army_unpriced`, never to a value (DL-20); units in training are
invisible to the exact clock until born - the sampled `in_progress_army` holds them.
"""
from __future__ import annotations

import bisect
import collections

from .adapter_sc2 import STAT, TRANSIENT

CATS = ("army", "economy", "technology")

# series name -> where it comes from (the provenance chip the workbench shows)
PROVENANCE = {
    "workers": "exact", "army_value": "exact", "army_supply": "exact", "army_unpriced": "exact (count)",
    "structures_value": "exact", "town_halls": "exact", "upgrades": "exact",
    "bank": "sample", "income": "sample", "food_used": "sample", "food_made": "sample",
    "workers_game": "sample",
    "army": "sample", "economy": "sample", "technology": "sample",
    "in_progress_army": "sample", "lost": "sample", "killed": "sample",
    "collected": "integrated", "standing": "integrated", "off_board": "integrated",
}
EXACT = ("workers", "army_value", "army_supply", "army_unpriced", "structures_value", "town_halls",
         "upgrades")
SAMPLED = ("bank", "income", "food_used", "food_made", "workers_game", "army", "economy", "technology",
           "in_progress_army", "lost", "killed", "collected", "standing", "off_board")


def _v(v, *names):
    return sum(v[STAT[n]] for n in names)


def board(v) -> dict:
    """The game's categories at one sample: used now + in progress, minerals + gas."""
    return {c: _v(v, "minerals_used_current_%s" % c, "vespene_used_current_%s" % c,
                  "minerals_used_in_progress_%s" % c, "vespene_used_in_progress_%s" % c) for c in CATS}


def _lost(v):
    return sum(_v(v, "minerals_lost_%s" % c, "vespene_lost_%s" % c) for c in CATS)


def _killed(v):
    return sum(_v(v, "minerals_killed_%s" % c, "vespene_killed_%s" % c) for c in CATS)


def _bank(v):
    return _v(v, "minerals_current", "vespene_current")


def sampled_rows(game: dict) -> dict:
    """pid -> [(t, {series: value})], one per stats sample, in time order."""
    by = collections.defaultdict(list)
    for s in game["stats"]:
        by[s["pid"]].append(s)
    out = {}
    for pid, ss in by.items():
        ss.sort(key=lambda s: s["t"])
        v0 = ss[0]["v"]
        start = _bank(v0) + sum(board(v0).values()) + _lost(v0)   # the starting endowment
        integ = 0.0
        rows = []
        for i, s in enumerate(ss):
            if i:
                a = ss[i - 1]
                integ += (a["m_rate"] + a["g_rate"]) * (s["t"] - a["t"]) / 60
            v = s["v"]
            b = board(v)
            collected = start + integ
            lost = _lost(v)
            standing = collected - lost
            rows.append((s["t"], {
                "bank": _bank(v), "income": s["m_rate"] + s["g_rate"],
                "food_used": s["food_used"], "food_made": s["food_made"], "workers_game": s["workers"],
                "army": b["army"], "economy": b["economy"], "technology": b["technology"],
                "in_progress_army": _v(v, "minerals_used_in_progress_army", "vespene_used_in_progress_army"),
                "lost": lost, "killed": _killed(v),
                "collected": round(collected, 1), "standing": round(standing, 1),
                "off_board": round(standing - _bank(v) - sum(b.values()), 1)}))
        out[pid] = rows
    return out


def _owner_spans(u: dict, changes: list) -> list:
    """[(from, to, owner)] over the unit's life: its owner at each moment (owner_changes)."""
    start = u["done"]
    end = u["died"] if u["died"] is not None else float("inf")
    if not changes:
        return [(start, end, u["owner"])]
    spans = []
    owner, t = changes[0][2], start                      # the owner before the first change
    for ct, _tag, _old, new in changes:
        if ct > t:
            spans.append((t, min(ct, end), owner))
        owner, t = new, max(t, ct)
    if t < end:
        spans.append((t, end, owner))
    return [s for s in spans if s[1] > s[0]]


def _first_lasting(u: dict) -> float | None:
    """When the unit first took a form that is not transient (a larva, an egg, a cocoon)."""
    if "transient" in u["kinds"] or "marker" in u["kinds"] or "neutral" in u["kinds"]:
        return None
    for t, name in u["types"]:
        if name not in TRANSIENT:
            return t
    return None


def _exact_events(game: dict) -> dict:
    """pid -> series -> [(t, delta)], a step at each start and end."""
    team = {p["pid"] for p in game["players"]}
    changes = collections.defaultdict(list)
    for c in game.get("owner_changes", []):
        changes[c[1]].append(c)
    ev = collections.defaultdict(lambda: collections.defaultdict(list))
    for u in game["units"]:
        if u["done"] is None:
            continue
        first = _first_lasting(u)
        if first is None:
            continue
        k = set(u["kinds"])
        value = u["m"] + u["g"]
        priced = u.get("vk", True)
        adds = []
        if "worker" in k:
            adds.append(("workers", 1))
        if "army" in k:
            adds.append(("army_supply", u.get("sup") or 0))
            adds.append(("army_value", value) if priced else ("army_unpriced", 1))
        if "structure" in k and priced:
            adds.append(("structures_value", value))
        if "townhall" in k:
            adds.append(("town_halls", 1))
        if not adds:
            continue
        lu = dict(u, done=max(u["done"], first))
        for t0, t1, owner in _owner_spans(lu, sorted(changes.get(u["tag"], []))):
            if owner not in team:
                continue
            for name, d in adds:
                ev[owner][name].append((t0, d))
                if t1 != float("inf"):
                    ev[owner][name].append((t1, -d))
    for t, pid, name in game.get("upgrades", []):
        if t > 1 and pid in team:                       # 0:00 upgrades are the game's set-up
            ev[pid]["upgrades"].append((t, 1))
    return ev


def _step_values(events: list, ts: list) -> list:
    """The running sum of (t, delta) at each t in ts: a start at t counts at t, an end at t does not."""
    events = sorted(events)
    out, total, i = [], 0, 0
    for t in ts:
        while i < len(events) and events[i][0] <= t:
            total += events[i][1]
            i += 1
        out.append(round(total, 2) if isinstance(total, float) else total)
    return out


def times(game: dict, step) -> list:
    """step 60 or 1 (seconds), or "sample": every stats sample time in the game."""
    end = game["end"]
    if step == "sample":
        return sorted({s["t"] for s in game["stats"]})
    ts = [float(t) for t in range(0, int(end) + 1, int(step))]
    if ts[-1] < end:
        ts.append(end)
    return ts


def timeline(game: dict, step=60) -> dict:
    """{"t", "players": {pid: {series: [..]}}, "age": {pid: [..]}, "teams": {team: {series: [..]}},
    "provenance"}. A sampled value is None before the player's first sample; its age is t - sample."""
    ts = times(game, step)
    ev = _exact_events(game)
    rows = sampled_rows(game)
    players, ages = {}, {}
    for p in game["players"]:
        pid = p["pid"]
        series = {name: _step_values(ev[pid][name], ts) for name in EXACT}
        prs = rows.get(pid, [])
        sts = [r[0] for r in prs]
        idx = [bisect.bisect_right(sts, t) - 1 for t in ts]
        for name in SAMPLED:
            series[name] = [prs[i][1][name] if i >= 0 else None for i in idx]
        ages[pid] = [round(t - sts[i], 2) if i >= 0 else None for t, i in zip(ts, idx)]
        players[pid] = series
    teams = {}
    for p in game["players"]:
        teams.setdefault(p["team"], []).append(p["pid"])
    team_series = {}
    for team, pids in teams.items():
        ts_out = {}
        for name in EXACT + SAMPLED:
            col = []
            for i in range(len(ts)):
                vals = [players[pid][name][i] for pid in pids]
                col.append(None if any(x is None for x in vals) else round(sum(vals), 2))
            ts_out[name] = col
        team_series[team] = ts_out
    return {"t": ts, "players": players, "age": ages, "teams": team_series,
            "team_of": {p["pid"]: p["team"] for p in game["players"]}, "provenance": PROVENANCE}


def gap(tl: dict, a, b, name: str) -> list:
    """Team a minus team b for one series, None where either is unknown."""
    xa, xb = tl["teams"][a][name], tl["teams"][b][name]
    return [None if x is None or y is None else round(x - y, 2) for x, y in zip(xa, xb)]


def at(tl: dict, t: float) -> int:
    """The index of the last time in the timeline at or before t."""
    return max(0, bisect.bisect_right(tl["t"], t) - 1)
