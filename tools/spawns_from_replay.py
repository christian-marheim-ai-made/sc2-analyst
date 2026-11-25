#!/usr/bin/env python3
"""Spawn positions come from the REPLAY, never from the map's StartLoc list.

Ruling 2026-09-24 (library finding L-4, ratified): a map may carry more `StartLoc` points than the
game has players — Old Sun Temple LE has four on a map played 1v1. So the map's list is only the
*candidates*; the spawn a player actually got is the town hall they own at frame 0.

Measured on the canon replay `andre_gamet` (White Chamber, 3v3, build 96883): six town halls are born
at frame 0, one per player, each 0.71 cells from a StartLoc — the object point sits on the cell's
half-coordinate (`75.5, 175.5`) and the unit on the whole one (`75.0, 175.0`).

    spawns(replay)                      -> [{pid, team, town_hall, x, y}]      (sorted by pid)
    match_to_candidates(spawns, starts) -> the same rows with start_loc / start_loc_distance

No player name is read or returned: a row carries the player id and team number only.
"""
from __future__ import annotations

TOWN_HALLS = ("CommandCenter", "Nexus", "Hatchery")
MATCH_TOLERANCE = 1.5   # cells; the measured offset is 0.71


def spawns(replay):
    """The town hall each player owns at frame 0. `replay` is an sc2reader replay at load_level=4."""
    out = []
    for e in replay.tracker_events:
        if e.frame != 0:
            continue
        if e.name != "UnitBornEvent" or e.unit_type_name not in TOWN_HALLS:
            continue
        player = replay.player.get(e.control_pid)
        team = getattr(getattr(player, "team", None), "number", None)
        out.append({"pid": e.control_pid, "team": team, "town_hall": e.unit_type_name,
                    "x": float(e.x), "y": float(e.y)})
    return sorted(out, key=lambda s: s["pid"])


def match_to_candidates(rows, start_locations, tolerance=MATCH_TOLERANCE):
    """Attach each spawn to the nearest map StartLoc within `tolerance`, or None if none is near.

    `start_locations` is the extractor's list: [{"name", "position": {"x", "y", ...}}].
    """
    for s in rows:
        best = None
        for c in start_locations:
            d = ((c["position"]["x"] - s["x"]) ** 2 + (c["position"]["y"] - s["y"]) ** 2) ** 0.5
            if d <= tolerance and (best is None or d < best[1]):
                best = (c["name"], d)
        s["start_loc"] = best[0] if best else None
        s["start_loc_distance"] = round(best[1], 2) if best else None
    return rows


def main(argv=None):
    import argparse
    import json
    import sys

    import sc2reader

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("replay")
    ap.add_argument("--features", help="the map's features.json, to match spawns to StartLoc candidates")
    args = ap.parse_args(argv)
    rows = spawns(sc2reader.load_replay(args.replay, load_level=4))
    if args.features:
        starts = json.load(open(args.features, encoding="utf-8"))["start_locations"]
        rows = match_to_candidates(rows, starts)
    print(json.dumps(rows, indent=1))
    return 0 if rows else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
