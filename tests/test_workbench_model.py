"""W9 · the workbench's model: started by the button, a task run landed, its record and the
bootstrap's own, stopped - on llm-bootstrap's stand-in, in a temporary workspace (never the card)."""
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
from sc2analyst import adapter_sc2, llm  # noqa: E402
from test_workbench_vm import game  # noqa: E402

KEY = "No_Such_Map_2026-10-01_1234"


@unittest.skipUnless(llm.BOOT.exists(), "vendored bootstrap absent")
class Model(unittest.TestCase):
    def test_start_run_land_record_stop(self):
        with tempfile.TemporaryDirectory() as d:
            recs, ws = Path(d) / "records", Path(d) / "standin"
            recs.mkdir()
            adapter_sc2.save(game(), recs / "synthetic.json.gz")
            saved = (W.STANDIN_WORKSPACE, W.LLM_WORKSPACE)
            W.STANDIN_WORKSPACE, W.LLM_WORKSPACE = ws, Path(d) / "real-never-used"
            wb = W.Workbench(recs, 8798)
            try:
                with self.assertRaises(KeyError):
                    wb.start_run({"task": "T-fight", "game": KEY, "target": {"fight": 1}})
                self.assertEqual(wb.start_model("mock")["model"], "mock")
                r = wb.start_run({"task": "T-fight", "game": KEY, "target": {"fight": 1}})
                cid = r["correlation_id"]
                for _ in range(100):
                    if wb.runs[cid].get("status") not in ("queued", "held"):
                        break
                    time.sleep(0.2)
                rec = wb.run_record(cid)
                self.assertEqual(rec["status"], "done")
                self.assertIn("verdicts", rec)
                self.assertTrue(any(x.startswith("samples") for x in rec["bootstrap_record"]))
                self.assertIn(cid, [x["correlation_id"] for x in wb.runs_listing()])
                # debug: held in Check until sent; the run follows it to the end (found W9: it stayed held)
                from sc2analyst import runs as RN
                h = wb.start_run({"task": "T-fight", "game": KEY, "target": {"fight": 1}, "debug": True})
                hid = h["correlation_id"]
                for _ in range(50):
                    if wb.runs[hid].get("status") == "held":
                        break
                    time.sleep(0.2)
                self.assertEqual(wb.runs[hid]["status"], "held")
                time.sleep(2.5)                                # the lander has asked again, still held
                RN.send(wb.client, wb.runs[hid])
                for _ in range(100):
                    if wb.runs[hid].get("status") not in ("queued", "held"):
                        break
                    time.sleep(0.2)
                self.assertEqual(wb.runs[hid]["status"], "done")
            finally:
                self.assertEqual(wb.stop_model()["model"], "none")
                W.STANDIN_WORKSPACE, W.LLM_WORKSPACE = saved


if __name__ == "__main__":
    unittest.main()
