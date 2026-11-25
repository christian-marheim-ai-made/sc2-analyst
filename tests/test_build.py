"""S3 · build order, idle production, the larva bank, supply blocks."""
from __future__ import annotations

import sys
import unittest

from fixtures import LIBRARY_TWINS, REPO

sys.path.insert(0, str(REPO / "src"))

from sc2analyst import build as B  # noqa: E402


def u(tag, typ, owner=1, born=0.0, how="born", done=None, died=None, ctag=None, kinds=None,
      types=None):
    return {"tag": tag, "type": typ, "owner": owner, "born": born, "how": how,
            "done": born if done is None and how == "born" else done, "died": died, "kpid": None,
            "ctag": ctag, "cab": None, "m": 50, "g": 0, "kinds": kinds or ["army"],
            "types": types or [[born, typ]], "x": 0, "y": 0, "dx": None, "dy": None}


BT = {"Marine": {"seconds": 18}, "SCV": {"seconds": 12}}


class Idle(unittest.TestCase):
    def game(self):
        rax = u(10, "Barracks", how="init", born=50, done=100, kinds=["structure", "producer"])
        return {"end": 200.0, "stats": [], "upgrades": [], "commands": [],
                "units": [rax, u(11, "Marine", born=118, ctag=10), u(12, "Marine", born=136, ctag=10),
                          u(13, "Marine", born=190, ctag=10)]}

    def test_idle_is_life_not_covered_by_training(self):
        r = B.idle_production(self.game(), 1, BT)[0]
        # life 100..200 = 100 s; busy 100-136 (two back to back) and 172-190 = 54 s
        self.assertEqual(r["idle_s"], 46.0)
        self.assertEqual(r["longest_idle"], [136.0, 172.0])

    def test_a_child_with_no_build_time_is_counted_apart(self):
        r = B.idle_production(self.game(), 1, {})[0]
        self.assertEqual((r["children_without_build_time"], r["idle_s"]), (3, 100.0))


class Larva(unittest.TestCase):
    def test_three_or_more_banked_and_hatcheries_are_not_producers(self):
        hatch = u(1, "Hatchery", kinds=["structure", "townhall", "larva_producer"])
        lv = [u(100 + i, "Larva", born=float(i * 10), ctag=1, kinds=["transient"],
                types=[[float(i * 10), "Larva"]] + ([[60.0, "Egg"]] if i == 0 else []))
              for i in range(4)]
        g = {"end": 100.0, "units": [hatch] + lv, "stats": []}
        r = B.larva_bank(g, 1)[0]
        # 3 alive from 20 to 60 (the first used at 60), back to 3 at 30: 20..100 >=3 except ...
        # alive counts: 0-10:1, 10-20:2, 20-30:3, 30-60:4, 60-100:3 -> banked 80 s
        self.assertEqual(r["banked_s"], 80.0)
        self.assertEqual(B.idle_production(g, 1, BT), [])


class Supply(unittest.TestCase):
    def test_blocks_span_their_samples(self):
        st = [{"pid": 1, "t": t, "food_used": fu, "food_made": fm} for t, fu, fm in
              [(0, 12, 15), (7.14, 15, 15), (14.29, 15, 15), (21.43, 16, 23), (28.57, 200, 200)]]
        bl = B.supply_blocks({"stats": st}, 1)
        self.assertEqual(len(bl), 1)
        self.assertEqual((bl[0]["samples"], bl[0]["supply"]), (2, 15))


class BuildTimes(unittest.TestCase):
    def test_back_to_back_gap_is_the_build_time(self):
        g = {"units": [u(10, "CommandCenter", kinds=["structure"])] +
             [u(20 + i, "SCV", born=12.0 * (i + 1), ctag=10, kinds=["worker"]) for i in range(5)]}
        self.assertEqual(B.measured_build_times([g])["SCV"]["seconds"], 12)


@unittest.skipUnless((LIBRARY_TWINS / "10000 Feet LE.SC2Replay").exists(), "twins on L: absent")
class AgainstSc2reader(unittest.TestCase):
    """Our structure starts against sc2reader's own unit tracking - a separate code path."""

    def test_structure_starts_match(self):
        import sc2reader

        from sc2analyst import adapter_sc2
        path = LIBRARY_TWINS / "10000 Feet LE.SC2Replay"
        g = adapter_sc2.load_game(path)
        r = sc2reader.load_replay(str(path), load_level=4)
        for p in r.players:
            theirs = sorted(round(x.started_at / 22.4, 2) for x in p.units
                            if x.is_building and x.started_at and x.started_at > 0
                            # creep tumours are left out of a build order on purpose
                            and x.name not in adapter_sc2.TRANSIENT)
            ours = sorted(e["t"] for e in B.build_order(g, p.pid)
                          if e["what"] == "structure" and e["event"] == "started")
            self.assertEqual(ours, theirs)


if __name__ == "__main__":
    unittest.main()
