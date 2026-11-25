"""S7 · the page: standalone, and its payload is the repositories' output exactly."""
from __future__ import annotations

import json
import re
import sys
import unittest

from fixtures import LIBRARY_TWINS, REPO

sys.path.insert(0, str(REPO / "src"))


@unittest.skipUnless(LIBRARY_TWINS.exists(), "twins on L: absent")
class Page(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sc2analyst import page, pick
        cls.game = pick.load(pick.ONE_V_ONE)
        cls.html, cls.payload = page.page_for(cls.game)

    def test_no_external_reference(self):
        refs = re.findall(r'(?:src|href)\s*=\s*"([^"]+)"', self.html)
        outside = [r for r in refs if not (r.startswith("data:") or r.startswith("#"))]
        self.assertEqual(outside, [])
        self.assertNotIn("<link", self.html)
        self.assertNotRegex(self.html, r"@import|url\(http")

    def test_payload_is_the_analysis_byte_for_byte(self):
        from sc2analyst import analyse, page
        embedded = page.payload_of(self.html)
        fresh = analyse.analyse(self.game)
        dump = lambda o: json.dumps(o, separators=(",", ":"), sort_keys=True)  # noqa: E731
        self.assertEqual(dump(embedded), dump(fresh))

    def test_three_tabs_and_every_model_slot_waits(self):
        for tab in ("strategic", "macro", "improve"):
            self.assertIn('id="%s"' % tab, self.html)
            self.assertIn('id="assess-%s"' % tab, self.html)
        self.assertEqual(self.html.count("awaiting the assessment stage"), 4)

    def test_no_alias_reaches_the_page(self):
        aliases = {p["name"] for p in self.game["players"] if not p["is_owner"]}
        for a in aliases:
            self.assertNotIn(a, self.html)


if __name__ == "__main__":
    unittest.main()
