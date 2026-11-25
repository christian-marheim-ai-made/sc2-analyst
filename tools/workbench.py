#!/usr/bin/env python3
"""W3 · The workbench: one local page to test the results, zoom into a game, and (from W9) ask the
local model - `docs/plan/PLAN-workbench.md` §5.

    .venv/Scripts/python.exe tools/workbench.py [--port 8798] [--records <folder>] [--model none|mock]

Then open http://127.0.0.1:8798/. Standard library only. Bound to 127.0.0.1; a request naming any
other host is refused, and every POST must come from the workbench's own page (its Origin), so a
website open in the same browser can never act on it.

What it serves: the page (`workbench/`), the vendored bootstrap kit by its path (never a copy), and
the view models of `src/sc2analyst/workbench_vm.py` as JSON. The page decides nothing.

`--model mock` starts llm-bootstrap's stand-in server beside it (no card, no model, made-up words),
so the kit's widgets have a server to show. No model is ever started without being asked: the
real card's server is W9's button, never this flag.
"""
from __future__ import annotations

import argparse
import collections
import http.server
import json
import os
import string
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from sc2analyst import adapter_sc2, library as L, llm, pick, positions as P, studies_vm as SV, workbench_vm as V  # noqa: E402

PAGE = REPO / "workbench"
KIT = REPO / "vendor/llm-bootstrap/llm-bootstrap-client/llm-bootstrap-kit.js"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/paint.js": ("paint.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}
PY = REPO / ".venv/Scripts/python.exe"
STANDIN_WORKSPACE = L.DATA_HOME / "llm-workspace" / "standin"
OPENINGS = L.DATA_HOME / "derived" / "openings-v1.json"
LLM_WORKSPACE = L.DATA_HOME / "llm-workspace"
GRADES_FILE = L.DATA_HOME / "grades" / "grades.jsonl"
FRONTIER_PACKETS = L.DATA_HOME / "frontier" / "packets"
NEED_MIB = 19623 + 256      # the thinker's need and the floor, the bootstrap's own figures (S10)


