"""W11 · the frontier paste: a packet's text, the parser on replies as they come back, the import's checks."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from fixtures import REPO

sys.path.insert(0, str(REPO / "src"))

from sc2analyst import frontier as FR, tasks as TK  # noqa: E402
from test_workbench_vm import game  # noqa: E402

ANSWER = {"claims": [{"text": "We lost 50.", "cites": ["F2"]}], "cannot_answer": [], "benefited": "A",
          "deciding": "army"}
REPLIES = {
    "fenced": "Here is my analysis.\n```json\n%s\n```\nHope it helps!" % json.dumps(ANSWER),
    "bare": json.dumps(ANSWER),
    "prose around": "Sure - the answer: %s Let me know." % json.dumps(ANSWER),
    "nested braces in a string": json.dumps(dict(ANSWER, cannot_answer=["the {vision} is not in the data"])),
}


class Parse(unittest.TestCase):
    def test_every_shape_a_reply_comes_in(self):
        for name, reply in REPLIES.items():
            self.assertEqual(FR.parse(reply)["benefited"], "A", name)

    def test_no_json_is_none(self):
        self.assertIsNone(FR.parse("I cannot help with that."))
        self.assertIsNone(FR.parse("{broken: json"))


class PacketAndImport(unittest.TestCase):
    def test_the_packet_carries_the_same_prompt_and_the_shape(self):
        with tempfile.TemporaryDirectory() as d:
            pk = FR.packet(TK.TASKS["T-fight"], {"game": game(), "fight": 1, "key": "K"}, packets=Path(d))
            self.assertIn(TK.TASKS["T-fight"].system[:60], pk["text"])
            self.assertIn("FACTS", pk["text"])
            self.assertIn('"benefited"', pk["text"])
            self.assertTrue((Path(d) / ("%s.json" % pk["packet_id"])).exists())

    def test_an_imported_reply_meets_the_same_checks(self):
        with tempfile.TemporaryDirectory() as d:
            pk = FR.packet(TK.TASKS["T-fight"], {"game": game(), "fight": 1, "key": "K"}, packets=Path(d))
            fid = pk["facts"][0]["id"]
            # a claim citing a real fact with a number that is not in it: dropped, as for the local model
            reply = json.dumps({"claims": [{"text": "They lost 12345.", "cites": [fid]}], "cannot_answer": [],
                                "benefited": pk["truth"]["benefited"], "deciding": "army"})
            rec = FR.import_reply(pk["packet_id"], reply, packets=Path(d), records=Path(d) / "sc2")
            self.assertTrue(rec["model"].startswith("chatgpt-pro (pasted "))
            self.assertEqual(len(rec["verdicts"]["dropped"]), 1)
            self.assertTrue((Path(d) / "sc2" / ("%s.json" % rec["correlation_id"])).exists())


if __name__ == "__main__":
    unittest.main()
