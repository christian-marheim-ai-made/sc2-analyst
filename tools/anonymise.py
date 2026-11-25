#!/usr/bin/env python3
"""
Anonymise player names across the repository.

Keeps the owner's own handles; pseudonymises everyone else. Mapping is persisted so the same
real name always becomes the same pseudonym across runs, including on files added later.

    python tools/anonymise.py              # scan + report, writes nothing (default)
    python tools/anonymise.py --apply      # rewrite text files in place
    python tools/anonymise.py --reverse    # restore real names from the map (local only)

Keep rule (owner, 2026-09-23): keep "Player_1" and any handle containing "ally_2"; anonymise the rest.

Two hard limits, reported and never silently ignored:

  * .SC2Replay files are NOT handled here - their names sit inside compressed MPQ members, so a
    text replace on the file misses them. Use tools/intake_replay.py, which decompresses, replaces
    same-length, recompresses and pads back into the original slot. The two tools share the name
    map, so a player gets the same PlayerNN number in both the replay and the JSON.
  * The name map contains real names. It is written with a .local. infix and is gitignored.

Name matching is WHOLE-WORD, never substring. Short handles do occur inside ordinary English words
-- one three-letter handle in this dataset appears inside "decisive", "decision", "precision" and
"precisely" -- and a substring replace would silently corrupt all of them.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_PATH = os.path.join(ROOT, "tools", "anon-map.local.json")

TEXT_EXT = (".md", ".json", ".py", ".txt", ".csv", ".yml", ".yaml")
BINARY_EXT = (".SC2Replay", ".SC2Map", ".zip", ".png", ".jpg")
SKIP_DIRS = {".git", "__pycache__", ".venv"}
# The replay intake keeps its own, much larger map beside this one (intake_replay.REPLAY_MAP_PATH);
# this tool must never rewrite the real handles that file holds as keys.
SKIP_FILES = {os.path.basename(MAP_PATH), "anon-map-replays.local.json"}


SIDE_LABEL = re.compile(r"(?:player|ally|enemy)_\d+")


def is_kept(name: str) -> bool:
    """Owner's own handles stay as they are - and so do the side labels the demo replays already carry
    (Player_1, Ally_2, Enemy_1 ...): they name no one, and a Battle.net name cannot hold an underscore."""
    low = name.lower()
    return low == "player_1" or "ally_2" in low or bool(SIDE_LABEL.fullmatch(low))


def walk(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn in SKIP_FILES:
                continue
            yield os.path.join(dirpath, fn)


def discover_names(root: str) -> dict:
    """Authoritative source: metadata.players in every extract JSON."""
    found = {}
    for p in walk(root):
        if not p.endswith(".json"):
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        players = None
        if isinstance(d.get("metadata"), dict):
            players = d["metadata"].get("players")
        elif isinstance(d.get("players"), list):
            players = d["players"]
        for pl in players or []:
            if isinstance(pl, dict) and pl.get("name"):
                found.setdefault(pl["name"], pl.get("race"))
    return found


def load_map() -> dict:
    if os.path.exists(MAP_PATH):
        return json.load(open(MAP_PATH, encoding="utf-8"))
    return {"_note": "REAL PLAYER NAMES - do not publish. Gitignored.", "names": {}}


def save_map(m: dict) -> None:
    os.makedirs(os.path.dirname(MAP_PATH), exist_ok=True)
    json.dump(m, open(MAP_PATH, "w", encoding="utf-8"), indent=2, ensure_ascii=False)


def is_alias(name: str) -> bool:
    """A pseudonym this tool already issued.

    Without this guard the tool is NOT idempotent: `discover_names` reads `metadata.players` from
    files it has already anonymised, finds `Player01`, decides it is not a kept handle, and issues
    it a *second* alias (`Player01 -> Player24`). A second `--apply` would then double-anonymise
    every file and destroy the map's reversibility. Caught by a pre-commit scan, 2026-09-23.
    """
    return bool(re.fullmatch(r"Player\d+", name))


def extend_map(m: dict, discovered: dict) -> list:
    """Assign pseudonyms to any newly-seen names. Stable: existing entries never change."""
    names = m["names"]
    used = {v for v in names.values()}
    new = []
    for real in sorted(discovered, key=str.lower):
        if is_kept(real) or is_alias(real) or real in names or real in used:
            continue
        n = 1
        while f"Player{n:02d}" in used:
            n += 1
        alias = f"Player{n:02d}"
        names[real] = alias
        used.add(alias)
        new.append((real, alias))
    return new


def word_pattern(name: str) -> re.Pattern:
    """Whole-word match. A bare substring replace would hit short handles inside ordinary words."""
    return re.compile(r"(?<![0-9A-Za-z_])" + re.escape(name) + r"(?![0-9A-Za-z_])")


def main() -> int:
    ap = argparse.ArgumentParser(description="Anonymise player names across the repo.")
    ap.add_argument("--apply", action="store_true", help="write changes (default: scan only)")
    ap.add_argument("--reverse", action="store_true", help="restore real names from the map")
    ap.add_argument("--root", default=ROOT)
    args = ap.parse_args()

    discovered = discover_names(args.root)
    m = load_map()
    new = extend_map(m, discovered)

    kept = sorted([n for n in discovered if is_kept(n)], key=str.lower)
    print("names discovered : %d" % len(discovered))
    print("  kept           : %s" % ", ".join(kept))
    print("  pseudonymised  : %d" % len(m["names"]))
    if new:
        print("  NEW this run   : %s" % ", ".join("%s->%s" % (a, b) for a, b in new))
    print()

    if args.reverse:
        pairs = [(alias, real) for real, alias in m["names"].items()]
    else:
        pairs = list(m["names"].items())
    patterns = [(word_pattern(a), b, a) for a, b in pairs]

    changed = defaultdict(lambda: defaultdict(int))
    binaries = []
    total = 0

    for p in walk(args.root):
        rel = os.path.relpath(p, args.root).replace("\\", "/")
        if p.endswith(BINARY_EXT):
            blob = open(p, "rb").read()
            hits = sorted({src for _, _, src in patterns
                           if re.search(rb"(?<![0-9A-Za-z_])" + re.escape(src.encode()) + rb"(?![0-9A-Za-z_])", blob)})
            if hits and p.endswith(".SC2Replay"):
                binaries.append((rel, hits))
            continue
        if not p.endswith(TEXT_EXT):
            continue
        try:
            t = open(p, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        orig = t
        for pat, dst, src in patterns:
            t, n = pat.subn(dst, t)
            if n:
                changed[rel][src] = n
                total += n
        if t != orig and args.apply:
            open(p, "w", encoding="utf-8", newline="").write(t)

    verb = "replaced" if args.apply else "would replace"
    print("%s %d occurrence(s) across %d file(s)" % (verb, total, len(changed)))
    for rel in sorted(changed):
        det = ", ".join("%s(%d)" % (k, v) for k, v in sorted(changed[rel].items()))
        print("  %-58s %s" % (rel[:58], det))

    if binaries:
        print()
        print("!! CANNOT BE ANONYMISED - names are inside the MPQ archive:")
        for rel, hits in binaries:
            print("   %-52s %s" % (rel[:52], ", ".join(hits)))
        print("   These must be excluded from publication (.gitignore) or run through a")
        print("   dedicated replay anonymiser. Text replacement would corrupt them.")

    if args.apply:
        save_map(m)
        print("\nmap saved: %s (gitignored)" % os.path.relpath(MAP_PATH, args.root))
    else:
        print("\nscan only - nothing written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
