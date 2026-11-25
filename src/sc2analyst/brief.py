"""S9 · The brief: the payload cut into numbered facts the model reads and cites (R-F, R-0).

Every line is one fact with an id (F1, F2 ...), the team it is about (or none for a two-sided fact)
and a text in which every number was computed by the repositories. The model may only cite these ids;
`assess.py` drops any claim whose numbers are not in the facts it cites. Budget: BUDGET_TOKENS for the
facts (the 16k context holds the instructions, the facts and the answer); the brief is cut by
priority, lowest first, and says what it left out.

The consumer is the team pass (R-A: one pass per party - party = team, the Q8 reading - then a
deterministic aggregate). The same facts serve the final pass and the battle report's plot.
"""
from __future__ import annotations

from . import library as L

BUDGET_TOKENS = 9000
CHARS_PER_TOKEN = 3.3  # measured on the proof's prompt: 5,066 chars -> 1,535 tokens


def _c(t):
    return L.clock(t)


class Facts:
    def __init__(self):
        self.rows: list[dict] = []

    def add(self, topic: str, text: str, team=None, prio: int = 1):
        self.rows.append({"id": "F%d" % (len(self.rows) + 1), "topic": topic, "team": team,
                          "prio": prio, "text": text})


def build(p: dict) -> dict:
    names = {x["pid"]: x["name"] for x in p["players"]}
    team_of = {x["pid"]: x["team"] for x in p["players"]}
    teams = sorted(set(team_of.values()))
    pov_team = team_of[p["pov"]]
    tname = {t: ("Team %s (%s's team)" % ("AB"[i], names[p["pov"]]) if t == pov_team else
                 "Team %s" % "AB"[i]) for i, t in enumerate(teams)}
    F = Facts()
    g = p["game"]
    F.add("game", "%s, %s, length %s, build %d." % (g["map"], g["format"], _c(g["length_s"]), g["build"]), None, 0)
    for t in teams:
        members = [x for x in p["players"] if x["team"] == t]
        res = members[0]["result"] or "no result"
        F.add("game", "%s: %s. Result: %s." % (tname[t], ", ".join("%s (%s)" % (x["name"], x["race"])
                                                                for x in members), res), t, 0)
    # fights: the biggest by value, every fight of 10+ deaths, the rest summarised
    fights = sorted(p["fights"], key=lambda f: -f["value"])
    shown = [f for f in fights[:12]] + [f for f in fights[12:] if f["deaths"] >= 10]
    for rank, f in enumerate(sorted(shown, key=lambda f: f["start"])):
        lost = " and ".join("%s lost %d value (%d deaths)" % (tname[t], f["lost_value_by_team"][str(t)],
                                                             f["deaths_by_team"][str(t)]) for t in teams)
        # who came out ahead is stated, not left to be read off two numbers: the trial's wordsmith
        # inverted a fight's winner from "A lost 500 and B lost 1550" (2026-10-26)
        lv = {t: f["lost_value_by_team"][str(t)] for t in teams}
        best = min(lv.values())
        ahead = [t for t in teams if lv[t] == best]
        verdict = ("even by value lost" if len(ahead) > 1 else
                   "%s came out ahead (it lost less value)" % tname[ahead[0]].split(" (")[0])
        F.add("fight", "Fight %d, %s-%s at %s: %s; %s. Deaths fell in %d separate place(s)." % (
            f["id"], _c(f["start"]), _c(f["end"]), f["where"] or "unknown", lost, verdict,
            f["places_of_3plus"] or 1), None, 0 if f in fights[:6] else 2)
    small = [f for f in p["fights"] if f not in shown]
    if small:
        F.add("fight", "%d smaller fights not listed (%d deaths, %d value in all)." % (
            len(small), sum(f["deaths"] for f in small), sum(f["value"] for f in small)), None, 3)
    # trade and economy per player
    for x in p["players"]:
        t = p["trade"][str(x["pid"])]
        F.add("trade", "%s: lost %d value, killed %d (ratio %s); collected %d; lost %s of what it collected." % (
            x["name"], t["lost"], t["killed"], t["ratio"], t["income"] or 0,
            "%d%%" % round(100 * t["lost_share_of_income"]) if t["lost_share_of_income"] is not None else "n/a"),
            x["team"], 0)
    for t in teams:
        per = {}
        for x in p["players"]:
            if x["team"] != t:
                continue
            for m, lost, _ in p["trade"][str(x["pid"])]["per_minute"]:
                per[m] = per.get(m, 0) + lost
        F.add("trade", "%s value lost per minute: %s." % (tname[t], ", ".join(
            "%d'=%d" % (m, v) for m, v in sorted(per.items()) if v)), t, 2)
    for x in p["players"]:
        e = p["expansions"][str(x["pid"])]
        w = dict(e["workers_by_minute"])
        halls = ", ".join("%s started %s" % (h["type"], _c(h["started"])) for h in e["town_halls"][1:]) or "none"
        F.add("macro", "%s: expansions %s; workers at 4'/6'/8'/10'/12': %s; reached 44 workers at %s; max %s." % (
            x["name"], halls, "/".join(str(w.get(m, "-")) for m in (4, 6, 8, 10, 12)),
            _c(e["workers_first_reached"]["44"]), e["workers_max"]), x["team"], 1)
    for x in p["players"]:
        pp = p["per_player"][str(x["pid"])]
        idle = [r for r in pp["idle"] if r["to"] - r["from"] >= 60]
        if idle:
            life = sum(r["to"] - r["from"] for r in idle)
            idle_s = sum(r["idle_s"] for r in idle)
            worst = max(idle, key=lambda r: r["longest_idle"][1] - r["longest_idle"][0] if r["longest_idle"] else 0)
            F.add("macro", "%s: production structures idle %d%% of their combined life; longest single idle: %s %s-%s." % (
                x["name"], round(100 * idle_s / life), worst["type"], _c(worst["longest_idle"][0]),
                _c(worst["longest_idle"][1])), x["team"], 1)
        blocks = pp["supply_blocks"]
        if blocks:
            F.add("macro", "%s: supply blocked %d time(s), at least %d s in all; at %s." % (
                x["name"], len(blocks), round(sum(b["at_least_s"] for b in blocks)),
                ", ".join("%s (%d)" % (_c(b["from"]), b["supply"]) for b in blocks[:6])), x["team"], 1)
        lv = [r for r in pp["larva"] if r["life_s"] >= 60]
        if lv:
            life = sum(r["life_s"] for r in lv)
            F.add("macro", "%s: hatcheries held 3+ unused larva %d%% of their life." % (
                x["name"], round(100 * sum(r["banked_s"] for r in lv) / life)), x["team"], 1)
        if pp["warp"]["warp_gates"]:
            F.add("macro", "%s: %d warp gates, %d warp-ins (idle per gate not computable)." % (
                x["name"], pp["warp"]["warp_gates"], pp["warp"]["warp_ins"]), x["team"], 3)
        bo = [e for e in pp["build_order"] if e["what"] in ("structure", "upgrade", "morph") and e["t"] <= 300]
        if bo:
            F.add("build", "%s opening to 5:00: %s." % (x["name"], ", ".join(
                "%s %s" % (_c(e["t"]), e["name"]) for e in bo[:16])), x["team"], 2)
        ups = [e for e in pp["build_order"] if e["what"] == "upgrade"]
        if ups:
            F.add("build", "%s upgrades done: %s." % (x["name"], ", ".join(
                "%s %s" % (e["name"], _c(e["t"])) for e in ups[:12])), x["team"], 2)
    for x in p["players"]:
        d = p["death_causes"][str(x["pid"])]["by_killer_type"][:4]
        if d:
            F.add("deaths", "%s's units were killed mostly by: %s." % (x["name"], ", ".join(
                "%s (%d deaths, %d value)" % (k, n, v) for k, n, v in d)), x["team"], 2)
    for pid, w in p["walls"].items():
        if w.get("structures"):
            F.add("walls", "%s at its main ramp: %s%s." % (names[int(pid)], ", ".join(
                "%s %s" % (s["type"], _c(s["started"])) for s in w["structures"]),
                "; first three done at %s" % _c(w["complete_at"]) if w["complete_at"] else ""),
                team_of[int(pid)], 3)
    for fe in p["focus"]:
        parts = []
        for pid, f in fe["players"].items():
            if f["near_share"] is None:
                continue
            parts.append("%s %d%% near%s" % (names[int(pid)], round(100 * f["near_share"]),
                                             "" if f["near_at_start"] else " (elsewhere at the start)"))
        shapes = "; ".join("%s %s" % (tname[int(k)], v["shape"]) for k, v in fe["teams"].items())
        F.add("focus", "Camera during fight %d (%s-%s): %s. At its start: %s." % (
            fe["fight"], _c(fe["start"]), _c(fe["end"]), ", ".join(parts), shapes), None, 1)
    for k in p["killzones"][:3]:
        F.add("terrain", "Deaths fell in %d different fights at %s (%d deaths, %d value: %s)." % (
            k["fights"], k["where"], k["deaths"], k["value"], ", ".join(
                "%s lost %d" % (names[int(a)], b) for a, b in k["lost_by_player"].items())), None, 3)
    F.add("limits", "Not in this data: unit stats (army strength, counters, upgrade value, spell damage); "
                    "vision; where armies were between births and deaths; which warp gate was idle.", None, 0)

    # cut to the budget, lowest priority (highest number) first, latest facts first within it
    rows = F.rows
    dropped = []
    while sum(len(r["text"]) + 8 for r in rows) / CHARS_PER_TOKEN > BUDGET_TOKENS:
        worst = max(rows, key=lambda r: (r["prio"], int(r["id"][1:])))
        if worst["prio"] == 0:
            break
        rows = [r for r in rows if r is not worst]
        dropped.append(worst["id"])
    text = "\n".join("%s [%s] %s" % (r["id"], "both" if r["team"] is None else tname[r["team"]].split(" (")[0],
                                     r["text"]) for r in rows)
    return {"facts": {r["id"]: r for r in rows}, "text": text, "teams": {str(t): tname[t] for t in teams},
            "pov_team": pov_team, "dropped": dropped,
            "tokens_estimate": round(len(text) / CHARS_PER_TOKEN)}
