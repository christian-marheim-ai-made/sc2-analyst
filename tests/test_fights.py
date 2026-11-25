"""S1 · the fight segmenter (DL-1): the gap rule, the observable, the mirror, stability on a twin."""
from __future__ import annotations

import sys
import unittest

from fixtures import LIBRARY_TWINS, REPO

sys.path.insert(0, str(REPO / "src"))

from sc2analyst import fights  # noqa: E402


def death(t, team=1, pid=1, kpid=2, x=50, y=50, value=100):
    return {"t": t, "x": x, "y": y, "pid": pid, "team": team, "kpid": kpid, "type": "Marine",
            "ktype": "Zealot", "value": value, "kinds": ["army"]}


def unit(died, owner, kpid, value=100, x=50, y=50, typ="Marine"):
    return {"died": died, "owner": owner, "kpid": kpid, "dx": x, "dy": y, "m": value, "g": 0,
            "types": [[0, typ]], "ktype": "Zealot", "kinds": ["army"]}


GAME = {"players": [{"pid": 1, "team": 1}, {"pid": 2, "team": 1}, {"pid": 3, "team": 2}],
        "units": [unit(100, 1, 3), unit(105, 3, 1, value=300), unit(108, 1, None),  # morph
                  unit(109, 1, 1),  # self-destruct
                  unit(112, 2, 1),  # ally kill
                  unit(200, 3, 2, value=50)]}


class GapRule(unittest.TestCase):
    def test_ten_seconds_keeps_the_fight(self):
        runs = fights.segment([death(0), death(5), death(15)])
        self.assertEqual([len(r) for r in runs], [3])

    def test_more_than_ten_seconds_ends_it(self):
        runs = fights.segment([death(0), death(10), death(20.01)])
        self.assertEqual([len(r) for r in runs], [2, 1])

    def test_a_lone_death_is_a_fight_of_one(self):
        self.assertEqual([len(r) for r in fights.segment([death(0), death(30)])], [1, 1])


class Observable(unittest.TestCase):
    def test_only_kills_across_teams_count(self):
        d = fights.combat_deaths(GAME)
        self.assertEqual([x["t"] for x in d], [100, 105, 200])

    def test_morphs_and_self_kills_do_not_bridge_a_gap(self):
        # 105 -> 200 has a morph at 108, a self kill at 109 and an ally kill at 112 between:
        # none is fighting, so the fight at 100-105 ends and 200 is a new one
        f = fights.fights(GAME)
        self.assertEqual([(x["start"], x["end"]) for x in f], [(100, 105), (200, 200)])

    def test_categories_account_for_every_death(self):
        c = fights.death_categories(GAME)
        self.assertEqual(c, {"enemy": 3, "no_killer": 1, "self": 1, "ally": 1,
                             "neutral_victim": 0, "other": 0})


class Mirror(unittest.TestCase):
    """DL-5: what one team lost in a fight is exactly what the other killed."""

    def test_lost_equals_killed(self):
        for f in fights.fights(GAME):
            lost = sum(f["lost_value_by_team"].values())
            killed = sum(p["killed_value"] for p in f["by_player"].values())
            self.assertEqual(lost, killed)
            self.assertEqual(sum(p["deaths"] for p in f["by_player"].values()),
                             sum(p["kills"] for p in f["by_player"].values()))


class Places(unittest.TestCase):
    def test_two_places_are_reported_not_split(self):
        f = fights.fights({"players": GAME["players"],
                           "units": [unit(10, 1, 3, x=10, y=10), unit(11, 1, 3, x=12, y=10),
                                     unit(12, 3, 1, x=150, y=150), unit(13, 3, 1, x=151, y=152)]})
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["places"], 2)


@unittest.skipUnless((LIBRARY_TWINS / "10000 Feet LE.SC2Replay").exists(), "twins on L: absent")
class RealTwin(unittest.TestCase):
    def test_two_runs_give_the_same_fights(self):
        from sc2analyst import adapter_sc2
        a = fights.fights(adapter_sc2.load_game(LIBRARY_TWINS / "10000 Feet LE.SC2Replay"))
        b = fights.fights(adapter_sc2.load_game(LIBRARY_TWINS / "10000 Feet LE.SC2Replay"))
        self.assertEqual(a, b)
        self.assertGreaterEqual(len(a), 2)
        for f in a:
            self.assertEqual(sum(f["lost_value_by_team"].values()),
                             sum(p["killed_value"] for p in f["by_player"].values()))


if __name__ == "__main__":
    unittest.main()
