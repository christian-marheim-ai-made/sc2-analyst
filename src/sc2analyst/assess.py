"""S10 · The assessment, and S11 · the battle report: the model assesses; this module checks it.

Passes, per game (R-A: multi-pass per party, then a deterministic aggregate, never one-shot):
  1. team pass, once per team (party = team, the Q8 reading): findings about that team, each citing
     the brief's fact ids, each with its mirror for the other team (DL-5);
  2. the aggregate - DETERMINISTIC (R-0): every finding checked; what fails is dropped and counted;
  3. final pass: the three tabs' assessment for the point-of-view player, built only from the kept
     findings;
  4. plot pass (S11): the thinker picks the beats of the battle report from the facts (Q14);
  5. wordsmith pass (S11): Cydonia rewrites the beats as prose, adding nothing (Q14).

The checks (`check_claim`), applied to every claim, mirror and beat the model writes:
  - it cites at least one fact, and every cited id exists in the brief;
  - every number in it appears in the text of a fact it cites (a clock 12:34, a percentage, a value)
    - the model quotes numbers, it never computes them (DL-21); small counts 0-10 are free words;
  - a team-pass finding's mirror cites a fact about the other team or a two-sided fact.
Failing any check drops the claim; the page shows how many were kept and dropped.
"""
from __future__ import annotations

import re

NUM = re.compile(r"\d+:\d{2}|\d[\d,]*(?:\.\d+)?%?")
FREE = {str(i) for i in range(11)}

SYSTEM = (
    "You are a StarCraft II analyst with a military mind: zero-sum games, build orders, information, "
    "tempo. You are given a numbered list of facts measured deterministically from one replay. Rules, "
    "all of them binding:\n"
    "1. Use only the facts. Every claim cites the fact ids (F1, F2 ...) it rests on.\n"
    "2. Every number you write must appear, exactly as written, in a fact you cite. Never compute a new "
    "number (no sums, differences, ratios, rounding): quote.\n"
    "3. If a fact is useless or you do not understand it, ignore it. Never assume what the facts do not "
    "say. Unit strength, counters, vision and army positions are not in the data.\n"
    "4. Real tips only: what these facts show this player could have done, not general advice.\n"
    "5. If a question cannot be answered from the facts, say so in cannot_answer. That is a correct "
    "answer.\n"
    "6. Mirror: what is good for one team is bad for the other. For each finding, state the "
    "counterpart for the other team and cite the fact that shows it.\n"
    "Be intellectually honest and objective. Answer in the JSON shape asked for, nothing else.")

_FINDING = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["deciding_factor", "strength", "mistake",
                                            "missed_opportunity", "macro"]},
        "claim": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "mirror": {"type": "string"},
        "mirror_evidence": {"type": "array", "items": {"type": "string"}},
        "impact": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["kind", "claim", "evidence", "mirror", "mirror_evidence", "impact"],
}
TEAM_SCHEMA = {
    "type": "object",
    "properties": {"findings": {"type": "array", "items": _FINDING, "maxItems": 8},
                   "cannot_answer": {"type": "array", "items": {"type": "string"}, "maxItems": 4}},
    "required": ["findings", "cannot_answer"],
}
_TAB = {
    "type": "object",
    "properties": {
        "findings": {"type": "array", "maxItems": 5, "items": {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": ["deciding_factor", "strength", "mistake",
                                                               "missed_opportunity", "macro", "tip"]},
                           "claim": {"type": "string"},
                           "evidence": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                           "mirror": {"type": "string"}},
            "required": ["kind", "claim", "evidence", "mirror"]}},
        "cannot_answer": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
    },
    "required": ["findings", "cannot_answer"],
}
FINAL_SCHEMA = {"type": "object", "properties": {"strategic": _TAB, "macro": _TAB, "improve": _TAB},
                "required": ["strategic", "macro", "improve"]}
