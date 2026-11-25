"""Break the rules on purpose, one at a time, and see which rows notice.

    python mutate.py <mutants.py> [--jobs 4] [--controls 3] [--only M01,M02] [--out <folder>]

A mutants file is plain Python:

    MUTANTS = [
        # (id, the rule it breaks, file, old text, new text, suites to run)
        ("M01", "a refusal is never a success", "llm-bootstrap-server/server.py",
         "return 409, {", "return 200, {", ("server",)),
    ]

`old` must occur exactly once in its file; a tuple of olds and a tuple of news
make several edits in one mutant (a rule broken by MOVING a line is two
edits). Suites are named as `gate.py` names them. Write the list BEFORE the
run, checking each mutant against the rows as they stand: a mutant you cannot
name a row for is a row you have not written.

Each mutant runs in its own copy of the whole tree, outside it. The tree is
never touched, and the run checks that it was not.

    KILLED      a row went red: the rule is pinned
    SURVIVED    every row stayed green: no row can tell the rule is broken - write one
    TIMEOUT     a suite never finished: a row waits without a bound - bound it
    NO VERDICT  the suite printed no tally, or ran a different number of rows
                than the controls did

CONTROLS are copies with no mutant that run every suite. They must be green
and agree on every suite's row count, or no verdict can be trusted. A
SELF-CHECK copy, one suite of which cannot even start, is added to every run:
it must come back NO VERDICT, never SURVIVED - a harness that calls a crash a
survivor, or a kill, is not measuring anything.

A red control is not noise to re-run away: it is a row that failed on
untouched code. Read it.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import gate                                                     # noqa: E402

SUITES = {name: (part, script) for name, part, script in gate.SUITES if name != "real_card"}
UNITTEST = ("queue", "runner", "stats", "llamacpp", "whisper", "adapter", "coordination",
            "sessions")
FAILED_ROW = re.compile(r"^(?:FAIL|ERROR): (\S+) \((?:__main__\.)?([\w.]+)\)", re.M)
FAILED_CHECK = re.compile(r"^FAIL  (.+?)(?:\s{3,}|$)", re.M)
TALLY = re.compile(r"(\d+) passed, (\d+) failed, (\d+) total")
SELF_CHECK = "MX"


def sources() -> dict:
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in gate.files()}


def prepare(out: Path, tag: str, rel=None, old=None, new=None) -> Path:
    base = out / tag
    if base.exists():
        shutil.rmtree(base)
    shutil.copytree(ROOT, base, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    if rel:
        f = base / rel
        text = f.read_bytes().decode("utf-8")
        olds = old if isinstance(old, tuple) else (old,)
        news = new if isinstance(new, tuple) else (new,)
        for o, n in zip(olds, news, strict=True):
            count = text.count(o)
            if count != 1:
                raise SystemExit(f"{tag}: its text occurs {count} times in {rel}, not once:\n{o}")
            text = text.replace(o, n)
        f.write_bytes(text.encode("utf-8"))
    return base


def run_suite(base: Path, suite: str, timeout: float) -> dict:
    """Run one suite of a copy. By its script's whole path, so the suite's
    command line names the copy - and each suite starts its children by whole
    paths too: that is what `reap` finds them by when the suite is timed out
    and killed, and its children are not (a relative script path named
    nothing, and an orphaned coordinator outlived its run)."""
    part, script = SUITES[suite]
    t0 = time.monotonic()
    try:
        p = subprocess.run([sys.executable, str(base / part / script)], cwd=base / part,
                           capture_output=True, timeout=timeout, encoding="utf-8",
                           errors="replace")
        out, rc, timed_out = p.stdout + p.stderr, p.returncode, False
    except subprocess.TimeoutExpired as e:
        # what a timed-out run printed is str, bytes or None
        out = "".join(x.decode("utf-8", "replace") if isinstance(x, bytes) else (x or "")
                      for x in (e.stdout, e.stderr))
        rc, timed_out = None, True
    (base / f"out-{suite}.txt").write_bytes(out.encode("utf-8"))
    m = TALLY.findall(out)
    ran = re.findall(r"^Ran (\d+) tests?", out, re.M)
    passed = failed = total = None
    if m:
        passed, failed, total = (int(x) for x in m[-1])
    elif ran and suite in UNITTEST:
        total = int(ran[-1])
        bad = re.search(r"FAILED \((?:failures=(\d+))?(?:, )?(?:errors=(\d+))?", out)
        failed = sum(int(x or 0) for x in bad.groups()) if bad else 0
        passed = total - failed
    if suite in UNITTEST:
        failing = sorted({f"{cls.split('.')[-2] if '.' in cls else cls}.{name}"
                          for name, cls in FAILED_ROW.findall(out)})
    else:
        failing = sorted({n.strip() for n in FAILED_CHECK.findall(out)})
    return {"rc": rc, "timed_out": timed_out, "wall_s": round(time.monotonic() - t0, 1),
            "passed": passed, "failed": failed, "total": total, "failing": failing}


def suite_verdict(r, want_total, is_mutant) -> str:
    if r["timed_out"]:
        return "TIMEOUT"
    if r["total"] is None:
        return "NO VERDICT - no tally (the suite crashed, or printed nothing)"
    if r["failed"]:
        short = "" if want_total in (None, r["total"]) else \
            f" (ran {r['total']} of the controls' {want_total})"
        return f"{r['failed']} red{short}"
    if want_total is not None and r["total"] != want_total:
        return f"NO VERDICT - ran {r['total']} rows, the controls ran {want_total}"
    if r["rc"] != 0:
        return f"NO VERDICT - 0 failed but exit code {r['rc']}"
    return "SURVIVED" if is_mutant else "green"


def verdict(res, want) -> str:
    is_mutant = not res["control"]
    per = {s: suite_verdict(v, want.get(s), is_mutant) for s, v in res["suites"].items()}
    res["per_suite"] = per
    red = [f"{s} {v}" for s, v in per.items() if re.match(r"\d+ red", v)]
    if red:
        return "KILLED (" + ", ".join(red) + ")"
    if any(v == "TIMEOUT" for v in per.values()):
        return "TIMEOUT"
    unread = [v for v in per.values() if v.startswith("NO VERDICT")]
    if unread:
        return unread[0]
    return "SURVIVED" if is_mutant else "green"


#: how often the process list is asked for, in all, before the reap says it
#: could not look (H4's R4: one query that did not answer was read as
#: "nothing to reap"), and the pause between
REAP_ASKS = 3
REAP_PAUSE_S = 2.0


def process_listing():
    """Every process's "pid<TAB>command line", one a line - or None: the
    operating system did not answer. A listing always holds this process, so
    an empty one is no answer either."""
    if sys.platform == "win32":
        query = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine } | "
                 "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
        try:
            got = subprocess.run(["powershell", "-NoProfile", "-Command", query],
                                 capture_output=True, text=True, timeout=120)
        except Exception:                                   # noqa: BLE001
            return None
        return got.stdout if got.returncode == 0 and got.stdout.strip() else None
    listing = ""
    for d in Path("/proc").glob("[0-9]*"):
        try:
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        listing += f"{d.name}\t{cmd}\n"
    return listing or None


def reap(out: Path, listing=None, end=None):
    """A suite that dies runs no cleanup, and a mutant can break what a
    cleanup finds its processes by - so once every run is over, any process
    whose command line names this run's folder is ended. By command line,
    never by time - and never this run itself, nor what started it: the run's
    own command line names the folder too (found by building: it ended
    itself, verdicts unprinted). -> the pids ended - or None: the process
    list could not be read, asked REAP_ASKS times, so what outlived the run
    is NOT known - never read as "nothing to reap". `listing` and `end` are
    this machine's own unless a row hands in its stand-ins."""
    listing = listing or process_listing
    needle = str(out).lower()
    # each end reads the command line again, as it ends it (H6's register 4)
    end = end or (lambda pid: _end(pid, needle))
    spared = {os.getpid(), os.getppid()}
    for ask in range(REAP_ASKS):
        got = listing()
        if got is not None:
            break
        if ask + 1 < REAP_ASKS:
            time.sleep(REAP_PAUSE_S)
    else:
        return None
    ended = []
    for line in got.splitlines():
        pid, _, cmd = line.partition("\t")
        if pid.strip().isdigit() and needle in cmd.lower() and int(pid) not in spared \
                and "mutate.py" not in cmd.lower():
            if end(pid.strip()) is not False:           # an end that did not end says False
                ended.append(int(pid))
    return ended


