"""S5 terrain, S6 focus, and the minimap transform they draw on."""
from __future__ import annotations

import sys
import unittest

from fixtures import LIBRARY_TWINS, REPO

sys.path.insert(0, str(REPO / "src"))

from sc2analyst import focus as FO, minimap as M, terrain as T  # noqa: E402

LIBRARY_MAP_SAMPLE = ["Old Sun Temple LE", "Sandstorm CE", "10000 Feet LE", "Rorschach LE"]


class Minimap(unittest.TestCase):
    def test_camera_box_is_where_the_terrain_is_drawn(self):
        # includes two non-square minimaps: 10000 Feet (128 x 256), Rorschach (256 x 128)
        for name in LIBRARY_MAP_SAMPLE:
            if not (REPO / "canon/maps/source" / (name + ".SC2Map")).exists():
                self.skipTest("map archive absent")
            v = M.verify(name)
            self.assertLessEqual(v["error_px"], 1, v)

    def test_pow2_per_axis(self):
        # Old Sun Temple: camera 26..182, 24..180 -> 256 cells; the left edge sits at pixel 50
        x, y = M.to_pixel(26, 180, (26, 24, 182, 180), 256, 256)
        self.assertEqual((round(x), round(y)), (50, 50))

    def test_spawns_land_on_drawn_terrain(self):
        from PIL import Image

        from sc2analyst import pick, repositories as R
        try:
            g = pick.load(pick.ONE_V_ONE)
        except Exception:
            self.skipTest("game records absent")
        t = M.Transform(g["map"])
        im = Image.open(M.image_path(g["map"])).convert("L")
        for p in g["players"]:
            x, y = R.spawn(g, p["pid"])
            self.assertGreater(im.getpixel(tuple(int(v) for v in t(x, y))), 12)


class Terrain(unittest.TestCase):
    def test_labels(self):
        names = {1: "Player_1", 2: "Enemy 1"}
        self.assertEqual(T.label({"base": {"main_of": 1, "holder": 1, "id": 3}}, names),
                         "Player_1's main (held by Player_1)")
        self.assertEqual(T.label({"base": {"main_of": None, "holder": None, "id": 3}}, names),
                         "base 3 (empty)")
        self.assertEqual(T.label({"ramp": {"at": True, "id": 2, "hi": 3, "lo": 2}}, names),
                         "at ramp 2 (level 3/2)")
        self.assertEqual(T.label({}, names), "open ground")


class Focus(unittest.TestCase):
    def test_dwells_numbered_and_merged(self):
        cam = [[0, 10, 10], [5, 12, 11], [8, 100, 100], [9, 101, 99], [20, 10, 10]]
        ds = FO.dwells(cam, 1.0, 25.0)
        self.assertEqual([d["n"] for d in ds], [1, 2, 3])
        self.assertEqual([d["start"] for d in ds], [1.0, 8, 20])
        self.assertEqual(ds[0]["seconds"], 7.0)

    def test_near_share_and_team_shape(self):
        g = {"players": [{"pid": 1, "team": 1}, {"pid": 2, "team": 1}],
             "camera": [[0, 1, 50, 50], [0, 2, 150, 150]]}
        f = {"start": 20.0, "end": 30.0, "place_centres": [[52, 52, 5]]}
        per = FO.focus(g, f)
        self.assertEqual((per["1"]["near_share"], per["2"]["near_share"]), (1.0, 0.0))
        self.assertEqual(FO.team_view(g, f, per)["1"]["shape"], "split: 1 of 2 on the fight")


@unittest.skipUnless(LIBRARY_TWINS.exists(), "twins on L: absent")
class Payload(unittest.TestCase):
    def test_one_game_end_to_end(self):
        from sc2analyst import analyse, pick
        p = analyse.analyse(pick.load(pick.ONE_V_ONE))
        self.assertEqual(p["mirror_problems"], [])
        self.assertTrue(p["walls"][str(p["pov"])]["wall_candidate"])
        for f in p["fights"]:
            self.assertIn("where", f)
        # names: the owner's handle, and nothing else that could be an alias
        self.assertEqual(sorted(x["name"] for x in p["players"]), ["Enemy_1", "Player_1"])


if __name__ == "__main__":
    unittest.main()