PLOT_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"},
                   "beats": {"type": "array", "maxItems": 8, "items": {
                       "type": "object", "properties": {"fact": {"type": "string"}, "line": {"type": "string"}},
                       "required": ["fact", "line"]}}},
    "required": ["title", "beats"],
}
PROSE_SCHEMA = {"type": "object",
                "properties": {"paragraphs": {"type": "array", "items": {"type": "string"},
                                              "minItems": 2, "maxItems": 8}},
                "required": ["paragraphs"]}


# names, not quantities: "Fight 26", "F12", "base 16", "ramp 2", "Enemy 3", "Ally 1", "level 3/2",
# a minute mark "17'" - measured on the 20-game run, the bulk of the remaining "stray numbers".
# Since 2026-11-25 the players are "Ally_2", "Enemy_1" and, in the demo replays, "Player_1": the
# underscore joins the name too.
IDENT = re.compile(r"\b(?:[Ff]ights?|[Bb]ases?|[Rr]amps?|Enemy|Ally|Player|[Ll]evel|[Tt]eam|P)[\s_]*\d+(?:/\d+)?"
                   r"|\bF\d+\b|\b\d+'(?!\d)")


def numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in NUM.findall(IDENT.sub(" ", text or ""))}


def check_claim(text: str, cited: list, facts: dict) -> str | None:
    """None when the claim may stand; else the reason it is dropped."""
    if not cited:
        return "cites no fact"
    missing = [c for c in cited if c not in facts]
    if missing:
        return "cites unknown fact(s) %s" % ", ".join(missing[:3])
    allowed = set()
    for c in cited:
        allowed |= numbers(facts[c]["text"])
    stray = sorted(n for n in numbers(text) - allowed if n not in FREE)
    if stray:
        return "number(s) not in the cited facts: %s" % ", ".join(stray[:4])
    return None


def check_team_findings(answer: dict | None, facts: dict, team: int) -> dict:
    kept, dropped = [], []
    if not isinstance(answer, dict):
        return {"kept": [], "dropped": [{"reason": "the answer was not JSON"}], "cannot_answer": []}
    for i, f in enumerate(answer.get("findings", [])):
        why = check_claim(f.get("claim", ""), f.get("evidence", []), facts)
        if why is None:
            me = f.get("mirror_evidence", [])
            # the mirror may quote any fact the finding cites, its own or the claim's (measured on 20
            # games: 162 of 205 drops were mirrors quoting the claim's facts); it must still cite one
            # of its own about the other side
            why = check_claim(f.get("mirror", ""), list(f.get("evidence", [])) + list(me), facts)                 if me else "cites no fact"
            if why is None and not any(facts[m]["team"] in (None,) or facts[m]["team"] != team for m in me):
                why = "the mirror cites no fact about the other team"
            if why is not None:
                why = "mirror: " + why
        (kept if why is None else dropped).append(dict(f, n=i + 1) if why is None else
                                                  {"finding": f, "reason": why})
    return {"kept": kept, "dropped": dropped, "cannot_answer": answer.get("cannot_answer", [])}


def check_final(answer: dict | None, facts: dict) -> tuple[dict, int]:
    out, dropped = {}, 0
    if not isinstance(answer, dict):
        return {}, -1
    for tab in ("strategic", "macro", "improve"):
        a = answer.get(tab) or {}
        keep = []
        for f in a.get("findings", []):
            why = check_claim(f.get("claim", ""), f.get("evidence", []), facts)
            if why is not None:
                dropped += 1
                continue
            if f.get("mirror") and check_claim(f["mirror"], f.get("evidence", []), facts) is not None:
                f = dict(f, mirror="")
            keep.append({"kind": f.get("kind"), "claim": f["claim"], "mirror": f.get("mirror", ""),
                         "evidence": ["%s: %s" % (e, facts[e]["text"]) for e in f["evidence"]]})
        out[tab] = {"findings": keep, "cannot_answer": a.get("cannot_answer", []), "dropped": 0}
    return out, dropped


def team_prompt(brief: dict, team: str) -> str:
    return ("FACTS\n%s\n\nTASK\nAnalyse %s. List up to 8 findings: the deciding factors of the game's "
            "outcome as seen from this team's side, what went well, what went wrong, mistakes, and "
            "opportunities not capitalised on. Each finding cites its facts and its mirror for the other "
            "team. Order by impact on the outcome." % (brief["text"], brief["teams"][team]))


