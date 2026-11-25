"""Library discovery: the inventory never carries a player, and maps import from the cache idempotently.

Fixtures are synthetic except two proofs against data the repo already holds, which skip when absent:
- the naming rule reproduces the tracked data/maps file names
- extracting a canon .SC2Map through the tool writes what data/maps already holds
- a scan of the real canon replays contains no name those replays carry (names never printed)
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from fixtures import MAP_NAMES, MAPS, REPLAYS, REPO, need

sys.path.insert(0, str(REPO / "tools"))
from discover_library import (ROW_KEYS, canon_filename, data_stem, import_maps,  # noqa: E402
                              index_cache, map_versions, report_markdown, scan_replays,
                              summarise)
from anonymise import is_kept  # noqa: E402

H1, H2, H3 = "1" * 64, "2" * 64, "3" * 64


def _row(**kw):
    base = {"file": "Map X.SC2Replay", "size": 100, "map_name": "Map X", "map_hash": H1,
            "build": 96883, "base_build": 96883, "game_type": "3v3", "category": "Ladder",
            "game_length_s": 900, "real_length_s": 1250, "date": "2026-09-23 20:00:00",
            "region": "eu"}
    base.update(kw)
    return base


class Naming(unittest.TestCase):
    def test_data_stem_replaces_spaces_only(self):
        self.assertEqual(data_stem("Tacit Sanctity LE"), "Tacit_Sanctity_LE")
        self.assertEqual(data_stem("At Eternity's Edge LE"), "At_Eternity's_Edge_LE")
        self.assertEqual(canon_filename("White Chamber"), "White Chamber.SC2Map")

    def test_naming_reproduces_the_tracked_map_layer(self):
        for name in MAP_NAMES:
            self.assertTrue((REPO / "data/maps" / (data_stem(name) + ".features.json")).exists(), name)
            self.assertTrue((REPO / "data/maps/minimaps" / (data_stem(name) + ".png")).exists(), name)


class CacheIndex(unittest.TestCase):
    def test_finds_s2ma_two_levels_down_case_insensitively_and_ignores_s2ml(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            h = "AB12" + "c" * 60
            (tmp / "ab" / "12").mkdir(parents=True)
            (tmp / "ab" / "12" / (h + ".s2ma")).write_bytes(b"map")
            (tmp / "ab" / "12" / ("d" * 64 + ".s2ml")).write_bytes(b"localisation")
            idx = index_cache(tmp)
            self.assertEqual(set(idx), {h.lower()})
            self.assertEqual(index_cache(tmp / "does-not-exist"), {})
        finally:
            shutil.rmtree(tmp)


class Import(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cache, self.canon, self.data = self.tmp / "cache", self.tmp / "canon", self.tmp / "data"
        self.cache.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _cached(self, h, payload):
        p = self.cache / h[:2] / h[2:4] / (h + ".s2ma")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(payload)

    def test_copies_missing_verifies_md5_skips_present_and_is_idempotent(self):
        self._cached(H1, b"NEW-ARCHIVE")
        self._cached(H2, b"CACHE-COPY-OF-OLD")
        self.canon.mkdir()
        (self.canon / "Old Map.SC2Map").write_bytes(b"OLD-AS-IN-CANON")
        rows = [_row(map_name="New Map", map_hash=H1), _row(map_name="Old Map", map_hash=H2)]

        rep = import_maps(rows, index_cache(self.cache), self.canon, self.data, extract_features=False)
        self.assertEqual([c["map_name"] for c in rep["copied"]], ["New Map"])
        self.assertTrue(rep["copied"][0]["md5_ok"])
        self.assertEqual((self.canon / "New Map.SC2Map").read_bytes(), b"NEW-ARCHIVE")
        self.assertEqual(rep["present"], ["Old Map"])
        self.assertEqual((self.canon / "Old Map.SC2Map").read_bytes(), b"OLD-AS-IN-CANON")  # never overwritten
        self.assertEqual(rep["missing_from_cache"], [])
        self.assertEqual(rep["version_conflicts"], {})

        rep2 = import_maps(rows, index_cache(self.cache), self.canon, self.data, extract_features=False)
        self.assertEqual(rep2["copied"], [])
        self.assertEqual(sorted(rep2["present"]), ["New Map", "Old Map"])

    def test_hash_absent_from_cache_is_reported_not_guessed(self):
        rep = import_maps([_row(map_name="Ghost", map_hash=H3)], index_cache(self.cache),
                          self.canon, self.data, extract_features=False)
        self.assertEqual(rep["missing_from_cache"], [{"map_name": "Ghost", "map_hash": H3}])
        self.assertEqual(rep["copied"], [])
        self.assertFalse((self.canon / "Ghost.SC2Map").exists())

    def test_two_hashes_for_one_name_import_nothing(self):
        self._cached(H1, b"v1")
        self._cached(H2, b"v2")
        rows = [_row(map_name="Same", map_hash=H1), _row(map_name="Same", map_hash=H2),
                _row(map_name="Same", map_hash=H1)]
        rep = import_maps(rows, index_cache(self.cache), self.canon, self.data, extract_features=False)
        self.assertEqual(rep["version_conflicts"], {"Same": {H1: 2, H2: 1}})
        self.assertEqual(rep["copied"], [])
        self.assertFalse((self.canon / "Same.SC2Map").exists())

    def test_extraction_through_the_tool_matches_the_tracked_layer(self):
        """REGRESSION against data/maps for a map already in canon: nothing copied, output identical."""
        need(MAPS / "White Chamber.SC2Map")
        rows = [_row(map_name="White Chamber", map_hash="0" * 64)]
        rep = import_maps(rows, {}, MAPS, self.data)           # empty cache: the copy path must not run
        self.assertEqual(rep["present"], ["White Chamber"])
        self.assertEqual(rep["copied"], [])
        self.assertEqual([x["map_name"] for x in rep["extracted"]], ["White Chamber"])

        ours = self.data / "White_Chamber.features.json"
        tracked = REPO / "data/maps/White_Chamber.features.json"
        norm = lambda p: p.read_text(encoding="utf-8").replace("\r\n", "\n")  # noqa: E731
        self.assertEqual(json.loads(norm(ours)), json.loads(norm(tracked)))
        self.assertEqual(norm(ours), norm(tracked))            # byte-for-byte modulo line endings

        from PIL import Image
        a = Image.open(self.data / "minimaps" / "White_Chamber.png")
        b = Image.open(REPO / "data/maps/minimaps/White_Chamber.png")
        self.assertEqual(a.size, b.size)
        self.assertEqual(a.tobytes(), b.tobytes())


class Privacy(unittest.TestCase):
    def test_row_vocabulary_is_exactly_the_documented_one(self):
        self.assertEqual(ROW_KEYS, ("file", "size", "map_name", "map_hash", "build", "base_build",
                                    "game_type", "category", "game_length_s", "real_length_s",
                                    "date", "region"))

    def test_scan_of_real_replays_carries_no_name_those_replays_carry(self):
        """The oracle is the replay itself, loaded deeper in the test only; names are never printed."""
        need(REPLAYS)
        rows, errors = scan_replays(REPLAYS)
        self.assertTrue(rows, "no replays found under canon/replays")
        self.assertEqual(errors, [])
        self.assertTrue(all(tuple(r) == ROW_KEYS for r in rows))
        text = json.dumps(rows).lower()

        import sc2reader
        names = set()
        for r in rows:
            rep = sc2reader.load_replay(str(REPLAYS / r["file"]), load_level=2)
            names |= {p.name for p in rep.players if p.name and not is_kept(p.name)}
        self.assertGreaterEqual(len(names), 2, "non-vacuity: the replays must carry non-kept names")
        leaked = sorted(n for n in names if n.lower() in text)
        self.assertEqual(len(leaked), 0, "%d name(s) leaked into the inventory" % len(leaked))


class Summary(unittest.TestCase):
    def test_counts_and_ordering(self):
        rows = [_row(map_name="A", map_hash=H1, build=1, game_length_s=900, date="2026-01-02 10:00:00"),
                _row(map_name="A", map_hash=H1, build=1, game_length_s=30, date="2026-01-01 10:00:00"),
                _row(map_name="B", map_hash=H2, build=2, game_type="1v1", game_length_s=600,
                     date="2026-02-01 10:00:00")]
        s = summarise(rows, short_seconds=120)
        self.assertEqual(s["replays"], 3)
        self.assertEqual(s["distinct_maps"], 2)
        self.assertEqual(s["maps"][0], {"map_name": "A", "map_hash": H1, "replays": 2,
                                        "first": "2026-01-01", "last": "2026-01-02"})
        self.assertEqual(s["by_game_type"], {"3v3": 2, "1v1": 1})
        self.assertEqual(s["by_build"], {1: 2, 2: 1})
        self.assertEqual(s["date_range"], ["2026-01-01", "2026-02-01"])
        self.assertEqual(s["length_s"], {"min": 30, "median": 600, "max": 900})
        self.assertEqual(s["short_games"], {"threshold_s": 120, "count": 1})
        self.assertEqual(map_versions(rows), {"A": {H1: 2}, "B": {H2: 1}})

    def test_report_lists_every_map(self):
        rows = [_row(map_name="Alpha", map_hash=H1), _row(map_name="Beta", map_hash=H2)]
        out = report_markdown(summarise(rows))
        self.assertIn("| Alpha | 1 |", out)
        self.assertIn("| Beta | 1 |", out)
        self.assertIn("| 3v3 | 2 |", out)


if __name__ == "__main__":
    unittest.main()
