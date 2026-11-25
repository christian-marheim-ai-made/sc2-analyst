#!/usr/bin/env python3
"""Discover a replay library: inventory every replay's header, and import the maps it references.

    .venv/Scripts/python.exe tools/discover_library.py <replay_folder> [--import-maps]
                                                       [--inventory out.json] [--report out.md]

What it reads from each replay: sc2reader at load_level=1 - the header and details only: map name,
map hash, build, game type, category, length, date, region. **No player data is read.** At that
load level sc2reader does not populate `players` at all, and nothing here asks for them; the
inventory carries map names and dates, never a handle. The privacy rule of R-G applies to every
artifact this tool writes, and tests/test_discover_library.py checks it against a real replay.

Where the maps come from: the StarCraft II client keeps every map it has played in the Battle.net
cache, one archive per map *version*, named by the same hash the replay records:

    %PROGRAMDATA%/Blizzard Entertainment/Battle.net/Cache/<h[0:2]>/<h[2:4]>/<hash>.s2ma

A `.s2ma` is the map archive (MPQ) that extract_map_features.py reads. Measured 2026-09-23: the seven
canon `.SC2Map` files produce feature-identical output to their cached counterparts (the bytes differ,
which is packaging only). So a replay's map is importable with no download: copy the archive to
canon/maps/source/<map name>.SC2Map, then extract features + minimap into data/maps/ under the
existing naming (spaces -> underscores).

Rules, each pinned by a test:
- a map already in canon is never overwritten; re-running is free
- one map name with two different hashes in the library is a version conflict: NOTHING is imported
  for that name and it is reported - the naming rule for versions is the owner's call, not a default
- a hash absent from the cache is reported, never guessed at
- the cache is a cache; nothing relies on it persisting, which is why the copy is made at all
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import shutil
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
from extract_map_features import extract  # noqa: E402

DEFAULT_CACHE = (Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
                 / "Blizzard Entertainment" / "Battle.net" / "Cache")
CANON_MAPS = REPO / "canon" / "maps" / "source"
DATA_MAPS = REPO / "data" / "maps"

# The whole vocabulary of an inventory row. A player never appears in it.
ROW_KEYS = ("file", "size", "map_name", "map_hash", "build", "base_build", "game_type",
            "category", "game_length_s", "real_length_s", "date", "region")


# ----------------------------------------------------------------------------- naming
def data_stem(map_name: str) -> str:
    """`Tacit Sanctity LE` -> `Tacit_Sanctity_LE`: the convention of the tracked data/maps files."""
    return map_name.replace(" ", "_")


def canon_filename(map_name: str) -> str:
    return map_name + ".SC2Map"


# ----------------------------------------------------------------------------- inventory
def scan_replays(folder):
    """Header-only pass over every .SC2Replay under `folder`. Returns (rows, errors)."""
    import sc2reader  # lazy: the pure functions in this module need no parser

    rows, errors = [], []
    for p in sorted(Path(folder).rglob("*.SC2Replay")):
        try:
            r = sc2reader.load_replay(str(p), load_level=1)
            gl, rl, dt = r.game_length, r.real_length, r.date
            rows.append({
                "file": p.name,
                "size": p.stat().st_size,
                "map_name": r.map_name,
                "map_hash": (r.map_hash or "").lower(),
                "build": r.build,
                "base_build": r.base_build,
                "game_type": r.game_type,
                "category": r.category,
                "game_length_s": gl.seconds if gl is not None else None,
                "real_length_s": rl.seconds if rl is not None else None,
                "date": str(dt) if dt is not None else None,
                "region": r.region,
            })
        except Exception as e:  # noqa: BLE001 - one bad file must not stop an inventory
            errors.append({"file": p.name, "error": repr(e)[:200]})
    return rows, errors


def map_versions(rows):
    """{map_name: {map_hash: replay count}} - more than one hash per name is a version conflict."""
    v = collections.defaultdict(collections.Counter)
    for r in rows:
        v[r["map_name"]][r["map_hash"]] += 1
    return {name: dict(c) for name, c in v.items()}


def summarise(rows, short_seconds=120):
    """Aggregates only. `short_seconds` is a reporting threshold, not a definition of anything."""
    by_map = collections.defaultdict(list)
    for r in rows:
        by_map[(r["map_name"], r["map_hash"])].append(r)
    maps = []
    for (name, h), rs in by_map.items():
        dates = sorted(str(r["date"])[:10] for r in rs if r["date"])
        maps.append({"map_name": name, "map_hash": h, "replays": len(rs),
                     "first": dates[0] if dates else None, "last": dates[-1] if dates else None})
    maps.sort(key=lambda m: (-m["replays"], m["map_name"]))
    lengths = sorted(r["game_length_s"] for r in rows if r["game_length_s"] is not None)
    dates = sorted(str(r["date"])[:10] for r in rows if r["date"])

    def count(key):
        c = collections.Counter(str(r[key]) for r in rows)
        return dict(sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))

    return {
        "replays": len(rows),
        "bytes": sum(r["size"] for r in rows),
        "distinct_maps": len(maps),
        "maps": maps,
        "by_game_type": count("game_type"),
        "by_category": count("category"),
        "by_build": dict(sorted(collections.Counter(r["build"] for r in rows).items(),
                                key=lambda kv: str(kv[0]))),
        "date_range": [dates[0], dates[-1]] if dates else [None, None],
        "length_s": ({"min": lengths[0], "median": int(statistics.median(lengths)), "max": lengths[-1]}
                     if lengths else None),
        "short_games": {"threshold_s": short_seconds,
                        "count": sum(1 for x in lengths if x < short_seconds)},
    }


# ----------------------------------------------------------------------------- maps
def index_cache(cache_root):
    """{hash (lower hex): path} for every .s2ma under the cache. `.s2ml` is localisation, not a map."""
    root = Path(cache_root)
    if not root.exists():
        return {}
    return {p.stem.lower(): p for p in root.rglob("*.s2ma")}


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_features(path, d):
    """Exactly what extract_map_features.main writes: indent=2, no trailing newline, text mode."""
    Path(path).write_text(json.dumps(d, indent=2), encoding="utf-8")


def import_maps(rows, cache, canon_dir, data_dir, extract_features=True, force=False):
    """Copy each referenced map from `cache` into `canon_dir` unless present; extract into `data_dir`."""
    canon_dir, data_dir = Path(canon_dir), Path(data_dir)
    rep = {"copied": [], "present": [], "missing_from_cache": [], "version_conflicts": {},
           "extracted": [], "extract_skipped": []}
    for name, hashes in sorted(map_versions(rows).items()):
        if len(hashes) > 1:
            rep["version_conflicts"][name] = hashes
            continue
        h = next(iter(hashes))
        dst = canon_dir / canon_filename(name)
        if dst.exists():
            rep["present"].append(name)
        else:
            src = cache.get(h.lower())
            if src is None:
                rep["missing_from_cache"].append({"map_name": name, "map_hash": h})
                continue
            canon_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            rep["copied"].append({"map_name": name, "map_hash": h, "bytes": dst.stat().st_size,
                                  "md5_ok": _md5(src) == _md5(dst), "source": str(src)})
        if not extract_features:
            continue
        out = data_dir / (data_stem(name) + ".features.json")
        png = data_dir / "minimaps" / (data_stem(name) + ".png")
        if out.exists() and png.exists() and not force:
            rep["extract_skipped"].append(name)
            continue
        png.parent.mkdir(parents=True, exist_ok=True)
        d = extract(str(dst), minimap_out=str(png))
        write_features(out, d)
        rep["extracted"].append({"map_name": name,
                                 "ramps": len(d["ramps"]), "starts": len(d["start_locations"]),
                                 "no_fly": len(d["no_fly_zones"]), "towers": len(d["xelnaga_towers"]),
                                 "destructibles": len(d["destructibles"]),
                                 "clusters": len(d["resource_clusters"])})
    return rep


# ----------------------------------------------------------------------------- report
def report_markdown(summary, imp=None):
    s = summary
    out = ["| map | replays | first | last | hash |", "|---|---|---|---|---|"]
    for m in s["maps"]:
        out.append("| %s | %d | %s | %s | `%s` |"
                   % (m["map_name"], m["replays"], m["first"], m["last"], m["map_hash"][:8]))
    out += ["", "replays: %d · distinct maps: %d · dates %s … %s · %.0f MB"
            % (s["replays"], s["distinct_maps"], s["date_range"][0], s["date_range"][1], s["bytes"] / 1e6),
            "", "| game type | replays |", "|---|---|"]
    out += ["| %s | %d |" % kv for kv in s["by_game_type"].items()]
    out += ["", "| build | replays |", "|---|---|"]
    out += ["| %s | %d |" % kv for kv in s["by_build"].items()]
    if s["length_s"]:
        out += ["", "game length (s): min %d · median %d · max %d"
                % (s["length_s"]["min"], s["length_s"]["median"], s["length_s"]["max"])]
    out += ["games under %d s: %d" % (s["short_games"]["threshold_s"], s["short_games"]["count"])]
    if imp:
        out += ["", "| import | count |", "|---|---|",
                "| already in canon | %d |" % len(imp["present"]),
                "| copied from cache | %d |" % len(imp["copied"]),
                "| extracted | %d |" % len(imp["extracted"]),
                "| extraction skipped (present) | %d |" % len(imp["extract_skipped"]),
                "| missing from cache | %d |" % len(imp["missing_from_cache"]),
                "| version conflicts (nothing imported) | %d |" % len(imp["version_conflicts"])]
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------------- cli
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("replays", help="folder of .SC2Replay files, searched recursively")
    ap.add_argument("--inventory", help="write rows + summary as JSON (map names and dates; never a player)")
    ap.add_argument("--report", help="write the summary as Markdown")
    ap.add_argument("--import-maps", action="store_true",
                    help="copy missing maps from the cache into canon and extract them into data/maps")
    ap.add_argument("--cache", default=str(DEFAULT_CACHE))
    ap.add_argument("--canon", default=str(CANON_MAPS))
    ap.add_argument("--data", default=str(DATA_MAPS))
    ap.add_argument("--force", action="store_true", help="re-extract maps whose features already exist")
    ap.add_argument("--short-seconds", type=int, default=120,
                    help="reporting threshold for the 'short games' count (a count, not a definition)")
    args = ap.parse_args(argv)

    rows, errors = scan_replays(args.replays)
    s = summarise(rows, args.short_seconds)
    print("replays=%d errors=%d maps=%d dates=%s..%s types=%s"
          % (len(rows), len(errors), s["distinct_maps"], s["date_range"][0], s["date_range"][1],
             s["by_game_type"]))
    for e in errors[:10]:
        print("  error:", e["file"], e["error"], file=sys.stderr)

    imp = None
    if args.import_maps:
        cache = index_cache(args.cache)
        print("cache: %d archives under %s" % (len(cache), args.cache))
        imp = import_maps(rows, cache, args.canon, args.data, force=args.force)
        print("present=%d copied=%d extracted=%d skipped=%d missing_from_cache=%d version_conflicts=%d"
              % (len(imp["present"]), len(imp["copied"]), len(imp["extracted"]),
                 len(imp["extract_skipped"]), len(imp["missing_from_cache"]),
                 len(imp["version_conflicts"])))
        for c in imp["copied"]:
            print("  copied  %-28s %9d B  md5_ok=%s" % (c["map_name"], c["bytes"], c["md5_ok"]))
        for x in imp["extracted"]:
            print("  extract %-28s ramps=%d starts=%d nofly=%d towers=%d destr=%d clusters=%d"
                  % (x["map_name"], x["ramps"], x["starts"], x["no_fly"], x["towers"],
                     x["destructibles"], x["clusters"]))
        for m in imp["missing_from_cache"]:
            print("  MISSING from cache:", m, file=sys.stderr)
        for n, hs in imp["version_conflicts"].items():
            print("  VERSION CONFLICT, nothing imported:", n, hs, file=sys.stderr)

    if args.inventory:
        Path(args.inventory).write_text(
            json.dumps({"rows": rows, "errors": errors, "summary": s, "import": imp}, indent=1),
            encoding="utf-8")
        print("inventory:", args.inventory)
    if args.report:
        Path(args.report).write_text(report_markdown(s, imp), encoding="utf-8")
        print("report:", args.report)

    bad = bool(errors) or bool(imp and (imp["missing_from_cache"] or imp["version_conflicts"]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
