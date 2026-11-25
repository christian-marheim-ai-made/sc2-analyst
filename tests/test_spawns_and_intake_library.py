"""Spawns come from the replay (L-4); the library intake driver keeps every name out of its manifest.

Synthetic fixtures throughout, plus two proofs against the canon replay `andre_gamet`, which skip
when it is absent. Names are never printed: the real roster is compared inside the test only.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from fixtures import LIBRARY_TWINS, REPLAYS, REPO, load, need

sys.path.insert(0, str(REPO / "tools"))
from spawns_from_replay import match_to_candidates, spawns  # noqa: E402
import intake_library  # noqa: E402
from intake_library import ROW_KEYS, intake_folder, strip_report  # noqa: E402
from intake_replay import REPLAY_MAP_PATH  # noqa: E402
from anonymise import is_kept  # noqa: E402

ANDRE = REPLAYS / "andre_gamet.SC2Replay"


class SpawnMatching(unittest.TestCase):
    STARTS = [{"name": "Start Location 001", "position": {"x": 10.5, "y": 20.5, "z": 0}},
              {"name": "Start Location 002", "position": {"x": 90.5, "y": 80.5, "z": 0}},
              {"name": "Start Location 003", "position": {"x": 50.5, "y": 50.5, "z": 0}}]

    def test_nearest_candidate_within_tolerance_else_none(self):
        rows = [{"pid": 1, "team": 1, "town_hall": "Nexus", "x": 10.0, "y": 20.0},
                {"pid": 2, "team": 2, "town_hall": "Hatchery", "x": 30.0, "y": 30.0}]
        out = match_to_candidates(rows, self.STARTS)
        self.assertEqual(out[0]["start_loc"], "Start Location 001")
        self.assertAlmostEqual(out[0]["start_loc_distance"], 0.71, places=2)
        self.assertIsNone(out[1]["start_loc"])              # 20+ cells from every candidate
        self.assertIsNone(out[1]["start_loc_distance"])

    def test_more_candidates_than_players_is_not_an_error(self):
        """The L-4 case: a map with more StartLocs than spawns; unused candidates simply stay unused."""
        rows = [{"pid": 1, "team": 1, "town_hall": "CommandCenter", "x": 90.0, "y": 80.0}]
        out = match_to_candidates(rows, self.STARTS)
        self.assertEqual([r["start_loc"] for r in out], ["Start Location 002"])


class SpawnsFromCanonReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        need(ANDRE)
        import sc2reader
        cls.replay = sc2reader.load_replay(str(ANDRE), load_level=4)
        cls.rows = spawns(cls.replay)

    def test_one_town_hall_per_player_at_frame_zero(self):
        self.assertEqual([r["pid"] for r in self.rows], [1, 2, 3, 4, 5, 6])
        self.assertEqual(sorted(r["team"] for r in self.rows), [1, 1, 1, 2, 2, 2])
        self.assertTrue(all(r["town_hall"] in ("CommandCenter", "Nexus", "Hatchery") for r in self.rows))
        self.assertFalse(any("name" in r for r in self.rows))

    def test_every_spawn_matches_a_distinct_start_loc_of_its_map(self):
        feats = load(REPO / "data/maps" / (self.replay.map_name.replace(" ", "_") + ".features.json"))
        out = match_to_candidates([dict(r) for r in self.rows], feats["start_locations"])
        locs = [r["start_loc"] for r in out]
        self.assertTrue(all(locs), "a spawn matched no StartLoc: %s" % out)
        self.assertEqual(len(set(locs)), len(locs), "two spawns matched the same StartLoc")
        self.assertTrue(all(r["start_loc_distance"] < 1.0 for r in out))


class SpawnsOnTheOldSunTempleTwins(unittest.TestCase):
    """L-4 proven on the map that raised it: four StartLocs, two spawns per game, each matched.

    Reads the anonymised twins on L: (skips without them). Measured 2026-09-24: game one spawned on
    002 and 003, game two on 003 and 001 - the map's list is candidates, the replay is the truth.
    """

    def test_two_spawns_per_game_on_a_four_start_map(self):
        feats = load(REPO / "data/maps/Old_Sun_Temple_LE.features.json")
        self.assertEqual(len(feats["start_locations"]), 4)
        import sc2reader
        used = set()
        # both twins first: a skip inside the subtest would leave the last assert with nothing read
        twins = [need(LIBRARY_TWINS / f) for f in ("Old Sun Temple LE.SC2Replay", "Old Sun Temple LE (2).SC2Replay")]
        for f in twins:
            with self.subTest(replay=f.name):
                replay = sc2reader.load_replay(str(f), load_level=4)
                rows = match_to_candidates(spawns(replay), feats["start_locations"])
                self.assertEqual(len(rows), 2, rows)
                self.assertEqual(sorted(r["team"] for r in rows), [1, 2])
                locs = [r["start_loc"] for r in rows]
                self.assertTrue(all(locs) and len(set(locs)) == 2, rows)
                self.assertTrue(all(r["start_loc_distance"] < 1.0 for r in rows))
                used |= set(locs)
        self.assertGreater(len(used), 2, "both games used the same two of four - the proof is weak")


def _fake_report(dst, real_names):
    return {"input": "x", "output": str(dst), "events": 100, "map": "Synthetic Map", "length": "0.10.00",
            "players_before": [(n, 1) for n in real_names],
            "players_after": [("Player0%d" % i, 1) for i, _ in enumerate(real_names, 1)],
            "touched_members": [("replay.details", 3, 10, 10)],
            "aliases": {n: "Player0%d" % i for i, n in enumerate(real_names, 1)}}


class IntakeDriver(unittest.TestCase):
    NAMES = ["Zorblax_Synthetic", "Quuxling_Synthetic"]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src, self.dst = self.tmp / "src", self.tmp / "dst"
        self.src.mkdir()
        for n in ("Map A.SC2Replay", "Map B.SC2Replay", "Map C.SC2Replay"):
            (self.src / n).write_bytes(b"not a real replay: " + n.encode())
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _anonymise(self, src, dst, map_path):
        self.calls.append(src.name)
        if src.name == "Map C.SC2Replay":
            dst.write_bytes(b"partial")
            raise RuntimeError("real name still visible in player list: %r" % self.NAMES[0])
        dst.write_bytes(src.read_bytes())
        return _fake_report(dst, self.NAMES)

    def test_manifest_carries_no_name_and_rows_have_only_the_documented_keys(self):
        summary = intake_folder(self.src, self.dst, self.tmp / "map.local.json", log=lambda s: None,
                                anonymise=self._anonymise)
        self.assertEqual((summary["done"], summary["skipped"], summary["failed"]), (2, 0, 1))
        text = (self.dst / intake_library.MANIFEST).read_text(encoding="utf-8")
        for n in self.NAMES:
            self.assertNotIn(n, text)
        m = json.loads(text)
        self.assertEqual(sorted(m["replays"]), ["Map A.SC2Replay", "Map B.SC2Replay"])
        for row in m["replays"].values():
            self.assertEqual(sorted(row), sorted(ROW_KEYS))
        self.assertEqual(m["errors"], {"Map C.SC2Replay": "RuntimeError"})    # the type, never the text
        self.assertFalse((self.dst / "Map C.SC2Replay").exists(), "a failed twin must not remain")
        self.assertIn(self.NAMES[0], (self.dst / intake_library.ERROR_LOG).read_text(encoding="utf-8"),
                      "the local error log is where the text goes")

    def test_second_run_skips_existing_twins(self):
        intake_folder(self.src, self.dst, self.tmp / "map.local.json", log=lambda s: None,
                      anonymise=self._anonymise)
        self.calls.clear()
        summary = intake_folder(self.src, self.dst, self.tmp / "map.local.json", log=lambda s: None,
                                anonymise=self._anonymise)
        self.assertEqual(self.calls, ["Map C.SC2Replay"])                  # only the failed one retried
        self.assertEqual((summary["done"], summary["skipped"], summary["failed"]), (0, 2, 1))

    def test_strip_report_drops_every_name_bearing_field(self):
        row = strip_report(_fake_report(Path("x"), self.NAMES))
        self.assertEqual(row, {"events": 100, "map": "Synthetic Map", "length": "0.10.00",
                               "players": 2, "renamed": 2, "touched_members": 1})


class IntakeDriverOnACanonReplay(unittest.TestCase):
    """The real intake on one real replay through the driver, against an ISOLATED name map."""

    def test_twin_verified_and_manifest_free_of_the_replays_own_roster(self):
        need(ANDRE)
        tmp = Path(tempfile.mkdtemp())
        try:
            src = tmp / "src"
            src.mkdir()
            shutil.copy(ANDRE, src / ANDRE.name)
            map_copy = tmp / "map.local.json"
            if Path(REPLAY_MAP_PATH).exists():
                shutil.copy(REPLAY_MAP_PATH, map_copy)      # isolated copy - the real map untouched
            summary = intake_folder(src, tmp / "dst", map_copy, log=lambda s: None)
            self.assertEqual((summary["done"], summary["failed"]), (1, 0))
            twin = tmp / "dst" / ANDRE.name
            self.assertEqual(twin.stat().st_size, ANDRE.stat().st_size)
            text = (tmp / "dst" / intake_library.MANIFEST).read_text(encoding="utf-8")
            import sc2reader
            roster = [p.name for p in sc2reader.load_replay(str(ANDRE), load_level=2).players]
            real = [n for n in roster if n and not is_kept(n)]
            self.assertGreaterEqual(len(real), 2, "non-vacuity: the replay must carry non-kept names")
            self.assertEqual([n for n in real if n in text], [])
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
