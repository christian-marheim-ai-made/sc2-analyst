"""Intake step 1: anonymising a .SC2Replay in place.

The round-trip tests re-check the OUTPUT independently - they do not trust the tool's own verify().
Fixtures are synthetic wherever a name is needed; the real replay's real names never appear here
(see test_anonymiser.py for why).

The test runs against an ISOLATED copy of the name map, never the real one, because
anonymise_replay extends and saves whatever map it is given.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from fixtures import (EXTRACTOR_VER3, REF_REPLAY_EXTRACT, REPLAYS, REPO, count_events, load, need)

sys.path.insert(0, str(REPO / "tools"))
from intake_replay import (IntakeError, anonymise_replay, decompress_unit,  # noqa: E402
                           recompress_to_slot, replay_alias)
from anonymise import MAP_PATH, is_kept  # noqa: E402

SRC_REPLAY = REPLAYS / "andre_gamet.SC2Replay"


def _load_ver3():
    spec = importlib.util.spec_from_file_location("ver3", need(EXTRACTOR_VER3))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RoundTrip(unittest.TestCase):
    """Anonymise a real replay once, then inspect the result from several angles."""

    @classmethod
    def setUpClass(cls):
        need(SRC_REPLAY)
        cls.tmp = Path(tempfile.mkdtemp())
        cls.map = cls.tmp / "map.local.json"
        if Path(MAP_PATH).exists():
            shutil.copy(MAP_PATH, cls.map)           # isolated copy - real map untouched
        cls.out = cls.tmp / "anon.SC2Replay"
        cls.report = anonymise_replay(SRC_REPLAY, cls.out, cls.map)
        import sc2reader
        cls.orig = sc2reader.load_replay(str(SRC_REPLAY), load_level=4)
        cls.anon = sc2reader.load_replay(str(cls.out), load_level=4)
        cls.targets = [n for n, _ in cls.report["players_before"] if not is_kept(n)]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_source_has_someone_to_anonymise(self):
        """Non-vacuity guard: a replay with only kept handles would make every test below trivial."""
        self.assertGreater(len(self.targets), 0)

    def test_output_is_byte_for_byte_same_size(self):
        self.assertEqual(self.out.stat().st_size, SRC_REPLAY.stat().st_size)

    def test_event_count_identical(self):
        self.assertEqual(len(self.orig.events), len(self.anon.events))
        self.assertGreater(len(self.anon.events), 1000, "suspiciously few events parsed")

    def test_map_and_length_unchanged(self):
        self.assertEqual(self.orig.map_name, self.anon.map_name)
        self.assertEqual(str(self.orig.length), str(self.anon.length))

    def test_no_real_name_in_player_list(self):
        after = [p.name for p in self.anon.players]
        for real in self.targets:
            self.assertNotIn(real, after)

    def test_owner_kept_on_the_same_team(self):
        """Owner ruling: Player_1 and his team must always be identifiable."""
        before = {p.name: p.team_id for p in self.orig.players}
        after = {p.name: p.team_id for p in self.anon.players}
        self.assertIn("Player_1", after)
        self.assertEqual(before["Player_1"], after["Player_1"])
        for k in before:
            if is_kept(k):
                self.assertEqual(before[k], after.get(k), "kept handle %s lost its team" % k)

    def test_team_structure_preserved(self):
        """Same team sizes, same team ids - only the labels changed."""
        def teams(r):
            t = {}
            for p in r.players:
                t.setdefault(p.team_id, 0)
                t[p.team_id] += 1
            return t
        self.assertEqual(teams(self.orig), teams(self.anon))

    def test_no_real_name_bytes_in_any_decompressed_member(self):
        """Independent of the tool's own verify: walk every member ourselves, both encodings."""
        from mpyq import MPQArchive
        a = MPQArchive(str(self.out))
        leaks = []
        for name in a.files:
            try:
                data = a.read_file(name)
            except Exception:
                continue
            if not data:
                continue
            for real in self.targets:
                if real.encode("utf-8") in data or real.encode("utf-16-le") in data:
                    leaks.append((name, real))
        self.assertEqual(leaks, [])

    def test_aliases_are_length_exact_and_distinct(self):
        aliases = self.report["aliases"]
        self.assertEqual(len(set(aliases.values())), len(aliases), "alias collision")
        for real, alias in aliases.items():
            self.assertEqual(len(alias.encode("ascii")), len(real.encode("utf-8")), real)

    def test_extractor_produces_identical_structure_from_the_anonymised_replay(self):
        """The whole point: step 2 must not be able to tell the difference."""
        ref = load(REF_REPLAY_EXTRACT)
        mine = _load_ver3().Sc2ReplayRepositoryMvp(focus_player_name="ally_2") \
                           .extract(str(self.out))
        for repo in ("raw_information", "attention_information", "financial_information",
                     "scout_information", "strategical_information", "tactical_information"):
            with self.subTest(repository=repo):
                self.assertEqual(count_events(mine, repo), count_events(ref, repo))
        names = [p["name"] for p in mine["metadata"]["players"]]
        for real in self.targets:
            self.assertNotIn(real, names)
        self.assertIn("Player_1", names)

    def test_isolated_map_was_extended_not_the_real_one(self):
        self.assertTrue(self.map.exists())
        import json
        m = json.loads(self.map.read_text(encoding="utf-8"))["names"]
        for real in self.targets:
            self.assertIn(real, m)


