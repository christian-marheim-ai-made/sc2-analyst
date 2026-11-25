#!/usr/bin/env python3
"""W10/W11 · A batch on the card from the command line: the workbench's Plan tonight without the page.

    .venv/Scripts/python.exe tools/run_batch.py [--benchmark] [--set20] [--fights 5] [--backend llamacpp|mock]

--benchmark: T-fight on the fights of W11's six frontier packets (the same facts ChatGPT gets);
--set20:     T-fight on each game's biggest --fights fights over the 20-game set (W10's night).
Starts llm-bootstrap's server, queues every task as batch work (one model, one answer at a time),
lands and checks each as it comes, prints one line per landing, stops the server in every case.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import workbench as W  # noqa: E402


def _below_normal(pid: int):
    """The owner's machine freezes under load (2026-10-29, during the night): every process of the batch
    runs below normal, so his own programs come first."""
    if sys.platform != "win32":
        return
    import ctypes
    h = ctypes.windll.kernel32.OpenProcess(0x0200 | 0x0400, False, pid)   # SET_INFORMATION | QUERY
    if h:
        ctypes.windll.kernel32.SetPriorityClass(h, 0x4000)                # BELOW_NORMAL
        ctypes.windll.kernel32.CloseHandle(h)


def _children_below_normal():
    """Every descendant of this process (the bootstrap server, llama-server), found by parent id."""
    if sys.platform != "win32":
        return
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId | ConvertTo-Csv -NoTypeInformation"],
                         capture_output=True, text=True, timeout=60).stdout
    kids = {}
    for line in out.splitlines()[1:]:
        try:
            pid, ppid = (int(x.strip('"')) for x in line.split(","))
        except ValueError:
            continue
        kids.setdefault(ppid, []).append(pid)
    todo, seen = [os.getpid()], set()
    while todo:
        p = todo.pop()
        for c in kids.get(p, []):
            if c not in seen:
                seen.add(c)
                _below_normal(c)
                todo.append(c)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--benchmark", action="store_true")
    ap.add_argument("--set20", action="store_true")
    ap.add_argument("--fights", type=int, default=5)
    ap.add_argument("--backend", choices=("llamacpp", "mock"), default="llamacpp")
    ap.add_argument("--resume", action="store_true", help="only land what an earlier launch left queued")
    ap.add_argument("--repeat", type=int, default=1, help="ask each task this many times (W10's n = 3 measurement)")
    a = ap.parse_args()
    _below_normal(os.getpid())
    wb = W.Workbench()
    targets = []
    if a.benchmark:
        for pk in wb.packets():
            if pk["task"] == "T-fight" and pk.get("game_key"):
                rec = json.loads((W.FRONTIER_PACKETS / ("%s.json" % pk["packet_id"])).read_text(encoding="utf-8"))
                targets.append((pk["game_key"], {"fight": rec["target"]["fight"]}))
    if a.set20:
        targets += wb.batch_targets({"task": "T-fight", "set": "20", "fights_per_game": a.fights})
    print("tasks: %d · card free %s MiB" % (len(targets), W.card_free_mib()), flush=True)
    t0 = time.time()
    try:
        st = wb.start_model(a.backend)
        if st.get("started") is False:
            print("NOT STARTED:", st["why"])
            return 2
        _children_below_normal()
        cids = [cid for cid, r in wb.runs.items() if r.get("status") in ("queued", "held")]   # resumed
        print("resumed from an earlier launch: %d" % len(cids), flush=True)
        for key, target in ([] if a.resume else [t for t in targets for _ in range(a.repeat)]):
            cids.append(wb.start_run({"task": "T-fight", "game": key, "target": target, "batch_at": time.time()})["correlation_id"])
        done = set()
        while len(done) < len(cids):
            time.sleep(10)
            _children_below_normal()           # llama-server starts only when the first job loads it
            for cid in cids:
                r = wb.runs.get(cid, {})
                if cid not in done and r.get("status") not in (None, "queued", "held"):
                    done.add(cid)
                    v = r.get("verdicts") or {}
                    print("%3d/%d %s %s kept %d dropped %d%s · %.0f s in" % (
                        len(done), len(cids), r.get("status"), (r.get("target") or {}).get("label", "")[:60],
                        len(v.get("kept", [])), len(v.get("dropped", [])),
                        " · answer dropped: %s" % v["answer_dropped"] if v.get("answer_dropped") else "",
                        time.time() - t0), flush=True)
    finally:
        wb.stop_model()
        time.sleep(3)
        print("done in %.0f s · card free after %s MiB" % (time.time() - t0, W.card_free_mib()), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
