"""W3 · the workbench's view models: every screen's decisions, on a synthetic two-player game."""
from __future__ import annotations

import sys
import unittest

from fixtures import REPO

sys.path.insert(0, str(REPO / "src"))

from sc2analyst import adapter_sc2 as A, workbench_vm as V  # noqa: E402


def unit(tag, owner, typ="Marine", kinds=("army",), born=10.0, died=None, kpid=None, m=50, how="born",
         vk=True, x=20.0, y=20.0):
    return {"tag": tag, "owner": owner, "born": born, "done": born if how == "born" else born + 60,
            "died": died, "how": how, "kinds": list(kinds), "m": m, "g": 0, "sup": 1.0, "vk": vk,
            "types": [[born, typ]], "kpid": kpid, "ktype": "Zergling" if kpid else None,
            "x": x, "y": y, "dx": x if died else None, "dy": y if died else None, "ctag": None}


def sample(pid, t, rate, bank, army=0, lost=0):
    v = [0] * len(A.STATS_FIELDS)
    v[A.STAT["minerals_current"]] = bank
    v[A.STAT["minerals_used_current_army"]] = army
    v[A.STAT["minerals_lost_army"]] = lost
    return {"pid": pid, "t": t, "m_rate": rate, "g_rate": 0, "workers": 12, "food_used": 20,
            "food_made": 30, "v": v}


def game():
    units = [unit(1, 1, "SCV", ("worker",), born=0.0),
             unit(2, 2, "Drone", ("worker",), born=0.0, died=101.0, kpid=1),
             unit(3, 2, "Zergling", born=50.0, died=102.0, kpid=1, m=25),
             unit(4, 2, "Lurker", born=50.0, died=103.0, kpid=1, m=0, vk=False),
             unit(5, 1, "CommandCenter", ("structure", "townhall"), born=40.0, how="init", m=400),
             unit(6, 1, "Marine", born=60.0, died=104.0, kpid=2)]
    stats = []
    for t in range(0, 241, 10):
        stats.append(sample(1, float(t), 600, 100 + t, army=50, lost=50 if t >= 110 else 0))
        stats.append(sample(2, float(t), 500, 100, army=75, lost=75 if t >= 110 else 0))
    return {"map": "No Such Map", "start_time": "2026-10-01 12:34:56", "build": 1, "end": 240.0,
            "players": [{"pid": 1, "team": 1, "race": "Terran", "result": "Win", "name": "Player_1",
                         "is_owner": True},
                        {"pid": 2, "team": 2, "race": "Zerg", "result": "Loss", "name": "Pq1",
                         "is_owner": False}],
            "units": units, "stats": stats, "owner_changes": [], "positions": [],
            "upgrades": [[0.0, 1, "SomeDefault"], [150.0, 1, "Stimpack"]], "camera": [], "commands": []}


class Naming(unittest.TestCase):
    def test_a_key_is_map_and_minute_never_a_file_name(self):
        self.assertEqual(V.game_key("Sandstorm CE", "2026-09-23 16:07:12"), "Sandstorm_CE_2026-09-23_1607")

    def test_the_owners_team_is_team_a_and_names_hide_aliases(self):
        g = game()
        g["players"].reverse()
        self.assertEqual(V.team_order(g), [1, 2])
        self.assertEqual(V.names(g), {1: "Player_1", 2: "Enemy_1"})

    def test_a_3v3_numbers_like_the_demo_replays(self):
        """The owner is number 1 of his team: allies Ally_2, Ally_3; enemies Enemy_1.. (pid order)."""
        g = {"players": [{"pid": pid, "team": team, "is_owner": pid == 2, "name": "x%d" % pid}
                         for pid, team in ((1, 1), (2, 1), (3, 1), (4, 2), (5, 2), (6, 2))]}
        self.assertEqual(V.pick.side_names(g), {1: "Ally_2", 2: "x2", 3: "Ally_3",
                                                4: "Enemy_1", 5: "Enemy_2", 6: "Enemy_3"})

    def test_without_the_owner_names_are_by_team(self):
        g = game()
        g["players"][0]["is_owner"] = False
        self.assertEqual(V.names(g)[1], "Team 1 · 1")


