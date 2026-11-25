"""W1 · the stats sample keeps all 39 numbers, in sc2reader's order (DL-23)."""
from __future__ import annotations

import sys
import unittest

from fixtures import REPO

sys.path.insert(0, str(REPO / "src"))

from sc2analyst import adapter_sc2 as A  # noqa: E402


def synthetic_event():
    """A PlayerStatsEvent built by sc2reader itself from made-up numbers: stats[i] = (i + 1) * 4096."""
    from sc2reader.events.tracker import PlayerStatsEvent
    return PlayerStatsEvent(4480, [2, [(i + 1) * 4096 for i in range(39)]], 95299)


class StatsRow(unittest.TestCase):
    def test_the_field_list_is_39_names_none_twice(self):
        self.assertEqual(len(A.STATS_FIELDS), 39)
        self.assertEqual(len(set(A.STATS_FIELDS)), 39)

    def test_every_number_lands_at_its_own_index(self):
        row = A.stats_row(synthetic_event(), 200.0)
        for i, name in enumerate(A.STATS_FIELDS):
            want = (i + 1) if name.startswith("food_") else (i + 1) * 4096  # food is stored / 4096
            self.assertEqual(row["v"][i], want, name)

    def test_the_v1_names_are_unchanged(self):
        row = A.stats_row(synthetic_event(), 200.0)
        v = row["v"]
        self.assertEqual(row["m_bank"], v[A.STAT["minerals_current"]])
        self.assertEqual(row["workers"], v[A.STAT["workers_active_count"]])
        self.assertEqual(row["army_value"], v[A.STAT["minerals_used_current_army"]]
                         + v[A.STAT["vespene_used_current_army"]])
        lost = sum(v[A.STAT[k % c]] for k in ("minerals_lost_%s", "vespene_lost_%s")
                   for c in ("army", "economy", "technology"))
        self.assertEqual(row["lost"], lost)
        self.assertEqual((row["t"], row["pid"]), (200.0, 2))


if __name__ == "__main__":
    unittest.main()
