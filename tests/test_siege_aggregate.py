"""W6b · the aggregation seam (select, registry, the sum) and its first repository, siege tanks."""
from __future__ import annotations

import sys
import unittest

from fixtures import REPO

sys.path.insert(0, str(REPO / "src"))

from sc2analyst import aggregate as AG, siege as S  # noqa: E402


def tank(tag, owner, born, types, died=None):
    return {"tag": tag, "owner": owner, "born": born, "done": born, "died": died, "how": "born",
            "kinds": ["army"], "m": 150, "g": 125, "vk": True, "types": types, "kpid": 2 if died else None,
            "ktype": "Marine" if died else None, "x": 50, "y": 50, "dx": 50 if died else None,
            "dy": 50 if died else None, "sup": 3.0}


def victim(tag, t, ktag_type):
    return {"tag": tag, "owner": 2, "born": 1.0, "done": 1.0, "died": t, "how": "born", "kinds": ["army"],
            "m": 100, "g": 0, "vk": True, "types": [[1.0, "Marine"]], "kpid": 1, "ktype": ktag_type,
            "x": 60, "y": 60, "dx": 60, "dy": 60, "sup": 1.0}


def game():
    # tank 1 sieges at 90, before the fight (100-110); tank 2 sieges at 105, during it; tank 3 never
    t1 = tank(1, 1, 10.0, [[10.0, "SiegeTank"], [90.0, "SiegeTankSieged"]])
    t2 = tank(2, 1, 10.0, [[10.0, "SiegeTank"], [105.0, "SiegeTankSieged"], [140.0, "SiegeTank"]])
    t3 = tank(3, 1, 10.0, [[10.0, "SiegeTank"]], died=108.0)
    t3["types"] = [[10.0, "SiegeTank"]]
    return {"map": "Nowhere", "end": 200.0, "players": [{"pid": 1, "team": 1}, {"pid": 2, "team": 2}],
            "units": [t1, t2, t3, victim(10, 100.0, "SiegeTankSieged"), victim(11, 104.0, "SiegeTank")],
            "positions": [], "stats": [], "upgrades": [], "owner_changes": [], "commands": []}


class Siege(unittest.TestCase):
    def test_intervals(self):
        self.assertEqual(S.intervals(game()["units"][1]), [(105.0, 140.0)])
        self.assertEqual(S.intervals(game()["units"][0]), [(90.0, None)])

    def test_before_during_never_and_the_losses(self):
        r = S.per_game(game(), 1)
        self.assertEqual((r["tanks"], r["siege_ups"], r["fights_with_tanks"]), (3, 2, 1))
        self.assertEqual((r["sieged_at_start"], r["sieged_in_fight"], r["never_sieged"]), (1, 1, 1))
        self.assertEqual((r["lost_unsieged"], r["lost_sieged"]), (1, 0))
        self.assertEqual((r["killed_sieged"], r["killed_unsieged"]), (100, 100))
        self.assertEqual(r["sieged_seconds"], (200 - 90) + (140 - 105))
        self.assertEqual(r["spot_unknown"], 2)                 # no map layer, no positions

    def test_the_sum_takes_shares_from_the_sums(self):
        a, b = S.per_game(game(), 1), S.per_game(game(), 1)
        t = S.total([a, b])
        self.assertEqual(t["tanks"], 6)
        self.assertEqual(t["share_sieged_at_start"], round(100 * 2 / 6, 1))
        self.assertEqual(t["share_lost_unsieged"], 100.0)


class Seam(unittest.TestCase):
    ROWS = [{"file": "a", "map": "M", "start_time": "2026-10-01 10:00", "format": "3v3", "owner_played": True},
            {"file": "b", "map": "M", "start_time": "2026-10-03 10:00", "format": "3v3", "owner_played": True},
            {"file": "c", "map": "N", "start_time": "2026-10-02 10:00", "format": "2v2", "owner_played": True},
            {"file": "d", "map": "M", "start_time": "2026-10-04 10:00", "format": "3v3", "owner_played": False}]

    def test_the_owners_games_newest_first_narrowed(self):
        self.assertEqual([r["file"] for r in AG.select(self.ROWS)], ["b", "c", "a"])
        self.assertEqual([r["file"] for r in AG.select(self.ROWS, fmt="3v3")], ["b", "a"])

    def test_the_repository_is_registered_by_name(self):
        self.assertIn("siege", AG.REPOSITORIES)
        self.assertEqual(AG.REPOSITORIES["siege"].version, "v1")

    def test_a_selection_has_a_stable_name(self):
        self.assertEqual(AG.slug(last=30, fmt="3v3"), "fmt-3v3_last-30")


if __name__ == "__main__":
    unittest.main()
