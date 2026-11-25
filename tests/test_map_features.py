"""Map feature extraction: regression against the supplied reference, plus invariants.

TWO KINDS OF ASSERTION, deliberately:

  * REGRESSION  — our output vs the reference data the owner supplied. Catches drift.
  * INVARIANT   — a property that must hold regardless of any reference. Catches the case where
                  the REFERENCE is the thing that is wrong.

That distinction is not theoretical here. `resource_clusters` in the reference file mis-splits five
bases; the invariant test below is what proves it, and a regression-only suite would have quietly
enshrined the defect. See docs/PROOF-map-extractor.md.
"""
from __future__ import annotations

import functools
import unittest

from fixtures import (LIBRARY_MAP_NAMES, MAP_NAMES, MAPS, REF_WC_FEATURES, load, need,
                      reference_map_repositories)


@functools.lru_cache(maxsize=None)
def extract_map(name, radius=None):
    """Extract once per (map, radius); the invariants read the same result many times.

    The default radius is the tool's own CLUSTER_RADIUS, so the suite tests what ships. Until
    2026-09-24 this helper defaulted to 9.0 while the tool shipped 6.0.
    """
    from extract_map_features import CLUSTER_RADIUS, extract
    return extract(str(need(MAPS / (name + ".SC2Map"))),
                   CLUSTER_RADIUS if radius is None else radius)


class ReferenceMatrix(unittest.TestCase):
    """REGRESSION: counts must match the supplied 7-map awareness file."""

    @classmethod
    def setUpClass(cls):
        cls.ref = reference_map_repositories()

    def test_reference_is_not_vacuous(self):
        """Guard: a comparison against empty reference data proves nothing."""
        ramps = [len(self.ref[m]["terrain_information"]["ramp_choke_candidates"]) for m in MAP_NAMES]
        self.assertEqual(len(ramps), 7)
        self.assertTrue(all(n > 0 for n in ramps), "reference ramp lists are empty: %s" % ramps)
        self.assertGreater(len(set(ramps)), 1, "all reference ramp counts identical - suspicious")

    def test_ramps_starts_towers_destructibles(self):
        for name in MAP_NAMES:
            with self.subTest(map=name):
                d = extract_map(name)
                r = self.ref[name]
                mo, ti = r["map_object_information"], r["terrain_information"]
                self.assertEqual(len(d["ramps"]), len(ti["ramp_choke_candidates"]), "ramps")
                self.assertEqual(len(d["start_locations"]), len(mo["start_locations"]), "starts")
                self.assertEqual(len(d["xelnaga_towers"]), len(mo["xelnaga_towers"]), "towers")
                self.assertEqual(len(d["destructibles"]),
                                 len(mo["destructibles_and_blockers"]), "destructibles")


class WhiteChamberFeatureFile(unittest.TestCase):
    """REGRESSION: reproduce the supplied features file, field by field."""

    @classmethod
    def setUpClass(cls):
        cls.orig = load(REF_WC_FEATURES)
        cls.mine = extract_map("White Chamber")

    @staticmethod
    def _norm(o):
        if isinstance(o, float):
            return round(o, 3)
        if isinstance(o, dict):
            return {k: WhiteChamberFeatureFile._norm(v) for k, v in sorted(o.items())}
        if isinstance(o, list):
            return [WhiteChamberFeatureFile._norm(x) for x in o]
        return o

    def _same_set(self, field):
        import json
        a = {json.dumps(self._norm(x), sort_keys=True) for x in self.mine[field]}
        b = {json.dumps(self._norm(x), sort_keys=True) for x in self.orig[field]}
        self.assertEqual(a, b, "%s differs" % field)

    def test_map_file(self):
        self.assertEqual(self.mine["map_file"], self.orig["map_file"])

    def test_height_map(self):
        self.assertEqual(self._norm(self.mine["height_map"]), self._norm(self.orig["height_map"]))

    def test_files_available(self):
        self._same_set("files_available")

    def test_start_locations(self):
        self._same_set("start_locations")

    def test_ramps_including_raw_strings(self):
        self._same_set("ramps")

    def test_xelnaga_towers(self):
        self._same_set("xelnaga_towers")

    def test_destructibles(self):
        self._same_set("destructibles")

    def test_no_fly_zones_with_both_radii(self):
        self._same_set("no_fly_zones")

    def test_resource_clusters_KNOWN_DIVERGENCE(self):
        """The reference is wrong here. Pinned so a silent change is visible.

        Both partitions cover the same 260 nodes; ours is 26 valid bases, the reference is
        21 valid plus 6 ragged fragments. See the invariant test below for the proof.
        """
        self.assertEqual(len(self.orig["resource_clusters"]), 27, "reference changed")
        self.assertEqual(len(self.mine["resource_clusters"]), 26, "our partition changed")


