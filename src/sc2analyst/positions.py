"""S2 · Where a unit was at a moment, and how sure that is (D-2).

The replay carries a unit's position at three kinds of moment: where it was born (or its
construction began), where it died, and the tracker's position samples (`UnitPositionsEvent`).
Between them nothing is known. Every position this module gives carries its `age` - seconds since
the moment it was recorded - and a `location_confidence`:

  exact   recorded at this very moment (a birth, a death, a sample)
  fresh   recorded at most FRESH_S ago
  stale   older: the unit has had time to cross the map
  none    nothing recorded yet

A structure does not move (a Terran lift-off aside, which the replay does not sample here): its
birth point is `exact` for its whole life.

FRESH_S is a reporting threshold set from the measured cadence (docs/review/S2-cadence.md), not a
rule of the game.
"""
from __future__ import annotations

import bisect

FRESH_S = 15.0


class Positions:
    def __init__(self, game: dict):
        self.units = {u["tag"]: u for u in game["units"]}
        tracks: dict[int, list] = {}
        for u in game["units"]:
            tracks.setdefault(u["tag"], []).append((u["born"], u["x"], u["y"], "born"))
            if u["died"] is not None and u["dx"] is not None:
                tracks[u["tag"]].append((u["died"], u["dx"], u["dy"], "died"))
        for t, tag, x, y in game["positions"]:
            tracks.setdefault(tag, []).append((t, x, y, "sample"))
        self.tracks = {k: sorted(v) for k, v in tracks.items()}
        self.times = {k: [p[0] for p in v] for k, v in self.tracks.items()}

    def at(self, tag: int, t: float) -> dict:
        tr = self.tracks.get(tag)
        if not tr:
            return {"x": None, "y": None, "age": None, "source": None, "location_confidence": "none"}
        i = bisect.bisect_right(self.times[tag], t) - 1
        if i < 0:
            return {"x": None, "y": None, "age": None, "source": None, "location_confidence": "none"}
        pt, x, y, src = tr[i]
        age = round(t - pt, 2)
        u = self.units.get(tag)
        if u is not None and "structure" in u["kinds"]:
            conf = "exact"
        elif age == 0:
            conf = "exact"
        elif age <= FRESH_S:
            conf = "fresh"
        else:
            conf = "stale"
        return {"x": x, "y": y, "age": age, "source": src, "location_confidence": conf}


def sample_intervals(game: dict) -> list[float]:
    ts = sorted({p[0] for p in game["positions"]})
    return [round(b - a, 2) for a, b in zip(ts, ts[1:])]
