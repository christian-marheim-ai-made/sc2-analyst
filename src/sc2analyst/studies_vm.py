"""W7 · The Study screen's view models: the reviewed tables - W6's studies and W6b's aggregates.

Pure: from the table files to what the screen shows (tables, bar charts, drill-down links, whether
the owner has marked it reviewed). The Reviewed mark itself is written only by the owner's own click
on the workbench's button (`mark_reviewed`), never by a script: R-M - the model reads a table only
once he has seen it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import workbench_vm as V

STUDY_TITLES = {"S-A": "A one-base attack against a fast expansion",
                "S-B": "How much damage the attacker needed to catch up",
                "S-C": "How much worse a trade is when it kills army only"}


def _reviewed(d: Path):
    f = d / "reviewed.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def mark_reviewed(d: Path) -> dict:
    """The owner's Reviewed click: written by the workbench's POST from its own page, nothing else."""
    rec = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "by": "the owner, in the workbench"}
    (d / "reviewed.json").write_text(json.dumps(rec), encoding="utf-8")
    return rec


def listing(studies: Path, aggregates: Path) -> dict:
    out = {"studies": [], "aggregates": []}
    for sid, title in STUDY_TITLES.items():
        for v in sorted((studies / sid).glob("v*")) if (studies / sid).exists() else []:
            if (v / "table.json").exists():
                out["studies"].append({"id": sid, "version": v.name, "title": title, "reviewed": _reviewed(v)})
    if aggregates.exists():
        for t in sorted(aggregates.glob("*/*/*/table.json")):
            repo, sel, ver = t.parts[-4], t.parts[-3], t.parts[-2]
            out["aggregates"].append({"repository": repo, "selection": sel, "version": ver,
                                      "reviewed": _reviewed(t.parent)})
    return out


def _fmt(v, suffix=""):
    if v is None:
        return "-"
    if isinstance(v, float):
        v = round(v) if abs(v) >= 10 else round(v, 2)
    return "%s%s" % (v, suffix)


def study_vm(studies: Path, sid: str, version: str = "v1") -> dict:
    d = studies / sid / version
    t = json.loads((d / "table.json").read_text(encoding="utf-8"))
    tables, charts = [], []
    if sid == "S-A":
        for part, title in (("attack_vs_fast_expand", "Attack against a fast expansion"),
                            ("any_one_sided_attack", "Any game where one team attacked")):
            rows = [[name, r["format"], r["games"], _fmt(r["attacking_team_won"], "%")]
                    for name, v in t["variants"].items() for r in v[part]]
            tables.append({"title": title, "columns": ["variant", "format", "games", "the attacking team won"],
                           "rows": rows})
        charts.append({"title": "The attacking team won (%), default thresholds",
                       "bars": [{"label": r["format"], "value": r["attacking_team_won"]}
                                for r in t["variants"]["default"]["attack_vs_fast_expand"]], "unit": "%"})
    elif sid == "S-B":
        for name, v in t["variants"].items():
            for fr, rs in v.items():
                tables.append({"title": "%s · %s framing" % (name, fr),
                               "columns": ["minute", "attacks", "needed (p50)", "grows / min (p50)", "caught up",
                                           "won if caught up", "won if not"],
                               "rows": [[r["minute"], r["attacks"], _fmt(r["needed_p50"]), _fmt(r["grows_per_min_p50"]),
                                         _fmt(r["caught_up"], "%"), _fmt(r["won_if_caught"], "%"),
                                         _fmt(r["won_if_not"], "%")] for r in rs]})
        charts.append({"title": "Damage needed when the attack started (p50, team framing), by minute",
                       "bars": [{"label": "%d′" % r["minute"], "value": r["needed_p50"]}
                                for r in t["variants"]["default"]["team"]], "unit": ""})
    elif sid == "S-C":
        tables.append({"title": "By the economy's share of what was lost",
                       "columns": ["economy share", "victims", "value lost (p50)", "denied (p50)",
                                   "effective / nominal (p50)", "observed income change / min (p50)"],
                       "rows": [[b["economy_share"], b["victims"], _fmt(b["nominal_p50"]), _fmt(b["denied_p50"]),
                                 _fmt(b["effective_over_nominal_p50"]), _fmt(b["observed_income_change_p50"])]
                                for b in t["buckets"]]})
        coef = t["fit"]["coef"]
        tables.append({"title": "The fit: income / min after a fight, per 100 value lost (n %d)" % t["fit"]["n"],
                       "columns": ["term", "per 100 value", "over 5 minutes"],
                       "rows": [[k, "%+.1f" % c, "" if k == "intercept" else "%+.0f" % (5 * c)] for k, c in coef.items()]})
        charts.append({"title": "Income a minute taken per 100 value killed",
                       "bars": [{"label": k, "value": round(-c, 1)} for k, c in coef.items() if k != "intercept"],
                       "unit": ""})
    links = []
    if sid in ("S-A", "S-B"):
        links = [{"label": "Games with a one-base attack", "href": "#/library?opening=ONE-BASE%20ATTACK"},
                 {"label": "Games with a fast expansion", "href": "#/library?opening=FAST%20EXPAND"}]
    return {"kind": "study", "id": sid, "version": version, "title": STUDY_TITLES.get(sid, sid),
            "tables": tables, "charts": charts, "links": links, "causation": t.get("causation"),
            "reviewed": _reviewed(d), "review_doc": "docs/review/W6-%s.md" % sid}


def aggregate_vm(aggregates: Path, repo: str, sel: str, ver: str) -> dict:
    d = aggregates / repo / sel / ver
    t = json.loads((d / "table.json").read_text(encoding="utf-8"))
    total = [[k, _fmt(v)] for k, v in t["total"].items()]
    cols = [k for k in (t["rows"][0] if t["rows"] else {}) if k not in ("game", "race", "result")
            and not isinstance(t["rows"][0].get(k), (dict, list))]
    rows, hrefs = [], []
    for r in t["rows"]:
        rows.append([r["game"], r.get("race"), r.get("result")] + [_fmt(r.get(c)) for c in cols])
        map_name, start = r["game"][:-17], r["game"][-16:]
        hrefs.append("#/game/" + V.game_key(map_name, start))
    shares = [(k, v) for k, v in t["total"].items() if k.startswith("share_") and v is not None]
    return {"kind": "aggregate", "repository": repo, "selection": sel, "version": ver, "games": t["games"],
            "about": t.get("about"),
            "tables": [{"title": "The sum over %d games" % t["games"], "columns": ["measure", "value"], "rows": total},
                       {"title": "Per game (click a game to open it)", "columns": ["game", "race", "result"] + cols,
                        "rows": rows, "hrefs": hrefs}],
            "charts": [{"title": "Shares over the selection (%)", "bars": [{"label": k[6:], "value": v} for k, v in shares],
                        "unit": "%"}],
            "reviewed": _reviewed(d)}

