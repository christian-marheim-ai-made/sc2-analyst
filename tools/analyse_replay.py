#!/usr/bin/env python3
"""One replay in, its page and battle report out - the whole pipeline, no person and no agent in it.

    .venv/Scripts/python.exe tools/analyse_replay.py "<path>.SC2Replay" [--mock]

Every step is a script of this repository; the only model is the local one, through the vendored
llm-bootstrap (`config/runner.toml`: Qwen3.6-27B thinks, Cydonia 24B only rewords the battle report):

  1. intake      an anonymised twin into <data home>/replays (the owner's handle kept)
  2. map         if the map is new, imported from the Battle.net cache and extracted (data/maps)
  3. record      the game record (stage 1) into <data home>/derived/sc2-game-v2
  4. payload     every deterministic repository (S1-S6)
  5. brief       the payload as numbered facts
  6. model       one pass per team, the deterministic check, the three-tab assessment, the plot,
                 the wordsmith - each answer checked against the facts before it may stand
  7. page        one standalone HTML file

Out: <data home>/assessments/<map>_<start>/ - page.html, battle_report.md, every pass's
answer, and run.json with the card's memory before and after. The battle report is also printed.
The original replay is read once, by the intake, and never changed.
Exit codes: 0 done · 2 the card has no room (nothing was loaded) · 3 the replay could not be taken in.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tools"))

import assess as RUN  # noqa: E402
from intake_replay import REPLAY_MAP_PATH, anonymise_replay  # noqa: E402
from sc2analyst import adapter_sc2, library as L, repositories as R  # noqa: E402

TWINS = L.DATA_HOME / "replays"
PY = Path(sys.executable)   # the interpreter running this, whatever its venv


def step(n, text):
    print("[%d/7] %s" % (n, text), flush=True)


def report_md(game: dict, br: dict | None, page: Path) -> str:
    head = "# Battle report — %s, %s\n\n" % (game["map"], game["start_time"][:16])
    if not br:
        return head + "_No battle report: the model's plot or prose did not pass the checks._\n\nPage: %s\n" % page
    return (head + "## %s\n\n" % br.get("title", "") + "\n\n".join(br["paragraphs"]) +
            "\n\n---\n_%s · every number checked against the replay's facts; %s paragraph(s) dropped; "
            "beats not told: %s._\n\n"
            "Page: %s\n" % (br.get("_meta", {}).get("model", ""), br.get("dropped", 0),
                             ", ".join(br.get("untold_beats", [])) or "none", page))


def runtime_missing() -> list[str]:
    """What a run on the card needs and this folder lacks: llama-server and the model files of the
    runner settings (README: "Add llama-server and the models")."""
    import shutil
    import tomllib
    from sc2analyst import llm
    cfg = tomllib.loads(llm.resolve(llm.REAL).read_text(encoding="utf-8"))
    out = []
    server = cfg["runner"].get("llama_server", "llama-server")
    if not (Path(server).exists() or shutil.which(server)):
        out.append("llama-server not found at %s: see README, 'Add llama-server and the models'" % server)
    models = Path(cfg["runner"].get("models_dir", "models"))
    for m in cfg.get("models", {}).values():
        if not (models / m["file"]).exists():
            out.append("put %s into %s" % (m["file"], models))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("replay", type=Path)
    ap.add_argument("--mock", action="store_true", help="the bootstrap's stand-in: no card, no model")
    ap.add_argument("--player", help="your handle in this replay (default: the code's own, as in the demos)")
    a = ap.parse_args()
    src = a.replay.resolve()
    if not src.exists():
        print("no such replay:", src)
        return 3
    if not a.mock:
        missing = runtime_missing()
        if missing:
            print("\n".join(missing))
            return 5

    TWINS.mkdir(parents=True, exist_ok=True)
    twin = TWINS / src.name
    step(1, "intake -> %s" % TWINS)
    if twin.exists():
        print("      twin exists, kept")
    else:
        try:
            anonymise_replay(src, twin, REPLAY_MAP_PATH, keep=(a.player,) if a.player else ())
        except Exception as exc:  # the intake verifies its own output and removes it on failure
            print("      intake failed: %s" % type(exc).__name__)
            return 3

    step(2, "map")
    game = adapter_sc2.load_game(twin, **({"owner_handle": a.player} if a.player else {}))
    if not any(p["is_owner"] for p in game["players"]):
        print("no player named %s in this replay: pass --player <your handle>"
              % (a.player or adapter_sc2.load_game.__defaults__[0]))
        return 4
    if R.map_features(game["map"]) is None:
        print("      %s is new: importing it from the Battle.net cache" % game["map"])
        subprocess.run([str(PY), str(REPO / "tools/discover_library.py"), str(src.parent), "--import-maps"],
                       check=False)
        if R.map_features(game["map"]) is None:
            print("      the map could not be imported; the page will lack the map layer")
    else:
        print("      %s known" % game["map"])

    step(3, "game record")
    record = L.RECORDS / (twin.stem + ".json.gz")
    adapter_sc2.save(game, record)
    games = [(game["map"], game["start_time"][:16])]

    step(4, "payload, brief, model passes and page (this is the long part: ~8 min for a 3v3)")
    rec = RUN.run(games, RUN.OUT, a.mock,
                  record="run-%s_%s.json" % (game["map"].replace(" ", "_"),
                                             game["start_time"][:16].replace(":", "").replace(" ", "_")))
    if rec.get("blocked"):
        print("BLOCKED:", rec["blocked"])
        return 2
    folder = RUN.folder(RUN.OUT, game)
    page = folder / "page.html"
    br_file = folder / "battle_report.json"
    br = json.loads(br_file.read_text(encoding="utf-8")) if br_file.exists() else None
    md = report_md(game, br, page)
    (folder / "battle_report.md").write_text(md, encoding="utf-8")
    step(7, "done in %.0f s · card %s -> %s MiB used" % (
        rec["total_s"], (rec.get("card_before") or {}).get("used"), (rec.get("card_after") or {}).get("used")))
    print()
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