class ResourceClusterInvariants(unittest.TestCase):
    """INVARIANT: holds without reference to any supplied file.

    A StarCraft II base is a fixed arrangement of mineral patches plus geysers. A partition of the
    resource nodes into bases is only correct if every group is a whole base and no node is lost.

    Since 2026-09-24 these run over every map in canon (38), and `resource_clusters` holds BASES
    only: wall material (450-class patches, classified per patch from minerals at map start) lands
    in `mineral_walls`. See docs/canon/rulings-2026-09-24.md and DECISION-LEDGER DL-12.
    """

    # A base is NOT uniform across the pool. Measured type names include LabMineralField,
    # RichMineralField, MineralField450 and six geyser skins - fifteen resource types on the 38
    # maps. So "every cluster is identical" holds on White Chamber only, and is asserted there.
    # These are the invariants that hold POOL-WIDE.

    MAX_BASE_NODES = 14   # a real base tops out ~9 minerals + 2-4 geysers; 20 means two merged
    MIN_BASE_NODES = 4    # Void Training Stage 3 has 3-patch mini bases with one geyser each

    def test_no_cluster_is_two_merged_bases(self):
        for name in LIBRARY_MAP_NAMES:
            with self.subTest(map=name):
                clusters = extract_map(name)["resource_clusters"]
                self.assertGreater(len(clusters), 0, "no clusters - test would be vacuous")
                big = [c["point_count"] for c in clusters if c["point_count"] > self.MAX_BASE_NODES]
                self.assertEqual(big, [], "%s: clusters too large to be one base: %s" % (name, big))

    def test_no_cluster_is_a_fragment(self):
        for name in LIBRARY_MAP_NAMES:
            with self.subTest(map=name):
                clusters = extract_map(name)["resource_clusters"]
                tiny = [c["point_count"] for c in clusters if c["point_count"] < self.MIN_BASE_NODES]
                self.assertEqual(tiny, [], "%s: fragment clusters: %s" % (name, tiny))

    def test_every_cluster_has_minerals(self):
        """A geyser-only group is not a base."""
        for name in LIBRARY_MAP_NAMES:
            with self.subTest(map=name):
                for c in extract_map(name)["resource_clusters"]:
                    self.assertGreater(c["mineral_count"], 0,
                                       "%s: cluster %s has geysers but no minerals"
                                       % (name, c["id"]))

    def test_every_resource_node_is_accounted_for(self):
        """Partition, not a sample: totals must equal the raw counts from the archive."""
        import xml.etree.ElementTree as ET
        from mpyq import MPQArchive
        for name in LIBRARY_MAP_NAMES:
            with self.subTest(map=name):
                root = ET.fromstring(MPQArchive(str(need(MAPS / (name + ".SC2Map"))))
                                     .read_file("Objects").decode("utf-8", "replace"))
                raw_min = raw_gey = 0
                for e in root:
                    if e.tag != "ObjectUnit":
                        continue
                    low = (e.get("UnitType") or "").lower()
                    raw_min += "mineral" in low
                    raw_gey += "geyser" in low and "mineral" not in low
                d = extract_map(name)
                cl = d["resource_clusters"] + d["mineral_walls"]
                self.assertGreater(raw_min, 0, "no minerals in archive - test would be vacuous")
                self.assertEqual(sum(c["mineral_count"] for c in cl), raw_min, "minerals lost")
                self.assertEqual(sum(c["geyser_count"] for c in cl), raw_gey, "geysers lost")

    def test_walls_are_wall_material_only_and_own_no_geyser(self):
        """Ruling 2026-09-24: a patch below BASE_PATCH_MIN_MINERALS at map start is wall material.

        Guard: at least one map must have walls, or this asserts nothing. Five of 38 do
        (10000 Feet, Desolator, Old Sun Temple, Sentinel, Voracity).
        """
        from resource_contents import BASE_PATCH_MIN_MINERALS
        walls_seen = 0
        for name in LIBRARY_MAP_NAMES:
            for w in extract_map(name)["mineral_walls"]:
                walls_seen += 1
                self.assertEqual(w["kind"], "wall")
                self.assertEqual(w["geyser_count"], 0, "%s: a wall owns a geyser" % name)
                self.assertLess(w["patch_min_minerals"], BASE_PATCH_MIN_MINERALS)
                self.assertTrue(all(k.endswith("450") for k in w["resource_counts"]),
                                "%s: wall made of %s" % (name, w["resource_counts"]))
        self.assertGreater(walls_seen, 0, "no walls on any map - this test was vacuous")

    def test_no_base_contains_wall_material(self):
        from resource_contents import BASE_PATCH_MIN_MINERALS
        for name in LIBRARY_MAP_NAMES:
            with self.subTest(map=name):
                for c in extract_map(name)["resource_clusters"]:
                    self.assertEqual(c["kind"], "base")
                    self.assertGreaterEqual(c["patch_min_minerals"], BASE_PATCH_MIN_MINERALS,
                                            "%s: base %s holds wall material" % (name, c["id"]))

    def test_every_resource_type_on_the_pool_has_a_known_content(self):
        for name in LIBRARY_MAP_NAMES:
            with self.subTest(map=name):
                self.assertEqual(extract_map(name)["unknown_resource_types"], [])

    def test_PINNED_totals_do_not_separate_walls_from_bases(self):
        """Why the rule is per patch, not per cluster total: measured 2026-09-24, a 12-patch wall
        (5400) equals a gasless gold pocket and a 16-patch wall (7200) equals every rich base.
        If this ever fails, the premise of the per-patch rule has changed - revisit DL-12."""
        walls = [w["minerals_at_start"] for n in LIBRARY_MAP_NAMES
                 for w in extract_map(n)["mineral_walls"]]
        bases = [c["minerals_at_start"] for n in LIBRARY_MAP_NAMES
                 for c in extract_map(n)["resource_clusters"]]
        self.assertTrue(walls and bases, "vacuous: no walls or no bases on the pool")
        self.assertGreaterEqual(max(walls), min(bases), "totals now separate walls from bases")

    def test_white_chamber_is_uniform(self):
        """MAP-SPECIFIC: White Chamber's 26 bases are each 8 minerals + 2 geysers.

        This uniformity is what proves the supplied reference file mis-split five of them.
        """
        cl = extract_map("White Chamber")["resource_clusters"]
        self.assertEqual(len(cl), 26)
        comps = {tuple(sorted(c["resource_counts"].items())) for c in cl}
        self.assertEqual(len(comps), 1, "White Chamber no longer uniform: %s" % comps)
        self.assertTrue(all(c["mineral_count"] == 8 and c["geyser_count"] == 2 for c in cl))

    def test_reference_file_fails_this_invariant(self):
        """The defect, asserted. If the reference is ever corrected this test must be deleted."""
        orig = load(REF_WC_FEATURES)["resource_clusters"]
        sizes = sorted({c["point_count"] for c in orig})
        self.assertGreater(len(sizes), 1,
                           "reference no longer ragged - it may have been fixed; update this test")
        ragged = [c["point_count"] for c in orig if c["point_count"] != 10]
        self.assertEqual(sorted(ragged), [4, 5, 6, 8, 12, 15])