def final_prompt(brief: dict, kept: dict, pov_name: str) -> str:
    lines = []
    for team, ks in kept.items():
        for f in ks:
            lines.append("- (%s, %s, impact %s) %s [cites %s] Mirror: %s [cites %s]" % (
                brief["teams"][team], f["kind"], f.get("impact"), f["claim"], ", ".join(f["evidence"]),
                f.get("mirror", ""), ", ".join(f.get("mirror_evidence", []))))
    return ("FACTS\n%s\n\nCHECKED FINDINGS (from one pass per team; every number in them was checked "
            "against the facts)\n%s\n\nTASK\nWrite the assessment for %s. Three parts:\n"
            "- strategic: the big deciding factors of the game's outcome (at most 5), most decisive "
            "first;\n- macro: the financial and macro performance - economy, production, supply, "
            "expansions (at most 5);\n- improve: mistakes and opportunities not capitalised on, each "
            "with a real tip the facts support (at most 5).\nBuild only on the checked findings and "
            "the facts; cite fact ids." % (brief["text"], "\n".join(lines) or "(none survived)", pov_name))


def plot_prompt(brief: dict) -> str:
    return ("FACTS\n%s\n\nTASK\nYou plan a short, dramatic battle report of this game - for fun, but "
            "every beat true. Pick up to 8 beats in time order: the opening tension, the turning "
            "fights, the end. Each beat names ONE fact id and one line of plot that stays inside that "
            "fact (its numbers quoted exactly, nothing invented). Give it a title." % brief["text"])


def check_plot(answer: dict | None, facts: dict) -> tuple[dict | None, int]:
    if not isinstance(answer, dict):
        return None, -1
    beats, dropped = [], 0
    for b in answer.get("beats", []):
        if check_claim(b.get("line", ""), [b.get("fact", "")], facts) is None:
            beats.append({"fact": b["fact"], "line": b["line"], "text": facts[b["fact"]]["text"]})
        else:
            dropped += 1
    return {"title": answer.get("title", ""), "beats": beats}, dropped


WORDSMITH_SYSTEM = (
    "You are a war correspondent with a sharp pen. You are given the beats of a battle report, each with "
    "the fact it rests on. Rewrite them as 3 to 6 short paragraphs of vivid prose. Cover EVERY beat, in "
    "order; the last paragraph tells the last beat. Who lost what and who came out ahead is exactly as "
    "the fact says - never reverse it. You add NOTHING: no number, unit, place, name or event that is not "
    "in the beats. Keep every name exactly as written. Answer in the JSON shape asked for.")


def prose_prompt(plot: dict) -> str:
    # one paragraph per beat, said with its count: the first real report stopped at the schema's
    # paragraph limit and left out the decisive last fight (2026-10-26)
    n = len(plot["beats"])
    return "TITLE: %s\nWrite exactly %d paragraphs, one per beat, in this order.\nBEATS\n%s" % (
        plot["title"], n, "\n".join("%d. %s  (fact: %s)" % (i + 1, b["line"], b["text"])
                                    for i, b in enumerate(plot["beats"])))


def untold_beats(paragraphs: list, plot: dict) -> list:
    """Beats whose first clock time appears in no paragraph - an omission the number check misses."""
    text = " ".join(paragraphs)
    out = []
    for b in plot["beats"]:
        clocks = re.findall(r"\d+:\d{2}", b["line"] + " " + b["text"])
        if clocks and clocks[0] not in text:
            out.append(b["fact"])
    return out


def check_prose(answer: dict | None, plot: dict) -> tuple[list, int]:
    if not isinstance(answer, dict):
        return [], -1
    allowed = set()
    for b in plot["beats"]:
        allowed |= numbers(b["line"]) | numbers(b["text"])
    keep, dropped = [], 0
    for par in answer.get("paragraphs", []):
        if numbers(par) - allowed - FREE:
            dropped += 1
        else:
            keep.append(par)
    return keep, dropped
