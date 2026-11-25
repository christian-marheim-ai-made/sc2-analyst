#!/usr/bin/env python3
"""W4 · The openings over the library: thresholds measured, frozen, every player classed (DL-26).

    .venv/Scripts/python.exe tools/measure_openings.py

Writes:
  data/measured/openings.json                   the thresholds (in git): per race and format, the
                                                quantiles of the first expansion's start among the
                                                players who expanded before cut_s; FAST EXPAND = p33
  <data home>/derived/openings-v1.json   every player's class, by record (off git)
  docs/review/W4-openings.md                    the review table: thresholds, counts, the variants,
                                                ten examples per class to open in the workbench
"""
from __future__ import annotations

import collections
import json
import random
import sys
from multiprocessing import Pool
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from sc2analyst import adapter_sc2 as A, library as L, openings as O, terrain as TR, workbench_vm as V  # noqa: E402

OUT_JSON = REPO / "data/measured/openings.json"
OUT_CLASSES = L.DATA_HOME / "derived" / "openings-v1.json"
OUT_MD = REPO / "docs/review/W4-openings.md"
QS = (0.25, 0.33, 0.5)
MIN_N = 30
ATTACK_MINS = (100, 300, 1000)
CUTS = (360.0, 480.0, 600.0)


def _below_normal():
    try:
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
    except Exception:
        pass


def one(path: Path) -> dict:
    g = A.load(path)
    frame = TR.Frame(g)
    halls = O.town_halls(g, frame)
    kills = O.attack_kills(g, frame)
    nm = V.names(g)
    fmt = L.game_format(g)
    players = {}
    for p in g["players"]:
        pid = p["pid"]
        exps = [h for h in halls[pid] if h["kind"] == "expansion"]
        first = exps[0] if exps else None
        players[pid] = {"race": p["race"], "team": p["team"], "result": p["result"], "name": nm[pid],
                        "first": first["started"] if first else None, "via": first["via"] if first else None,
                        "macro": sum(1 for h in halls[pid] if h["kind"] == "macro"),
                        "kills": [(k[0], k[1]) for k in kills[pid] if k[0] < max(CUTS)]}
    return {"file": path.name, "key": V.game_key(g["map"], g["start_time"]), "fmt": fmt,
            "has_map": bool(frame.bases), "players": players}


def quantiles(v):
    q = L.quantiles(v, QS)
    return {str(k): x for k, x in zip(QS, q)}


def classify(p, fe, attack_min, cut):
    first = p["first"]
    until = min(first if first is not None else float("inf"), cut)
    value = sum(v for t, v in p["kills"] if t < until)
    if value >= attack_min:
        return "ONE-BASE ATTACK"
    if first is not None and fe is not None and first <= fe:
        return "FAST EXPAND"
    if first is not None and first < cut:
        return "STANDARD"
    return "NO EXPANSION"


