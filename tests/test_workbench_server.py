"""W3 · the workbench's routes, over real HTTP on a free port, against a synthetic record folder."""
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
from sc2analyst import adapter_sc2  # noqa: E402
from test_workbench_vm import game  # noqa: E402

KEY = "No_Such_Map_2026-10-01_1234"


def free_port() -> int:
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


class Routes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        adapter_sc2.save(game(), Path(cls.tmp.name) / "synthetic.json.gz")
        cls.port = free_port()
        cls.wb = W.Workbench(Path(cls.tmp.name), cls.port)
        cls.httpd = W.serve(cls.wb, cls.port)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read(), r.headers.get("Content-Type")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers.get("Content-Type")

    def post(self, path, body, origin=None):
        h = {"Content-Type": "application/json"}
        if origin:
            h["Origin"] = origin
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_the_page_and_the_vendored_kit_by_its_path(self):
        status, body, kind = self.get("/")
        self.assertEqual((status, kind.split(";")[0]), (200, "text/html"))
        status, body, _ = self.get("/llm-bootstrap-kit.js")
        self.assertEqual(body, W.KIT.read_bytes())

    def test_the_library_lists_the_game_by_key_never_by_file(self):
        status, body, _ = self.get("/api/library")
        lib = json.loads(body)
        self.assertEqual((status, lib["count"]), (200, 1))
        self.assertEqual(lib["games"][0]["key"], KEY)
        self.assertNotIn(b"synthetic", body)

    def test_a_game_its_snapshot_and_a_fight(self):
        s, body, _ = self.get("/api/game/%s/series?debug=1" % KEY)
        self.assertEqual((s, json.loads(body)["step"]), (200, 1))
        s, body, _ = self.get("/api/game/%s/at?t=15" % KEY)
        self.assertEqual((s, json.loads(body)["clock"]), (200, "0:15"))
        s, body, _ = self.get("/api/game/%s/fight/1" % KEY)
        self.assertEqual((s, json.loads(body)["id"]), (200, 1))

    def test_unknown_things_are_404_with_a_reason(self):
        s, body, _ = self.get("/api/game/Nowhere_2000-01-01_0000/series")
        self.assertEqual((s, json.loads(body)["reason"]), (404, "not_found"))
        s, body, _ = self.get("/api/game/%s/fight/99" % KEY)
        self.assertEqual(s, 404)

    def test_a_post_from_another_page_is_refused(self):
        s, body = self.post("/api/library/scan", {"folder": self.tmp.name})
        self.assertEqual((s, body["reason"]), (403, "origin_not_allowed"))
        s, body = self.post("/api/library/scan", {"folder": self.tmp.name}, origin="http://evil.example")
        self.assertEqual(s, 403)

    def test_another_host_name_is_refused(self):
        s, body, _ = self.get("/api/library", {"Host": "evil.example:%d" % self.port})
        self.assertEqual((s, json.loads(body)["reason"]), (403, "host_not_allowed"))

    def test_the_folder_listing_counts_replays(self):
        Path(self.tmp.name, "sub").mkdir(exist_ok=True)
        Path(self.tmp.name, "sub", "x.SC2Replay").write_bytes(b"")
        s, body, _ = self.get("/api/folders?path=%s" % urllib.request.quote(self.tmp.name))
        got = json.loads(body)
        self.assertEqual([(f["name"], f["replays"]) for f in got["folders"]], [("sub", 1)])


if __name__ == "__main__":
    unittest.main()
