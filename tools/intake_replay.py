#!/usr/bin/env python3
"""Intake step 1: anonymise a .SC2Replay in place, safely.

    python tools/intake_replay.py <in.SC2Replay> <out.SC2Replay>

Keeps the owner's handles (Player_1, anything containing "ally_2"), pseudonymises every other
player, and writes a byte-for-byte same-size replay that sc2reader parses identically.

WHY THIS IS NOT A PLAIN TEXT REPLACE (measured 2026-09-23 on andre_gamet.SC2Replay):
  * player names live in FOUR members: replay.details, replay.initData,
    replay.resumable.events, replay.server.battlelobby
  * three of them are bzip2-compressed (method byte 0x10); a raw byte search sees only the one
    stored uncompressed, so a naive replace leaves the real names in place
  * all four are SINGLE_UNIT and none is ENCRYPTED, so each is one blob at one offset

HOW IT WORKS:
  for every member whose decompressed bytes contain a target name:
    decompress -> same-LENGTH replace -> recompress as zlib (0x02) -> zero-pad to the original
    archived_size -> write back at the original offset
  The hash table, block table, offsets and file size are never touched. zlib ignores trailing
  padding (verified), and mpyq dispatches on the method byte, so 0x02 in a slot that held 0x10 is
  read correctly.

HARDENING (each is a measured failure mode, not a hypothetical):
  * aliases are byte-length-exact, derived from the stable PlayerNN number the JSON anonymiser
    already issued, so the two tools agree on who is who
  * if a recompressed blob will not fit its slot, ABORT - never write a truncated member
  * refuses to overwrite its input; never touches canon/
  * after writing, re-opens the OUTPUT and asserts: parses, event count identical, owner present
    on the same team, and NO real-name bytes remain in ANY decompressed member (utf-8 or utf-16)
  * on any failure the output is deleted
"""
from __future__ import annotations

import argparse
import bz2
import json
import os
import struct
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from anonymise import MAP_PATH, extend_map, is_kept  # noqa: E402

import mpyq  # noqa: E402
from mpyq import MPQArchive  # noqa: E402

FLAG_COMPRESS = 0x00000200
FLAG_ENCRYPTED = 0x00010000
FLAG_SINGLE_UNIT = 0x01000000

METHOD_ZLIB = 0x02
METHOD_BZIP2 = 0x10

#: The name map for REPLAY intake - separate from anonymise.MAP_PATH (the text/JSON anonymiser's
#: map) since 2026-09-24. The library intake taught the shared map ~2,000 handles, among them
#: ordinary words and identifiers ("player", "max", "red"), which would have made a text `--apply`
#: rewrite prose and code across the repo. Replay members hold names as exact fields, so this map
#: may grow without limit; the text map stays the small, curated set. Both are gitignored
#: (`*.local.json`), and the text tool must never be pointed at this one.
REPLAY_MAP_PATH = HERE / "anon-map-replays.local.json"

ALIAS_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


class IntakeError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# aliases
# ---------------------------------------------------------------------------
def replay_alias(real: str, json_alias: str) -> bytes:
    """A byte-length-exact alias for `real`, derived from its JSON alias number.

    'P' + the alias number in fixed-width base 62, zero-padded on the LEFT to fill the handle's
    byte length exactly. Fixed width keeps every number unambiguous within a width, and the width
    itself keeps different byte lengths apart, so two players never share an in-replay alias.

    Until 2026-09-24 the scheme was 'Player18' or 'P18' padded with zeros on the right. It could
    not fit a 3-byte handle once the map passed 99 names - 198 of the 774 library replays failed -
    and 'P18'+'0' collided with 'P180' in a 4-byte slot. Capacity now: 62**(n-1) numbers for an
    n-byte handle, 3,844 for the 3-byte ones (the binding limit; the map holds ~2,000 names).
    Raises rather than truncating, and never quotes the handle.
    """
    n = len(real.encode("utf-8"))
    num = int(json_alias[len("Player"):])
    width = n - 1
    if width < 1 or num >= len(ALIAS_ALPHABET) ** width:
        raise IntakeError("cannot build a %d-byte alias for alias number %d" % (n, num))
    digits = []
    x = num
    while x:
        digits.append(ALIAS_ALPHABET[x % len(ALIAS_ALPHABET)])
        x //= len(ALIAS_ALPHABET)
    return ("P" + "".join(reversed(digits)).rjust(width, "0")).encode("ascii")


# ---------------------------------------------------------------------------
# MPQ helpers (read side is mpyq; write side is ours)
# ---------------------------------------------------------------------------
def decompress_unit(raw: bytes, size: int) -> bytes:
    if len(raw) == size:                      # stored, no method byte
        return raw
    m = raw[0]
    if m == METHOD_ZLIB:
        return zlib.decompress(raw[1:])
    if m == METHOD_BZIP2:
        return bz2.decompress(raw[1:])
    raise IntakeError("unsupported compression method 0x%02x" % m)


def recompress_to_slot(data: bytes, slot: int, stored: bool) -> bytes:
    """Return exactly `slot` bytes that decompress to `data`."""
    if stored:
        if len(data) != slot:
            raise IntakeError("stored member changed length - replacement was not same-length")
        return data
    # zlib first (what the round-trip proof used); bzip2 when zlib cannot match the slot - the game
    # wrote these members with bzip2, and on 6 of 774 library replays zlib came out 20-30% larger.
    sizes = {}
    for method, packed in ((METHOD_ZLIB, zlib.compress(data, 9)), (METHOD_BZIP2, bz2.compress(data, 9))):
        blob = bytes([method]) + packed
        sizes[method] = len(blob)
        if len(blob) <= slot:
            return blob + b"\x00" * (slot - len(blob))
    raise IntakeError("recompressed member does not fit its slot (%d): zlib %d, bzip2 %d"
                      % (slot, sizes[METHOD_ZLIB], sizes[METHOD_BZIP2]))


