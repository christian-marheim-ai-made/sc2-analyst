#!/usr/bin/env python3
"""Intake a whole replay folder: an anonymised twin of every .SC2Replay, outside the repo.

    .venv/Scripts/python.exe tools/intake_library.py <src_folder> [<dst_folder>] [--map PATH] [--force]

Ruling 2026-09-24: data unfit for git lives in the data home (config/analyst*.toml), so the default
destination is <data home>/replays. Every replay goes through intake_replay.py's
`anonymise_replay`: same file size, sc2reader-identical event count, kept handles unchanged, no
real-name bytes in any member — that function verifies its own output and deletes it on failure.

What this driver adds:
- idempotence: a twin that already exists is skipped, so re-running after new games is cheap
- one failure never stops the batch; the failing twin is removed, the batch goes on
- a MANIFEST beside the twins that carries **no name** — counts, map, length, event count only.
  The real-name map is intake_replay's REPLAY map (gitignored, separate from the text anonymiser's
  map since 2026-09-24 - see intake_replay.REPLAY_MAP_PATH); this tool never prints a roster, and
  an exception's text (which may quote a name) goes to a local log beside the twins, never into
  the manifest.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from intake_replay import REPLAY_MAP_PATH, anonymise_replay  # noqa: E402
sys.path.insert(0, str(HERE.parent / "src"))
from sc2analyst import library as L  # noqa: E402

DEFAULT_DST = L.DATA_HOME / "replays"
MANIFEST = "_manifest.json"
ERROR_LOG = "_errors.local.log"

# Everything a manifest row may carry. No roster, no alias table, no path outside the twin's name.
ROW_KEYS = ("source", "size", "events", "map", "length", "players", "renamed", "touched_members")


def strip_report(rep: dict) -> dict:
    """The name-free projection of an intake_replay report."""
    return {
        "events": rep["events"],
        "map": rep["map"],
        "length": rep["length"],
        "players": len(rep["players_after"]),
        "renamed": len(rep["aliases"]),
        "touched_members": len(rep["touched_members"]),
    }


def intake_folder(src_dir, dst_dir, map_path, force=False, log=print, anonymise=anonymise_replay):
    """Anonymise every .SC2Replay under src_dir into dst_dir. Returns the batch summary."""
    src_dir, dst_dir, map_path = Path(src_dir), Path(dst_dir), Path(map_path)
    files = sorted(src_dir.rglob("*.SC2Replay"))
    dst_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = dst_dir / MANIFEST
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        manifest = {"note": "anonymised twins of the owner's replays; no player name appears here",
                    "replays": {}, "errors": {}}
    done = skipped = failed = 0
    t0 = time.time()

    def flush():
        manifest_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")

    for i, src in enumerate(files, 1):
        dst = dst_dir / src.name
        if dst.exists() and not force:
            skipped += 1
            continue
        try:
            rep = anonymise(src, dst, map_path)
            row = strip_report(rep)
            row["source"] = src.name
            row["size"] = dst.stat().st_size
            manifest["replays"][src.name] = row
            manifest["errors"].pop(src.name, None)
            done += 1
        except Exception as e:  # noqa: BLE001 - one bad replay must not stop the batch
            manifest["errors"][src.name] = type(e).__name__          # the type only, never the text
            with open(dst_dir / ERROR_LOG, "a", encoding="utf-8") as f:
                f.write("%s\t%s\t%r\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), src.name, e))
            if dst.exists():
                dst.unlink()
            failed += 1
        if i % 25 == 0 or i == len(files):
            flush()
            log("%d/%d  done=%d skipped=%d failed=%d  %.0fs" % (i, len(files), done, skipped, failed,
                                                                time.time() - t0))
    flush()
    return {"files": len(files), "done": done, "skipped": skipped, "failed": failed,
            "manifest": str(manifest_path), "seconds": round(time.time() - t0, 1)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", help="folder of .SC2Replay files, searched recursively")
    ap.add_argument("dst", nargs="?", default=str(DEFAULT_DST),
                    help="where the anonymised twins go (default: the ruled data home on L:)")
    ap.add_argument("--map", default=str(REPLAY_MAP_PATH),
                    help="the replay name map intake_replay maintains (never the text anonymiser's)")
    ap.add_argument("--force", action="store_true", help="re-anonymise twins that already exist")
    args = ap.parse_args(argv)
    if "canon" in Path(args.dst).resolve().parts:
        print("refusing to write twins into canon/", file=sys.stderr)
        return 2
    summary = intake_folder(args.src, args.dst, args.map, force=args.force)
    print(json.dumps(summary, indent=1))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
