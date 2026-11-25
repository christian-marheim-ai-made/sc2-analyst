"""W5 · What a kill costs beyond its value, catching up, and where each team stood at the end.

DL-28 · income denied - two estimators, reported side by side, never merged:
  ledger    a LOWER BOUND: after the fight, every second a worker short of the victim's pre-fight
            count (the unit ledger's exact count) times the victim's own income per active worker
            at the last sample before the fight, from the fight's end for at most HORIZON_S. New
            workers built meanwhile fill the gap, so the victim's own production is credited and
            the number is never more than what was lost.
  observed  the victim's income over (end + 60 s, end + 180 s] against the 60 s before the fight -
            what the game's own rate did; over the library, fitted on the value lost per class
            (`ols`), an association
  effective trade = value lost + ledger income denied

DL-27 · catching up, per one-base attack (openings.py): the attack window runs from the start of
the attacker's first attack fight (t0) to 60 s after its last attack fight before its own first
expansion (t1). Pair framing: the attacker against the players whose units it killed at their
bases. Team framing: the two teams. E = collected(expander side) - collected(attacker side);
D = what the expander side lost in the window - what the attacker side lost in it; r = the income
gap, the 60 s before t0 and after t1. Caught up when D >= E(t1) - E(t0-) + max(0, E(t0-)).

The final position (the owner's ruling of 2026-10-29): each team's standing, income, workers, army,
economy and tech at the game's last sample, and on which of them the team that LEFT (the loser) was
ahead. A player leaves on what he can see; the enemy's struggle is what he cannot.

What it does not prove: causes. A worker short after a fight may be short because the victim chose
to stop building workers; an income change after a fight moves with bases finishing and gas taken.
"""
from __future__ import annotations

import collections

from . import fights as F, timeline as T

HORIZON_S = 300.0
OBS_FROM_S, OBS_TO_S, OBS_BEFORE_S = 60.0, 180.0, 60.0
CLASSES = ("worker", "town hall", "other structure", "army")


def class_of(kinds) -> str:
    k = set(kinds)
    if "worker" in k:
        return "worker"
    if "townhall" in k:
        return "town hall"
    if "structure" in k:
        return "other structure"
    return "army"


def _last_at_or_before(rows_pid: list, t: float):
    r = None
    for ts, row in rows_pid:
        if ts <= t:
            r = row
        else:
            break
    return r


def _mean_income(rows_pid: list, t0: float, t1: float):
    v = [row["income"] for ts, row in rows_pid if t0 < ts <= t1]
    return sum(v) / len(v) if v else None


def _alive(events: list, t: float) -> int:
    return sum(d for et, d in events if et <= t)


def worker_deficit_seconds(events: list, pre: int, start: float, horizon: float) -> tuple[float, float]:
    """∫ max(0, pre - alive(t)) dt over [start, start + horizon]; -> (worker-seconds, recovered at or None)."""
    events = sorted(events)
    steps = sorted({start} | {t for t, _ in events if start < t < start + horizon} | {start + horizon})
    total, recovered = 0.0, None
    for a, b in zip(steps, steps[1:]):
        short = max(0, pre - _alive(events, a))
        if short == 0 and recovered is None:
            recovered = a
        total += short * (b - a)
    return total, recovered


def denied_income(game: dict, fight: dict, rows: dict, ev: dict | None = None, deaths=None) -> dict:
    """pid -> the ledger lower bound for one fight (only victims that lost workers). `ev`, `deaths`:
    the game's exact events and combat deaths, when the caller has them already."""
    ev = ev if ev is not None else T._exact_events(game)
    deaths = [d for d in (deaths if deaths is not None else F.combat_deaths(game))
              if fight["start"] <= d["t"] <= fight["end"]]
    lost = collections.Counter(d["pid"] for d in deaths if class_of(d["kinds"]) == "worker")
    out = {}
    for pid, n in lost.items():
        events = ev[pid]["workers"]
        pre = _alive(sorted(events), fight["start"] - 0.01)
        row = _last_at_or_before(rows.get(pid, []), fight["start"])
        per = row["income"] / row["workers_game"] if row and row["workers_game"] else None
        secs, back = worker_deficit_seconds(events, pre, fight["end"], HORIZON_S)
        out[pid] = {"workers_lost": n, "pre": pre, "per_worker_min": None if per is None else round(per, 1),
                    "worker_seconds": round(secs, 1), "back_at": back,
                    "denied": None if per is None else round(secs * per / 60, 1)}
    return out


def observed_change(rows: dict, pid: int, fight: dict):
    rs = rows.get(pid, [])
    before = _mean_income(rs, fight["start"] - OBS_BEFORE_S, fight["start"])
    after = _mean_income(rs, fight["end"] + OBS_FROM_S, fight["end"] + OBS_TO_S)
    return None if before is None or after is None else round(after - before, 1)


def losses_by_class(game: dict, fight: dict, deaths=None) -> dict:
    """pid -> class -> priced value lost in the fight."""
    out = collections.defaultdict(lambda: {c: 0 for c in CLASSES})
    for d in (deaths if deaths is not None else F.combat_deaths(game)):
        if fight["start"] <= d["t"] <= fight["end"] and d["vk"]:
            out[d["pid"]][class_of(d["kinds"])] += d["value"]
    return out


# --- the fit: least squares in the standard library ------------------------------------------------