def members(archive: MPQArchive):
    """(name, block) for every named member."""
    for name in archive.files:
        h = archive.get_hash_table_entry(name)
        if h is None:
            continue
        yield name, archive.block_table[h.block_table_index]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def players_of(path: str):
    import sc2reader
    r = sc2reader.load_replay(path, load_level=2)
    return [(p.name, p.team_id) for p in r.players]


def anonymise_replay(src: Path, dst: Path, map_path: Path = REPLAY_MAP_PATH, keep: tuple = ()) -> dict:
    """`keep`: more handles kept as they are, besides the keep rule's (analyse_replay --player)."""
    if src.resolve() == dst.resolve():
        raise IntakeError("refusing to overwrite the input; write to a new path")
    if "canon" in dst.resolve().parts:
        raise IntakeError("refusing to write into canon/")

    # 1. who is in this replay, and what do we call them. The map is read and written at
    #    `map_path` - NOT the module default - so tests can run against an isolated copy.
    roster = players_of(str(src))
    if map_path.exists():
        m = json.loads(map_path.read_text(encoding="utf-8"))
    else:
        m = {"_note": "REAL PLAYER NAMES - do not publish. Gitignored.", "names": {}}
    extend_map(m, {n: None for n, _ in roster})
    map_path.parent.mkdir(parents=True, exist_ok=True)
    map_path.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")
    targets = {n: replay_alias(n, m["names"][n]) for n, _ in roster if not (is_kept(n) or n in keep)}
    kept = [n for n, _ in roster if is_kept(n) or n in keep]

    # 2. rewrite members in a copy
    buf = bytearray(src.read_bytes())
    a = MPQArchive(str(src))
    base = a.header["offset"]
    touched = []
    for name, b in members(a):
        if b.flags & FLAG_ENCRYPTED:
            raise IntakeError("%s is encrypted; not supported" % name.decode())
        stored = b.archived_size == b.size
        if not stored and not (b.flags & FLAG_SINGLE_UNIT):
            # multi-sector members would need a sector offset table rewrite; none seen in SC2
            # replays, and none of the name-bearing members are. Skip unless it holds a name.
            pass
        raw = bytes(buf[base + b.offset: base + b.offset + b.archived_size])
        try:
            data = decompress_unit(raw, b.size)
        except Exception:
            continue                          # not a name-bearing member we can read; leave it
        new = data
        hits = 0
        for real, alias in targets.items():
            rb = real.encode("utf-8")
            c = new.count(rb)
            if c:
                new = new.replace(rb, alias)
                hits += c
        if not hits:
            continue
        if not stored and not (b.flags & FLAG_SINGLE_UNIT):
            raise IntakeError("%s holds a name but is multi-sector; unsupported" % name.decode())
        blob = recompress_to_slot(new, b.archived_size, stored)
        buf[base + b.offset: base + b.offset + b.archived_size] = blob
        touched.append((name.decode(), hits, b.archived_size, len(blob)))

    assert len(buf) == src.stat().st_size, "file size must not change"
    dst.write_bytes(bytes(buf))

    # 3. verify the OUTPUT, not our intentions
    try:
        report = verify(src, dst, targets, kept, roster)
    except Exception:
        dst.unlink(missing_ok=True)
        raise
    report["touched_members"] = touched
    report["aliases"] = {k: v.decode() for k, v in targets.items()}
    return report


def verify(src: Path, dst: Path, targets: dict, kept: list, roster: list) -> dict:
    import sc2reader
    o = sc2reader.load_replay(str(src), load_level=4)
    n = sc2reader.load_replay(str(dst), load_level=4)

    if len(o.events) != len(n.events):
        raise IntakeError("event count changed: %d -> %d" % (len(o.events), len(n.events)))
    names_after = [p.name for p in n.players]
    for real in targets:
        if real in names_after:
            raise IntakeError("real name still visible in player list: %r" % real)
    for k in kept:
        if k not in names_after:
            raise IntakeError("kept handle disappeared: %r" % k)
    team_o = {p.name: p.team_id for p in o.players}
    team_n = {p.name: p.team_id for p in n.players}
    for k in kept:
        if team_o[k] != team_n[k]:
            raise IntakeError("%s changed team %s -> %s" % (k, team_o[k], team_n[k]))

    # no real-name BYTES anywhere in any decompressed member, either encoding
    a = MPQArchive(str(dst))
    leaks = []
    for name, b in members(a):
        try:
            data = a.read_file(name)
        except Exception:
            continue
        if not data:
            continue
        for real in targets:
            if real.encode("utf-8") in data or real.encode("utf-16-le") in data:
                leaks.append((name.decode(), real))
    if leaks:
        raise IntakeError("real-name bytes remain: %s" % leaks)

    return {
        "input": str(src), "output": str(dst),
        "events": len(n.events), "map": n.map_name, "length": str(n.length),
        "players_before": roster,
        "players_after": [(p.name, p.team_id) for p in n.players],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--map", default=str(REPLAY_MAP_PATH),
                    help="the replay name map (never the text anonymiser's map)")
    args = ap.parse_args()
    try:
        rep = anonymise_replay(Path(args.src), Path(args.dst), Path(args.map))
    except IntakeError as e:
        print("INTAKE FAILED: %s" % e, file=sys.stderr)
        return 2
    print(json.dumps(rep, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
