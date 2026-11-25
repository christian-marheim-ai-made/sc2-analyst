"""W2 · the timeline: two clocks, never mixed (DL-22); collected and standing (DL-25 amended)."""
from __future__ import annotations

import sys
import unittest

from fixtures import REPO

sys.path.insert(0, str(REPO / "src"))

from sc2analyst import adapter_sc2 as A, timeline as T  # noqa: E402


def unit(tag, owner, typ="Marine", kinds=("army",), born=10.0, done=None, died=None, m=50, g=0,
         sup=1.0, how="born", types=None, vk=True):
    return {"tag": tag, "owner": owner, "type": typ, "born": born,
            "done": born if done is None and how == "born" else done, "died": died, "how": how,
            "kinds": list(kinds), "m": m, "g": g, "sup": sup, "vk": vk,
            "types": types or [[born, typ]], "kpid": None, "ktype": None}


def v(**named):
    out = [0] * len(A.STATS_FIELDS)
    for k, x in named.items():
        out[A.STAT[k]] = x
    return out


def sample(pid, t, rate=0, **named):
    return {"pid": pid, "t": t, "m_rate": rate, "g_rate": 0, "workers": named.pop("workers", 0),
            "food_used": 0, "food_made": 0, "v": v(**named)}


def game(units, stats=(), owner_changes=(), upgrades=(), end=100.0):
    return {"end": end, "players": [{"pid": 1, "team": 1}, {"pid": 2, "team": 2}, {"pid": 3, "team": 1}],
            "units": list(units), "stats": list(stats), "owner_changes": list(owner_changes),
            "upgrades": list(upgrades)}


class ExactClock(unittest.TestCase):
    def series(self, g, name, pid=1, step=1):
        tl = T.timeline(g, step)
        return dict(zip(tl["t"], tl["players"][pid][name]))

    def test_alive_from_done_until_died(self):
        s = self.series(game([unit(1, 1, born=10, died=20)]), "army_value")
        self.assertEqual((s[9.0], s[10.0], s[19.0], s[20.0]), (0, 50, 50, 0))

    def test_a_structure_counts_from_its_finish_not_its_start(self):
        g = game([unit(1, 1, typ="Barracks", kinds=("structure", "producer"), born=5, done=40,
                       how="init", m=150)])
        s = self.series(g, "structures_value")
        self.assertEqual((s[39.0], s[40.0]), (0, 150))

    def test_one_never_finished_never_counts(self):
        g = game([unit(1, 1, typ="Barracks", kinds=("structure",), born=5, done=None, how="init",
                       died=30, m=150)])
        self.assertEqual(set(self.series(g, "structures_value").values()), {0})

    def test_an_owner_change_moves_the_unit(self):
        g = game([unit(7, 1, born=10)], owner_changes=[[30.0, 7, 1, 2]])
        s1, s2 = self.series(g, "army_value", 1), self.series(g, "army_value", 2)
        self.assertEqual((s1[29.0], s1[30.0], s2[29.0], s2[30.0]), (50, 0, 0, 50))

    def test_a_larva_counts_only_once_it_has_hatched_into_a_lasting_form(self):
        g = game([unit(3, 1, typ="Drone", kinds=("worker",), born=10, m=50,
                       types=[[10.0, "Larva"], [12.0, "Egg"], [25.0, "Drone"]])])
        s = self.series(g, "workers")
        self.assertEqual((s[11.0], s[24.0], s[25.0]), (0, 0, 1))

    def test_unpriced_army_is_counted_apart_never_as_zero_value(self):
        g = game([unit(1, 1, typ="Liberator", vk=False, m=0, born=10)])
        self.assertEqual((self.series(g, "army_value")[50.0], self.series(g, "army_unpriced")[50.0]), (0, 1))

    def test_setup_upgrades_at_zero_do_not_count(self):
        g = game([], upgrades=[[0.0, 1, "SomeDefault"], [60.0, 1, "Stimpack"]])
        s = self.series(g, "upgrades")
        self.assertEqual((s[0.0], s[59.0], s[60.0]), (0, 0, 1))


class SampleClock(unittest.TestCase):
    STATS = [sample(1, 0.0, rate=600, minerals_current=50, minerals_used_current_economy=1000),
             sample(1, 7.14, rate=600, minerals_current=120, minerals_used_current_economy=1000),
             sample(1, 14.28, rate=600, minerals_current=40, minerals_used_current_economy=1150,
                    minerals_lost_army=0)]

    def test_never_interpolated_the_last_sample_holds(self):
        tl = T.timeline(game([], self.STATS), 1)
        bank = dict(zip(tl["t"], tl["players"][1]["bank"]))
        self.assertEqual((bank[7.0], bank[8.0], bank[14.0], bank[15.0]), (50, 120, 120, 40))
        age = dict(zip(tl["t"], tl["age"][1]))
        self.assertEqual(age[8.0], round(8.0 - 7.14, 2))

    def test_none_before_the_first_sample(self):
        stats = [dict(s, t=s["t"] + 5) for s in self.STATS]
        tl = T.timeline(game([], stats), 1)
        self.assertIsNone(tl["players"][1]["bank"][0])
        self.assertIsNone(tl["age"][1][0])

    def test_collected_is_the_endowment_plus_the_rate_summed(self):
        rows = T.sampled_rows(game([], self.STATS))[1]
        self.assertEqual(rows[0][1]["collected"], 1050)                    # 50 bank + 1000 board
        self.assertEqual(rows[1][1]["collected"], round(1050 + 600 * 7.14 / 60, 1))
        self.assertEqual(rows[0][1]["off_board"], 0)

    def test_standing_is_collected_less_lost(self):
        stats = self.STATS[:2] + [sample(1, 14.28, rate=600, minerals_current=40,
                                         minerals_used_current_economy=1000, minerals_lost_army=100)]
        r = T.sampled_rows(game([], stats))[1][2][1]
        self.assertEqual(r["standing"], round(r["collected"] - 100, 1))


class Teams(unittest.TestCase):
    def test_a_team_is_its_players_summed_and_a_gap_is_a_minus_b(self):
        g = game([unit(1, 1, born=0), unit(2, 3, born=0, m=100), unit(3, 2, born=0, m=25)])
        tl = T.timeline(g, 60)
        self.assertEqual(tl["teams"][1]["army_value"][0], 150)
        self.assertEqual(T.gap(tl, 1, 2, "army_value")[0], 125)

    def test_a_team_value_is_unknown_while_any_player_is(self):
        g = game([], [sample(1, 0.0, minerals_current=50)])
        tl = T.timeline(g, 60)
        self.assertIsNone(tl["teams"][1]["bank"][0])                       # player 3 has no sample


if __name__ == "__main__":
    unittest.main()
