"""W4 · openings and attack fights (DL-26), on a synthetic map: two mains, two naturals, one open base."""
from __future__ import annotations

import sys
import unittest
from unittest import mock

from fixtures import REPO

sys.path.insert(0, str(REPO / "src"))

from sc2analyst import openings as O, repositories as R, terrain as TR  # noqa: E402

FEATURES = {"resource_clusters": [
    {"kind": "base", "id": 1, "center": {"x": 20, "y": 20}},     # P1's main
    {"kind": "base", "id": 2, "center": {"x": 40, "y": 20}},     # P1's natural
    {"kind": "base", "id": 3, "center": {"x": 120, "y": 120}},   # P2's main
    {"kind": "base", "id": 4, "center": {"x": 100, "y": 120}},   # P2's natural
    {"kind": "base", "id": 5, "center": {"x": 70, "y": 70}}],
    "ramps": [], "xelnaga_towers": [], "mineral_walls": []}
TH = {"fe_threshold_s": {"Zerg": {"1v1": 55.0}, "Terran": {"1v1": 70.0}}, "attack_min": 300, "cut_s": 480.0}


def hall(tag, owner, x, y, born, types=None, how="init"):
    t0 = types[0][1] if types else ("CommandCenter" if owner == 1 else "Hatchery")
    return {"tag": tag, "owner": owner, "x": x, "y": y, "born": born, "done": born + 70 if how == "init" else born,
            "died": None, "how": how, "kinds": ["structure", "townhall"], "m": 400, "g": 0, "vk": True,
            "types": types or [[born, t0]], "kpid": None, "dx": None, "dy": None}


def victim(tag, owner, t, kpid, x, y, value=100):
    return {"tag": tag, "owner": owner, "x": x, "y": y, "born": 1.0, "done": 1.0, "died": t, "how": "born",
            "kinds": ["army"], "m": value, "g": 0, "vk": True, "types": [[1.0, "Zergling"]], "kpid": kpid,
            "ktype": "Marine", "dx": x, "dy": y}


def game(units, commands=()):
    return {"map": "Synthetic", "end": 600.0,
            "players": [{"pid": 1, "team": 1, "race": "Terran"}, {"pid": 2, "team": 2, "race": "Zerg"}],
            "units": [hall(100, 1, 20, 20, 0.0, how="born"), hall(200, 2, 120, 120, 0.0, how="born")] + list(units),
            "commands": list(commands), "stats": [], "positions": [], "upgrades": [], "owner_changes": []}


class Openings(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(R, "map_features", return_value=FEATURES)
        p.start()
        self.addCleanup(p.stop)

    def test_a_hall_built_at_a_new_base_is_an_expansion_from_its_start(self):
        h = O.town_halls(game([hall(201, 2, 100, 120, 50.0)]))[2]
        self.assertEqual([(x["kind"], x["started"], x["via"]) for x in h],
                         [("start", 0.0, None), ("expansion", 50.0, "started")])

    def test_a_hall_built_in_the_main_and_flown_to_the_natural_is_an_expansion(self):
        cc = hall(101, 1, 22, 21, 60.0, types=[[60.0, "CommandCenter"], [150.0, "CommandCenterFlying"],
                                                [170.0, "CommandCenter"]])
        g = game([cc], commands=[[165.0, 1, "LandCommandCenter", 40.0, 20.0]])
        exp = O.town_halls(g)[1][1]
        self.assertEqual((exp["kind"], exp["started"], exp["landed"], exp["via"]), ("expansion", 60.0, 170.0, "landed"))

    def test_the_landing_spot_holds_the_natural_in_the_map_frame(self):
        cc = hall(101, 1, 22, 21, 60.0, types=[[60.0, "CommandCenter"], [150.0, "CommandCenterFlying"],
                                                [170.0, "CommandCenter"]])
        frame = TR.Frame(game([cc], commands=[[165.0, 1, "LandCommandCenter", 40.0, 20.0]]))
        natural = FEATURES["resource_clusters"][1]
        self.assertIsNone(frame.holder(natural, 160.0))
        self.assertEqual(frame.holder(natural, 200.0), 1)

    def test_a_hall_at_no_base_is_a_macro_hall(self):
        h = O.town_halls(game([hall(202, 2, 60, 100, 200.0)]))[2]
        self.assertEqual(h[1]["kind"], "macro")

    def test_kills_at_the_enemys_natural_before_expanding_make_a_one_base_attack(self):
        g = game([hall(201, 2, 100, 120, 50.0)] +
                 [victim(300 + i, 2, 130.0 + i, 1, 101, 121, value=100) for i in range(4)])
        c = O.classify(g, TH)
        self.assertEqual((c[1]["class"], c[1]["attack_value"], c[1]["first_attack"]), ("ONE-BASE ATTACK", 400, 130.0))

    def test_kills_at_your_own_base_are_defence_not_attack(self):
        g = game([victim(300 + i, 2, 130.0 + i, 1, 21, 21) for i in range(4)])
        self.assertEqual(O.classify(g, TH)[1]["attack_value"], 0)

    def test_fast_standard_and_no_expansion(self):
        fast = O.classify(game([hall(201, 2, 100, 120, 50.0)]), TH)[2]["class"]
        late = O.classify(game([hall(201, 2, 100, 120, 200.0)]), TH)[2]["class"]
        none = O.classify(game([]), TH)[2]["class"]
        self.assertEqual((fast, late, none), ("FAST EXPAND", "STANDARD", "NO EXPANSION"))

    def test_an_attack_after_the_expansion_does_not_make_a_one_base_attack(self):
        g = game([hall(102, 1, 40, 20, 60.0)] + [victim(300 + i, 2, 130.0 + i, 1, 101, 121) for i in range(4)])
        c = O.classify(g, TH)[1]
        self.assertEqual((c["class"], c["attack_value"]), ("FAST EXPAND", 0))


if __name__ == "__main__":
    unittest.main()
