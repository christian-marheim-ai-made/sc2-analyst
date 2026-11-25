#!/usr/bin/env python3
"""W6 · Run the three studies over the library and write their tables.

    .venv/Scripts/python.exe tools/run_studies.py

One pass over the game records (Pool(4), below normal) extracts each game's rows; `studies.py` adds
them up. Writes <data home>/studies/<S-A|S-B|S-C>/v1/table.json and the review tables
docs/review/W6-S-A.md, W6-S-B.md, W6-S-C.md. The model reads a study only once the owner has marked
it reviewed (`reviewed.json` beside its table, written by the workbench's Reviewed button - W7).
"""
from __future__ import annotations

import json
import sys
from multiprocessing import Pool
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from sc2analyst import (adapter_sc2 as A, econ as E, fights as F, library as L, openings as O,  # noqa: E402
                        studies as S, terrain as TR, timeline as T)

STUDIES = L.DATA_HOME / "studies"
REVIEW = REPO / "docs/review"
VERSION = "v1"
VARIANTS = [("default", {}), ("attack ≥ 100", {"attack_min": 100}), ("attack ≥ 1000", {"attack_min": 1000}),
            ("before 6:00", {"cut_s": 360.0}), ("before 10:00", {"cut_s": 600.0}),
            ("fast expand by p25", {"fe_q": 0.25}), ("fast expand by p50", {"fe_q": 0.5})]
SB_VARIANTS = ("default", "attack ≥ 100", "attack ≥ 1000")


def _below_normal():
    try:
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
    except Exception:
        pass


def one(path: Path) -> dict:
    g = A.load(path)
    teams = sorted({p["team"] for p in g["players"]})
    out = {"fmt": L.game_format(g), "variants": {}, "attacks": {}, "fights": []}
    if len(teams) != 2:
        return out
    winner = next((p["team"] for p in g["players"] if p["result"] == "Win"), None)
    th = O.load_thresholds()
    frame = TR.Frame(g)
    if not frame.bases:
        return out
    parts = (O.town_halls(g, frame), O.attack_kills(g, frame))
    rows = T.sampled_rows(g)
    for name, kw in VARIANTS:
        cls = O.classify(g, th, parts=parts, **kw)
        out["variants"][name] = {"fmt": out["fmt"], "winner": winner,
                                 "teams": {t: dict(S.team_profile(cls, t)) for t in teams}}
        if name in SB_VARIANTS:
            out["attacks"][name] = E.catch_up(g, rows, cls, parts[1])
    ev = T._exact_events(g)
    deaths = F.combat_deaths(g)
    for f in F.fights(g):
        denied = E.denied_income(g, f, rows, ev, deaths)
        for pid, lost in E.losses_by_class(g, f, deaths).items():
            if sum(lost.values()) >= 300:
                out["fights"].append({"lost": lost, "denied": (denied.get(pid) or {}).get("denied"),
                                      "observed": E.observed_change(rows, pid, f)})
    return out


def head(study: str, n: int) -> str:
    return ("**PROVENANCE: MEASURED by `tools/run_studies.py` over %d game records · STATUS: FOR THE OWNER'S "
            "REVIEW (mark it Reviewed in the workbench before the model reads it) · module: "
            "`src/sc2analyst/studies.py` · plan: `docs/plan/PLAN-workbench.md` W6 · table: "
            "`<data home>/studies/%s/%s/table.json`**" % (n, study, VERSION))


def write(study: str, table: dict, md: list):
    d = STUDIES / study / VERSION
    d.mkdir(parents=True, exist_ok=True)
    (d / "table.json").write_text(json.dumps(dict(table, causation=S.CAUSATION), indent=1), encoding="utf-8")
    (REVIEW / ("W6-%s.md" % study)).write_text("\n".join(md + ["", "_%s_" % S.CAUSATION, ""]), encoding="utf-8")


def cell(v, suffix=""):
    return "-" if v is None else ("%s%s" % (round(v) if isinstance(v, float) and abs(v) >= 10 else v, suffix))


