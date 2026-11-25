"""Intake at library scale: two defects the 774-replay batch of 2026-09-24 exposed.

198 replays failed with "cannot build a N-byte alias": the in-replay alias was 'Player18' or 'P18'
padded with zeros on the right, which cannot fit a 3-byte handle once the name map passes 99
entries (it holds ~2,000 after the batch), and 'P18'+'0' collided with 'P180' in a 4-byte slot.
6 replays failed on slot fit: their name-bearing members were bzip2-compressed by the game, and
zlib at level 9 could not match the original size.

All fixtures synthetic. No handle from the real name map appears here (the guard in
test_anonymiser.py checks every test and tool file).
"""
from __future__ import annotations

import bz2
import random
import sys
import unittest
import zlib

from fixtures import REPO

sys.path.insert(0, str(REPO / "tools"))
from intake_replay import (IntakeError, decompress_unit, recompress_to_slot,  # noqa: E402
                           replay_alias)


class AliasScheme(unittest.TestCase):
    def test_fits_every_byte_length_from_three_up_for_large_numbers(self):
        for n in (3, 4, 5, 8, 12):
            handle = "x" * n
            for num in (1, 18, 99, 100, 180, 1980, 3843):
                a = replay_alias(handle, "Player%d" % num)
                self.assertEqual(len(a), n, (n, num, a))
                self.assertTrue(a.isascii() and a.isalnum(), a)
                self.assertTrue(a.startswith(b"P"), a)

    def test_non_ascii_handle_gets_a_byte_length_alias(self):
        handle = "abé"                       # three characters, four UTF-8 bytes
        self.assertEqual(len(replay_alias(handle, "Player7")), 4)

    def test_no_two_numbers_share_an_alias_within_a_width(self):
        seen = {}
        for num in range(1, 3844):
            a = replay_alias("abc", "Player%d" % num)
            self.assertNotIn(a, seen, (num, seen.get(a)))
            seen[a] = num
        self.assertNotEqual(replay_alias("abcd", "Player18"), replay_alias("wxyz", "Player180"),
                            "the old right-padding collision is back")

    def test_same_handle_same_number_same_alias(self):
        self.assertEqual(replay_alias("abcd", "Player42"), replay_alias("abcd", "Player42"))

    def test_over_capacity_raises_without_quoting_the_handle(self):
        with self.assertRaises(IntakeError) as cm:
            replay_alias("abc", "Player3844")
        self.assertNotIn("abc", str(cm.exception))


class SlotFit(unittest.TestCase):
    @staticmethod
    def _text():
        rnd = random.Random(20260924)
        words = ["replay", "player", "zerg", "terran", "protoss", "siege", "tank", "marine",
                 "stim", "storm", "nexus", "hatchery", "upgrade", "armor", "shield", "chrono"]
        return " ".join(rnd.choice(words) for _ in range(40000)).encode("ascii")

    def test_falls_back_to_bzip2_when_zlib_does_not_fit(self):
        data = self._text()
        z = len(zlib.compress(data, 9)) + 1
        b = len(bz2.compress(data, 9)) + 1
        if b >= z:
            self.skipTest("bzip2 is not smaller than zlib on this synthetic text")
        blob = recompress_to_slot(data, b, stored=False)          # zlib needs z > b bytes
        self.assertEqual(len(blob), b)
        self.assertEqual(blob[0], 0x10, "expected the bzip2 method byte")
        self.assertEqual(decompress_unit(blob, len(data)), data)

    def test_zlib_still_preferred_when_it_fits(self):
        data = self._text()
        z = len(zlib.compress(data, 9)) + 1
        blob = recompress_to_slot(data, z + 50, stored=False)
        self.assertEqual(len(blob), z + 50)
        self.assertEqual(blob[0], 0x02)
        self.assertEqual(decompress_unit(blob, len(data)), data)

    def test_raises_when_neither_fits(self):
        data = self._text()
        smallest = min(len(zlib.compress(data, 9)), len(bz2.compress(data, 9)))
        with self.assertRaises(IntakeError):
            recompress_to_slot(data, smallest - 10, stored=False)


if __name__ == "__main__":
    unittest.main()
