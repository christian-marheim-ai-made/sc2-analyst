"""Replay extraction and the map<->replay join.

The oracles here are the owner's own artifacts:
  * `andre_gamet_extracted_repositories.json` - a known extract, reproduced exactly
  * the recorded console output of the recovered position-warfare script
  * `white_chamber_decisive_terran_tanklines_summary.json` - the named chokes

Known defects are PINNED rather than ignored, so that fixing one shows up as a failing test
instead of a silent behaviour change. Each such test names its audit row.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from fixtures import (EXTRACTOR_VER3, POSITION_WARFARE, REF_REPLAY_EXTRACT, REF_TANKLINES,
                      REPLAYS, VENV_PY, count_events, load, need)


def _load_ver3():
    spec = importlib.util.spec_from_file_location("ver3", need(EXTRACTOR_VER3))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ReplayExtractReproduces(unittest.TestCase):
    """REGRESSION: re-running the extractor must reproduce the supplied extract."""

    @classmethod
    def setUpClass(cls):
        need(REPLAYS / "andre_gamet.SC2Replay")
        cls.ref = load(REF_REPLAY_EXTRACT)
        mod = _load_ver3()
        cls.mine = mod.Sc2ReplayRepositoryMvp(focus_player_name="ally_2") \
                      .extract(str(REPLAYS / "andre_gamet.SC2Replay"))

    def test_event_counts_per_repository(self):
        for repo in ("raw_information", "attention_information", "financial_information",
                     "scout_information", "strategical_information", "tactical_information",
                     "cliffs_repository", "chokes_repository", "aironly_repository"):
            with self.subTest(repository=repo):
                a, b = count_events(self.mine, repo), count_events(self.ref, repo)
                self.assertGreater(b, 0, "reference repo empty - comparison would be vacuous")
                self.assertEqual(a, b)

    def test_metadata_matches(self):
        for f in ("schema_version", "map_name", "focus_player",
                  "game_length_seconds", "build", "game_type"):
            with self.subTest(field=f):
                self.assertEqual(self.mine["metadata"][f], self.ref["metadata"][f])

    def test_R1_two_clocks_still_present(self):
        """PINNED DEFECT (audit R1): metadata length is real time, event seconds are game time.

        Ratio must be ~1.4 (SC2 'Faster'). When the clock is fixed this test SHOULD fail.
        """
        gl = self.mine["metadata"]["game_length_seconds"]
        mx = max(int(s) for k, v in self.mine.items()
                 if k != "metadata" and isinstance(v, dict) for s in v)
        self.assertAlmostEqual(mx / gl, 1.4, delta=0.02,
                               msg="clock ratio changed - R1 may be fixed; update this test")

    def test_R2_financial_snapshots_truncate(self):
        """PINNED DEFECT (audit R2): snapshots stop at the real-time bound, ~72% through."""
        fin = sorted(int(s) for s in self.mine["financial_information"])
        mx = max(int(s) for k, v in self.mine.items()
                 if k != "metadata" and isinstance(v, dict) for s in v)
        self.assertLess(fin[-1], mx, "R2 may be fixed - update this test")

    def test_R20_ability_decodes_as_chrono_boost(self):
        """PINNED (audit R20): a sc2reader version change renamed these 67 events.

        `ChronoBoostEnergyCost` is correct - the targets are Stargate/CyberneticsCore/Forge, and
        you cannot Mass Recall onto a Forge. If this ever reads NexusMassRecall again, the decoder
        regressed and every ability-based conclusion is suspect.
        """
        abilities = [e.get("ability") for evs in self.mine["raw_information"].values()
                     for e in evs if e.get("event_type") == "player_command"]
        self.assertIn("ChronoBoostEnergyCost", abilities)
        self.assertNotIn("NexusMassRecall", abilities)


class PositionWarfareJoin(unittest.TestCase):
    """REGRESSION: the recovered map<->replay join reproduces its recorded output."""

    @classmethod
    def setUpClass(cls):
        need(POSITION_WARFARE)
        need(REPLAYS / "White Chamber (38).SC2Replay")
        cls.tmp = tempfile.mkdtemp()
        r = subprocess.run([str(need(VENV_PY)), str(POSITION_WARFARE), cls.tmp],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise unittest.SkipTest("position-warfare run failed: %s" % r.stderr[-400:])
        cls.stdout = r.stdout
        cls.doc = json.loads(
            (Path(cls.tmp) / "white_chamber_position_warfare_analysis.json").read_text("utf-8"))

    def test_recorded_headline(self):
        """The dump recorded exactly this line on the successful run."""
        self.assertIn("deployments 12 chokes 24", self.stdout)

    def test_twelve_deployments(self):
        self.assertEqual(len(self.doc["tank_siege_deployments"]), 12)

    def test_cliff_levels_decoded_from_the_archive(self):
        cg = self.doc["cliff_grid"]
        self.assertEqual(cg["source"], "t3SyncCliffLevel")
        self.assertIn("64", cg["encoding"])          # uint16 / 64.0
        self.assertEqual(cg["dimensions_cells"], {"width": 256, "height": 216})

    def test_every_deployment_carries_provenance(self):
        """INVARIANT: a derived position must say where it came from and how sure it is."""
        for d in self.doc["tank_siege_deployments"]:
            with self.subTest(second=d.get("siege_time")):
                self.assertIn("position_source", d)
                self.assertIn("location_confidence", d)
                self.assertTrue(d["location_confidence"])

    def test_low_confidence_positions_are_labelled_not_hidden(self):
        confs = {d["location_confidence"] for d in self.doc["tank_siege_deployments"]}
        self.assertTrue(any("low_confidence" in c for c in confs),
                        "no low-confidence rows - the label may have stopped being applied")

    def test_busiest_choke_is_the_named_tank_line(self):
        """Cross-artifact: the choke with most deaths is 'Choke A' in the tank-line summary."""
        busiest = max(self.doc["ramp_choke_candidates"], key=lambda r: r.get("death_hits_18", 0))
        summary = load(REF_TANKLINES)
        choke_a = next(c for c in summary["chokes"] if c["label"] == "Choke A")
        self.assertEqual(round(busiest["center"]["x"], 1), float(choke_a["center"]["x"]))
        self.assertEqual(round(busiest["center"]["y"], 1), float(choke_a["center"]["y"]))
        self.assertGreater(busiest["death_hits_18"], 0, "no deaths - comparison would be vacuous")


if __name__ == "__main__":
    unittest.main(verbosity=2)
