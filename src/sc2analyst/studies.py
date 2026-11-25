"""W6 · The three studies - the owner's questions over the whole library, counted, never argued.

  S-A  openings meet: does the team with a one-base attack beat the team that fast-expanded?
  S-B  catching up: how much damage the attacker needed when it hit, how that grew per minute,
       and whether the attacks that reached it won
  S-C  what was killed: how much worse a trade is when it kills army only, against the same value
       of workers or town halls

Pure aggregation over rows that `tools/run_studies.py` extracts per game (DL-26, DL-27, DL-28). Every
table is printed at the threshold variants too; a conclusion that flips between them is said to flip.
**What none of them proves: causes.** Who attacks and who expands is the players' choice, and a
stronger team may do both well. Every study table carries that sentence, and the model reads it with
the table (T-study, W8) only after the owner has marked the table reviewed (R-M).
"""
from __future__ import annotations

import collections

from . import library as L

CAUSATION = ("These are associations over the games that happened, not causes: who attacks and who "
             "expands is the players' choice, and a stronger team may do both well.")
ECON_BUCKETS = [(0.0, 0.0, "none"), (0.0, 0.25, "1–25%"), (0.25, 0.5, "25–50%"), (0.5, 1.01, "over 50%")]


def _pct(n, d):
    return None if not d else round(100 * n / d, 1)


# --- S-A ---------------------------------------------------------------------------------------------

def team_profile(classes: dict, team: int) -> collections.Counter:
    return collections.Counter(c["class"] for c in classes.values() if c["team"] == team)


def sa_rows(game_rows: list) -> list:
    """Per format: games where one team had a one-base attacker and the other had none but a fast
    expander - and how often the attacking team won."""
    by = collections.defaultdict(lambda: [0, 0])
    for g in game_rows:
        teams = sorted(g["teams"])
        if len(teams) != 2 or g["winner"] is None:
            continue
        prof = {t: g["teams"][t] for t in teams}
        atk = [t for t in teams if prof[t].get("ONE-BASE ATTACK", 0) > 0]
        if len(atk) != 1:
            continue
        a = atk[0]
        b = teams[1] if a == teams[0] else teams[0]
        if prof[b].get("FAST EXPAND", 0) == 0:
            continue
        by[g["fmt"]][0] += 1
        by[g["fmt"]][1] += g["winner"] == a
    return [{"format": f, "games": n, "attacking_team_won": _pct(w, n)} for f, (n, w) in sorted(by.items())]


def sa_any(game_rows: list) -> list:
    """Per format: every game where exactly one team had a one-base attacker - that team's win rate."""
    by = collections.defaultdict(lambda: [0, 0])
    for g in game_rows:
        teams = sorted(g["teams"])
        if len(teams) != 2 or g["winner"] is None:
            continue
        atk = [t for t in teams if g["teams"][t].get("ONE-BASE ATTACK", 0) > 0]
        if len(atk) == 1:
            by[g["fmt"]][0] += 1
            by[g["fmt"]][1] += g["winner"] == atk[0]
    return [{"format": f, "games": n, "attacking_team_won": _pct(w, n)} for f, (n, w) in sorted(by.items())]


# --- S-B ---------------------------------------------------------------------------------------------

def sb_by_minute(attacks: list, framing: str) -> list:
    rows = collections.defaultdict(list)
    for a in attacks:
        if a["framing"] == framing and a["E0"] is not None:
            rows[int(a["t0"] // 60)].append(a)
    out = []
    for m in sorted(rows):
        v = rows[m]
        caught = [a for a in v if a["caught_up"]]
        not_caught = [a for a in v if a["caught_up"] is False]
        out.append({"minute": m, "attacks": len(v),
                    "needed_p50": L.quantiles([a["E0"] for a in v], (0.5,))[0],
                    "grows_per_min_p50": L.quantiles([a["r_before"] for a in v if a["r_before"] is not None], (0.5,))[0],
                    "caught_up": _pct(len(caught), len(v)),
                    "won_if_caught": _pct(sum(1 for a in caught if a["attacker_won"]),
                                          sum(1 for a in caught if a["attacker_won"] is not None)),
                    "won_if_not": _pct(sum(1 for a in not_caught if a["attacker_won"]),
                                       sum(1 for a in not_caught if a["attacker_won"] is not None))})
    return out


# --- S-C ---------------------------------------------------------------------------------------------

def econ_share(lost: dict) -> float:
    total = sum(lost.values())
    return 0.0 if not total else (lost.get("worker", 0) + lost.get("town hall", 0)) / total


def bucket_of(share: float) -> str:
    if share == 0:
        return "none"
    for lo, hi, label in ECON_BUCKETS[1:]:
        if lo < share <= hi or (lo == 0.0 and 0 < share <= hi):
            return label
    return "over 50%"


def sc_buckets(fight_rows: list) -> list:
    """Per bucket of the economy's share of what was killed: the nominal trade, the income denied
    (ledger, a lower bound), the effective trade over the nominal, the observed income change."""
    by = collections.defaultdict(list)
    for r in fight_rows:
        by[bucket_of(econ_share(r["lost"]))].append(r)
    out = []
    for _, _, label in ECON_BUCKETS:
        v = by.get(label, [])
        if not v:
            continue
        nominal = [sum(r["lost"].values()) for r in v]
        denied = [r["denied"] or 0 for r in v]
        eff = [(n + d) / n for n, d in zip(nominal, denied) if n]
        obs = [r["observed"] for r in v if r["observed"] is not None]
        out.append({"economy_share": label, "victims": len(v),
                    "nominal_p50": L.quantiles(nominal, (0.5,))[0],
                    "denied_p50": L.quantiles(denied, (0.5,))[0],
                    "effective_over_nominal_p50": round(L.quantiles(eff, (0.5,))[0], 2) if eff else None,
                    "observed_income_change_p50": L.quantiles(obs, (0.5,))[0] if obs else None})
    return out