class Library(unittest.TestCase):
    ROWS = [{"file": "a.json.gz", "map": "M", "start_time": "2026-01-01 10:00:00", "format": "1v1",
             "end": 600, "races": {"1": ["Terran"], "2": ["Zerg"]}, "owner_played": True, "owner_result": "Win"},
            {"file": "b.json.gz", "map": "M", "start_time": "2026-01-01 10:00:30", "format": "1v1",
             "end": 300, "races": {"1": ["Protoss"], "2": ["Zerg"]}, "owner_played": False, "owner_result": None}]

    def test_same_minute_keys_are_made_unique_and_files_stay_server_side(self):
        vm = V.library_vm(self.ROWS)
        keys = [g["key"] for g in vm["games"]]
        self.assertEqual(len(set(keys)), 2)
        self.assertTrue(all("file" not in g for g in vm["games"]))
        self.assertEqual(set(vm["files"].values()), {"a.json.gz", "b.json.gz"})

    def test_the_opening_filter_reads_each_games_classes(self):
        rows = [dict(self.ROWS[0], openings=["FAST EXPAND", "ONE-BASE ATTACK"]), self.ROWS[1]]
        vm = V.library_vm(rows, {"opening": "ONE-BASE ATTACK"})
        self.assertEqual(vm["shown"], 1)
        self.assertEqual(vm["filters"]["openings"], ["ONE-BASE ATTACK", "FAST EXPAND"])

    def test_filters(self):
        self.assertEqual(V.library_vm(self.ROWS, {"race": "Protoss"})["shown"], 1)
        self.assertEqual(V.library_vm(self.ROWS, {"owner": "1"})["shown"], 1)
        self.assertEqual(V.library_vm(self.ROWS, {"result": "Win"})["shown"], 1)


class Game(unittest.TestCase):
    def test_debug_adds_the_ledgers_series_and_one_second_steps(self):
        normal, debug = V.game_vm(game()), V.game_vm(game(), debug=True)
        self.assertEqual((normal["step"], debug["step"]), (60, 1))
        self.assertLess(len(normal["gap"]["lines"]), len(debug["gap"]["lines"]))
        self.assertIn("workers", [m["key"] for m in debug["metrics"]])
        self.assertNotIn("workers", [m["key"] for m in normal["metrics"]])

    def test_every_series_says_where_it_comes_from(self):
        vm = V.game_vm(game(), debug=True)
        provs = {pl["metrics"][k]["provenance"] for pl in vm["players"] for k in pl["metrics"]}
        self.assertTrue(provs <= {"exact", "sample", "integrated", "exact (count)"})

    def test_one_scale_per_metric_across_players(self):
        vm = V.game_vm(game())
        lo, hi = vm["ranges"]["bank"]
        self.assertEqual((lo, hi), (100, 340))

    def test_the_fight_strip_carries_both_verdicts(self):
        vm = V.game_vm(game())
        self.assertEqual(len(vm["fights"]), 1)
        f = vm["fights"][0]
        self.assertEqual(f["lost_less_label"], "Your team")       # team 1 lost 50, team 2 lost 75
        self.assertIn("benefit_5min", f)

    def test_events_skip_the_setup_upgrade_and_name_the_town_hall(self):
        kinds = [(e["kind"], e["t"]) for e in V.game_vm(game())["events"]]
        self.assertIn(("town hall", 40.0), kinds)
        self.assertIn(("upgrade", 150.0), kinds)
        self.assertNotIn(("upgrade", 0.0), kinds)


class Snapshot(unittest.TestCase):
    def test_an_exact_cell_has_no_age_a_sampled_cell_has_its_own(self):
        vm = V.snapshot_vm(game(), 15.0)
        me = vm["players"][0]["cells"]
        self.assertEqual(me["town_halls"]["age"], 0)
        self.assertEqual(me["bank"]["age"], 5.0)
        self.assertEqual(me["bank"]["v"], 110)

    def test_no_map_layer_says_so_instead_of_drawing(self):
        vm = V.snapshot_vm(game(), 15.0, positions=object())
        self.assertIsNone(vm["minimap"])
        self.assertIn("no minimap", vm["minimap_note"])


class Fight(unittest.TestCase):
    def test_losses_by_class_and_the_unpriced_counted_apart(self):
        vm = V.fight_vm(game(), 1)
        enemy = next(p for p in vm["players"] if p["pid"] == 2)
        self.assertEqual(enemy["losses"]["worker"], {"value": 50, "count": 1})
        self.assertEqual(enemy["losses"]["army"], {"value": 25, "count": 2})   # zergling + unpriced lurker
        self.assertEqual(enemy["unpriced"], 1)

    def test_before_and_after_bracket_the_fight(self):
        vm = V.fight_vm(game(), 1)
        self.assertLessEqual(vm["before_t"], vm["start"])
        self.assertGreaterEqual(vm["after_t"], vm["end"])

    def test_the_death_rows_only_in_debug(self):
        self.assertNotIn("death_rows", V.fight_vm(game(), 1))
        self.assertEqual(len(V.fight_vm(game(), 1, debug=True)["death_rows"]), 4)

    def test_an_unknown_fight_is_refused(self):
        with self.assertRaises(KeyError):
            V.fight_vm(game(), 9)


if __name__ == "__main__":
    unittest.main()
