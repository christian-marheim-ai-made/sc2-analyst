#!/usr/bin/env python3
"""Run the whole pipeline on N games: payload -> brief -> model passes (checked) -> page.

    .venv/Scripts/python.exe tools/assess.py --pick 20 [--mock] [--out <folder>]
    .venv/Scripts/python.exe tools/assess.py --game "Sandstorm CE" "2026-09-23 16:07"

Phases, so the card loads each model once: A, the thinker for every game (team passes, the
deterministic aggregate, the final pass, the plot); B, the wordsmith for every game's battle report;
C, the pages. Every pass's answer is saved as it lands, and a rerun skips what exists - a stopped run
resumes. The card is checked before the start (free memory against the thinker's measured need) and
read after the end.

--pick N: the owner's games, spread over formats (3v3, 2v2, 1v1 in the library's proportions with at
least 4 of each that exists), most recent first, 5 minutes or longer.
"""
from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from sc2analyst import analyse, assess as A, brief as BR, library as L, llm, page, pick  # noqa: E402

OUT = L.DATA_HOME / "assessments"
# the bootstrap's own memory check (2026-10-26): weights 16,038 + cache 2,048 at 16k + slack 1,536
# = 19,623 MiB, and 256 kept free beside it. Checked here first so a full card is said before any
# server starts. (The card proof measured 16,837 actually used; the bootstrap's estimate governs.)
THINKER_NEED_MIB = 19623
MARGIN_MIB = 256


def card() -> dict:
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, check=True).stdout
    used, total = (int(x) for x in out.strip().splitlines()[0].split(","))
    return {"used": used, "total": total, "free": total - used}


def choose(n: int) -> list[tuple[str, str]]:
    by = collections.defaultdict(list)
    for _, g in L.iter_records():
        if g["end"] < 300 or L.owner_pid(g) is None:
            continue
        by[L.game_format(g)].append((g["start_time"], g["map"]))
    for v in by.values():
        v.sort(reverse=True)
    total = sum(len(v) for v in by.values())
    quota = {k: max(min(4, len(v)), round(n * len(v) / total)) for k, v in by.items()}
    while sum(quota.values()) > n:
        k = max(quota, key=lambda k: quota[k])
        quota[k] -= 1
    while sum(quota.values()) < n:
        k = max(by, key=lambda k: len(by[k]) - quota[k])
        quota[k] += 1
    chosen = []
    for k, q in quota.items():
        chosen += [(m, s[:16]) for s, m in by[k][:q]]
    return chosen


def folder(out: Path, game: dict) -> Path:
    return out / ("%s_%s" % (game["map"].replace(" ", "_"), game["start_time"][:16].replace(":", "").replace(" ", "_")))


