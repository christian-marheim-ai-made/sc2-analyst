"""W6 · the studies' aggregation, on synthetic rows."""
from __future__ import annotations

import sys
import unittest
from collections import Counter

from fixtures import REPO

sys.path.insert(0, str(REPO / "src"))

from sc2analyst import studies as S  # noqa: E402


def g(fmt, t1, t2, winner):
    return {"fmt": fmt, "teams": {1: Counter(t1), 2: Counter(t2)}, "winner": winner}


class OpeningsMeet(unittest.TestCase):
    ROWS = [g("3v3", {"ONE-BASE ATTACK": 1, "STANDARD": 2}, {"FAST EXPAND": 2, "STANDARD": 1}, 1),
            g("3v3", {"ONE-BASE ATTACK": 1, "STANDARD": 2}, {"FAST EXPAND": 1, "STANDARD": 2}, 2),
            g("3v3", {"STANDARD": 3}, {"FAST EXPAND": 3}, 1),                       # no attacker: out
            g("3v3", {"ONE-BASE ATTACK": 1}, {"ONE-BASE ATTACK": 1}, 1),            # both attack: out
            g("2v2", {"FAST EXPAND": 2}, {"ONE-BASE ATTACK": 1, "STANDARD": 1}, 2),
            g("2v2", {"STANDARD": 2}, {"ONE-BASE ATTACK": 1, "STANDARD": 1}, 1)]    # no fast expander

    def test_attack_against_fast_expand_counts_only_the_clean_matchups(self):
        rows = {r["format"]: r for r in S.sa_rows(self.ROWS)}
        self.assertEqual((rows["3v3"]["games"], rows["3v3"]["attacking_team_won"]), (2, 50.0))
        self.assertEqual((rows["2v2"]["games"], rows["2v2"]["attacking_team_won"]), (1, 100.0))

    def test_any_one_sided_attack(self):
        rows = {r["format"]: r for r in S.sa_any(self.ROWS)}
        self.assertEqual((rows["2v2"]["games"], rows["2v2"]["attacking_team_won"]), (2, 50.0))


class CatchingUp(unittest.TestCase):
    def test_by_the_minute_the_attack_started(self):
        a = [{"framing": "pair", "t0": 200.0, "E0": 300, "r_before": 100, "caught_up": True, "attacker_won": True},
             {"framing": "pair", "t0": 230.0, "E0": 500, "r_before": 200, "caught_up": False, "attacker_won": False},
             {"framing": "team", "t0": 230.0, "E0": 900, "r_before": 0, "caught_up": False, "attacker_won": True}]
        r = S.sb_by_minute(a, "pair")[0]
        self.assertEqual((r["minute"], r["attacks"], r["caught_up"], r["won_if_caught"], r["won_if_not"]),
                         (3, 2, 50.0, 100.0, 0.0))


class WhatWasKilled(unittest.TestCase):
    def test_buckets_by_the_economys_share(self):
        self.assertEqual(S.bucket_of(S.econ_share({"worker": 0, "army": 500})), "none")
        self.assertEqual(S.bucket_of(S.econ_share({"worker": 100, "army": 400})), "1–25%")
        self.assertEqual(S.bucket_of(S.econ_share({"worker": 200, "army": 300})), "25–50%")
        self.assertEqual(S.bucket_of(S.econ_share({"worker": 400, "town hall": 400, "army": 100})), "over 50%")

    def test_the_effective_trade_counts_the_income_denied(self):
        rows = [{"lost": {"worker": 500, "army": 0}, "denied": 1000, "observed": -200},
                {"lost": {"worker": 0, "army": 500}, "denied": 0, "observed": -10}]
        out = {r["economy_share"]: r for r in S.sc_buckets(rows)}
        self.assertEqual(out["over 50%"]["effective_over_nominal_p50"], 3.0)
        self.assertEqual(out["none"]["effective_over_nominal_p50"], 1.0)


if __name__ == "__main__":
    unittest.main()