class RefusesUnsafeWrites(unittest.TestCase):

    def test_refuses_to_overwrite_its_input(self):
        need(SRC_REPLAY)
        with self.assertRaises(IntakeError):
            anonymise_replay(SRC_REPLAY, SRC_REPLAY, Path(tempfile.mkdtemp()) / "m.json")

    def test_refuses_to_write_into_canon(self):
        need(SRC_REPLAY)
        with self.assertRaises(IntakeError):
            anonymise_replay(SRC_REPLAY, REPO / "canon/replays/should_never_exist.SC2Replay",
                             Path(tempfile.mkdtemp()) / "m.json")
        self.assertFalse((REPO / "canon/replays/should_never_exist.SC2Replay").exists())


class Aliases(unittest.TestCase):
    """Byte-length-exact, derived from the JSON alias number. All names synthetic."""

    def test_exact_byte_length(self):
        cases = [("Zephyr", "Player07"), ("Q" * 12, "Player20"), ("Abc", "Player01"),
                 ("Зефир", "Player23")]        # 5 Cyrillic chars = 10 bytes
        for real, ja in cases:
            with self.subTest(real=real):
                al = replay_alias(real, ja)
                self.assertEqual(len(al), len(real.encode("utf-8")))
                self.assertTrue(al.isascii())

    def test_fixed_width_base62_left_padded(self):
        """Contract since 2026-09-24 (DL-15): 'P' + the number in base 62, left-padded with zeros
        to the handle's byte length. The right-padded 'Player20' / 'P07' form it replaces could
        not fit a 3-byte handle past 99 names and collided across numbers (198 of 774 replays)."""
        self.assertEqual(replay_alias("Q" * 12, "Player20"), b"P0000000000K")     # 20 -> K
        self.assertEqual(replay_alias("Zephyr", "Player07"), b"P00007")
        self.assertEqual(replay_alias("Abc", "Player100"), b"P1c")                # 100 = 1*62 + 38

    def test_too_short_or_over_capacity_raises(self):
        with self.assertRaises(IntakeError):
            replay_alias("A", "Player07")            # a 1-byte handle has no room for a number
        with self.assertRaises(IntakeError):
            replay_alias("Ab", "Player62")           # a 2-byte slot holds numbers below 62


class Recompression(unittest.TestCase):

    def test_padded_zlib_roundtrips(self):
        data = b"the quick brown fox " * 50
        blob = recompress_to_slot(data, slot=400, stored=False)
        self.assertEqual(len(blob), 400)
        self.assertEqual(blob[0], 0x02)
        self.assertEqual(decompress_unit(blob, len(data)), data)

    def test_refuses_to_overflow_the_slot(self):
        incompressible = os.urandom(4000)
        with self.assertRaises(IntakeError):
            recompress_to_slot(incompressible, slot=100, stored=False)

    def test_stored_member_must_keep_its_length(self):
        with self.assertRaises(IntakeError):
            recompress_to_slot(b"abc", slot=5, stored=True)
        self.assertEqual(recompress_to_slot(b"abcde", slot=5, stored=True), b"abcde")


if __name__ == "__main__":
    unittest.main(verbosity=2)