def ols(X: list, y: list) -> dict:
    """Least squares with an intercept: y ~ b0 + Σ bi·xi. -> {coef, n, resid_sd}; Gaussian elimination."""
    n, k = len(y), len(X[0]) + 1
    A = [[1.0] + [float(v) for v in row] for row in X]
    m = [[sum(A[r][i] * A[r][j] for r in range(n)) for j in range(k)] for i in range(k)]
    v = [sum(A[r][i] * y[r] for r in range(n)) for i in range(k)]
    for c in range(k):                                    # forward elimination with partial pivoting
        p = max(range(c, k), key=lambda r: abs(m[r][c]))
        m[c], m[p], v[c], v[p] = m[p], m[c], v[p], v[c]
        if abs(m[c][c]) < 1e-12:
            raise ValueError("the classes are collinear on this data")
        for r in range(c + 1, k):
            f = m[r][c] / m[c][c]
            m[r] = [a - f * b for a, b in zip(m[r], m[c])]
            v[r] -= f * v[c]
    coef = [0.0] * k
    for c in reversed(range(k)):
        coef[c] = (v[c] - sum(m[c][j] * coef[j] for j in range(c + 1, k))) / m[c][c]
    res = [y[r] - sum(A[r][j] * coef[j] for j in range(k)) for r in range(n)]
    sd = (sum(e * e for e in res) / max(1, n - k)) ** 0.5
    return {"coef": coef, "n": n, "resid_sd": sd}


# --- DL-27 catching up --------------------------------------------------------------------------------

def _team_sum(rows: dict, pids, t: float, name: str):
    total = 0.0
    for pid in pids:
        r = _last_at_or_before(rows.get(pid, []), t)
        if r is None:
            return None
        total += r[name]
    return total


def _team_mean_income(rows: dict, pids, t0: float, t1: float):
    vals = [_mean_income(rows.get(pid, []), t0, t1) for pid in pids]
    return None if any(v is None for v in vals) else sum(vals)


def catch_up(game: dict, rows: dict, classes: dict, kills: dict) -> list:
    """One row per (attacker, framing). `classes` = openings.classify(game); `kills` = openings.attack_kills."""
    fights = {f["id"]: f for f in F.fights(game)}
    team = {p["pid"]: p["team"] for p in game["players"]}
    out = []
    for pid, c in classes.items():
        if c["class"] != "ONE-BASE ATTACK":
            continue
        until = min(c["first_expansion"] if c["first_expansion"] is not None else float("inf"), 480.0)
        mine = [k for k in kills.get(pid, []) if k[0] < until and k[2] in fights]
        if not mine:
            continue
        t0 = fights[mine[0][2]]["start"]
        t1 = max(fights[k[2]]["end"] for k in mine) + 60.0
        sides = {"pair": ({pid}, {k[3] for k in mine}),
                 "team": ({p for p, t in team.items() if t == team[pid]},
                          {p for p, t in team.items() if t != team[pid]})}
        for framing, (atk, exp) in sides.items():
            e0 = _gap(rows, exp, atk, t0 - 0.01, "collected")
            e1 = _gap(rows, exp, atk, t1, "collected")
            la0, la1 = _team_sum(rows, atk, t0 - 0.01, "lost"), _team_sum(rows, atk, t1, "lost")
            le0, le1 = _team_sum(rows, exp, t0 - 0.01, "lost"), _team_sum(rows, exp, t1, "lost")
            d = None if None in (la0, la1, le0, le1) else (le1 - le0) - (la1 - la0)
            rb = _income_gap(rows, exp, atk, t0 - 60.0, t0)
            ra = _income_gap(rows, exp, atk, t1, t1 + 60.0)
            caught = None if None in (d, e0, e1) else d >= e1 - e0 + max(0.0, e0)
            out.append({"attacker": pid, "framing": framing, "expanders": sorted(exp), "t0": t0, "t1": t1,
                        "E0": e0, "E1": e1, "D": d, "r_before": rb, "r_after": ra, "caught_up": caught,
                        "needed_at_t0": e0, "attacker_won": _won(game, pid)})
    return out


def _gap(rows, a, b, t, name):
    x, y = _team_sum(rows, a, t, name), _team_sum(rows, b, t, name)
    return None if x is None or y is None else round(x - y, 1)


def _income_gap(rows, a, b, t0, t1):
    x, y = _team_mean_income(rows, a, t0, t1), _team_mean_income(rows, b, t0, t1)
    return None if x is None or y is None else round(x - y, 1)


def _won(game, pid):
    p = next((p for p in game["players"] if p["pid"] == pid), None)
    return None if p is None or p["result"] not in ("Win", "Loss") else p["result"] == "Win"


# --- the final position ----------------------------------------------------------------------------------

FINAL_MEASURES = ("standing", "income", "workers_game", "army", "economy", "technology", "collected")


def final_position(game: dict, rows: dict | None = None) -> dict | None:
    """Each team at the game's last common sample, and on which measures the team that left was ahead."""
    rows = rows if rows is not None else T.sampled_rows(game)
    teams = sorted({p["team"] for p in game["players"]})
    if len(teams) != 2:
        return None
    result = {}
    for t in teams:
        rs = {p["result"] for p in game["players"] if p["team"] == t}
        result[t] = rs.pop() if len(rs) == 1 else None
    common = None
    for p in game["players"]:
        ts = {t for t, _ in rows.get(p["pid"], [])}
        common = ts if common is None else common & ts
    if not common:
        return None
    t_last = max(common)
    pos = {}
    for t in teams:
        pids = [p["pid"] for p in game["players"] if p["team"] == t]
        pos[t] = {m: _team_sum(rows, pids, t_last, m) for m in FINAL_MEASURES}
    loser = next((t for t in teams if result.get(t) == "Loss"), None)
    winner = next((t for t in teams if result.get(t) == "Win"), None)
    ahead = None
    if loser is not None and winner is not None:
        ahead = {m: pos[loser][m] > pos[winner][m] for m in FINAL_MEASURES}
    return {"t": t_last, "end": game["end"], "teams": pos, "result": result, "loser": loser,
            "winner": winner, "loser_ahead": ahead}