def main() -> int:
    with Pool(4, initializer=_below_normal) as pool:
        games = pool.map(one, L.record_files(), chunksize=8)
    n = len(games)

    # S-A
    sa = {}
    for name, _ in VARIANTS:
        rows = [g["variants"][name] for g in games if name in g["variants"]]
        sa[name] = {"attack_vs_fast_expand": S.sa_rows(rows), "any_one_sided_attack": S.sa_any(rows)}
    md = ["# W6 · S-A — a one-base attack against a fast expansion", "", head("S-A", n), "",
          "Games where exactly one team had a ONE-BASE ATTACK player and the other team had none but at "
          "least one FAST EXPAND player (W4's classes): how often the attacking team won. Then every game "
          "where exactly one team attacked.", ""]
    rows = []
    for name, _ in VARIANTS:
        for r in sa[name]["attack_vs_fast_expand"]:
            rows.append([name, r["format"], r["games"], cell(r["attacking_team_won"], "%")])
    md += ["## Attack against a fast expansion", "", L.md_table(["variant", "format", "games", "the attacking team won"], rows), ""]
    rows = []
    for name, _ in VARIANTS:
        for r in sa[name]["any_one_sided_attack"]:
            rows.append([name, r["format"], r["games"], cell(r["attacking_team_won"], "%")])
    md += ["## Any game where one team attacked", "", L.md_table(["variant", "format", "games", "the attacking team won"], rows)]
    write("S-A", {"variants": sa}, md)

    # S-B
    sb = {}
    for name in SB_VARIANTS:
        atts = [a for g in games for a in g["attacks"].get(name, [])]
        sb[name] = {fr: S.sb_by_minute(atts, fr) for fr in ("pair", "team")}
    md = ["# W6 · S-B — how much damage the attacker needed to catch up", "", head("S-B", n), "",
          "Per one-base attack (DL-27), by the minute its first attack fight started. **Needed** = the "
          "expander side's lead in collected resources then (the damage needed to be even). **Grows** = the "
          "income gap before the attack, per minute (how much more is needed for every minute later). "
          "**Caught up** = the attack's damage balance covered the lead it had and what it gained during "
          "the attack. Pair framing: the attacker against the players it hit (with two victims, their "
          "economies are summed against one attacker's); team framing: the two teams.", ""]
    for name in SB_VARIANTS:
        for fr in ("pair", "team"):
            rows = [[r["minute"], r["attacks"], cell(r["needed_p50"]), cell(r["grows_per_min_p50"]),
                     cell(r["caught_up"], "%"), cell(r["won_if_caught"], "%"), cell(r["won_if_not"], "%")]
                    for r in sb[name][fr]]
            md += ["## %s · %s framing" % (name, fr), "",
                   L.md_table(["minute", "attacks", "needed (p50)", "grows / min (p50)", "caught up",
                               "won if caught up", "won if not"], rows), ""]
    write("S-B", {"variants": sb}, md)

    # S-C
    fr = [f for g in games for f in g["fights"]]
    buckets = S.sc_buckets(fr)
    fit = E.ols([[f["lost"][c] / 100 for c in E.CLASSES] for f in fr if f["observed"] is not None],
                [f["observed"] for f in fr if f["observed"] is not None])
    coef = dict(zip(("intercept",) + E.CLASSES, fit["coef"]))
    md = ["# W6 · S-C — how much worse a trade is when it kills army only", "", head("S-C", n), "",
          "Per fight × victim that lost 300+ priced value (%d), by the share of the loss that was economy "
          "(workers + town halls). **Denied** = the ledger's lower bound of income denied by the workers "
          "lost (DL-28). **Effective / nominal** = (value lost + denied) ÷ value lost. **Observed** = the "
          "victim's income 60–180 s after against the 60 s before." % len(fr), "",
          L.md_table(["economy share of the loss", "victims", "value lost (p50)", "denied (p50)",
                      "effective / nominal (p50)", "observed income change / min (p50)"],
                     [[b["economy_share"], b["victims"], cell(b["nominal_p50"]), cell(b["denied_p50"]),
                       cell(b["effective_over_nominal_p50"]), cell(b["observed_income_change_p50"])]
                      for b in buckets]), "",
          "## The fit: income / min after a fight, per 100 value lost", "",
          L.md_table(["term", "per 100 value", "over 5 minutes"],
                     [[k, "%+.1f" % v, "" if k == "intercept" else "%+.0f" % (5 * v)] for k, v in coef.items()]), "",
          "n = %d, residual spread %.0f / min." % (fit["n"], fit["resid_sd"]), "",
          "**Read:** per 100 value killed, workers take %.1f income a minute from the victim and army "
          "%.1f - %s times as much per value. A trade that kills only army is, by the economy it denies, "
          "that much weaker than one that kills the same value of workers." % (
              -coef["worker"], -coef["army"], round(coef["worker"] / coef["army"], 1) if coef["army"] else "-")]
    write("S-C", {"buckets": buckets, "fit": {"coef": coef, "n": fit["n"], "resid_sd": fit["resid_sd"]}}, md)
    print("written: S-A, S-B, S-C ->", STUDIES, "and docs/review/W6-S-*.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
