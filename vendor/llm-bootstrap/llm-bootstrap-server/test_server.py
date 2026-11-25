"""The server's box: every route over real HTTP, and a real process killed.

    python test_server.py

THE ROUTES run against a real server on a free port, with a model the test
steers. EVERY response is validated against the contract's shape for its route
and status, and every (route, status, reason) the contract declares must be
produced at least once by the real server - a declared answer nothing can
provoke is a promise with no test behind it. The contract's `bad_request`
examples are replayed, and the server must answer them word for word.

THE PROCESSES are real: a server process holding a folder, another server
refused it, the process killed, and the folder free once it is gone. These
print what they measured.

A PAGE FROM ANOTHER ADDRESS is asked as its browser asks, headers and all: the
answer the contract declares before any route, provoked; what an allowed
page's browser is told, a preflight's included; and a request with no Origin,
or from the server's own page, answered as before - an OPTIONS compared with
what http.server alone answers.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
# by name: this suite's own `http()` would shadow the package
from http.client import HTTPConnection, HTTPException, HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONTRACT = HERE.parent / "llm-bootstrap-contract"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(CONTRACT))
from _suites import START_S, quiet_ports                        # noqa: E402
import _suites                                                  # noqa: E402
_suites.forbid_the_card()        # a suite never asks the real card (nvidia-smi)
from _suites import killed as kill_and_wait                     # noqa: E402
import ids                                                      # noqa: E402
import jobqueue as Q                                            # noqa: E402
import server as S                                              # noqa: E402
import validate as V                                            # noqa: E402
from test_queue import every_parameter, same                    # noqa: E402

SHAPES, ALL_ROUTES = V.load(CONTRACT)
ROUTES = {r["id"]: r for key, r in ALL_ROUTES.items() if key.startswith("server/")}
#: what the server answers before any route, for the page that sent a request
BEFORE = V.load_origins(CONTRACT)["server"]
FAST = dict(tick_s=0.02, supervise_s=0.02, cancel_wait_s=1.0, give_up_grace_s=0.3,
            stall_s=60.0, load_s=120.0, stale_head_s=600.0, recover_timeout_s=0.3)
MODEL = "demo-7b"
results: list = []
measured: list = []

#: EVERY PORT THIS SUITE HAD A SERVER LISTEN ON - (host, the port asked for,
#: the port had) - read by its last section: a suite takes its ports from a
#: range of its own, never one the operating system picks (H5's register 5:
#: rows served on port 0). The server's own `listen`, recorded where it is had
_listen = S.listen


def _listening(port, host="127.0.0.1"):
    got = _listen(port, host)
    LISTENED.append((host, port, got.server_address[1]))
    return got


LISTENED: list = []
#: ...and every port a plain server of this suite's listened on
BARE: list = []
S.listen = _listening


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name, detail))


def http(url: str, method: str, path: str, body=None, headers=None):
    """-> (status, body). A request that gets no answer at all is status 0 -
    a failed check, never an exception that ends the suite before its tally."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read().decode("utf-8"))
    except (OSError, ValueError) as e:
        return 0, {"error": f"no answer: {type(e).__name__}: {e}", "reason": "no_answer"}


def route_of(method: str, path: str):
    """The contract's route for a request - found with the CONTRACT's own
    path rule, never the server's, which is the thing under test."""
    hits = [r for r in ROUTES.values()
            if r["method"] == method and V.path_matches(r["path"], path)]
    return hits[0] if len(hits) == 1 else None


def declared() -> set:
    """Every answer the contract says a server route can give."""
    out = set()
    for r in ROUTES.values():
        for status, shape in r["responses"].items():
            reasons = V.fields_of(shape, SHAPES).get("reason", {}).get("enum", [None])
            for reason in (reasons if int(status) >= 400 else [None]):
                out.add((r["id"], int(status), reason))
    return out


class Probe:
    """Every request through here is checked against the contract, and what
    it provoked is remembered for the coverage check."""

    def __init__(self, url: str):
        self.url = url
        self.seen: set = set()

    def __call__(self, method: str, path: str, body=None, headers=None):
        status, out = http(self.url, method, path, body, headers)
        route = route_of(method, path)
        if route is None:
            check(f"contract: {method} {path} is a declared route", False)
            return status, out
        shape = route["responses"].get(str(status))
        bad = [f"{route['id']} does not declare {status}"] if shape is None \
            else V.check(out, shape, SHAPES)
        check(f"contract: {method} {path} -> {status}", not bad, "; ".join(bad))
        reason = out.get("reason") if status >= 400 and isinstance(out, dict) else None
        self.seen.add((route["id"], status, reason))
        return status, out


class Gates:
    """A model the test steers. It answers `text`; with `gate` it waits for the
    test to open that gate, watching the cancel unless `stubborn`; with `fail`
    it fails."""

    def __init__(self):
        self.gates, self.calls, self.cancelled = {}, [], []
        #: what each job's generation was handed, by job id
        self.params: dict = {}
        self.lock = threading.Lock()

    def gate(self, name) -> threading.Event:
        with self.lock:
            return self.gates.setdefault(name, threading.Event())

    def open_all(self) -> None:
        with self.lock:
            gates = list(self.gates.values())
        for g in gates:
            g.set()

    def __call__(self, request, params, run):
        r = request if isinstance(request, dict) else {}
        self.calls.append(run.job_id)
        self.params[run.job_id] = json.loads(json.dumps(params))
        if "gate" in r:
            ev = self.gate(r["gate"])
            run.report(tokens=1)
            while not ev.wait(0.01):
                if run.stopped() and not r.get("stubborn"):
                    raise Q.Cancelled("the backend saw the cancel")
        if "fail" in r:
            raise Q.GenerationFailed(r["fail"], reason=r.get("reason"))
        return {"text": r.get("text", "Rain on the tin roof."), "tokens": 5,
                "finish_reason": "stop"}

    def cancel(self, handle) -> None:
        self.cancelled.append(handle)


