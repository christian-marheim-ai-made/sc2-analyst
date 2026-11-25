"""S10/S11 · the deterministic checks every model claim passes before it may stand."""
from __future__ import annotations

import sys
import unittest

from fixtures import REPO

sys.path.insert(0, str(REPO / "src"))

from sc2analyst import assess as A  # noqa: E402

FACTS = {
    "F1": {"team": None, "text": "Fight 21, 15:13-17:16 at open ground: Team A lost 21310 value (205 deaths) "
                                 "and Team B lost 20200 value (138 deaths)."},
    "F2": {"team": 1, "text": "Player_1: lost 13500 value, killed 30600 (ratio 2.27); lost 34% of what it collected."},
    "F3": {"team": 2, "text": "Enemy 2: lost 24050 value, killed 7095 (ratio 0.3); lost 85% of what it collected."},
}


class Claims(unittest.TestCase):
    def test_quoted_numbers_stand(self):
        self.assertIsNone(A.check_claim("At 15:13 Team A lost 21,310 value against 20200.", ["F1"], FACTS))

    def test_a_computed_number_is_dropped(self):
        why = A.check_claim("Team A lost 1110 more than Team B.", ["F1"], FACTS)
        self.assertIn("1110", why)

    def test_a_number_from_an_uncited_fact_is_dropped(self):
        self.assertIsNotNone(A.check_claim("Player_1 traded at 2.27.", ["F1"], FACTS))

    def test_small_counts_are_words(self):
        self.assertIsNone(A.check_claim("Two of the 3 big fights went to Team A.", ["F1"], FACTS))

    def test_unknown_and_missing_citations(self):
        self.assertEqual(A.check_claim("x", [], FACTS), "cites no fact")
        self.assertIn("unknown", A.check_claim("x", ["F9"], FACTS))


class TeamFindings(unittest.TestCase):
    def finding(self, **kw):
        f = {"kind": "strength", "claim": "Player_1 killed 30600 for 13500.", "evidence": ["F2"],
             "mirror": "Enemy 2 lost 85% of what it collected.", "mirror_evidence": ["F3"], "impact": "high"}
        f.update(kw)
        return f

    def test_a_mirrored_finding_is_kept(self):
        c = A.check_team_findings({"findings": [self.finding()], "cannot_answer": []}, FACTS, 1)
        self.assertEqual((len(c["kept"]), len(c["dropped"])), (1, 0))

    def test_a_mirror_about_the_same_team_is_dropped(self):
        f = self.finding(mirror="Player_1 lost 34%.", mirror_evidence=["F2"])
        c = A.check_team_findings({"findings": [f], "cannot_answer": []}, FACTS, 1)
        self.assertIn("mirror", c["dropped"][0]["reason"])

    def test_not_json_drops_everything(self):
        self.assertEqual(A.check_team_findings(None, FACTS, 1)["kept"], [])


class Final(unittest.TestCase):
    def test_evidence_is_carried_as_text(self):
        ans = {t: {"findings": [{"kind": "macro", "claim": "Player_1 lost 34% of income.", "evidence": ["F2"],
                                 "mirror": ""}], "cannot_answer": []} for t in ("strategic", "macro", "improve")}
        out, dropped = A.check_final(ans, FACTS)
        self.assertEqual(dropped, 0)
        self.assertTrue(out["macro"]["findings"][0]["evidence"][0].startswith("F2: Player_1"))


class Prose(unittest.TestCase):
    def test_a_paragraph_with_a_new_number_is_dropped(self):
        plot = {"title": "t", "beats": [{"fact": "F1", "line": "At 15:13 the armies met.", "text": FACTS["F1"]["text"]}]}
        keep, dropped = A.check_prose({"paragraphs": ["At 15:13 the armies met.", "Then 9000 marines fell."]}, plot)
        self.assertEqual((len(keep), dropped), (1, 1))


class Identifiers(unittest.TestCase):
    """Names are not quantities: measured on the 20-game run, they were most of the false drops."""

    def test_fight_ids_fact_ids_bases_and_players_are_names(self):
        self.assertEqual(A.numbers("In Fight 26 (F12) at base 16, Enemy 3 lost 21310 at 15:13, 34%."),
                         {"21310", "15:13", "34%"})

    def test_the_underscored_player_names_are_names(self):
        self.assertEqual(A.numbers("Ally_3 and Player_1 held while Enemy_2 lost 4100 at 9:08."),
                         {"4100", "9:08"})

    def test_a_mirror_may_quote_the_claims_facts(self):
        f = {"kind": "strength", "claim": "Player_1 killed 30600.", "evidence": ["F2"],
             "mirror": "Enemy 2 lost 85%, while Player_1 killed 30600.", "mirror_evidence": ["F3"],
             "impact": "high"}
        c = A.check_team_findings({"findings": [f], "cannot_answer": []}, FACTS, 1)
        self.assertEqual(len(c["kept"]), 1)

    def test_a_mirror_without_its_own_fact_is_still_dropped(self):
        f = {"kind": "strength", "claim": "Player_1 killed 30600.", "evidence": ["F2"],
             "mirror": "Player_1 killed 30600.", "mirror_evidence": [], "impact": "high"}
        c = A.check_team_findings({"findings": [f], "cannot_answer": []}, FACTS, 1)
        self.assertEqual(len(c["kept"]), 0)


if __name__ == "__main__":
    unittest.main()
