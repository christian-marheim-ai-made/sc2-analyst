#!/usr/bin/env python3
"""W6b · One question over a chosen set of the owner's games, through the aggregation seam.

    .venv/Scripts/python.exe tools/aggregate.py siege --last 30 [--map M] [--format 3v3]
                                                [--race Terran] [--vs Protoss] [--opening "FAST EXPAND"]
                                                [--review docs/review/W6b-siege-last30.md]

Writes the table to <data home>/aggregates/<repository>/<selection>/<version>/table.json
and, with --review, a review table (name-free: games by map and minute). The model reads an aggregate
only once `reviewed.json` sits beside its table (R-M).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from sc2analyst import aggregate as AG, library as L, pick, siege  # noqa: E402,F401  (siege registers itself)

OPENINGS = L.DATA_HOME / "derived" / "openings-v1.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repository", choices=sorted(AG.REPOSITORIES))
    ap.add_argument("--last", type=int, default=30)
    ap.add_argument("--map")
    ap.add_argument("--format")
    ap.add_argument("--race")
    ap.add_argument("--vs")
    ap.add_argument("--opening")
    ap.add_argument("--review", type=Path)
    a = ap.parse_args()
    openings = json.loads(OPENINGS.read_text(encoding="utf-8"))["classes"] if OPENINGS.exists() else {}
    picked = AG.select(pick.index(), map_name=a.map, fmt=a.format, race=a.race, vs=a.vs, opening=a.opening,
                       openings=openings)
    res = AG.run(a.repository, picked, last=a.last)
    sel = AG.slug(last=a.last, map=a.map, fmt=a.format, race=a.race, vs=a.vs, opening=a.opening)
    d = AG.write(res, sel)
    print("%s over %d games -> %s" % (a.repository, res["games"], d))
    if a.review:
        t = res["total"]
        rows = res["rows"]
        span = "%s → %s" % (rows[-1]["game"][-16:], rows[0]["game"][-16:]) if rows else "-"
        lines = ["# W6b · %s — the owner's last %d games" % (a.repository, a.last), "",
                 "**PROVENANCE: MEASURED by `tools/aggregate.py %s --last %d` · STATUS: FOR THE OWNER'S REVIEW — "
                 "and the measures are his to rule (proposed, built as the default) · seam: "
                 "`src/sc2analyst/aggregate.py`, repository `src/sc2analyst/%s.py` · table: `%s`**" % (
                     a.repository, a.last, a.repository, (d / "table.json").as_posix()), "",
                 "Games: %d (%s), %d with tanks." % (res["games"], span, t.get("games_with_tanks", 0)), "",
                 "## The sum", "",
                 L.md_table(["measure", "value"], [[k, v] for k, v in t.items()]), "",
                 "## Per game", ""]
        cols = ["tanks", "siege_ups", "fights_with_tanks", "sieged_at_start", "sieged_in_fight", "never_sieged",
                "lost_sieged", "lost_unsieged", "killed_sieged", "killed_unsieged"]
        lines += [L.md_table(["game", "race", "result"] + cols,
                             [[r["game"], r["race"], r["result"]] + [r.get(c) for c in cols] for r in rows]), "",
                  "## What it measures, and what it does not prove", "", res["about"], ""]
        a.review.write_text("\n".join(lines), encoding="utf-8")
        print("review:", a.review)
    return 0


if __name__ == "__main__":
    sys.exit(main())
