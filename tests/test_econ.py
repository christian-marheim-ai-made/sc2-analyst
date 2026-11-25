"""W5 · income denied (DL-28), the least-squares fit, catching up (DL-27), the final position."""
from __future__ import annotations

import sys
import unittest

from fixtures import REPO

sys.path.insert(0, str(REPO / "src"))

from sc2analyst import econ as E  # noqa: E402


def worker(tag, owner, born, died=None, kpid=None):
    return {"tag": tag, "owner": owner, "born": born, "done": born, "died": died, "how": "born",
            "kinds": ["worker"], "m": 50, "g": 0, "vk": True, "types": [[born, "SCV"]], "kpid": kpid,
            "ktype": "Zergling" if kpid else None, "x": 10, "y": 10, "dx": 10 if died else None,
            "dy": 10 if died else None, "sup": 1.0}


def rows_for(pid_income_workers, until=400):
    """pid -> constant (income, workers) samples every 10 s."""
    out = {}
    for pid, (inc, w) in pid_income_workers.items():
        out[pid] = [(float(t), {"income": inc, "workers_game": w, "collected": 1000 + inc * t / 60,
                                "lost": 0, "standing": 1000 + inc * t / 60, "army": 0, "economy": 0,
                                "technology": 0}) for t in range(0, until + 1, 10)]
    return out


GAME = {"end": 400.0, "players": [{"pid": 1, "team": 1, "result": "Win"}, {"pid": 2, "team": 2, "result": "Loss"}],
        "units": [], "upgrades": [], "owner_changes": []}


class Denied(unittest.TestCase):
    def test_a_known_deficit_gives_its_lower_bound(self):
        # 10 workers; 2 die at 100 s; one rebuilt at 130 s, the other at 160 s - 30 + 60 = 90 worker-seconds short
        units = [worker(i, 2, 0.0) for i in range(8)] + [worker(8, 2, 0.0, died=100.0, kpid=1),
                                                         worker(9, 2, 0.0, died=100.0, kpid=1),
                                                         worker(10, 2, 130.0), worker(11, 2, 160.0)]
        g = dict(GAME, units=units)
        fight = {"start": 100.0, "end": 100.0}
        d = E.denied_income(g, fight, rows_for({2: (600, 10)}))[2]
        self.assertEqual((d["workers_lost"], d["pre"], d["worker_seconds"], d["back_at"]), (2, 10, 90.0, 160.0))
        self.assertEqual(d["per_worker_min"], 60.0)
        self.assertEqual(d["denied"], 90.0)                  # 90 worker-seconds x 60 a minute / 60

    def test_never_rebuilt_is_capped_at_the_horizon(self):
        units = [worker(i, 2, 0.0) for i in range(4)] + [worker(9, 2, 0.0, died=50.0, kpid=1)]
        d = E.denied_income(dict(GAME, units=units), {"start": 50.0, "end": 50.0}, rows_for({2: (300, 5)}))[2]
        self.assertEqual((d["worker_seconds"], d["back_at"]), (E.HORIZON_S, None))

    def test_no_workers_lost_denies_nothing(self):
        units = [worker(i, 2, 0.0) for i in range(4)]
        self.assertEqual(E.denied_income(dict(GAME, units=units), {"start": 50.0, "end": 60.0}, rows_for({2: (300, 4)})), {})


class Fit(unittest.TestCase):
    def test_least_squares_recovers_known_coefficients(self):
        X = [[a, b] for a in range(0, 50, 5) for b in range(0, 30, 7)]
        y = [3.0 + 2.0 * a - 0.5 * b for a, b in X]
        f = E.ols(X, y)
        for got, want in zip(f["coef"], [3.0, 2.0, -0.5]):
            self.assertAlmostEqual(got, want, places=6)
        self.assertLess(f["resid_sd"], 1e-6)

    def test_collinear_classes_are_refused(self):
        with self.assertRaises(ValueError):
            E.ols([[1, 2], [2, 4], [3, 6]], [1, 2, 3])


class Final(unittest.TestCase):
    def test_the_team_that_left_can_be_ahead(self):
        # the loser (team 2) collected more and had more income at the end: it left while ahead
        f = E.final_position(GAME, rows_for({1: (600, 30), 2: (900, 40)}))
        self.assertEqual((f["loser"], f["winner"], f["t"]), (2, 1, 400.0))
        self.assertTrue(f["loser_ahead"]["income"])
        self.assertTrue(f["loser_ahead"]["workers_game"])
        self.assertTrue(f["loser_ahead"]["standing"])

    def test_no_final_position_without_two_teams(self):
        g = dict(GAME, players=GAME["players"] + [{"pid": 3, "team": 3, "result": "Loss"}])
        self.assertIsNone(E.final_position(g, rows_for({1: (1, 1), 2: (1, 1), 3: (1, 1)})))


class CatchUp(unittest.TestCase):
    def test_an_attack_that_does_enough_damage_has_caught_up(self):
        fights = [{"id": 1, "start": 100.0, "end": 120.0}]
        rows = rows_for({1: (400, 12), 2: (600, 16)})
        for ts, r in rows[2]:                        # the expander loses 3000 in the fight
            if ts >= 110:
                r["lost"] = 3000
        g = dict(GAME, units=[])
        import sc2analyst.fights as F
        orig = F.fights
        F.fights = lambda game: [dict(f, lost_value_by_team={}, by_player={}) for f in fights]
        try:
            classes = {1: {"class": "ONE-BASE ATTACK", "first_expansion": None}, 2: {"class": "FAST EXPAND"}}
            kills = {1: [(105.0, 3000, 1, 2)]}
            rows_ = E.catch_up(g, rows, classes, kills)
        finally:
            F.fights = orig
        pair = next(r for r in rows_ if r["framing"] == "pair")
        self.assertEqual((pair["t0"], pair["t1"], pair["D"]), (100.0, 180.0, 3000))
        # the expander's lead at t1: 1000 + 600*180/60 - (1000 + 400*180/60) = 600; at t0-: ~333
        self.assertTrue(pair["caught_up"])
        self.assertEqual(pair["attacker_won"], True)


if __name__ == "__main__":
    unittest.main()
