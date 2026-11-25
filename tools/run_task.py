#!/usr/bin/env python3
"""W9 · One task on the model, from the command line: the workbench's run without the page.

    .venv/Scripts/python.exe tools/run_task.py T-fight --game Sandstorm_CE_2026-09-23_1607 --fight 21
                                               [--backend llamacpp|mock]

Starts llm-bootstrap's server (llamacpp: the card, after checking it has room; mock: the stand-in),
submits the task, waits for the answer, checks it, records it beside the bootstrap's own request
record, prints the card before and after, and stops the server in every case.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import workbench as W  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("--game", required=True)
    ap.add_argument("--fight", type=int)
    ap.add_argument("--pid", type=int)
    ap.add_argument("--t", type=float)
    ap.add_argument("--backend", choices=("llamacpp", "mock"), default="llamacpp")
    a = ap.parse_args()
    wb = W.Workbench()
    before = W.card_free_mib()
    print("card free before: %s MiB (need %d)" % (before, W.NEED_MIB), flush=True)
    try:
        st = wb.start_model(a.backend)
        if st.get("started") is False:
            print("NOT STARTED:", st["why"])
            return 2
        target = {k: v for k, v in (("fight", a.fight), ("pid", a.pid), ("t", a.t)) if v is not None}
        t0 = time.time()
        r = wb.start_run({"task": a.task, "game": a.game, "target": target})
        cid = r["correlation_id"]
        peak = before
        while wb.runs[cid].get("status") in ("queued", "held"):
            time.sleep(5)
            f = W.card_free_mib()
            peak = f if peak is None or (f is not None and f < peak) else peak
        rec = wb.run_record(cid)
        print(json.dumps({"correlation_id": cid, "status": rec["status"], "seconds": round(time.time() - t0, 1),
                          "usage": rec.get("usage"), "card_free_low": peak,
                          "kept": len(rec["verdicts"]["kept"]), "dropped": len(rec["verdicts"]["dropped"]),
                          "answer_dropped": rec["verdicts"]["answer_dropped"]}, indent=1))
    finally:
        wb.stop_model()
        time.sleep(3)
        print("card free after: %s MiB" % W.card_free_mib())
    return 0


if __name__ == "__main__":
    sys.exit(main())