def main() -> int:
    with Pool(4, initializer=_below_normal) as pool:
        games = pool.map(one, L.record_files(), chunksize=8)
    mapped = [g for g in games if g["has_map"]]

    # the thresholds: first expansions before the default cut, per race x format and per race
    starts = collections.defaultdict(list)
    for g in mapped:
        for p in g["players"].values():
            if p["first"] is not None and p["first"] < O.DEFAULTS["cut_s"]:
                starts[(p["race"], g["fmt"])].append(p["first"])
                starts[(p["race"], "all")].append(p["first"])
    races = sorted({r for r, _ in starts})
    fe_th, qs, ns = {}, {}, {}
    for r in races:
        fe_th[r], qs[r], ns[r] = {}, {}, {}
        for (rr, fmt), v in sorted(starts.items()):
            if rr != r:
                continue
            ns[r][fmt] = len(v)
            qs[r][fmt] = quantiles(v)
            if len(v) >= MIN_N or fmt == "all":
                fe_th[r][fmt] = qs[r][fmt][str(O.DEFAULTS["fe_quantile"])]
    th = {"version": 1, "measured_on": len(mapped), "attack_min": O.DEFAULTS["attack_min"],
          "cut_s": O.DEFAULTS["cut_s"], "fe_quantile": O.DEFAULTS["fe_quantile"], "fe_threshold_s": fe_th,
          "quantiles": qs, "n": ns, "min_n": MIN_N,
          "method": "first expansion start (a town hall at a base other than the main; a hall flown "
                    "from the main counts from its start) among players who expanded before cut_s; "
                    "a race x format with fewer than min_n players uses the race's 'all'"}
    OUT_JSON.write_text(json.dumps(th, indent=1) + "\n", encoding="utf-8")

    # every player classed with the frozen thresholds; the variants beside it
    classes = {}
    counts = collections.Counter()
    by_fmt_race = collections.Counter()
    variants = collections.defaultdict(collections.Counter)
    examples = collections.defaultdict(list)
    via = collections.Counter()
    macro = 0
    for g in mapped:
        row = {}
        for pid, p in g["players"].items():
            fe = O.threshold_for(th, p["race"], g["fmt"])
            c = classify(p, fe, th["attack_min"], th["cut_s"])
            row[str(pid)] = {"class": c, "first_expansion": p["first"], "via": p["via"]}
            counts[c] += 1
            by_fmt_race[(g["fmt"], p["race"], c)] += 1
            examples[c].append((g["key"], p["name"], p))
            if p["via"]:
                via[(p["race"], p["via"])] += 1
            macro += p["macro"]
            for am in ATTACK_MINS:
                for cut in CUTS:
                    variants["attack ≥ %d, before %s" % (am, L.clock(cut))][classify(p, fe, am, cut)] += 1
            for q in QS:
                feq = (th["quantiles"].get(p["race"], {}).get(g["fmt"]) if ns[p["race"]].get(g["fmt"], 0) >= MIN_N
                       else th["quantiles"].get(p["race"], {}).get("all")) or {}
                variants["fast expand by p%d" % round(100 * q)][classify(p, feq.get(str(q)), th["attack_min"], th["cut_s"])] += 1
        classes[g["file"]] = row
    OUT_CLASSES.write_text(json.dumps({"version": 1, "thresholds": "data/measured/openings.json v1",
                                       "classes": classes}), encoding="utf-8")

    head = ("**PROVENANCE: MEASURED by `tools/measure_openings.py` over %d game records with a map layer "
            "(of %d) · STATUS: FOR THE OWNER'S REVIEW · module: `src/sc2analyst/openings.py` · plan: "
            "`docs/plan/PLAN-workbench.md` W4 · your call: O-1**" % (len(mapped), len(games)))
    lines = ["# W4 · Openings and attack fights — review table", "", head, "",
             "## The rule (DL-26, the defaults of O-1)", "",
             "- **ONE-BASE ATTACK:** killed ≥ %d priced value at the other team's bases before starting its "
             "first expansion, and before %s." % (th["attack_min"], L.clock(th["cut_s"])),
             "- **FAST EXPAND:** not that; first expansion started by its race's p33 for the format (below).",
             "- **STANDARD:** expanded before %s. **NO EXPANSION:** none of the above." % L.clock(th["cut_s"]),
             "- An expansion is a town hall at a base other than the main. A Command Center built in the "
             "main and flown out counts from its start; its landing spot comes from the player's land "
             "command (the tracker does not carry it).", "",
             "## The fast-expand thresholds, measured", "",
             "First expansion start among players who expanded before %s:" % L.clock(th["cut_s"]), ""]
    rows = []
    for r in races:
        for fmt in sorted(qs[r], key=lambda f: (f == "all", f)):
            q = qs[r][fmt]
            rows.append([r, fmt, ns[r][fmt]] + [L.clock(q[str(k)]) for k in QS] +
                        [L.clock(fe_th[r][fmt]) if fmt in fe_th[r] else "uses all"])
    lines += [L.md_table(["race", "format", "players", "p25", "p33", "p50", "FAST EXPAND by"], rows), ""]

    lines += ["## The classes", "", L.md_table(["class", "players"], [[c, counts[c]] for c in O.CLASSES]), ""]
    rows = []
    for fmt in sorted({k[0] for k in by_fmt_race}):
        for r in races:
            n = [by_fmt_race[(fmt, r, c)] for c in O.CLASSES]
            if sum(n):
                rows.append([fmt, r] + n)
    lines += [L.md_table(["format", "race"] + list(O.CLASSES), rows), ""]

    lines += ["## The variants — does a class hold if the line moves?", "",
              "Counts per class with one threshold moved at a time. A study conclusion that flips "
              "between these is reported as flipping.", ""]
    lines += [L.md_table(["variant"] + list(O.CLASSES),
                         [[k] + [variants[k][c] for c in O.CLASSES] for k in sorted(variants)]), ""]

    lines += ["## How expansions were found", "",
              L.md_table(["race", "built at the base", "flown from the main"],
                         [[r, via[(r, "started")], via[(r, "landed")]] for r in races]), "",
              "Macro halls (a town hall at no base): %d." % macro, ""]

    rnd = random.Random(7)
    lines += ["## Ten examples per class — open them in the workbench", "",
              "Run `tools/workbench.py`, then the link. Each line: the player, its first expansion, "
              "what it killed at the other team's bases before it.", ""]
    for c in O.CLASSES:
        pool_ = examples[c]
        pick = rnd.sample(pool_, min(10, len(pool_)))
        lines += ["**%s**" % c, ""]
        for key, name, p in pick:
            killed = sum(v for t, v in p["kills"] if t < min(p["first"] or 9e9, th["cut_s"]))
            lines.append("- [%s](http://127.0.0.1:8798/#/game/%s) · %s (%s) · first expansion %s%s · "
                         "killed at enemy bases before it: %d" % (
                             key, key, name, p["race"], L.clock(p["first"]) if p["first"] is not None else "none",
                             " (flown)" if p["via"] == "landed" else "", killed))
        lines.append("")
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("written:", OUT_JSON, OUT_CLASSES, OUT_MD)
    return 0


if __name__ == "__main__":
    sys.exit(main())
