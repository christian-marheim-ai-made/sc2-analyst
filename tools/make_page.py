#!/usr/bin/env python3
"""Write the standalone page for one game.

    .venv/Scripts/python.exe tools/make_page.py --map "Old Sun Temple LE" --start "2026-07-16 09:14"
                                               [--out <file.html>] [--assessment <run folder>]

Default output: <data home>/pages/<map>_<date>.html. With --assessment, the folder an
assessment run wrote (tools/assess.py) fills the model slots.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from sc2analyst import library as L, page, pick  # noqa: E402


def default_out(game: dict) -> Path:
    stamp = game["start_time"][:16].replace(":", "").replace(" ", "_")
    return L.DATA_HOME / "pages" / ("%s_%s.html" % (game["map"].replace(" ", "_"), stamp))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", required=True)
    ap.add_argument("--start", required=True, help="start time prefix, e.g. 2026-07-16 09:14")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--assessment", type=Path)
    a = ap.parse_args()
    _, game = pick.find(a.map, a.start)
    assessment = report = None
    if a.assessment:
        f = a.assessment / "assessment.json"
        if f.exists():
            assessment = json.loads(f.read_text(encoding="utf-8"))
        f = a.assessment / "battle_report.json"
        if f.exists():
            report = json.loads(f.read_text(encoding="utf-8"))
    html_text, _ = page.page_for(game, assessment, report)
    out = a.out or default_out(game)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_text, encoding="utf-8")
    print("wrote", out, "%d KB" % (len(html_text.encode("utf-8")) // 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