class MapStructureInvariants(unittest.TestCase):

    def test_six_start_locations_on_every_3v3_map(self):
        """The original seven only: a map's StartLoc list is candidates, not the player count.
        Old Sun Temple LE carries four on a map played 1v1 (library finding L-4, ratified)."""
        for name in MAP_NAMES:
            with self.subTest(map=name):
                self.assertEqual(len(extract_map(name)["start_locations"]), 6)

    def test_ramp_count_matches_the_xml_header(self):
        """<rampList num="N"> must agree with the number of <ramp> children we parsed."""
        import xml.etree.ElementTree as ET
        from mpyq import MPQArchive
        for name in LIBRARY_MAP_NAMES:
            with self.subTest(map=name):
                x = ET.fromstring(MPQArchive(str(need(MAPS / (name + ".SC2Map"))))
                                  .read_file("t3Terrain.xml").decode("utf-8", "replace"))
                node = x.find("heightMap/rampList")
                declared = int(node.get("num"))
                self.assertEqual(len(extract_map(name)["ramps"]), declared)

    def test_no_fly_zone_radii_are_paired_and_not_invented(self):
        """Measured: 33 of White Chamber's 34 zones carry both radii; 'No Fly Zone 006' carries
        neither. So the invariant is PAIRING - both or neither - and the tool must not fabricate
        a default for the one that has none.

        Guard: at least one map must have zones, or this asserts nothing. Six of seven genuinely
        have zero, which is map design, not an extraction gap.
        """
        seen = with_radii = 0
        for name in LIBRARY_MAP_NAMES:
            for z in extract_map(name)["no_fly_zones"]:
                seen += 1
                h, s = z["hard_radius"], z["soft_radius"]
                self.assertEqual(h is None, s is None,
                                 "%s: %s has only one radius (%r/%r)" % (name, z["name"], h, s))
                with_radii += h is not None
        self.assertGreater(seen, 0, "no no-fly zones on any map - this test was vacuous")
        self.assertGreater(with_radii, 0, "no zone carries radii - the parse may have broken")

    def test_only_white_chamber_has_no_fly_zones(self):
        """Pinned: the asymmetry is real map design (a space platform with void gaps), not a bug."""
        counts = {n: len(extract_map(n)["no_fly_zones"]) for n in MAP_NAMES}
        self.assertEqual(counts["White Chamber"], 34)
        self.assertTrue(all(v == 0 for k, v in counts.items() if k != "White Chamber"),
                        "another map gained no-fly zones: %s" % counts)

    def test_minimap_decodes_for_every_map(self):
        import io
        from PIL import Image
        from mpyq import MPQArchive
        sizes = {}
        for name in LIBRARY_MAP_NAMES:
            with self.subTest(map=name):
                raw = MPQArchive(str(need(MAPS / (name + ".SC2Map")))).read_file("Minimap.tga")
                im = Image.open(io.BytesIO(raw))
                self.assertEqual(im.mode, "RGB")
                self.assertTrue(all(s in (128, 256, 512, 1024) for s in im.size), im.size)
                sizes[name] = im.size
        # The original seven are square, 256 or 1024. The pool is not: 10000 Feet, Canyon of
        # Tribulation and Void Training Stage 3 ship 128x256, Rorschach 256x128, Fields of Death
        # 512x512 (measured 2026-09-24). A minimap transform (analysers F8) must not assume square.
        for name in MAP_NAMES:
            self.assertIn(sizes[name], [(256, 256), (1024, 1024)], name)
        if len(LIBRARY_MAP_NAMES) > len(MAP_NAMES):
            self.assertTrue(any(w != h for (w, h) in sizes.values()),
                            "no rectangular minimap on the pool - the note above is stale")


if __name__ == "__main__":
    unittest.main(verbosity=2)