def save(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _version(system: str, schema) -> str:
    """A pass's version (DL-32): its system prompt and schema, hashed - a changed prompt is a new version."""
    import hashlib
    return hashlib.sha256(json.dumps([system, schema], sort_keys=True).encode("utf-8")).hexdigest()[:8]


def ask_once(sess, path: Path, model: str, system: str, user: str, schema: dict, name: str, **kw) -> dict:
    """The saved answer if it exists, else ask and save (answer, usage, seconds, error)."""
    got = load(path)
    if got is not None:
        return got
    try:
        a = sess.ask(model, system, user, schema, name=name, **kw)
        rec = {"version": _version(system, schema),
               "json": a["json"], "text": a["text"] if a["json"] is None else None, "usage": a["usage"],
               "seconds": a["seconds"], "timings": {k: (a["timings"] or {}).get(k) for k in
                                                    ("prompt_per_second", "predicted_per_second")}}
    except llm.ModelFailed as exc:
        rec = {"json": None, "error": str(exc)}
    save(path, rec)
    return rec


def phase_a(sess, games, out: Path, model: str, log: list) -> None:
    for i, (m, s) in enumerate(games):
        _, g = pick.find(m, s)
        d = folder(out, g)
        d.mkdir(parents=True, exist_ok=True)
        p = analyse.analyse(g)
        b = BR.build(p)
        save(d / "brief.json", {"text": b["text"], "teams": b["teams"], "dropped": b["dropped"],
                                "tokens_estimate": b["tokens_estimate"]})
        t0 = time.time()
        kept, dropped, cannot = {}, {}, []
        for team in b["teams"]:
            r = ask_once(sess, d / ("team-%s.json" % team), model, A.SYSTEM, A.team_prompt(b, team),
                         A.TEAM_SCHEMA, "team-%s" % team, max_tokens=7000, reasoning_budget=3000)
            c = A.check_team_findings(r.get("json"), b["facts"], int(team))
            kept[team], dropped[team] = c["kept"], c["dropped"]
            cannot += c["cannot_answer"]
        save(d / "aggregate.json", {"kept": kept, "dropped": dropped, "cannot_answer": cannot})
        pov_name = next(x["name"] for x in p["players"] if x["pid"] == p["pov"])
        r = ask_once(sess, d / "final.json", model, A.SYSTEM, A.final_prompt(b, kept, pov_name),
                     A.FINAL_SCHEMA, "final", max_tokens=7000, reasoning_budget=3000)
        assessment, fdropped = A.check_final(r.get("json"), b["facts"])
        if assessment:
            for tab in assessment.values():
                tab["_meta"] = {"model": "Qwen3.6-27B, checked"}
            # the final pass's drops are counted once, for all three tabs, on the first
            assessment["strategic"]["dropped"] = fdropped
        save(d / "assessment.json", assessment or None)
        r = ask_once(sess, d / "plot.json", model, A.SYSTEM, A.plot_prompt(b), A.PLOT_SCHEMA, "plot",
                     max_tokens=5000, reasoning_budget=2000)
        plot, pdropped = A.check_plot(r.get("json"), b["facts"])
        save(d / "plot-checked.json", {"plot": plot, "dropped": pdropped})
        log.append({"game": "%s %s" % (m, s), "phase": "A", "seconds": round(time.time() - t0, 1),
                    "team_kept": {k: len(v) for k, v in kept.items()},
                    "team_dropped": {k: [x["reason"] for x in v] for k, v in dropped.items()},
                    "final_dropped": fdropped, "plot_beats": len(plot["beats"]) if plot else None})
        print("[A %d/%d] %s %s: kept %s, dropped %s, final dropped %s, %.0f s" % (
            i + 1, len(games), m, s, {k: len(v) for k, v in kept.items()},
            {k: len(v) for k, v in dropped.items()}, fdropped, time.time() - t0), flush=True)


def phase_b(sess, games, out: Path, model: str, log: list) -> None:
    for i, (m, s) in enumerate(games):
        _, g = pick.find(m, s)
        d = folder(out, g)
        pc = load(d / "plot-checked.json")
        if not pc or not pc.get("plot") or not pc["plot"]["beats"]:
            save(d / "battle_report.json", None)
            continue
        r = ask_once(sess, d / "prose.json", model, A.WORDSMITH_SYSTEM, A.prose_prompt(pc["plot"]),
                     A.PROSE_SCHEMA, "prose", max_tokens=1500, reasoning_budget=None)
        pars, dropped = A.check_prose(r.get("json"), pc["plot"])
        untold = A.untold_beats(pars, pc["plot"])
        save(d / "battle_report.json", {"title": pc["plot"]["title"], "paragraphs": pars, "dropped": dropped,
                                        "untold_beats": untold,
                                        "_meta": {"model": "plot: Qwen3.6-27B · words: Cydonia 24B"}}
             if pars else None)
        log.append({"game": "%s %s" % (m, s), "phase": "B", "paragraphs": len(pars), "dropped": dropped})
        print("[B %d/%d] %s %s: %d paragraphs, %s dropped" % (i + 1, len(games), m, s, len(pars), dropped), flush=True)


def phase_c(games, out: Path) -> list:
    pages = []
    for m, s in games:
        _, g = pick.find(m, s)
        d = folder(out, g)
        html_text, _ = page.page_for(g, load(d / "assessment.json"), load(d / "battle_report.json"))
        (d / "page.html").write_text(html_text, encoding="utf-8")
        pages.append(str(d / "page.html"))
    return pages


def run(games: list, out: Path = OUT, mock: bool = False, record: str = "run.json") -> dict:
    """The three phases over `games` [(map, start)]; -> the run record, saved as `record` in `out`
    (a one-game run names its own, so it never overwrites a batch's)."""
    out.mkdir(parents=True, exist_ok=True)
    save(out / record.replace("run", "games", 1), games)
    settings = llm.MOCK if mock else llm.REAL
    rec = {"games": games, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "log": []}
    if not mock:
        rec["card_before"] = card()
        if rec["card_before"]["free"] < THINKER_NEED_MIB + MARGIN_MIB:
            rec["blocked"] = "the card has %d MiB free; the thinker needs %d + %d" % (
                rec["card_before"]["free"], THINKER_NEED_MIB, MARGIN_MIB)
            save(out / record, rec)
            return rec
    t0 = time.time()
    # one workspace for every model call (W12): the workbench's Model screen and the model statistics
    # see these passes too; the stand-in keeps its own
    ws = (L.DATA_HOME / "llm-workspace") if not mock else (L.DATA_HOME / "llm-workspace" / "standin")
    with llm.Session(["thinker"], settings=settings, workspace=ws) as sess:
        rec["thinker_up_s"] = sess.log[-1]["s"] if sess.log else None
        phase_a(sess, games, out, "thinker", rec["log"])
        rec["phase_a_s"] = round(time.time() - t0, 1)
        sess.swap("thinker", "wordsmith")
        t1 = time.time()
        phase_b(sess, games, out, "wordsmith", rec["log"])
        rec["phase_b_s"] = round(time.time() - t1, 1)
        rec["session_log"] = sess.log
    rec["pages"] = phase_c(games, out)
    if not mock:
        time.sleep(3)
        rec["card_after"] = card()
    rec["total_s"] = round(time.time() - t0, 1)
    save(out / record, rec)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pick", type=int, default=0)
    ap.add_argument("--game", nargs=2, action="append", default=[])
    ap.add_argument("--mock", action="store_true", help="the bootstrap's stand-in: no card")
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args()
    games = [tuple(x) for x in a.game] + (choose(a.pick) if a.pick else [])
    rec = run(games, a.out, a.mock)
    if rec.get("blocked"):
        print("BLOCKED:", rec["blocked"])
        return 2
    print("done in %.0f s; pages:" % rec["total_s"])
    for p in rec["pages"]:
        print("  " + p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