def _end(pid: str, needle: str) -> bool:
    """End process *pid*, found by its command line a moment ago - only if
    its command line, read AGAIN as it is ended, still names *needle*: a pid
    is handed on the moment its process ends, so between the listing and the
    end it may be somebody else's (H6's register 4). On Windows the line is
    read through a handle held across the end - while it is open the pid
    cannot be handed on - and the end waited for on it. -> whether it was
    ended."""
    if sys.platform != "win32":
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
        except OSError:
            return False                                    # gone meanwhile
        if needle not in cmd.decode("utf-8", "replace").lower():
            return False
        subprocess.run(["kill", "-9", pid], capture_output=True, timeout=30)
        return True
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    nt = ctypes.WinDLL("ntdll")
    nt.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
    nt.NtQueryInformationProcess.restype = ctypes.c_long
    # PROCESS_TERMINATE | SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
    handle = k32.OpenProcess(0x0001 | 0x00100000 | 0x1000, False, int(pid))
    if not handle:
        return False                                        # gone, or none a sweep may end
    try:
        size = wintypes.ULONG(0)
        nt.NtQueryInformationProcess(handle, 60, None, 0, ctypes.byref(size))  # its command line
        if not size.value:
            return False
        buf = ctypes.create_string_buffer(size.value)
        if nt.NtQueryInformationProcess(handle, 60, buf, size.value, ctypes.byref(size)) != 0:
            return False
        class UnicodeString(ctypes.Structure):
            _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort),
                        ("Buffer", ctypes.c_void_p)]
        text = UnicodeString.from_buffer(buf)
        line = ctypes.wstring_at(text.Buffer, text.Length // 2) if text.Buffer else ""
        if needle not in line.lower():
            return False
        k32.TerminateProcess(handle, 1)
        return k32.WaitForSingleObject(handle, 30_000) == 0
    finally:
        k32.CloseHandle(handle)


def load_mutants(path: Path) -> list:
    spec = importlib.util.spec_from_file_location("mutants", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return list(module.MUTANTS)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="break the rules on purpose, one at a time")
    ap.add_argument("mutants", help="a Python file defining MUTANTS")
    ap.add_argument("--only", default="", help="mutant ids to run, comma-separated")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--controls", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=300.0, help="seconds per suite")
    ap.add_argument("--out", help="where the copies and the results go (a new temporary "
                                  "folder by default - never this tree)")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if a.controls < 1:
        raise SystemExit("at least one control: without it no verdict can be trusted")
    out = Path(a.out) if a.out else Path(tempfile.mkdtemp(prefix="llmb-mutate-"))
    out.mkdir(parents=True, exist_ok=True)
    before = sources()
    only = {s.strip() for s in a.only.split(",") if s.strip()}
    todo = [m for m in load_mutants(Path(a.mutants)) if not only or m[0] in only]
    seen = set()
    for tag, _, rel, old, new, suites in todo:
        if tag in seen or tag == SELF_CHECK or re.fullmatch(r"C[0-9]+", tag):
            raise SystemExit(f"{tag}: ids are unique, and {SELF_CHECK} and C<n> are the run's own")
        unknown = set(suites) - set(SUITES)
        if unknown:
            raise SystemExit(f"{tag}: no suite called {sorted(unknown)} (they are {sorted(SUITES)})")
        seen.add(tag)
        prepare(out, tag, rel, old, new)
    first = todo[0][5][0] if todo else "server"
    part, script = SUITES[first]
    # the whole script, not a line of it: a line put after the first could land
    # inside a docstring and do nothing - a self-check that cannot fail
    (prepare(out, SELF_CHECK) / part / script).write_bytes(
        b"raise SystemExit('the self-check copy cannot run')\n")
    runs = [{"tag": t, "rule": r, "suites": tuple(s), "control": False}
            for t, r, _, _, _, s in todo]
    runs.append({"tag": SELF_CHECK, "rule": "the harness's own check - must be NO VERDICT",
                 "suites": (first,), "control": False})
    controls = [f"C{i}" for i in range(1, a.controls + 1)]
    for i, c in enumerate(controls):
        prepare(out, c)
        runs.insert(min(len(runs), (i * len(runs)) // max(1, len(controls))),
                    {"tag": c, "rule": "control - no mutant", "suites": tuple(SUITES),
                     "control": True})
    results = {}

    def go(run):
        base = out / run["tag"]
        return {**run, "suites": {s: run_suite(base, s, a.timeout) for s in run["suites"]}}

    with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        for f in cf.as_completed([ex.submit(go, r) for r in runs]):
            r = f.result()
            results[r["tag"]] = r
            tally = "  ".join(f"{s}: " + ("timed out" if v["timed_out"] else "no tally"
                                          if v["total"] is None else f"{v['passed']}/{v['total']}")
                              for s, v in r["suites"].items())
            print(f"  ran {r['tag']:5} {tally}", flush=True)
    want = {}
    for s in SUITES:
        totals = {results[c]["suites"][s]["total"] for c in controls}
        want[s] = totals.pop() if len(totals) == 1 else None
    sound = all(want[s] is not None for s in SUITES) and \
        all(verdict(results[c], want) == "green" for c in controls)
    print()
    for tag in sorted(results):
        r = results[tag]
        r["verdict"] = verdict(r, want)
        print(f"{tag:5} {r['verdict']:<52} {r['rule']}", flush=True)
    check = results[SELF_CHECK]["verdict"]
    if not check.startswith("NO VERDICT"):
        sound = False
        print(f"\n!! THE SELF-CHECK CAME BACK {check} - this harness cannot be trusted")
    if not sound:
        print(f"\n!! CONTROLS NOT GREEN OR NOT AGREED ({want}) - every verdict above is suspect")
    ended = reap(out)
    if ended is None:
        sound = False
        print(f"\n!! the process list could not be read, asked {REAP_ASKS} times: a process "
              f"a run started may still be running - find it by its command line, which "
              f"names {out}")
    elif ended:
        print(f"\n!! {len(ended)} process(es) outlived their run and were ended: {ended}")
    after = sources()
    (out / "results.json").write_bytes(json.dumps(
        {"sources_untouched": before == after, "want": want, "reaped": ended,
         "controls_sound": sound, "results": results}, indent=1).encode("utf-8"))
    print(f"\nthe tree untouched: {before == after}")
    print(f"copies, every suite's output and results.json: {out}")
    return 0 if sound and before == after else 1


if __name__ == "__main__":
    raise SystemExit(main())
