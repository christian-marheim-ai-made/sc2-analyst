"""S8 · the session through llm-bootstrap, on its stand-in: no card, no lock, no model."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from fixtures import REPO

sys.path.insert(0, str(REPO / "src"))

from sc2analyst import llm  # noqa: E402


@unittest.skipUnless(llm.BOOT.exists(), "vendored bootstrap absent")
class StandIn(unittest.TestCase):
    def test_open_ask_swap_close_stop(self):
        with tempfile.TemporaryDirectory() as d:
            with llm.Session(["thinker"], settings=llm.MOCK, workspace=Path(d)) as s:
                a = s.ask("thinker", "system words", "count the rows", max_tokens=40,
                          reasoning_budget=None, name="t1")
                self.assertEqual(a["status"], "done")
                self.assertTrue(a["text"])
                s.swap("thinker", "wordsmith")
                b = s.ask("wordsmith", "system", "rewrite", max_tokens=20, reasoning_budget=None,
                          name="t2")
                self.assertEqual(b["status"], "done")
                proc = s.server.proc if hasattr(s.server, "proc") else None
            # the server is gone once the block is left
            if proc is not None:
                self.assertIsNotNone(proc.poll())

    def test_the_server_stops_when_the_block_raises(self):
        with tempfile.TemporaryDirectory() as d:
            box = {}
            with self.assertRaises(RuntimeError):
                with llm.Session(["thinker"], settings=llm.MOCK, workspace=Path(d)) as s:
                    box["s"] = s
                    raise RuntimeError("boom")
            proc = getattr(box["s"].server, "proc", None)
            if proc is not None:
                self.assertIsNotNone(proc.poll())


if __name__ == "__main__":
    unittest.main()
