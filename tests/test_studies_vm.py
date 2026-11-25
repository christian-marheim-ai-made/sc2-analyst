"""W7 · the Study screen: the listing, a study's tables, an aggregate's, and the owner's Reviewed mark.

Every row works in a temporary folder: no row ever writes a Reviewed mark on a real table."""
from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from fixtures import REPO

sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tools"))

import workbench as W  # noqa: E402
from sc2analyst import studies_vm as SV  # noqa: E402

SC = {"buckets": [{"economy_share": "none", "victims": 3, "nominal_p50": 500, "denied_p50": 0,
                   "effective_over_nominal_p50": 1.0, "observed_income_change_p50": 10}],
      "fit": {"coef": {"intercept": 300.0, "worker": -46.8, "town hall": -73.2, "other structure": -3.3,
                       "army": -8.8}, "n": 10, "resid_sd": 1.0}, "causation": "associations"}
AGG = {"repository": "siege", "version": "v1", "games": 1, "about": "tanks",
       "rows": [{"game": "Sandstorm CE 2026-09-23 16:07", "race": "Terran", "result": "Win", "tanks": 3}],
       "total": {"tanks": 3, "share_time_sieged": 50.0}}


def folders(tmp: Path):
    s = tmp / "studies" / "S-C" / "v1"
    s.mkdir(parents=True)
    (s / "table.json").write_text(json.dumps(SC), encoding="utf-8")
    a = tmp / "aggregates" / "siege" / "last-30" / "v1"
    a.mkdir(parents=True)
    (a / "table.json").write_text(json.dumps(AGG), encoding="utf-8")
    return tmp / "studies", tmp / "aggregates"


class ViewModels(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st, self.ag = folders(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_listing_says_what_is_not_reviewed(self):
        ls = SV.listing(self.st, self.ag)
        self.assertEqual([(s["id"], s["reviewed"]) for s in ls["studies"]], [("S-C", None)])
        self.assertEqual(ls["aggregates"][0]["selection"], "last-30")

    def test_a_study_has_its_tables_chart_and_the_causation_sentence(self):
        vm = SV.study_vm(self.st, "S-C")
        self.assertEqual(len(vm["tables"]), 2)
        self.assertEqual([b["label"] for b in vm["charts"][0]["bars"]], ["worker", "town hall", "other structure", "army"])
        self.assertEqual(vm["causation"], "associations")

    def test_an_aggregate_row_opens_its_game(self):
        vm = SV.aggregate_vm(self.ag, "siege", "last-30", "v1")
        self.assertEqual(vm["tables"][1]["hrefs"], ["#/game/Sandstorm_CE_2026-09-23_1607"])

    def test_the_mark_is_written_beside_the_table(self):
        SV.mark_reviewed(self.st / "S-C" / "v1")
        self.assertIsNotNone(SV.study_vm(self.st, "S-C")["reviewed"])


class ReviewedRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        st, ag = folders(Path(cls.tmp.name))
        with socket.socket() as so:
            so.bind(("127.0.0.1", 0))
            cls.port = so.getsockname()[1]
        cls.wb = W.Workbench(Path(cls.tmp.name), cls.port, studies=st, aggregates=ag)
        cls.httpd = W.serve(cls.wb, cls.port)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def post(self, body, origin=True):
        h = {"Content-Type": "application/json"}
        if origin:
            h["Origin"] = "http://127.0.0.1:%d" % self.port
        req = urllib.request.Request("http://127.0.0.1:%d/api/reviewed" % self.port, data=json.dumps(body).encode(),
                                     headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_only_the_page_can_mark_and_only_a_real_table(self):
        self.assertEqual(self.post({"kind": "study", "id": "S-C", "version": "v1"}, origin=False), 403)
        self.assertEqual(self.post({"kind": "study", "id": "../../x", "version": "v1"}), 404)
        self.assertEqual(self.post({"kind": "study", "id": "S-C", "version": "v1"}), 200)
        self.assertTrue((self.wb.studies / "S-C" / "v1" / "reviewed.json").exists())


if __name__ == "__main__":
    unittest.main()
