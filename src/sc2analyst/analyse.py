"""One game -> its deterministic payload: every repository of S1-S6, for one player's point of view.

The payload is what the page renders (S7), what the brief is cut from (S9) and the only thing the
model sees (S10). Everything in it is computed here; nothing in it is the model's. Each section
carries its evidence rule - what it is and what it does not prove.

Names: the payload shows the owner's handle and Ally_2.. / Enemy_1.. for the others (pick.side_names);
a twin's aliases never leave the data home.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import build as B, fights as F, focus as FO, library as L, pick, repositories as R, terrain as T

REPO = Path(__file__).resolve().parents[2]
PAYLOAD_VERSION = "payload-v2"   # v2 (W12): standing per minute, both fight verdicts

EVIDENCE = {
    "fights": "A fight is a run of combat deaths (a player's unit killed by another team) no more than "
              "10 s apart (DL-1). It does not show who fought without losing anything, and two places "
              "fighting at once are one fight (time-only, as ruled); `places` says where the deaths fell.",
    "trade": "Value is sc2reader's cost per build (minerals + gas); deaths it cannot price are counted "
             "apart (DL-20). Income is the game's collection rate integrated over its samples. "
             "Every lost/killed pair is computed from both sides and checked equal (DL-5).",
    "build": "Structures by construction start; units, morphs and upgrades by completion. Supply is the "
             "latest stats sample (every 7.1 s).",
    "idle": "A production structure is idle when no unit it made was in training; build times are "
            "measured from the library (DL-19). Warp gates are not computable (a warp-in names no gate). "
            "A reactor's second slot, a morph and a lift-off are not separated. No threshold: the "
            "owner decides what is a mistake.",
    "larva": "Larva alive per hatchery over time; a larva ends when it turns into an egg. Seconds with "
             "three or more = natural larva generation stopped.",
    "supply": "Stats samples with supply used >= supply made below 200; bounds from the 7.1 s sampling.",
    "expansions": "Town halls by construction start and finish; worker counts are the game's own.",
    "standing": "Each team's standing value (collected - lost, DL-25) and the game's own workers / army / "
                "economy / tech counters, every minute (W12). Collected is the game's collection rate summed; "
                "research is on no counter (W1).",
    "fight_verdicts": "Per fight, two verdicts: which team lost less value, and which benefited with the "
                      "economy counted (standing now + 5 minutes of the income gap, an estimate - DL-29; the "
                      "fight the game ends in at its last sample).",
    "reinforcement": "Army units completed while a fight of six deaths or more ran. Not where they went.",
    "death_causes": "The killing unit's type is in the replay for combat deaths (D-7, verified).",
    "walls": "Wall structures started within 6 cells of the ramp nearest the player's spawn. A wall "
             "CANDIDATE is two or more; whether it closed the ramp is not computed.",
    "terrain": "Fights placed by where their deaths fell: the base (and who held it then), the nearest "
               "ramp, towers, mineral walls. Open ground has no cliff level in this map layer.",
    "killzones": "Grid cells (8 cells) where deaths fell in two or more fights, ranked by value lost. "
                 "Candidates only (P-6).",
    "focus": "Camera dwells numbered in a fight's window; near = within 20 cells of a place of the fight "
             "(one screen is unmeasured, DL-3). Camera is not attention.",
}


def build_times() -> dict:
    return json.loads((REPO / "data/measured/build_times.json").read_text(encoding="utf-8"))["build_times"]


def analyse(game: dict, pov: int | None = None) -> dict:
    names = pick.side_names(game)
    pov = pov if pov is not None else L.owner_pid(game)
    bt = build_times()
    fights = F.fights(game)
    placed = {p["fight"]: p for p in T.place_fights(game, names)}
    for f in fights:
        f["where"] = placed[f["id"]]["where"]
        f["places"] = placed[f["id"]]["places"]
    big = FO.big_fights(game)
    focus = []
    for f in big:
        per = FO.focus(game, f)
        focus.append({"fight": f["id"], "start": f["start"], "end": f["end"],
                      "place_centres": f["place_centres"], "players": per,
                      "teams": FO.team_view(game, f, per)})
    players = []
    for p in sorted(game["players"], key=lambda p: p["pid"]):
        pid = p["pid"]
        players.append({"pid": pid, "name": names[pid], "team": p["team"], "race": p["race"],
                        "result": p["result"], "is_pov": pid == pov})
    per_player = {}
    for p in players:
        pid = p["pid"]
        per_player[str(pid)] = {
            "build_order": B.build_order(game, pid),
            "idle": B.idle_production(game, pid, bt),
            "warp": B.warp_ins(game, pid),
            "larva": B.larva_bank(game, pid),
            "supply_blocks": B.supply_blocks(game, pid),
            "chrono_casts": B.chrono_casts(game, pid),
        }
    repos = R.all_repositories(game)
    standing, verdicts = _standing_and_verdicts(game, names)
    return {
        "payload": PAYLOAD_VERSION,
        "adapter": game["adapter"],
        "game": {"map": game["map"], "build": game["build"], "start_time": game["start_time"],
                 "length_s": game["end"], "format": L.game_format(game)},
        "pov": pov,
        "players": players,
        "fights": fights,
        "big_fights": [f["id"] for f in big],
        "per_player": per_player,
        "trade": repos["trade"],
        "expansions": repos["expansions"],
        "reinforcement": repos["reinforcement"],
        "death_causes": repos["death_causes"],
        "walls": repos["walls"],
        "mirror_problems": repos["mirror_problems"],
        "killzones": T.killzones(game, names=names),
        "bases": T.base_timeline(game),
        "focus": focus,
        "standing": standing,
        "fight_verdicts": verdicts,
        "evidence": EVIDENCE,
    }


def _standing_and_verdicts(game: dict, names: dict):
    """W12: the team standing per minute and both verdicts per fight - the workbench's numbers on the page."""
    from . import benefit as BF, timeline as TL
    teams = sorted({p["team"] for p in game["players"]})
    tl = TL.timeline(game, 60)
    standing = {"t": tl["t"], "teams": {str(t): {k: tl["teams"][t][k] for k in
                                                 ("standing", "workers_game", "army", "economy", "technology")}
                                        for t in teams}}
    verdicts = []
    if len(teams) == 2:
        rows = TL.sampled_rows(game)
        for f in F.fights(game):
            b = BF.fight_benefit(game, rows, f, teams[0], teams[1])
            verdicts.append({"fight": f["id"], "start": f["start"], "lost_less": b["lost_less"],
                             "benefited": b["benefited"], "verdict": b["verdict"], "at_end": b["at_end"],
                             "benefit_5min": b["benefit_5min"], "disagree": b["disagree"]})
    return standing, verdicts
