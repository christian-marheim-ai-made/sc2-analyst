"""The anonymiser: keep rule, whole-word safety, idempotence, reversibility.

EVERY FIXTURE IN THIS FILE IS SYNTHETIC. Not one real player handle appears.

That is deliberate and was learned the hard way: an earlier version of this file used the actual
handles, and running `anonymise.py --apply` rewrote its own test data. Assertions collapsed into
tautologies -- `word_pattern("X").sub("X", text)`, and a round-trip over an identity mapping -- while
still passing. A tool that edits text in place will edit the tests that test it.

The substring-collision case below preserves the real lesson with a synthetic handle: "act" sits
inside "tactical", exactly as the real three-letter handle sat inside "decisive" and "precision".
"""
from __future__ import annotations

import sys
import unittest

from fixtures import REPO

sys.path.insert(0, str(REPO / "tools"))
from anonymise import extend_map, is_alias, is_kept, word_pattern  # noqa: E402


class KeepRule(unittest.TestCase):
    """Owner ruling: keep "Player_1" and any handle containing "ally_2"; anonymise the rest."""

    def test_keeps_owner_handles(self):
        for n in ("Player_1", "player_1", "PLAYER_1", "Ally_2", "ally_2", "ally_2king"):
            with self.subTest(handle=n):
                self.assertTrue(is_kept(n))

    def test_the_demo_side_labels_are_kept(self):
        for n in ("Player_1", "Ally_2", "Ally_3", "Enemy_1", "enemy_3"):
            with self.subTest(label=n):
                self.assertTrue(is_kept(n))
        for n in ("Enemy_x", "Ally3", "Enemy_1b", "xPlayer_1"):
            with self.subTest(handle=n):
                self.assertFalse(is_kept(n))

    def test_anonymises_everyone_else(self):
        for n in ("Zephyr", "act", "QQQ", "someone", "Player_1dom_", "pleeb"):
            with self.subTest(handle=n):
                self.assertFalse(is_kept(n), "%r should not be kept" % n)


class WholeWordSafety(unittest.TestCase):

    def test_short_handle_does_not_corrupt_ordinary_words(self):
        """The case that made whole-word matching mandatory.

        Synthetic stand-in: handle "act" occurs inside "tactical" and "action".
        """
        text = "the tactical action was decisive; the player act was there"
        self.assertIn("tactical", text, "guard: collision word absent - test would be vacuous")

        out = word_pattern("act").sub("PlayerXX", text)

        self.assertIn("tactical", out, "substring inside 'tactical' was corrupted")
        self.assertIn("action", out, "substring inside 'action' was corrupted")
        self.assertIn("player PlayerXX was there", out, "the real handle was not replaced")

    def test_naive_substring_replace_would_have_corrupted_it(self):
        """Asserted so nobody 'simplifies' the regex back to str.replace."""
        text = "the tactical action"
        self.assertEqual(text.replace("act", "PlayerXX"),
                         "the tPlayerXXical PlayerXXion")          # what NOT to do
        self.assertEqual(word_pattern("act").sub("PlayerXX", text), text)

    def test_matches_at_boundaries_and_punctuation(self):
        p = word_pattern("Zephyr")
        for s, expect in (("Zephyr", True), ("Zephyr,", True), ("(Zephyr)", True),
                          ("'Zephyr'", True), ("said Zephyr.", True),
                          ("Zephyrus", False), ("xZephyr", False), ("Zephyr_2", False)):
            with self.subTest(sample=s):
                self.assertEqual(bool(p.search(s)), expect)


