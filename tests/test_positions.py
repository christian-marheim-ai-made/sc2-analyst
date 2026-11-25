"""S2 · every position carries its age and location_confidence."""
from __future__ import annotations

import sys
import unittest

from fixtures import REPO

sys.path.insert(0, str(REPO / "src"))

from sc2analyst import positions as P  # noqa: E402

GAME = {"units": [
    {"tag": 1, "born": 10.0, "x": 5, "y": 5, "died": 100.0, "dx": 50, "dy": 50, "kinds": ["army"]},
    {"tag": 2, "born": 0.0, "x": 9, "y": 9, "died": None, "dx": None, "dy": None,
     "kinds": ["structure"]}],
    "positions": [[40.0, 1, 20, 20], [40.0, 3, 1, 1], [55.0, 1, 30, 30]]}


class Confidence(unittest.TestCase):
    def setUp(self):
        self.p = P.Positions(GAME)

    def test_before_birth_is_none(self):
        self.assertEqual(self.p.at(1, 5.0)["location_confidence"], "none")

    def test_at_a_sample_is_exact(self):
        r = self.p.at(1, 40.0)
        self.assertEqual((r["x"], r["location_confidence"], r["source"]), (20, "exact", "sample"))

    def test_fresh_then_stale(self):
        self.assertEqual(self.p.at(1, 50.0)["location_confidence"], "fresh")
        r = self.p.at(1, 55.0 + P.FRESH_S + 1)
        self.assertEqual((r["location_confidence"], r["age"]), ("stale", P.FRESH_S + 1))

    def test_death_point_is_exact(self):
        r = self.p.at(1, 100.0)
        self.assertEqual((r["x"], r["location_confidence"]), (50, "exact"))

    def test_a_structure_is_exact_all_its_life(self):
        self.assertEqual(self.p.at(2, 900.0)["location_confidence"], "exact")

    def test_every_answer_carries_the_two_fields(self):
        for tag in (1, 2, 3, 99):
            r = self.p.at(tag, 60.0)
            self.assertIn("age", r)
            self.assertIn("location_confidence", r)

    def test_intervals(self):
        self.assertEqual(P.sample_intervals(GAME), [15.0])


if __name__ == "__main__":
    unittest.main()