def wait_for(pred, timeout: float = 6.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        got = pred()
        if got:
            return time.time() - t0
        time.sleep(0.02)
    return None


def status_of(url: str, jid: str):
    s, out = http(url, "GET", f"/api/jobs/{jid}/result")
    return out["job"]["status"] if s == 200 else None


def reach(url: str, jid: str, *want, timeout: float = 6.0) -> bool:
    return wait_for(lambda: status_of(url, jid) in want, timeout) is not None


def job(p: Probe, **fields) -> str:
    body = {"kind": "generate", "gen": {"model": MODEL}, **fields}
    s, out = p("POST", "/api/jobs", body)
    if s != 200:
        raise AssertionError(f"submit refused: {s} {out}")
    return out["job_id"]


def timed(p: Probe, path: str):
    """-> (status, body, seconds) for one GET through the probe."""
    t0 = time.monotonic()
    s, out = p("GET", path)
    return s, out, time.monotonic() - t0


def ask_aside(p: Probe, path: str):
    """A GET on a thread of its own. -> (thread, box): once it is answered,
    the box holds the answer and when it came (`at`, monotonic)."""
    box: dict = {}

    def ask():
        box["answer"] = p("GET", path)
        box["at"] = time.monotonic()
    t = threading.Thread(target=ask, daemon=True)
    t.start()
    return t, box


# ----------------------------------------------------------------- the routes

def routes(tmp: Path) -> Probe:
    gates, lines = Gates(), []
    httpd = S.serve(free_port(), backend=gates, log=lines.append, **FAST)
    app = httpd.app
    url, p = app.url, Probe(app.url)
    others = []
    try:
        # --- nothing bound: every job question has one answer
        s, h = p("GET", "/api/health")
        check("health answers with nothing bound", s == 200 and h["loaded"] is False, str(h))
        check("...and names this server's own actor", h.get("actor") == app.actor
              and ids.matches("actor", app.actor) and app.actor.startswith("srv-"), str(h))
        check("...and the version of the contract it serves",
              h.get("contract_version") == V.contract_version(CONTRACT),
              f"{h.get('contract_version')}, the contract's {V.contract_version(CONTRACT)}")
        s, w = p("GET", "/api/workspace")
        check("the workspace says nothing is bound", (s, w) == (200, {"loaded": False,
                                                                    "workspace": None}))
        for method, path, body in (("GET", "/api/jobs", None),
                                   ("POST", "/api/jobs", {"kind": "generate",
                                                          "gen": {"model": MODEL}}),
                                   ("GET", "/api/jobs/j-0001/result", None),
                                   ("GET", "/api/jobs/j-0001/result?wait_s=30", None),
                                   ("POST", "/api/jobs/j-0001/claim", None),
                                   ("POST", "/api/jobs/j-0001/cancel", None),
                                   ("POST", "/api/jobs/j-0001/retry", None),
                                   ("POST", "/api/jobs/j-0001/give-up", None),
                                   ("POST", "/api/jobs/j-0001/forget", None),
                                   ("GET", "/api/jobs/j-0001/held", None),
                                   ("POST", "/api/jobs/j-0001/send", {}),
                                   ("POST", "/api/jobs/prune", {})):
            s, out = p(method, path, body)
            check(f"{method} {path} with nothing bound is no_workspace",
                  (s, out.get("reason")) == (409, "no_workspace"), f"{s} {out}")
        s, out = p("GET", "/api/jobs/j-0001/result?wait_s=abc")
        check("a wait that is malformed is bad_wait with nothing bound too: the question "
              "is refused before the queue is asked", (s, out.get("reason")) == (400, "bad_wait"),
              f"{s} {out}")
        for path, reason in (("/api/jobs?client=tab-2", "bad_client"),
                             ("/api/jobs?scope=a&scope=b", "bad_scope")):
            s, out = p("GET", path)
            check(f"{path} is {reason} with nothing bound too: the list is refused before the "
                  f"queue is asked", (s, out.get("reason")) == (400, reason), f"{s} {out}")
        s, out = p("POST", "/api/workspace/unload")
        check("unloading nothing answers `was: null`",
              (s, out) == (200, {"loaded": False, "was": None}), str(out))

        # --- binding: every refusal leaves the server where it was
        book = tmp / "book"
        book.mkdir()
        afile = tmp / "a-file.txt"
        afile.write_text("not a folder", encoding="utf-8")
        for body, reason in (({}, "no_path"), ({"path": "   "}, "no_path"),
                             ({"path": 7}, "bad_request"),
                             ({"path": str(tmp / "nowhere")}, "unusable_folder"),
                             ({"path": str(afile)}, "unusable_folder")):
            s, out = p("POST", "/api/workspace", body)
            check(f"binding {body} is refused {reason}", (s, out.get("reason")) == (400, reason),
                  f"{s} {out}")
        s, out = p("POST", "/api/workspace", {"path": str(book)})
        check("a real folder binds", s == 200 and out["workspace"] == str(book.resolve()),
              str(out))
        s, again = p("POST", "/api/workspace", {"path": str(book)})
        check("binding the folder it is already bound to changes nothing",
              s == 200 and again == out, str(again))

        # --- submitting: a missing field is the route's own refusal
        for body, reason in (({}, "no_kind"),
                             ({"kind": "", "gen": {"model": MODEL}}, "no_kind"),
                             ({"kind": "generate"}, "no_model"),
                             ({"kind": "generate", "gen": {}}, "no_model"),
                             ({"kind": "generate", "gen": {"model": MODEL, "n": "three"}},
                              "bad_request"),
                             ({"kind": "generate", "gen": {"model": MODEL},
                               "retry_of": "j-0001"}, "bad_request"),
                             ({"kind": "generate", "gen": {"model": MODEL},
                               "after": ["j-9999"]}, "unknown_after"),
                             ({"kind": "generate", "gen": {"model": MODEL},
                               "group_size": 2}, "bad_group_size"),
                             ({"kind": "generate", "gen": {"model": MODEL},
                               "card_patience_s": -1}, "bad_patience"),
                             ({"kind": "generate", "gen": {"model": MODEL},
                               "card_patience_s": "soon"}, "bad_request"),
                             # S18: live work is now - never a time to come, or a
                             # wait for room; and a time is a number
                             ({"kind": "generate", "gen": {"model": MODEL},
                               "priority": "interactive", "not_before": time.time() + 3600},
                              "live_is_now"),
                             ({"kind": "generate", "gen": {"model": MODEL},
                               "priority": "interactive", "if_no_room": "wait"}, "live_is_now"),
                             # S18b: a job that names no priority is live too
                             ({"kind": "generate", "gen": {"model": MODEL},
                               "not_before": time.time() + 3600}, "live_is_now"),
                             ({"kind": "generate", "gen": {"model": MODEL},
                               "if_no_room": "wait"}, "live_is_now"),
                             ({"kind": "generate", "gen": {"model": MODEL},
                               "if_no_room": "later"}, "bad_request"),
                             # S10b RG3: a length llama-server refuses, refused here first
                             ({"kind": "generate", "gen": {"model": MODEL, "max_tokens": -2}},
                              "bad_max_tokens"),
                             ({"kind": "generate", "gen": {"model": MODEL,
                                                           "max_tokens": 2 ** 31}},
                              "bad_max_tokens")):
            s, out = p("POST", "/api/jobs", body)
            check(f"submitting {json.dumps(body)} is refused {reason}",
                  (s, out.get("reason")) == (400, reason), f"{s} {out}")
        # S18b: with a priority said and with none - each, word for word
        lives = [ex for ex in ROUTES["server.jobs.submit"]["examples"]
                 if ex["status"] == 400 and ex["response"]["reason"] == "live_is_now"
                 and "if_no_room" in ex["request"]]
        check("the contract shows live work asked to wait for room, with a priority and "
              "with none", sorted("priority" in ex["request"] for ex in lives) == [False, True],
              [ex["name"] for ex in lives])
        for live in lives:
            s, out = p("POST", "/api/jobs", live["request"])
            check(f"the contract's example '{live['name']}' is answered word for word",
                  (s, out) == (400, live["response"]), f"{s} {out}")
        lengths = [ex for ex in ROUTES["server.jobs.submit"]["examples"]
                   if ex["status"] == 400 and ex["response"]["reason"] == "bad_max_tokens"]
        check("the contract shows a length llama-server refuses, refused at the submit",
              bool(lengths))
        for ex in lengths:
            s, out = p("POST", "/api/jobs", ex["request"])
            check(f"the contract's example '{ex['name']}' is answered word for word",
                  (s, out) == (400, ex["response"]), f"{s} {out}")
        # H5's register 4: llama-server's other hard limits, refused here first
        ranges = [ex for ex in ROUTES["server.jobs.submit"]["examples"]
                  if ex["status"] == 400 and ex["response"]["reason"] == "out_of_range"]
        check("the contract shows a value another of llama-server's hard limits refuses, "
              "refused at the submit", bool(ranges))
        for ex in ranges:
            s, out = p("POST", "/api/jobs", ex["request"])
            check(f"the contract's example '{ex['name']}' is answered word for word",
                  (s, out) == (400, ex["response"]), f"{s} {out}")
        for body in ({"kind": "generate", "gen": {"model": MODEL, "min_keep": -1}},
                     {"kind": "generate", "gen": {"model": MODEL, "adaptive_decay": 1.0}},
                     {"kind": "generate", "gen": {"model": MODEL,
                                                  "t_max_predict_ms": 2 ** 63}}):
            s, out = p("POST", "/api/jobs", body)
            check(f"submitting {json.dumps(body)} is refused out_of_range, naming it",
                  (s, out.get("reason")) == (400, "out_of_range")
                  and f"$.gen.{list(body['gen'])[1]}" in out.get("error", ""), f"{s} {out}")
        one = job(p, input={"text": "the only reading"}, group="g", group_size=1)
        s, out = p("POST", "/api/jobs", {"kind": "generate", "gen": {"model": MODEL},
                                         "group": "g"})
        check("a member beyond a fan's size is refused group_full",
              (s, out.get("reason")) == (409, "group_full"), f"{s} {out}")
        reach(url, one, "done")

        # --- a running job, and one queued behind it
        hold = job(p, input={"gate": "hold", "text": "held"}, name="the held one")
        check("the steered model has the job", wait_for(lambda: hold in gates.calls) is not None)
        waiting = job(p, input={"text": "waiting"}, scope="book-1")
        s, out = p("GET", "/api/jobs")
        check("the list holds every job in the folder",
              s == 200 and [j["id"] for j in out["jobs"]] == [one, hold, waiting], str(out))
        s, out = p("GET", "/api/jobs?scope=book-1")
        check("?scope narrows the list to one key's jobs",
              s == 200 and [j["id"] for j in out["jobs"]] == [waiting], str(out))
        s, out = p("GET", f"/api/jobs/{waiting}/result")
        check("a queued job's result is its record and nothing yet",
              s == 200 and out["candidates"] == [] and out["job"]["status"] == "queued")
        s, out = p("POST", f"/api/jobs/{waiting}/claim")
        check("claiming a queued job is not_done",
              (s, out.get("reason"), out.get("status")) == (409, "not_done", "queued"), str(out))

        # --- a result that waits for its job to end: ?wait_s=N, from 0 to 60
        s, out, took = timed(p, f"/api/jobs/{hold}/result?wait_s=0.4")
        check("a wait that runs out answers as without it: the job as it stands",
              s == 200 and out["job"]["status"] == "running" and out["candidates"] == []
              and 0.35 <= took < 5.0, f"{s} after {took:.2f}s: {str(out)[:160]}")
        for path, want in ((f"/api/jobs/{one}/result?wait_s=60", 200),
                           ("/api/jobs/j-9999/result?wait_s=60", 404)):
            s, out, took = timed(p, path)
            check(f"a job that has ended, or is not there, is answered at once: {path}",
                  s == want and took < 3.0, f"{s} after {took:.2f}s: {str(out)[:160]}")
        for said in ("0", "0.25", "60", "060.0"):
            s, out, took = timed(p, f"/api/jobs/{one}/result?wait_s={said}")
            check(f"wait_s={said} is a wait, from 0 to 60", s == 200 and took < 3.0,
                  f"{s} after {took:.2f}s: {str(out)[:160]}")
        for said, shown in (("61", "61"), ("60.5", "60.5"), ("-1", "-1"), ("abc", "abc"),
                            ("1e1", "1e1"), ("nan", "nan"), ("inf", "inf"), ("%205", " 5"),
                            ("5.", "5."), (".5", ".5"),
                            ("%D9%A3", "٣")):     # an Arabic-Indic 3: float() takes it
            s, out = p("GET", f"/api/jobs/{one}/result?wait_s={said}")
            check(f"wait_s={said} is refused bad_wait, naming what it was",
                  (s, out.get("reason")) == (400, "bad_wait")
                  and json.dumps(shown, ensure_ascii=False) in out.get("error", ""),
                  f"{s} {out}")
        s, out = p("POST", f"/api/jobs/{hold}/forget")
        check("forgetting a running job is refused unfinished",
              (s, out.get("reason")) == (409, "unfinished"), str(out))
        other = tmp / "other"
        other.mkdir()
        s, out = p("POST", "/api/workspace", {"path": str(other)})
        check("binding elsewhere while a job runs is busy, naming the job",
              (s, out.get("reason"), out.get("job")) == (409, "busy", hold), str(out))
        check("...and the server stays where it was",
              http(url, "GET", "/api/workspace")[1]["workspace"] == str(book.resolve()))
        s, out = p("POST", "/api/workspace/unload")
        check("unloading while a job runs is busy",
              (s, out.get("reason")) == (409, "busy"), str(out))
        s, out = p("GET", "/api/health")
        check("health says the card is held while a job runs",
              s == 200 and out["card"]["holding"] is True and out["queue"]["running"] == 1,
              str(out.get("card")))

        s, out = p("POST", f"/api/jobs/{waiting}/give-up")
        check("giving up on a queued job ends it at once",
              s == 200 and out["was"] == "queued" and out["job"]["status"] == "cancelled",
              str(out))
        waiter, box = ask_aside(p, f"/api/jobs/{hold}/result?wait_s=30")
        time.sleep(0.5)                          # its ask is in, and waiting
        cancelled_at = time.monotonic()
        s, out = p("POST", f"/api/jobs/{hold}/cancel")
        check("cancelling the running job answers was: running",
              s == 200 and out["was"] == "running", str(out))
        check("...and it ends cancelled", reach(url, hold, "cancelled"))
        waiter.join(12)
        s, out = box.get("answer", (0, {}))
        took = box.get("at", float("inf")) - cancelled_at
        check("a result waiting for its job answers the moment it ends - by a cancel here",
              s == 200 and out["job"]["status"] == "cancelled" and took < 3.0,
              f"{s} {took:.2f}s after the cancel: {str(out)[:160]}")
        s, out = p("POST", f"/api/jobs/{hold}/cancel")
        check("cancelling it again is refused finished",
              (s, out.get("reason"), out.get("status")) == (409, "finished", "cancelled"),
              str(out))
        s, out = p("POST", f"/api/jobs/{hold}/give-up")
        check("giving up on a finished job is refused finished",
              (s, out.get("reason")) == (409, "finished"), str(out))
        s, out = p("POST", f"/api/jobs/{hold}/claim")
        check("claiming a cancelled job says it ended - waiting will not help",
              (s, out.get("reason"), out.get("status")) == (409, "ended", "cancelled"),
              str(out))

        # --- failure, retry, and what a failure stopped
        bad = job(p, input={"fail": "the model went away"})
        reach(url, bad, "failed")
        dep = job(p, input={"text": "after the bad one"}, after=[bad])
        check("a job whose `after` already failed is accepted and stopped at once",
              status_of(url, dep) == "cancelled")
        s, out = p("POST", f"/api/jobs/{bad}/retry")
        check("retrying a failed job makes a new one and revives what it stopped",
              s == 200 and out["job"]["retry_of"] == bad and len(out["revived"]) == 1,
              str(out))
        s, out = p("POST", f"/api/jobs/{bad}/retry")
        check("retrying it again is already_retried",
              (s, out.get("reason")) == (409, "already_retried"), str(out))
        live = job(p, input={"fail": "the card is busy, and this request would not wait for "
                                     "it (card_busy)", "reason": "card_busy"},
                   priority="interactive", card_patience_s=0)
        check("a job may say how long it waits for the card: 0 is now or not at all",
              reach(url, live, "failed"))
        s, out = p("GET", f"/api/jobs/{live}/result")
        check("a job that ended for a reason of its own says which, in the contract's words",
              s == 200 and (out["job"]["status"], out["job"].get("reason"))
              == ("failed", "card_busy") and "card_busy" in out["job"]["error"], str(out))
        s, out = p("GET", f"/api/jobs/{bad}/result")
        check("...and one that failed for no reason of its own has none",
              s == 200 and "reason" not in out["job"], str(out))

        # --- who asked, and what names the request
        tab, other_tab = "cli-w5r8t2h6", "cli-b2x7q4n8"
        corr = "c-20260923-140012-a3k9m2p7"
        s, out = p("POST", "/api/jobs", {"kind": "generate", "gen": {"model": MODEL},
                                         "input": {"text": "a good result"},
                                         "client": tab, "correlation_id": corr})
        ok = out.get("job_id")
        check("a client's own correlation id is kept, and the answer says the client minted it",
              s == 200 and (out["correlation_id"], out["correlation_source"]) == (corr, "client"),
              str(out))
        s, out = p("POST", "/api/jobs", {"kind": "generate", "gen": {"model": MODEL},
                                         "client": other_tab, "correlation_id": corr})
        check("the same correlation id again is correlation_taken, naming the job that has it",
              (s, out.get("reason")) == (409, "correlation_taken") and ok in out.get("error", ""),
              str(out))
        s, out = p("POST", "/api/jobs", {"kind": "generate", "gen": {"model": MODEL},
                                         "input": {"text": "the other tab's"},
                                         "client": other_tab})
        theirs = out.get("job_id")
        check("a request that came without an id is given one, and says the server minted it",
              s == 200 and out["correlation_source"] == "server"
              and ids.matches("correlation", out["correlation_id"]), str(out))
        s, out = p("GET", f"/api/jobs?client={tab}")
        check("?client narrows the list to one client's work",
              s == 200 and [j["id"] for j in out["jobs"]] == [ok], str(out)[:300])
        # ?client is an actor id, and each of the list's questions is asked once:
        # a malformed client once listed nothing, and a repeated one the first
        s, out = p("GET", "/api/jobs?client=tab-2")
        check("a client that is not an actor id is bad_client, naming what it was - never an "
              "empty list", (s, out.get("reason")) == (400, "bad_client")
              and '"tab-2"' in out.get("error", ""), f"{s} {out}")
        s, out = p("GET", "/api/jobs?client=srv-k3x9m2p7")
        check("...any actor's id is one: a server's narrows the list to nothing it asked for",
              (s, out) == (200, {"jobs": []}), f"{s} {str(out)[:200]}")
        for twice in (f"client={tab}&client={other_tab}", f"client={tab}&client={tab}"):
            s, out = p("GET", f"/api/jobs?{twice}")
            check(f"a client given twice is bad_client, never the first of the two: ?{twice}",
                  (s, out.get("reason")) == (400, "bad_client")
                  and "given once, not 2 times" in out.get("error", ""), f"{s} {out}")
        s, out = p("GET", "/api/jobs?scope=book-1&scope=book-1")
        check("a scope given twice is bad_scope", (s, out.get("reason")) == (400, "bad_scope")
              and "given once, not 2 times" in out.get("error", ""), f"{s} {out}")
        s, out = p("GET", "/api/jobs?client=")
        check("a client left blank counts as not given: the whole list",
              s == 200 and ok in [j["id"] for j in out["jobs"]]
              and theirs in [j["id"] for j in out["jobs"]], f"{s} {str(out)[:200]}")

        # --- success, claim, and a claim that came second
        reach(url, ok, "done")
        reach(url, theirs, "done")
        s, out = p("GET", f"/api/jobs/{ok}/result")
        check("a done job's result carries what the model said",
              s == 200 and out["candidates"][0]["text"] == "a good result", str(out))
        check("...and its handle names this server's actor",
              s == 200 and ids.parse_handle(out["candidates"][0]["handle"])
              == {"server": app.actor, "job": ok, "sample": 1}, str(out["candidates"][0]))
        s, out = p("POST", f"/api/jobs/{ok}/claim", {"client": tab})
        check("the first claim takes it, and says who took it",
              s == 200 and out["job"]["status"] == "claimed" and out["job"]["claimed_by"] == tab,
              str(out))
        s, out = p("POST", f"/api/jobs/{ok}/claim")
        check("the second is claimed_elsewhere",
              (s, out.get("reason")) == (409, "claimed_elsewhere"), str(out))
        s, out = p("POST", f"/api/jobs/{ok}/retry")
        check("retrying a job that did not fail is not_retryable",
              (s, out.get("reason")) == (409, "not_retryable"), str(out))

        # --- a finished job something still waits on
        first = job(p, input={"text": "part one"})
        reach(url, first, "done")
        later = job(p, input={"gate": "part2", "text": "part two"}, after=[first])
        wait_for(lambda: later in gates.calls)
        s, out = p("POST", f"/api/jobs/{first}/forget")
        check("forgetting a result a running job was built from is refused needed",
              (s, out.get("reason")) == (409, "needed"), str(out))
        waiter, box = ask_aside(p, f"/api/jobs/{later}/result?wait_s=30")
        time.sleep(0.5)                          # its ask is in, and waiting
        opened_at = time.monotonic()
        gates.gate("part2").set()
        waiter.join(12)
        s, out = box.get("answer", (0, {}))
        took = box.get("at", float("inf")) - opened_at
        check("...and the moment it ends done, with what it produced",
              s == 200 and out["job"]["status"] == "done"
              and [c["text"] for c in out["candidates"]] == ["part two"] and took < 3.0,
              f"{s} {took:.2f}s after the gate opened: {str(out)[:160]}")
        reach(url, later, "done")
        wait_for(lambda: http(url, "POST", f"/api/jobs/{first}/forget")[0] == 200, 3)
        check("...and once nothing needs it, it is forgotten",
              status_of(url, first) is None)
        s, out = p("POST", f"/api/jobs/{later}/forget")
        check("forgetting a finished job nothing needs answers `forgot`",
              (s, out) == (200, {"forgot": later}), str(out))

        # --- a generation that will not let go, given up on
        stuck = job(p, input={"gate": "never", "stubborn": True})
        wait_for(lambda: stuck in gates.calls)
        s, out = p("POST", f"/api/jobs/{stuck}/give-up")
        check("giving up on a running job starts the clock",
              s == 200 and out["was"] == "running" and out["grace_s"] == FAST["give_up_grace_s"],
              str(out))
        check("...and after the grace the server takes it back over the worker's head",
              reach(url, stuck, "failed", timeout=5))

        # --- the jobs that are not there
        for method, action in (("GET", "result"), ("POST", "claim"), ("POST", "cancel"),
                               ("POST", "retry"), ("POST", "give-up"), ("POST", "forget")):
            s, out = p(method, f"/api/jobs/j-9999/{action}")
            check(f"{action} of a job that does not exist is no_such_job",
                  (s, out.get("reason")) == (404, "no_such_job"), str(out))

        # --- pruning
        s, out = p("POST", "/api/jobs/prune", {"statuses": ["queued"]})
        check("pruning an unfinished status is bad_request",
              (s, out.get("reason")) == (400, "bad_request"), str(out))
        s, out = p("POST", "/api/jobs/prune", {"statuses": ["failed"], "scope": "nobody"})
        check("a prune narrowed to a scope with nothing in it removes nothing",
              s == 200 and out["removed"] == 0, str(out))
        s, out = p("POST", "/api/jobs/prune", {"statuses": ["cancelled"]})
        check("a prune takes finished jobs", s == 200 and out["removed"] >= 1, str(out))

        # --- a store that cannot be read is said so on every job route
        store = app.queue.store
        real = store.read_bytes
        store.read_bytes = lambda: (_ for _ in ()).throw(OSError(5, "Input/output error"))
        try:
            for method, path, body in (("GET", "/api/jobs", None),
                                       ("POST", "/api/jobs", {"kind": "generate",
                                                              "gen": {"model": MODEL}}),
                                       ("GET", f"/api/jobs/{ok}/result", None),
                                       ("GET", f"/api/jobs/{ok}/result?wait_s=30", None),
                                       ("POST", f"/api/jobs/{ok}/claim", None),
                                       ("POST", f"/api/jobs/{ok}/cancel", None),
                                       ("POST", f"/api/jobs/{ok}/retry", None),
                                       ("POST", f"/api/jobs/{ok}/give-up", None),
                                       ("POST", f"/api/jobs/{ok}/forget", None),
                                       ("GET", f"/api/jobs/{ok}/held", None),
                                       ("POST", f"/api/jobs/{ok}/send", {}),
                                       ("POST", "/api/jobs/prune", {})):
                s, out = p(method, path, body)
                check(f"{method} {path} on an unreadable store is store_unreadable",
                      (s, out.get("reason")) == (503, "store_unreadable"), f"{s} {out}")
            s, out = p("GET", "/api/health")
            check("health still answers, and carries the fault",
                  s == 200 and out["queue"]["store_fault"]["count"] > 0, str(out.get("queue")))
            s, out = p("POST", "/api/workspace/unload")
            check("a folder whose store cannot be read can still be let go",
                  s == 200 and out["was"] == str(book.resolve()), str(out))
        finally:
            store.read_bytes = real
        s, out = p("POST", "/api/workspace", {"path": str(book)})
        check("...and bound again once it can be read", s == 200, str(out))

        # --- one server per folder
        second = S.serve(free_port(), log=lambda s: None, **FAST)
        others.append(second)
        q2 = Probe(second.app.url)
        mine = tmp / "second-servers-own"
        mine.mkdir()
        q2("POST", "/api/workspace", {"path": str(mine)})
        s, out = q2("POST", "/api/workspace", {"path": str(book)})
        check("a second server is refused the folder: folder_in_use",
              (s, out.get("reason")) == (409, "folder_in_use"), str(out))
        check("...naming the server that has it - its process, its address, its actor",
              out.get("holder") == {"pid": os.getpid(), "url": url, "actor": app.actor,
                                    "since": (out.get("holder") or {}).get("since")},
              str(out.get("holder")))
        check("...and the refused server stays bound where it was",
              http(second.app.url, "GET", "/api/workspace")[1]["workspace"]
              == str(mine.resolve()))
        s, out = p("POST", "/api/workspace/unload")
        check("the first server lets the folder go", s == 200 and out["was"] == str(book.resolve()),
              str(out))
        s, out = q2("POST", "/api/workspace", {"path": str(book)})
        check("...and the second one can bind it now", s == 200, str(out))
        s, out = q2("GET", "/api/jobs")
        check("...and finds the first server's jobs there",
              s == 200 and ok in [j["id"] for j in out["jobs"]], str(out)[:200])
        s, out = q2("POST", "/api/workspace/unload")
        p.seen |= q2.seen                    # what the second server gave counts too

        # --- the contract's own bad_request examples, word for word
        s, out = p("POST", "/api/workspace", {"path": str(book)})
        for r in ROUTES.values():
            for ex in r["examples"]:
                if not V.refuses_the_request(ex):
                    continue
                path = ex.get("path", r["path"])
                s, out = p(r["method"], path, ex["request"])
                check(f"the contract's example '{ex['name']}' is answered word for word",
                      (s, out) == (ex["status"], ex["response"]), f"{s} {out}")

        # --- what no route can declare
        s, out = http(url, "GET", "/nowhere")
        check("a path no route has is 404 no_route", (s, out.get("reason")) == (404, "no_route"))
        s, out = http(url, "GET", "/api/jobs/prune")
        check("a method a route does not take is 405 method_not_allowed",
              (s, out.get("reason")) == (405, "method_not_allowed"))

        check("every answer is in the server's log, with its reason",
              any("POST /api/jobs -> 400 bad_request" in line for line in lines))
        check("nothing the routes were asked made the server fail",
              not any(line.startswith("THE SERVER FAILED") for line in lines),
              next((line for line in lines if line.startswith("THE SERVER FAILED")), ""))
    finally:
        gates.open_all()
        for o in others:
            S.stop(o)
        S.stop(httpd)
    return p


def coverage(probes) -> None:
    seen = set().union(*(p.seen for p in probes))
    want = declared()
    missing = sorted(want - seen, key=str)
    # not vacuous: every route of the contract's contributes answers to `want`
    check(f"every answer the contract declares for the server ({len(want)}), the server gave",
          {r for r, _, _ in want} == set(ROUTES) and not missing,
          f"{len(want)} declared; missing: " + "; ".join(f"{r} {s} {why}" for r, s, why in missing))


def the_table_is_the_contract(tmp: Path) -> None:
    """A route the contract declares that the server does not serve stops it
    from starting - so a route added to the contract cannot go unserved."""
    copy = tmp / "contract-with-one-more-route"
    shutil.copytree(CONTRACT, copy, ignore=shutil.ignore_patterns("__pycache__"))
    extra = dict(ROUTES["server.workspace.get"], id="server.workspace.describe",
                 path="/api/workspace/describe")
    (copy / "routes" / "server" / "workspace-describe.json").write_text(
        json.dumps(extra), encoding="utf-8")
    real = S.CONTRACT
    S.CONTRACT = copy
    try:
        S.Server()
        refused = ""
    except RuntimeError as e:
        refused = str(e)
    finally:
        S.CONTRACT = real
    check("a route the contract declares and the server does not serve stops it starting",
          "server.workspace.describe" in refused, refused or "it started")


def the_version_is_the_contracts(tmp: Path) -> None:
    """Health's `contract_version` is the version of the contract the server
    SERVES - hashed from the folder its route table is read from - never a
    number of its own."""
    copy = tmp / "contract-with-one-word-changed"
    shutil.copytree(CONTRACT, copy, ignore=shutil.ignore_patterns("__pycache__"))
    route = copy / "routes" / "server" / "health.json"
    route.write_bytes(route.read_bytes().replace(b"What the server is bound to",
                                                 b"What this server is bound to", 1))
    real = S.CONTRACT
    S.CONTRACT = copy
    try:
        _, h = S.Server(log=lambda s: None).health()
    finally:
        S.CONTRACT = real
    theirs, ours = V.contract_version(copy), V.contract_version(CONTRACT)
    check("health's contract_version is the version of the contract the server serves",
          h.get("contract_version") == theirs != ours,
          f"{h.get('contract_version')}; the copy's {theirs}, the tree's {ours}")


def a_stop_ends_a_wait(tmp: Path) -> Probe:
    """A server that stops answers a result waiting for its job at once: a
    stop never waits out a wait nobody will be there to read."""
    gates = Gates()
    httpd = S.serve(free_port(), backend=gates, log=lambda s: None, **FAST)
    p = Probe(httpd.app.url)
    folder = tmp / "a-stop-ends-a-wait"
    folder.mkdir()
    stopper = None
    try:
        p("POST", "/api/workspace", {"path": str(folder)})
        jid = job(p, input={"gate": "never"})
        wait_for(lambda: jid in gates.calls)
        waiter, box = ask_aside(p, f"/api/jobs/{jid}/result?wait_s=30")
        time.sleep(1.0)                          # its ask is in, and waiting
        stopped_at = time.monotonic()
        stopper = threading.Thread(target=S.stop, args=(httpd,), daemon=True)
        stopper.start()
        waiter.join(12)
        s, out = box.get("answer", (0, {}))
        took = box.get("at", float("inf")) - stopped_at
        check("a server that stops answers a waiting result at once, never after its wait",
              s in (200, 409) and 0.0 <= took < 3.0,
              f"{s} {took:.2f}s after the stop: {str(out)[:160]}")
    finally:
        gates.open_all()
        if stopper is None:
            S.stop(httpd)
        else:
            stopper.join(15)
    return p


def the_page(tmp: Path) -> None:
    """`--client <folder>` serves the client's built page at `/` - that file
    alone, read at every request - and without it `/` is no route."""
    folder = tmp / "client"
    folder.mkdir()
    (folder / "index.html").write_bytes(b"<!doctype html><p>the first page</p>\n")
    (folder / "secret.txt").write_bytes(b"not for serving\n")
    httpd = S.serve(free_port(), log=lambda s: None, client=folder)
    plain = S.serve(free_port(), log=lambda s: None)
    try:
        def get(srv, path):
            try:
                with urllib.request.urlopen(srv.app.url + path, timeout=10) as r:
                    return r.status, r.headers.get("Content-Type", ""), r.read()
            except urllib.error.HTTPError as e:
                with e:
                    return e.code, e.headers.get("Content-Type", ""), e.read()
        s, kind, raw = get(httpd, "/")
        check("with --client, / answers the client's page, as a page",
              (s, raw) == (200, b"<!doctype html><p>the first page</p>\n")
              and kind.startswith("text/html"), f"{s} {kind} {raw[:60]!r}")
        (folder / "index.html").write_bytes(b"<!doctype html><p>built again</p>\n")
        check("...read at every request: a page built again is the one served",
              get(httpd, "/")[2] == b"<!doctype html><p>built again</p>\n")
        s, _, raw = get(httpd, "/secret.txt")
        check("...and nothing else in its folder is served",
              s == 404 and json.loads(raw)["reason"] == "no_route", f"{s} {raw[:80]!r}")
        s, _, raw = get(plain, "/")
        check("without --client, / is no route", s == 404 and json.loads(raw)["reason"] == "no_route",
              f"{s} {raw[:80]!r}")
        s, _, raw = get(httpd, "/api/health")
        check("...and the routes answer as ever beside the page", s == 200, f"{s}")
    finally:
        S.stop(httpd)
        S.stop(plain)
    try:
        S.parse_args(["--client", str(tmp / "no-page-here")])
        refused = False
    except SystemExit:
        refused = True
    check("a --client folder with no built page is refused at the start", refused)


def the_usage_lines(tmp: Path) -> None:
    """The usage line - server.py's own, and its README's - names every flag
    the command line takes: a flag missing from it is one nobody reads of."""
    said = io.StringIO()
    with contextlib.redirect_stdout(said):
        try:
            S.parse_args(["--help"])
        except SystemExit:
            pass
    flags = sorted(set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", said.getvalue())) - {"--help"})

    def usage(text: str) -> str:
        lines = text.splitlines()
        first = next(i for i, ln in enumerate(lines) if ln.startswith("    python server.py"))
        block = [lines[first]]
        for ln in lines[first + 1:]:
            if not ln.strip() or ln.startswith("    python "):
                break
            block.append(ln)
        return "\n".join(block)

    for where, text in (("server.py", S.__doc__),
                        ("README.md", (HERE / "README.md").read_bytes().decode("utf-8"))):
        line = usage(text)
        missing = [f for f in flags if not re.search(rf"\[{re.escape(f)}[\] ]", line)]
        check(f"{where}'s usage line names every flag the command line takes",
              "--client" in flags and not missing, f"missing {missing} from:\n{line}")


# ---------------------------------------------- a page from another address

#: an application's own page, showing the widgets: the page a server allows
PAGE = "http://127.0.0.1:7867"
#: a website open in the same browser
ELSEWHERE = "http://example.com"
JSON_BODY = {"Content-Type": "application/json"}


def ask(url: str, method: str, path: str, body=None, headers=None):
    """-> (status, headers, raw): the whole answer, headers and all - what a
    browser is given. A request that gets no answer at all is status 0, never
    an exception that ends the suite before its tally."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, list(r.headers.items()), r.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, list(e.headers.items()), e.read()
    except OSError as e:
        return 0, [], f"no answer: {type(e).__name__}: {e}".encode("utf-8")


def cors(headers) -> dict:
    """What tells a browser whether a page may read an answer: every
    Access-Control-* header, and Vary - by lower-case name."""
    return {k.lower(): v for k, v in headers
            if k.lower().startswith("access-control-") or k.lower() == "vary"}


def names(headers) -> list:
    return sorted(k.lower() for k, _ in headers)


#: a website open in the same browser whose name was made to point at this
#: machine (DNS rebinding): its browser sends that name as the Host, and the
#: page's address - the same name - as the Origin
REBOUND = "rebound.example"


def raw(port: int, method: str, path: str, headers: dict, body: bytes = b"",
        version: str = "HTTP/1.1", timeout: float = 5.0, until_closed: bool = False,
        host: str = "127.0.0.1"):
    """One request exactly as a program writes it - its headers as given and
    no others (a Host of any name, or none at all), then *body*, whatever the
    Content-Length says - and the answer read as http.client reads one; sent
    to *host*, whatever the Host header names.
    -> (status, headers, raw, seconds, closed): status 0 when nothing came back
    in time; `closed` - with *until_closed* - whether the server then closed
    the connection within the timeout."""
    t0 = time.monotonic()
    head = f"{method} {path} {version}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall((head + "\r\n").encode("latin-1") + body)
            answer = HTTPResponse(s, method=method)         # a HEAD's answer has no body
            try:
                answer.begin()
                data = answer.read()
            finally:
                answer.close()
            took = time.monotonic() - t0
            closed = False
            if until_closed:
                try:
                    closed = s.recv(1) == b""
                except OSError:
                    closed = False
            return answer.status, answer.getheaders(), data, took, closed
    except (OSError, HTTPException) as e:
        return (0, [], f"no answer: {type(e).__name__}: {e}".encode("utf-8"),
                time.monotonic() - t0, False)


def everything(port: int, data: bytes, limit: float = 5.0):
    """*data* sent on a connection of its own, then every byte the server
    sends read until it closes the connection. -> (every byte it sent,
    whether it closed the connection within *limit*)."""
    got = b""
    with socket.create_connection(("127.0.0.1", port), timeout=limit) as s:
        s.sendall(data)
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            s.settimeout(max(0.01, deadline - time.monotonic()))
            try:
                piece = s.recv(65536)
            except TimeoutError:
                break
            except OSError:
                return got, True                            # a reset: closed
            if not piece:
                return got, True
            got += piece
    return got, False


def answers_in(data: bytes):
    """Every answer in what a server sent on one connection, in order. ->
    (the status of each, what is left that is not an answer)."""
    statuses, rest = [], data
    while rest.startswith(b"HTTP/"):
        head, _, rest = rest.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        statuses.append(int(lines[0].split()[1]))
        length = next((int(ln.split(b":", 1)[1]) for ln in lines[1:]
                       if ln.lower().startswith(b"content-length:")), 0)
        rest = rest[length:]
    return statuses, rest


def in_chunks(*pieces: bytes) -> bytes:
    """A body as Transfer-Encoding: chunked sends it: each piece, then the
    last, empty chunk."""
    return b"".join(b"%x\r\n%s\r\n" % (len(p), p) for p in pieces) + b"0\r\n\r\n"


def held(port: int, first: bytes, drip: bytes = b"", every: float = 0.0, limit: float = 5.0):
    """What a slow asker meets: *first* sent at once, then - with *every* -
    one byte of *drip* every *every* seconds, for as long as the connection
    is open. -> (every byte the server sent, the seconds until it closed the
    connection - None when it had not within *limit*)."""
    t0 = time.monotonic()
    got, sent = b"", 0
    with socket.create_connection(("127.0.0.1", port), timeout=limit) as s:
        if first:
            s.sendall(first)
        due = t0 + every if every else None
        while time.monotonic() - t0 < limit:
            wait = 0.05 if due is None else min(0.05, max(0.001, due - time.monotonic()))
            s.settimeout(wait)
            try:
                piece = s.recv(65536)
            except TimeoutError:
                piece = None
            except OSError:
                return got, time.monotonic() - t0          # a reset: closed
            if piece == b"":
                return got, time.monotonic() - t0
            got += piece or b""
            if due is not None and time.monotonic() >= due and sent < len(drip):
                try:
                    s.sendall(drip[sent:sent + 1])
                except OSError:
                    return got, time.monotonic() - t0
                sent += 1
                due += every
    return got, None


def at_once(*calls):
    """Each of *calls* on a thread of its own, all at once: -> their answers,
    in order (an exception is an answer too)."""
    out = [None] * len(calls)

    def run(i, fn):
        try:
            out[i] = fn()
        except Exception as e:                              # noqa: BLE001
            out[i] = e
    threads = [threading.Thread(target=run, args=(i, fn), daemon=True)
               for i, fn in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return out


@contextlib.contextmanager
def reusing():
    """A helper's socket that binds as the standard library's http.server
    does - SO_REUSEADDR set: another program's server."""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        yield s
    finally:
        s.close()


def reason_of(data: bytes):
    """The `reason` of a JSON answer, or None."""
    try:
        out = json.loads(data)
    except ValueError:
        return None
    return out.get("reason") if isinstance(out, dict) else None


def show(got) -> str:
    return f"{got[0]} {got[2][:160]!r} after {got[3]:.2f}s {cors(got[1])}"


def header(got, name: str):
    return next((v for k, v in got[1] if k.lower() == name.lower()), None)


class Bare(BaseHTTPRequestHandler):
    """http.server with no method of its own: what an OPTIONS was answered
    before this server took the method."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass


def origins(tmp: Path) -> None:
    """A page from another address is refused before any route, unless the
    server was started to allow it; an allowed page's browser is told what it
    needs, its preflight answered; a request with no Origin, and one from the
    server's own page, are answered as before."""
    folder, other, client = tmp / "origins", tmp / "origins-other", tmp / "origins-page"
    for f in (folder, other, client):
        f.mkdir()
    (client / "index.html").write_bytes(b"<!doctype html><p>the page</p>\n")
    lines: list = []
    httpd = S.serve(free_port(), backend=S.EchoBackend(delay_s=0.001), log=lines.append, client=client,
                    allow_origins=(PAGE,), **FAST)
    closed = S.serve(free_port(), log=lambda s: None, **FAST)            # started to allow no page
    bare = ThreadingHTTPServer(("127.0.0.1", free_port()), Bare)
    BARE.append(bare.server_address[1])
    threading.Thread(target=bare.serve_forever, daemon=True, name="bare-http").start()
    url, port = httpd.app.url, httpd.server_address[1]
    own = f"http://127.0.0.1:{port}"
    seen: set = set()

    def before_any_route(status: int, raw: bytes) -> bool:
        """The answer checked against what the contract declares before any
        route - and remembered, for the coverage."""
        shape = BEFORE["responses"].get(str(status))
        try:
            out = json.loads(raw)
        except ValueError:
            out = None
        bad = [f"{status} is not declared before any route"] if shape is None \
            else V.check(out, shape, SHAPES)
        check(f"contract: {BEFORE['id']} -> {status}", not bad, "; ".join(bad))
        if not bad:
            seen.add((status, out["reason"]))
        return not bad

    def jobs_now() -> set:
        return {j["id"] for j in json.loads(ask(url, "GET", "/api/jobs")[2])["jobs"]}

    try:
        # --- no Origin, and the server's own page: answered as before
        s, h, raw = ask(url, "GET", "/api/workspace")
        check("a request with no Origin is answered as before, and says nothing of pages",
              (s, json.loads(raw)) == (200, {"loaded": False, "workspace": None}) and not cors(h),
              f"{s} {raw[:80]!r} {cors(h)}")
        s2, h2, raw2 = ask(url, "GET", "/api/workspace", headers={"Origin": own})
        check("...one from the server's own page is answered the same, header for header",
              (s2, raw2) == (s, raw) and names(h2) == names(h), f"{s2} {raw2[:80]!r} {names(h2)}")
        s, h, raw = ask(url, "POST", "/api/workspace", {"path": str(folder)},
                        {**JSON_BODY, "Host": f"localhost:{port}",
                         "Origin": f"http://localhost:{port}"})
        check("...however its address is written (localhost) - and it acts, as before",
              s == 200 and json.loads(raw).get("workspace") == str(folder.resolve())
              and not cors(h), f"{s} {raw[:120]!r} {cors(h)}")
        ref = ask(f"http://127.0.0.1:{bare.server_address[1]}", "OPTIONS", "/api/jobs")
        for said, headers in (("with no Origin", {}), ("from the server's own page", {"Origin": own})):
            s, h, raw = ask(url, "OPTIONS", "/api/jobs", headers=headers)
            check(f"OPTIONS {said} is what it always was: 501, a method the server does not take",
                  ref[0] == 501 and (s, raw) == (ref[0], ref[2]) and names(h) == names(ref[1]),
                  f"{s} {raw[:60]!r} {names(h)}; http.server alone: {ref[0]} {ref[2][:60]!r}")

        # --- the page the server was started to allow
        s, h, raw = ask(url, "GET", "/api/health", headers={"Origin": PAGE})
        check("a page the server was started to allow is answered, and its browser told it may "
              "read the answer",
              s == 200 and not V.check(json.loads(raw), ROUTES["server.health"]["responses"]["200"],
                                       SHAPES)
              and cors(h) == {"access-control-allow-origin": PAGE, "vary": "Origin"},
              f"{s} {cors(h)}")
        before = jobs_now()
        s, h, raw = ask(url, "POST", "/api/jobs", {"kind": "generate", "gen": {"model": MODEL},
                                                   "input": {"prompt": "from the page"}},
                        {**JSON_BODY, "Origin": PAGE})
        check("...it acts: its job is taken, and the answer is its to read",
              s == 200 and jobs_now() - before == {json.loads(raw).get("job_id")}
              and cors(h).get("access-control-allow-origin") == PAGE,
              f"{s} {raw[:120]!r} {cors(h)}")
        s, h, raw = ask(url, "GET", "/api/jobs/j-9999/result", headers={"Origin": PAGE})
        check("...a refusal a route gives it is its to read too, in the server's words",
              s == 404 and json.loads(raw).get("reason") == "no_such_job"
              and cors(h).get("access-control-allow-origin") == PAGE, f"{s} {raw[:120]!r} {cors(h)}")
        s, h, raw = ask(url, "GET", "/", headers={"Origin": PAGE})
        check("...and so is the server's own page",
              s == 200 and raw.startswith(b"<!doctype html>")
              and cors(h).get("access-control-allow-origin") == PAGE, f"{s} {cors(h)}")
        s, h, raw = ask(url, "OPTIONS", "/api/jobs", headers={
            "Origin": PAGE, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type"})
        c = cors(h)
        check("its preflight is answered 204: the methods the routes take, and the header a "
              "JSON body needs",
              s == 204 and raw == b"" and c.get("access-control-allow-origin") == PAGE
              and {m.strip() for m in c.get("access-control-allow-methods", "").split(",")}
              == {"GET", "POST"}
              and c.get("access-control-allow-headers", "").lower() == "content-type"
              and c.get("vary") == "Origin", f"{s} {c} {raw[:80]!r}")

        # --- a page from anywhere else: refused before any route
        s, h, raw = ask(url, "GET", "/api/health", headers={"Origin": ELSEWHERE})
        check("a page from anywhere else is refused before any route, its browser told nothing",
              s == 403 and before_any_route(s, raw) and not cors(h), f"{s} {raw[:120]!r} {cors(h)}")
        # a text body: what a browser sends from a page without asking first -
        # and the server reads a body as JSON whatever its type
        s, h, raw = ask(url, "POST", "/api/workspace", {"path": str(other)},
                        {"Content-Type": "text/plain", "Origin": ELSEWHERE})
        _, _, now = ask(url, "GET", "/api/workspace")
        check("...not even a request its browser sends without asking first acts: the folder stays",
              s == 403 and before_any_route(s, raw)
              and json.loads(now).get("workspace") == str(folder.resolve()),
              f"{s} {raw[:120]!r}, then {now[:120]!r}")
        before = jobs_now()
        s, h, raw = ask(url, "POST", "/api/jobs", {"kind": "generate", "gen": {"model": MODEL},
                                                   "input": {"prompt": "from elsewhere"}},
                        {"Content-Type": "text/plain", "Origin": ELSEWHERE})
        check("...and no job is taken from it", s == 403 and before_any_route(s, raw)
              and jobs_now() == before, f"{s} {raw[:120]!r}")
        # a browser keeps its connection open, so a refusal must leave it in
        # step: the refused body read and set aside, never taken for the start
        # of the next request (urllib closes every connection: this cannot)
        got = []
        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("POST", "/api/workspace", body=json.dumps({"path": str(other)}),
                         headers={"Content-Type": "text/plain", "Origin": ELSEWHERE})
            first = conn.getresponse()
            got.append((first.status, first.read()[:80]))
            conn.request("GET", "/api/workspace")
            second = conn.getresponse()
            got.append((second.status, json.loads(second.read())))
        except (OSError, HTTPException, ValueError) as e:
            got.append((0, f"{type(e).__name__}: {e}"))
        finally:
            conn.close()
        check("...its body is read and set aside: the next request on the same connection "
              "is answered as itself",
              len(got) == 2 and got[0][0] == 403
              and got[1] == (200, {"loaded": True, "workspace": str(folder.resolve())}), str(got))
        s, h, raw = ask(url, "OPTIONS", "/api/jobs", headers={
            "Origin": ELSEWHERE, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type"})
        check("...its preflight is refused, its browser told nothing",
              s == 403 and before_any_route(s, raw) and not cors(h), f"{s} {cors(h)}")
        got = [ask(url, "GET", path, headers={"Origin": ELSEWHERE}) for path in ("/", "/nowhere")]
        check("...whatever it asks: the page, and a path no route has",
              all(g[0] == 403 and before_any_route(g[0], g[2]) for g in got),
              str([(g[0], g[2][:60]) for g in got]))
        s, _, raw = ask(url, "GET", "/api/health", headers={"Origin": "null"})
        check("...and a page that gives no address of its own (Origin: null) is refused too",
              s == 403 and before_any_route(s, raw), f"{s} {raw[:120]!r}")
        s, _, raw = ask(closed.app.url, "GET", "/api/health", headers={"Origin": PAGE})
        check("a server started to allow no page refuses that one too: a page is allowed by name",
              s == 403 and before_any_route(s, raw), f"{s} {raw[:120]!r}")
        check("the refusal is in the server's log, naming the page's address",
              any(line.startswith(f"GET /api/health -> 403 origin_not_allowed from {ELSEWHERE} (")
                  for line in lines), "\n".join(lines[-6:]))
        check("...and so is the preflight answered, naming the page",
              any(line.startswith(f"OPTIONS /api/jobs -> 204 for {PAGE} (") for line in lines),
              "\n".join(lines[-6:]))
        names_and_bodies(tmp, httpd, lines, before_any_route, folder, other)
        chunks_and_methods(tmp, httpd, lines, before_any_route, folder,
                           f"http://127.0.0.1:{bare.server_address[1]}")
        expect_continue(httpd, before_any_route)
        want = {(int(status), reason) for status, shape in BEFORE["responses"].items()
                for reason in V.fields_of(shape, SHAPES)["reason"]["enum"]}
        check(f"every answer the contract declares before any route ({len(want)}), the server gave",
              bool(want) and want <= seen, f"declared {sorted(want)}; given {sorted(seen)}")
    finally:
        S.stop(httpd)
        S.stop(closed)
        bare.shutdown()
        bare.server_close()


#: the cap on a request's body for the rows about bodies: small, so a body over
#: it is small too
CAP = 1024


def submit_of(size: int) -> bytes:
    """A submit's body of exactly *size* bytes: a job whose input is padded."""
    base = {"kind": "generate", "gen": {"model": MODEL}, "input": {"text": ""}}
    base["input"]["text"] = "x" * (size - len(json.dumps(base).encode("utf-8")))
    data = json.dumps(base).encode("utf-8")
    if len(data) != size:
        raise AssertionError(f"a body of {len(data)} bytes, not {size}")
    return data


def post_heard(url: str, data: bytes):
    """-> (status, reason) as a real client hears them (urllib), or the name of
    what cut it off - never an exception that ends the section."""
    req = urllib.request.Request(url + "/api/jobs", data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, reason_of(r.read())
    except urllib.error.HTTPError as e:
        with e:
            try:
                return e.code, reason_of(e.read())
            except OSError as cut:
                return e.code, f"then {type(cut).__name__}"
    except OSError as e:
        return 0, type(e).__name__


def names_and_bodies(tmp: Path, httpd, lines: list, before_any_route, folder: Path,
                     other: Path) -> None:
    """The name a request was sent to (its Host) and its body's size, checked
    before any route as its Origin is: a website whose name was made to point
    at this machine is refused whatever it asks, with or without an Origin; a
    body over the cap is refused, and never read. The server's own names, and
    a request that names no Host, are answered as before."""
    url, port = httpd.app.url, httpd.server_address[1]
    rebound = f"{REBOUND}:{port}"
    bound = str(folder.resolve())

    def refused(got, want: str) -> bool:
        """Refused before any route for *want*, its browser told nothing -
        and checked against the contract, for its coverage."""
        return (got[0] == 403 and before_any_route(got[0], got[2])
                and reason_of(got[2]) == want and not cors(got[1]))

    def workspace_now() -> str:
        return json.loads(ask(url, "GET", "/api/workspace")[2]).get("workspace")

    # --- the name it was sent to
    got = raw(port, "GET", "/api/health", {"Host": rebound, "Origin": f"http://{rebound}"})
    check("a website whose name was made to point at this machine is refused before any route "
          "- that name is its Host, and its Origin too - its browser told nothing",
          refused(got, "host_not_allowed"), show(got))
    text = json.dumps({"path": str(other)}).encode("utf-8")
    got = raw(port, "POST", "/api/workspace", {"Host": rebound, "Origin": f"http://{rebound}",
                                               "Content-Type": "text/plain",
                                               "Content-Length": str(len(text))}, text)
    check("...not even a request its browser sends without asking first acts: the folder stays",
          refused(got, "host_not_allowed") and workspace_now() == bound, show(got))
    got = raw(port, "GET", "/api/workspace", {"Host": rebound})
    check("...and a request with no Origin that names a host the server does not have is refused "
          "too - no longer answered as before", refused(got, "host_not_allowed"), show(got))
    got = [raw(port, method, path, {"Host": rebound})
           for method, path in (("OPTIONS", "/api/jobs"), ("GET", "/"), ("GET", "/nowhere"))]
    check("...whatever it asks: its preflight, the page, a path no route has",
          all(refused(g, "host_not_allowed") for g in got), "; ".join(show(g) for g in got))
    got = raw(port, "GET", "/api/health", {"Host": rebound, "Origin": ELSEWHERE})
    check("the name is checked before the page: a page from elsewhere asking by a name the "
          "server does not have is refused for the name", refused(got, "host_not_allowed"),
          show(got))
    check("the refusal is in the server's log, naming the host it was sent to",
          any(line.startswith(f"GET /api/health -> 403 host_not_allowed for {rebound} (")
              for line in lines), "\n".join(lines[-6:]))
    for said, host in (("localhost", f"localhost:{port}"), ("[::1]", f"[::1]:{port}"),
                       ("127.0.0.1, with no port", "127.0.0.1"),
                       ("written in capitals, as a program may", f"LocalHost:{port}")):
        got = raw(port, "GET", "/api/workspace", {"Host": host})
        check(f"a request to one of the server's own names is answered as before: {said}",
              got[0] == 200 and json.loads(got[2]).get("workspace") == bound, show(got))
    got = [raw(port, "GET", "/api/workspace", {"Host": host})
           for host in (f"{REBOUND}@127.0.0.1:{port}", f"127.0.0.1:{port}/api",
                        f"127.0.0.1:{port}:{port}", "")]
    check("a Host that is not a name and a port is refused, never guessed around",
          all(refused(g, "host_not_allowed") for g in got), "; ".join(show(g) for g in got))
    got = raw(port, "GET", "/api/workspace", {}, version="HTTP/1.0")
    check("a request that names no host at all - a program speaking HTTP/1.0, never a browser "
          "- is answered as before", got[0] == 200 and json.loads(got[2]).get("workspace") == bound,
          show(got))
    t0 = time.monotonic()
    s, _, answer = ask(f"http://localhost:{port}", "GET", "/api/workspace")
    took = time.monotonic() - t0
    check("a real client names the host it was sent to, and is answered: urllib, at localhost",
          s == 200 and json.loads(answer).get("workspace") == bound, f"{s} {answer[:120]!r}")
    measured.append(f"urllib asking localhost of a server on 127.0.0.1 alone: {took:.2f}s "
                    f"(::1 is tried first)")
    aside = S.serve(free_port(), host="127.0.0.2", log=lambda s: None, **FAST)
    named = S.serve(free_port(), log=lambda s: None,
                    allow_origins=("http://app.example:7867", "http://[::2]:7867"), **FAST)
    try:
        s, _, answer = ask(aside.app.url, "GET", "/api/workspace")
        check("the address a server listens on is one of its names: asked at 127.0.0.2, it "
              "answers", s == 200, f"{s} {answer[:120]!r}")
        got = raw(port, "GET", "/api/workspace", {"Host": f"127.0.0.2:{port}"})
        check("...and it is that server's alone: the one on 127.0.0.1 does not answer to it",
              refused(got, "host_not_allowed"), show(got))
        at = aside.server_address[1]
        got = [raw(at, "GET", "/api/workspace", {"Host": f"{name}:{at}"}, host="127.0.0.2")
               for name in ("127.0.0.1", "localhost", "[::1]")]
        check("...and every server's own names are its too, wherever it listens: the one on "
              "127.0.0.2 answers to 127.0.0.1, localhost and [::1]",
              [g[0] for g in got] == [200, 200, 200], "; ".join(show(g) for g in got))
        there = named.server_address[1]
        got = raw(there, "GET", "/api/workspace", {"Host": f"app.example:{there}"})
        check("the host of a page a server was started to allow is one of its names",
              got[0] == 200, show(got))
        got = raw(there, "GET", "/api/workspace", {"Host": f"[::2]:{there}"})
        check("...an IPv6 one in its brackets, as a Host header writes it", got[0] == 200,
              show(got))
        got = raw(there, "GET", "/api/workspace", {"Host": f"other.example:{there}"})
        check("...and no other host is", refused(got, "host_not_allowed"), show(got))
    finally:
        S.stop(aside)
        S.stop(named)

    # --- its body, against a cap of CAP bytes
    small_lines: list = []
    small = S.serve(free_port(), backend=S.EchoBackend(delay_s=0.001), log=small_lines.append,
                    allow_origins=(PAGE,), max_body=CAP, **FAST)
    sp = small.server_address[1]
    host = {"Host": f"127.0.0.1:{sp}"}
    jsonish = {**host, "Content-Type": "application/json"}
    bodies = tmp / "bodies"
    bodies.mkdir()

    def jobs_there() -> set:
        return {j["id"] for j in json.loads(ask(small.app.url, "GET", "/api/jobs")[2])["jobs"]}
    try:
        ask(small.app.url, "POST", "/api/workspace", {"path": str(bodies)}, JSON_BODY)
        before = jobs_there()
        got = raw(sp, "POST", "/api/jobs", {**jsonish, "Content-Length": str(CAP + 1)},
                  submit_of(CAP + 1))
        check("a body one byte over the cap is refused 413 too_large before any route: no job "
              "is taken", got[0] == 413 and before_any_route(got[0], got[2])
              and reason_of(got[2]) == "too_large" and jobs_there() == before, show(got))
        got = raw(sp, "POST", "/api/jobs", {**jsonish, "Content-Length": str(CAP)}, submit_of(CAP))
        check("...and one of exactly the cap is read, and answered by its route: its job is taken",
              got[0] == 200 and jobs_there() - before == {json.loads(got[2]).get("job_id")},
              show(got))
        got = raw(sp, "POST", "/api/jobs", {**jsonish, "Content-Length": str(10 ** 12)},
                  until_closed=True)
        check("a body over the cap is never read: the answer comes at once, though not a byte "
              "of the body is ever sent", got[0] == 413 and reason_of(got[2]) == "too_large"
              and got[3] < 3.0, show(got))
        check("...it says Connection: close, and the connection is closed",
              header(got, "Connection") == "close" and got[4] is True,
              f"{header(got, 'Connection')!r}, closed {got[4]}")
        heard = [post_heard(small.app.url, submit_of(8 * 1024 * 1024)) for _ in range(3)]
        check("a real client whose body is over the cap hears the answer, never a reset: urllib, "
              "8 MiB, three times", heard == [(413, "too_large")] * 3, str(heard))
        got = raw(sp, "POST", "/api/jobs", {**jsonish, "Origin": ELSEWHERE,
                                            "Content-Length": str(10 ** 12)})
        check("a refused page's body over the cap is not read either: its 403 comes at once, "
              "though the body is never sent", got[0] == 403
              and reason_of(got[2]) == "origin_not_allowed" and got[3] < 3.0, show(got))
        got = raw(sp, "POST", "/api/jobs", {**jsonish, "Origin": PAGE,
                                            "Content-Length": str(10 ** 12)})
        check("a page the server allows is told it may read its 413, as every answer it is given",
              got[0] == 413 and cors(got[1]).get("access-control-allow-origin") == PAGE, show(got))
        # "²" (a superscript two) is a digit to str.isdigit, and never to int()
        for length in ("-1", "abc", "²"):
            got = raw(sp, "POST", "/api/workspace", {**jsonish, "Content-Length": length})
            check(f"a Content-Length of {length!a} is not a number of bytes: it reads as no body - "
                  f"the route's own refusal answers it, and nothing waits for a body",
                  got[0] == 400 and reason_of(got[2]) == "no_path" and got[3] < 3.0, show(got))
        check("the 413 is in the server's log, with the body's length",
              any(line.startswith(f"POST /api/jobs -> 413 too_large - a body of {CAP + 1} bytes (")
                  for line in small_lines), "\n".join(small_lines[-6:]))
    finally:
        S.stop(small)

    # --- what the contract says a server answers before any route, word for word
    for ex in BEFORE["examples"]:
        said = {"Host": ex.get("host", f"127.0.0.1:{port}")}
        if "origin" in ex:
            said["Origin"] = ex["origin"]
        if "transfer_encoding" in ex:
            got = raw(port, "POST", "/api/jobs", {**said, "Content-Type": "application/json",
                                                  "Transfer-Encoding": ex["transfer_encoding"]},
                      in_chunks(b"{}"))
        elif "length" in ex:
            got = raw(port, "POST", "/api/jobs", {**said, "Content-Type": "application/json",
                                                  "Content-Length": str(ex["length"])})
        else:
            got = raw(port, "GET", "/api/health", said)
        try:
            out = json.loads(got[2])
        except ValueError:
            out = None
        check(f"the contract's example '{ex['name']}' before any route is answered word for word",
              (got[0], out) == (ex["status"], ex["response"]), show(got))


def chunks_and_methods(tmp: Path, httpd, lines: list, before_any_route, folder: Path,
                       bare_url: str) -> None:
    """A body sent in chunks - its length never said - is refused before any
    route, and never read: before, the server read no body but by its
    Content-Length, and on a connection kept open the chunks were read as the
    next request. A body no route reads - a GET's - is read and set aside for
    the same reason. And every request goes through the checks, whatever its
    method: before, any but GET, POST and OPTIONS was answered 501 by
    http.server before any of them."""
    url, port = httpd.app.url, httpd.server_address[1]
    host = f"127.0.0.1:{port}"
    rebound = f"{REBOUND}:{port}"
    bound = str(folder.resolve())
    job = json.dumps({"kind": "generate", "gen": {"model": MODEL},
                      "input": {"prompt": "in chunks"}}).encode("utf-8")
    # a request of its own, carried in a body: read as a request, it is answered
    inside = f"GET /api/nowhere HTTP/1.1\r\nHost: {host}\r\n\r\n".encode("latin-1")

    def jobs_now() -> set:
        return {j["id"] for j in json.loads(ask(url, "GET", "/api/jobs")[2])["jobs"]}

    def head(method: str, path: str, headers: dict) -> bytes:
        return (f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in
                                                          headers.items()) + "\r\n").encode()

    def refused(got, status: int, want: str) -> bool:
        return (got[0] == status and before_any_route(got[0], got[2])
                and reason_of(got[2]) == want and not cors(got[1]))

    # --- a body in chunks
    before = jobs_now()
    got = raw(port, "POST", "/api/jobs", {"Host": host, "Content-Type": "application/json",
                                          "Transfer-Encoding": "chunked"}, in_chunks(job),
              until_closed=True)
    check("a body sent in chunks - its length never said - is refused 411 length_required "
          "before any route: no job is taken", refused(got, 411, "length_required")
          and jobs_now() == before, show(got))
    check("...it says Connection: close, and the connection is closed",
          header(got, "Connection") == "close" and got[4] is True,
          f"{header(got, 'Connection')!r}, closed {got[4]}")
    sent, closed = everything(port, head("POST", "/api/jobs", {
        "Host": host, "Content-Type": "application/json", "Transfer-Encoding": "chunked"})
        + in_chunks(inside))
    check("...its chunks are never read as a request of their own: one answer, then the "
          "connection closed", answers_in(sent) == ([411], b"") and closed,
          f"{answers_in(sent)}, closed {closed}: {sent[:300]!r}")
    got = raw(port, "GET", "/api/health", {"Host": host, "Transfer-Encoding": "chunked"},
              in_chunks(inside))
    check("...whatever the method: a GET with a body in chunks is refused too",
          refused(got, 411, "length_required"), show(got))
    got = raw(port, "POST", "/api/jobs", {"Host": host, "Content-Type": "application/json",
                                          "Transfer-Encoding": "chunked",
                                          "Content-Length": "100"})
    check("...and one naming a Content-Length too: the Transfer-Encoding says how its body "
          "comes, and nothing is read by the length - the answer comes at once, though no "
          "byte of a body is sent", refused(got, 411, "length_required") and got[3] < 3.0,
          show(got))
    for said, headers in (("its Host", {"Host": rebound}),
                          ("its page", {"Host": host, "Origin": ELSEWHERE})):
        sent, closed = everything(port, head("POST", "/api/jobs", {
            **headers, "Content-Type": "application/json", "Transfer-Encoding": "chunked"})
            + in_chunks(inside))
        statuses, rest = answers_in(sent)
        check(f"a body in chunks refused for {said} is not read either: one answer, 403, and "
              f"the connection closed", statuses == [403] and rest == b"" and closed
              and b"Connection: close" in sent, f"{statuses}, closed {closed}: {sent[:300]!r}")
    check("the 411 is in the server's log, naming how the body came",
          any(line.startswith("POST /api/jobs -> 411 length_required - Transfer-Encoding: "
                              "chunked (") for line in lines), "\n".join(lines[-6:]))

    # --- a body no route reads
    got = []
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", "/api/workspace", body=inside)  # its Content-Length said
        first = conn.getresponse()
        got.append((first.status, json.loads(first.read())))
        conn.request("GET", "/api/health")
        second = conn.getresponse()
        got.append((second.status, second.read()[:60]))
    except (OSError, HTTPException, ValueError) as e:
        got.append((0, f"{type(e).__name__}: {e}"))
    finally:
        conn.close()
    check("a GET's body is read and set aside: the next request on the same connection is "
          "answered as itself, never the body as a request",
          len(got) == 2 and got[0] == (200, {"loaded": True, "workspace": bound})
          and got[1][0] == 200 and got[1][1].startswith(b'{"'), str(got))
    got = []
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("PUT", "/api/workspace", body=inside, headers={"Origin": ELSEWHERE})
        first = conn.getresponse()
        got.append((first.status, reason_of(first.read())))
        conn.request("GET", "/api/health")
        second = conn.getresponse()
        got.append((second.status, second.read()[:60]))
    except (OSError, HTTPException, ValueError) as e:
        got.append((0, f"{type(e).__name__}: {e}"))
    finally:
        conn.close()
    check("...and so is the body of a request refused before any route, whatever its method: a "
          "PUT's, from a page elsewhere",
          len(got) == 2 and got[0] == (403, "origin_not_allowed") and got[1][0] == 200
          and got[1][1].startswith(b'{"'), str(got))

    # --- every method goes through the checks
    others = ("PUT", "DELETE", "PATCH", "BREW")
    got = [raw(port, method, "/api/jobs", {"Host": rebound}) for method in others]
    check("a method no route takes is checked as every request is: from a name the server does "
          "not have, PUT, DELETE, PATCH and a made-up one are refused host_not_allowed - not "
          "501", all(refused(g, 403, "host_not_allowed") for g in got),
          "; ".join(show(g) for g in got))
    got = raw(port, "PUT", "/api/jobs", {"Host": host, "Origin": ELSEWHERE})
    check("...from a page elsewhere, origin_not_allowed", refused(got, 403, "origin_not_allowed"),
          show(got))
    got = raw(port, "DELETE", "/api/jobs", {"Host": host, "Transfer-Encoding": "chunked"},
              in_chunks(inside))
    check("...with a body in chunks, length_required", refused(got, 411, "length_required"),
          show(got))
    check("...and the refusal is in the server's log, naming the method",
          any(line.startswith(f"PUT /api/jobs -> 403 host_not_allowed for {rebound} (")
              for line in lines), "\n".join(lines[-6:]))
    small = S.serve(free_port(), log=lambda s: None, max_body=CAP, **FAST)
    try:
        got = raw(small.server_address[1], "PUT", "/api/jobs",
                  {"Host": f"127.0.0.1:{small.server_address[1]}",
                   "Content-Length": str(10 ** 12)})
        check("...and with a body over the cap, too_large - at once, not a byte of it read",
              got[0] == 413 and reason_of(got[2]) == "too_large" and got[3] < 3.0, show(got))
    finally:
        S.stop(small)
    for method in ("PUT", "DELETE", "HEAD", "BREW"):
        mine = raw(port, method, "/api/jobs", {"Host": host})
        ref = raw(int(bare_url.rsplit(":", 1)[1]), method, "/api/jobs", {"Host": host})
        check(f"{method} passing the checks is answered as http.server answers a method it does "
              f"not take: 501, word for word",
              ref[0] == 501 and (mine[0], mine[2]) == (ref[0], ref[2])
              and names(mine[1]) == names(ref[1]),
              f"{show(mine)} {names(mine[1])}; http.server alone: {show(ref)} {names(ref[1])}")
    heard = []
    for _ in range(3):
        req = urllib.request.Request(url + "/api/jobs", data=b"x" * (8 * 1024 * 1024),
                                     method="PUT")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                heard.append(r.status)
        except urllib.error.HTTPError as e:
            with e:
                heard.append(e.code)
        except OSError as e:
            heard.append(type(e).__name__)
    check("...its body within the cap read and set aside first: a real client sending 8 MiB "
          "hears the 501, never a reset - urllib, three times", heard == [501] * 3, str(heard))


def expect_continue(httpd, before_any_route) -> None:
    """H3 R1: an asker that says `Expect: 100-continue` sends its body only
    once it is told to continue - so it is told only once the checks before
    any route let the request through. http.server told it before any of
    them: a request refused for its Host, its page or its size was invited to
    send the body the refusal would never read - and one within the cap was
    then waited for until the read timeout, the refusal unsent."""
    port = httpd.server_address[1]
    host = f"127.0.0.1:{port}"
    body = json.dumps({"kind": "generate", "gen": {"model": MODEL},
                       "input": {"prompt": "told to continue"}}).encode("utf-8")

    def head(headers: dict) -> bytes:
        return ("POST /api/jobs HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in
                                                       headers.items()) + "\r\n").encode()
    for what, headers, status, reason in (
            ("sent to a name not its own", {"Host": f"{REBOUND}:{port}"}, 403,
             "host_not_allowed"),
            ("from a page it does not allow", {"Host": host, "Origin": ELSEWHERE}, 403,
             "origin_not_allowed"),
            ("with a body over the cap", {"Host": host, "Content-Length": str(10 ** 12)}, 413,
             "too_large")):
        got, closed = everything(port, head({"Content-Length": str(len(body)), **headers,
                                             "Expect": "100-continue"}), limit=3.0)
        statuses, _ = answers_in(got)
        refusal = got.partition(b"\r\n\r\n")[2]
        check(f"a request {what} that expects 100 Continue hears the refusal, never 100 "
              f"Continue: {status} {reason}", statuses == [status]
              and reason_of(refusal) == reason and before_any_route(status, refusal),
              f"{got[:200]!r}")
        check("...its body never waited for: the answer says the connection closes, and it "
              "does", closed and b"\r\nConnection: close\r\n" in got, f"closed={closed}")
    t0 = time.monotonic()
    with socket.create_connection(("127.0.0.1", port), timeout=3.0) as s:
        s.sendall(head({"Host": host, "Content-Type": "application/json",
                        "Content-Length": str(len(body)), "Expect": "100-continue"}))
        told = b""
        try:
            while b"\r\n\r\n" not in told:
                piece = s.recv(4096)
                if not piece:
                    break
                told += piece
        except OSError as e:
            told += f" (no more: {type(e).__name__})".encode()
        check("a request that passes the checks and expects 100 Continue is told to continue",
              told.startswith(b"HTTP/1.1 100 Continue\r\n"), f"{told[:120]!r} after "
              f"{time.monotonic() - t0:.2f}s")
        s.sendall(body)
        answer = HTTPResponse(s, method="POST")
        try:
            answer.begin()
            out = json.loads(answer.read() or b"{}")
        except (OSError, HTTPException, ValueError) as e:
            out = {"error": f"no answer: {type(e).__name__}: {e}"}
        finally:
            answer.close()
    check("...and, its body sent, is answered by its route", answer.status == 200
          and str(out.get("job_id", "")).startswith("j-"), f"{answer.status} {out}")


#: the read timeout the rows about a request's time give a server: short, so
#: a request cut off is seen soon
READ_S = 1.0


def a_reset_connection(tmp: Path) -> None:
    """R-B5, the server's side: a connection its asker resets mid-request - a
    body promised and never sent whole, then closed with SO_LINGER 0, a reset
    (RST) as a process ended mid-request sends - is said in one line of the
    server's log, never a traceback; the server answers the next request."""
    lines: list = []
    httpd = S.serve(free_port(), backend=S.EchoBackend(delay_s=0.001), log=lines.append, **FAST)
    port = httpd.server_address[1]
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            s.sendall((f"POST /api/jobs HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                       f"Content-Type: application/json\r\nContent-Length: 400\r\n\r\n"
                       f'{{"kind": "ge').encode())
            time.sleep(0.3)                             # its handler is reading the body
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            s.close()
            wait_for(lambda: err.getvalue() or any("reset" in line for line in lines), 5.0)
            time.sleep(0.3)                             # a second word, if one comes
        status, _ = Probe(httpd.app.url)("GET", "/api/health")
    finally:
        S.stop(httpd)
    said = [line for line in lines if "reset" in line]
    check("a connection its asker reset mid-request is said in one line of the server's log: "
          "that it was reset, by whom, and that nothing was answered",
          len(said) == 1 and "127.0.0.1" in said[0] and "nothing was answered" in said[0],
          str(lines[-4:]))
    check("...never a traceback", not err.getvalue(), err.getvalue()[:600])
    check("...and the server answers the next request", status == 200, str(status))


def a_request_s_time(tmp: Path) -> None:
    """A request must arrive whole - its line, its headers and its body -
    within the read timeout of the server starting to wait for it, or it is
    never answered and its connection is closed: before, a body within the cap
    that never finished arriving held a handler thread for ever. The bound is
    the whole request's, never each read's; each request on a kept connection
    has its own; a connection that never sent a byte is closed without a
    word."""
    lines: list = []
    httpd = S.serve(free_port(), backend=S.EchoBackend(delay_s=0.001), log=lines.append,
                    read_timeout_s=READ_S, **FAST)
    port = httpd.server_address[1]
    host = f"127.0.0.1:{port}"
    post = (f"POST /api/jobs HTTP/1.1\r\nHost: {host}\r\nContent-Type: application/json\r\n"
            f"Content-Length: 1000\r\n\r\n").encode()
    folder = tmp / "a-request-s-time"
    folder.mkdir()
    try:
        ask(httpd.app.url, "POST", "/api/workspace", {"path": str(folder)}, JSON_BODY)
        body, heads, idle, drip = at_once(
            lambda: held(port, post + b'{"kind": "gen'),
            lambda: held(port, f"POST /api/jobs HTTP/1.1\r\nHost: {host}\r\n".encode()),
            lambda: held(port, b""),
            lambda: held(port, post, drip=b"x" * 1000, every=0.1, limit=6.0))
        for what, (got, closed) in (("a body that never finishes arriving", body),
                                    ("...headers that never finish", heads),
                                    ("...a connection that never sends a byte", idle)):
            check(f"{what} is never answered: its connection is closed once the read timeout "
                  f"({READ_S:g}s) has passed", got == b"" and closed is not None
                  and READ_S * 0.8 <= closed < READ_S + 2.5, f"{got[:80]!r}, closed after {closed}")
        got, closed = drip
        check("the bound is the whole request's, never each read's: a body dripping a byte "
              "every 0.1s is closed once the read timeout has passed",
              got == b"" and closed is not None and READ_S * 0.8 <= closed < READ_S + 2.5,
              f"{got[:80]!r}, closed after {closed}")
        cut = [ln for ln in lines if "not read whole" in ln]
        check("a request cut off part way is in the server's log - and a connection that never "
              "sent a byte is not", len(cut) == 3 and sum(ln.startswith(
                  f"POST /api/jobs -> its connection closed: not read whole within {READ_S:g}s")
                  for ln in cut) == 3, "\n".join(cut) or "\n".join(lines[-6:]))
        statuses = []
        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            for i in range(4):
                if i:
                    time.sleep(READ_S * 0.4)
                conn.request("GET", "/api/health")
                r = conn.getresponse()
                r.read()
                statuses.append(r.status)
        except (OSError, HTTPException) as e:
            statuses.append(type(e).__name__)
        finally:
            conn.close()
        check("each request on a connection kept open has its own time, from the answer before "
              f"it: four asked {READ_S * 0.4:g}s apart, longer than the read timeout in all, are "
              "all answered", statuses == [200] * 4, str(statuses))
        t0 = time.monotonic()
        with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
            data = submit_of(300)
            s.sendall(f"POST /api/jobs HTTP/1.1\r\nHost: {host}\r\nContent-Type: "
                      f"application/json\r\nContent-Length: {len(data)}\r\n\r\n".encode())
            time.sleep(READ_S * 0.2)
            s.sendall(data[:100])
            time.sleep(READ_S * 0.2)
            s.sendall(data[100:])
            answer = HTTPResponse(s)
            try:
                answer.begin()
                said = (answer.status, reason_of(answer.read()))
            except (OSError, HTTPException) as e:
                said = (0, type(e).__name__)
            finally:
                answer.close()
        check("a request that arrives whole within the read timeout is answered, in however many "
              "pieces it came", said[0] == 200, f"{said} after {time.monotonic() - t0:.2f}s")
    finally:
        S.stop(httpd)


def the_read_timeout_is_a_setting(tmp: Path) -> None:
    """--read-timeout: how long a request has to arrive whole, in seconds - 10
    unless it is given - read at the start, and anything that is not a number
    of seconds above 0 refused there."""
    a, said = parsed(["--read-timeout", "2.5"])
    check("--read-timeout sets how long a request has to arrive whole, in seconds",
          a is not None and a.read_timeout == 2.5, said or str(a.read_timeout))
    a, said = parsed([])
    check("...10 seconds when it is not given", a is not None and a.read_timeout == 10.0,
          said or str(a.read_timeout))
    for wrong in ("0", "-1", "abc", "nan", "inf"):
        a, said = parsed(["--read-timeout", wrong])
        check(f"...{wrong!r} is not a number of seconds above 0: refused at the start, saying so",
              a is None and "--read-timeout" in said and "not a number of seconds" in said,
              said[-160:] if said else f"taken, as {a.read_timeout}")
    for wrong in (0, -1, True, "1", float("inf"), float("nan")):
        try:
            S.stop(S.serve(free_port(), log=lambda s: None, read_timeout_s=wrong))
            refused = ""
        except ValueError as e:
            refused = str(e)
        check(f"a server made in code is refused a read timeout that is not a number of seconds "
              f"above 0, too: {wrong!r}", "read_timeout_s" in refused, refused or "it started")
    port = free_port()
    child, _ = start(["server.py", "--port", str(port), "--backend", "echo",
                      "--read-timeout", "0.5"], "listening on")
    try:
        got, closed = held(port, (f"POST /api/jobs HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                                  f"Content-Length: 100\r\n\r\n{{").encode())
        check("a server started with --read-timeout closes a request that has not come whole "
              "within it", got == b"" and closed is not None and 0.4 <= closed < 3.0,
              f"{got[:80]!r}, closed after {closed}")
    finally:
        kill_and_wait(child)


def a_failed_load_from_its_settings(tmp: Path) -> None:
    """S14b R3: the stand-in server, started from its command line with a
    settings file naming a model in `[mock] fail_load_models`, shows that
    model's load failed - `failed`, with why, in the load's own answer and in
    the list of models - with no card and no model file: what a page's Models
    widget shows for a failed load, seen without a card."""
    settings = tmp / "failing.toml"
    settings.write_bytes(b'[mock]\nload_seconds = 0.05\nunload_seconds = 0\n'
                         b'fail_load_models = ["broken"]\n\n'
                         b'[models.broken]\nlabel = "Broken"\n\n[models.fine]\nlabel = "Fine"\n')
    port = free_port()
    child, _ = start(["server.py", "--port", str(port), "--backend", "mock",
                      "--runner", str(settings)], "listening on")
    try:
        p = Probe(f"http://127.0.0.1:{port}")
        s, opened = p("POST", "/api/pipeline/open?wait_s=8", {"models": ["fine"], "keep_s": 0})
        s, out = p("POST", "/api/models/broken/load?wait_s=8")
        failed = out.get("model") or {}
        check("a stand-in server started with settings naming a model in fail_load_models: "
              "its load answers failed, with why in the stand-in's words",
              failed.get("state") == "failed" and "broken" in (failed.get("why") or "")
              and "fail_load_models" in (failed.get("why") or ""), json.dumps([opened, out]))
        s, listed = p("GET", "/api/models")
        by = {m["key"]: m for m in listed.get("models") or []}
        check("...and the list of models says so: failed, with that why - while the model it "
              "does not name is up", (by.get("broken") or {}).get("state") == failed
              and ((by.get("fine") or {}).get("state") or {}).get("state") == "up",
              json.dumps([by.get("broken"), by.get("fine")]))
    finally:
        kill_and_wait(child)


def its_port_alone(tmp: Path) -> None:
    """The server's port is its alone. http.server binds SO_REUSEADDR, and on
    Windows a second program's socket that sets it too was let bind the port
    of a listening server; and a server started on a port such a program
    already listened on started, said it was listening - and of 20 requests
    heard none: the program took them all (measured before this was fixed).
    Each way round is provoked with a helper's socket, bound as http.server
    binds; the second at the command line too."""
    httpd = S.serve(free_port(), log=lambda s: None, **FAST)
    port = httpd.server_address[1]
    try:
        with reusing() as helper:
            try:
                helper.bind(("127.0.0.1", port))
                said = "it bound"
            except OSError as e:
                said = ""
                refusal = str(e)
        check("a program binding SO_REUSEADDR is refused the port a server listens on",
              not said, said or refusal)
    finally:
        S.stop(httpd)
    port = free_port()
    with reusing() as helper:
        helper.bind(("127.0.0.1", port))
        helper.listen(8)
        try:
            S.stop(S.serve(port, log=lambda s: None, **FAST))
            refused = ""
        except OSError as e:
            refused = str(e)
        check("a server made on a port a program listens on - bound SO_REUSEADDR - is refused "
              "it, never shares it", bool(refused), refused or "it started")
        try:
            done = subprocess.run([sys.executable, str(HERE / "server.py"), "--port", str(port),
                                   "--backend", "echo"], capture_output=True, cwd=HERE,
                                  encoding="utf-8", errors="replace", timeout=20)
            code, out = done.returncode, done.stdout + done.stderr
        except subprocess.TimeoutExpired as e:
            code, out = "still running after 20s", str(e.stdout or "")
        check("...and one started there says it cannot start, and never that it listens",
              code == 2 and "cannot start" in out and str(port) in out
              and "listening on" not in out, f"{code}: {out[-300:]}")
        check("...in the words of what it was: it cannot listen on its port",
              f"cannot start: cannot listen on 127.0.0.1:{port}: " in out, out[-300:])
    # H3 R3: any OSError while serving started was said to be the port - here
    # the port is free, and the backend cannot be attached
    class Unattachable:
        def attach(self, actor):
            raise OSError("the runner's record cannot be written: the disk is full")
    port = free_port()
    a, _ = parsed(["--port", str(port), "--backend", "echo"])
    real = S.build_backend
    S.build_backend = lambda *args, **kw: Unattachable()
    let_go = None
    try:
        S.stop(S.start_server(a, log=lambda s: None))
        said = "it started"
    except S.CannotStart as e:
        said = str(e)
        # asked while the error - and all it holds - is still here: the port
        # is let go as the start stops, never when the error is collected
        let_go = bindable(port)
    finally:
        S.build_backend = real
    check("a start that fails for another reason says what it was - never that it cannot "
          "listen", said == "the runner's record cannot be written: the disk is full", said)
    check("...and the port it had is let go as it stops", let_go is True, str(let_go))


def bindable(port: int) -> bool:
    """Can a socket bind *port* on 127.0.0.1 now, as the server binds it?"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def the_body_cap_is_a_setting(tmp: Path) -> None:
    """--max-body: the largest body the server reads, a whole number of bytes,
    16 MiB unless it is given - read at the start, and anything that is not
    one refused there."""
    a, said = parsed(["--max-body", "4096"])
    check("--max-body sets the largest body the server reads, in bytes",
          a is not None and a.max_body == 4096, said or str(a.max_body))
    a, said = parsed([])
    check("...16 MiB when it is not given", a is not None and a.max_body == 16 * 1024 * 1024,
          said or str(a.max_body))
    for wrong in ("0", "-5", "1.5", "lots"):
        a, said = parsed(["--max-body", wrong])
        check(f"...{wrong!r} is not a number of bytes, 1 or more: refused at the start, saying so",
              a is None and "--max-body" in said and "not a whole number of bytes" in said,
              said[-160:] if said else f"taken, as {a.max_body}")
    # True is refused though Python's True is an int: a cap is a number of bytes
    for wrong in (0, True, 1.5):
        try:
            S.stop(S.serve(free_port(), log=lambda s: None, max_body=wrong))
            refused = ""
        except ValueError as e:
            refused = str(e)
        check(f"a server made in code is refused a cap that is not a whole number of bytes, 1 "
              f"or more, too: {wrong!r}", "max_body" in refused, refused or "it started")
    port = free_port()
    child, _ = start(["server.py", "--port", str(port), "--backend", "echo", "--max-body", "64"],
                     "listening on")
    try:
        url = f"http://127.0.0.1:{port}"
        s1, _, over = ask(url, "POST", "/api/workspace", {"path": "x" * 80}, JSON_BODY)
        s2, _, under = ask(url, "POST", "/api/workspace", {"path": "nowhere"}, JSON_BODY)
        check("a server started with --max-body reads bodies up to it, and refuses one over it",
              (s1, reason_of(over)) == (413, "too_large")
              and (s2, reason_of(under)) == (400, "unusable_folder"),
              f"{s1} {over[:120]!r}; {s2} {under[:120]!r}")
    finally:
        kill_and_wait(child)


def parsed(argv: list):
    """-> (the arguments, None), or (None, what the refusal said): a command
    line refused is a check's answer, never the end of the suite - argparse
    refuses by SystemExit, which no section catches."""
    said = io.StringIO()
    try:
        with contextlib.redirect_stderr(said):
            return S.parse_args(argv), None
    except SystemExit:
        return None, said.getvalue().strip()


def allowing_by_name(tmp: Path) -> None:
    """--allow-origin, read as a browser writes a page's address - and
    anything that is not one refused at the start, never met at the first
    request."""
    a, said = parsed(["--allow-origin", PAGE, "--allow-origin", "HTTP://LocalHost:80/",
                      "--allow-origin", "https://App.Example:443",
                      "--allow-origin", "http://[::1]:7867"])
    check("--allow-origin names pages as their browser writes them - lower case, no default "
          "port, no slash, an IPv6 host in brackets - and may be given again",
          a is not None and a.allow_origin == [PAGE, "http://localhost", "https://app.example",
                                               "http://[::1]:7867"],
          said or str(a.allow_origin))
    a, said = parsed([])
    check("...and none given, none is allowed", a is not None and not a.allow_origin,
          said or str(a.allow_origin))
    for wrong in ("127.0.0.1:7867", "http://127.0.0.1:7867/app", "null", "*",
                  "ftp://127.0.0.1", "http://127.0.0.1:7867?x=1", "http://127.0.0.1:7867#top",
                  "http://me@127.0.0.1:7867", "http://127.0.0.1:99999"):
        a, said = parsed(["--allow-origin", wrong])
        check(f"...{wrong!r} is not a page's address: refused at the start, saying so",
              a is None and "not a page's address" in said,
              said[-160:] if said else f"taken, as {a.allow_origin}")
    try:
        S.stop(S.serve(free_port(), log=lambda s: None, allow_origins=("null",)))
        refused = ""
    except ValueError as e:
        refused = str(e)
    check("a server made in code is refused an address that is not a page's too",
          "not a page's address" in refused, refused or "it started")
    port = free_port()
    child, line = start(["server.py", "--port", str(port), "--backend", "echo",
                         "--allow-origin", PAGE], "may use it")
    try:
        url = f"http://127.0.0.1:{port}"
        s, h, _ = ask(url, "GET", "/api/health", headers={"Origin": PAGE})
        s2, _, _ = ask(url, "GET", "/api/health", headers={"Origin": ELSEWHERE})
        check("a server started with --allow-origin says so, answers that page, and refuses "
              "any other", PAGE in line and s == 200
              and cors(h).get("access-control-allow-origin") == PAGE and s2 == 403,
              f"{line!r} {s} {s2}")
    finally:
        kill_and_wait(child)


# ------------------------------------------------------------------ the echo

def echo(tmp: Path) -> Probe:
    folder = tmp / "echo"
    folder.mkdir()
    httpd = S.serve(free_port(), backend=S.EchoBackend(delay_s=0.001), log=lambda s: None, **FAST)
    p = Probe(httpd.app.url)
    try:
        p("POST", "/api/workspace", {"path": str(folder)})
        jid = job(p, input={"prompt": "a haiku about rain"})
        reach(httpd.app.url, jid, "done", "failed")
        s, out = p("GET", f"/api/jobs/{jid}/result")
        check("the echo backend answers the prompt back, through every layer",
              s == 200 and out["candidates"][0]["text"] == "a haiku about rain", str(out))
        s, out = p("GET", "/api/health")
        check("...and health reports it as the last job that ran",
              out["queue"]["last"]["job"] == jid and out["queue"]["last"]["status"] == "done",
              str(out["queue"]))
        p("POST", "/api/workspace/unload")
    finally:
        S.stop(httpd)
    httpd = S.serve(free_port(), backend=None, log=lambda s: None, **FAST)
    p2 = Probe(httpd.app.url)
    try:
        p2("POST", "/api/workspace", {"path": str(folder)})
        jid = job(p2, input={"prompt": "anything"})
        reach(httpd.app.url, jid, "failed", "done")
        s, out = p2("GET", f"/api/jobs/{jid}/result")
        check("with no backend a generation fails, and says why",
              out["job"]["status"] == "failed" and "no backend" in (out["job"]["error"] or ""),
              str(out["job"]))
    finally:
        S.stop(httpd)
    return p


# ---------------------------------------------------------- transcriptions

def transcriptions(tmp: Path) -> Probe:
    """A whisper model over real HTTP (PLAN-4 D11): the list shows its own
    header and no context; its request is checked AT SUBMIT against the
    fields whisper-server reads - a misspelt one, one of the wrong kind, the
    engine's own `response_format`, a prompt - refused `bad_audio_request`
    in its words, and no job made of any; its own request is taken, and
    answered whole."""
    import struct
    import wave
    root = tmp / "ears-models"
    root.mkdir()
    (root / "ears.bin").write_bytes(struct.pack("<I11i", 0x67676d6c, 51866, 1500, 1280, 20, 32,
                                                448, 1280, 20, 32, 128, 1))
    settings = tmp / "ears.toml"
    settings.write_text(
        f'[runner]\nbackend = "mock"\nmodels_dir = "{root.as_posix()}"\n'
        '[mock]\nload_seconds = 0.0\nanswer_seconds = 0.0\n'
        '[models.demo]\nlabel = "the stand-in"\n'
        '[models.ears]\nengine = "whisper"\nfile = "ears.bin"\n', encoding="utf-8")
    backend = S.build_backend("mock", settings, log=lambda s: None, vram=lambda: None)
    httpd = S.serve(free_port(), backend=backend, log=lambda s: None, **FAST)
    p = Probe(httpd.app.url)
    folder = tmp / "ears-folder"
    folder.mkdir()
    take = folder / "take.wav"
    with wave.open(str(take), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((struct.pack("<h", 1200) + struct.pack("<h", -1200)) * 8000)
    try:
        s, out = p("GET", "/api/models")
        ears = {m["key"]: m for m in out.get("models", [])}.get("ears") or {}
        check("a whisper model: no context to set, and its own header - the eleven numbers",
              ears.get("context", 0) is None and (ears.get("header") or {}).get("shape")
              == "large v3" and ears["header"].get("n_mels") == 128, json.dumps(ears))
        p("POST", "/api/workspace", {"path": str(folder)})
        for why, input, said in (
                ("a field whisper-server does not read", {"audio": str(take),
                                                          "fields": {"langauge": "no"}},
                 '$.fields: unexpected field "langauge"'),
                ("a field of the wrong kind", {"audio": str(take), "fields": {"beam_size": "5"}},
                 "$.fields.beam_size"),
                ("the engine's own response_format", {"audio": str(take),
                                                      "fields": {"response_format": "json"}},
                 "response_format is the engine's own"),
                ("a prompt", "Tell me about rain.", "ears transcribes audio")):
            s, out = p("POST", "/api/jobs", {"kind": "transcribe", "input": input,
                                             "gen": {"model": "ears"}})
            check(f"{why}: refused at submit, bad_audio_request, in its words",
                  s == 400 and out.get("reason") == "bad_audio_request"
                  and said in str(out.get("error")), f"{s} {out}")
        s, out = p("GET", "/api/jobs")
        check("...and no job is made of any of them", s == 200 and out.get("jobs") == [], str(out))
        s, out = p("POST", "/api/jobs", {"kind": "transcribe", "gen": {"model": "ears"},
                                         "input": {"audio": str(take),
                                                   "fields": {"language": "no", "beam_size": 5}}})
        check("its own request is taken", s == 200, f"{s} {out}")
        jid = out.get("job_id")
        check("...and answered whole", reach(httpd.app.url, jid, "done", timeout=20), str(jid))
        s, out = p("GET", f"/api/jobs/{jid}/result")
        got = (out.get("candidates") or [{}])[0]
        check("...its transcript and its audio's length beside its text",
              got.get("audio_s") == 1.0 and (got.get("transcript") or {}).get("language")
              == "norwegian" and got.get("text") == got["transcript"]["text"], str(got)[:400])
    finally:
        S.stop(httpd)
    return p


# ------------------------------------------------------- the request check

def request_check(tmp: Path) -> Probe:
    """THE REQUEST CHECK over real HTTP (PLAN-4 D16), on the stand-in
    runner: a job marked `check` is held once its request is built - read
    whole, sent as built or changed, refused as a submit is, cancelled - a
    whisper model's audio and fields shown; a held job is no work the card
    is kept for; a job not marked says nothing of a check."""
    import struct
    import wave
    root = tmp / "check-models"
    root.mkdir()
    (root / "ears.bin").write_bytes(struct.pack("<I11i", 0x67676d6c, 51866, 1500, 1280, 20, 32,
                                                448, 1280, 20, 32, 128, 1))
    settings = tmp / "check.toml"
    settings.write_text(
        f'[runner]\nbackend = "mock"\nmodels_dir = "{root.as_posix()}"\n'
        '[mock]\nload_seconds = 0.0\nanswer_seconds = 0.0\n'
        '[models.demo]\nlabel = "the stand-in"\ntemperature = 0.6\ndisable_thinking = true\n'
        '[models.small]\nlabel = "a small stand-in"\nctx = 16\n'
        '[models.ears]\nengine = "whisper"\nfile = "ears.bin"\n', encoding="utf-8")
    backend = S.build_backend("mock", settings, log=lambda s: None, vram=lambda: None)
    httpd = S.serve(free_port(), backend=backend, log=lambda s: None, **FAST)
    app, url = httpd.app, httpd.app.url
    p = Probe(url)
    folder = tmp / "check-folder"
    folder.mkdir()
    take = folder / "take.wav"
    with wave.open(str(take), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((struct.pack("<h", 1200) + struct.pack("<h", -1200)) * 8000)
    try:
        p("POST", "/api/workspace", {"path": str(folder)})
        plain = job(p, gen={"model": "demo"}, input={"prompt": "not marked"})
        reach(url, plain, "done", "failed")
        s, out = p("GET", f"/api/jobs/{plain}/result")
        check("a job not marked runs as ever, and says nothing of a check",
              out["job"]["status"] == "done" and "check" not in out["job"], str(out["job"]))
        s, out = p("GET", f"/api/jobs/{plain}/held")
        check("...and is not held: not_held, with its status",
              (s, out.get("reason"), out.get("status")) == (409, "not_held", "done"), str(out))
        s, out = p("POST", f"/api/jobs/{plain}/send", {})
        check("...nor sent", (s, out.get("reason"), out.get("status"))
              == (409, "not_held", "done"), str(out))
        for method, path, body in (("GET", "/api/jobs/j-9999/held", None),
                                   ("POST", "/api/jobs/j-9999/send", {})):
            s, out = p(method, path, body)
            check(f"{method} {path}: no_such_job", (s, out.get("reason")) == (404, "no_such_job"),
                  str(out))

        jid = job(p, gen={"model": "demo", "max_tokens": 40}, input={"prompt": "Tell me of rain."},
                  check=True)
        s, out = p("GET", f"/api/jobs/{jid}/result")
        check("a marked job says so", out["job"].get("check") in ("marked", "held"), str(out))
        check("...and is held once its request is built", reach(url, jid, "held"), jid)
        s, out = p("GET", f"/api/jobs/{jid}/held")
        h = out.get("held") or {}
        check("the held request: its model, its engine, its messages by role",
              s == 200 and (h.get("model"), h.get("engine"), h.get("messages"))
              == ("demo", "llamacpp", [{"role": "user", "content": "Tell me of rain."}]),
              json.dumps(out)[:600])
        check("...every parameter and where it came from",
              {x["name"]: (x["value"], x["from"]) for x in h.get("params") or []} == {
                  "max_tokens": (40, "application"), "temperature": (0.6, "model"),
                  "top_p": (1.0, "model"), "min_p": (0.05, "model"),
                  "repeat_penalty": (1.05, "model"),
                  "chat_template_kwargs.enable_thinking": (False, "model")},
              json.dumps(h.get("params")))
        # CHANGED ON PURPOSE (S16a R1): the stand-in counts now - each word a
        # token, against its model's context
        check("...and its count: the stand-in counts what it is asked against its model's "
              "context", h.get("count") == {"prompt_tokens": 4, "room": 40, "context": 8192}
              and h.get("fits") is True
              and not any("cannot count" in w for w in h.get("said") or []),
              json.dumps({"count": h.get("count"), "said": h.get("said")}))
        long = job(p, gen={"model": "small", "max_tokens": 5},
                   input={"prompt": " ".join(["rain"] * 12)})
        check("a request past the stand-in's context fails prompt_too_long, before it is asked "
              "(S16a R1)", reach(url, long, "failed", timeout=20), long)
        s, out = p("GET", f"/api/jobs/{long}/result")
        check("...its reason the contract's, its words the numbers",
              out["job"].get("reason") == "prompt_too_long" and "16" in out["job"]["error"],
              json.dumps(out["job"])[:600])
        check("a held job is no work the card is kept for", app._work_pending() is False,
              "the queue's own pipeline would keep the card for a person reading")
        for body, reason, said in (
                ({"gen": {"model": "other"}}, "bad_request", '$.gen: unexpected field "model"'),
                ({"gen": {"max_tokens": -2}}, "bad_max_tokens", "max_tokens"),
                ({"gen": {"n_keep": -5}}, "out_of_range", "n_keep"),
                ({"request": {"nope": 1}}, "bad_request", "a runner generation needs a prompt")):
            s, out = p("POST", f"/api/jobs/{jid}/send", body)
            check(f"a send of {body} is refused {reason}, in its words, never sent",
                  s == 400 and out.get("reason") == reason and said in str(out.get("error")),
                  f"{s} {out}")
        check("...and the job is still held", status_of(url, jid) == "held", jid)
        s, out = p("POST", f"/api/jobs/{jid}/send",
                   {"request": {"prompt": "Tell me of snow."}, "gen": {"max_tokens": 30}})
        check("sent changed: tuned, naming what changed",
              s == 200 and (out.get("tuned"), out.get("changed"), out["job"].get("check"))
              == (True, ["request", "max_tokens"], "tuned"), str(out))
        check("...it runs, and ends done", reach(url, jid, "done", timeout=20), jid)
        s, out = p("GET", f"/api/jobs/{jid}/result")
        rec = Q.request_record(folder, out["job"]["correlation_id"])
        check("...a tuned request, never mistaken for a normal one: it says so, it ran with "
              "what was sent, and its record keeps both",
              out["job"].get("check") == "tuned" and out["job"]["progress"].get("budget") == 30
              and rec["request"] == {"prompt": "Tell me of rain."}
              and (rec["sent"] or {}).get("request") == {"prompt": "Tell me of snow."}
              and (rec["sent"] or {}).get("tuned") is True,
              json.dumps({"job": out["job"], "sent": rec["sent"]})[:800])
        s, out = p("GET", f"/api/jobs/{jid}/held")
        check("...and it is held no more", (s, out.get("reason")) == (409, "not_held"), str(out))

        again = job(p, gen={"model": "demo"}, input={"prompt": "As it is."}, check=True)
        t0 = time.monotonic()
        s, out = p("GET", f"/api/jobs/{again}/result?wait_s=10")
        check("a wait on a marked job's result is answered the moment it is held (R-B3) - never "
              "waited out", s == 200 and out["job"]["status"] == "held"
              and time.monotonic() - t0 < 5.0,
              f"{out.get('job', {}).get('status')} after {time.monotonic() - t0:.2f}s")
        check("another marked job is held", reach(url, again, "held"), again)
        s, out = p("POST", f"/api/jobs/{again}/send", {})
        check("sent with nothing changed: as built, never mistaken for a tuned one",
              s == 200 and (out.get("tuned"), out.get("changed"), out["job"].get("check"))
              == (False, [], "sent"), str(out))
        check("...and it runs, and ends done", reach(url, again, "done", timeout=20), again)

        ears = job(p, kind="transcribe", gen={"model": "ears"}, check=True,
                   input={"audio": str(take), "fields": {"language": "no"}})
        check("a marked transcription is held", reach(url, ears, "held"), ears)
        s, out = p("GET", f"/api/jobs/{ears}/held")
        h = out.get("held") or {}
        check("...its audio and its length, and whisper-server's own fields",
              s == 200 and (h.get("engine"), h.get("audio"), h.get("fields"), h.get("messages"))
              == ("whisper", {"path": str(take), "seconds": 1.0}, {"language": "no"}, None),
              json.dumps(out)[:600])
        check("...and only what whisper reads (R-B1): no text model's numbers - gen and params "
              "empty, never max_tokens 600 and a seed", (h.get("gen"), h.get("params")) == ({}, []),
              json.dumps({"gen": h.get("gen"), "params": h.get("params")}))
        s, out = p("POST", f"/api/jobs/{ears}/send",
                   {"request": {"audio": str(take), "fields": {"langauge": "no"}}})
        check("a transcription changed into what whisper-server does not read: "
              "bad_audio_request", s == 400 and out.get("reason") == "bad_audio_request"
              and '"langauge"' in str(out.get("error")), f"{s} {out}")
        s, out = p("POST", f"/api/jobs/{ears}/cancel")
        check("a held job cancelled ends at once", s == 200 and (out.get("was"),
              out["job"]["status"]) == ("held", "cancelled"), str(out))
        ears = job(p, kind="transcribe", gen={"model": "ears"}, check=True,
                   input={"audio": str(take), "fields": {"language": "no"}})
        check("another marked transcription is held", reach(url, ears, "held"), ears)
        s, out = p("POST", f"/api/jobs/{ears}/send", {})
        check("...sent as it was built: nothing changed - no text model's number put back",
              s == 200 and (out.get("tuned"), out.get("changed")) == (False, []), str(out))
        check("...and it runs, and ends done", reach(url, ears, "done", timeout=20), ears)
    finally:
        S.stop(httpd)
    return p


# ---------------------------------------------------- every text parameter

def text_parameters(tmp: Path) -> None:
    """Every text parameter, over real HTTP: a job may name every
    parameter the contract's `gen` names, and its model is handed each one
    exactly as it was named; one the contract does not name is refused
    before any job is made."""
    folder = tmp / "parameters"
    folder.mkdir()
    gates = Gates()
    httpd = S.serve(free_port(), backend=gates, log=lambda s: None, **FAST)
    p = Probe(httpd.app.url)
    try:
        p("POST", "/api/workspace", {"path": str(folder)})
        named = every_parameter()
        s, out = p("POST", "/api/jobs", {"kind": "generate", "input": {"text": "every one"},
                                         "gen": {"model": MODEL, **named}})
        check("a job may name every parameter the contract names for its gen",
              s == 200, f"{s} {out}")
        jid = out.get("job_id")
        check("...and it runs", reach(httpd.app.url, jid, "done"), str(jid))
        sent = gates.params.get(jid) or {}
        changed = sorted(k for k, v in named.items() if not same(sent.get(k), v))
        check("...and its model is handed every one exactly as it was named", not changed,
              f"lost or changed on the way: {changed}")
        s, out = p("POST", "/api/jobs", {"kind": "generate", "input": {"text": "misspelt"},
                                         "gen": {"model": MODEL, "temprature": 0.7}})
        check("a parameter the contract does not name is refused bad_request, in its words",
              (s, out.get("reason"), out.get("error"))
              == (400, "bad_request", '$.gen: unexpected field "temprature"'), f"{s} {out}")
        s, out = p("GET", "/api/jobs")
        check("...and no job is made of it", s == 200 and [j["id"] for j in out["jobs"]] == [jid],
              str(out))
    finally:
        S.stop(httpd)


# ---------------------------------------------------- the kept configuration

def config(tmp: Path) -> Probe:
    """A folder's kept configuration, over real HTTP: kept whole under the
    hash of its bytes, the same kept twice one file, read back checked
    against its name - and every refusal the contract declares, provoked."""
    httpd = S.serve(free_port(), backend=None, log=lambda s: None, **FAST)
    p = Probe(httpd.app.url)
    folder = tmp / "config-folder"
    folder.mkdir()
    where = folder / "client" / "configs"
    layout = {"v": 1, "layout": {"slots": {"left": ["workspace"], "middle": ["queue"],
                                           "right": ["log", "customize"]},
                                 "shed": ["workspace", "queue", "log"]}}
    try:
        s, out = p("GET", "/api/workspace/config")
        check("nothing bound: no folder to read a configuration from",
              (s, out.get("reason")) == (409, "no_workspace"), json.dumps(out))
        s, out = p("POST", "/api/workspace/config", {"config": layout})
        check("...nor one to keep it in", (s, out.get("reason")) == (409, "no_workspace"), json.dumps(out))
        p("POST", "/api/workspace", {"path": str(folder)})
        s, out = p("GET", "/api/workspace/config")
        check("a folder that has kept nothing: null, never an empty configuration",
              (s, out) == (200, {"config": None, "sha256": None}), json.dumps(out))
        s, out = p("POST", "/api/workspace/config", {"config": layout})
        data = json.dumps(layout, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        sha = hashlib.sha256(data).hexdigest()
        check("kept: named by the sha256 of its canonical bytes, and new",
              (s, out) == (200, {"sha256": sha, "new": True}), json.dumps(out))
        check("...its file holds exactly those bytes", (where / f"{sha}.json").read_bytes() == data)
        check("...and current.json names it",
              json.loads((where / "current.json").read_text(encoding="utf-8")) == {"sha256": sha})
        s, out = p("GET", "/api/workspace/config")
        check("read back: the configuration, and its name", (s, out) == (200, {"config": layout, "sha256": sha}),
              json.dumps(out))
        reordered = {"layout": {"shed": layout["layout"]["shed"], "slots": layout["layout"]["slots"]}, "v": 1}
        s, out = p("POST", "/api/workspace/config", {"config": reordered})
        check("the same configuration, its keys in another order: the same name, and no new file",
              (s, out) == (200, {"sha256": sha, "new": False})
              and sorted(x.name for x in where.iterdir()) == sorted([f"{sha}.json", "current.json"]),
              json.dumps(out))
        moved = {**layout, "layout": {**layout["layout"], "shed": ["queue", "workspace", "log"]}}
        s, out = p("POST", "/api/workspace/config", {"config": moved})
        sha2 = out.get("sha256")
        check("another: its own file, and the one in use now",
              s == 200 and out.get("new") is True and sha2 != sha
              and p("GET", "/api/workspace/config")[1] == {"config": moved, "sha256": sha2}, json.dumps(out))
        check("...and the first is still kept", (where / f"{sha}.json").is_file())
        big = {"v": 1, "blob": "x" * 70000}
        size = len(json.dumps(big, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        s, out = p("POST", "/api/workspace/config", {"config": big})
        check("too large: refused, saying the limit and the size",
              (s, out.get("reason")) == (400, "too_large")
              and out.get("error") == f"a configuration is at most 65536 bytes as JSON; this one is {size}",
              json.dumps(out)[:200])
        (where / f"{sha2}.json").write_bytes((where / f"{sha2}.json").read_bytes() + b" ")
        s, out = p("GET", "/api/workspace/config")
        check("a kept file changed since: said so, never handed out as the one kept",
              (s, out.get("reason")) == (503, "config_unreadable")
              and out.get("error") == f"configs/{sha2}.json does not hash to its name: it was changed "
                                      "after it was kept", json.dumps(out))
        (where / f"{sha2}.json").unlink()
        s, out = p("GET", "/api/workspace/config")
        check("...the one in use gone: said so",
              (s, out.get("reason")) == (503, "config_unreadable")
              and out.get("error") == f"configs/{sha2}.json, the one in use, is not there", json.dumps(out))
        (where / "current.json").write_text("not json", encoding="utf-8")
        s, out = p("GET", "/api/workspace/config")
        check("...a current.json that names nothing: said so",
              (s, out.get("reason")) == (503, "config_unreadable")
              and out.get("error") == "configs/current.json does not name a configuration", json.dumps(out))
        (where / "current.json").write_text(json.dumps({"sha256": "../current"}), encoding="utf-8")
        s, out = p("GET", "/api/workspace/config")
        check("...nor one that names something not a hash: never a path out of its folder",
              (s, out.get("reason")) == (503, "config_unreadable")
              and out.get("error") == "configs/current.json does not name a configuration", json.dumps(out))
        # a folder that cannot be written: where its configurations go, a file is
        other = tmp / "config-folder-2"
        (other / "client").mkdir(parents=True)
        (other / "client" / "configs").write_text("in the way", encoding="utf-8")
        p("POST", "/api/workspace", {"path": str(other)})
        s, out = p("POST", "/api/workspace/config", {"config": layout})
        check("a folder that cannot be written: 503, in the system's own words",
              (s, out.get("reason")) == (503, "config_unwritable")
              and out.get("error", "").startswith("the configuration could not be kept: "), json.dumps(out))
        p("POST", "/api/workspace/unload")
    finally:
        S.stop(httpd)
    return p


# ------------------------------------------------------------------ the models

MIB = 1024 * 1024


def models(tmp: Path) -> Probe:
    """The models routes, against a stand-in runner whose settings name real
    GGUF headers on disk: one fine, one that asks for more context than it was
    trained for, one whose file is gone, and the stand-in, which has none. The
    stand-in is all that ever loads, and no row touches the card."""
    import test_llamacpp as TL                              # the GGUF writer, and its guards
    root = tmp / "models"
    root.mkdir()
    for name in ("fine.gguf", "wide.gguf"):
        TL.build_gguf(root, name=name, ctx=32768, tensor_elems=(MIB,), bits_per_weight=4.5)
    settings = tmp / "models.toml"
    settings.write_text(
        f'[runner]\nbackend = "mock"\nmodels_dir = "{root.as_posix()}"\n'
        '[mock]\nload_seconds = 1.5\ntokens_per_second = 2000.0\n'
        '[models.fine]\nfile = "fine.gguf"\nctx = 8192\n'
        '[models.wide]\nfile = "wide.gguf"\nctx = 65536\n'
        '[models.gone]\nfile = "gone.gguf"\n'
        '[models.demo]\nlabel = "the stand-in"\n', encoding="utf-8")
    backend = S.build_backend("mock", settings, log=lambda s: None, vram=lambda: None)
    httpd = S.serve(free_port(), backend=backend, log=lambda s: None, **FAST)
    bare = S.serve(free_port(), backend=None, log=lambda s: None, **FAST)
    url, p, q = httpd.app.url, Probe(httpd.app.url), Probe(bare.app.url)
    folder = tmp / "models-folder"
    (folder / "runner").mkdir(parents=True)
    try:
        s, out = p("GET", "/api/models")
        by = {m["key"]: m for m in out["models"]}
        check("every model the settings name, by key, and the runner's own settings",
              list(by) == ["demo", "fine", "gone", "wide"] and out["runner"] == "mock"
              and Path(out["models_dir"]) == root, str(list(by)))
        check("nothing bound: no load history - null, never an empty record",
              out["loads_from"] is None and all(m["loads"] is None for m in out["models"]))
        check("...and no statistics (S11b, PLAN-4 D12): null, never empty ones",
              all(m["stats"] is None for m in out["models"]), str([m["stats"] for m in
                                                                    out["models"]]))
        fine = by["fine"]
        check("a file that is there: its size, its disk, and what its header says",
              fine["file"]["present"] and fine["file"]["size"] == (root / "fine.gguf").stat().st_size
              and fine["file"]["disk"] == (os.path.splitdrive(str(root.resolve()))[0]
                                           if sys.platform == "win32" else fine["file"]["disk"])
              and fine["file"]["disk"]
              and fine["header"] == {"architecture": "llama", "trained_context": 32768,
                                     "chat_template": True, "bits_per_weight": 4.5}
              and fine["problems"] == [], json.dumps(fine))
        check("...more context asked than it was trained for: said as a load would say it",
              len(by["wide"]["problems"]) == 1 and "ctx 65,536" in by["wide"]["problems"][0]
              and "trained for 32,768" in by["wide"]["problems"][0], str(by["wide"]["problems"]))
        check("a file that is gone: said, and no header",
              by["gone"]["file"]["present"] is False and by["gone"]["header"] is None
              and by["gone"]["problems"] == [f"the file is not there: {root / 'gone.gguf'}"],
              json.dumps(by["gone"]))
        check("the stand-in: no file, and nothing wrong with it",
              by["demo"]["file"] is None and by["demo"]["problems"] == []
              and by["demo"]["label"] == "the stand-in", json.dumps(by["demo"]))

        # the folder's record: four cold loads, the last far above those before it
        record = {"version": 1, "buckets": {
            "fine::cold": {"model": "fine", "cold": True, "samples": [10.0, 11.0, 10.5, 40.0],
                           "n": 4, "last": 40.0, "last_at": 1790000000.0},
            "fine::warm": {"model": "fine", "cold": False, "samples": [2.0, 2.2],
                           "n": 2, "last": 2.2, "last_at": 1790000100.0}}}
        (folder / "runner" / "model_loads.json").write_text(json.dumps(record), encoding="utf-8")
        # ...and its statistics (S11b): three loads of `fine`, as the runner keeps them
        from runner import stats as ST
        kept = ST.ModelStats(folder / "runner" / ST.FILE)
        for i, seconds in enumerate((4.0, 6.0, 5.0)):
            kept.note("fine", "load_s", seconds, at=1790000000.0 + i)
        p("POST", "/api/workspace", {"path": str(folder)})
        s, out = p("GET", "/api/models")
        by = {m["key"]: m for m in out["models"]}
        check("bound: each model's statistics, as the folder keeps them - over all time, and "
              "of the samples kept the median and the 90th percentile; none measured is none",
              by["fine"]["stats"] == {"load_s": {"count": 3, "mean": 5.0, "least": 4.0,
                                                 "most": 6.0, "last": 5.0,
                                                 "last_at": 1790000002.0, "median": 5.0,
                                                 "p90": 6.0, "kept": 3}}
              and by["demo"]["stats"] == {}, json.dumps([by["fine"]["stats"],
                                                         by["demo"]["stats"]]))
        check("bound: the history is the folder's own record",
              out["loads_from"] is not None
              and Path(out["loads_from"]).samefile(folder / "runner" / "model_loads.json"),
              str(out["loads_from"]))
        check("...cold: how many, the normal (the median), the last - slow against the normal "
              "BEFORE it", by["fine"]["loads"]["cold"] == {"runs": 4, "normal": 10.75, "last": 40.0,
                                                           "last_at": 1790000000.0, "slow": True},
              json.dumps(by["fine"]["loads"]))
        check("...warm: under three loads there is no normal yet, and so nothing is slow",
              by["fine"]["loads"]["warm"] == {"runs": 2, "normal": None, "last": 2.2,
                                              "last_at": 1790000100.0, "slow": False})
        check("...a model never loaded has no history, which is not a crash",
              by["wide"]["loads"]["cold"] == {"runs": 0, "normal": None, "last": None,
                                              "last_at": None, "slow": False})

        size = (root / "fine.gguf").stat().st_size
        s, out = p("POST", "/api/models/fine/read-speed")
        m = out.get("measure") or {}
        check("a measure: one stretch, a third of the way in on a MiB boundary, to the end at most",
              s == 200 and m["offset"] == (size // 3) // MIB * MIB
              and m["bytes"] == min(64 * MIB, size - m["offset"]) and m["seconds"] > 0,
              json.dumps(out))
        past = True if sys.platform == "win32" else None
        check("...past the cache where the platform allows it - and the sentence says which",
              m.get("past_cache") is past
              and ("past the operating system's cache" in m.get("said", "")) is (past is True),
              json.dumps(m))
        check("...a stretch this small says it is too little to say much",
              "too little to say much about the disk" in m.get("said", ""), m.get("said"))
        s, listed = p("GET", "/api/models")
        check("the list keeps the last measure", s == 200 and {x["key"]: x for x in listed["models"]}
              ["fine"]["read_speed"] == m)

        s, out = p("POST", "/api/models/nope/read-speed")
        check("a model the settings do not name: 404, naming the ones they do",
              (s, out.get("reason")) == (404, "no_model")
              and out.get("error") == "no model 'nope' here; the runner knows demo, fine, "
                                      "gone, wide", json.dumps(out))
        s, out = p("POST", "/api/models/demo/read-speed")
        check("the stand-in has no file to read",
              (s, out.get("reason")) == (409, "no_file")
              and out["error"] == "demo is a stand-in: it has no file to read", json.dumps(out))
        s, out = p("POST", "/api/models/gone/read-speed")
        check("a file that is gone is not measured",
              (s, out.get("reason")) == (409, "no_file")
              and out["error"] == f"gone's file is not there: {root / 'gone.gguf'}", json.dumps(out))
        backend.manager._measuring.acquire()
        try:
            s, out = p("POST", "/api/models/fine/read-speed")
        finally:
            backend.manager._measuring.release()
        check("one measure at a time", (s, out.get("reason")) == (409, "busy")
              and "already running" in out["error"], json.dumps(out))

        jid = job(p, gen={"model": "fine"}, input={"prompt": "a haiku about rain"})
        loading = wait_for(lambda: (http(url, "GET", "/api/health")[1].get("runner") or {})
                           .get("loading") == "fine", 5.0)
        s, out = p("POST", "/api/models/fine/read-speed")
        check("a model loading: refused - the measure would measure the load as well",
              loading is not None and (s, out.get("reason")) == (409, "busy")
              and "loading" in out["error"], f"loading seen: {loading}; {json.dumps(out)}")
        reach(url, jid, "done", "failed", timeout=10.0)
        s, out = p("GET", f"/api/jobs/{jid}/result")
        cand = (out.get("candidates") or [{}])[0]
        check("a runner's answer carries its load, its usage and its timings, as declared",
              s == 200 and ids.matches("actor", str(cand.get("load")))
              and (cand.get("usage") or {}).get("completion_tokens") == cand.get("tokens")
              and (cand.get("timings") or {}).get("predicted_per_second", 0) > 0, json.dumps(cand))
        s, out = p("GET", "/api/models")
        check("the model that is up says so",
              {x["key"]: x["loaded"] for x in out["models"]}
              == {"demo": False, "fine": True, "gone": False, "wide": False},
              json.dumps({x["key"]: x["loaded"] for x in out["models"]}))

        target = root / "fine.gguf"
        if sys.platform == "win32":
            import msvcrt
            with open(target, "r+b") as fh:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, size)
                try:
                    s, out = p("POST", "/api/models/fine/read-speed")
                finally:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, size)
        else:
            target.chmod(0)
            try:
                s, out = p("POST", "/api/models/fine/read-speed")
            finally:
                target.chmod(0o644)
        check("a file that cannot be read: said, in the system's own words",
              (s, out.get("reason")) == (409, "unreadable") and str(target) in out["error"],
              json.dumps(out))

        s, out = q("GET", "/api/models")
        check("no runner behind the server: no models",
              (s, out) == (200, {"runner": None, "models_dir": None, "loads_from": None,
                                 "slow_answer": None, "models": []}), json.dumps(out))
        s, out = q("POST", "/api/models/fine/read-speed")
        check("...and nothing to measure", (s, out.get("reason")) == (409, "no_runner"),
              json.dumps(out))
        p("POST", "/api/workspace/unload")
    finally:
        S.stop(httpd)
        S.stop(bare)
    p.seen |= q.seen
    return p


# -------------------------------------------------------------- the pipeline

#: health's pipeline before any was opened (the contract's words) - CHANGED ON
#: PURPOSE (S7): a pipeline says `up`, the models on the card, too
NO_PIPELINE = {"id": None, "state": "closed", "why": None, "since": None, "label": None,
               "models": [], "up": [], "class": None, "keep_s": None, "opened_by": None}


def loaded(p: Probe):
    """The model a server's runner has up, asked AROUND the probe: a wait asks
    as many times as the machine is slow, and the suite's count of checks must
    not move with it."""
    return ((http(p.url, "GET", "/api/health")[1] or {}).get("runner") or {}).get("loaded")


def pipeline(tmp: Path) -> Probe:
    """The pipeline's three routes over real HTTP (PLAN-4 D6), against a
    stand-in runner whose model can be made to refuse to stop: every answer
    they declare, and health's `pipeline` as it moves. The stand-in's card is
    this process's own: no row touches a card."""
    from runner.adapter import RunnerBackend
    from runner.base import BackendError
    from runner.config import Config
    from runner.manager import ModelManager
    from runner.mock import MockBackend
    settings = tmp / "pipeline.toml"
    settings.write_text('[runner]\nbackend = "mock"\n[mock]\nload_seconds = 0.0\n'
                        'unload_seconds = 0.0\ntokens_per_second = 2000.0\n'
                        '[models.alpha]\nlabel = "Alpha"\n[models.beta]\nlabel = "Beta"\n',
                        encoding="utf-8")
    cfg = Config.load(settings)
    stubborn = {"on": False}

    class Stubborn(MockBackend):
        def stop(self_):
            if stubborn["on"]:
                raise BackendError("it would not stop")
            super().stop()
    manager = ModelManager(cfg, None, vram=lambda: None, backend_factory=lambda key, port:
                           Stubborn(key, cfg.model(key).label, cfg.mock))
    backend = RunnerBackend(manager, log=lambda s: None, idle_poll_s=3600)
    httpd = S.serve(free_port(), backend=backend, log=lambda s: None, **FAST)
    bare = S.serve(free_port(), backend=None, log=lambda s: None, **FAST)
    p, q = Probe(httpd.app.url), Probe(bare.app.url)

    def piped():
        return p("GET", "/api/health")[1]["pipeline"]
    try:
        check("health before any pipeline: closed, and every field null but its state and "
              "its models", piped() == NO_PIPELINE, json.dumps(piped()))
        check("health with no runner behind the server: no pipeline at all",
              q("GET", "/api/health")[1]["pipeline"] is None)
        s, out = p("POST", "/api/pipeline/open", {"models": ["alpha"],
                                                  "label": "plotting session"})
        opened = out.get("pipeline") or {}
        check("an open answers at once, the application's, with its label, its models and "
              "the settings' keep_s",
              s == 200 and ids.matches("pipeline", opened.get("id"))
              and (opened["opened_by"], opened["label"], opened["models"], opened["keep_s"],
                   opened["class"]) == ("application", "plotting session", ["alpha"], 120.0,
                                        "responsive")
              and opened["state"] in ("waiting", "holding"), json.dumps(out))
        pid = opened.get("id")
        wait_for(lambda: loaded(p) == "alpha")
        s, h = p("GET", "/api/health")
        check("health carries it: holding the card, since when, and its model up",
              h["pipeline"]["id"] == pid and h["pipeline"]["state"] == "holding"
              and isinstance(h["pipeline"]["since"], float) and h["runner"]["loaded"] == "alpha",
              json.dumps([h["pipeline"], h["runner"]["loaded"]]))
        # CHANGED ON PURPOSE (S7): two models are a pipeline side by side;
        # too_many_models is past the runner's max_loads (1 here, for the row)
        manager.cfg.runner["max_loads"] = 1
        for body, reason in (({"class": "urgent"}, "bad_request"),
                             ({"models": ["no-such-model"]}, "unknown_model"),
                             ({"models": ["alpha", "beta"]}, "too_many_models"),
                             ({"keep_s": -1}, "bad_keep"),
                             # S18: live work is now - it never waits for room
                             ({"if_no_room": "wait"}, "live_is_now"),
                             ({"class": "responsive", "if_no_room": "wait"}, "live_is_now")):
            s, out = p("POST", "/api/pipeline/open", body)
            check(f"an open refused {reason}: 400, and the open one untouched",
                  (s, out.get("reason")) == (400, reason) and piped()["id"] == pid, str(out))
        live = next(ex for ex in ROUTES["server.pipeline.open"]["examples"]
                    if ex["status"] == 400 and ex["response"]["reason"] == "live_is_now")
        s, out = p("POST", "/api/pipeline/open", live["request"])
        check(f"the contract's example '{live['name']}' is answered word for word",
              (s, out) == (400, live["response"]), f"{s} {out}")
        manager.cfg.runner["max_loads"] = 8
        s, out = p("POST", "/api/pipeline/open", {})
        check("a second open while one is open is 409 already_open, naming the one open",
              (s, out.get("reason")) == (409, "already_open") and pid in out["error"], str(out))
        s, out = p("POST", "/api/pipeline/renew", {"pipeline": pid})
        check("a renewal answers the pipeline as it stands",
              s == 200 and (out["pipeline"]["id"], out["pipeline"]["state"]) == (pid, "holding"),
              str(out))
        for route in ("renew", "close"):
            s, out = p("POST", f"/api/pipeline/{route}", {"pipeline": "pipe-w5r8t2hh"})
            check(f"a {route} of an id this server never opened is 409 not_open, saying so",
                  (s, out) == (409, {"error": "no pipeline pipe-w5r8t2hh is open here",
                                     "reason": "not_open"}), str(out))
        s, out = p("POST", "/api/pipeline/close", {"pipeline": pid})
        h = p("GET", "/api/health")[1]
        check("a close answers once every model is off: the pipeline closed, saying why",
              s == 200 and (out["pipeline"]["id"], out["pipeline"]["state"],
                            out["pipeline"]["why"]) == (pid, "closed", "closed by the application")
              and h["runner"]["loaded"] is None and h["pipeline"] == out["pipeline"],
              json.dumps([out, h["runner"]["loaded"]]))
        for route in ("renew", "close"):
            s, out = p("POST", f"/api/pipeline/{route}", {"pipeline": pid})
            check(f"a {route} after the close is 409 not_open, saying it was closed",
                  (s, out) == (409, {"error": f"{pid} is not open: closed by the application",
                                     "reason": "not_open"}), str(out))
        s, out = p("POST", "/api/pipeline/open", {"models": ["beta"], "keep_s": 0})
        again = (out.get("pipeline") or {}).get("id")
        wait_for(lambda: loaded(p) == "beta")
        stubborn["on"] = True
        s, out = p("POST", "/api/pipeline/close", {"pipeline": again})
        h = p("GET", "/api/health")[1]
        check("a close whose model would not come off is 409 unload_failed: the pipeline "
              "stays open, and holds the card", (s, out.get("reason")) == (409, "unload_failed")
              and "beta would not come off" in out.get("error", "")
              and (h["pipeline"]["id"], h["pipeline"]["state"]) == (again, "holding"),
              json.dumps([out, h["pipeline"]]))
        stubborn["on"] = False
        for route, body in (("open", {}), ("renew", {"pipeline": pid}),
                            ("close", {"pipeline": pid})):
            s, out = q("POST", f"/api/pipeline/{route}", body)
            check(f"a {route} with no runner behind the server is 409 no_runner",
                  (s, out.get("reason")) == (409, "no_runner"), str(out))
        httpd.app.backend.stopping("the server is stopping")
        s, out = p("POST", "/api/pipeline/open", {})
        check("no pipeline opens while the server stops: 409 stopping",
              (s, out.get("reason")) == (409, "stopping"), str(out))
    finally:
        S.stop(httpd)
        S.stop(bare)
    p.seen |= q.seen
    # AN APPLICATION STARTS READY: its settings name the session's models, and
    # the server opens that session as it starts - nothing asked of it
    ready = tmp / "ready.toml"
    ready.write_text(settings.read_text(encoding="utf-8").replace(
        'backend = "mock"', 'backend = "mock"\nsession_models = ["beta"]'), encoding="utf-8")
    started = S.serve(free_port(), backend=S.build_backend("mock", ready, log=lambda s: None,
                                                  vram=lambda: None),
                      log=lambda s: None, **FAST)
    r = Probe(started.app.url)
    try:
        wait_for(lambda: loaded(r) == "beta")
        h = r("GET", "/api/health")[1]
        session = h["pipeline"]
        check("a server whose settings name a session opens it as it starts, and its model "
              "is up with nothing asked: held until it is closed",
              (session["opened_by"], session["state"], session["models"], session["keep_s"])
              == ("start", "holding", ["beta"], 0.0) and h["runner"]["loaded"] == "beta",
              json.dumps([session, h["runner"]["loaded"]]))
        s, out = r("POST", "/api/pipeline/open", {})
        check("...and it is this server's pipeline: another open is already_open",
              (s, out.get("reason")) == (409, "already_open") and session["id"] in out["error"]
              and "the server as it started" in out["error"], str(out))
    finally:
        S.stop(started)
    p.seen |= r.seen
    return p


# ---------------------------------------------------------- side by side (S7)

def side_by_side(tmp: Path) -> Probe:
    """Models side by side over real HTTP (PLAN-4 D7): a pipeline opened
    with two, answered once both are up (`?wait_s`); a model loaded and kept
    with `load`, and taken off with `unload` while the rest stay; every
    answer the two model routes declare, and the open's new refusals. The
    stand-in's card is this process's own: no row touches a card."""
    from runner.adapter import RunnerBackend
    from runner.base import BackendError
    from runner.config import Config
    from runner.manager import ModelManager
    from runner.mock import MockBackend
    settings = tmp / "side.toml"
    settings.write_text('[runner]\nbackend = "mock"\n[mock]\nload_seconds = 0.2\n'
                        'unload_seconds = 0.0\ntokens_per_second = 2000.0\n'
                        '[models.alpha]\nlabel = "Alpha"\n[models.beta]\nlabel = "Beta"\n'
                        '[models.gamma]\nlabel = "Gamma"\n[models.delta]\nlabel = "Delta"\n',
                        encoding="utf-8")
    cfg = Config.load(settings)
    stubborn = {"on": False}

    class Stubborn(MockBackend):
        def stop(self_):
            if stubborn["on"]:
                raise BackendError("it would not stop")
            super().stop()
    manager = ModelManager(cfg, None, vram=lambda: None, backend_factory=lambda key, port:
                           Stubborn(key, cfg.model(key).label, cfg.mock))
    backend = RunnerBackend(manager, log=lambda s: None, idle_poll_s=3600)
    httpd = S.serve(free_port(), backend=backend, log=lambda s: None, **FAST)
    bare = S.serve(free_port(), backend=None, log=lambda s: None, **FAST)
    p, q = Probe(httpd.app.url), Probe(bare.app.url)

    def health():
        return p("GET", "/api/health")[1]
    try:
        s, out = p("POST", "/api/models/alpha/load")
        check("a load with no pipeline of the application's open is 409 no_pipeline",
              (s, out.get("reason")) == (409, "no_pipeline")
              and "no pipeline is open to keep alpha in" in out.get("error", ""), str(out))
        for bad in ("90", "soon"):
            s, out = p("POST", f"/api/pipeline/open?wait_s={bad}", {"models": ["alpha"]})
            check(f"an open waiting {bad!r} is 400 bad_wait, and opens nothing",
                  (s, out.get("reason")) == (400, "bad_wait") and bad in out.get("error", "")
                  and health()["pipeline"]["state"] == "closed", str(out))
        s, out = p("POST", "/api/pipeline/open", {"models": ["alpha", "beta", "alpha"]})
        check("an open naming a model twice is 400 repeated_model",
              (s, out.get("reason")) == (400, "repeated_model")
              and "alpha is named twice" in out.get("error", ""), str(out))
        manager.cfg.runner["max_loads"] = 1
        s, out = p("POST", "/api/pipeline/open", {"models": ["alpha", "beta"]})
        manager.cfg.runner["max_loads"] = 8
        check("an open naming more models than the runner keeps at once is 400 "
              "too_many_models", (s, out.get("reason")) == (400, "too_many_models")
              and "at most 1" in out.get("error", ""), str(out))
        t0 = time.monotonic()
        s, out = p("POST", "/api/pipeline/open?wait_s=8", {"models": ["alpha", "beta"],
                                                             "keep_s": 0})
        took = time.monotonic() - t0
        opened = out.get("pipeline") or {}
        check("an open that waits is answered once both its models answer - side by side, "
              "long before its bound", s == 200 and (opened.get("state"), opened.get("models"),
                                                     opened.get("up"))
              == ("holding", ["alpha", "beta"], ["alpha", "beta"]) and took < 6.0,
              json.dumps([out, round(took, 2)]))
        h = health()
        check("health: the card held with no job running, and both models up",
              h["card"]["holding"] is True and h["queue"]["running"] == 0
              and h["pipeline"]["up"] == ["alpha", "beta"], json.dumps([h["card"], h["pipeline"]]))
        for bad in ("61", "1e1"):
            s, out = p("POST", f"/api/models/gamma/load?wait_s={bad}")
            check(f"a load waiting {bad!r} is 400 bad_wait, and loads nothing",
                  (s, out.get("reason")) == (400, "bad_wait")
                  and health()["pipeline"]["models"] == ["alpha", "beta"], str(out))
        s, out = p("POST", "/api/models/gamma/load?wait_s=8")
        model = out.get("model") or {}
        check("a load that waits is answered the moment the model is up: kept by the "
              "pipeline, beside the others", s == 200
              and (model.get("key"), model.get("state"), model.get("why")) == ("gamma", "up", None)
              and ids.matches("actor", model.get("load") or "")
              and (out["pipeline"]["models"], out["pipeline"]["up"])
              == (["alpha", "beta", "gamma"], ["alpha", "beta", "gamma"]), json.dumps(out))
        s, again = p("POST", "/api/models/gamma/load")
        check("a load of a model already up answers at once, the same load",
              s == 200 and again["model"] == model, json.dumps(again))
        s, out = p("POST", "/api/models/omega/load")
        check("a load of a model the settings do not name is 404 no_model",
              (s, out.get("reason")) == (404, "no_model"), str(out))
        manager.cfg.runner["max_loads"] = 3
        s, out = p("POST", "/api/models/delta/load")
        manager.cfg.runner["max_loads"] = 8
        check("a load past as many models as the runner keeps is 409 too_many_models",
              (s, out.get("reason")) == (409, "too_many_models"), str(out))
        s, out = p("POST", "/api/models/alpha/unload")
        h = health()
        check("an unload takes that model off and leaves the rest, and the card",
              (s, out) == (200, {"model": {"key": "alpha", "state": "off", "why": None,
                                           "load": None}})
              and (h["pipeline"]["models"], h["pipeline"]["up"], h["card"]["holding"])
              == (["beta", "gamma"], ["beta", "gamma"], True),
              json.dumps([out, h["pipeline"], h["card"]]))
        s, out = p("POST", "/api/models/omega/unload")
        check("an unload of a model the settings do not name is 404 no_model",
              (s, out.get("reason")) == (404, "no_model"), str(out))
        with manager.in_use("beta"):
            s, out = p("POST", "/api/models/beta/unload")
        check("an unload of a model answering a request is 409 in_use, and it stays up",
              (s, out.get("reason")) == (409, "in_use") and "beta" in health()["pipeline"]["up"],
              str(out))
        stubborn["on"] = True
        s, out = p("POST", "/api/models/gamma/unload")
        stubborn["on"] = False
        check("an unload whose stop failed is 409 unload_failed, saying it may still be on "
              "the card", (s, out.get("reason")) == (409, "unload_failed")
              and "may still be on the card" in out.get("error", ""), str(out))
        for route in ("load", "unload"):
            s, out = q("POST", f"/api/models/alpha/{route}")
            check(f"a {route} with no runner behind the server is 409 no_runner",
                  (s, out.get("reason")) == (409, "no_runner"), str(out))
        httpd.app.backend.stopping("the server is stopping")
        s, out = p("POST", "/api/models/alpha/load")
        check("no model loads while the server stops: 409 stopping",
              (s, out.get("reason")) == (409, "stopping"), str(out))
    finally:
        S.stop(httpd)
        S.stop(bare)
    p.seen |= q.seen
    return p


#: a whisper model file's header as whisper.cpp reads it: the ggml magic, then
#: large v3's eleven numbers (runner/ggml.py names them)
GGML_LARGE_V3 = (0x67676D6C, 51866, 1500, 1280, 20, 32, 448, 1280, 20, 32, 128, 1)


def where_each_stands(tmp: Path) -> Probe:
    """Each model where it stands, in `GET /api/models` (PLAN-4 S14b, for the
    Models widget): its `state` - the word a load of it answers - and whether
    the open pipeline keeps it (`pinned`), or a job loaded it on demand; and
    a whisper model's file's header, the ggml header, read as whisper.cpp
    reads it. A stand-in runner whose loads can be held or refused: no row
    touches a card."""
    import struct
    import threading
    from runner.adapter import RunnerBackend
    from runner.base import BackendError
    from runner.config import Config
    from runner.manager import ModelManager
    from runner.mock import MockBackend
    root = tmp / "where-models"
    root.mkdir()
    (root / "ears.bin").write_bytes(struct.pack("<I11i", *GGML_LARGE_V3))
    (root / "wrong.bin").write_bytes(b"GGUF" + bytes(60))
    settings = tmp / "where.toml"
    settings.write_text(f'[runner]\nbackend = "mock"\nmodels_dir = "{root.as_posix()}"\n'
                        '[mock]\nload_seconds = 0.0\nunload_seconds = 0.0\n'
                        'tokens_per_second = 2000.0\n'
                        '[models.alpha]\nlabel = "Alpha"\n[models.beta]\nlabel = "Beta"\n'
                        '[models.gamma]\nlabel = "Gamma"\n'
                        '[models.ears]\nengine = "whisper"\nfile = "ears.bin"\n'
                        '[models.wrong]\nengine = "whisper"\nfile = "wrong.bin"\n',
                        encoding="utf-8")
    cfg = Config.load(settings)
    gate, held, refused = threading.Event(), {"beta"}, {"gamma"}

    class Where(MockBackend):
        """beta's load waits for the row to let it go on; gamma's is refused"""
        def start(self_):
            if self_.model_key in refused:
                raise BackendError(f"{self_.model_key} would not start")
            if self_.model_key in held:
                gate.wait(10)
            super().start()
    manager = ModelManager(cfg, None, vram=lambda: None, backend_factory=lambda key, port:
                           Where(key, cfg.model(key).label, cfg.mock))
    backend = RunnerBackend(manager, log=lambda s: None, idle_poll_s=3600)
    httpd = S.serve(free_port(), backend=backend, log=lambda s: None, **FAST)
    url, p = httpd.app.url, Probe(httpd.app.url)
    folder = tmp / "where-folder"
    folder.mkdir()

    def listed():
        s, out = p("GET", "/api/models")
        return {m["key"]: m for m in out.get("models") or []}

    def stands(by):
        return {k: (m["state"]["state"], m["pinned"]) for k, m in by.items()}
    try:
        by = listed()
        check("nothing open, nothing up: every model off, and none pinned",
              stands(by) == {k: ("off", False) for k in ("alpha", "beta", "ears", "gamma",
                                                          "wrong")}
              and all(m["state"] == {"key": k, "state": "off", "why": None, "load": None}
                      for k, m in by.items()), json.dumps(stands(by)))
        check("a whisper model's header is the ggml header: its eleven numbers under "
              "whisper.cpp's names, and its shape in words",
              by["ears"]["header"] == {"shape": "large v3", "n_vocab": 51866,
                                       "n_audio_ctx": 1500, "n_audio_state": 1280,
                                       "n_audio_head": 20, "n_audio_layer": 32,
                                       "n_text_ctx": 448, "n_text_state": 1280,
                                       "n_text_head": 20, "n_text_layer": 32, "n_mels": 128,
                                       "ftype": 1} and by["ears"]["problems"] == [],
              json.dumps(by["ears"]))
        check("...a file that is not a whisper model: no header, and said in the whisper "
              "reader's words", by["wrong"]["header"] is None
              and by["wrong"]["problems"] == ["wrong.bin is not a whisper model: it does not "
                                              "start with the ggml magic 0x67676d6c (its first "
                                              "bytes are b'GGUF')"],
              json.dumps(by["wrong"]))

        s, opened = p("POST", "/api/pipeline/open?wait_s=8", {"models": ["alpha"], "keep_s": 0})
        s, kept = p("POST", "/api/models/alpha/load")
        by = listed()
        check("a model the pipeline keeps, up: pinned, and its state the word its load answers",
              stands(by)["alpha"] == ("up", True) and by["alpha"]["state"] == kept.get("model")
              and by["alpha"]["loaded"] is True
              and all(stands(by)[k] == ("off", False) for k in ("beta", "gamma")),
              json.dumps([stands(by), kept]))

        s, out = p("POST", "/api/models/gamma/load?wait_s=8")
        failed = out.get("model") or {}
        by = listed()
        check("a model whose load failed: failed, with why - the load's own answer - and "
              "still pinned: the pipeline keeps it", failed.get("state") == "failed"
              and "gamma would not start" in (failed.get("why") or "")
              and by["gamma"]["state"] == failed and by["gamma"]["pinned"] is True
              and by["gamma"]["loaded"] is False, json.dumps([by["gamma"], out]))

        p("POST", "/api/workspace", {"path": str(folder)})
        jid = job(p, gen={"model": "beta"}, input={"prompt": "a haiku about rain"})
        loading = wait_for(lambda: (http(url, "GET", "/api/health")[1].get("runner") or {})
                           .get("loading") == "beta", 5.0)
        by = listed()
        check("a model a job needs, its weights going up: loading, and not pinned - loaded "
              "on demand", loading is not None and stands(by)["beta"] == ("loading", False)
              and by["beta"]["loaded"] is False, json.dumps([loading, by["beta"]]))
        gate.set()
        reach(url, jid, "done", "failed", timeout=10.0)
        s, out = p("GET", f"/api/jobs/{jid}/result")
        ran_on = ((out.get("candidates") or [{}])[0]).get("load")
        by = listed()
        check("...up once it answers: still not pinned, and its load the one the job ran on",
              stands(by)["beta"] == ("up", False) and by["beta"]["state"]["load"] == ran_on
              and ids.matches("actor", str(ran_on)), json.dumps([by["beta"], ran_on]))

        s, out = p("POST", "/api/models/alpha/unload")
        by = listed()
        check("unloaded: off, and no longer pinned", stands(by)["alpha"] == ("off", False),
              json.dumps(stands(by)))
        pinned_before = by["gamma"]["pinned"]
        s, out = p("POST", "/api/pipeline/close",
                   {"pipeline": (opened.get("pipeline") or {}).get("id")})
        by = listed()
        check("the pipeline closed: nothing is pinned, the model it kept whose load failed "
              "with the rest", s == 200 and pinned_before is True
              and not any(m["pinned"] for m in by.values()), json.dumps([s, out, stands(by)]))
        p("POST", "/api/workspace/unload")
    finally:
        gate.set()
        S.stop(httpd)
    return p


# ------------------------------------------------------------- the processes

#: where a server process's port comes from: BELOW every common system's range
#: for passing connections (Windows from 49152, Linux from 32768), and clear of
#: every other suite's range. A port picked by the operating system comes from
#: that range, and between the pick and the child binding it, any connection on
#: the machine could be handed it.
QUIET_PORTS = (29500, 29999)


def free_port() -> int:
    """A port nothing listens on now, of the block of QUIET_PORTS this copy of
    the suite claimed: another copy never hands it out."""
    return quiet_ports(*QUIET_PORTS).take()


def its_ports(tmp: Path) -> None:
    """Every copy of this suite on the machine takes its ports from one range
    - a sweep's copies, a gate beside another agent's - and two that
    bind-checked one free port in the same moment both launched on it. So each
    copy claims a block of the range for as long as it runs
    (`_suites.QuietPorts`, whose rows are the llama.cpp suite's)."""
    first, last = quiet_ports(*QUIET_PORTS).block
    stray = sorted({free_port() for _ in range(50)} - set(range(first, last + 1)))
    check("every port this copy hands out is of the block of its range it claimed",
          not stray, f"{stray[:8]} are not of this copy's block {first}-{last}")


def every_port_its_own(tmp: Path) -> None:
    """Last: every server this suite started listened on a port of this
    copy's block - never on port 0, a port the operating system picks, which
    may be anybody's the moment after (H5's register 5)."""
    first, last = quiet_ports(*QUIET_PORTS).block
    stray = [(host, asked, had) for host, asked, had in LISTENED if not first <= had <= last]
    check("every server this suite started listened on a port of its own block, never one the "
          "operating system picked", bool(LISTENED) and not stray,
          f"{len(stray)} of {len(LISTENED)} were not, e.g. {stray[:4]} (block {first}-{last})")
    bare = [p for p in BARE if not first <= p <= last]
    check("...and so did the plain server it started beside them", bool(BARE) and not bare,
          f"{bare} (block {first}-{last})")


def start(args: list, expect: str, timeout: float = START_S, clock=time.monotonic):
    """Start a child; wait for a stdout line containing *expect*. -> (proc, line)

    Its script by its whole path, so its command line names this copy's
    folder: what `mutate.py` finds a timed-out suite's children by. Within
    START_S: a start that outlasts a few seconds is one on a busy machine,
    not one that failed. A child that ends without saying it is up is said
    at once - in its own words, with its exit code - never waited out to the
    bound."""
    proc = subprocess.Popen([sys.executable, str(HERE / args[0]), *args[1:]],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=HERE,
                            encoding="utf-8")
    lines: queue.Queue = queue.Queue()

    def read():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)                                     # its output has ended
    threading.Thread(target=read, daemon=True).start()
    deadline, said = clock() + timeout, []
    while clock() < deadline:
        try:
            line = lines.get(timeout=0.1)
        except queue.Empty:
            continue
        if line is None:
            break
        said.append(line)
        if expect in line:
            return proc, line.strip()
    proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(START_S)
    raise RuntimeError(f"{args[0]} never printed {expect!r} (its exit code: {proc.returncode}); "
                       f"it said: {''.join(said)[-600:]!r}")


def a_slow_start(tmp: Path) -> None:
    """A start a busy machine makes slow is waited for: a child that says it
    is up only once more than fifteen seconds - this suite's old bound - have
    passed on the clock `start` reads, which then stands still."""
    go, script = tmp / "busy-go", tmp / "busy-start.py"
    script.write_bytes(b"import pathlib, sys, time\n"
                       b"go = pathlib.Path(sys.argv[1])\n"
                       b"while not go.exists():\n"
                       b"    time.sleep(0.02)\n"
                       b"print('up and about', flush=True)\n")
    now = [0.0]

    def slow_clock():
        if now[0] < 16.0:
            now[0] += 1.0
        elif not go.exists():
            go.write_bytes(b"")
        return now[0]
    try:
        child, said = start([str(script), str(go)], "up and about", clock=slow_clock)
        child.wait(START_S)
    except RuntimeError as e:
        said = str(e)
    check("a process that takes more than fifteen seconds to say it is up - a busy machine's "
          "start - is waited for", said == "up and about", said)
    ends = tmp / "busy-ends.py"
    ends.write_bytes(b"print('cannot start: somebody holds the port', flush=True)\n"
                     b"raise SystemExit(2)\n")
    t0 = time.monotonic()
    try:
        start([str(ends)], "listening on")
        said = "it was taken for started"
    except RuntimeError as e:
        said = str(e)
    took = time.monotonic() - t0
    check("...and one that ends without saying it is up is said at once, in its own words and "
          "its exit code - never waited out to the bound",
          "somebody holds the port" in said and "exit code: 2" in said and took < START_S / 2,
          f"{said} - after {took:.1f}s")


def processes(tmp: Path) -> None:
    folder = tmp / "shared"
    folder.mkdir()
    port = free_port()
    child, _ = start(["server.py", "--port", str(port), "--workspace", str(folder),
                      "--backend", "echo"], "listening on")
    other = S.serve(free_port(), log=lambda s: None, **FAST)
    try:
        line = subprocess.list2cmdline(child.args)
        check("a server process this suite starts names this copy's folder on its command "
              "line - what mutate.py finds it by when the suite is timed out", str(HERE) in line,
              line[:200])
        s, out = other.app.bind({"path": str(folder)})
        check("a folder a live server PROCESS has is refused to another server",
              (s, out.get("reason")) == (409, "folder_in_use"), str(out))
        check("...naming that process and where it answers",
              (out.get("holder") or {}).get("pid") == child.pid
              and (out.get("holder") or {}).get("url") == f"http://127.0.0.1:{port}",
              str(out.get("holder")))
        # a child started on a port is recorded by the port it was given: it
        # is refused before it listens, and a start that is not would listen
        # on it - one of this copy's block (H7's register 1: it was given
        # port 0, which the operating system picks)
        cli_port = free_port()
        LISTENED.append(("127.0.0.1", cli_port, cli_port))
        try:
            cli = subprocess.run([sys.executable, "server.py", "--port", str(cli_port),
                                  "--workspace", str(folder)],
                                 cwd=HERE, capture_output=True, encoding="utf-8", timeout=30)
            check("the command line refuses a folder another server has, and says so",
                  cli.returncode == 2 and "bound by another server" in cli.stdout,
                  f"rc={cli.returncode} {cli.stdout.strip()[:200]}")
        except subprocess.TimeoutExpired:
            check("the command line refuses a folder another server has, and says so",
                  False, "it did not refuse: it bound the folder and kept serving")
        killed = time.time()
        kill_and_wait(child)
        freed = wait_for(lambda: other.app.bind({"path": str(folder)})[0] == 200, 10)
        check("killing the server that had the folder lets it go - no lock is left behind",
              freed is not None, "the folder was still held 10s after the kill")
        if freed is not None:
            measured.append(f"server process killed -> its folder bound by another: "
                            f"{time.time() - killed:.2f}s")
    finally:
        S.stop(other)
        if child.poll() is None:
            kill_and_wait(child)


def the_stop(tmp: Path) -> Probe:
    """server.stop (H8, S13b R2): a PROGRAM stops the server over HTTP - the
    answer first, then its last act, its folder put down - and a PAGE never
    can: its own page, and a page it was started to allow, are refused
    `from_a_page`, and it goes on."""
    folder = tmp / "stop-folder"
    folder.mkdir()
    lines: list = []

    class Closing(S.EchoBackend):
        """The echo, telling each close the server makes of its runner."""
        closes = 0

        def close(self):
            Closing.closes += 1
    httpd = S.serve(free_port(), backend=Closing(delay_s=0.001), log=lines.append,
                    allow_origins=(PAGE,), **FAST)
    other = S.serve(free_port(), log=lambda s: None, **FAST)
    p = Probe(httpd.app.url)
    own = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        p("POST", "/api/workspace", {"path": str(folder)})
        for who, origin in (("its own page", own), ("a page it was started to allow", PAGE)):
            s, out = p("POST", "/api/stop", headers={"Origin": origin})
            check(f"a stop asked by {who} is refused: a page never stops the server",
                  (s, out.get("reason")) == (403, "from_a_page"), json.dumps(out))
        s, out = p("GET", "/api/health")
        check("...and the server goes on, its folder bound", s == 200 and out.get("loaded") is True,
              json.dumps(out)[:200])
        s, out = p("POST", "/api/stop")
        check("a program's stop: 202, naming the server that stops",
              (s, out) == (202, {"stopping": True, "actor": httpd.app.actor}), json.dumps(out))
        gone = wait_for(lambda: http(httpd.app.url, "GET", "/api/health")[0] == 0, 30)
        check("...then it stops answering", gone is not None,
              "it still answered 30s after its stop")
        s, out = other.app.bind({"path": str(folder)})
        check("...its folder put down: another server binds it at once", s == 200, str(out))
        check("...its stop said in its log, after the answer",
              any("POST /api/stop -> 202" in x for x in lines)
              and any("stopped: asked over HTTP" in x for x in lines),
              " | ".join(lines[-6:]))
        S.stop(httpd)
        check("...and one stop: stopped again - at the console, by its start's `finally` - "
              "nothing is closed twice", Closing.closes == 1, f"closed {Closing.closes} times")
    finally:
        S.stop(httpd)
        S.stop(other)
    return p


def a_stopped_process(tmp: Path) -> None:
    """A server process stopped over HTTP ends by itself, exit code 0, its
    folder let go - never ended as a process by whoever asked."""
    folder = tmp / "stopped-process"
    folder.mkdir()
    port = free_port()
    child, _ = start(["server.py", "--port", str(port), "--workspace", str(folder),
                      "--backend", "echo"], "listening on")
    other = S.serve(free_port(), log=lambda s: None, **FAST)
    try:
        s, out = http(f"http://127.0.0.1:{port}", "POST", "/api/stop")
        check("a server process asked to stop says it stops", s == 202, json.dumps(out))
        try:
            code = child.wait(60)
        except subprocess.TimeoutExpired:
            code = None
        check("...and ends by itself, exit code 0", code == 0, f"exit {code}")
        s, out = other.app.bind({"path": str(folder)})
        check("...its folder let go", s == 200, str(out))
    finally:
        S.stop(other)
        if child.poll() is None:
            kill_and_wait(child)


def section(name: str, fn, *a):
    """Run one section. One that dies is a FAIL naming where, never a suite
    that ends before its tally - a crashed suite reports no failures only
    because it never reported anything."""
    try:
        return fn(*a)
    except Exception as e:                                  # noqa: BLE001
        tb = traceback.extract_tb(e.__traceback__)
        where = next((f"line {f.lineno} in {f.name}" for f in reversed(tb)
                      if f.filename == __file__), "?")
        check(f"the {name} section ran to its end", False,
              f"{type(e).__name__}: {e} ({where})")
        return None


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    tmp = Path(tempfile.mkdtemp(prefix="server-box-"))
    try:
        probes = [section("routes", routes, tmp), section("echo", echo, tmp),
                  section("config", config, tmp), section("models", models, tmp),
                  section("pipeline", pipeline, tmp),
                  section("models side by side", side_by_side, tmp),
                  section("each model, where it stands", where_each_stands, tmp),
                  section("transcriptions", transcriptions, tmp),
                  section("the request check", request_check, tmp),
                  section("the stop", the_stop, tmp)]
        if all(probes):
            coverage(probes)
        else:
            check("every answer the contract declares for the server, the server gave",
                  False, "not measured: a section before it died")
        section("every text parameter", text_parameters, tmp)
        section("route table", the_table_is_the_contract, tmp)
        section("the contract's version", the_version_is_the_contracts, tmp)
        section("a stop ends a wait", a_stop_ends_a_wait, tmp)
        section("the client's page", the_page, tmp)
        section("the usage lines", the_usage_lines, tmp)
        section("a page from another address", origins, tmp)
        section("pages allowed by name", allowing_by_name, tmp)
        section("the body cap is a setting", the_body_cap_is_a_setting, tmp)
        section("a request's time", a_request_s_time, tmp)
        section("a reset connection", a_reset_connection, tmp)
        section("the read timeout is a setting", the_read_timeout_is_a_setting, tmp)
        section("a failed load, from its settings", a_failed_load_from_its_settings, tmp)
        section("its port, alone", its_port_alone, tmp)
        section("processes", processes, tmp)
        section("a process stopped over HTTP", a_stopped_process, tmp)
        section("a slow start", a_slow_start, tmp)
        section("its ports", its_ports, tmp)
        section("every port its own", every_port_its_own, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    width = max(len(n) for _, n, _ in results)
    for ok, name, detail in results:
        if not ok or not name.startswith("contract:"):
            print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}"
                  + (f"   {detail}" if detail and not ok else ""))
    contract = [ok for ok, n, _ in results if n.startswith("contract:")]
    print(f"      ({sum(contract)}/{len(contract)} responses checked against the contract)")
    for m in measured:
        print(f"  measured: {m}")
    fails = sum(1 for ok, _, _ in results if not ok)
    print("-" * 60)
    print(f"{len(results) - fails} passed, {fails} failed, {len(results)} total")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
