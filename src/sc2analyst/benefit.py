"""DL-29 · Which team benefited from a fight - counting the economy, not only the value lost.

For a two-team game, per fight, from the game's stats samples (`timeline.sampled_rows`):

  ΔN    the change in the standing-value gap (team A − team B), from the last sample at or before the
        fight's start to the first sample at or after its end - what the fight did now
  Δr    the change in the income gap (per minute), the mean over the 60 s before the start against
        the mean over the 60 s after the end - what it did to the economy from then on
  benefit over the next 5 minutes = ΔN + 5·Δr, an ESTIMATE, its two parts always shown

The verdict goes to the team the benefit favours ("even" when it is exactly 0). Beside it stays the
existing verdict, "came out ahead (it lost less value)". When the two disagree the fight is flagged:
"won the fight, lost the economy" is a finding, not an error.

The game's end (the owner's ruling of 2026-10-29, `docs/canon/rulings-2026-10-29.md`): every fight
gets a verdict, the one the game ends in too. With no sample after the end, "after" is the game's
last sample and the income after is the last 60 s of samples, both marked `at_end`. Players leave
on what they can see; whether the enemy was struggling is what they cannot.

What it does not prove: the income after a fight also moves for reasons the fight did not cause
(a base finishing, workers sent to gas); the 60 s windows are a choice (DL-29), not a measurement of
how long a fight's effect lasts. At the game's end "after" is inside or at the end of the fight.
"""
from __future__ import annotations

WINDOW_S = 60.0
HORIZON_MIN = 5.0


def _times(rows: dict, pids: list) -> list:
    """The sample times every one of these players has, in order."""
    sets = [{t for t, _ in rows.get(pid) or []} for pid in pids]
    return sorted(set.intersection(*sets)) if sets and all(sets) else []


def _value(rows: dict, pids: list, t: float, name: str) -> float:
    total = 0.0
    for pid in pids:
        total += next(r[name] for ts, r in rows[pid] if ts == t)
    return total


def _mean(rows: dict, pids: list, ts: list, name: str):
    return sum(_value(rows, pids, t, name) for t in ts) / len(ts) if ts else None


def fight_benefit(game: dict, rows: dict, fight: dict, a: int, b: int) -> dict:
    """Team a's view of one fight (team b's is the negative). `rows` = timeline.sampled_rows(game)."""
    pids = {t: [p["pid"] for p in game["players"] if p["team"] == t] for t in (a, b)}
    common = _times(rows, pids[a] + pids[b])
    lost = fight["lost_value_by_team"]
    la, lb = lost.get(str(a), 0), lost.get(str(b), 0)
    ahead = a if la < lb else (b if lb < la else None)
    out = {"team_a": a, "team_b": b, "lost_less": ahead, "at_end": False}
    if not common:
        return dict(out, dN=None, dr=None, benefit_5min=None, standing_gap={}, income_gap={},
                    benefited=None, verdict="unknown", disagree=False)
    start, end = fight["start"], fight["end"]
    t_before = max([t for t in common if t <= start], default=common[0])
    after = [t for t in common if t >= end]
    at_end = not after
    t_after = after[0] if after else common[-1]
    win_before = [t for t in common if start - WINDOW_S < t <= start] or [t_before]
    win_after = [t for t in common if end < t <= end + WINDOW_S]
    if not win_after:                      # the game ended with the fight: its last 60 s of samples
        win_after = [t for t in common if common[-1] - WINDOW_S < t <= common[-1]]
        at_end = True

    def gap(t, name):
        return _value(rows, pids[a], t, name) - _value(rows, pids[b], t, name)

    n = {"before": gap(t_before, "standing"), "after": gap(t_after, "standing")}
    r = {"before": _mean(rows, pids[a], win_before, "income") - _mean(rows, pids[b], win_before, "income"),
         "after": _mean(rows, pids[a], win_after, "income") - _mean(rows, pids[b], win_after, "income")}
    dn = round(n["after"] - n["before"], 1)
    dr = round(r["after"] - r["before"], 1)
    total = round(dn + HORIZON_MIN * dr, 1)
    benefited = None if total == 0 else (a if total > 0 else b)
    return dict(out, dN=dn, dr=dr, benefit_5min=total, standing_gap=n, income_gap=r, at_end=at_end,
                before_t=t_before, after_t=t_after, benefited=benefited,
                verdict="even" if benefited is None else "team",
                disagree=benefited is not None and ahead is not None and benefited != ahead)
