"""S7 · The page (R-E, Q18, Q20): one standalone HTML file for one player in one replay.

Three tabs, his words (Q18): *"a tab for watching what happened in the game, strategically, a tab for
financially/macro performance, a tab for things we try to improve on"*. Everything shown is from the
payload (`analyse.analyse`), which is embedded whole in the page (`<script id="payload">`) so every
number can be traced. The assessment and the battle report (S10, S11) fill their slots when they
exist; until then each slot says it awaits the assessment stage.

Nothing is fetched: the minimap is a data: URI, the script is inline, no font or library is loaded.
"""
from __future__ import annotations

import html
import json
import math

from . import baselines as BL, library as L, render

E = html.escape


def _clock(t):
    return L.clock(t)


def _team_colour(team: int, teams: list) -> str:
    return render.TEAM_COLOURS[teams.index(team) % len(render.TEAM_COLOURS)]


def _table(header, rows, cls="") -> str:
    h = "".join("<th>%s</th>" % E(str(x)) for x in header)
    b = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in r) for r in rows)
    return '<div class="scroll"><table class="%s"><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>' % (cls, h, b)


def _evidence(p, key) -> str:
    return '<p class="evidence">%s</p>' % E(p["evidence"][key])


def _slot(section: str, assessment: dict | None) -> str:
    if not assessment or section not in assessment:
        return ('<div class="slot empty" id="assess-%s"><strong>Assessment</strong> — awaiting the '
                'assessment stage.</div>' % section)
    a = assessment[section]
    items = []
    for f in a.get("findings", []):
        cites = "".join("<li>%s</li>" % E(c) for c in f.get("evidence", []))
        mirror = ('<div class="mirror">Mirror: %s</div>' % E(f["mirror"])) if f.get("mirror") else ""
        items.append('<li class="finding %s"><div class="claim">%s</div>%s<ul class="cites">%s</ul></li>'
                     % (E(f.get("kind", "")), E(f.get("claim", "")), mirror, cites))
    cannot = "".join("<li>%s</li>" % E(c) for c in a.get("cannot_answer", []))
    dropped = a.get("dropped", 0)
    meta = a.get("_meta", {})
    return ('<div class="slot" id="assess-%s"><strong>Assessment</strong> <span class="muted">'
            '(%s, checked against the payload: %d kept, %d dropped)</span><ol>%s</ol>%s</div>'
            % (section, E(meta.get("model", "model")), len(a.get("findings", [])), dropped,
               "".join(items), ('<div class="cannot"><strong>Not answerable from this data</strong>'
                                '<ul>%s</ul></div>' % cannot) if cannot else ""))


def _battle_map(p, image_ref) -> tuple[str, list]:
    teams = sorted({x["team"] for x in p["players"]})
    names = {x["pid"]: x["name"] for x in p["players"]}
    team_of = {x["pid"]: x["team"] for x in p["players"]}
    s = render.MinimapSVG(p["game"]["map"], 520, image_ref)
    for b in p["bases"]:
        last = b["held"][-1] if b["held"] else None
        if last and last["to"] is None:
            col = _team_colour(team_of[last["pid"]], teams)
            s.circle(b["x"], b["y"], 4, col, "#000", 0.95, "base held at the end by %s" % names[last["pid"]])
        else:
            s.circle(b["x"], b["y"], 3, "#9ca3af", "#000", 0.6, "base empty at the end")
    for b in p["bases"]:
        if b["main_of"] is not None:
            s.ring(b["x"], b["y"], 11, _team_colour(team_of[b["main_of"]], teams), 3,
                   "%s's spawn" % names[b["main_of"]])
    ranked = sorted(p["fights"], key=lambda f: -f["value"])
    top = ranked[:5]
    for f in p["fights"]:
        if not f["places"]:
            continue
        x, y = f["places"][0]["x"], f["places"][0]["y"]
        lost = f["lost_value_by_team"]
        loser = max(lost, key=lambda k: lost[k])
        r = 3 + 2.2 * math.sqrt(max(1, f["value"])) / 10
        s.circle(x, y, round(min(r, 22), 1), _team_colour(int(loser), teams), "#fff",
                 0.45 if f not in top else 0.8,
                 "%s–%s · %s · lost %s" % (_clock(f["start"]), _clock(f["end"]), f["where"],
                                          ", ".join("team %s %d" % (k, v) for k, v in lost.items())))
    for i, f in enumerate(top):
        if f["places"]:
            s.label(f["places"][0]["x"], f["places"][0]["y"], i + 1, size=13)
    return s.svg("battle map"), top


