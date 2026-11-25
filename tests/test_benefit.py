"""DL-29 · which team benefited from a fight: standing now, plus five minutes of the income gap."""
from __future__ import annotations

import sys
import unittest

from fixtures import REPO

sys.path.insert(0, str(REPO / "src"))

from sc2analyst import benefit as B  # noqa: E402

GAME = {"players": [{"pid": 1, "team": 1}, {"pid": 2, "team": 2}]}


def rows(standing_1, standing_2, income_1, income_2):
    """Samples every 10 s from 0 to 300; each argument is a function of t."""
    out = {1: [], 2: []}
    for t in range(0, 301, 10):
        out[1].append((float(t), {"standing": standing_1(t), "income": income_1(t)}))
        out[2].append((float(t), {"standing": standing_2(t), "income": income_2(t)}))
    return out


FIGHT = {"start": 100.0, "end": 120.0, "lost_value_by_team": {"1": 800, "2": 300}}


class Benefit(unittest.TestCase):
    def test_a_team_that_lost_more_value_can_still_benefit_through_the_economy(self):
        # team 1 loses 500 more standing in the fight, but team 2's income falls by 200 a minute
        r = rows(lambda t: 5000 - (500 if t >= 120 else 0), lambda t: 5000,
                 lambda t: 1000, lambda t: 1000 - (200 if t > 120 else 0))
        b = B.fight_benefit(GAME, r, FIGHT, 1, 2)
        self.assertEqual(b["dN"], -500)
        self.assertEqual(b["dr"], 200)
        self.assertEqual(b["benefit_5min"], -500 + 5 * 200)
        self.assertEqual((b["benefited"], b["lost_less"], b["disagree"]), (1, 2, True))

    def test_no_economic_change_means_the_value_decides_and_they_agree(self):
        r = rows(lambda t: 5000 - (500 if t >= 120 else 0), lambda t: 5000, lambda t: 900, lambda t: 900)
        b = B.fight_benefit(GAME, r, FIGHT, 1, 2)
        self.assertEqual((b["dr"], b["benefited"], b["lost_less"], b["disagree"]), (0, 2, 2, False))

    def test_the_fight_the_game_ends_in_gets_its_verdict_at_the_games_end(self):
        # the owner's ruling of 2026-10-29: never "not known" because the game ended
        last = dict(FIGHT, start=260.0, end=305.0, lost_value_by_team={"1": 300, "2": 900})
        r = rows(lambda t: 5000 - (800 if t >= 280 else 0), lambda t: 5000,
                 lambda t: 900, lambda t: 900 - (300 if t >= 280 else 0))
        b = B.fight_benefit(GAME, r, last, 1, 2)
        self.assertTrue(b["at_end"])
        self.assertEqual(b["after_t"], 300.0)                 # the game's last sample
        self.assertEqual(b["dN"], -800)
        self.assertIsNotNone(b["benefit_5min"])
        self.assertEqual(b["verdict"], "team")

    def test_exactly_even_is_a_verdict_not_an_unknown(self):
        r = rows(lambda t: 5000, lambda t: 5000, lambda t: 900, lambda t: 900)
        b = B.fight_benefit(GAME, r, FIGHT, 1, 2)
        self.assertEqual((b["verdict"], b["benefited"]), ("even", None))

    def test_unknown_only_without_a_single_common_sample(self):
        b = B.fight_benefit(GAME, {1: [], 2: []}, FIGHT, 1, 2)
        self.assertEqual(b["verdict"], "unknown")


if __name__ == "__main__":
    unittest.main()
