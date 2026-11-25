"""src/sc2analyst/rename.py: the rename table and the old player labels, one deterministic pass."""
from __future__ import annotations

import json
import sys
import unittest

from fixtures import REPO

sys.path.insert(0, str(REPO / "src"))
from sc2analyst import rename as RN  # noqa: E402

TABLE = [("superbob", "Ally_2"), ("bob", "Ally_2"), ("tester", "Player_1")]


class Names(unittest.TestCase):
    def test_case_is_copied(self):
        self.assertEqual(RN.apply("Tester tester TESTER TeStEr", TABLE, relabel=False),
                         "Player_1 player_1 PLAYER_1 Player_1")

    def test_longest_first_and_glued_or_escaped_names(self):
        self.assertEqual(RN.apply("Superbob met bob; \\u2014Tester's team", TABLE),
                         "Ally_2 met ally_2; \\u2014Player_1's team")

    def test_base64_images_untouched(self):
        uri = "data:image/png;base64,QWxseSAxVGVzdGVyYm9i"
        self.assertEqual(RN.apply("<img src=\"%s\"> Tester" % uri, TABLE), "<img src=\"%s\"> Player_1" % uri)


class Labels(unittest.TestCase):
    def test_old_labels_one_pass(self):
        self.assertEqual(RN.apply("Ally 1 and Ally 2 met Enemy 3; Ally_2 stays", TABLE),
                         "Ally_2 and Ally_3 met Enemy_3; Ally_2 stays")

    def test_after_an_escape_yes_inside_a_word_no(self):
        self.assertEqual(RN.apply("\\nAlly 1 left; Rally 1 is a word; Enemy 12x", TABLE),
                         "\\nAlly_2 left; Rally 1 is a word; Enemy_12x")

    def test_json_keys_and_values(self):
        rules = RN.Rules(TABLE)
        got = rules.json({"Tester": ["Enemy 1 hit Ally 1", 3, None], "n": 1.5})
        self.assertEqual(got, {"Player_1": ["Enemy_1 hit Ally_2", 3, None], "n": 1.5})
        self.assertEqual(rules.counts, {"tester": 1, "Enemy N": 1, "Ally N": 1})

    def test_load_table(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.json"
            p.write_text(json.dumps([["bob", "Ally_2"]]), encoding="utf-8")
            self.assertEqual(RN.load_table(p), [("bob", "Ally_2")])


if __name__ == "__main__":
    unittest.main()
