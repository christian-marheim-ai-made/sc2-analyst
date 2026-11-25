"""W8 · the task registry: facts with sources, versions, every family's check, and a run end to end
on llm-bootstrap's stand-in - submit, held in Check, sent, landed, checked, recorded."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from fixtures import REPO

sys.path.insert(0, str(REPO / "src"))

from sc2analyst import llm, runs as R, tasks as TK  # noqa: E402
from test_workbench_vm import game  # noqa: E402

FACTS = {"F1": {"id": "F1", "text": "Your team lost 50 value; the enemy 75.", "team": "Your team"},
         "F2": {"id": "F2", "text": "Enemy 1 lost 2 workers.", "team": "Enemy team"}}


class Checks(unittest.TestCase):
    def test_claims_need_existing_facts_and_their_numbers(self):
        a = {"claims": [{"text": "We lost 50, they lost 75.", "cites": ["F1"]},
                        {"text": "They lost 900.", "cites": ["F1"]},
                        {"text": "Something.", "cites": ["F9"]}], "cannot_answer": []}
        v = TK.check_claims(a, FACTS)
        self.assertEqual(len(v["kept"]), 1)
        self.assertEqual([d["why"][:9] for d in v["dropped"]], ["number(s)", "cites unk"])

    def test_not_json_drops_the_answer(self):
        self.assertIsNotNone(TK.check_claims(None, FACTS)["answer_dropped"])

    def test_a_fight_answer_that_contradicts_dl29_is_dropped_whole(self):
        a = {"claims": [{"text": "We lost 50.", "cites": ["F1"]}], "cannot_answer": [], "benefited": "B",
             "deciding": "army"}
        v = TK.check_fight(a, FACTS, {"benefited": "A"})
        self.assertEqual((len(v["kept"]), v["answer_dropped"][:5]), (0, "names"))
        self.assertEqual(len(TK.check_fight(dict(a, benefited="A"), FACTS, {"benefited": "A"})["kept"]), 1)

    def test_an_opening_answer_must_agree_with_dl27(self):
        a = {"claims": [], "cannot_answer": [], "caught_up": True}
        self.assertIsNotNone(TK.check_opening(a, FACTS, {"caught_up": False})["answer_dropped"])

    def test_a_study_claim_of_cause_is_dropped(self):
        a = {"claims": [{"text": "Attacking causes wins.", "cites": ["F1"]}], "cannot_answer": []}
        self.assertEqual(TK.check_study(a, FACTS, {})["dropped"][0]["why"], "states a cause; the study is an association")

    def test_a_window_claim_about_the_other_team_must_cite_it(self):
        a = {"claims": [{"text": "The Enemy team lost 50.", "cites": ["F1"]}], "cannot_answer": []}
        self.assertEqual(len(TK.check_window(a, FACTS, {"me_team": "Your team"})["dropped"]), 1)


class Registry(unittest.TestCase):
    def test_four_families_each_with_a_version(self):
        self.assertEqual(sorted(TK.TASKS), ["T-fight", "T-opening", "T-study", "T-window"])
        self.assertEqual(len({t.version for t in TK.TASKS.values()}), 4)

    def test_a_changed_question_is_a_new_version(self):
        t = TK.TASKS["T-fight"]
        self.assertNotEqual(t.version, TK.Task(t.id, t.target, t.question + "!", t.schema, t.facts, t.check).version)

    def test_every_fact_carries_its_source(self):
        facts, truth = TK.fight_facts({"game": game(), "fight": 1})
        self.assertTrue(all(r["src"]["repo"] and r["src"]["key"] for r in facts.rows))
        self.assertIn(truth["benefited"], ("A", "B", "even"))

    def test_over_budget_is_refused_never_cut(self):
        fl = TK.FactList()
        for _ in range(2000):
            fl.add("x" * 20, "test", "k")
        with self.assertRaises(ValueError):
            fl.render()

    def test_a_study_is_locked_until_reviewed(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "S-C" / "v1").mkdir(parents=True)
            with self.assertRaises(TK.Locked):
                TK.study_facts({"studies": d, "study": "S-C"})


@unittest.skipUnless(llm.BOOT.exists(), "vendored bootstrap absent")
class EndToEnd(unittest.TestCase):
    def test_submit_held_send_land_check_record(self):
        with tempfile.TemporaryDirectory() as d:
            ws, records = Path(d) / "ws", Path(d) / "sc2"
            with llm.Session(["thinker"], settings=llm.MOCK, workspace=ws) as s:
                run = R.submit(s.app, TK.TASKS["T-fight"], {"game": game(), "fight": 1, "label": "synthetic F1"},
                               debug=True, scope="synthetic")
                first = R.land(s.app, run, within_s=60, records=records)
                self.assertEqual(first["status"], "held")            # waits in the bootstrap's Check
                h = R.held(s.app, run)
                self.assertIn("FACTS", json.dumps(h))
                R.send(s.app, run)
                rec = R.land(s.app, run, within_s=60, records=records)
            self.assertEqual(rec["status"], "done")
            # the stand-in answers made-up words: the check says so, and the record keeps it
            self.assertEqual(rec["verdicts"]["answer_dropped"], "the answer is not the JSON asked for")
            self.assertTrue((records / ("%s.json" % run["correlation_id"])).exists())
            line = json.loads((records / "index.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual((line["task"], line["status"]), ("T-fight", "done"))
            self.assertTrue((ws / "requests" / run["correlation_id"]).exists())   # the bootstrap's own record


if __name__ == "__main__":
    unittest.main()