class Idempotence(unittest.TestCase):
    """Running the tool twice must be a no-op on the second run.

    REGRESSION, caught by a pre-commit scan 2026-09-23: it was not. `discover_names` re-reads
    files it has already anonymised, found an issued alias, decided it was not a kept handle, and
    issued it a SECOND alias. A second `--apply` would have double-anonymised everything and
    destroyed the map's reversibility.
    """

    def test_issued_aliases_are_recognised(self):
        for n in ("Player01", "Player18", "Player45", "Player9"):
            with self.subTest(alias=n):
                self.assertTrue(is_alias(n))
        for n in ("Player_1", "Ally_2", "Playerone", "Player", "PlayerX", "Zephyr"):
            with self.subTest(handle=n):
                self.assertFalse(is_alias(n))

    def test_second_pass_assigns_nothing(self):
        m = {"names": {}}
        first = extend_map(m, {"Zephyr": "P", "Quill": "P", "Player_1": "T"})
        self.assertEqual(len(first), 2, "expected two new aliases on the first pass")
        self.assertEqual(sorted(m["names"]), ["Quill", "Zephyr"])

        # second pass: the world now also contains the aliases just issued
        again = extend_map(m, {"Zephyr": "P", "Quill": "P", "Player_1": "T",
                               m["names"]["Zephyr"]: "P", m["names"]["Quill"]: "P"})
        self.assertEqual(again, [], "second pass issued new aliases: %s" % again)
        self.assertEqual(len(m["names"]), 2, "map grew on an idempotent run")

    def test_an_alias_never_becomes_a_key(self):
        m = {"names": {"Zephyr": "Player01"}}
        extend_map(m, {"Player01": "P"})
        self.assertNotIn("Player01", m["names"],
                         "a pseudonym was itself pseudonymised - reversal would break")


class Reversibility(unittest.TestCase):

    NAMES = {"Zephyr": "Player01", "Quill": "Player02", "act": "Player03"}

    def test_round_trip_restores_the_original(self):
        text = ("Zephyr and Quill fought; act watched. "
                "The tactical action was decisive for Zephyr.")

        fwd = text
        for real, alias in self.NAMES.items():
            fwd = word_pattern(real).sub(alias, fwd)

        self.assertNotIn("Zephyr", fwd)
        self.assertIn("tactical", fwd, "an ordinary word was corrupted on the way out")
        self.assertIn("Player01", fwd)

        back = fwd
        for real, alias in self.NAMES.items():
            back = word_pattern(alias).sub(real, back)
        self.assertEqual(back, text)

    def test_aliases_are_distinct(self):
        self.assertEqual(len(set(self.NAMES.values())), len(self.NAMES),
                         "two handles share an alias - reversal would be ambiguous")

    def test_mapping_is_not_an_identity(self):
        """Guard: an identity mapping would make the round-trip test pass while proving nothing."""
        for real, alias in self.NAMES.items():
            self.assertNotEqual(real, alias)


class ToolProtectsItsOwnFixtures(unittest.TestCase):
    """No test or tool file may contain a handle from the TEXT anonymiser's map, or `--apply`
    rewrites it (it did once: calibration row 9).

    The oracle is deliberately the small text map (`tools/anon-map.local.json`, the handles of the
    canon extracts) and not the replay map: since the library intake of 2026-09-24 the replay map
    holds ~2,000 handles, among them ordinary words and identifiers - "player", "max", "red" - so
    a whole-word scan against it flags every file in the repo. That is also why the two maps are
    separate files (`intake_replay.REPLAY_MAP_PATH`), and why the text map must stay small.
    """

    def test_no_text_map_handle_appears_in_any_test_or_tool_file(self):
        import json
        import re

        map_path = REPO / "tools/anon-map.local.json"
        if not map_path.exists():
            self.skipTest("no local name map on this checkout")
        real = json.loads(map_path.read_text(encoding="utf-8"))["names"]
        self.assertGreater(len(real), 0, "empty name map - this test would be vacuous")
        self.assertLess(len(real), 100, "the text map has grown into a replay map - see docstring")

        found = []
        for f in sorted((REPO / "tests").glob("*.py")) + sorted((REPO / "tools").glob("*.py")):
            src = f.read_text(encoding="utf-8")
            for r in real:
                if re.search(r"(?<![0-9A-Za-z_])%s(?![0-9A-Za-z_])" % re.escape(r), src):
                    found.append((f.name, len(r)))              # file and length, never the handle
        self.assertEqual(found, [], "text-map handles in code or fixtures (file, length): %s" % found)


if __name__ == "__main__":
    unittest.main(verbosity=2)
