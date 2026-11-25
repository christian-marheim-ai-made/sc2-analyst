#!/usr/bin/env python3
"""W11 · The benchmark packets: the games behind the frontier's analyses, the same tasks as the local model.

    .venv/Scripts/python.exe tools/make_frontier_packets.py [--fights 2]

The three games of docs/corpus/frontier-coaching-analyses.md and archive/reference/examples/: for each,
a T-fight packet for its biggest fights. Packets go to <data home>/frontier/packets/;
the workbench's Frontier screen lists them (Copy for ChatGPT, then Import the reply). Writes the
review page docs/review/W11-frontier.md.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from sc2analyst import adapter_sc2 as A, frontier as FR, library as L, pick, tasks as TK, workbench_vm as V  # noqa: E402

GAMES = [("White Chamber", "2026-05-18 17:21"), ("Baalpeak LE", "2026-05-19 14:06"), ("White Chamber", "2026-05-12 13:59")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fights", type=int, default=2)
    a = ap.parse_args()
    lines = ["# W11 · The frontier packets — the same tasks on ChatGPT Pro, by your paste", "",
             "**PROVENANCE: MADE by `tools/make_frontier_packets.py` · STATUS: OWED BY THE OWNER — paste each "
             "packet into ChatGPT Pro and its reply back · plan: `docs/plan/PLAN-workbench.md` W11**", "",
             "How: run `tools/workbench.py`, open **Model → The ChatGPT packets** (`#/frontier`), press "
             "*Copy for ChatGPT*, paste into ChatGPT Pro, paste its whole reply into the box, press *Import the "
             "reply*. The same checks run on it as on the local model, and **Grade** shows both blind. Nothing is "
             "sent by the tool. Once the card is back, the same fights run locally (W10's night) for the "
             "side-by-side in **Compare**.", "",
             "| game | fight | when | value | packet |", "|---|---|---|---|---|"]
    for m, s in GAMES:
        path, g = pick.find(m, s)
        key = V.game_key(g["map"], g["start_time"])
        fights = sorted(V.fight_rows(g), key=lambda f: -f["value"])[:a.fights]
        for f in sorted(fights, key=lambda f: f["id"]):
            pk = FR.packet(TK.TASKS["T-fight"], {"game": g, "fight": f["id"], "key": key},
                           label="%s fight %d" % (key, f["id"]))
            lines.append("| [%s](http://127.0.0.1:8798/#/game/%s) | %d | %s | %d | `%s` |" % (
                key, key, f["id"], f["clock"], f["value"], pk["packet_id"]))
            print(key, f["id"], pk["packet_id"], len(pk["text"]), "chars")
    lines.append("")
    (REPO / "docs/review/W11-frontier.md").write_text("\n".join(lines), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
