"""W8 · The task registry: the analysis split into small questions the local model answers one at a time.

A task is a family (T-fight, T-window, T-opening, T-study) with:
  facts(target) -> FactList   numbered facts, each with its SOURCE (DL-31): {repo, key, t}; at most
                              BUDGET_CHARS (~4,000 tokens) - a target that needs more is split, never cut
  system, question, schema    what the model is told and the JSON shape it answers in
  check(answer, facts)        every claim's verdict: kept, or dropped with the reason
  gen                         its generation settings
  version                     sha256 of all of the above (DL-32): a changed prompt never inherits grades

The numbers come from the deterministic layer (R-0, DL-21): the model never counts. Shared checks
(`assess.check_claim`): a claim cites facts that exist, and every number in it is in a fact it cites.
Each family adds its own:
  T-fight    the team it names as having benefited must be DL-29's verdict, or the answer is dropped
  T-window   a claim about the other team must cite a fact about the other team
  T-opening  "caught up" must be DL-27's yes / no, or the answer is dropped
  T-study    a claim that says one thing CAUSES another is dropped (the studies are associations); the
             task is LOCKED until the study's table carries the owner's Reviewed mark (R-M)
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import assess as AS, library as L

BUDGET_CHARS = 13000            # ~4,000 tokens at the 3.3 chars a token measured on the card (brief.py)

SYSTEM = AS.SYSTEM.replace("from one replay", "from one replay (or from the library, for a study)")


class Locked(RuntimeError):
    """The task may not run yet: its input has not been reviewed by the owner (R-M)."""


class FactList:
    def __init__(self):
        self.rows: list[dict] = []

    def add(self, text: str, src: str, key: str, t: float | None = None, team=None) -> str:
        fid = "F%d" % (len(self.rows) + 1)
        self.rows.append({"id": fid, "text": text, "team": team, "src": {"repo": src, "key": key, "t": t}})
        return fid

    def by_id(self) -> dict:
        return {r["id"]: r for r in self.rows}

    def render(self) -> str:
        out = "\n".join("%s%s: %s" % (r["id"], " [team %s]" % r["team"] if r["team"] is not None else "",
                                      r["text"]) for r in self.rows)
        if len(out) > BUDGET_CHARS:
            raise ValueError("facts over budget (%d chars): split the target" % len(out))
        return out


def _claims_schema(extra: dict | None = None, required=()) -> dict:
    props = {"claims": {"type": "array", "maxItems": 6, "items": {
        "type": "object", "properties": {"text": {"type": "string"},
                                         "cites": {"type": "array", "items": {"type": "string"}}},
        "required": ["text", "cites"]}},
        "cannot_answer": {"type": "array", "items": {"type": "string"}}}
    props.update(extra or {})
    return {"type": "object", "properties": props, "required": ["claims", "cannot_answer", *required]}


def check_claims(answer: dict | None, facts: dict, extra: Callable | None = None) -> dict:
    """{"kept": [...], "dropped": [{"claim", "why"}], "answer_dropped": why or None}."""
    if not isinstance(answer, dict) or not isinstance(answer.get("claims"), list):
        return {"kept": [], "dropped": [], "answer_dropped": "the answer is not the JSON asked for"}
    kept, dropped = [], []
    for c in answer["claims"]:
        why = AS.check_claim(c.get("text", ""), c.get("cites") or [], facts)
        if why is None and extra is not None:
            why = extra(c, facts)
        (dropped.append({"claim": c, "why": why}) if why else kept.append(c))
    return {"kept": kept, "dropped": dropped, "answer_dropped": None}


@dataclass
class Task:
    id: str
    target: str
    question: str
    schema: dict
    facts: Callable                    # (target dict) -> (FactList, truth dict)
    check: Callable                    # (answer, facts by id, truth) -> verdicts
    gen: dict = field(default_factory=lambda: {"max_tokens": 3000, "reasoning_budget": 1500})
    system: str = SYSTEM

    @property
    def version(self) -> str:
        blob = json.dumps([self.system, self.question, self.schema, self.check.__name__, self.gen], sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8]

    def prompt(self, facts: FactList) -> str:
        return "FACTS\n%s\n\nQUESTION\n%s" % (facts.render(), self.question)


TASKS: dict = {}


def register(t: Task) -> Task:
    TASKS[t.id] = t
    return t


# --- T-fight ----------------------------------------------------------------------------------------

def fight_facts(target: dict):
    from . import workbench_vm as V
    g, fid = target["game"], int(target["fight"])
    vm = V.fight_vm(g, fid)
    fl = FactList()
    a = vm["verdict"]["team_a"]
    fl.add("Fight %d, %s, at %s: %d deaths, %d value lost in all." % (fid, vm["clock"], vm["where"] or "an unknown place",
                                                                       vm["deaths"], vm["value"]), "fight", "F%d" % fid, vm["start"])
    for team in vm["teams"]:
        t = team["table"]
        parts = ["%s %s→%s" % (m["label"], _n(t[m["key"]]["before"]), _n(t[m["key"]]["after"])) for m in vm["metrics"]]
        fl.add("%s, before → after: %s." % (team["label"], "; ".join(parts)), "fight", "F%d.%s" % (fid, team["team"]),
               vm["after_t"], team=team["label"])
    for p in vm["players"]:
        lost = ", ".join("%s %s (%d)" % (c, _n(v["value"]), v["count"]) for c, v in p["losses"].items() if v["count"])
        if lost:
            fl.add("%s lost: %s%s." % (p["name"], lost, ", %d unpriced" % p["unpriced"] if p["unpriced"] else ""),
                   "fight", "F%d.%s.losses" % (fid, p["pid"]), vm["end"],
                   team=next(t["label"] for t in vm["teams"] if t["team"] == p["team"]))
    v = vm["verdict"]
    fl.add("Lost less value: %s." % (v["lost_less"] or "even"), "benefit", "F%d.lost_less" % fid, vm["end"])
    fl.add("The economy counted (seen from %s): standing gap %s now, income gap %s a minute after, %s over the next 5 "
           "minutes (an estimate%s). Benefited: %s." % (a, _s(v["dN"]), _s(v["dr"]), _s(v["benefit_5min"]),
                                                         ", at the game's end" if v["at_end"] else "", v["benefited"]),
           "benefit", "F%d.benefit" % fid, vm["end"])
    fl.add("Income denied by the workers lost, a lower bound: %s." % vm["denied_income"], "econ", "F%d.denied" % fid, vm["end"])
    benefited = v["benefited"] or ""
    truth = {"benefited": "even" if benefited == "even" else
             ("A" if benefited.startswith(a) else "B" if benefited else None), "team_a": a}
    return fl, truth


def _n(x):
    return "-" if x is None else ("%d" % round(x) if isinstance(x, (int, float)) else str(x))


def _s(x):
    return "-" if x is None else ("%+d" % round(x))


def check_fight(answer, facts, truth):
    out = check_claims(answer, facts)
    if out["answer_dropped"] is None and truth.get("benefited") and answer.get("benefited") != truth["benefited"]:
        out = {"kept": [], "dropped": [{"claim": c, "why": "contradicts DL-29's verdict"} for c in answer.get("claims", [])],
               "answer_dropped": "names %s as benefiting; DL-29 says %s" % (answer.get("benefited"), truth["benefited"])}
    return out


register(Task("T-fight", "fight",
              "Which team benefited from this fight (A = the first team in the facts, B = the other, or even), "
              "which category decided it (workers, army, economy, tech, bank, income), and what each team gained "
              "or gave up. Cite every claim.",
              _claims_schema({"benefited": {"enum": ["A", "B", "even"]},
                              "deciding": {"enum": ["workers", "army", "economy", "tech", "bank", "income", "none"]}},
                             ("benefited", "deciding")),
              fight_facts, check_fight))


# --- T-window -------------------------------------------------------------------------------------------

WINDOW_S = 120.0


def window_facts(target: dict):
    from . import workbench_vm as V
    g, pid, t0 = target["game"], int(target["pid"]), float(target["t"])
    a, b = V.snapshot_vm(g, t0), V.snapshot_vm(g, t0 + WINDOW_S)
    fl = FactList()
    me = next(p for p in a["players"] if p["pid"] == pid)
    mine = next(t for t in a["teams"] if t["team"] == me["team"])
    keys = [m["key"] for m in a["metrics"]]
    labels = {m["key"]: m["label"] for m in a["metrics"]}

    def row(cells_a, cells_b):
        return "; ".join("%s %s→%s" % (labels[k], _n(cells_a[k]["v"]), _n(cells_b[k]["v"])) for k in keys)

    fl.add("Window %s–%s." % (a["clock"], b["clock"]), "timeline", "window", t0)
    pb = next(p for p in b["players"] if p["pid"] == pid)
    fl.add("%s: %s." % (me["name"], row(me["cells"], pb["cells"])), "timeline", "player.%d" % pid, t0, team=mine["label"])
    for ta in a["teams"]:
        tb = next(t for t in b["teams"] if t["team"] == ta["team"])
        fl.add("%s: %s." % (ta["label"], row(ta["cells"], tb["cells"])), "timeline", "team.%s" % ta["team"], t0,
               team=ta["label"])
    for f in V.fight_rows(g):
        if t0 <= f["start"] < t0 + WINDOW_S:
            fl.add("Fight %d at %s, %s: value %d; benefited %s; lost less value %s." % (
                f["id"], f["clock"], f["where"] or "?", f["value"], f["benefited_label"], f["lost_less_label"]),
                "fight", "F%d" % f["id"], f["start"])
    return fl, {"me_team": mine["label"]}


def check_window(answer, facts, truth):
    def other_team_needs_its_fact(c, facts_):
        text = c.get("text", "")
        teams = {r["team"] for r in facts_.values() if r["team"]}
        others = [t for t in teams if t != truth["me_team"]]
        if any(t.lower() in text.lower() for t in others):
            if not any(facts_[x]["team"] in others or facts_[x]["team"] is None for x in c.get("cites", []) if x in facts_):
                return "speaks of the other team without citing a fact about it"
        return None
    return check_claims(answer, facts, other_team_needs_its_fact)


register(Task("T-window", "player-window",
              "Over these two minutes, what changed for this player in workers, army, economy and tech, against "
              "the other team - and what it cost. Cite every claim.",
              _claims_schema(), window_facts, check_window))


# --- T-opening ------------------------------------------------------------------------------------------

def opening_facts(target: dict):
    from . import econ as E, openings as O, terrain as TR, timeline as T, workbench_vm as V
    g, pid = target["game"], int(target["pid"])
    th = O.load_thresholds()
    frame = TR.Frame(g)
    parts = (O.town_halls(g, frame), O.attack_kills(g, frame))
    cls = O.classify(g, th, parts=parts)
    nm = V.names(g)
    fl = FactList()
    for p, c in cls.items():
        fl.add("%s (%s): %s; first expansion %s; killed %d at the other team's bases before it." % (
            nm[p], c["race"], c["class"], L.clock(c["first_expansion"]) if c["first_expansion"] is not None else "none",
            c["attack_value"]), "openings", "class.%d" % p, c["first_expansion"])
    rows = [r for r in E.catch_up(g, T.sampled_rows(g), cls, parts[1]) if r["attacker"] == pid]
    truth = {"caught_up": None}
    for r in rows:
        fl.add("%s framing: the attack ran %s–%s; the expander side's lead in collected resources was %s when it "
               "started and %s at its end; the damage balance in the window was %s; the income gap %s a minute "
               "before and %s after; caught up: %s." % (
                   r["framing"], L.clock(r["t0"]), L.clock(r["t1"]), _n(r["E0"]), _n(r["E1"]), _n(r["D"]),
                   _s(r["r_before"]), _s(r["r_after"]), "yes" if r["caught_up"] else "no"),
               "econ", "catch_up.%s" % r["framing"], r["t0"])
        if r["framing"] == "pair":
            truth["caught_up"] = r["caught_up"]
    res = next((p["result"] for p in g["players"] if p["pid"] == pid), None)
    fl.add("%s's result: %s." % (nm[pid], res), "game", "result", g["end"])
    return fl, truth


def check_opening(answer, facts, truth):
    out = check_claims(answer, facts)
    if out["answer_dropped"] is None and truth.get("caught_up") is not None and answer.get("caught_up") != truth["caught_up"]:
        out = {"kept": [], "dropped": [{"claim": c, "why": "contradicts DL-27"} for c in answer.get("claims", [])],
               "answer_dropped": "says caught_up=%s; DL-27 says %s" % (answer.get("caught_up"), truth["caught_up"])}
    return out


register(Task("T-opening", "opening",
              "Was this one-base attack enough to catch up with the expansion's economy, and what was missing? "
              "Cite every claim.",
              _claims_schema({"caught_up": {"type": "boolean"}}, ("caught_up",)), opening_facts, check_opening))


# --- T-study ----------------------------------------------------------------------------------------------

CAUSAL = re.compile(r"\b(causes?|caused|leads? to|led to|results? in|because of|makes? .{0,20}win)\b", re.I)


def study_facts(target: dict):
    from . import studies_vm as SV
    studies, sid = Path(target["studies"]), target["study"]
    d = studies / sid / target.get("version", "v1")
    if not (d / "reviewed.json").exists():
        raise Locked("%s is not reviewed: the model reads a study only after the owner has (R-M)" % sid)
    vm = SV.study_vm(studies, sid, target.get("version", "v1"))
    fl = FactList()
    for t in vm["tables"]:
        for r in t["rows"]:
            fl.add("%s — %s." % (t["title"], "; ".join("%s %s" % (c, v) for c, v in zip(t["columns"], r))),
                   "study", "%s.%s" % (sid, t["title"][:20]))
    fl.add(vm["causation"], "study", "%s.causation" % sid)
    return fl, {}


def check_study(answer, facts, truth):
    return check_claims(answer, facts, lambda c, f: "states a cause; the study is an association"
                        if CAUSAL.search(c.get("text", "")) else None)


register(Task("T-study", "study", "What does this table say, and what are its limits? Cite every claim.",
              _claims_schema(), study_facts, check_study))
