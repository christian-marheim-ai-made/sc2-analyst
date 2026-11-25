#!/usr/bin/env python3
"""Every anonymised twin -> its game record (stage 1), cached on the data home.

    .venv/Scripts/python.exe tools/extract_library.py [--twins <folder>] [--out <folder>]
                                                     [--jobs 4] [--limit N] [--force]

Idempotent: a twin whose record exists is skipped unless --force. Workers run at below-normal
priority, four by default (the owner's machine freezes under full-core builds). Records are
gzip JSON named after the twin; they hold the twin's aliases and the owner's handle, so they stay
on L: and never enter git. A failure is written to `_errors.json` beside them, by twin name.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import sys
import time
import warnings
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from sc2analyst import adapter_sc2, library as L  # noqa: E402

TWINS = L.DATA_HOME / "replays"
OUT = L.DATA_HOME / "derived" / adapter_sc2.ADAPTER_VERSION


def _below_normal():
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)


def _one(args):
    twin, dest = args
    warnings.filterwarnings("ignore")
    t0 = time.time()
    try:
        rec = adapter_sc2.load_game(twin)
        adapter_sc2.save(rec, dest)
        return twin.name, None, round(time.time() - t0, 2)
    except Exception as exc:  # recorded per twin, never fatal to the batch
        return twin.name, "%s: %s" % (type(exc).__name__, str(exc)[:300]), round(time.time() - t0, 2)


def record_path(out: Path, twin: Path) -> Path:
    return out / (twin.stem + ".json.gz")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--twins", type=Path, default=TWINS)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    twins = sorted(a.twins.glob("*.SC2Replay"))
    if a.limit:
        twins = twins[:a.limit]
    todo = [(t, record_path(a.out, t)) for t in twins
            if a.force or not record_path(a.out, t).exists()]
    print("twins %d, to extract %d -> %s" % (len(twins), len(todo), a.out), flush=True)
    errors_file = a.out / "_errors.json"
    errors = json.loads(errors_file.read_text(encoding="utf-8")) if errors_file.exists() else {}
    t0 = time.time()
    done = 0
    with mp.Pool(a.jobs, initializer=_below_normal) as pool:
        for name, err, secs in pool.imap_unordered(_one, todo, chunksize=2):
            done += 1
            if err:
                errors[name] = err
            else:
                errors.pop(name, None)
            if done % 50 == 0 or done == len(todo):
                print("%d/%d  %.0f s" % (done, len(todo), time.time() - t0), flush=True)
    errors_file.write_text(json.dumps(errors, indent=1, sort_keys=True), encoding="utf-8")
    print("errors: %d" % len(errors))
    return 0


if __name__ == "__main__":
    sys.exit(main())
