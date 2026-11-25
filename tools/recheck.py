#!/usr/bin/env python3
"""W8 · Re-check every landed task run with the checks as they are now - offline, no model (DL-30).

    .venv/Scripts/python.exe tools/recheck.py [--records <folder>] [--write]

Prints, per task and version, how many claims are kept and dropped now against when they landed.
--write stores the new verdicts in each record (the old ones kept under `verdicts_before`).
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from sc2analyst import runs as R  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", type=Path, default=R.RECORDS)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    tally = collections.defaultdict(lambda: [0, 0, 0, 0])
    for p in sorted(a.records.glob("c-*.json")):
        rec = json.loads(p.read_text(encoding="utf-8"))
        now = R.recheck(rec)
        t = tally[(rec["task"], rec["version"])]
        t[0] += len(rec["verdicts"]["kept"])
        t[1] += len(rec["verdicts"]["dropped"])
        t[2] += len(now["kept"])
        t[3] += len(now["dropped"])
        if a.write:
            rec["verdicts_before"], rec["verdicts"] = rec["verdicts"], now
            p.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    for (task, ver), (k0, d0, k1, d1) in sorted(tally.items()):
        print("%-10s %s  kept %d -> %d   dropped %d -> %d" % (task, ver, k0, k1, d0, d1))
    if not tally:
        print("no landed runs in", a.records)
    return 0


if __name__ == "__main__":
    sys.exit(main())
