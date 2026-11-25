#!/usr/bin/env python3
"""The owner's library baselines: every metric of `sc2analyst.baselines`, sorted, per race and format.

    .venv/Scripts/python.exe tools/make_baselines.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from sc2analyst import analyse, baselines, library as L  # noqa: E402


def main() -> int:
    games = [g for _, g in L.iter_records()]
    groups = baselines.build(games, analyse.build_times())
    baselines.FILE.write_text(json.dumps({
        "provenance": "MEASURED by tools/make_baselines.py over %d game records, 2026-10-26; the "
                      "owner's player-games of 3 minutes or more; each list sorted" % len(games),
        "metrics": {k: v[0] for k, v in baselines.METRICS.items()},
        "groups": groups}, separators=(",", ":")), encoding="utf-8")
    print("wrote", baselines.FILE, {k: len(v.get("killed_lost_ratio", [])) for k, v in groups.items()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