def _bars_per_minute(p) -> str:
    """Value lost per minute, POV's team above the axis, the enemy below."""
    team_of = {x["pid"]: x["team"] for x in p["players"]}
    pov_team = team_of[p["pov"]]
    mine, theirs = {}, {}
    for pid, t in p["trade"].items():
        for m, lost, _ in t["per_minute"]:
            d = mine if team_of[int(pid)] == pov_team else theirs
            d[m] = d.get(m, 0) + lost
    n = int(p["game"]["length_s"] // 60) + 1
    top = max(list(mine.values()) + list(theirs.values()) + [1])
    W, H, pad = 640, 220, 28
    bw = (W - pad * 2) / n
    mid = H / 2
    bars = []
    for m in range(n):
        a, b = mine.get(m, 0), theirs.get(m, 0)
        x = pad + m * bw
        ha = (mid - 12) * a / top
        hb = (mid - 12) * b / top
        if a:
            bars.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" class="bar-us"><title>%d′ your team lost %d</title></rect>' % (x + 1, mid - ha, bw - 2, ha, m, a))
        if b:
            bars.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" class="bar-them"><title>%d′ enemy lost %d</title></rect>' % (x + 1, mid, bw - 2, hb, m, b))
        if m % 5 == 0:
            bars.append('<text x="%.1f" y="%d" class="axis">%d′</text>' % (x + 1, H - 4, m))
    return ('<svg viewBox="0 0 %d %d" class="chart" role="img" aria-label="value lost per minute">'
            '<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="axisline"/>%s'
            '<text x="%d" y="14" class="axis">your team lost ↑</text>'
            '<text x="%d" y="%d" class="axis">enemy lost ↓</text></svg>'
            % (W, H, pad, mid, W - pad, mid, "".join(bars), pad, pad, H - 18))


def _workers_chart(p) -> str:
    teams = sorted({x["team"] for x in p["players"]})
    W, H, pad = 640, 220, 30
    n = int(p["game"]["length_s"] // 60) + 1
    top = max([v for e in p["expansions"].values() for _, v in e["workers_by_minute"]] + [1])
    lines = []
    for x in p["players"]:
        pts = p["expansions"][str(x["pid"])]["workers_by_minute"]
        if not pts:
            continue
        col = _team_colour(x["team"], teams)
        path = " ".join("%.1f,%.1f" % (pad + m * (W - 2 * pad) / max(1, n - 1),
                                       H - pad - v * (H - 2 * pad) / top) for m, v in pts)
        dash = "" if x["is_pov"] else ' stroke-dasharray="4 3"'
        lines.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="%s"%s><title>%s</title></polyline>'
                     % (path, col, 3 if x["is_pov"] else 1.5, dash, E(x["name"])))
    return ('<svg viewBox="0 0 %d %d" class="chart" role="img" aria-label="workers per minute">'
            '<line x1="%d" y1="%d" x2="%d" y2="%d" class="axisline"/>%s'
            '<text x="%d" y="14" class="axis">workers (max %d) · solid = you</text></svg>'
            % (W, H, pad, H - pad, W - pad, H - pad, "".join(lines), pad, top))


def _standing_chart(p) -> str:
    """W12: each team's standing value every minute (DL-25), one line per team."""
    st = p.get("standing")
    if not st:
        return ""
    teams = sorted(st["teams"], key=int)
    W, H, pad = 640, 220, 30
    vals = [v for t in teams for v in st["teams"][t]["standing"] if v is not None]
    if not vals:
        return ""
    top = max(vals + [1])
    n = max(1, len(st["t"]) - 1)
    lines = []
    for t in teams:
        pts = [(i, v) for i, v in enumerate(st["teams"][t]["standing"]) if v is not None]
        path = " ".join("%.1f,%.1f" % (pad + i * (W - 2 * pad) / n, H - pad - v * (H - 2 * pad) / top) for i, v in pts)
        lines.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="2.5"><title>team %s</title></polyline>'
                     % (path, _team_colour(int(t), [int(x) for x in teams]), E(t)))
    return ('<svg viewBox="0 0 %d %d" class="chart" role="img" aria-label="standing value per minute">'
            '<line x1="%d" y1="%d" x2="%d" y2="%d" class="axisline"/>%s'
            '<text x="%d" y="14" class="axis">standing value (max %d) per minute, one line per team</text></svg>'
            % (W, H, pad, H - pad, W - pad, H - pad, "".join(lines), pad, top))


