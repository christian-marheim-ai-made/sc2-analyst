"""W10 · grading (blind, the owner's clicks only, the comparison) and a stand-in night: a batch planned
ahead, the server stopped and started again, every job landed and checked."""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

from fixtures import REPO

sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tools"))

import workbench as W  # noqa: E402
from sc2analyst import adapter_sc2, grading as G, llm  # noqa: E402
from test_workbench_vm import game  # noqa: E402

KEY = "No_Such_Map_2026-10-01_1234"
REC = {"correlation_id": "c-1", "task": "T-fight", "version": "abcd1234", "model": "thinker",
       "facts": [{"id": "F1", "text": "We lost 50."}], "submitted": 0, "landed": 30,
       "usage": {"completion_tokens": 400},
       "verdicts": {"kept": [{"text": "We lost 50.", "cites": ["F1"]}, {"text": "A second.", "cites": []}],
                    "dropped": [{"claim": {"text": "x"}, "why": "w"}]}}


class Grading(unittest.TestCase):
    def test_blind_hides_who_said_it_and_skips_the_graded(self):
        cs = G.claims([REC])
        self.assertEqual(len(cs), 2)
        b = G.blind(cs, {cs[0]["hash"]: {"grade": "right"}}, seed=1)
        self.assertEqual(len(b), 1)
        self.assertNotIn("model", b[0])
        self.assertNotIn("version", b[0])

    def test_only_the_four_grades(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(KeyError):
                G.grade("h", "c-1", "brilliant", path=Path(d) / "g.jsonl")

    def test_compare_counts_precision_from_the_grades(self):
        cs = G.claims([REC])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "g.jsonl"
            G.grade(cs[0]["hash"], "c-1", "insight", path=path)
            G.grade(cs[1]["hash"], "c-1", "wrong", path=path)
            row = G.compare([REC], G.load_grades(path))[0]
        self.assertEqual((row["runs"], row["auto_pass"], row["graded"], row["precision"], row["insight"]),
                         (1, round(100 * 2 / 3, 1), 2, 50.0, 50.0))
        self.assertEqual((row["seconds_p50"], row["tokens_p50"]), (30, 400))


@unittest.skipUnless(llm.BOOT.exists(), "vendored bootstrap absent")
class StandInNight(unittest.TestCase):
    def test_planned_ahead_survives_a_restart_and_lands(self):
        with tempfile.TemporaryDirectory() as d:
            recs = Path(d) / "records"
            recs.mkdir()
            adapter_sc2.save(game(), recs / "synthetic.json.gz")
            saved = (W.STANDIN_WORKSPACE, W.LLM_WORKSPACE)
            W.STANDIN_WORKSPACE, W.LLM_WORKSPACE = Path(d) / "standin", Path(d) / "real-never-used"
            wb = W.Workbench(recs, 8798)
            try:
                wb.start_model("mock")
                self.assertEqual(wb.batch_preview({"task": "T-fight", "keys": [KEY]})["tasks"], 1)
                r = wb.batch_plan({"task": "T-fight", "keys": [KEY], "not_before": time.time() + 6})
                self.assertEqual(r["planned"], 1)
                pending = list((W.STANDIN_WORKSPACE / "sc2" / "pending").glob("c-*.json"))
                self.assertEqual(len(pending), 1)
                wb.stop_model()                            # the night: the workbench closed and opened
                wb.start_model("mock")                     # lands what the earlier launch left
                cid = pending[0].stem
                for _ in range(150):
                    if wb.runs.get(cid, {}).get("status") not in (None, "queued", "held"):
                        break
                    time.sleep(0.2)
                self.assertEqual(wb.runs[cid]["status"], "done")
                self.assertFalse(pending[0].exists())
                self.assertTrue((W.STANDIN_WORKSPACE / "sc2" / ("%s.json" % cid)).exists())
            finally:
                wb.stop_model()
                W.STANDIN_WORKSPACE, W.LLM_WORKSPACE = saved


if __name__ == "__main__":
    unittest.main()
