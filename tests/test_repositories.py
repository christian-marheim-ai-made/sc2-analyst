"""S4 · the all-HAVE repositories and the mirror (DL-5)."""
from __future__ import annotations

import sys
import unittest

from fixtures import LIBRARY_TWINS, REPO

sys.path.insert(0, str(REPO / "src"))

from sc2analyst import repositories as R  # noqa: E402


def unit(tag, owner, died=None, kpid=None, ktype=None, m=100, typ="Marine", kinds=None, born=10.0,
         how="born", x=50, y=50):
    return {"tag": tag, "owner": owner, "died": died, "kpid": kpid, "ktype": ktype, "dx": x, "dy": y,
            "m": m, "g": 0, "vk": True, "types": [[born, typ]], "kinds": kinds or ["army"],
            "born": born, "done": born, "how": how, "x": x, "y": y, "ctag": None}


GAME = {"map": "nowhere", "end": 300.0,
        "players": [{"pid": 1, "team": 1, "race": "Terran"}, {"pid": 2, "team": 2, "race": "Zerg"}],
        "units": [unit(1, 1, died=100, kpid=2, ktype="Roach"), unit(2, 1, died=101, kpid=2, ktype="Roach"),
                  unit(3, 2, died=102, kpid=1, ktype="Marine", m=75, typ="Roach"),
                  unit(4, 1, died=150, kpid=None),  # a morph: no trade
                  unit(5, 2, born=100.5, typ="Roach")],
        "stats": [{"pid": 1, "t": 0, "m_rate": 600, "g_rate": 0, "workers": 12},
                  {"pid": 1, "t": 60, "m_rate": 600, "g_rate": 0, "workers": 22}],
        "positions": [], "commands": [], "upgrades": [], "camera": []}


class Trade(unittest.TestCase):
    def test_values_and_ratio(self):
        t = R.trade(GAME)
        self.assertEqual((t["1"]["lost"], t["1"]["killed"], t["1"]["ratio"]), (200, 75, 0.38))
        self.assertEqual(t["1"]["income"], 600)

    def test_mirror_holds(self):
        self.assertEqual(R.mirror_check(GAME), [])

    def test_mirror_catches_a_one_sided_ledger(self):
        broken = dict(GAME, units=GAME["units"] + [unit(9, 2, died=103, kpid=1)])
        # consistent again: both ledgers see it - so break the killer's ledger on purpose
        original = R.trade

        def one_sided(game):
            t = original(game)
            t["1"]["killed_of"]["2"] -= 1
            return t
        R.trade = one_sided
        try:
            self.assertTrue(R.mirror_check(broken))
        finally:
            R.trade = original


class Reinforcement(unittest.TestCase):
    def test_completed_during_the_fight(self):
        g = dict(GAME, units=GAME["units"] + [unit(10 + i, 1, died=100 + i * 0.1, kpid=2) for i in range(5)])
        r = R.reinforcement(g)
        self.assertEqual(r[0]["completed_during"], {"2": {"units": 1, "value": 100}})


class DeathCauses(unittest.TestCase):
    def test_what_killed_my_stuff(self):
        d = R.death_causes(GAME)["1"]
        self.assertEqual(d["by_killer_type"], [["Roach", 2, 200]])


@unittest.skipUnless((LIBRARY_TWINS / "10000 Feet LE.SC2Replay").exists(), "twins on L: absent")
class RealTwin(unittest.TestCase):
    def test_mirror_on_a_real_game_and_d7(self):
        from sc2analyst import adapter_sc2, fights
        g = adapter_sc2.load_game(LIBRARY_TWINS / "10000 Feet LE.SC2Replay")
        self.assertEqual(R.mirror_check(g), [])
        # D-7: a combat death names the killing unit's type on this build
        d = fights.combat_deaths(g)
        self.assertGreater(sum(1 for x in d if x["ktype"]) / len(d), 0.95)


if __name__ == "__main__":
    unittest.main()
