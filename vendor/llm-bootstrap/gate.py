"""The gate: everything that must hold before a change is committed, in one run.

    python gate.py [--out <folder>] [--only queue,runner] [--no-suites]

The checks that read the tree:

  * no file is CRLF - every file here is LF (`.gitattributes`), and a file
    written with `Path.write_text` on Windows turns CRLF without a word;
  * every Python file compiles with warnings as errors - Python 3.14 warns of
    a `return` inside a `finally`, which swallows whatever was raised;
  * every import is the standard library's or this repository's own - no
    part installs a package to run or to test;
  * the client's committed page and kit are what its parts build.

Then every suite, one after another, each one's whole output kept in the
`--out` folder (a new temporary folder by default - never this tree). A suite
fails the gate when it exits non-zero, prints no tally of its own ("P passed,
F failed, T total" - never unittest's "Ran N tests"), its tally says a row
failed, or it prints a ResourceWarning: a warning nobody reads is a leak
nobody fixes.

A suite that needs the real card skips unless it is asked for, and the gate
never asks.
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
import tempfile
import time
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PARTS = ("llm-bootstrap-contract", "llm-bootstrap-coordinator", "llm-bootstrap-server",
         "llm-bootstrap-client")
SUITES = [
    ("contract", "llm-bootstrap-contract", "test_contract.py"),
    ("coordinator", "llm-bootstrap-coordinator", "test_coordinator.py"),
    ("queue", "llm-bootstrap-server", "test_queue.py"),
    ("server", "llm-bootstrap-server", "test_server.py"),
    ("runner", "llm-bootstrap-server", "test_runner.py"),
    ("stats", "llm-bootstrap-server", "test_stats.py"),
    ("llamacpp", "llm-bootstrap-server", "test_llamacpp.py"),
    ("whisper", "llm-bootstrap-server", "test_whisper.py"),
    ("adapter", "llm-bootstrap-server", "test_adapter.py"),
    ("coordination", "llm-bootstrap-server", "test_coordination.py"),
    ("sessions", "llm-bootstrap-server", "test_sessions.py"),
    ("client", "llm-bootstrap-client", "test_client.py"),
    ("real_card", "llm-bootstrap-server", "test_real_card.py"),
]
#: a suite's last words, its own tally: "N passed, M failed, T total" - every
#: suite prints one, a unittest suite through `_suites.report`
TALLY = re.compile(r"(\d+) passed, (\d+) failed, (\d+) total")
TEXT = {".py", ".js", ".json", ".md", ".html", ".css", ".toml", ".txt", ".gitattributes",
        ".gitignore"}


def files() -> list:
    out = []
    for p in sorted(ROOT.rglob("*")):
        if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts:
            out.append(p)
    return out


def no_crlf(all_files) -> list:
    return [str(p.relative_to(ROOT)) for p in all_files
            if (p.suffix in TEXT or p.name in TEXT) and b"\r\n" in p.read_bytes()]


def compiles_cleanly(py) -> list:
    bad = []
    for p in py:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            try:
                compile(p.read_bytes().decode("utf-8"), str(p), "exec")
            except (SyntaxError, Warning) as e:
                bad.append(f"{p.relative_to(ROOT)}: {type(e).__name__}: {e}")
    return bad


def imports_are_ours_or_the_standard_librarys(py) -> list:
    ours = {p.stem for p in py} | {p.parent.name for p in py if p.name == "__init__.py"}
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    bad = []
    for p in py:
        tree = ast.parse(p.read_bytes().decode("utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                if top not in stdlib and top not in ours:
                    bad.append(f"{p.relative_to(ROOT)}: imports {name}")
    return bad


def page_is_built() -> list:
    build = ROOT / "llm-bootstrap-client" / "build.py"
    if not build.exists():
        return []
    r = subprocess.run([sys.executable, str(build), "--check"], capture_output=True, text=True)
    return [] if r.returncode == 0 else [r.stdout.strip() or r.stderr.strip()]


def tally_of(text: str):
    """A suite's own tally - the last "P passed, F failed, T total" it printed
    - as (passed, failed, total); None when it printed none. Never unittest's
    own count ("Ran N tests"): unittest prints that after the tally, and it
    leaves out every row of a class that could not be set up."""
    found = TALLY.findall(text)
    return tuple(int(x) for x in found[-1]) if found else None


def run_suite(name, part, script, out: Path) -> dict:
    env = dict(os.environ)
    env.pop("LLM_BOOTSTRAP_REAL_CARD", None)                # the gate never asks for the card
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    t0 = time.monotonic()
    try:
        p = subprocess.run([sys.executable, script], cwd=ROOT / part, env=env,
                           capture_output=True, timeout=900)
        code, text = p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")
    except subprocess.TimeoutExpired as e:
        code = "timed out"
        text = ((e.stdout or b"") + (e.stderr or b"")).decode("utf-8", "replace")
    (out / f"out-{name}.txt").write_bytes(text.encode("utf-8"))
    counted = tally_of(text)
    tally = f"{counted[0]}/{counted[2]}" if counted else None
    problems = []
    if code != 0:
        problems.append(f"exit {code}")
    if counted is None:
        problems.append("no tally")
    elif counted[1]:
        problems.append(f"its tally says {counted[1]} failed")
    # one warning, not its hint: "ResourceWarning: Enable tracemalloc ..."
    warned = len(re.findall(r"ResourceWarning: (?!Enable tracemalloc)", text))
    if warned:
        problems.append(f"{warned} ResourceWarning(s)")
    skipped = re.search(r"OK \(skipped=(\d+)\)", text)
    return {"name": name, "tally": tally, "s": time.monotonic() - t0, "problems": problems,
            "skipped": int(skipped.group(1)) if skipped else 0}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="everything that must hold before a commit")
    ap.add_argument("--out", help="where each suite's whole output is kept")
    ap.add_argument("--only", default="", help="suites to run, by name, comma-separated")
    ap.add_argument("--no-suites", action="store_true", help="the tree checks alone")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    all_files = files()
    py = [p for p in all_files if p.suffix == ".py"]
    failed = 0
    for what, problems in (("the client's page and kit are what its parts build", page_is_built()),
                           ("no file is CRLF", no_crlf(all_files)),
                           ("every Python file compiles with warnings as errors",
                            compiles_cleanly(py)),
                           ("every import is the standard library's or ours",
                            imports_are_ours_or_the_standard_librarys(py))):
        print(f"{'PASS' if not problems else 'FAIL'}  {what}")
        for line in problems:
            print(f"        {line}")
        failed += bool(problems)
    if a.no_suites:
        return 1 if failed else 0
    out = Path(a.out) if a.out else Path(tempfile.mkdtemp(prefix="llmb-gate-"))
    out.mkdir(parents=True, exist_ok=True)
    only = {s for s in a.only.split(",") if s}
    for name, part, script in SUITES:
        if only and name not in only:
            continue
        r = run_suite(name, part, script, out)
        failed += bool(r["problems"])
        said = ", ".join(r["problems"]) or (f"skipped {r['skipped']}" if r["skipped"] else "")
        print(f"{'PASS' if not r['problems'] else 'FAIL'}  {name:13} {str(r['tally']):>12}  "
              f"{r['s']:6.1f}s  {said}", flush=True)
    print(f"\nevery suite's whole output: {out}")
    print("GATE PASSED" if not failed else f"GATE FAILED ({failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