def card_free_mib():
    """The card's free memory by nvidia-smi, or None when it cannot be read."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        return int(out.splitlines()[0])
    except Exception:
        return None
STUDIES = L.DATA_HOME / "studies"
AGGREGATES = L.DATA_HOME / "aggregates"
GAMES_KEPT = 32


class Workbench:
    """The server's state: the records folder, the library, the games in memory, a folder scan."""

    def __init__(self, records: Path = L.RECORDS, port: int = 8798, studies: Path = STUDIES,
                 aggregates: Path = AGGREGATES):
        self.records = Path(records)
        self.studies, self.aggregates = Path(studies), Path(aggregates)
        self.origin = "http://127.0.0.1:%d" % port
        self.hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}
        self.origins = {"http://" + h for h in self.hosts}     # the page by either of its two names
        self.llm_server = None
        self.model = "none"
        self.server = self.client = self.workspace = None   # the bootstrap server this workbench started (W9)
        self.runs: dict = {}          # correlation id -> the run in flight or landed this launch
        self._lib = None
        self._games: collections.OrderedDict = collections.OrderedDict()
        self._lock = threading.Lock()
        self.scan = {"running": False, "folder": None, "log": [], "exit": None}

    def library(self, q=None) -> dict:
        with self._lock:
            if self._lib is None:
                rows = pick.index(self.records)
                # each game's openings (W4), measured by tools/measure_openings.py; absent = no filter
                if OPENINGS.exists():
                    cls = json.loads(OPENINGS.read_text(encoding="utf-8"))["classes"]
                    rows = [dict(r, openings=sorted({p["class"] for p in cls.get(r["file"], {}).values()}))
                            for r in rows]
                self._lib = rows
            rows = self._lib
        return V.library_vm(rows, q)

    def _entry(self, key: str) -> dict:
        with self._lock:
            if key in self._games:
                self._games.move_to_end(key)
                return self._games[key]
        files = self.library()["files"]
        if key not in files:
            raise KeyError("no game %s" % key)
        g = adapter_sc2.load(self.records / files[key])
        entry = {"game": g, "positions": None}
        with self._lock:
            self._games[key] = entry
            while len(self._games) > GAMES_KEPT:
                self._games.popitem(last=False)
        return entry

    def game(self, key: str) -> dict:
        return self._entry(key)["game"]

    def positions(self, key: str):
        e = self._entry(key)
        if e["positions"] is None:
            e["positions"] = P.Positions(e["game"])
        return e["positions"]

    # --- the folder picker (R-B): the server lists folders; the browser never sees a path it typed --

    @staticmethod
    def folders(path: str | None) -> dict:
        if not path:
            roots = []
            sc2 = Path.home() / "Documents" / "StarCraft II" / "Accounts"
            if sc2.exists():
                roots.append(str(sc2))
            roots += ["%s:\\" % d for d in string.ascii_uppercase if os.path.exists("%s:\\" % d)]
            return {"path": None, "parent": None,
                    "folders": [{"name": r, "path": r, "replays": None} for r in roots]}
        p = Path(path)
        if not p.is_dir():
            raise KeyError("not a folder: %s" % path)
        out = []
        try:
            kids = sorted((k for k in p.iterdir() if k.is_dir()), key=lambda k: k.name.lower())
        except PermissionError:
            kids = []
        for k in kids[:300]:
            try:
                n = sum(1 for _ in k.glob("*.SC2Replay"))
            except OSError:
                n = None
            out.append({"name": k.name, "path": str(k), "replays": n})
        here = sum(1 for _ in p.glob("*.SC2Replay"))
        return {"path": str(p), "parent": str(p.parent) if p.parent != p else None,
                "replays_here": here, "folders": out}

    def start_scan(self, folder: str) -> dict:
        """intake -> map import -> records, the existing tools, one after the other, in the background."""
        if self.scan["running"]:
            return self.scan_status()
        src = Path(folder)
        if not src.is_dir():
            raise KeyError("not a folder: %s" % folder)
        steps = [[str(PY), str(REPO / "tools/intake_library.py"), str(src)],
                 [str(PY), str(REPO / "tools/discover_library.py"), str(src), "--import-maps"],
                 [str(PY), str(REPO / "tools/extract_library.py"), "--out", str(self.records)]]
        self.scan = {"running": True, "folder": str(src), "log": [], "exit": None}

        def run():
            code = 0
            for cmd in steps:
                self.scan["log"].append("$ %s" % Path(cmd[1]).name)
                p = subprocess.Popen(cmd, cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace")
                for line in p.stdout:
                    self.scan["log"].append(line.rstrip())
                    del self.scan["log"][:-200]
                code = p.wait()
                if code:
                    break
            with self._lock:
                self._lib = None
            self.scan.update(running=False, exit=code)
        threading.Thread(target=run, daemon=True).start()
        return self.scan_status()

    def _table_dir(self, body: dict) -> Path:
        if body.get("kind") == "study":
            d = self.studies / body["id"] / body["version"]
        else:
            d = self.aggregates / body["repository"] / body["selection"] / body["version"]
        base = self.studies if body.get("kind") == "study" else self.aggregates
        if base.resolve() not in d.resolve().parents or not (d / "table.json").exists():
            raise KeyError("no such table")
        return d

    def mark_reviewed(self, body: dict) -> dict:
        return SV.mark_reviewed(self._table_dir(body))

    def run_aggregate(self, body: dict) -> dict:
        from sc2analyst import aggregate as AG
        repo = body.get("repository")
        if repo not in AG.load_repositories():
            raise KeyError("no repository %s" % repo)
        last = int(body.get("last") or 30)
        f = {k: body.get(k) or None for k in ("map_name", "fmt", "race", "vs", "opening")}
        openings = json.loads(OPENINGS.read_text(encoding="utf-8"))["classes"] if OPENINGS.exists() else {}
        res = AG.run(repo, AG.select(self.library_rows(), openings=openings, **f), last=last, records=self.records)
        sel = AG.slug(last=last, map=f["map_name"], fmt=f["fmt"], race=f["race"], vs=f["vs"], opening=f["opening"])
        d = AG.write(res, sel) if self.aggregates == AGGREGATES else self._write_to(res, sel)
        return {"repository": repo, "selection": sel, "version": res["version"], "games": res["games"], "dir": str(d)}

    def _write_to(self, res: dict, sel: str) -> Path:
        d = self.aggregates / res["repository"] / sel / res["version"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "table.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
        return d

    def library_rows(self) -> list:
        self.library()
        return self._lib

    # --- the model (W9): started only by the person's button, stopped by theirs or by the exit -------

    def start_model(self, backend: str) -> dict:
        if self.server is not None:
            return self.model_status()
        if backend not in ("mock", "llamacpp"):
            raise KeyError("no backend %s" % backend)
        if backend == "llamacpp":
            free = card_free_mib()
            if free is not None and free < NEED_MIB:
                return dict(self.model_status(), started=False,
                            why="the card has %d MiB free; the thinker needs %d" % (free, NEED_MIB))
        ws = LLM_WORKSPACE if backend == "llamacpp" else STANDIN_WORKSPACE
        ws.mkdir(parents=True, exist_ok=True)
        port = llm._free_port()
        args = ["--backend", backend, "--runner", str(llm.resolve(llm.REAL) if backend == "llamacpp" else llm.MOCK),
                "--port", str(port), "--workspace", str(ws)]
        for o in sorted(self.origins):
            args += ["--allow-origin", o]
        if backend == "llamacpp":
            args += ["--name", "sc2-analyst"]
        self.server = llm._rc().start_server(args, start_s=60.0)
        self.client = self.server.client()
        self.workspace = ws
        self.llm_server, self.model = "http://127.0.0.1:%d" % port, backend
        self.land_pending()
        return self.model_status()

    def stop_model(self) -> dict:
        if self.server is not None:
            self.server.stop()
        self.server = self.client = None
        self.llm_server, self.model = None, "none"
        return self.model_status()

    def model_status(self) -> dict:
        return {"model": self.model, "llm_server": self.llm_server, "need_mib": NEED_MIB,
                "card_free_mib": card_free_mib() if self.model == "llamacpp" else None}

    def start_run(self, body: dict) -> dict:
        from sc2analyst import runs as RN, tasks as TK
        if self.server is None:
            raise KeyError("no model server: start it first")
        task = TK.TASKS.get(body.get("task"))
        if task is None:
            raise KeyError("no task %s" % body.get("task"))
        key = body.get("game")
        given = dict(body.get("target") or {})
        target = dict(given)
        if key:
            target["game"] = self.game(key)
        target["label"] = "%s %s" % (key or "", " ".join("%s=%s" % kv for kv in sorted(given.items())))
        if task.id == "T-study":
            target["studies"] = str(self.studies)
        run = RN.submit(self.client, task, target, debug=bool(body.get("debug")), scope=key,
                        batch_at=body.get("batch_at"))
        run["game_key"] = key
        self._pend(run)
        self._lander(run)
        return {"correlation_id": run["correlation_id"], "status": "queued", "debug": run["debug"]}

    def _pend(self, run: dict):
        """A submitted run is kept on disk until it lands, so a restart lands it too (the jobs are the
        bootstrap folder's and outlive its server)."""
        d = self.workspace / "sc2" / "pending"
        d.mkdir(parents=True, exist_ok=True)
        (d / ("%s.json" % run["correlation_id"])).write_text(json.dumps(run), encoding="utf-8")

    def _lander(self, run: dict):
        cid, key = run["correlation_id"], run.get("game_key")
        self.runs[cid] = dict(run, status="queued", workspace=str(self.workspace))
        records = self.workspace / "sc2"
        from sc2analyst import runs as RN

        def land():
            # a held job answers every wait at once, held, until the person sends it in Check: keep
            # asking (measured W9: one more wait right away saw `held` again, and the run stayed so)
            # a batch planned for tonight outlasts any one wait: a wait that runs out is asked again
            rec = {}
            while self.client is not None:
                client = self.client
                try:
                    rec = RN.land(client, run, records=records)
                except TimeoutError:
                    continue
                except Exception:              # the server stopped under the wait: pending stays
                    return
                self.runs[cid] = dict(rec, game_key=key, workspace=str(self.workspace))
                if rec.get("status") != "held":
                    break
                time.sleep(2.0)
            if rec.get("status") not in ("held", "queued", None):
                (records / "pending" / ("%s.json" % cid)).unlink(missing_ok=True)
        threading.Thread(target=land, daemon=True).start()

    def land_pending(self) -> int:
        """At the model's start: every run an earlier launch left in flight gets its lander again."""
        d = self.workspace / "sc2" / "pending"
        n = 0
        for f in sorted(d.glob("c-*.json")) if d.exists() else []:
            self._lander(json.loads(f.read_text(encoding="utf-8")))
            n += 1
        return n

    # --- the batch (W10): planned for tonight, run when the card has room --------------------------

    def batch_targets(self, body: dict) -> list:
        """[(game key, target)] for a family over a set of games; T-fight takes each game's biggest fights."""
        from sc2analyst import workbench_vm as VV
        keys = list(body.get("keys") or [])
        if body.get("set") == "20":
            run = json.loads((L.DATA_HOME / "assessments" / "run.json").read_text(encoding="utf-8"))
            keys = [VV.game_key(m, s_) for m, s_ in run["games"]]
        per = int(body.get("fights_per_game") or 5)
        out = []
        for k in keys:
            g = self.game(k)
            if body.get("task") == "T-fight":
                fs = sorted(VV.fight_rows(g), key=lambda f: -f["value"])[:per]
                out += [(k, {"fight": f["id"]}) for f in sorted(fs, key=lambda f: f["id"])]
            elif body.get("task") == "T-opening":
                from sc2analyst import openings as O
                cls = O.classify(g, O.load_thresholds())
                out += [(k, {"pid": pid}) for pid, c in cls.items() if c["class"] == "ONE-BASE ATTACK"]
        return out

    def batch_preview(self, body: dict) -> dict:
        from sc2analyst import grading as GR
        targets = self.batch_targets(body)
        rows = [r for r in GR.compare(GR.load_records(self._record_dirs()), {}) if r["task"] == body.get("task")
                and r["seconds_p50"] is not None and r["model"] == "thinker"]
        per = rows[0]["seconds_p50"] if rows else None
        return {"tasks": len(targets), "games": len({k for k, _ in targets}),
                "seconds_each": per, "hours": None if per is None else round(len(targets) * per / 3600, 2),
                "note": None if per else "no measured answer time for this family on the card yet"}

    def batch_plan(self, body: dict) -> dict:
        if self.server is None:
            raise KeyError("no model server: start it first")
        at = float(body.get("not_before") or 0) or time.time()
        n = 0
        for k, target in self.batch_targets(body):
            self.start_run({"task": body["task"], "game": k, "target": target, "batch_at": at})
            n += 1
        return {"planned": n, "not_before": at}

    def _record_dirs(self) -> list:
        return [ws / "sc2" for ws in (LLM_WORKSPACE, STANDIN_WORKSPACE)]

    # --- the frontier (W11): packets the owner pastes into ChatGPT Pro himself ----------------------

    def make_packet(self, body: dict) -> dict:
        from sc2analyst import frontier as FR, tasks as TK
        task = TK.TASKS.get(body.get("task"))
        if task is None:
            raise KeyError("no task %s" % body.get("task"))
        key = body.get("game")
        target = dict(body.get("target") or {}, key=key)
        if key:
            target["game"] = self.game(key)
        if task.id == "T-study":
            target["studies"] = str(self.studies)
        rec = FR.packet(task, target, body.get("label") or key or "", packets=FRONTIER_PACKETS)
        return {k: rec[k] for k in ("packet_id", "task", "version", "label", "text", "game_key")}

    def packets(self) -> list:
        out = []
        for f in sorted(FRONTIER_PACKETS.glob("c-*.json")) if FRONTIER_PACKETS.exists() else []:
            r = json.loads(f.read_text(encoding="utf-8"))
            out.append({k: r.get(k) for k in ("packet_id", "task", "version", "label", "text", "game_key")})
        return out

    def import_reply(self, body: dict) -> dict:
        from sc2analyst import frontier as FR
        rec = FR.import_reply(body["packet_id"], body.get("reply", ""), packets=FRONTIER_PACKETS,
                              records=LLM_WORKSPACE / "sc2")
        v = rec["verdicts"]
        return {"correlation_id": rec["correlation_id"], "kept": len(v["kept"]), "dropped": len(v["dropped"]),
                "answer_dropped": v["answer_dropped"]}

    # --- grading (W10): the owner's clicks only ----------------------------------------------------

    def to_grade(self, seed: int) -> list:
        from sc2analyst import grading as GR
        return GR.blind(GR.claims(GR.load_records(self._record_dirs())), GR.load_grades(GRADES_FILE), seed)

    def give_grade(self, body: dict) -> dict:
        from sc2analyst import grading as GR
        return GR.grade(body["hash"], body["cid"], body["grade"], body.get("note", ""), path=GRADES_FILE)

    def compare_table(self) -> list:
        from sc2analyst import grading as GR
        return GR.compare(GR.load_records(self._record_dirs()), GR.load_grades(GRADES_FILE))

    def runs_listing(self) -> list:
        out = {}
        for ws in (LLM_WORKSPACE, STANDIN_WORKSPACE):
            idx = ws / "sc2" / "index.jsonl"
            if idx.exists():
                for line in idx.read_text(encoding="utf-8").splitlines():
                    r = json.loads(line)
                    out[r["correlation_id"]] = dict(r, workspace=ws.name)
        for cid, r in self.runs.items():
            row = out.setdefault(cid, {"correlation_id": cid, "task": r["task"], "target": r.get("target"),
                                       "submitted": r.get("submitted")})
            row["status"] = r.get("status")
        return sorted(out.values(), key=lambda r: r.get("submitted") or 0, reverse=True)

    def run_record(self, cid: str) -> dict:
        rec = None
        if cid in self.runs and self.runs[cid].get("status") in ("queued", "held"):
            rec = dict(self.runs[cid])
        else:
            for ws in (LLM_WORKSPACE, STANDIN_WORKSPACE):
                f = ws / "sc2" / ("%s.json" % cid)
                if f.exists():
                    rec = json.loads(f.read_text(encoding="utf-8"))
                    rec["workspace"] = str(ws)
                    break
        if rec is None:
            raise KeyError("no run %s" % cid)
        req = Path(rec.get("workspace") or ".") / "requests" / cid
        rec["bootstrap_record"] = (sorted(str(x.relative_to(req)) for x in req.rglob("*") if x.is_file())
                                   if req.exists() else [])
        return rec

    def scan_status(self) -> dict:
        return dict(self.scan, log=self.scan["log"][-40:])


def handler_for(wb: Workbench):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):          # quiet: the page asks often
            pass

        def _send(self, status: int, body: bytes, kind: str):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status: int = 200):
            self._send(status, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            if self.headers.get("Host") not in wb.hosts:
                self._json({"error": "this host is not the workbench's", "reason": "host_not_allowed"}, 403)
                return False
            return True

        def do_GET(self):
            if not self._host_ok():
                return
            u = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(u.query).items()}
            debug = q.get("debug") == "1"
            parts = [unquote(x) for x in u.path.strip("/").split("/")]
            try:
                if u.path in STATIC:
                    name, kind = STATIC[u.path]
                    return self._send(200, (PAGE / name).read_bytes(), kind)
                if u.path == "/llm-bootstrap-kit.js":
                    return self._send(200, KIT.read_bytes(), "text/javascript; charset=utf-8")
                if u.path == "/api/config":
                    return self._json({"llm_server": wb.llm_server, "model": wb.model})
                if u.path == "/api/library":
                    lib = wb.library(q)
                    lib.pop("files")
                    return self._json(lib)
                if u.path == "/api/folders":
                    return self._json(wb.folders(q.get("path")))
                if u.path == "/api/library/scan":
                    return self._json(wb.scan_status())
                if u.path == "/api/model":
                    return self._json(wb.model_status())
                if u.path == "/api/frontier/packets":
                    return self._json(wb.packets())
                if u.path == "/api/grade/next":
                    return self._json(wb.to_grade(int(q.get("seed", 7))))
                if u.path == "/api/compare":
                    return self._json(wb.compare_table())
                if u.path == "/api/runs":
                    return self._json(wb.runs_listing())
                if len(parts) == 3 and parts[:2] == ["api", "run"]:
                    return self._json(wb.run_record(parts[2]))
                if u.path == "/api/studies":
                    return self._json(SV.listing(wb.studies, wb.aggregates))
                if len(parts) == 4 and parts[:2] == ["api", "study"]:
                    return self._json(SV.study_vm(wb.studies, parts[2], parts[3]))
                if len(parts) == 5 and parts[:2] == ["api", "aggregate"]:
                    return self._json(SV.aggregate_vm(wb.aggregates, parts[2], parts[3], parts[4]))
                if len(parts) >= 3 and parts[:2] == ["api", "game"]:
                    key = parts[2]
                    rest = parts[3:]
                    if rest == ["series"]:
                        return self._json(V.game_vm(wb.game(key), debug, key))
                    if rest == ["at"]:
                        return self._json(V.snapshot_vm(wb.game(key), float(q.get("t", 0)), debug,
                                                        wb.positions(key)))
                    if len(rest) == 2 and rest[0] == "fight":
                        return self._json(V.fight_vm(wb.game(key), int(rest[1]), debug))
                return self._json({"error": "no such route: %s" % u.path, "reason": "not_found"}, 404)
            except KeyError as exc:
                return self._json({"error": str(exc).strip("'\""), "reason": "not_found"}, 404)
            except Exception as exc:  # shown to the person, and the trace kept in the server's console
                traceback.print_exc()
                return self._json({"error": "%s: %s" % (type(exc).__name__, exc), "reason": "failed"}, 500)

        def do_POST(self):
            # read the body before any refusal: a socket closed with unread data is reset on Windows,
            # and the asker sees a broken connection instead of the refusal (measured, 2 runs in 6)
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if not self._host_ok():
                return
            if self.headers.get("Origin") not in wb.origins:
                return self._json({"error": "a POST must come from the workbench's own page",
                                   "reason": "origin_not_allowed"}, 403)
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                return self._json({"error": "the body is not JSON", "reason": "bad_request"}, 400)
            try:
                if self.path == "/api/library/scan":
                    return self._json(wb.start_scan(body.get("folder", "")))
                if self.path == "/api/reviewed":       # the owner's own click on Reviewed, nothing else
                    return self._json(wb.mark_reviewed(body))
                if self.path == "/api/model/start":     # the person's button; never at start-up
                    return self._json(wb.start_model(body.get("backend", "mock")))
                if self.path == "/api/model/stop":
                    return self._json(wb.stop_model())
                if self.path == "/api/frontier/packet":
                    return self._json(wb.make_packet(body))
                if self.path == "/api/frontier/import":  # the owner's paste of ChatGPT Pro's reply
                    return self._json(wb.import_reply(body))
                if self.path == "/api/grade":           # the owner's click, and only that
                    return self._json(wb.give_grade(body))
                if self.path == "/api/batch/preview":
                    return self._json(wb.batch_preview(body))
                if self.path == "/api/batch/plan":      # the owner's button: plan tonight
                    return self._json(wb.batch_plan(body))
                if self.path == "/api/run":
                    return self._json(wb.start_run(body))
                if self.path == "/api/aggregate/run":
                    return self._json(wb.run_aggregate(body))
                return self._json({"error": "no such route: %s" % self.path, "reason": "not_found"}, 404)
            except KeyError as exc:
                return self._json({"error": str(exc).strip("'\""), "reason": "not_found"}, 404)
    return Handler


def serve(wb: Workbench, port: int) -> http.server.ThreadingHTTPServer:
    return http.server.ThreadingHTTPServer(("127.0.0.1", port), handler_for(wb))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8798)
    ap.add_argument("--records", type=Path, default=L.RECORDS)
    ap.add_argument("--model", choices=("none", "mock"), default="none",
                    help="mock: llm-bootstrap's stand-in beside the page (no card); the real card is W9's")
    a = ap.parse_args()
    wb = Workbench(a.records, a.port)
    try:
        if a.model == "mock":
            wb.start_model("mock")
        httpd = serve(wb, a.port)
        print("workbench on %s (records %s, model %s)" % (wb.origin, wb.records, wb.model), flush=True)
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        wb.stop_model()               # in every case: the server this workbench started goes with it
    return 0


if __name__ == "__main__":
    sys.exit(main())