def _verdict_rows(p) -> list:
    pov_team = next((x["team"] for x in p["players"] if x["is_pov"]), None)

    def label(t):
        if t is None:
            return "even"
        return "your team" if t == pov_team else ("the enemy team" if pov_team is not None else "team %s" % t)
    rows = []
    for v in p.get("fight_verdicts") or []:
        rows.append([str(v["fight"]), _clock(v["start"]), label(v["lost_less"]),
                     label(v["benefited"]) + (" (at the game's end)" if v["at_end"] else ""),
                     "differ" if v["disagree"] else ""])
    return rows


def render_page(p: dict, assessment: dict | None = None, battle_report: dict | None = None,
                baseline_rows: list | None = None) -> str:
    names = {x["pid"]: x["name"] for x in p["players"]}
    teams = sorted({x["team"] for x in p["players"]})
    pov = p["pov"]
    me = next(x for x in p["players"] if x["pid"] == pov)
    image_id = "mm"
    shared = render.shared_image(p["game"]["map"], image_id)
    battle_svg, top = _battle_map(p, image_id)
    chips = "".join('<span class="chip" style="border-color:%s">%s · %s%s</span>'
                    % (_team_colour(x["team"], teams), E(x["name"]), E(x["race"]),
                       " · " + E(x["result"]) if x["result"] else "") for x in p["players"])

    # --- strategic ---------------------------------------------------------------------------
    top_rows = []
    for i, f in enumerate(top):
        top_rows.append([str(i + 1), "%s–%s" % (_clock(f["start"]), _clock(f["end"])), E(f["where"] or "-"),
                         str(f["deaths"]), " / ".join("%s" % f["lost_value_by_team"][str(t)] for t in teams)])
    fight_rows = []
    for f in p["fights"]:
        if f["deaths"] < 3:
            continue
        fight_rows.append([str(f["id"]), "%s–%s" % (_clock(f["start"]), _clock(f["end"])), E(f["where"] or "-"),
                           " / ".join(str(f["deaths_by_team"][str(t)]) for t in teams),
                           " / ".join(str(f["lost_value_by_team"][str(t)]) for t in teams),
                           str(f["places_of_3plus"])])
    focus_html = []
    for fe in p["focus"]:
        me_f = fe["players"][str(pov)]
        svg = render.focus_svg(p["game"]["map"], fe, str(pov), _team_colour(me["team"], teams), 300, image_id)
        team_line = "; ".join("team %s: %s" % (k, E(v["shape"])) for k, v in fe["teams"].items())
        la = me_f["longest_away"]
        focus_html.append('<figure class="focus">%s<figcaption><strong>Fight %d</strong> %s–%s · camera near '
                          'the fight %s of the window · %d switches%s<br><span class="muted">%s</span></figcaption></figure>'
                          % (svg, fe["fight"], _clock(fe["start"]), _clock(fe["end"]),
                             "%.0f%%" % (100 * me_f["near_share"]) if me_f["near_share"] is not None else "-",
                             me_f["switches"],
                             " · longest look away %.0f s (dwell %d)" % (la["seconds"], la["n"]) if la else "",
                             team_line))
    wall_rows = []
    for pid, w in p["walls"].items():
        if not w.get("ramp"):
            continue
        wall_rows.append([E(names[int(pid)]), ", ".join("%s %s" % (E(s["type"]), _clock(s["started"]))
                                                       for s in w["structures"]) or "none",
                          "yes" if w["wall_candidate"] else "no", _clock(w["complete_at"])])
    dc = p["death_causes"][str(pov)]
    dc_rows = [[E(k), str(n), str(v)] for k, n, v in dc["by_killer_type"][:8]]
    kz_rows = [["%g, %g" % (k["x"], k["y"]), str(k["fights"]), str(k["deaths"]), str(k["value"]),
                ", ".join("%s %s" % (E(names[int(a)]), b) for a, b in k["lost_by_player"].items()), E(k["where"])]
               for k in p["killzones"]]
    br = ""
    if battle_report:
        br = ('<div class="slot report" id="battle-report"><strong>Battle report</strong> <span class="muted">'
              '(%s — for fun; the facts are the tables)</span>%s</div>'
              % (E(battle_report.get("_meta", {}).get("model", "")),
                 "".join("<p>%s</p>" % E(par) for par in battle_report.get("paragraphs", []))))
    else:
        br = ('<div class="slot empty" id="battle-report"><strong>Battle report</strong> — awaiting the '
              'assessment stage.</div>')
    strategic = (
        _slot("strategic", assessment) +
        '<h2>The battle map</h2><div class="split"><div>%s</div><div><p class="muted">Rings: spawns. Dots: '
        'bases (team colour = held at the end). Circles: fights, sized by value lost, coloured by the team '
        'that lost more. Numbers: the five largest fights by value lost.</p>%s</div></div>'
        % (battle_svg, _table(["#", "when", "where", "deaths", "lost " + " / ".join("team %s" % t for t in teams)], top_rows)) +
        _evidence(p, "terrain") + br +
        '<h2>Fights</h2>' + _evidence(p, "fights") +
        _table(["#", "when", "where", "deaths " + "/".join("t%s" % t for t in teams),
                "value lost " + "/".join("t%s" % t for t in teams), "places of 3+"], fight_rows) +
        '<h2>Where your camera was</h2>' + _evidence(p, "focus") + '<div class="grid">%s</div>' % "".join(focus_html) +
        '<h2>Walls</h2>' + _evidence(p, "walls") + _table(["player", "at the main ramp", "candidate", "done"], wall_rows) +
        '<h2>What killed your units</h2>' + _evidence(p, "death_causes") + _table(["killer", "deaths", "value"], dc_rows) +
        '<h2>Killzone candidates</h2>' + _evidence(p, "killzones") +
        _table(["cell", "fights", "deaths", "value", "lost by", "where"], kz_rows))

    # --- macro ---------------------------------------------------------------------------------
    tr_rows = []
    for x in p["players"]:
        t = p["trade"][str(x["pid"])]
        tr_rows.append([E(x["name"]), str(t["lost"]), str(t["killed"]), str(t["ratio"]), str(t["income"]),
                        "%.0f%%" % (100 * t["lost_share_of_income"]) if t["lost_share_of_income"] is not None else "-",
                        str(t["deaths_value_unknown"])])
    ex_rows = []
    for x in p["players"]:
        e = p["expansions"][str(x["pid"])]
        w = e["workers_first_reached"]
        ex_rows.append([E(x["name"]), ", ".join("%s %s" % (_clock(h["started"]), E(h["type"])) for h in e["town_halls"]),
                        _clock(w["22"]), _clock(w["44"]), _clock(w["66"]), str(e["workers_max"])])
    pp = p["per_player"][str(pov)]
    idle_rows = [[E(r["type"]), "%s–%s" % (_clock(r["from"]), _clock(r["to"])), str(r["children"]),
                  "%.0f s" % r["idle_s"], "%.0f%%" % (100 * r["idle_share"]),
                  "%s–%s" % (_clock(r["longest_idle"][0]), _clock(r["longest_idle"][1])) if r["longest_idle"] else "-"]
                 for r in pp["idle"]]
    sb_rows = [[_clock(b["from"]), _clock(b["to"]), str(b["supply"]), "%g–%g s" % (b["at_least_s"], b["at_most_s"])]
               for b in pp["supply_blocks"]]
    bo = {x["pid"]: [e for e in p["per_player"][str(x["pid"])]["build_order"] if e["t"] <= 360] for x in p["players"]}
    bo_html = "".join('<details%s><summary>%s (%s) — first 6:00</summary>%s</details>'
                      % (" open" if x["is_pov"] else "", E(x["name"]), E(x["race"]),
                         _table(["time", "supply", "", "what"],
                                [[_clock(e["t"]), E(e["supply"] or ""), {"structure": "▣", "upgrade": "⇧", "morph": "↻"}.get(e["what"], ""),
                                  E(e["name"])] for e in bo[x["pid"]]], "compact"))
                      for x in p["players"])
    larva = pp["larva"]
    larva_html = (_evidence(p, "larva") + _table(["hatchery", "larva", "3+ larva s", "share of life"],
                                                 [[E(r["type"]), str(r["larva"]), "%.0f" % r["banked_s"],
                                                   "%.0f%%" % (100 * r["banked_share"]) if r["banked_share"] is not None else "-"]
                                                  for r in larva])) if larva else ""
    warp = pp["warp"]
    macro = (
        _slot("macro", assessment) +
        '<h2>Trade</h2>' + _evidence(p, "trade") +
        _table(["player", "value lost", "value killed", "killed/lost", "income", "lost/income", "deaths not priced"], tr_rows) +
        '<h2>Value lost per minute</h2>' + _bars_per_minute(p) +
        '<h2>Where each team stood</h2>' + _standing_chart(p) + _evidence(p, "standing") +
        '<h2>Workers</h2>' + _workers_chart(p) + _evidence(p, "expansions") +
        _table(["player", "town halls started", "22 workers", "44", "66", "max"], ex_rows) +
        '<h2>Your production: idle time</h2>' + _evidence(p, "idle") +
        _table(["structure", "alive", "units made", "idle", "idle share", "longest idle"], idle_rows) +
        ('<h2>Who benefited from each fight</h2>' + _evidence(p, "fight_verdicts") +
         _table(["fight", "when", "lost less value", "benefited, the economy counted", ""], _verdict_rows(p))
         if p.get("fight_verdicts") else "") +
        ('<p class="muted">Warp gates: %d, %d warp-ins — idle per gate not computable.</p>'
         % (warp["warp_gates"], warp["warp_ins"]) if warp["warp_gates"] else "") + larva_html +
        '<h2>Your supply blocks</h2>' + _evidence(p, "supply") +
        (_table(["first blocked sample", "last", "at supply", "blocked"], sb_rows) if sb_rows else "<p>None.</p>") +
        '<p class="muted">Chrono casts: %d</p>' % pp["chrono_casts"] +
        '<h2>Build orders</h2>' + _evidence(p, "build") + bo_html)

    # --- improve -------------------------------------------------------------------------------
    bl_rows = []
    for r in baseline_rows or []:
        worse = (r["percentile"] >= 75 if r["better"] == "lower" else r["percentile"] <= 25)
        better = (r["percentile"] <= 25 if r["better"] == "lower" else r["percentile"] >= 75)
        tag = '<span class="tag bad">worse than 3 in 4 of your games</span>' if worse else (
            '<span class="tag good">better than 3 in 4</span>' if better else "")
        if "share" in r["metric"]:
            fmt = lambda v: "%.0f%%" % (100 * v)  # noqa: E731
        elif r["metric"].startswith("t_"):
            fmt = _clock
        else:
            fmt = lambda v: "%g" % round(v, 2)  # noqa: E731
        bl_rows.append([E(r["label"]), fmt(r["value"]), "%s / %s / %s" % (fmt(r["p10"]), fmt(r["p50"]), fmt(r["p90"])),
                        "%d" % r["percentile"], tag, "%s, %d games" % (E(r["group"]), r["n"])])
    away = []
    for fe in p["focus"]:
        f = fe["players"][str(pov)]
        if f["near_at_start"] is False:
            away.append("fight %d (%s): your camera was elsewhere when it started" % (fe["fight"], _clock(fe["start"])))
    longest_idle = sorted((r for r in pp["idle"] if r["longest_idle"]),
                          key=lambda r: -(r["longest_idle"][1] - r["longest_idle"][0]))[:5]
    improve = (
        _slot("improve", assessment) +
        '<h2>This game against your library</h2><p class="evidence">Each metric computed the same way for '
        'every one of your games (`data/measured/baselines.json`). Percentile = share of your games with '
        'a lower value. A tag marks the outer quarters only; whether it matters is the assessment\'s call.</p>' +
        _table(["metric", "this game", "your p10 / p50 / p90", "percentile", "", "compared with"], bl_rows) +
        '<h2>Measured, for you to judge</h2><ul>' +
        "".join("<li>%s idle %s–%s (%.0f s)</li>" % (E(r["type"]), _clock(r["longest_idle"][0]), _clock(r["longest_idle"][1]),
                                                      r["longest_idle"][1] - r["longest_idle"][0]) for r in longest_idle) +
        "".join("<li>supply blocked %s–%s at %d</li>" % (_clock(b["from"]), _clock(b["to"]), b["supply"]) for b in pp["supply_blocks"]) +
        "".join("<li>%s</li>" % E(a) for a in away) + '</ul>' +
        '<h2>Not computable from this data</h2><ul class="muted">'
        '<li>Army strength, counters, upgrade value, spell damage — no unit stats table yet (D-1).</li>'
        '<li>Which warp gate sat idle — a warp-in names no gate.</li>'
        '<li>What a player could see — no vision model (D-3).</li>'
        '<li>Where an army was between births and deaths — positions are sampled every ~10.7 s, '
        'for a share of units only (S2).</li></ul>')

    mirror = ("Mirror check: every lost/killed pair agrees from both sides." if not p["mirror_problems"]
              else "Mirror check FAILED: " + "; ".join(p["mirror_problems"]))
    title = "%s · %s" % (p["game"]["map"], p["game"]["start_time"][:10])
    payload_json = json.dumps(p, separators=(",", ":"), sort_keys=True).replace("</", "<\\/")
    assess_json = json.dumps({"assessment": assessment, "battle_report": battle_report},
                             separators=(",", ":"), sort_keys=True).replace("</", "<\\/")
    return PAGE % {
        "title": E(title), "map": E(p["game"]["map"]), "date": E(p["game"]["start_time"][:16]),
        "format": E(p["game"]["format"]), "length": _clock(p["game"]["length_s"]),
        "result": E(me["result"] or "no result"), "me": E(me["name"]), "race": E(me["race"]),
        "chips": chips, "shared": shared, "strategic": strategic, "macro": macro, "improve": improve,
        "mirror": E(mirror), "payload": payload_json, "assess": assess_json,
        "versions": E("%s · %s · build %s" % (p["payload"], p["adapter"], p["game"]["build"])),
    }


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(title)s</title>
<style>
:root{--bg:#f7f7f5;--fg:#1c1c1c;--muted:#666;--card:#fff;--line:#ddd;--accent:#2563eb;--us:#3b82f6;--them:#ef4444;--good:#15803d;--bad:#b91c1c}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#121314;--fg:#e8e8e6;--muted:#9a9a98;--card:#1b1c1e;--line:#333;--accent:#60a5fa;--good:#4ade80;--bad:#f87171}}
:root[data-theme="dark"]{--bg:#121314;--fg:#e8e8e6;--muted:#9a9a98;--card:#1b1c1e;--line:#333;--accent:#60a5fa;--good:#4ade80;--bad:#f87171}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
header,main{max-width:1100px;margin:0 auto;padding:16px}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 6px}
.muted{color:var(--muted)}.chip{display:inline-block;border:2px solid;border-radius:999px;padding:1px 10px;margin:4px 6px 0 0;font-size:13px}
nav{display:flex;gap:4px;border-bottom:1px solid var(--line);margin-top:12px;flex-wrap:wrap}
nav button{background:none;border:0;border-bottom:3px solid transparent;color:var(--fg);padding:8px 14px;font:inherit;cursor:pointer}
nav button[aria-selected="true"]{border-color:var(--accent);font-weight:600}
section[hidden]{display:none}.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%%;font-size:14px;background:var(--card)}
th,td{border-bottom:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top}th{font-weight:600}
table.compact td{padding:1px 8px}.evidence{font-size:13px;color:var(--muted);font-style:italic;margin:2px 0 8px}
.slot{border:1px solid var(--line);border-left:4px solid var(--accent);background:var(--card);padding:10px 14px;margin:12px 0;border-radius:4px}
.slot.empty{border-left-color:var(--muted);color:var(--muted)}.slot ol{margin:6px 0 0;padding-left:20px}.finding{margin:6px 0}
.claim{font-weight:600}.cites{font-size:13px;color:var(--muted);margin:2px 0}.mirror{font-size:13px}.cannot{margin-top:8px;font-size:14px}
.report p{font-family:Georgia,serif;font-size:16px}.split{display:grid;grid-template-columns:minmax(0,540px) minmax(0,1fr);gap:16px;align-items:start}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}.focus{margin:0}.focus figcaption{font-size:13px}
svg{max-width:100%%;height:auto}.chart{background:var(--card);border:1px solid var(--line)}.axis{font-size:11px;fill:var(--muted)}
.axisline{stroke:var(--muted)}.bar-us{fill:var(--us)}.bar-them{fill:var(--them)}.tag{font-size:12px;padding:1px 6px;border-radius:4px}
.tag.bad{color:var(--bad);border:1px solid var(--bad)}.tag.good{color:var(--good);border:1px solid var(--good)}
footer{max-width:1100px;margin:24px auto;padding:0 16px 32px;font-size:13px;color:var(--muted)}
@media (max-width:760px){.split{grid-template-columns:1fr}header,main{padding:12px 16px}}
</style></head><body>
%(shared)s
<header><h1>%(map)s</h1><div class="muted">%(date)s · %(format)s · %(length)s · %(me)s (%(race)s): %(result)s</div><div>%(chips)s</div>
<nav role="tablist"><button role="tab" aria-selected="true" data-tab="strategic">What happened</button><button role="tab" aria-selected="false" data-tab="macro">Macro &amp; economy</button><button role="tab" aria-selected="false" data-tab="improve">To improve</button></nav></header>
<main>
<section id="strategic" role="tabpanel">%(strategic)s</section>
<section id="macro" role="tabpanel" hidden>%(macro)s</section>
<section id="improve" role="tabpanel" hidden>%(improve)s</section>
</main>
<footer><p>%(mirror)s</p><p>Every number on this page is in the payload below this page's content (<code>#payload</code>); every rule in the italic lines. %(versions)s.</p></footer>
<script type="application/json" id="payload">%(payload)s</script>
<script type="application/json" id="assessment">%(assess)s</script>
<script>
document.querySelectorAll('nav button').forEach(function(b){b.addEventListener('click',function(){
document.querySelectorAll('nav button').forEach(function(x){x.setAttribute('aria-selected',x===b?'true':'false')});
document.querySelectorAll('main section').forEach(function(s){s.hidden=s.id!==b.dataset.tab});});});
</script>
</body></html>"""


def payload_of(page_html: str) -> dict:
    start = page_html.index('<script type="application/json" id="payload">') + len('<script type="application/json" id="payload">')
    end = page_html.index("</script>", start)
    return json.loads(page_html[start:end].replace("<\\/", "</"))


def page_for(game: dict, assessment=None, battle_report=None) -> tuple[str, dict]:
    from . import analyse
    p = analyse.analyse(game)
    try:
        rows = BL.compare(game, p["pov"], analyse.build_times(), BL.load())
    except FileNotFoundError:
        rows = []
    return render_page(p, assessment, battle_report, rows), p
