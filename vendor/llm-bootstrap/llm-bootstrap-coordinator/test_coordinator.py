"""The coordinator's box: its rules, its contract, and real processes.

    python test_coordinator.py

THE RULES run against a fake clock, so every lifetime, claim window and drop
is exact and nothing waits - except a held wait, which is real time by its
nature, and is bounded here by a fraction of a second. EVERY response they
produce is validated against the contract's shape for its route and status,
and every answer the coordinator's routes declare - each status, each refusal
reason - must be provoked at least once (`coverage`): a declared answer
nothing provokes is a promise with no test behind it.

THE PROCESSES are real: a coordinator subprocess, holders that are separate
processes, a holder killed mid-rental, and the coordinator itself killed and
restarted under a holder that must not notice. These print what they measured.

THE CARD IS NEVER ASKED: every coordinator started here is started with
`--hardware none`, every one built here is handed a fake machine or none, and
nvidia-smi is replaced by a tripwire before anything runs. The only processes
ended on proof are this suite's own children.

WHAT IT ANSWERS BEFORE ANY ROUTE is asked as a browser asks, headers and all
(`origins/coordinator.json`): a page from another address, a website whose
name was made to point at this machine, a body over the cap - each refused,
and every answer the contract declares there provoked; a request with no
Origin, one from the dashboard's own address and one to any of its own names
answered as before; an allowed page's browser told what it needs.
"""
from __future__ import annotations

import atexit
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import queue
import shutil
import random
import socket
import subprocess
import struct
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.client import HTTPException, HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONTRACT = HERE.parent / "llm-bootstrap-contract"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(CONTRACT))
import ids                                             # noqa: E402
import memory as MEM                                   # noqa: E402
import validate as V                                   # noqa: E402
import machine as M                                    # noqa: E402
import coordinator as C                                # noqa: E402
from coordinator import Coordinator, hardware_for, token_hash  # noqa: E402
from holder import Holder, Refused, call               # noqa: E402
from machine import NoHardware, ThisMachine, end_on_proof  # noqa: E402


def _never_the_card():
    raise AssertionError("this suite never asks the card: hand ThisMachine a query")


M.ask_nvidia_smi = _never_the_card


def _never_the_machines_lock():
    raise AssertionError("this suite never takes the machine's own card lock: hand serve, and "
                         "every coordinator it starts, a lock of the row's own (--card-lock)")


M.machine_lock = _never_the_machines_lock
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: EVERY PORT THIS SUITE HAD SOMETHING LISTEN ON - (what listened, the port
#: asked for, the port had) - read by its last section: a coordinator, and
#: the plain server beside one. A suite takes its ports from a range of its
#: own, never one the operating system picks, which may be anybody's the
#: moment after (H6's register 3: the plain server listened on port 0). The
#: coordinator's own `listen`, recorded where it is had
LISTENED: list = []
_listen = C.listen


def _listening(port, host="127.0.0.1"):
    got = _listen(port, host)
    LISTENED.append(("coordinator", port, got.server_address[1]))
    return got


C.listen = _listening

SHAPES, ROUTES = V.load(CONTRACT)
OURS = {k: r for k, r in ROUTES.items() if k.startswith("coordinator/")}
results: list = []
measured: list = []
#: every (route id, status, reason) the coordinator answered here - the coverage
SEEN: set = set()


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name, detail))


def conforms(route: str, status: int, body) -> list:
    """Problems with a response against the contract's shape for it."""
    r = ROUTES[route]
    shape = r["responses"].get(str(status))
    if shape is None:
        return [f"{route} does not declare status {status}"]
    return V.check(body, shape, SHAPES)


def seen(route: str, status: int, body) -> None:
    reason = body.get("reason") if status >= 400 and isinstance(body, dict) else None
    SEEN.add((ROUTES[route]["id"], status, reason))


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class Card:
    """A fake machine. It says how much of the card is in use - or that it
    cannot be asked - and, given `readings`, one reading after another at
    each FRESH look, the last one staying (a look that may keep an old answer
    reads the last one again); it says which of the pids it is asked about
    are alive (`up`); and it ends what it is handed, writing down every list,
    answering each process's outcome as the row set it (`outcome`, ended by
    default) - `mangle`, when set, changes that answer as a broken machine
    would. `hold` makes an end wait - at most five seconds - until the row
    lets it go; `waiting` says an end is waiting now."""
    def __init__(self, used=None, total=24576):
        self.used, self.total = used, total
        self.readings: list = []
        self.probes = 0
        self.fresh = 0                  # the probes that asked for a fresh answer
        self.up: set = set()
        self.ended: list = []
        self.outcome: dict = {}
        self.hold: threading.Event | None = None
        self.waiting = False
        self.fails = None
        self.mangle = None

    def probe(self, fresh=False):
        self.probes += 1
        self.fresh += bool(fresh)
        if fresh and self.readings:
            self.used = self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]
        if self.used is None:
            return {"known": False, "used_mib": None, "total_mib": None}
        return {"known": True, "used_mib": self.used, "total_mib": self.total}

    def alive(self, processes):
        return [p["pid"] in self.up for p in processes]

    def end(self, processes):
        self.waiting = True
        if self.hold is not None:
            self.hold.wait(5)
        self.waiting = False
        if self.fails:
            raise RuntimeError(self.fails)
        self.ended.append([dict(p) for p in processes])
        out = []
        for p in processes:
            pid, proof = p["pid"], p["proof"]
            kind = self.outcome.get(pid, "ended")
            out.append({"outcome": kind, "detail": {
                "ended": f"ended its process {pid} ({proof})",
                "gone": f"its process {pid} was already gone",
                "left_alone": f"pid {pid} does not carry {proof} on its command line - it is "
                              f"not the process the rental named; left alone",
                "not_ended": f"ended its process {pid} ({proof}), and it had not gone 15s "
                             f"later"}[kind]})
            if kind in ("ended", "gone"):
                self.up.discard(pid)
        return self.mangle(out) if self.mangle else out


class Probe:
    """A coordinator whose every answer is checked against the contract, and
    remembered for the coverage."""
    ROUTE = {"ask": "coordinator/card-ask", "wait": "coordinator/card-wait",
             "claim": "coordinator/card-claim", "heartbeat": "coordinator/card-heartbeat",
             "release": "coordinator/card-release", "cancel": "coordinator/card-cancel",
             "amend": "coordinator/card-amend",
             "state": "coordinator/state", "events": "coordinator/events",
             "up": "coordinator/up",
             "request_port": "coordinator/port-request",
             "heartbeat_port": "coordinator/port-heartbeat",
             "release_port": "coordinator/port-release"}

    def __init__(self, c: Coordinator):
        self.c = c

    def _fn(self, verb):
        return getattr(self.c, {"events": "events_since"}.get(verb, verb))

    def _go(self, verb, *args, **kw):
        status, body = self._fn(verb)(*args, **kw)
        bad = conforms(self.ROUTE[verb], status, body)
        check(f"contract: {verb} -> {status} is in the declared shape", not bad,
              "; ".join(bad))
        seen(self.ROUTE[verb], status, body)
        return status, body

    def __getattr__(self, verb):
        return lambda *a, **kw: self._go(verb, *a, **kw)


# Applications' actors, and one request a rental can be for.
W, I, X = "srv-k3x9m2p7", "srv-d4h6j9s3", "srv-e5g7k2m9"
A1, A2, A3, A4 = "srv-a2b3c4d5", "srv-f6g7h8j9", "srv-m2n3p4q5", "srv-r6s7t8u9"
CORR = "c-20260923-140012-a3k9m2p7"


def proc(pid, proof, model="demo-7b", engine="llamacpp", port=None, mib=None) -> dict:
    """A process as a heartbeat names it."""
    return {"pid": pid, "proof": proof, "model": model, "engine": engine, "port": port,
            "mib": mib}


PROC = proc(18344, "load-k3x9m2p7", port=31001)


def bare(named) -> list:
    """Processes as a rental names them, without the `since` the coordinator
    keeps: what their holder said."""
    return [{k: v for k, v in x.items() if k != "since"} for x in named]


def died(c: Coordinator) -> Path:
    """*c* stops as a coordinator process that died would: nothing more of
    it reaches the record the next one rebuilds from. Built in this process,
    its threads outlive its "death" - a reclaim or a release still under way
    finishes, and writes - so what they write goes to a file of their own.
    -> that file."""
    c.path = c.path.with_name("events-of-a-stopped-coordinator.jsonl")
    return c.path


def record_of(path: Path) -> list:
    """Every line of a record file, as written."""
    return [json.loads(ln) for ln in path.read_bytes().decode("utf-8").splitlines() if ln.strip()]


def until(pred, timeout: float = 5.0) -> bool:
    """Wait for *pred*, at most *timeout*: -> whether it came true."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return bool(pred())


# A BUSY MACHINE. This suite runs on somebody's machine, beside its owner's
# builds and games and other agents' sweeps; in wave 3 a holder it started did
# not say "holding" within 10 s, and one query of the process list, half a
# second alone, took 14 s. What a busy machine does late is waited for, and
# what it could not answer is asked again - within bounds that are there for
# what did not work, never for a slow machine. (The server part's suites keep
# the same in `_suites.py`: the parts share no code.)

#: how long a process this suite starts is given to say it is up: about a
#: second alone
START_S = 60.0
#: how long a question the process list could not answer is asked again:
#: four times its own bound (machine.QUERY_S)
ASK_AGAIN_S = 120.0


def asked_again(ask, answered, what: str, within_s: float = ASK_AGAIN_S,
                pause_s: float = 0.5, clock=time.monotonic, sleep=time.sleep):
    """`ask()` until `answered(what it said)`: -> the first answer that is
    one. What the process list could not answer is asked again, never read as
    an answer; asked for `within_s` seconds in vain, it is said - "could not
    tell <what>" - never guessed."""
    deadline = clock() + within_s
    while True:
        said = ask()
        if answered(said):
            return said
        if clock() >= deadline:
            raise AssertionError(f"could not tell {what}: the operating system gave no answer "
                                 f"in {within_s:g}s of asking (its last: {said!r})")
        sleep(pause_s)


#: how long a process this suite killed is waited for: six times the 15 s
#: after which a killed process had not ended (wave 3). A kill the system
#: accepted ends the process - the bound is for one that did not take
KILL_WAIT_S = 90.0


def killed(proc, within_s: float = KILL_WAIT_S):
    """*proc* - a process this suite started, by the handle its Popen holds -
    killed, and waited for until it has ended: -> its exit code. One that has
    not ended within `within_s` is said, by its pid (H4's R6: a kill and an
    unbounded wait held the suite for as long as the kill took)."""
    if proc.poll() is None:
        proc.kill()
    try:
        return proc.wait(within_s)
    except subprocess.TimeoutExpired:
        raise AssertionError(f"process {proc.pid} was killed and had not ended {within_s:g}s "
                             f"later") from None


#: the machine's own words for a command line its one query could not read:
#: the process is left alone - never ended on a guess - and this suite asks
#: again, since its own processes' command lines are there to be read
UNREAD = "could not be read"


def read_whole(said) -> bool:
    """Did the machine read every command line it was asked for? Its words -
    one outcome's, or a list of outcomes - say UNREAD where its query could
    not."""
    texts = [said] if isinstance(said, str) else [o["detail"] for o in said]
    return not any(UNREAD in text for text in texts)


def need(key, engine="llamacpp", mib=None) -> dict:
    return {"key": key, "engine": engine, "need_mib": mib}


def kinds(c: Coordinator, since: int = 0) -> list:
    return [e["kind"] for e in c.events if e["seq"] >= since]


def events_of(c: Coordinator, kind: str) -> list:
    return [e for e in c.events if e["kind"] == kind]


def granted(p: Probe, actor, holder="app", models=None, **kw):
    """Ask and claim at once, on a card nobody holds: -> (ticket id, token)."""
    s, b = p.ask(holder, models or [need("demo-7b")], actor=actor, **kw)
    tid, token = b["ticket"]["id"], b["token"]
    s2, b2 = p.claim(tid, token)
    if (s, b["ticket"]["state"], s2) != (200, "offered", 200):
        raise AssertionError(f"not granted at once: {s} {b} / {s2} {b2}")
    return tid, token


def asked(p: Probe, actor, holder="app", models=None, **kw):
    """-> (ticket id, token, state) of a new ticket."""
    s, b = p.ask(holder, models or [need("demo-7b")], actor=actor, **kw)
    if s != 200:
        raise AssertionError(f"the ask was refused: {s} {b}")
    return b["ticket"]["id"], b["token"], b["ticket"]["state"]


def line(p: Probe) -> list:
    """The tickets in line, as the operator's /state lists them."""
    return [(t["id"], t["state"]) for t in p.state()[1]["tickets"]]


# ------------------------------------------------------------------ tickets

def tickets(tmp: Path) -> None:
    """Ask, claim with the token, heartbeat, release - and every answer about
    the asker's own ticket alone."""
    clk, card = Clock(), Card()
    c = Coordinator(tmp / "t", ttl_s=15, hardware=card, clock=clk)
    p = Probe(c)

    s, b = p.ask("writer", [need("demo-7b")], actor=W, for_=CORR)
    check("an ask of a free card with nobody ahead is offered the card at once",
          s == 200 and b["ticket"]["state"] == "offered", str(b))
    check("...under a ticket id with a tail, and a secret token beside the ticket, never in it",
          ids.matches("ticket", b["ticket"]["id"]) and isinstance(b.get("token"), str)
          and len(b["token"]) >= 32 and "token" not in b["ticket"], str(b))
    check("...responsive unless it says otherwise, for the request it names",
          (b["ticket"]["class"], b["ticket"]["for"], b["ticket"]["actor"])
          == ("responsive", CORR, W), str(b["ticket"]))
    check("...and the offer lasts the claim window", b["ticket"]["offered_until"] == clk.t + 30,
          str(b["ticket"]))
    t1, k1 = b["ticket"]["id"], b["token"]
    clk.t += 1
    s, b = p.claim(t1, k1)
    check("the claim, with the token, is the rental - living the coordinator's lifetime",
          s == 200 and b["rental"]["id"] == t1 and b["rental"]["granted_at"] == clk.t
          and b["rental"]["expires_at"] == clk.t + 15, str(b))
    check("...held by the asker's actor, for its request, with the models it wants",
          (b["rental"]["actor"], b["rental"]["for"], b["rental"]["models"])
          == (W, CORR, [need("demo-7b")]), str(b["rental"]))
    s, b = p.claim(t1, k1)
    check("a claim asked again - its first answer lost - is answered with the same rental",
          s == 200 and b["rental"]["id"] == t1 and b["rental"]["granted_at"] == clk.t - 0,
          str(b))
    s, b = p.wait(t1, k1, 0)
    check("a wait on a claimed ticket answers at once: held",
          s == 200 and b["ticket"]["state"] == "held", str(b))
    clk.t += 5
    s, b = p.heartbeat(k1)
    check("a heartbeat with the token keeps the rental", s == 200
          and b["rental"]["expires_at"] == clk.t + 15, str(b))

    s, b = p.heartbeat("not-the-token")
    check("a token no ticket holds is unknown_ticket", s == 409
          and b["reason"] == "unknown_ticket", str(b))
    s, b = p.claim(t1, "not-the-token")
    check("the right ticket with the wrong token is answered as if there were none: "
          "no other application can claim it", s == 409 and b["reason"] == "unknown_ticket",
          str(b))
    s, b = p.wait(t1, "not-the-token", 0)
    check("...nor wait on it", s == 409 and b["reason"] == "unknown_ticket", str(b))

    s, b = p.ask("image", [need("sdxl", mib=9000)], actor=I)
    check("another application's ask while the card is held waits, and is told nothing more",
          s == 200 and b["ticket"]["state"] == "waiting" and b["ticket"]["offered_until"] is None
          and set(b) == {"ticket", "token"}, str(b))
    t2, k2 = b["ticket"]["id"], b["token"]
    s, b = p.claim(t2, k2)
    check("a ticket still waiting cannot claim: not_offered", s == 409
          and b["reason"] == "not_offered", str(b))
    for verb in ("heartbeat", "release"):
        s, b = getattr(p, verb)(k2)
        check(f"a ticket still waiting holds no rental to {verb}: not_held",
              s == 409 and b["reason"] == "not_held", str(b))
    s, b = p.cancel(k1)
    check("a held rental is released, not cancelled: claimed", s == 409
          and b["reason"] == "claimed", str(b))
    s, b = p.release(k1)
    check("a release gives the card back", s == 200 and b["released"]["id"] == t1, str(b))
    check("...and the card is offered at once to the next ticket in line",
          line(p) == [(t2, "offered")], str(line(p)))
    for verb, args in (("heartbeat", (k1,)), ("release", (k1,)), ("cancel", (k1,)),
                       ("claim", (t1, k1)), ("wait", (t1, k1, 0))):
        s, b = getattr(p, verb)(*args)
        check(f"a {verb} with a released rental's token is told it was released",
              s == 409 and b["reason"] == "released", str(b))
    s, b = p.wait(t2, k2, 0)
    check("a wait on an offered ticket answers at once: offered", s == 200
          and b["ticket"]["state"] == "offered", str(b))
    s, b = p.heartbeat(k2)
    check("an offered ticket holds no rental until it is claimed", s == 409
          and b["reason"] == "not_held", str(b))
    s, b = p.claim(t2, k2)
    check("the next ticket claims it", s == 200 and b["rental"]["actor"] == I, str(b))

    # --- liveness: a rental that goes quiet is taken back
    clk.t += 16
    s, b = p.state()
    check("a rental that goes a lifetime without a heartbeat is taken back",
          s == 200 and b["rental"] is None, str(b))
    rec = events_of(c, "rental_reclaimed")
    check("the record names who lost the card, and why",
          len(rec) == 1 and rec[0]["holder"] == "image" and rec[0]["actor"] == I
          and "no heartbeat for 16s" in rec[0]["detail"]
          and rec[0]["models"] == [need("sdxl", mib=9000)], str(rec))
    check("a lapsed rental that named no process has nothing to end: the machine is not asked, "
          "and the record says so", card.ended == [] and not events_of(c, "rental_reclaiming")
          and "it named no process on the card" in rec[0]["detail"], str(rec))
    for verb, args in (("heartbeat", (k2,)), ("release", (k2,)), ("cancel", (k2,)),
                       ("claim", (t2, k2)), ("wait", (t2, k2, 0))):
        s, b = getattr(p, verb)(*args)
        check(f"the holder learns it from an answer - a {verb} is told: reclaimed",
              s == 409 and b["reason"] == "reclaimed", str(b))

    for verb, args in (("cancel", ("no-such",)), ("release", ("no-such",)),
                       ("claim", ("t-9999-z2z2", "no-such"))):
        s, b = getattr(p, verb)(*args)
        check(f"a {verb} for a ticket never asked is unknown_ticket",
              s == 409 and b["reason"] == "unknown_ticket", str(b))

    tg, kg, state = asked(p, X, "notes")
    s, b = p.cancel(kg)
    check("an offered ticket its asker gives up is cancelled, and says what it was",
          state == "offered" and s == 200 and b["cancelled"]["id"] == tg
          and b["cancelled"]["state"] == "offered", str(b))
    check("...and it is out of the line", line(p) == [], str(line(p)))
    for verb, args in (("cancel", (kg,)), ("claim", (tg, kg)), ("wait", (tg, kg, 0)),
                       ("heartbeat", (kg,)), ("release", (kg,))):
        s, b = getattr(p, verb)(*args)
        check(f"a {verb} on a cancelled ticket is told: cancelled",
              s == 409 and b["reason"] == "cancelled", str(b))
    (gave_up,) = events_of(c, "ticket_cancelled")
    check("the record says it was given up", gave_up["ticket_id"] == tg
          and gave_up["actor"] == X, str(gave_up))
    for e in c.events:
        bad = V.check(e, {"ref": "event"}, SHAPES)
        if bad:
            check("every event of the protocol is in the declared shape", False, f"{e}: {bad}")
            break
    else:
        check("every event of the protocol is in the declared shape", True)


def refusals(tmp: Path) -> None:
    """A body a route cannot take is refused before anything moves."""
    c = Coordinator(tmp / "refusals", ttl_s=15, hardware=Card(), clock=Clock())
    p = Probe(c)
    m = [need("m")]
    for args, kw, reason in ((("", m), {"actor": W}, "no_holder"),
                             (("  ", m), {"actor": W}, "no_holder"),
                             ((None, m), {"actor": W}, "no_holder"),
                             (("h", [need("")]), {"actor": W}, "bad_models"),
                             (("h", [need("  ")]), {"actor": W}, "bad_models"),
                             (("h", [{"key": "m", "engine": "", "need_mib": None}]),
                              {"actor": W}, "bad_models"),
                             (("h", [need("m", mib=-1)]), {"actor": W}, "bad_models"),
                             (("h", m), {}, "bad_request"),
                             (("h", m), {"actor": "writer-1"}, "bad_request"),
                             (("h", m), {"actor": "srv-k3x9m2pl"}, "bad_request"),
                             (("h", m), {"actor": W, "klass": "urgent"}, "bad_request"),
                             (("h", "m"), {"actor": W}, "bad_request"),
                             (("h", [{"key": "m"}]), {"actor": W}, "bad_request"),
                             (("h", m), {"actor": W, "label": 7}, "bad_request"),
                             (("h", m), {"actor": W, "for_": "req-1"}, "bad_request")):
        s, b = p.ask(*args, **kw)
        check(f"ask{args!r} {kw!r} is refused as {reason}",
              s == 400 and b["reason"] == reason, str(b))
    check("...and nothing was asked for by any of them", not events_of(c, "ticket_asked"),
          str(kinds(c)))
    s, b = p.ask("h", [need("m", mib=0), need("n", engine="whisper", mib=4240)], actor=W,
                 klass="scheduled", label="two models")
    check("a ticket may want several models, each with its engine and need",
          s == 200 and [x["key"] for x in b["ticket"]["models"]] == ["m", "n"]
          and b["ticket"]["class"] == "scheduled" and b["ticket"]["label"] == "two models",
          str(b))
    for verb, args, why in (("wait", ("t-0041", "k", 0), "a ticket id that is not one"),
                            ("wait", ("t-0041-k3x9", 7, 0), "a token that is not a string"),
                            ("claim", ("t-0041", "k"), "a ticket id that is not one"),
                            ("claim", ("t-0041-k3x9", None), "no token"),
                            ("heartbeat", (None,), "no token"),
                            ("release", (41,), "a token that is not a string"),
                            ("cancel", (None,), "no token")):
        s, b = getattr(p, verb)(*args)
        check(f"a {verb} with {why} is refused as bad_request",
              s == 400 and b["reason"] == "bad_request", str(b))
    clk = Clock()
    c = Coordinator(tmp / "refusals-processes", ttl_s=15, hardware=Card(), clock=clk)
    p = Probe(c)
    _, token = granted(p, X)
    held_until = c.state()[1]["rental"]["expires_at"]
    clk.t += 3                                  # a renewal now would move expires_at
    s, b = p.heartbeat(token, processes=[dict(PROC, pid="18344")])
    check("a process named by something that is not a pid is refused, in the contract's words",
          (s, b.get("reason"), b.get("error")) == (400, "bad_request",
                                                   "$.processes[0].pid: expected integer, got "
                                                   "string"), str(b))
    nine = [proc(20001 + i, "load-a2b3c4" + "d5d6d7d8d9e2e3e4e5"[2 * i:2 * i + 2],
                 model=f"m{i + 1}", engine="whisper") for i in range(9)]
    for processes, why, words in (
            (nine, "more than eight processes", "a rental names at most 8 processes, not 9"),
            ([dict(PROC, proof="srv-k3x9m2p7")], "a proof that is another kind of actor",
             'processes[0]: a proof is the id of the load that started it (load-...), not '
             '"srv-k3x9m2p7"'),
            ([PROC, dict(PROC, proof="guessed")], "a proof that is no id at all",
             'processes[1]: a proof is the id of the load that started it (load-...), not '
             '"guessed"'),
            # shaped like a load's id, and no runner could have minted it: the
            # grammar's alphabet has no "o", and a string anyone could type is
            # no proof that only the process's starter put it there
            ([PROC, dict(PROC, pid=18345, proof="load-somebody")],
             "a proof that begins as a load's id and is not one",
             'processes[1]: a proof is the id of the load that started it (load-...), not '
             '"load-somebody"'),
            ([PROC, dict(PROC, proof="load-m4n8q2r5")], "one pid named twice",
             "processes[1]: pid 18344 is named twice")):
        s, b = p.heartbeat(token, processes=processes)
        check(f"a heartbeat naming {why} is refused bad_processes, saying so",
              (s, b.get("reason"), b.get("error")) == (400, "bad_processes", words), str(b))
    check("...and a refused heartbeat renewed nothing, and named nothing",
          c.state()[1]["rental"]["expires_at"] == held_until
          and c.state()[1]["rental"]["processes"] == [] and not events_of(c, "rental_processes"),
          str(c.state()[1]["rental"]))
    s, b = p.heartbeat(token, processes=nine[:8])
    check("...while eight processes, each on a load's id, are named",
          s == 200 and len(b["rental"]["processes"]) == 8, str(b)[:300])


# -------------------------------------------------------------------- order

def order(tmp: Path) -> None:
    """Responsive before scheduled, first come first served within a class,
    and nothing taken from a holder."""
    clk = Clock()
    c = Coordinator(tmp / "order", ttl_s=15, hardware=Card(), clock=clk)
    p = Probe(c)
    held, hk = granted(p, W, "holder", klass="scheduled")
    tickets_ = {}
    for actor, klass in ((A1, "scheduled"), (A2, "responsive"), (A3, "scheduled"),
                         (A4, "responsive")):
        clk.t += 1
        tid, token, state = asked(p, actor, klass=klass)
        tickets_[actor] = (tid, token)
        check(f"a {klass} ask while the card is held waits: nothing is taken from a holder",
              state == "waiting", state)
    s, st = p.state()
    check("the line is responsive first, then scheduled, first come first served in each",
          [t["actor"] for t in st["tickets"]] == [A2, A4, A1, A3],
          str([(t["actor"], t["class"]) for t in st["tickets"]]))
    check("...and the rental is still the holder's", st["rental"]["actor"] == W, str(st["rental"]))
    order_ = []
    token = hk
    for _ in range(4):
        clk.t += 1
        s, b = p.release(token)
        (offered,) = [t for t in p.state()[1]["tickets"] if t["state"] == "offered"]
        order_.append(offered["actor"])
        token = tickets_[offered["actor"]][1]
        s, b = p.claim(offered["id"], token)
    check("the card goes responsive first, then scheduled, each class in the order it asked",
          order_ == [A2, A4, A1, A3], str(order_))
    check("...as the record says, claim by claim",
          [e["actor"] for e in events_of(c, "rental_granted")] == [W, A2, A4, A1, A3],
          str([e["actor"] for e in events_of(c, "rental_granted")]))


# ------------------------------------------------------------------- window

def window(tmp: Path) -> None:
    """An offer nobody claims within the claim window passes to the next."""
    clk = Clock()
    c = Coordinator(tmp / "window", ttl_s=15, hardware=Card(), clock=clk)
    p = Probe(c)
    _, hk = granted(p, W)
    tb, kb, _ = asked(p, I)
    tc, kc, _ = asked(p, X)
    p.release(hk)
    s, b = p.wait(tb, kb, 0)
    offered_at = clk.t
    check("the next ticket is offered the card, for the claim window",
          s == 200 and b["ticket"]["state"] == "offered"
          and b["ticket"]["offered_until"] == offered_at + 30, str(b))
    clk.t += 29
    c.reap()
    check("within the window the offer stands", line(p) == [(tb, "offered"), (tc, "waiting")],
          str(line(p)))
    clk.t += 2
    c.reap()
    check("an offer not claimed within the window passes to the next ticket",
          line(p) == [(tc, "offered")], str(line(p)))
    rec = events_of(c, "ticket_unclaimed")
    check("...and the record says so", len(rec) == 1 and rec[0]["ticket_id"] == tb
          and "not claimed within 30s" in rec[0]["detail"], str(rec))
    for verb, args in (("claim", (tb, kb)), ("wait", (tb, kb, 0)), ("heartbeat", (kb,)),
                       ("release", (kb,)), ("cancel", (kb,))):
        s, b = getattr(p, verb)(*args)
        check(f"a {verb} on an offer that passed on is told: unclaimed",
              s == 409 and b["reason"] == "unclaimed", str(b))
    s, b = p.claim(tc, kc)
    check("the next ticket claims within its own window", s == 200, str(b))

    c5 = Coordinator(tmp / "window-5", ttl_s=15, hardware=Card(), clock=clk, claim_s=5)
    s, b = Probe(c5).ask("h", [need("m")], actor=W)
    check("the claim window is a setting", b["ticket"]["offered_until"] == clk.t + 5, str(b))


# --------------------------------------------------------------------- drop

def drop(tmp: Path) -> None:
    """A waiting ticket nobody waits on is dropped: its asker has gone. One
    whose asker is waiting on it is never dropped."""
    clk = Clock()
    # the holder's rental must outlive every minute this row moves the clock
    c = Coordinator(tmp / "drop", ttl_s=100_000, hardware=Card(), clock=clk, hold_s=5)
    p = Probe(c)
    _, hk = granted(p, W)
    tb, kb, _ = asked(p, I)
    clk.t += 119
    c.reap()
    check("a waiting ticket is kept while its drop time has not passed",
          line(p) == [(tb, "waiting")], str(line(p)))
    clk.t += 2
    c.reap()
    check("a waiting ticket nobody waited on for the drop time is dropped",
          line(p) == [], str(line(p)))
    rec = events_of(c, "ticket_dropped")
    check("...and the record says so", len(rec) == 1 and rec[0]["ticket_id"] == tb
          and "nobody waited on it" in rec[0]["detail"], str(rec))
    for verb, args in (("wait", (tb, kb, 0)), ("claim", (tb, kb)), ("heartbeat", (kb,)),
                       ("release", (kb,)), ("cancel", (kb,))):
        s, b = getattr(p, verb)(*args)
        check(f"its asker, back, is told by a {verb}: dropped",
              s == 409 and b["reason"] == "dropped", str(b))

    tc, kc, _ = asked(p, X)
    box = {}
    waiter = threading.Thread(target=lambda: box.update(got=p.wait(tc, kc, 1.0)), daemon=True)
    waiter.start()
    time.sleep(0.2)                                       # its wait is held open now
    clk.t += 500
    c.reap()
    check("a ticket whose asker is waiting on it is never dropped, however long",
          line(p) == [(tc, "waiting")], str(line(p)))
    waiter.join(5)
    check("...its wait answers when its hold ends: still waiting",
          box.get("got", (0, {}))[0] == 200
          and box["got"][1]["ticket"]["state"] == "waiting", str(box))
    clk.t += 119
    c.reap()
    check("...and its drop time runs from the end of its last wait",
          line(p) == [(tc, "waiting")], str(line(p)))
    clk.t += 2
    c.reap()
    check("...after which it is dropped too", line(p) == [], str(line(p)))

    c10 = Coordinator(tmp / "drop-10", ttl_s=15, hardware=Card(), clock=clk, drop_s=10)
    q = Probe(c10)
    granted(q, W)
    td, _, _ = asked(q, I)
    clk.t += 11
    c10.reap()
    check("the drop time is a setting", line(q) == [], str(line(q)))


# --------------------------------------------------------------- held waits

def held(tmp: Path) -> None:
    """A wait is held open, and answered the moment there is news."""
    c = Coordinator(tmp / "held", ttl_s=15, hardware=Card(), clock=Clock(), hold_s=0.4)
    p = Probe(c)
    _, hk = granted(p, W)
    tb, kb, _ = asked(p, I)
    t0 = time.monotonic()
    s, b = p.wait(tb, kb, 10)
    took = time.monotonic() - t0
    check("a wait with no news is held open - at most the coordinator's own hold - and then "
          "says: still waiting", s == 200 and b["ticket"]["state"] == "waiting"
          and 0.35 <= took < 2.0, f"{s} {b} after {took:.2f}s")
    t0 = time.monotonic()
    s, b = p.wait(tb, kb, 0.1)
    took = time.monotonic() - t0
    check("...and at most what the asker asked for", s == 200 and 0.05 <= took < 0.35,
          f"after {took:.2f}s")

    c2 = Coordinator(tmp / "held-2", ttl_s=15, hardware=Card(), clock=Clock(), hold_s=10)
    q = Probe(c2)
    _, hk = granted(q, W)
    tb, kb, _ = asked(q, I)
    tc, kc, _ = asked(q, X)
    box, done = {}, {}

    def wait(key, tid, token):
        box[key] = q.wait(tid, token, 8)
        done[key] = time.monotonic()
    w_b = threading.Thread(target=wait, args=("b", tb, kb), daemon=True)
    w_c = threading.Thread(target=wait, args=("c", tc, kc), daemon=True)
    w_b.start()
    w_c.start()
    time.sleep(0.3)
    t0 = time.monotonic()
    q.release(hk)
    w_b.join(5)
    check("a wait held open is answered the moment its ticket is offered the card",
          box.get("b", (0, {}))[0] == 200 and box["b"][1]["ticket"]["state"] == "offered"
          and done["b"] - t0 < 1.0, f"{box.get('b')} after {done.get('b', t0 + 99) - t0:.2f}s")
    measured.append(f"the card freed -> the waiting ticket told it is offered: "
                    f"{done.get('b', t0 + 99) - t0:.3f}s")
    check("...and another ticket's wait goes on holding", w_c.is_alive(), str(box.get("c")))
    t0 = time.monotonic()
    q.cancel(kc)
    w_c.join(5)
    check("a wait held open is answered at once when its ticket is cancelled: refused",
          box.get("c", (0, {}))[0] == 409 and box["c"][1]["reason"] == "cancelled"
          and done["c"] - t0 < 1.0, f"{box.get('c')}")
    td, kd, _ = asked(q, A1)
    w_d = threading.Thread(target=wait, args=("d", td, kd), daemon=True)
    w_d.start()
    time.sleep(0.3)
    t0 = time.monotonic()
    c2.close()
    w_d.join(5)
    check("a coordinator that stops answers every wait it holds at once",
          box.get("d", (0, {}))[0] == 200 and done["d"] - t0 < 1.0, f"{box.get('d')}")


# ------------------------------------------------------------------ wanted

def wanted(tmp: Path) -> None:
    """A renewal says whether the card is wanted - another ticket is in line
    for it - and nothing more: never whose, for what, or how many (PLAN-4 D6:
    a server's own pipeline lets the card go at once when it is wanted)."""
    clk = Clock()
    c = Coordinator(tmp / "wanted", ttl_s=15, hardware=Card(), clock=clk)
    p = Probe(c)
    _, hk = granted(p, W)
    s, b = p.heartbeat(hk)
    check("a renewal says the card is not wanted while nobody else is in line",
          s == 200 and b.get("wanted") is False, f"{s} {b}")
    s, a = p.ask("image-app", [need("beta-b", mib=1000)], actor=I, label="label-b",
                 klass="scheduled")
    tb, kb = a["ticket"]["id"], a["token"]
    s, b = p.heartbeat(hk)
    check("a renewal says the card is wanted once another ticket waits for it - a scheduled "
          "one too", s == 200 and b.get("wanted") is True, f"{s} {b}")
    text = json.dumps(b)
    check("...and says nothing of by whom: not its actor, its name, its models, its label "
          "or its ticket", not any(w in text for w in (I, "image-app", "beta-b", "label-b", tb)),
          text[:300])
    s, _ = p.cancel(kb)
    s, b = p.heartbeat(hk)
    check("a renewal says the card is no longer wanted once the ticket in line is given up",
          s == 200 and b.get("wanted") is False, f"{s} {b}")
    asked(p, X)
    said = []
    for _ in range(9):                                  # nobody waits on it: dropped at 120s
        clk.t += 14
        said.append(p.heartbeat(hk))
    check("a ticket dropped from the line wants nothing: the renewals say wanted until its "
          "drop, and not wanted after it",
          [b.get("wanted") for s, b in said] == [True] * 8 + [False]
          and all(s == 200 for s, _ in said) and events_of(c, "ticket_dropped"),
          str([(s, b.get("wanted")) for s, b in said]))


# ---------------------------------------------------------------------- D4

def d4(tmp: Path) -> None:
    """No answer to an application names another application - not while it
    waits, not when it is refused, not about a port. The operator's /state
    and /events still show everything."""
    clk = Clock()
    c = Coordinator(tmp / "d4", ttl_s=15, hardware=Card(used=9000), clock=clk,
                    ports=(51000, 51009), port_free=Machine())
    p = Probe(c)
    apps = {W: ("writer-app", "alpha-a", "label-a", CORR),
            I: ("image-app", "beta-b", "label-b", None),
            X: ("notes-app", "gamma-c", "label-c", None)}
    said = {a: [] for a in apps}

    def to(actor, answer):
        said[actor].append(answer)
        return answer

    def ask(actor, **kw):
        name, model, label, corr = apps[actor]
        return to(actor, p.ask(name, [need(model, mib=1000)], actor=actor, label=label,
                               **({"for_": corr} if corr else {}), **kw))

    s, a = ask(W)
    ta, ka = a["ticket"]["id"], a["token"]
    to(W, p.claim(ta, ka))
    to(W, p.heartbeat(ka, processes=[PROC]))
    to(W, p.request_port(W, "model-server"))
    s, b = ask(I)
    tb, kb = b["ticket"]["id"], b["token"]
    s, x = ask(X, klass="scheduled")
    tx, kx = x["ticket"]["id"], x["token"]
    for actor, tid, token in ((I, tb, kb), (X, tx, kx)):
        to(actor, p.wait(tid, token, 0))
        to(actor, p.claim(tid, token))
        to(actor, p.heartbeat(token))
        to(actor, p.release(token))
    to(I, p.heartbeat_port(51000, I))
    to(I, p.release_port(51000, I))
    to(W, p.release(ka))
    to(I, p.wait(tb, kb, 0))
    to(X, p.wait(tx, kx, 0))
    to(I, p.claim(tb, kb))
    to(W, p.heartbeat(ka))
    to(W, p.cancel(ka))
    to(X, p.cancel(kx))
    to(X, p.wait(tx, kx, 0))
    clk.t += 16
    to(I, p.heartbeat(kb))
    to(I, p.release(kb))
    leaks = []
    for actor, answers in said.items():
        text = json.dumps(answers)
        for other, words in apps.items():
            if other == actor:
                continue
            for word in (other, words[0], words[1], words[2]):
                if word in text:
                    leaks.append(f"{actor} was told {word!r}")
    check("no answer to an application names another application: not its actor, its name, "
          "its models or its label", not leaks and all(said.values()), "; ".join(leaks))
    refused = [b for answers in said.values() for s, b in answers if s >= 400]
    check("...its refusals included", len(refused) >= 8, str(len(refused)))
    s, st = p.state()
    s, ev = p.events(0)
    shown = json.dumps([st, ev])
    check("the operator's /state and /events still show everything: every application, "
          "its models and its label",
          all(w in shown for words in apps.values() for w in words[:3])
          and all(a in shown for a in apps), shown[:200])


# -------------------------------------------------------------------- token

def token(tmp: Path) -> None:
    """The token is in one answer, the ask's; the coordinator keeps only its
    sha256 - in memory and in the record."""
    clk = Clock()
    folder = tmp / "token"
    c = Coordinator(folder, ttl_s=15, hardware=Card(), clock=clk)
    p = Probe(c)
    _, first = granted(p, X)                            # the card is held: the ticket waits
    s, b = p.ask("writer", [need("demo-7b")], actor=W)
    tid, secret = b["ticket"]["id"], b["token"]
    sha = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    # the memory is looked at while the ticket lives - waiting, offered, held -
    # never only once it has gone, when nothing of it is left to find (the
    # first form of this row looked after the release, and could not fail)
    memory = [repr(vars(c))]                            # waiting
    p.release(first)
    memory.append(repr(vars(c)))                        # offered
    later = [p.claim(tid, secret), p.wait(tid, secret, 0), p.heartbeat(secret, processes=[PROC]),
             p.state(), p.events(0)]
    memory.append(repr(vars(c)))                        # held
    later.append(p.release(secret))
    check("the token is in no answer but the ask's",
          not any(secret in json.dumps(b) for _, b in later), str(later)[:300])
    record = (folder / "events.jsonl").read_bytes().decode("utf-8")
    check("the token is never in the record", secret not in record)
    (asked_,) = [e for e in events_of(c, "ticket_asked") if e["ticket_id"] == tid]
    check("...its sha256 is, on the line that asked for the ticket, and nowhere else",
          record.count(sha) == 1 and asked_.get("token_sha256") == sha, str(asked_))
    check("the coordinator keeps no token in memory - waiting, offered or held - only its "
          "sha256", len(memory) == 3 and all(secret not in m and sha in m for m in memory))
    other = Coordinator(tmp / "token-2", ttl_s=15, hardware=Card(), clock=clk)
    s, b2 = Probe(other).ask("writer", [need("demo-7b")], actor=W)
    check("every ticket has a token of its own", b2["token"] != secret)


# ------------------------------------------------------------------ rebuild

def rebuild(tmp: Path) -> None:
    clk = Clock()
    d = tmp / "r"
    c = Coordinator(d, ttl_s=10, hardware=Card(), clock=clk)
    ta, ka = granted(Probe(c), W, "writer", for_=CORR)
    c.heartbeat(ka, processes=[PROC])
    tb, kb, _ = asked(Probe(c), I, "image")
    tc, kc, _ = asked(Probe(c), X, "notes", klass="scheduled")
    td, kd, _ = asked(Probe(c), A1, "gone-app")
    c.cancel(kd)
    seq_before = c.seq

    clk.t += 200    # down far longer than a lifetime, and than a waiting ticket's drop time
    card = Card(used=18874)
    c2 = Coordinator(d, ttl_s=10, hardware=card, clock=clk)
    p = Probe(c2)
    s, st = p.state()
    r = st["rental"] or {}
    check("a restart finds the rental in its record: the same actor, for the same request",
          (r.get("id"), r.get("actor"), r.get("for")) == (ta, W, CORR), str(r))
    check("...with a fresh lifetime rather than blaming its holder for the outage",
          r.get("expires_at") == clk.t + 10, str(r))
    check("...and the processes it named", bare(r.get("processes") or []) == [PROC], str(r))
    check("the tickets in line come back in their order and class, none dropped for the outage",
          [(t["id"], t["class"], t["state"]) for t in st["tickets"]]
          == [(tb, "responsive", "waiting"), (tc, "scheduled", "waiting")], str(st["tickets"]))
    reb = events_of(c2, "state_rebuilt")
    check("the rebuild is on the record, one line for each, with what the card says",
          len(reb) == 3 and "the card has 18874 of 24576 MiB in use" in reb[0]["detail"]
          and [e["ticket_id"] for e in reb] == [ta, tb, tc], str(reb))
    check("sequence numbers continue across the restart",
          c2.events[seq_before]["kind"] == "coordinator_started"
          and [e["seq"] for e in c2.events] == list(range(c2.seq)))
    s, b = p.heartbeat(ka)
    check("the holder's heartbeat with its token is recognised again", s == 200, str(b))
    s, b = p.wait(tb, kb, 0)
    check("a waiting ticket's token is recognised again", s == 200
          and b["ticket"]["state"] == "waiting", str(b))
    s, b = p.cancel(kd)
    check("a ticket cancelled before the crash stays cancelled after it",
          s == 409 and b["reason"] == "cancelled", str(b))
    for _ in range(14):                                 # 119 s, the rental kept all along
        clk.t += 8.5
        p.heartbeat(ka)
    c2.reap()
    check("a waiting ticket's drop time starts again at the restart",
          [t["id"] for t in p.state()[1]["tickets"]] == [tb, tc], str(line(p)))
    p.release(ka)
    check("released, the card goes to the first in line, as before the restart",
          line(p) == [(tb, "offered"), (tc, "waiting")], str(line(p)))
    t_next, _, _ = asked(p, A2)
    check("ticket numbers continue, so an old id never names a new ticket",
          ids.ticket_seq(t_next) == ids.ticket_seq(td) + 1, f"{td} then {t_next}")

    d_off = tmp / "r-offered"
    c3 = Coordinator(d_off, ttl_s=10, hardware=Card(), clock=clk)
    to_, ko, state = asked(Probe(c3), W)
    clk.t += 25                                          # 5 s of the window left
    clk.t += 60
    c4 = Coordinator(d_off, ttl_s=10, hardware=Card(), clock=clk)
    q = Probe(c4)
    check("an offered ticket keeps its offer across a restart, with a fresh claim window",
          state == "offered" and q.state()[1]["tickets"][0]["offered_until"] == clk.t + 30,
          str(q.state()[1]["tickets"]))
    clk.t += 20
    s, b = q.claim(to_, ko)
    check("...and its asker claims it with its token", s == 200, str(b))

    lost_a = Coordinator(tmp / "lost-a", ttl_s=10, hardware=Card(), clock=clk)
    lost_b = Coordinator(tmp / "lost-b", ttl_s=10, hardware=Card(), clock=clk)
    ta_, _ = granted(Probe(lost_a), W)
    tb_, _ = granted(Probe(lost_b), W)
    check("a coordinator that lost its record cannot hand out an old ticket's id again",
          ids.ticket_seq(ta_) == ids.ticket_seq(tb_) == 1 and ta_ != tb_, f"{ta_} and {tb_}")

    for name, first in (("before-tickets", {"seq": 0, "at": 1.0, "kind": "turn_granted",
                                            "turn_id": "t-0041-k3x9", "holder": "writer-app",
                                            "actor": W, "model": "demo-7b"}),
                        ("before-actors", {"seq": 0, "at": 1.0, "kind": "turn_granted",
                                           "turn_id": "t-0041", "holder": "writer-app",
                                           "model": "demo-7b"})):
        d_old = tmp / name
        d_old.mkdir()
        (d_old / "events.jsonl").write_bytes((json.dumps(first) + "\n").encode("utf-8"))
        c_old = Coordinator(d_old, ttl_s=10, hardware=Card(), clock=clk)
        note = events_of(c_old, "state_rebuilt")
        check(f"a turn from a record written {name.replace('-', ' ')} is not carried over, "
              f"and says so", c_old.state()[1]["rental"] is None and len(note) == 1
              and "not carried over" in note[0]["detail"], str(note))
        served = Probe(c_old).events(0)[1]["events"]
        (began,) = [e for e in served if e["kind"] == "coordinator_started"]
        check(f"...its line in a shape since replaced stays in the file, and is left out of "
              f"/events - the start line says so ({name})",
              "turn_granted" in (d_old / "events.jsonl").read_bytes().decode("utf-8")
              and not any(e["kind"].startswith("turn_") for e in served)
              and "1 line of the record is in a shape since replaced (turn_granted), kept in "
                  "the file and left out of /events" in began["detail"], str(served)[:400])
        t_old, _, _ = asked(Probe(c_old), W)
        check(f"...and the numbers still continue past it ({name})",
              ids.ticket_seq(t_old) == 42, t_old)

    # a rental's one process, as a coordinator before the list recorded it
    d_one = tmp / "r-one-process"
    d_one.mkdir()
    old_lines = [
        {"seq": 0, "at": 5.0, "kind": "ticket_asked", "ticket_id": "t-0007-k3x9",
         "holder": "writer-app", "actor": W, "class": "responsive", "models": [need("demo-7b")],
         "token_sha256": hashlib.sha256(b"old-secret").hexdigest()},
        {"seq": 1, "at": 5.0, "kind": "ticket_offered", "ticket_id": "t-0007-k3x9",
         "holder": "writer-app", "actor": W, "detail": "the card is offered"},
        {"seq": 2, "at": 5.1, "kind": "rental_granted", "ticket_id": "t-0007-k3x9",
         "holder": "writer-app", "actor": W, "class": "responsive", "models": [need("demo-7b")]},
        {"seq": 3, "at": 6.0, "kind": "rental_process", "ticket_id": "t-0007-k3x9", "actor": W,
         "process": {"pid": 18344, "proof": "load-k3x9m2p7"},
         "detail": "the rental put pid 18344 on the card, carrying load-k3x9m2p7"}]
    (d_one / "events.jsonl").write_bytes("".join(json.dumps(x) + "\n" for x in old_lines)
                                         .encode("utf-8"))
    c_one = Coordinator(d_one, ttl_s=10, hardware=Card(), clock=clk)
    held_ = c_one.state()[1]["rental"] or {}
    check("a rental's one process, recorded before the list, is carried into the list - its "
          "model and engine the rental's, what it never said unknown",
          held_.get("processes") == [{"pid": 18344, "proof": "load-k3x9m2p7", "model": "demo-7b",
                                      "engine": "llamacpp", "port": None, "mib": None,
                                      "since": 6.0}], str(held_))
    served = Probe(c_one).events(0)[1]["events"]
    check("...its old line is left out of /events, the lines around it served",
          [e["kind"] for e in served][:3] == ["ticket_asked", "ticket_offered", "rental_granted"]
          and "rental_process" not in [e["kind"] for e in served], str(served)[:300])
    s, b = Probe(c_one).heartbeat("old-secret")
    check("...and its holder's token is known again, the list with it",
          s == 200 and [x["pid"] for x in b["rental"]["processes"]] == [18344], str(b))

    for name, end, why in (("released", lambda c_, k: c_.release(k), "released"),
                           ("reclaimed", lambda c_, k: (setattr(clk, "t", clk.t + 11),
                                                        c_.reap()), "reclaimed")):
        d_end = tmp / f"r-{name}"
        c5 = Coordinator(d_end, ttl_s=10, hardware=Card(), clock=clk)
        _, k5 = granted(Probe(c5), W)
        end(c5, k5)
        c6 = Coordinator(d_end, ttl_s=10, hardware=Card(), clock=clk)
        s, b = Probe(c6).heartbeat(k5)
        check(f"a rental {name} before the crash stays {name} after it",
              s == 409 and b["reason"] == why, str(b))
    for name, why, gap in (("dropped", "dropped", 121), ("unclaimed", "unclaimed", 31)):
        d_end = tmp / f"r-{name}"
        c7 = Coordinator(d_end, ttl_s=10, hardware=Card(), clock=clk)
        q7 = Probe(c7)
        if name == "dropped":
            granted(q7, W)
        t7, k7, _ = asked(q7, I)
        clk.t += gap
        c7.reap()
        c8 = Coordinator(d_end, ttl_s=10, hardware=Card(), clock=clk)
        s, b = Probe(c8).wait(t7, k7, 0)
        check(f"a ticket {name} before the crash stays {name} after it",
              s == 409 and b["reason"] == why, str(b))

    d3 = tmp / "r3"
    Coordinator(d3, ttl_s=10, hardware=Card(), clock=clk)
    c9 = Coordinator(d3, ttl_s=10, hardware=Card(used=20000), clock=clk)
    started = events_of(c9, "coordinator_started")
    check("what the card holds when nobody holds a rental is recorded, not guessed about",
          "the card has 20000 of 24576 MiB in use" in started[-1].get("detail", "")
          and c9.state()[1]["rental"] is None and not events_of(c9, "state_rebuilt"),
          str(started))
    for e in c9.events + c2.events + c4.events + c_one.events:
        bad = V.check(e, {"ref": "event"}, SHAPES)
        if bad:
            check("every event of a rebuild is in the declared shape", False, f"{e}: {bad}")
            break
    else:
        check("every event of a rebuild is in the declared shape", True)


# ------------------------------------------------------------------- limits

def limits(tmp: Path) -> None:
    """Every limit is a setting, with the plan's starting default."""
    s, st = Probe(Coordinator(tmp / "limits", hardware=Card(), clock=Clock())).state()
    # CHANGED ON PURPOSE (S4): the memory's three limits joined them - the
    # margin kept free beside a need, an amend's wait right after the rental's
    # own unload, and how often the card is read again for work waiting for room.
    # CHANGED ON PURPOSE (S5): the watch's three joined them - the hang limit,
    # how often the card is read during a rental, and the floor of free memory
    # under which a rental is degraded (PLAN-4 D0's 5 min, 30 s and 256 MiB)
    check("the limits, by default: a rental lives 15 s between heartbeats, an offer 30 s, a "
          "ticket nobody waits on 120 s, a wait is held at most 30 s, a release settles "
          "within 10 s, 256 MiB is kept free beside a need, an amend right after an unload "
          "waits 5 s for the memory, the card is read again every 5 s for work waiting "
          "for room, a rental working with no progress is taken back after 300 s, the card "
          "is read every 30 s during a rental, and a rental is degraded under 256 MiB free "
          "- and, with an answer far slower than its normal, under 1024 MiB free",
          # CHANGED ON PURPOSE (S11b): the card counts as full under 1024 MiB
          # free, where a slow answer marks the rental degraded (D3, D12)
          st["limits"] == {"ttl_s": 15.0, "claim_s": 30.0, "drop_s": 120.0, "hold_s": 30.0,
                           "settle_s": 10.0, "margin_mib": 256, "amend_settle_s": 5.0,
                           "recheck_s": 5.0, "hang_s": 300.0, "watch_s": 30.0,
                           "floor_mib": 256, "full_mib": 1024,
                           # CHANGED ON PURPOSE (S5b): while a rental is degraded
                           # the card is read every 2 s, so room again is seen
                           "recover_watch_s": 2.0}, str(st["limits"]))
    a = C.parse_args(["--data", str(tmp / "limits-cli")])
    check("...and so on the command line",
          (a.ttl, a.claim, a.drop, a.hold, a.settle, a.margin, a.amend_settle, a.recheck,
           a.hang, a.watch, a.floor, a.full, a.recover_watch)
          == (15.0, 30.0, 120.0, 30.0, 10.0, 256, 5.0, 5.0, 300.0, 30.0, 256, 1024, 2.0),
          str(vars(a)))
    a = C.parse_args(["--data", str(tmp / "limits-cli"), "--ttl", "3", "--claim", "4",
                      "--drop", "5", "--hold", "6", "--settle", "7", "--margin", "300",
                      "--amend-settle", "2", "--recheck", "9", "--hang", "45", "--watch", "11",
                      "--floor", "700", "--full", "2000", "--recover-watch", "3",
                      "--hardware", "none"])
    s, st = Probe(C.coordinator_from(a)).state()
    check("each is set from the command line, and the coordinator runs with it",
          st["limits"] == {"ttl_s": 3.0, "claim_s": 4.0, "drop_s": 5.0, "hold_s": 6.0,
                           "settle_s": 7.0, "margin_mib": 300, "amend_settle_s": 2.0,
                           "recheck_s": 9.0, "hang_s": 45.0, "watch_s": 11.0,
                           "floor_mib": 700, "full_mib": 2000, "recover_watch_s": 3.0},
          str(st["limits"]))
    for bad in ({"ttl_s": 0}, {"claim_s": 0}, {"drop_s": -1}, {"hold_s": 0}, {"settle_s": 0},
                {"keep_closed": 0}, {"margin_mib": -1}, {"margin_mib": 1.5},
                {"amend_settle_s": 0}, {"recheck_s": 0}, {"hang_s": 0}, {"hang_s": -5},
                {"watch_s": 0}, {"floor_mib": -1}, {"floor_mib": 1.5}, {"floor_mib": True},
                {"full_mib": -1}, {"full_mib": 1.5}, {"full_mib": True}):
        try:
            Coordinator(tmp / "limits-bad", hardware=Card(), clock=Clock(), **bad)
            refused = False
        except ValueError:
            refused = True
        check(f"a limit that cannot work ({bad}) is refused", refused)
    # S4 R9: a server waits the contract's AMEND_ANSWER_S for an amend's
    # answer, and one it never hears decides nothing - so a settle an answer
    # could not keep within it would have had every amend right after an
    # unload approved unverified
    top = MEM.AMEND_SETTLE_MAX_S
    for settle in (top, top + 0.5):
        try:
            Coordinator(tmp / "limits-settle", hardware=Card(), clock=Clock(),
                        amend_settle_s=settle)
            said = ""
        except ValueError as e:
            said = str(e)
        if settle == top:
            check(f"an amend may wait for the memory as long as the contract allows ({top:g}s)",
                  not said, said)
        else:
            check(f"...and no longer: a settle of {settle:g}s is refused, saying why",
                  "amend_settle_s" in said and f"{MEM.AMEND_ANSWER_S:g}s" in said, said)
    heard = io.StringIO()
    try:
        with contextlib.redirect_stderr(heard):
            C.parse_args(["--data", str(tmp / "limits-cli"), "--amend-settle", str(top + 0.5)])
        code = "it started"
    except SystemExit as e:
        code = e.code
    check("...and so on the command line, before anything is started",
          code == 2 and "--amend-settle" in heard.getvalue()
          and f"{MEM.AMEND_ANSWER_S:g}s" in heard.getvalue(), f"{code}: {heard.getvalue()}")
    shipped = importlib.util.spec_from_file_location("machine_read_bound", HERE / "machine.py")
    pristine = importlib.util.module_from_spec(shipped)
    shipped.loader.exec_module(pristine)
    asked: dict = {}

    def run(cmd, **kw):
        asked.update(kw)
        raise OSError("this suite never asks the card")
    pristine.subprocess = type("NoCard", (), {"run": staticmethod(run)})
    pristine.ask_nvidia_smi()
    read = asked.get("timeout")
    check("the longest an amend can take - the most settle the contract allows, then one "
          "reading of the card - is within what a server waits for its answer",
          isinstance(read, (int, float)) and top + read < MEM.AMEND_ANSWER_S,
          f"{top:g}s + a reading of {read}s, against {MEM.AMEND_ANSWER_S:g}s")


# ---------------------------------------------------------------- the ports

class Machine:
    """The operating system, as the port check sees it: these ports are in use
    by something else. Every port asked about is written down."""
    def __init__(self, *busy):
        self.busy, self.asked = set(busy), []

    def __call__(self, port: int) -> bool:
        self.asked.append(port)
        return port not in self.busy


def ports(tmp: Path) -> None:
    clk, machine = Clock(), Machine(51001)
    c = Coordinator(tmp / "ports", ttl_s=10, hardware=Card(), clock=clk,
                    ports=(51000, 51004), port_free=machine)
    p = Probe(c)

    s, b = p.request_port(W, "model")
    check("a port is handed out from the range, to an actor, for a purpose",
          s == 200 and (b["port"]["port"], b["port"]["actor"], b["port"]["purpose"])
          == (51000, W, "model"), str(b))
    first = b["port"]
    clk.t += 3
    s, b = p.request_port(W, "model")
    check("the same actor asking again for the same purpose renews the SAME port",
          s == 200 and b["port"]["port"] == 51000 and b["port"]["expires_at"] == clk.t + 10
          and b["port"]["granted_at"] == first["granted_at"], str(b))
    s, b = p.request_port(W, "server")
    check("another purpose of the same actor is another port - and one something else "
          "on the machine uses is skipped", s == 200 and b["port"]["port"] == 51002, str(b))
    check("...the machine was asked about it", 51001 in machine.asked, str(machine.asked))
    s, b = p.request_port(X, "server")
    check("another actor never gets a port already held", s == 200 and b["port"]["port"] == 51003,
          str(b))
    s, b = p.request_port(I, "model", 30)
    last = b["port"]["port"]
    check("...and a lifetime can be asked for", s == 200 and last == 51004
          and b["port"]["expires_at"] == clk.t + 30, str(b))
    s, b = p.request_port("srv-a2b3c4d5", "model")
    check("a range that is all held or in use is exhausted, and says how",
          s == 409 and b["reason"] == "exhausted" and "4 held, 1 in use" in b["error"], str(b))

    clk.t += 4
    s, b = p.heartbeat_port(51000, W)
    check("a heartbeat by the holder extends its port", s == 200
          and b["port"]["expires_at"] == clk.t + 10, str(b))
    held_until = b["port"]["expires_at"]
    s, b = p.heartbeat_port(51000, X)
    check("a heartbeat by another actor is refused, naming no one: not_yours",
          s == 409 and b["reason"] == "not_yours" and W not in b["error"], str(b))
    check("...and does not keep the port alive", c.ports[51000]["expires_at"] == held_until)
    s, b = p.release_port(51000, X)
    check("another actor cannot give the port back either, and is told nothing of its holder",
          s == 409 and b["reason"] == "not_yours" and W not in b["error"] and 51000 in c.ports,
          str(b))
    s, b = p.release_port(51000, W)
    check("the holder gives it back", s == 200 and b["released"]["port"] == 51000
          and 51000 not in c.ports, str(b))
    s, b = p.heartbeat_port(51000, W)
    check("a heartbeat on a port given back is told it was released",
          s == 409 and b["reason"] == "released", str(b))
    s, b = p.release_port(51000, W)
    check("giving it back twice is told the same", s == 409 and b["reason"] == "released", str(b))
    s, b = p.request_port("srv-a2b3c4d5", "model")
    check("a port given back is handed to the next asker",
          s == 200 and b["port"]["port"] == 51000, str(b))
    s, b = p.heartbeat_port(51000, W)
    check("...after which it is someone else's to its old holder",
          s == 409 and b["reason"] == "not_yours", str(b))
    s, b = p.heartbeat_port(51999, W)
    check("a port never held is unknown", s == 409 and b["reason"] == "unknown_port", str(b))
    s, b = p.release_port(51999, W)
    check("...and cannot be given back", s == 409 and b["reason"] == "unknown_port", str(b))

    for args, kw, reason in (((W, None), {}, "no_purpose"), ((W, "  "), {}, "no_purpose"),
                             ((W, "model", 0), {}, "bad_ttl"), ((W, "model", True), {}, "bad_ttl"),
                             (("writer-1", "model"), {}, "bad_request"),
                             ((None, "model"), {}, "bad_request")):
        s, b = p.request_port(*args, **kw)
        check(f"request_port{args!r} is refused as {reason}", s == 400 and b["reason"] == reason,
              str(b))
    for verb in ("heartbeat_port", "release_port"):
        s, b = getattr(p, verb)(None, W)
        check(f"{verb} without a port is refused", s == 400 and b["reason"] == "no_port", str(b))

    # --- liveness: a quiet holder loses its port
    clk.t += 31
    s, b = p.state()
    check("ports whose holders went a lifetime without a heartbeat are taken back",
          s == 200 and b["ports"] == [], str(b["ports"]))
    rec = events_of(c, "port_reclaimed")
    check("the record names each port, its holder, and why",
          sorted(e["port"] for e in rec) == [51000, 51002, 51003, 51004]
          and all("no heartbeat for" in e["detail"] for e in rec), str(rec))
    s, b = p.release_port(51002, W)
    check("the holder learns it from an answer: reclaimed", s == 409 and b["reason"] == "reclaimed",
          str(b))
    check("the state says the range", b and p.state()[1]["port_range"] == {"from": 51000,
                                                                          "to": 51004})
    kinds_ = [e["kind"] for e in c.events]
    check("every grant, release and reclaim is a line of the record",
          kinds_.count("port_granted") == 5 and kinds_.count("port_released") == 1
          and kinds_.count("port_reclaimed") == 4, str(kinds_))
    for e in c.events:
        bad = V.check(e, {"ref": "event"}, SHAPES)
        if bad:
            check("every port event is in the declared shape", False, f"{e}: {bad}")
            break
    else:
        check("every port event is in the declared shape", True)

    plain = Coordinator(tmp / "ports-default", ttl_s=10, hardware=Card(), clock=clk)
    check("by default ports come from BELOW every common system's range for passing "
          "connections (Windows from 49152, Linux from 32768)",
          1024 <= plain.port_from <= plain.port_to < 32768,
          f"{plain.port_from}-{plain.port_to}")
    for bad_range in ((80, 90), (40000, 39999), (60000, 70000)):
        try:
            Coordinator(tmp / "ports-bad", ttl_s=10, hardware=Card(), clock=clk, ports=bad_range)
            refused = False
        except ValueError:
            refused = True
        check(f"a range {bad_range} that cannot work is refused", refused)


def port_rebuild(tmp: Path) -> None:
    clk = Clock()
    d = tmp / "pr"
    c = Coordinator(d, ttl_s=10, hardware=Card(), clock=clk, ports=(51000, 51009),
                    port_free=Machine())
    kept = c.request_port(W, "server")[1]["port"]
    gone = c.request_port(X, "model")[1]["port"]
    c.release_port(gone["port"], X)
    quiet = c.request_port(I, "model", 2)[1]["port"]
    clk.t += 3
    c.reap()

    clk.t += 60                                 # down far longer than a lifetime
    c2 = Coordinator(d, ttl_s=10, hardware=Card(), clock=clk, ports=(51000, 51009),
                     port_free=Machine())
    p = Probe(c2)
    check("a restart finds a held port in its record",
          list(c2.ports) == [kept["port"]] and c2.ports[kept["port"]]["actor"] == W,
          str(c2.ports))
    check("...gives it a fresh lifetime, keeping when it was first granted",
          c2.ports[kept["port"]]["expires_at"] == clk.t + 10
          and c2.ports[kept["port"]]["granted_at"] == kept["granted_at"], str(c2.ports))
    reb = [e for e in c2.events if e["kind"] == "state_rebuilt" and e.get("port")]
    check("...and says so on the record", len(reb) == 1 and reb[0]["port"] == kept["port"],
          str(reb))
    s, b = p.heartbeat_port(kept["port"], W)
    check("its holder's next heartbeat simply works", s == 200, str(b))
    s, b = p.heartbeat_port(gone["port"], X)
    check("a port given back before the crash stays given back after it",
          s == 409 and b["reason"] == "released", str(b))
    s, b = p.heartbeat_port(quiet["port"], I)
    check("a port taken back before the crash stays taken back after it",
          s == 409 and b["reason"] == "reclaimed", str(b))
    s, b = p.request_port("srv-a2b3c4d5", "server")
    check("a port carried over is never handed to another asker",
          s == 200 and b["port"]["port"] != kept["port"], str(b))
    clk.t += 5
    c3 = Coordinator(d, ttl_s=10, hardware=Card(), clock=clk, ports=(51000, 51009),
                     port_free=Machine())
    check("a second restart still keeps the first grant's time",
          c3.ports[kept["port"]]["granted_at"] == kept["granted_at"], str(c3.ports))


# ---------------------------------------------------------------- the card

def named(tmp: Path) -> None:
    """A rental names EVERY process it put on the card, on its heartbeats: the
    whole list, which replaces the one the coordinator had - so a beat lost on
    the way is healed by the next - one line of the record when the list
    changes, never one per beat; each process kept with since when; a restart
    carries the list; /state lists each one, and how much of the card is in
    use."""
    clk, card = Clock(), Card(used=18874)
    c = Coordinator(tmp / "named", ttl_s=10, hardware=card, clock=clk)
    p = Probe(c)
    _, k = granted(p, W, "writer")
    s, b = p.heartbeat(k)
    check("a new rental names no process, and a heartbeat that names none leaves it so",
          s == 200 and b["rental"]["processes"] == [] and b["rental"]["state"] == "held", str(b))
    clk.t += 1
    s, b = p.heartbeat(k, processes=[PROC])
    since = clk.t
    check("a heartbeat names the processes the rental put on the card, and the rental carries "
          "them - each with since when the coordinator has known of it",
          s == 200 and bare(b["rental"]["processes"]) == [PROC]
          and b["rental"]["processes"][0]["since"] == since, str(b))
    told = events_of(c, "rental_processes")
    check("...once on the record: the whole list, and what was added",
          len(told) == 1 and bare(told[0]["processes"]) == [PROC] and told[0]["actor"] == W
          and "named pid 18344 (demo-7b on llamacpp, carrying load-k3x9m2p7)" in told[0]["detail"]
          and "it names 1 process now" in told[0]["detail"], str(told))
    for _ in range(3):
        clk.t += 2
        p.heartbeat(k, processes=[PROC])
    check("the same list said again writes nothing - never a line per beat",
          len(events_of(c, "rental_processes")) == 1, str(kinds(c)))
    clk.t += 1
    s, b = p.heartbeat(k, processes=[dict(PROC, mib=5120)])
    check("a process's memory, measured anew, is kept - and writes nothing either, its since "
          "unmoved", s == 200 and b["rental"]["processes"][0]["mib"] == 5120
          and b["rental"]["processes"][0]["since"] == since
          and len(events_of(c, "rental_processes")) == 1, str(b))
    second = proc(18377, "load-m4n8q2r5", model="beta-3b", port=31002)
    third = proc(18390, "load-p7q2x3k9", model="gamma-1b", engine="whisper", port=31003)
    clk.t += 1
    # the beat that named `second` alone was lost on the way; the next names it with `third`
    s, b = p.heartbeat(k, processes=[dict(PROC, mib=5120), second, third])
    check("a heartbeat's list REPLACES the one kept, so a beat lost on the way is healed by the "
          "next", s == 200
          and bare(b["rental"]["processes"]) == [dict(PROC, mib=5120), second, third], str(b))
    told = events_of(c, "rental_processes")
    check("...one line for the change, naming each process added",
          len(told) == 2 and "named pid 18377 (beta-3b on llamacpp, carrying load-m4n8q2r5)"
          in told[-1]["detail"] and "named pid 18390 (gamma-1b on whisper" in told[-1]["detail"]
          and "it names 3 processes now" in told[-1]["detail"], str(told[-1]))
    s, b = p.heartbeat(k)
    check("a heartbeat without a list leaves the list as it was",
          [x["pid"] for x in b["rental"]["processes"]] == [18344, 18377, 18390], str(b))
    clk.t += 1
    s, b = p.heartbeat(k, processes=[second, third])
    told = events_of(c, "rental_processes")
    check("a process no longer named is off the list, and its line says so",
          [x["pid"] for x in b["rental"]["processes"]] == [18377, 18390] and len(told) == 3
          and "no longer names pid 18344 (demo-7b, load-k3x9m2p7)" in told[-1]["detail"]
          and "it names 2 processes now" in told[-1]["detail"], str(told[-1]))
    clk.t += 1
    s, b = p.heartbeat(k, processes=[])
    check("an empty list: nothing of the rental is on the card - and the rental is still held",
          s == 200 and b["rental"]["processes"] == [] and b["rental"]["state"] == "held"
          and "it names no process now" in events_of(c, "rental_processes")[-1]["detail"], str(b))
    clk.t += 1
    s, b = p.heartbeat(k, processes=[PROC, second])
    b["rental"]["processes"][0]["pid"] = 1
    s, st = p.state()
    check("a rental handed out is a copy: changing it changes nothing here",
          [x["pid"] for x in st["rental"]["processes"]] == [18344, 18377], str(st["rental"]))
    check("the state lists each process: pid, proof, model, engine, port, memory and since when",
          st["rental"]["processes"] == [dict(PROC, since=clk.t), dict(second, since=clk.t)],
          str(st["rental"]))
    # CHANGED ON PURPOSE (S5): the card also says how much of it is the
    # rental's and how much is outside the bootstrap - neither known here: its
    # processes measured nothing, and their models declared no need
    check("...and how much of the card is in use",
          st["card"] == {"known": True, "used_mib": 18874, "total_mib": 24576,
                         "ours_mib": None, "outside_mib": None}, str(st))

    d = tmp / "named-restart"
    c3 = Coordinator(d, ttl_s=10, hardware=Card(), clock=clk)
    _, k3 = granted(Probe(c3), W, "writer")
    c3.heartbeat(k3, processes=[PROC, second])
    before = c3.state()[1]["rental"]["processes"]
    lines_before = len(events_of(c3, "rental_processes"))
    clk.t += 60
    c4 = Coordinator(d, ttl_s=10, hardware=Card(used=18874), clock=clk)
    reb = events_of(c4, "state_rebuilt")
    check("a restart carries the list - each process with its since - and says so",
          c4.state()[1]["rental"]["processes"] == before and len(reb) == 1
          and bare(reb[0]["processes"]) == [PROC, second]
          and "it names pid 18344 (demo-7b, load-k3x9m2p7), pid 18377 (beta-3b, "
              "load-m4n8q2r5)" in reb[0]["detail"], str(reb))
    s, b = Probe(c4).heartbeat(k3, processes=[PROC, second])
    check("...and its holder's next heartbeat, naming the same, writes nothing",
          s == 200 and len(events_of(c4, "rental_processes")) == lines_before, str(b))
    clk.t += 11
    c4.reap()
    until(lambda: c4.state()[1]["rental"] is None)
    check("...so a reclaim after the restart ends the processes the record carried",
          [bare(x) for x in c4.hardware.ended] == [[PROC, second]], str(c4.hardware.ended))

    n = Coordinator(tmp / "no-machine", ttl_s=10, clock=clk)
    s, st = Probe(n).state()
    # CHANGED ON PURPOSE (S5): with ours (nothing: no rental) and outside (not
    # known: the card was not asked) beside it
    check("a coordinator built in code asks nothing of the card",
          isinstance(n.hardware, NoHardware)
          and st["card"] == {"known": False, "used_mib": None, "total_mib": None,
                             "ours_mib": 0, "outside_mib": None}, str(st))
    check("...and its start says so", n.events[0].get("detail") == "the card was not asked",
          str(n.events[0]))
    _, nk = granted(Probe(n), W, "writer")
    n.heartbeat(nk, processes=[PROC])
    clk.t += 11
    n.reap()
    rec = events_of(n, "rental_reclaimed")
    check("...and ends nothing: the record says its process was not ended, and the card was "
          "not touched", len(rec) == 1 and not events_of(n, "rental_reclaiming")
          and "no machine configured: its 1 process was not ended" in rec[0]["detail"]
          and "the card is not touched" in rec[0]["detail"], str(rec))
    check("a coordinator started from its command line asks this machine by default",
          C.parse_args(["--data", "x"]).hardware == "this-machine"
          and C.parse_args(["--data", "x", "--hardware", "none"]).hardware == "none")
    check("--hardware names this machine or nothing",
          isinstance(hardware_for("this-machine"), ThisMachine)
          and isinstance(hardware_for("none"), NoHardware))
    try:
        hardware_for("gpu")
        check("--hardware refuses anything else", False)
    except ValueError:
        check("--hardware refuses anything else", True)
    for e in c.events + c4.events + n.events:
        bad = V.check(e, {"ref": "event"}, SHAPES)
        if bad:
            check("every event of a named process is in the declared shape", False, f"{e}: {bad}")
            break
    else:
        check("every event of a named process is in the declared shape", True)


def reclaiming(tmp: Path) -> None:
    """A lapsed rental is ended OUTSIDE the coordinator's lock: it goes to
    reclaiming; every process it named is handed to the machine at once, to be
    ended each on its own proof; each outcome is recorded; and only then is
    the card offered to the next ticket. Meanwhile everyone else is answered,
    and its holder is told it lost the card."""
    clk, card = Clock(), Card(used=1000)
    c = Coordinator(tmp / "reclaiming", ttl_s=10, hardware=card, clock=clk,
                    ports=(51000, 51009), port_free=Machine())
    p = Probe(c)
    ta, ka = granted(p, W, "writer")
    four = [proc(18344, "load-k3x9m2p7", port=31001),
            proc(18377, "load-m4n8q2r5", model="beta-3b", port=31002),
            proc(18390, "load-p7q2x3k9", model="gamma-1b", engine="whisper"),
            proc(18400, "load-q2r5s3t6", model="delta-2b")]
    card.up = {18344, 18377, 18390, 18400}
    card.outcome = {18377: "gone", 18390: "left_alone", 18400: "not_ended"}
    p.heartbeat(ka, processes=four)
    s, b = p.request_port(I, "model", 100)                  # it outlives the lapse below
    iport = b["port"]["port"]
    tb, kb, _ = asked(p, I, "image")
    card.hold = threading.Event()
    clk.t += 11
    reaper = threading.Thread(target=c.reap, daemon=True)
    t0 = time.monotonic()
    reaper.start()
    reaper.join(2)
    check("a lapse is seen without waiting on the machine: the reap returns at once",
          not reaper.is_alive() and time.monotonic() - t0 < 1.0,
          f"after {time.monotonic() - t0:.2f}s")
    check("...while the machine is ending the rental's processes",
          until(lambda: card.waiting, 2), "the machine was never asked to end anything")
    box = {}
    t0 = time.monotonic()
    beat = threading.Thread(target=lambda: box.update(got=p.heartbeat_port(iport, I)), daemon=True)
    beat.start()
    beat.join(2)
    took = time.monotonic() - t0
    check("another holder's port heartbeat is answered while a reclaim runs",
          not beat.is_alive() and box.get("got", (0, {}))[0] == 200 and took < 1.0,
          f"{box.get('got')} after {took:.2f}s")
    s, st = p.state()
    check("...the rental is `reclaiming` meanwhile, still naming what is being ended",
          st["rental"] is not None and st["rental"]["state"] == "reclaiming"
          and bare(st["rental"]["processes"]) == four, str(st["rental"]))
    check("...the card is offered to no one until it is done", line(p) == [(tb, "waiting")],
          str(line(p)))
    s, b = p.heartbeat(ka)
    check("...and its holder is told at once that it lost the card: reclaimed",
          s == 409 and b["reason"] == "reclaimed", str(b))
    (going,) = events_of(c, "rental_reclaiming")
    check("the record says it is being taken back, and what is being ended",
          bare(going["processes"]) == four and going["actor"] == W
          and "no heartbeat for 11s; ending its 4 processes on proof" in going["detail"],
          str(going))
    card.hold.set()
    # waited on the coordinator itself, then checked once: a Probe checks each
    # answer against the contract, and a check per poll made this suite's total
    # move with the machine's speed
    before = len(results)
    ended = until(lambda: c.state()[1]["rental"] is None, 5)
    check("the reclaim ends", ended, str(p.state()[1]))
    check("...waited for on the coordinator itself and checked once: the wait adds nothing to "
          "this suite's count, which moved with the machine's speed while each poll was checked",
          len(results) - before == 2, f"{len(results) - before} checks for one wait")
    check("the machine was handed every process at once: one call, the whole list",
          [bare(x) for x in card.ended] == [four], str(card.ended))
    ended_ = [(e["kind"], e["process"]["pid"]) for e in c.events
              if e["kind"].startswith("process_")]
    check("each outcome is recorded: ended, already gone, left alone, not ended",
          ended_ == [("process_ended", 18344), ("process_gone", 18377),
                     ("process_left_alone", 18390), ("process_not_ended", 18400)], str(ended_))
    (rec,) = events_of(c, "rental_reclaimed")
    check("...and the line that frees the card names each, and what the card reads after",
          all(w in rec["detail"] for w in ("no heartbeat for 11s",
                                           "ended its process 18344 (load-k3x9m2p7)",
                                           "its process 18377 was already gone", "left alone",
                                           "had not gone", "the card is back to 1000 MiB in use "
                                           "(1000 when the rental was granted)")), rec["detail"])
    offered = [e for e in c.events if e["kind"] == "ticket_offered" and e["ticket_id"] == tb]
    last_end = max(e["seq"] for e in c.events if e["kind"].startswith("process_"))
    check("the card goes to the next ticket only after that",
          len(offered) == 1 and offered[0]["seq"] > rec["seq"] > last_end
          and line(p) == [(tb, "offered")], str(line(p)))

    # the machine fails while it ends them: the card still comes back, saying why
    broken = Card()
    broken.fails = "the machine could not be asked"
    c2 = Coordinator(tmp / "reclaim-broken", ttl_s=10, hardware=broken, clock=clk)
    _, k2 = granted(Probe(c2), W)
    c2.heartbeat(k2, processes=[PROC])
    clk.t += 11
    c2.reap()
    check("a machine that fails while it ends them does not keep the card from the next ticket",
          until(lambda: c2.state()[1]["rental"] is None, 5), str(c2.state()[1]["rental"]))
    (not_ended,) = events_of(c2, "process_not_ended")
    check("...and the record says why its process was not ended",
          "the machine could not be asked" in not_ended["detail"]
          and not_ended["process"]["pid"] == 18344, str(not_ended))

    # a machine whose answer does not add up: every process is on the record, not ended
    for name, mangle, why in (
            ("reclaim-short", lambda out: out[:1], "fewer outcomes than processes"),
            ("reclaim-word", lambda out: [dict(o, outcome="vanished") for o in out],
             "an outcome it has no word for")):
        odd = Card()
        odd.mangle = mangle
        c3 = Coordinator(tmp / name, ttl_s=10, hardware=odd, clock=clk)
        _, k3 = granted(Probe(c3), W)
        c3.heartbeat(k3, processes=[PROC, proc(18377, "load-m4n8q2r5", model="beta-3b")])
        clk.t += 11
        c3.reap()
        until(lambda: c3.state()[1]["rental"] is None, 5)
        check(f"a machine answering {why} ends no process on the record: each one it named is "
              f"process_not_ended",
              [(e["kind"], e["process"]["pid"]) for e in c3.events
               if e["kind"].startswith("process_")]
              == [("process_not_ended", 18344), ("process_not_ended", 18377)],
              str([e for e in c3.events if e["kind"].startswith("process_")]))

    # the card's memory comes back a moment after its processes are ended
    slow = Card(used=1000)
    c7 = Coordinator(tmp / "reclaim-memory", ttl_s=10, hardware=slow, clock=clk)
    p7 = Probe(c7)
    _, k7 = granted(p7, W)
    t7, _, _ = asked(p7, I, "image")
    c7.heartbeat(k7, processes=[PROC])
    slow.readings = [6000, 6000, 1000]
    clk.t += 11
    c7.reap()
    until(lambda: c7.state()[1]["rental"] is None, 5)
    (rec7,) = events_of(c7, "rental_reclaimed")
    check("a reclaim waits for the card's memory to come back, looking afresh, before the card "
          "goes to the next ticket - and says what it read",
          slow.readings == [1000] and "the card is back to 1000 MiB in use (1000 when the "
          "rental was granted)" in rec7["detail"] and line(p7) == [(t7, "offered")],
          f"{rec7['detail']}; readings left {slow.readings}")

    # the coordinator dies in the middle of a reclaim
    d = tmp / "reclaim-restart"
    hung = Card()
    hung.hold = threading.Event()
    c5 = Coordinator(d, ttl_s=10, hardware=hung, clock=clk)
    t5, k5 = granted(Probe(c5), W)
    c5.heartbeat(k5, processes=[PROC])
    clk.t += 11
    c5.reap()
    until(lambda: hung.waiting, 2)
    dead = died(c5)
    again = Card()
    c6 = Coordinator(d, ttl_s=10, hardware=again, clock=clk)
    check("a coordinator restarted in the middle of a reclaim ends the processes again, on proof",
          until(lambda: c6.state()[1]["rental"] is None, 5)
          and [bare(x) for x in again.ended] == [[PROC]], str(again.ended))
    s, b = Probe(c6).heartbeat(k5)
    check("...and its holder is still told: reclaimed", s == 409 and b["reason"] == "reclaimed",
          str(b))
    reb = [e for e in events_of(c6, "state_rebuilt") if e.get("ticket_id") == t5]
    check("...and the rebuild says so", len(reb) == 1 and "being taken back" in reb[0]["detail"],
          str(reb))
    hung.hold.set()                                     # the dead one's thread may go
    until(lambda: c5.state()[1]["rental"] is None, 5)
    seqs = [e["seq"] for e in record_of(d / "events.jsonl")]
    check("...and the record it keeps is its own: the stopped one's thread, finishing its "
          "reclaim, wrote none of it", seqs == sorted(set(seqs))
          and "rental_reclaimed" in [e["kind"] for e in record_of(dead)],
          f"seqs {seqs}; the stopped one wrote {[e['kind'] for e in record_of(dead)]}")
    for e in c.events + c2.events + c6.events:
        bad = V.check(e, {"ref": "event"}, SHAPES)
        if bad:
            check("every event of a reclaim is in the declared shape", False, f"{e}: {bad}")
            break
    else:
        check("every event of a reclaim is in the declared shape", True)


def checked_release(tmp: Path) -> None:
    """A release is answered once every process the rental still names is
    gone and - where the card can be read - its memory is back to what the
    card read when the rental was granted; within the settle time. What is
    still there after it is ended on proof, and the record names each. The
    card goes to the next ticket only after that."""
    clk = Clock()

    def one(name, card, settle_s=2.0, named=(PROC,)):
        c = Coordinator(tmp / name, ttl_s=10, hardware=card, clock=clk, settle_s=settle_s)
        p = Probe(c)
        _, k = granted(p, W, "writer")
        if named:
            p.heartbeat(k, processes=list(named))
        tb, _, _ = asked(p, I, "image")
        return c, p, k, tb

    # the process goes a moment after the release is asked
    card = Card(used=1000)
    c, p, k, tb = one("release-waits", card)
    card.up = {PROC["pid"]}
    box = {}
    t0 = time.monotonic()
    releasing = threading.Thread(target=lambda: box.update(got=p.release(k)), daemon=True)
    releasing.start()
    time.sleep(0.3)
    s, st = p.state()
    check("while a release is checked, the rental is `releasing` and the card is offered to no "
          "one", releasing.is_alive() and st["rental"]["state"] == "releasing"
          and line(p) == [(tb, "waiting")], str(st["rental"]))
    s, b = p.heartbeat(k)
    check("...and its holder is told at once that it gave the card back: released",
          (s, b.get("reason")) == (409, "released"), str(b))
    going = events_of(c, "rental_releasing")
    check("the release is on the record the moment it is asked: what the rental names, and that "
          "its end is checked", len(going) == 1 and bare(going[0]["processes"]) == [PROC]
          and going[0]["actor"] == W and going[0]["holder"] == "writer"
          and "given back by its holder; its end is checked - its 1 process gone"
          in going[0]["detail"], str(going))
    card.up.discard(PROC["pid"])
    releasing.join(3)
    took = time.monotonic() - t0
    s, b = box.get("got", (0, {}))
    check("a release is answered once the process its rental named is gone - and not before",
          s == 200 and 0.3 <= took < 1.5, f"{s} after {took:.2f}s")
    check("...answering the rental released, with what it named",
          b["released"]["state"] == "released" and bare(b["released"]["processes"]) == [PROC],
          str(b))
    check("...having ended nothing: it went by itself", card.ended == [], str(card.ended))
    (rel,) = events_of(c, "rental_released")
    check("...and the card goes to the next ticket only then",
          line(p) == [(tb, "offered")] and "had gone" in rel["detail"], str(rel))
    check("...its release begun on the record before the line that ends it",
          len(going) == 1 and going[0]["seq"] < rel["seq"], f"{going} / {rel}")

    # the memory comes back after the process has gone
    card = Card(used=1000)
    c, p, k, tb = one("release-memory", card)
    at_claim = card.fresh
    card.readings = [6000, 6000, 1100]
    t0 = time.monotonic()
    s, b = p.release(k)
    took = time.monotonic() - t0
    (rel,) = events_of(c, "rental_released")
    check("where the card can be read, a release waits for its memory to come back to what it "
          "read when the rental was granted - give or take - reading it afresh",
          s == 200 and card.readings == [1100] and 0.2 <= took < 1.8
          and "the card is back to 1100 MiB in use (1000 when the rental was granted)"
          in rel["detail"], f"{rel['detail']} after {took:.2f}s")
    check("...the card read afresh when the rental was claimed - what its end is checked "
          "against - and at each of the check's three looks: never an answer kept from before",
          (at_claim, card.fresh) == (1, 4), f"{at_claim} fresh at the claim, {card.fresh} after")

    # the card cannot be read: the processes gone suffice
    card = Card(used=None)
    c, p, k, tb = one("release-card-unknown", card)
    t0 = time.monotonic()
    s, b = p.release(k)
    took = time.monotonic() - t0
    (rel,) = events_of(c, "rental_released")
    check("where the card cannot be read, the processes gone suffice: answered at once",
          s == 200 and took < 0.5 and "the card was not asked" in rel["detail"],
          f"{rel['detail']} after {took:.2f}s")

    # the memory never comes back within the settle time: answered after it, saying so
    card = Card(used=1000)
    c, p, k, tb = one("release-memory-never", card, settle_s=0.6)
    card.readings = [6000]
    t0 = time.monotonic()
    s, b = p.release(k)
    took = time.monotonic() - t0
    (rel,) = events_of(c, "rental_released")
    check("memory that does not come back holds a release no longer than the settle time - a "
          "setting - and the record says what the card read",
          s == 200 and 0.55 <= took < 1.5 and "the card has 6000 MiB in use 0.6s on, 5000 more "
          "than when the rental was granted" in rel["detail"], f"{rel['detail']} after {took:.2f}s")

    # a straggler: still there after the settle time, so ended on proof
    card = Card(used=None)
    c, p, k, tb = one("release-straggler", card, settle_s=0.5,
                      named=(PROC, proc(18377, "load-m4n8q2r5", model="beta-3b")))
    card.up = {PROC["pid"]}
    t0 = time.monotonic()
    s, b = p.release(k)
    took = time.monotonic() - t0
    check("a process still there after the settle time is ended on its proof - the one that "
          "went by itself is not", s == 200 and 0.45 <= took < 1.5
          and [bare(x) for x in card.ended] == [[PROC]], f"{card.ended} after {took:.2f}s")
    (ended_,) = events_of(c, "process_ended")
    (rel,) = events_of(c, "rental_released")
    check("...and the record names it, on a line of its own and on the release's",
          ended_["process"]["pid"] == 18344 and ended_["seq"] < rel["seq"]
          and "1 of its 2 processes had gone" in rel["detail"]
          and "ended its process 18344 (load-k3x9m2p7)" in rel["detail"], str(rel))
    check("...before the card goes to the next ticket", line(p) == [(tb, "offered")],
          str(line(p)))

    # a machine that cannot say what is alive: taken as still there, never as gone
    card = Card(used=None)

    def cannot_tell(processes):
        raise RuntimeError("the machine could not be asked")
    card.alive = cannot_tell
    c, p, k, tb = one("release-cannot-tell", card, settle_s=0.5)
    t0 = time.monotonic()
    s, b = p.release(k)
    took = time.monotonic() - t0
    check("a machine that cannot say whether the processes have gone keeps the release waiting "
          "the settle time, and then ends them on proof - never takes them as gone",
          s == 200 and took >= 0.45 and [bare(x) for x in card.ended] == [[PROC]],
          f"{card.ended} after {took:.2f}s")

    # nothing to ask: a coordinator with no machine answers at once, and says what it could not
    c, p, k, tb = one("release-no-machine", NoHardware())
    t0 = time.monotonic()
    s, b = p.release(k)
    took = time.monotonic() - t0
    (rel,) = events_of(c, "rental_released")
    check("with no machine to ask, a release is answered at once, and the record says what was "
          "not asked", s == 200 and took < 0.5
          and "no machine configured: whether its 1 process had gone was not asked"
          in rel["detail"], f"{rel['detail']} after {took:.2f}s")
    c, p, k, tb = one("release-nothing-named", Card(used=None), named=())
    s, b = p.release(k)
    (rel,) = events_of(c, "rental_released")
    check("a rental that names nothing is released at once, and says so",
          s == 200 and "it named no process on the card" in rel["detail"]
          and [e["detail"] for e in events_of(c, "rental_releasing")]
          == ["given back by its holder; it names no process on the card"], str(rel))

    # the coordinator stops in the middle of a checked release
    d = tmp / "release-restart"
    first = Card(used=None)
    first.up = {PROC["pid"]}
    c1 = Coordinator(d, ttl_s=10, hardware=first, clock=clk, settle_s=5.0)
    p1 = Probe(c1)
    t1, k1 = granted(p1, W, "writer")
    p1.heartbeat(k1, processes=[PROC])
    t2, _, _ = asked(p1, I, "image")
    threading.Thread(target=lambda: c1.release(k1), daemon=True).start()
    until(lambda: (c1.state()[1]["rental"] or {}).get("state") == "releasing", 2)
    dead = died(c1)
    card = Card(used=None)
    card.up = {PROC["pid"]}
    c2 = Coordinator(d, ttl_s=10, hardware=card, clock=clk, settle_s=5.0)
    p2 = Probe(c2)
    s, st = p2.state()
    check("a coordinator restarted in the middle of a release rebuilds the rental as being given "
          "back, naming what it named - never as held",
          st["rental"] is not None and st["rental"]["id"] == t1
          and st["rental"]["state"] == "releasing" and bare(st["rental"]["processes"]) == [PROC],
          str(st["rental"]))
    s, b = p2.heartbeat(k1)
    check("...its holder is told it gave the card back: released",
          (s, b.get("reason")) == (409, "released"), str(b))
    check("...the card goes to no one while its end is checked again",
          line(p2) == [(t2, "waiting")], str(line(p2)))
    reb = [e for e in events_of(c2, "state_rebuilt") if e.get("ticket_id") == t1]
    check("...and the rebuild says so", len(reb) == 1 and "being given back" in reb[0]["detail"]
          and "its end is checked again" in reb[0]["detail"], str(reb))
    card.up.discard(PROC["pid"])
    check("...then, its process gone, the release ends, and the card goes to the next ticket - "
          "with the rental's lifetime still to run: never waited out",
          until(lambda: c2.state()[1]["rental"] is None, 3) and line(p2) == [(t2, "offered")]
          and p2.state()[1]["rental"] is None, str(p2.state()[1]))
    rel = events_of(c2, "rental_released")
    check("...the record says it was released, never taken back",
          len(rel) == 1 and rel[0]["ticket_id"] == t1 and "its 1 process had gone"
          in rel[0]["detail"] and not events_of(c2, "rental_reclaiming")
          and not events_of(c2, "rental_reclaimed"), str(rel))
    first.up.discard(PROC["pid"])                     # the stopped one's release may end
    until(lambda: c1.state()[1]["rental"] is None, 6)
    seqs = [e["seq"] for e in record_of(d / "events.jsonl")]
    check("...and the record it keeps is its own: the stopped one's thread, finishing its "
          "release, wrote none of it", seqs == sorted(set(seqs))
          and "rental_released" in [e["kind"] for e in record_of(dead)],
          f"seqs {seqs}; the stopped one wrote {[e['kind'] for e in record_of(dead)]}")
    for e in c.events + c2.events:
        bad = V.check(e, {"ref": "event"}, SHAPES)
        if bad:
            check("every event of a release is in the declared shape", False, f"{e}: {bad}")
            break
    else:
        check("every event of a release is in the declared shape", True)


def bounded(tmp: Path) -> None:
    """What the coordinator keeps of the tickets that ended - why each ended,
    and its token's hash - is bounded: the newest, and no more, for the whole
    of its life and across a restart. An older one's token is simply
    unknown."""
    clk = Clock()
    d = tmp / "bounded"
    c = Coordinator(d, ttl_s=100_000, hardware=Card(), clock=clk, keep_closed=3)
    p = Probe(c)
    _, hk = granted(p, W)                               # the card is held: every ask waits
    ended = []
    for actor in (A1, A2, A3, A4, I, X):
        clk.t += 1
        tid, token, _ = asked(p, actor)
        p.cancel(token)
        ended.append((tid, token))
    for tid, token in ended[:3]:
        s, b = p.wait(tid, token, 0)
        check(f"a ticket that ended before the newest three is forgotten: unknown ({tid})",
              s == 409 and b["reason"] == "unknown_ticket", str(b))
    for tid, token in ended[3:]:
        s, b = p.wait(tid, token, 0)
        check(f"the newest three that ended are kept, and told why ({tid})",
              s == 409 and b["reason"] == "cancelled", str(b))
    shas = [token_hash(token) for _, token in ended]

    def kept(coordinator) -> str:
        """What the coordinator keeps besides its record."""
        return repr({k: v for k, v in vars(coordinator).items() if k != "events"})
    check("...and it keeps the hash of no token it forgot - only of those it keeps",
          not any(x in kept(c) for x in shas[:3]) and all(x in kept(c) for x in shas[3:]))
    check("the rental held all along is untouched", p.heartbeat(hk)[0] == 200)
    c2 = Coordinator(d, ttl_s=100_000, hardware=Card(), clock=clk, keep_closed=3)
    check("a restart keeps the same bound over its whole record",
          not any(x in kept(c2) for x in shas[:3]) and all(x in kept(c2) for x in shas[3:])
          and Probe(c2).wait(*ended[0], 0)[1]["reason"] == "unknown_ticket"
          and Probe(c2).wait(*ended[-1], 0)[1]["reason"] == "cancelled")
    check("by default the newest thousand are kept",
          Coordinator(tmp / "bounded-default", hardware=Card(), clock=clk).keep_closed == 1000)

    # the ports given back or taken back: what each was is kept the same way
    d = tmp / "bounded-ports"

    def ports_c():
        return Coordinator(d, ttl_s=10, hardware=Card(), clock=clk, keep_closed=3,
                           ports=(51000, 51009), port_free=Machine())
    c3 = ports_c()
    p3 = Probe(c3)
    ended = []
    for actor in (A1, A2, A3, A1, A4):                  # A1 twice: its port closed again
        port_ = p3.request_port(actor, "model")[1]["port"]["port"]
        p3.release_port(port_, actor)
        ended.append((port_, actor))
    quiet = p3.request_port(I, "model", 2)[1]["port"]["port"]
    clk.t += 3
    c3.reap()                                           # ...and one taken back

    def said(p, port_, actor) -> str:
        s, b = p.heartbeat_port(port_, actor)
        return b.get("reason") if s == 409 else f"{s}"
    for name, coordinator in (("", c3), ("after a restart, ", ports_c())):
        p_ = Probe(coordinator)
        got = [said(p_, *ended[i]) for i in (1, 2)]
        check(f"{name}a port closed before the newest three is forgotten: its holder is told it "
              f"holds no such port (unknown_port)", got == ["unknown_port"] * 2, str(got))
        got = [said(p_, *ended[i]) for i in (3, 4)] + [said(p_, quiet, I)]
        check(f"{name}the newest three closed are kept, and told why - a port closed again "
              f"counting from its last close", got == ["released", "released", "reclaimed"],
              str(got))


def up_and_version(tmp: Path) -> None:
    """A server asks whether the coordinator is up at a route of its own,
    answered with that and nothing more; the operator's /state says which
    contract the coordinator runs, as a server's health does."""
    c = Coordinator(tmp / "up", ttl_s=15, hardware=Card(used=100), clock=Clock())
    p = Probe(c)
    granted(p, W, "writer-app")
    asked(p, I, "image-app")
    s, b = p.up()
    # CHANGED ON PURPOSE (S13b, PLAN-4 D13): and the version of the contract
    # it runs - a server can tell whether the two speak one contract
    check("/up answers that the coordinator is up, and the version of the contract it runs - "
          "and nothing of the card, its rental, the tickets or the ports",
          (s, b) == (200, {"up": True, "contract_version": V.contract_version(CONTRACT)}),
          str(b))
    s, st = p.state()
    check("/state carries the version of the contract the coordinator runs",
          st["contract_version"] == V.contract_version(CONTRACT)
          and len(st["contract_version"]) == 12, str(st.get("contract_version")))


class Held:
    """A process as the machine holds it open while it ends it - here a
    stand-in: `state` is alive, gone or unopened; once ended it goes in `dies`
    seconds, or never (None)."""
    def __init__(self, pid, state="alive", dies=0.0, why=None):
        self.pid, self.state, self.dies, self.why = pid, state, dies, why
        self.terminated = False

    def terminate(self):
        self.terminated = True
        return None

    def wait(self, seconds):
        if self.dies is None or self.dies > seconds:
            time.sleep(seconds)
            return False
        time.sleep(self.dies)
        return True

    def close(self):
        pass


def this_machine(tmp: Path) -> None:
    """The machine as the coordinator asks it: the card through nvidia-smi
    (here, a stand-in answer); which named processes are still alive; and a
    rental's processes ended ON PROOF - their command lines read in ONE query,
    each ended in parallel and waited for with its own bound - for real, on
    this suite's own children."""
    asked_, now = [], [100.0]
    m = ThisMachine(query=lambda: asked_.append(1) or "18874, 24576\n", clock=lambda: now[0])
    check("the card as nvidia-smi says it",
          m.probe() == {"known": True, "used_mib": 18874, "total_mib": 24576}, str(m.probe()))
    m.probe()
    check("...asked once for two looks within a couple of seconds", len(asked_) == 1, str(asked_))
    now[0] += 2.5
    m.probe()
    check("...and again once its answer is old", len(asked_) == 2, str(asked_))
    m.probe(fresh=True)
    check("...and at once when a fresh answer is asked for", len(asked_) == 3, str(asked_))
    for said in (None, "", "[N/A], [N/A]", "a card is missing"):
        got = ThisMachine(query=lambda: said).probe()
        check(f"an answer it cannot read ({said!r}) is not known - never an empty card",
              got == {"known": False, "used_mib": None, "total_mib": None}, str(got))
    handed = []
    m2 = ThisMachine(query=lambda: None,
                     end=lambda ps: handed.append(("end", ps))
                     or [{"outcome": "ended", "detail": "ended as asked"}],
                     alive=lambda ps: handed.append(("alive", ps)) or [False])
    check("ending a rental's processes hands the whole list to the machine, in one call",
          m2.end([PROC]) == [{"outcome": "ended", "detail": "ended as asked"}]
          and handed == [("end", [PROC])], str(handed))
    handed.clear()
    check("...and so does asking which of them are still alive",
          m2.alive([PROC]) == [False] and handed == [("alive", [PROC])], str(handed))

    # the operating system stood in for: what is read, and how long each takes to go
    queries = []
    lines = {1001: "llama-server --alias load-a2b3c4d5", 1002: "llama-server --alias load-a2b3c4d6",
             1003: "something else entirely", 1005: None, 1006: "whisper-server /load-a2b3c4d9"}
    held_ = {1001: Held(1001, dies=0.5), 1002: Held(1002, dies=0.5), 1003: Held(1003),
             1004: Held(1004, state="gone"), 1005: Held(1005), 1006: Held(1006, dies=None),
             1007: Held(1007, state="unopened", why="error 5")}
    named_ = [proc(1001, "load-a2b3c4d5"), proc(1002, "load-a2b3c4d6"),
              proc(1003, "load-a2b3c4d7"), proc(1004, "load-a2b3c4d8"),
              proc(1005, "load-a2b3c4e2"), proc(1006, "load-a2b3c4d9"),
              proc(1007, "load-a2b3c4e3")]

    def read(pids):
        queries.append(list(pids))
        return {pid: lines.get(pid) for pid in pids}
    # CHANGED ON PURPOSE (H6, H4's R1): what the query could not read (1005) is
    # asked again - no pause here, so the ends' own timing is what is measured
    pause, M.QUERY_PAUSE_S = M.QUERY_PAUSE_S, 0.0
    try:
        t0 = time.monotonic()
        got = M.end_all_on_proof(named_, wait_s=0.8, read=read, hold=lambda pid: held_[pid])
        took = time.monotonic() - t0
    finally:
        M.QUERY_PAUSE_S = pause
    check("the command lines of every named process still there are read in ONE query - and "
          "only the one it could not read is asked again, until its asks are spent",
          queries == [[1001, 1002, 1003, 1005, 1006]] + [[1005]] * (M.QUERY_ROUNDS - 1)
          and M.QUERY_ROUNDS >= 2, str(queries))
    check("each outcome, in order: ended, ended, left alone (not its proof), already gone, left "
          "alone (unreadable), not gone within its bound, left alone (could not be opened)",
          [g["outcome"] for g in got] == ["ended", "ended", "left_alone", "gone", "left_alone",
                                          "not_ended", "left_alone"], str(got))
    check("...each said in words",
          got[0]["detail"] == "ended its process 1001 (load-a2b3c4d5)"
          and "does not carry load-a2b3c4d7" in got[2]["detail"]
          and got[3]["detail"] == "its process 1004 was already gone"
          and "could not be read; left alone" in got[4]["detail"]
          and f"asked {M.QUERY_ROUNDS} times" in got[4]["detail"]
          and got[5]["detail"] == "ended its process 1006 (load-a2b3c4d9), and it had not gone "
                                  "0.8s later"
          and "could not be opened (error 5); left alone" in got[6]["detail"], str(got))
    check("...and only a process carrying its proof was ended",
          [pid for pid, h in held_.items() if h.terminated] == [1001, 1002, 1006],
          str({pid: h.terminated for pid, h in held_.items()}))
    check("the processes are ended in parallel, each waited for with its own bound: the whole "
          "takes about the longest one, not their sum", 0.75 <= took < 1.3,
          f"{took:.2f}s for two ends of 0.5s and one never gone, each bounded at 0.8s")
    measured.append(f"seven named processes, three ended in parallel: {took:.2f}s "
                    f"(one bound is 0.8s)")

    class Hung(Held):
        """An end that does not keep its own bound: its wait outlasts it."""
        def wait(self, seconds):
            time.sleep(3.0)
            return False
    hung = {pid: Hung(pid) for pid in (2001, 2002, 2003)}
    margin, M.END_JOIN_MARGIN_S = M.END_JOIN_MARGIN_S, 0.3
    try:
        t0 = time.monotonic()
        got = M.end_all_on_proof([proc(pid, "load-a2b3c4d5") for pid in hung], wait_s=0.2,
                                 read=lambda pids: {x: "llama-server --alias load-a2b3c4d5"
                                                    for x in pids},
                                 hold=lambda pid: hung[pid])
        took = time.monotonic() - t0
    finally:
        M.END_JOIN_MARGIN_S = margin
    check("ends that outlast their own bound are given up at ONE deadline for them all - not a "
          "bound each, one after another, which adds up to their sum",
          [g["outcome"] for g in got] == ["not_ended"] * 3 and 0.45 <= took < 1.0
          and got[0]["detail"] == "its process 2001 (load-a2b3c4d5) was not ended within 0.5s",
          f"{took:.2f}s for three ends given 0.2s and a margin of 0.3s: {got}")

    # H4's R1: a reclaim whose one query of the command lines did not answer
    # left the rental's processes alone and never asked again - safe, but they
    # stayed on the card for good. Provoked: the query does not answer the
    # first time; the process, held open all the while, is ended on its proof
    # once it is read
    asks = []
    answers = iter([{3001: None}, {3001: "llama-server --alias load-a2b3c4d5"}])

    def late(pids):
        asks.append(list(pids))
        return next(answers)
    once = {3001: Held(3001)}
    t0 = time.monotonic()
    got = M.end_all_on_proof([proc(3001, "load-a2b3c4d5")], wait_s=0.5, read=late,
                             hold=lambda pid: once[pid])
    took = time.monotonic() - t0
    check("a query of the command lines that did not answer is asked again - the process held "
          "open all the while - and the process carrying its proof is ended once it is read",
          [g["outcome"] for g in got] == ["ended"] and asks == [[3001], [3001]]
          and once[3001].terminated, f"{got}; asked {asks}")
    check("...after a pause, never at once", took >= M.QUERY_PAUSE_S, f"{took:.2f}s")
    asks.clear()
    never = {3002: Held(3002)}
    pause, M.QUERY_PAUSE_S = M.QUERY_PAUSE_S, 0.01
    try:
        got = M.end_all_on_proof([proc(3002, "load-a2b3c4d5")], wait_s=0.5,
                                 read=lambda pids: asks.append(list(pids))
                                 or {p: None for p in pids}, hold=lambda pid: never[pid])
    finally:
        M.QUERY_PAUSE_S = pause
    check("...one it never answers is asked a bounded number of times, then left alone, and "
          "said to be - never ended on a guess",
          [g["outcome"] for g in got] == ["left_alone"] and len(asks) == M.QUERY_ROUNDS
          and not never[3002].terminated and f"asked {M.QUERY_ROUNDS} times" in got[0]["detail"],
          f"{got}; asked {asks}")

    proof = "load-" + ids.mint_actor("load")[5:]
    sleeper = [sys.executable, "-c", "import time; time.sleep(120)"]
    mine = subprocess.Popen(sleeper + [proof], creationflags=NO_WINDOW)
    other = subprocess.Popen(sleeper + [ids.mint_actor("load")], creationflags=NO_WINDOW)
    gone_ = subprocess.Popen(sleeper + [ids.mint_actor("load")], creationflags=NO_WINDOW)
    killed(gone_)
    stranger = ids.mint_actor("load")                   # nobody's command line carries it
    # the machine asks the process list for their command lines, and says so
    # when it could not (UNREAD) - each left alone, never ended on a guess:
    # these checks ask again, since their own processes' lines are there to
    # be read. On a busy machine one query did not answer in its 30 s (wave 3)
    try:
        check("which named processes are alive, asked of this machine",
              M.processes_alive([{"pid": mine.pid}, {"pid": gone_.pid}]) == [True, False])
        t0 = time.monotonic()
        got = asked_again(lambda: M.end_all_on_proof(
            [{"pid": mine.pid, "proof": proof}, {"pid": other.pid, "proof": stranger},
             {"pid": gone_.pid, "proof": ids.mint_actor("load")}]), read_whole,
            "the command lines of three processes of this suite's own")
        took = time.monotonic() - t0
        check("for real, in one call: the process carrying its proof is ended and waited for "
              "until it has gone; one alive without it is left alone; one gone is said to be",
              [g["outcome"] for g in got] == ["ended", "left_alone", "gone"]
              and mine.poll() is not None and other.poll() is None, str(got))
        measured.append(f"three processes on proof in one call, their command lines read in "
                        f"one query: {took:.2f}s")
    finally:
        for child in (mine, other):
            killed(child)
    sleeper = [sys.executable, "-c", "import time; time.sleep(120)"]
    mine = subprocess.Popen(sleeper + [proof], creationflags=NO_WINDOW)
    try:
        t0 = time.monotonic()
        said = asked_again(lambda: end_on_proof(mine.pid, proof), read_whole,
                           "the command line of a process of this suite's own")
        took = time.monotonic() - t0
        check("a process that carries the proof is ended, and waited for until it has gone",
              said == f"ended its process {mine.pid} ({proof})" and mine.poll() is not None,
              said)
        measured.append(f"a process ended on proof: {took:.2f}s, its command line read first")
    finally:
        killed(mine)
    other = subprocess.Popen(sleeper + ["load-somebody-else"], creationflags=NO_WINDOW)
    try:
        said = asked_again(lambda: end_on_proof(other.pid, proof), read_whole,
                           "the command line of a process of this suite's own")
        check("one alive without the proof is left alone, and the record will say so",
              "left alone" in said and "does not carry" in said and other.poll() is None, said)
    finally:
        killed(other)
    said = end_on_proof(other.pid, proof)
    check("one already gone is said to be gone", "already gone" in said, said)

    # provoked: the process list does not answer the first time it is asked,
    # as on a busy machine - the machine says it could not read the proof and
    # leaves the process alone; asked again, it reads it, and ends it
    real_read, refused = M.command_lines, []

    def not_answering_once(pids):
        if not refused:
            refused.append(list(pids))
            return {int(pid): None for pid in pids}
        return real_read(pids)
    mine = subprocess.Popen(sleeper + [proof], creationflags=NO_WINDOW)
    M.command_lines = not_answering_once
    try:
        said = asked_again(lambda: end_on_proof(mine.pid, proof), read_whole,
                           "the command line of a process of this suite's own")
        check("what the process list could not answer is asked again, never read as its answer: "
              "the process carrying its proof is ended once it is read",
              said == f"ended its process {mine.pid} ({proof})" and refused == [[mine.pid]]
              and mine.poll() is not None, f"{said}; refused {refused}")
    finally:
        M.command_lines = real_read
        killed(mine)


# -------------------------------------------------------------- processes

def of_its_own(argv: list) -> list:
    """A coordinator this suite starts names a card lock of its own
    (`--card-lock`), NEVER the machine's: the machine's is its owner's
    applications' - one of them may hold it - and two copies of this suite
    running at once found each other's coordinator holding it ("another
    coordinator runs on this machine") and went red. The suite's own process
    has a tripwire on the machine's lock; a coordinator it starts is another
    process, so its command line is held to it here. -> argv"""
    if any(str(a).endswith("coordinator.py") for a in argv) and "--card-lock" not in argv:
        raise AssertionError(f"a coordinator this suite starts names a card lock of its own "
                             f"(--card-lock), never the machine's: {argv}")
    return argv


def start(args: list, expect: str, timeout: float = START_S, clock=time.monotonic):
    """Start a child; wait for a stdout line containing *expect*. -> (proc, line)

    Its script by its whole path, so its command line names this copy's
    folder: what `mutate.py` finds a timed-out suite's children by. Within
    START_S: a holder that did not say "holding" within 10 s was one on a busy
    machine (wave 3), not one that failed. A child that ends without saying
    it is up is said at once - in its own words, with its exit code - never
    waited out to the bound: one said nothing for a whole minute."""
    proc = subprocess.Popen(of_its_own([sys.executable, str(HERE / args[0]), *args[1:]]),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=HERE,
                            encoding="utf-8")
    lines: queue.Queue = queue.Queue()

    def read():
        for l in proc.stdout:
            lines.put(l)
        lines.put(None)                                     # its output has ended
    threading.Thread(target=read, daemon=True).start()
    deadline, said = clock() + timeout, []
    while clock() < deadline:
        try:
            line_ = lines.get(timeout=0.1)
        except queue.Empty:
            continue
        if line_ is None:
            break
        said.append(line_)
        if expect in line_:
            return proc, line_.strip()
    proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(START_S)
    raise RuntimeError(f"{args[0]} never printed {expect!r} (its exit code: {proc.returncode}); "
                       f"it said: {''.join(said)[-600:]!r}")


class LateEnd:
    """A process as `killed` sees it - `poll`, `kill`, `wait` - that ends
    `ends_s` seconds after its kill: a kill the operating system accepted, on
    a machine too busy to finish it at once."""
    pid = 424243

    def __init__(self, ends_s: float = 0.0, ended: bool = False):
        self.ends_s, self.kills = ends_s, 0
        self.returncode = 1 if ended else None

    def poll(self):
        return self.returncode

    def kill(self):
        self.kills += 1

    def wait(self, timeout=None):
        if self.returncode is None and self.kills and (timeout is None or timeout >= self.ends_s):
            self.returncode = 1
        if self.returncode is None:
            raise subprocess.TimeoutExpired(["late"], timeout)
        return self.returncode


def a_late_kill(tmp: Path) -> None:
    """H4's R6: this suite killed its processes and waited for them without a
    bound - a kill that did not take held the suite for as long as it took.
    Its own `killed`: a kill that ends late (16 s, past the 15 s one row
    waited in wave 3) is waited for; one that never ends is said by its pid,
    never waited out; one already ended is not killed again."""
    p = LateEnd(ends_s=16.0)
    check("a kill that ends late is waited for", (killed(p), p.kills) == (1, 1), str(p.kills))
    p = LateEnd(ends_s=1e9)
    try:
        killed(p, within_s=0.2)
        said = "it was answered"
    except AssertionError as e:
        said = str(e)
    check("...one that has not ended within its bound is said, by its pid - never waited out",
          f"process {p.pid} was killed and had not ended 0.2s later" in said, said)
    p = LateEnd(ended=True)
    check("...and one already ended is not killed again", (killed(p), p.kills) == (1, 0),
          str(p.kills))


def the_sweeps_reap(tmp: Path) -> None:
    """H4's R4: once a sweep is over, mutate.py ends every process whose
    command line names its folder - and a process list that did not answer
    was read as "nothing to reap": whatever a mutant left running went on,
    and the run said nothing. Asked again, and then said to be unknown."""
    spec = importlib.util.spec_from_file_location("mutate_under_test", HERE.parent / "mutate.py")
    mut = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mut)
    mut.REAP_PAUSE_S = 0.0                               # this copy's own: no pause here
    out = tmp / "sweep-out"
    asks, ends = [], []
    got = mut.reap(out, listing=lambda: asks.append(1), end=ends.append)
    check("a sweep's reap whose process list does not answer asks again, a bounded number of "
          "times - and then says it could not look (None), never that nothing outlived the run",
          got is None and len(asks) == mut.REAP_ASKS and mut.REAP_ASKS >= 2 and ends == [],
          f"{got}; asked {len(asks)} times; ended {ends}")
    answers = iter([None, f"4242\tpython suite.py {out} --quiet\n4243\tpython another.py\n"])
    got = mut.reap(out, listing=lambda: next(answers), end=ends.append)
    check("...one that answers when asked again is read: the process naming the sweep's folder "
          "is ended, and nothing else", got == [4242] and ends == ["4242"], f"{got}; {ends}")
    # H6's register 4: the reap ended a pid read from a listing - handed on,
    # the moment its process ended, to somebody else's. Provoked: a listing
    # that names, as the sweep's, a live process that is not - its pid handed
    # on between the listing and the end - beside one that is
    def sleeper(*words):
        # this copy's folder on its command line too: a sweep reaps it
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)",
                                 str(HERE), *words], creationflags=NO_WINDOW)
    handed_on, still = sleeper(str(tmp / "somebody-elses")), sleeper(str(out))
    try:
        listed = (f"{handed_on.pid}\tpython suite.py {out} --quiet\n"
                  f"{still.pid}\tpython suite.py {out} --quiet\n")
        got = mut.reap(out, listing=lambda: listed)
        try:
            handed_on.wait(3)
            alive = False
        except subprocess.TimeoutExpired:
            alive = True
        check("a sweep's reap ends a listed pid only while its own command line, read again as "
              "it is ended, still names the sweep's folder: one handed on meanwhile is left "
              "alone", alive and got == [still.pid], f"alive: {alive}; ended {got}")
        try:
            still.wait(30)
            ended = True
        except subprocess.TimeoutExpired:
            ended = False
        check("...and one that still names it is ended", ended, f"pid {still.pid} still runs")
    finally:
        killed(handed_on)
        killed(still)


def busy_machine(tmp: Path) -> None:
    """What this suite does on a busy machine, provoked on purpose with the
    machine stood in for: what the process list could not answer is asked
    again, and said once asked long enough in vain; a start a busy machine
    makes slow is waited for."""
    answers, asked_ = iter([None, None, "the answer"]), []
    got = asked_again(lambda: asked_.append(1) or next(answers), lambda a: a is not None,
                      "a question", pause_s=0.01)
    check("a question the process list could not answer is asked again, never read as its "
          "answer", (got, len(asked_)) == ("the answer", 3), f"{got!r} after {len(asked_)} asks")
    try:
        asked_again(lambda: None, lambda a: a is not None, "whether it is up", within_s=0.2,
                    pause_s=0.01)
        said = None
    except AssertionError as e:
        said = str(e)
    check("...one it never answers is said, never guessed",
          said is not None and "could not tell whether it is up" in said, str(said))
    now = [0.0]
    got = asked_again(lambda: "yes" if now[0] >= 100.0 else None, lambda a: a is not None,
                      "a question", clock=lambda: now[0],
                      sleep=lambda s: now.__setitem__(0, now[0] + s))
    check("...asked again for two minutes: a question first answered 100 s after it was "
          "asked is answered", got == "yes", repr(got))
    # a child that says it is up only once more than ten seconds have passed on
    # the clock start() reads - the bound a holder once outlasted on a busy
    # machine - and the clock stands still from then on
    go, script = tmp / "busy-go", tmp / "busy-start.py"
    script.write_bytes(b"import pathlib, sys, time\n"
                       b"go = pathlib.Path(sys.argv[1])\n"
                       b"while not go.exists():\n"
                       b"    time.sleep(0.02)\n"
                       b"print('up and about', flush=True)\n")
    clock_ = [0.0]

    def slow_clock():
        if clock_[0] < 11.0:
            clock_[0] += 1.0
        elif not go.exists():
            go.write_bytes(b"")
        return clock_[0]
    try:
        child, said = start([str(script), str(go)], "up and about", clock=slow_clock)
        child.wait(START_S)
    except RuntimeError as e:
        said = str(e)
    check("a process that takes more than ten seconds to say it is up - a busy machine's "
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
    try:
        of_its_own([sys.executable, str(HERE / "coordinator.py"), "--port", "30000"])
        said = "it would have been started"
    except AssertionError as e:
        said = str(e)
    kept = of_its_own([sys.executable, str(HERE / "coordinator.py"), "--card-lock", "x.lock"])
    check("a coordinator this suite starts is held to a card lock of its own, never the "
          "machine's: one with no --card-lock is refused before it starts",
          "never the machine's" in said and kept[-2:] == ["--card-lock", "x.lock"], said)


def wait_for(pred, timeout: float) -> float | None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return time.time() - t0
        time.sleep(0.02)
    return None


def wire(url: str, route: str, body=None):
    """One call over the wire, its answer checked against the contract and
    remembered for the coverage."""
    r = ROUTES[route]
    s, b = call(url, r["method"], r["path"], body)
    bad = conforms(route, s, b)
    if bad:
        check(f"contract: {route} -> {s} over the wire", False, "; ".join(bad))
    seen(route, s, b)
    return s, b


def state(url: str):
    return wire(url, "coordinator/state")[1]


def processes(tmp: Path) -> None:
    ttl = 0.6
    port = quiet_port()
    url = f"http://127.0.0.1:{port}"
    data = str(tmp / "p")
    lock = str(tmp / "p-card.lock")         # the row's own lock, never the machine's
    coord, _ = start(["coordinator.py", "--hardware", "none", "--data", data, "--port", str(port),
                      "--ttl", str(ttl), "--card-lock", lock], "listening on")
    procs = [coord]
    try:
        check("a coordinator started with --ttl runs with that lifetime",
              state(url)["limits"]["ttl_s"] == ttl, str(state(url)["limits"]))
        # --- two actors, and one of them is killed mid-rental
        a, _ = start(["holder.py", "--url", url, "--name", "writer-app",
                      "--model", "demo-7b"], "holding")
        procs.append(a)
        check("a holder in its own process takes the card",
              (state(url)["rental"] or {}).get("holder") == "writer-app")
        b = Holder(url, "image-app", "sdxl", patience_s=15, wait_s=5)
        got = {}
        waiter = threading.Thread(target=lambda: got.setdefault("rental", b.acquire()))
        waiter.start()
        wait_for(lambda: b.ticket is not None, 5)
        check("the second application waits while the first is alive, its ticket in line",
              [t["holder"] for t in state(url)["tickets"]] == ["image-app"]
              and b.rental is None, str(state(url)["tickets"]))
        time.sleep(1.0)
        check("...having asked once: it waits, and never asks again",
              b.asks == 1 and b.rental is None, f"asks={b.asks}")
        killed_at = time.time()
        killed(a)
        waiter.join(timeout=15)
        latency = time.time() - killed_at
        check("the second application gets the card after the first is killed",
              (got.get("rental") or {}).get("holder") == "image-app", str(got))
        check(f"...within a lifetime plus slack ({ttl}s + 1s)", latency <= ttl + 1.0,
              f"{latency:.2f}s")
        check("...still on its one ask", b.asks == 1, f"asks={b.asks}")
        measured.append(f"kill -> the next holder granted: {latency:.2f}s (lifetime {ttl}s), "
                        f"on 1 ask and {b.waits} wait(s)")
        s, ev = wire(url, "coordinator/events")
        rec = [e for e in ev["events"] if e["kind"] == "rental_reclaimed"]
        check("the record says the killed holder lost the card",
              len(rec) == 1 and rec[0]["holder"] == "writer-app", str(rec))
        r = b.release()
        check("the second application gives the card back", r and r[0] == 200, str(r))

        # --- two applications under ONE name are two holders, over the wire.
        # Every holder here has a patience: a row never waits without a bound
        # (a holder that forgot its tickets once chased the card for ever)
        first = Holder(url, "writer-app", "demo-7b", for_=CORR, patience_s=10)
        first.acquire()
        first.running(4242, "load-k3x9m2p7")
        check("a holder names the process its rental put on the card, and the state says so",
              bare((state(url)["rental"] or {}).get("processes") or [])
              == [proc(4242, "load-k3x9m2p7")], str(state(url)["rental"]))
        first.running(4343, "load-m4n8q2r5", port=31005)
        check("...and every other it puts there: its heartbeat carries the whole list, at once",
              bare((state(url)["rental"] or {}).get("processes") or [])
              == [proc(4242, "load-k3x9m2p7"), proc(4343, "load-m4n8q2r5", port=31005)],
              str(state(url)["rental"]))
        first.stopped(4242)
        check("...and one it took off is off the list at once",
              bare((state(url)["rental"] or {}).get("processes") or [])
              == [proc(4343, "load-m4n8q2r5", port=31005)], str(state(url)["rental"]))
        s, up_ = wire(url, "coordinator/up")
        # CHANGED ON PURPOSE (S13b, PLAN-4 D13): /up is the one route an
        # application's server asks, and it now carries the contract's version
        # the coordinator runs - a server can tell it speaks the same contract
        check("a coordinator process answers /up: that it is up, the version of the contract it "
              "runs, and nothing more",
              (s, up_) == (200, {"up": True, "contract_version": V.contract_version(CONTRACT)}),
              f"{s} {up_}")
        held_ = state(url)["rental"] or {}
        check("a holder's rental says whose it is and what request it is for",
              (held_.get("actor"), held_.get("for")) == (first.actor, CORR), str(held_))
        second = Holder(url, "writer-app", "demo-7b", patience_s=0.6, wait_s=5)
        t0 = time.monotonic()
        try:
            second.acquire()
            refused = None
        except Refused as e:
            refused = e
        took = time.monotonic() - t0
        check("a second application under the same NAME waits, and is not handed the first "
              "one's rental", refused is not None and second.rental is None, str(refused))
        check("...until its patience runs out: card_patience, naming no one",
              getattr(refused, "reason", None) == "card_patience" and 0.5 <= took < 3.0
              and first.actor not in str(refused), f"{refused!r} after {took:.2f}s")
        check("...and it gives its ticket up as it goes",
              [t["actor"] for t in state(url)["tickets"]] == []
              and any(e["kind"] == "ticket_cancelled" and e["actor"] == second.actor
                      for e in wire(url, "coordinator/events")[1]["events"]))
        live = Holder(url, "live-app", "whisper-x", patience_s=0)
        t0 = time.monotonic()
        try:
            live.acquire()
            refused = None
        except Refused as e:
            refused = e
        took = time.monotonic() - t0
        check("a patience of 0 is now or not at all: card_busy at once, naming no one",
              getattr(refused, "reason", None) == "card_busy" and took < 1.0
              and "writer-app" not in str(refused) and first.actor not in str(refused),
              f"{refused!r} after {took:.2f}s")
        first.release()

        # --- the contract's bad_request examples, answered word for word
        for key, r in OURS.items():
            for ex in r["examples"]:
                if V.refuses_the_request(ex):
                    s, out = wire(url, key, ex["request"])
                    check(f"the contract's example '{ex['name']}' is answered word for word",
                          (s, out) == (ex["status"], ex["response"]), f"{s} {out}")

        # --- the coordinator itself dies under a holder that must not notice
        h = Holder(url, "writer-app", "demo-7b", patience_s=10)
        rental = h.acquire()
        wait_for(lambda: h.rental["expires_at"] > rental["expires_at"], 3)
        last_beat_before = h.rental["expires_at"]
        killed(coord)
        time.sleep(ttl * 4)
        check("while the coordinator is gone the holder cannot reach it",
              h.unreachable > 0, f"unreachable={h.unreachable}")
        check("...and does NOT conclude it lost the card", h.lost is None, str(h.lost))
        downtime_beats = h.unreachable
        # A connection refused on Windows takes ~2s. During an outage each
        # bounded attempt costs the interval's wait PLUS the interval's
        # timeout - two intervals, a third of a lifetime each - so four
        # lifetimes hold ~6 attempts. An unbounded heartbeat manages ~1,
        # blocked in connect. Four separates the two with room for timing.
        check("...and keeps its heartbeat cadence while the coordinator is gone",
              downtime_beats >= 4, f"{downtime_beats} attempts in {ttl * 4:.1f}s")
        coord, _ = start(["coordinator.py", "--hardware", "none", "--data", data,
                          "--port", str(port), "--ttl", str(ttl), "--card-lock", lock],
                         "listening on")
        procs.append(coord)
        back = wait_for(lambda: h.rental["expires_at"] > last_beat_before, 5)
        check("the holder's heartbeat lands again once the coordinator is back: its token is "
              "known again", back is not None, "no heartbeat succeeded after the restart")
        st = state(url)
        check("after the restart the same holder holds the same rental",
              (st["rental"] or {}).get("id") == rental["id"], str(st["rental"]))
        check("...its heartbeats never stopped working from its point of view",
              h.lost is None, str(h.lost))
        s, ev = wire(url, "coordinator/events")
        check("the rebuild is on the record",
              any(e["kind"] == "state_rebuilt" and e.get("ticket_id") == rental["id"]
                  for e in ev["events"]))
        measured.append(f"coordinator down for {ttl * 4:.1f}s: {downtime_beats} heartbeats "
                        f"failed, the rental survived, 0 rentals lost")
        r = h.release()
        check("...and it gives the card back cleanly after all that", r and r[0] == 200, str(r))

        # --- the ordinary way out: a context manager releasing on exit
        w = Holder(url, "image-app", "sdxl", patience_s=10, wait_s=5)
        order_ = []
        with Holder(url, "writer-app", "demo-7b", patience_s=10):
            t = threading.Thread(target=lambda: order_.append(w.acquire()))
            t.start()
            wait_for(lambda: w.ticket is not None, 5)
            left = time.time()
        t.join(timeout=10)
        handover = time.time() - left
        check("leaving a `with` block hands the card to the next waiter promptly",
              order_ and handover < 1.0, f"{handover:.2f}s")
        check("...which waited on its one ask, and claimed the moment it was offered",
              w.asks == 1, f"asks={w.asks}")
        measured.append(f"clean release -> the next holder granted: {handover:.2f}s")
        w.release()
    finally:
        for child in procs:
            if child.poll() is None:
                killed(child)


#: where this suite's over-the-wire ports come from: BELOW every common
#: system's range for passing connections, as the coordinator's own range is,
#: and clear of it. In that range the operating system hands a port to nobody
#: who did not ask for it by number.
QUIET_PORTS = (30000, 30499)


#: EVERY COPY of this suite on the machine takes its ports from QUIET_PORTS -
#: a sweep's copies, a gate beside another agent's - and two copies that
#: bind-checked one free port in the same moment both started on it. So each
#: copy claims a block of the range for as long as it runs, and hands out only
#: ports of its block. The claim is the operating system's lock on one byte of
#: a file every copy on the machine shares - the byte of the block's number -
#: which the kernel lets go when the process ends, however it ended. (The
#: server part's suites claim theirs the same way, in `_suites.py`: the parts
#: share no code.)
PORT_BLOCK = 10
_CLAIMED: list = []


def claims_file() -> Path:
    return Path(tempfile.gettempdir()) / f"llmb-ports-{QUIET_PORTS[0]}-{QUIET_PORTS[1]}.claims"


def port_block() -> tuple:
    """(first, last) of this copy's block of QUIET_PORTS: claimed the first
    time it is asked for, kept until the process ends."""
    if not _CLAIMED:
        # held by name: a lock file the collector closes lets its lock go
        fh = open(claims_file(), "a+b", buffering=0)
        count = (QUIET_PORTS[1] - QUIET_PORTS[0] + 1) // PORT_BLOCK
        deadline, said = time.monotonic() + 300.0, False
        while not _CLAIMED:
            for i in random.sample(range(count), count):
                try:
                    if sys.platform == "win32":
                        import msvcrt
                        fh.seek(i)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.lockf(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB, 1, i, os.SEEK_SET)
                except OSError:
                    continue
                _CLAIMED.extend((fh, QUIET_PORTS[0] + i * PORT_BLOCK))
                # closed as the copy ends - the kernel lets its lock go either
                # way - so no file is left for the collector to warn of (H8)
                atexit.register(fh.close)
                break
            else:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"every block of {QUIET_PORTS} has been claimed by "
                                       f"other copies of this suite for 300s")
                if not said:
                    print(f"[ports] every block of {QUIET_PORTS} is claimed by another copy "
                          f"of this suite; waiting for one", flush=True)
                    said = True
                time.sleep(0.5)
    return _CLAIMED[1], _CLAIMED[1] + PORT_BLOCK - 1


def its_ports(tmp: Path) -> None:
    first, last = port_block()
    stray = sorted({quiet_port() for _ in range(50)} - set(range(first, last + 1)))
    check("every port this copy hands out is of the block of its range it claimed",
          not stray, f"{stray[:8]} are not of this copy's block {first}-{last}")
    p = two_free_ports()
    check("...and two side by side are too", first <= p and p + 1 <= last,
          f"{p}, {p + 1} (the block is {first}-{last})")


_TURN: list = [0]


def quiet_port(avoid=()) -> int:
    """A port nothing listens on now, of this copy's block of QUIET_PORTS, not
    in *avoid*: the block's ports in turn."""
    first, last = port_block()
    for _ in range(PORT_BLOCK):
        p = first + _TURN[0] % PORT_BLOCK
        _TURN[0] += 1
        if p in avoid:
            continue
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
    raise RuntimeError(f"no free port in this copy's block {first}-{last}")


def two_free_ports(avoid=()) -> int:
    """A port p with p and p+1 both free right now, and neither in *avoid*.
    Found by building, TWICE. First the coordinator's own port, picked after
    the range, was handed out by the operating system as p+1 - inside its
    range. Then a mutation sweep ran suites side by side, and p+1 - free when
    this looked - was given to another suite's server before the coordinator
    looked: in the range for passing connections, a port can be handed to
    anyone at any moment. So both come from QUIET_PORTS now - from this
    copy's block of it."""
    last = port_block()[1]
    for _ in range(2 * PORT_BLOCK):
        p = quiet_port(avoid)
        if p + 1 > last or p + 1 in avoid:
            continue
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", p + 1))
            return p
        except OSError:
            continue
    raise RuntimeError("no two free ports side by side")


def ports_over_the_wire(tmp: Path) -> None:
    """The operating system's own answer, not a stand-in: a port something
    else listens on is skipped by a real coordinator process."""
    port = quiet_port()                         # the coordinator's own, chosen FIRST
    url = f"http://127.0.0.1:{port}"
    lo = two_free_ports(avoid={port, port - 1})
    squatter = socket.socket()
    squatter.bind(("127.0.0.1", lo))
    squatter.listen()
    coord, _ = start(["coordinator.py", "--hardware", "none", "--data", str(tmp / "pw"),
                      "--port", str(port), "--card-lock", str(tmp / "pw-card.lock"),
                      "--ttl", "5", "--ports", f"{lo}-{lo + 1}"], "listening on")
    try:
        line = subprocess.list2cmdline(coord.args)
        check("a coordinator process this suite starts names this copy's folder on its "
              "command line - what mutate.py finds it by when the suite is timed out",
              str(HERE) in line, line[:200])
        s, b = wire(url, "coordinator/port-request", {"actor": W, "purpose": "model"})
        check("over the wire, a port something else listens on is never handed out",
              s == 200 and b["port"]["port"] == lo + 1, f"{s} {b} (the squatter holds {lo})")
        s, b = wire(url, "coordinator/port-request", {"actor": X, "purpose": "model"})
        check("...and with the rest held, the range is exhausted",
              s == 409 and b["reason"] == "exhausted" and "1 in use" in b["error"], f"{s} {b}")
        st = state(url)
        check("the state lists the port and who holds it",
              [(x["port"], x["actor"]) for x in st["ports"]] == [(lo + 1, W)], str(st["ports"]))
        s, b = wire(url, "coordinator/port-release", {"port": lo + 1, "actor": W})
        check("a port is given back over the wire", s == 200, f"{s} {b}")
        squatter.close()
        s, b = wire(url, "coordinator/port-request", {"actor": X, "purpose": "model"})
        check("once the squatter is gone, the lowest port is the one handed out",
              s == 200 and b["port"]["port"] == lo, f"{s} {b}")
    finally:
        squatter.close()
        killed(coord)


def example(route: str, name: str):
    return next(ex["response"] for ex in ROUTES[route]["examples"] if ex["name"] == name)


def painted(tmp: Path, state_name: str, record_name: str = "a crash, recovered") -> dict:
    """What the operator's page paints, run in node, for one of the contract's
    own /state examples and one of its /events examples."""
    script = C.DASHBOARD.split("<script>", 1)[1].split("</script>", 1)[0]
    state_ = example("coordinator/state", state_name)
    record = example("coordinator/events", record_name)
    page = tmp / "dashboard.js"
    page.write_bytes(("const painted = {};\n"
                      "const document = {getElementById: (id) => (painted[id] = painted[id]"
                      " || {innerHTML: '', textContent: ''})};\n"
                      f"const answers = {{'/state': {json.dumps(state_)}, "
                      f"'/events': {json.dumps(record)}}};\n"
                      "const fetch = async (url) => ({json: async () => answers[url]});\n"
                      "const setInterval = () => 0;\n"
                      + script +
                      "\nsetTimeout(() => process.stdout.write(JSON.stringify(Object.fromEntries("
                      "Object.entries(painted).map(([k, v]) => [k, v.innerHTML || v.textContent]"
                      ")))), 100);\n").encode("utf-8"))
    r = subprocess.run(["node", str(page)], capture_output=True, encoding="utf-8", timeout=30)
    try:
        return json.loads(r.stdout)
    except ValueError:
        check(f"the dashboard runs ({state_name})", False, (r.stdout + r.stderr)[:400])
        return {}


def dashboard(tmp: Path) -> None:
    """The operator's page, run in node against the contract's own examples:
    what it paints for the rental and each process it put on the card, the
    tickets in line - with their class and since when - and the record."""
    shown = painted(tmp, "held, two tickets in line")
    rows = shown.get("line", "").split("</tr>")
    check("the dashboard shows the tickets in line, each with its class and since when",
          len(rows) == 3 and "dictaphone" in rows[0] and "responsive" in rows[0]
          and "replay-analyst" in rows[1] and "scheduled" in rows[1]
          and all("since " in x and "s ago" in x for x in rows[:2]), shown.get("line", "")[:400])
    check("...and who holds the card, and for what",
          all(w in shown.get("card", "") for w in ("held", "writer-app", "demo-7b")),
          shown.get("card", "")[:300])
    procs = shown.get("procs", "").split("</tr>")
    check("...and each process the rental put on the card: its pid, model, engine, memory and "
          "since when", len(procs) == 2 and all(w in procs[0] for w in (
              "pid 18344", "demo-7b", "llamacpp", "5120 MiB", "load-k3x9m2p7", "since ", "s ago")),
          shown.get("procs", "")[:400])
    check("...and the limits in force - a release's settle time among them - and the record",
          "an offer 30s" in shown.get("limits", "") and "a release settles within 10s"
          in shown.get("limits", "") and "rental_reclaimed" in shown.get("events", ""),
          str(shown)[:300])
    shown = painted(tmp, "taken back: its two processes being ended on proof, and the next "
                         "ticket waiting until they are",
                    "two models side by side, one taken off, then a lapse: one ended, one left "
                    "alone")
    procs = shown.get("procs", "").split("</tr>")
    check("a rental being taken back says so, and lists what is being ended",
          "being taken back" in shown.get("card", "") and "dictaphone" in shown.get("card", "")
          and len(procs) == 3 and "pid 20112" in procs[0] and "4233 MiB" in procs[0]
          and "pid 20150" in procs[1] and "whisper" in procs[1], str(shown)[:500])
    check("...and the record, the processes' lines among it, each naming its pids",
          all(k in shown.get("events", "") for k in ("rental_processes", "rental_reclaiming",
                                                      "process_left_alone",
                                                      "pid 20112, pid 20150")),
          shown.get("events", "")[:400])
    shown = painted(tmp, "given back: its release checked until its process has gone")
    procs = shown.get("procs", "").split("</tr>")
    check("a rental being given back says so, and lists what it still names",
          "being given back" in shown.get("card", "") and len(procs) == 2
          and "pid 18344" in procs[0], str(shown)[:500])
    shown = painted(tmp, "free")
    check("a free card lists no process", "free" in shown.get("card", "")
          and "pid" not in shown.get("procs", ""), str(shown)[:300])
    shown = painted(tmp, "waiting for the machine's lock: an application run on its own has "
                         "the card, and nothing is offered until it lets go",
                    "the machine's lock waited for, then taken")
    check("the dashboard says when the coordinator waits for the machine's lock, and who "
          "holds it", "waiting for the machine's lock" in shown.get("lock", "")
          and "pid 9316" in shown.get("lock", ""), str(shown)[:400])
    check("...and its record, that it waited and then took it",
          all(k in shown.get("events", "") for k in ("lock_waiting", "lock_taken")),
          shown.get("events", "")[:400])
    shown = painted(tmp, "waiting for the machine's lock: its holder has not said who it is, "
                         "and the note beside it is stale - a dead holder's, naming nobody",
                    "the machine's lock waited for, then taken")
    check("the dashboard says when the note beside the lock is stale - its writer gone, and what "
          "it claimed - and names nobody as the holder (H5's register 6)",
          "the note beside it is stale" in shown.get("lock", "")
          and "pid 4120, which wrote it, has gone" in shown.get("lock", "")
          and "holds it (pid" not in shown.get("lock", ""), str(shown)[:400])
    shown = painted(tmp, "held, two tickets in line",
                    "the memory verified: refused, passed over, amended")
    check("the dashboard says when the coordinator holds the machine's lock",
          "holds the machine's lock" in shown.get("lock", ""), str(shown)[:400])
    check("...the memory's limits among the limits",
          "256 MiB is kept free beside what is asked" in shown.get("limits", "")
          and "an amend waits 5s" in shown.get("limits", ""), shown.get("limits", ""))
    check("...and the memory's lines of the record, each with its numbers",
          all(k in shown.get("events", "") for k in ("ticket_refused", "ticket_passed_over",
                                                     "rental_amended", "amend_refused"))
          and "needs 18842 of 12900 MiB free" in shown.get("events", ""),
          shown.get("events", "")[:600])
    # the watch (S5, PLAN-4 D3): the rental's work, a degraded card, and how
    # much of the card is outside the bootstrap
    shown = painted(tmp, "held, and degraded: the card over-full, most of its memory outside "
                         "the bootstrap",
                    "the card over-full: degraded, then room again - the rental never ended")
    check("the dashboard says a rental is degraded, and why - and that it is still held",
          "degraded" in shown.get("card", "") and "held" in shown.get("card", "")
          and "under the floor of 256 MiB" in shown.get("card", ""), shown.get("card", "")[:400])
    check("...how much of the card is outside the bootstrap, and what the rental's own use "
          "is taken to be", "16996 MiB outside the bootstrap" in shown.get("mem", "")
          and "7400 MiB" in shown.get("mem", ""), shown.get("mem", "")[:400])
    check("...the watch's limits among the limits",
          "taken back after 300s" in shown.get("limits", "")
          and "read every 30s during a rental" in shown.get("limits", "")
          and "degraded under 256 MiB free" in shown.get("limits", ""), shown.get("limits", ""))
    # ADDED (S5b): how often the card is read while degraded, and the room
    # the degraded rental waits for
    check("...how often the card is read while a rental is degraded",
          "read every 2s while it is" in shown.get("limits", ""), shown.get("limits", ""))
    check("...and the room the degraded rental waits for",
          "it clears with 256 MiB free" in shown.get("card", ""), shown.get("card", "")[:400])
    check("...and the watch's lines of the record",
          all(k in shown.get("events", "") for k in ("rental_degraded", "rental_restored")),
          shown.get("events", "")[:400])
    shown = painted(tmp, "held, two tickets in line",
                    "a hang: working, its progress still - taken back as for a crash, and the "
                    "record says why")
    check("the dashboard says the rental's work: working, its progress, and when it last moved",
          all(w in shown.get("work", "") for w in ("working", "412", "moved ", "s ago")),
          shown.get("work", "")[:300])
    check("...and a hang's lines of the record",
          all(k in shown.get("events", "") for k in ("rental_hung", "rental_reclaiming")),
          shown.get("events", "")[:400])
    # S11b R2: the count of slow answers the coordinator only counts, on a
    # card with room, is shown - the example's watch says 2
    check("the dashboard shows how many of the rental's answers were far slower than their "
          "model's normal",
          "2 of its answers far slower than their model's normal" in shown.get("work", ""),
          shown.get("work", "")[:300])
    shown = painted(tmp, "held, and degraded: the card over-full, most of its memory outside "
                         "the bootstrap")
    check("...and none when its heartbeats counted none",
          shown.get("work", "") != "" and "slower" not in shown.get("work", ""),
          shown.get("work", "")[:300])
    shown = painted(tmp, "free")
    check("...and nothing of the work when no rental is held", shown.get("work", "") == "",
          shown.get("work", "")[:300])
    # S18: scheduled work's time, and the room it waits for
    shown = painted(tmp, "free, with a batch planned for tonight and another waiting for room: "
                         "neither is offered the card",
                    "a batch planned in the morning for tonight: asked, offered at its time, "
                    "passed over while the card had no room, then granted once it did")
    rows = shown.get("line", "").split("</tr>")
    check("the dashboard says when a planned ticket starts, and what a ticket passed over waits "
          "for: the numbers, and what programs outside the bootstrap hold",
          len(rows) == 3 and "waits for room" in rows[0] and "needs 18842 MiB" in rows[0]
          and "13376 MiB" in rows[0] and "starts at " in rows[1] and "night batch" in rows[1],
          shown.get("line", "")[:600])
    check("...and the planned ticket's time on its record line",
          "starts at " in shown.get("events", ""), shown.get("events", "")[:600])


# ----------------------------------------------- what it answers before any route

#: the contract's word on what the coordinator answers before any route
BEFORE = V.load_origins(CONTRACT)["coordinator"]
#: every (status, reason) the coordinator answered before any route here
BEFORE_SEEN: set = set()
#: a page an operator may start the coordinator to allow
PAGE = "http://127.0.0.1:7867"
#: a website open in the same browser
ELSEWHERE = "http://example.com"
#: a website whose name was made to point at this machine (DNS rebinding)
REBOUND = "rebound.example"
#: the cap on a request's body for the rows about bodies: small, so a body over
#: it is small too
CAP = 512


def raw(port: int, method: str, path: str, headers: dict, body: bytes = b"",
        version: str = "HTTP/1.1", timeout: float = 5.0, until_closed: bool = False,
        host: str = "127.0.0.1"):
    """One request exactly as a program writes it - its headers as given and
    no others (a Host of any name, or none at all), then *body*, whatever the
    Content-Length says - and the answer read as http.client reads one.
    -> (status, headers, raw, seconds, closed): status 0 when nothing came back
    in time; `closed` - with *until_closed* - whether the coordinator then
    closed the connection within the timeout."""
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


def reason_of(data: bytes):
    """The `reason` of a JSON answer, or None."""
    try:
        out = json.loads(data)
    except ValueError:
        return None
    return out.get("reason") if isinstance(out, dict) else None


def everything(port: int, data: bytes, limit: float = 5.0):
    """*data* sent on a connection of its own, then every byte the
    coordinator sends read until it closes the connection. -> (every byte it
    sent, whether it closed the connection within *limit*)."""
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


def in_chunks(*pieces: bytes) -> bytes:
    """A body as Transfer-Encoding: chunked sends it: each piece, then the
    last, empty chunk."""
    return b"".join(b"%x\r\n%s\r\n" % (len(p), p) for p in pieces) + b"0\r\n\r\n"


def held_open(port: int, first: bytes, drip: bytes = b"", every: float = 0.0,
              limit: float = 5.0):
    """What a slow asker meets: *first* sent at once, then - with *every* -
    one byte of *drip* every *every* seconds, for as long as the connection
    is open. -> (every byte the coordinator sent, the seconds until it closed
    the connection - None when it had not within *limit*)."""
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


def cors(headers) -> dict:
    """What tells a browser whether a page may read an answer: every
    Access-Control-* header, and Vary - by lower-case name."""
    return {k.lower(): v for k, v in headers
            if k.lower().startswith("access-control-") or k.lower() == "vary"}


def names(headers) -> list:
    return sorted(k.lower() for k, _ in headers)


def header(got, name: str):
    return next((v for k, v in got[1] if k.lower() == name.lower()), None)


def show(got) -> str:
    return f"{got[0]} {got[2][:160]!r} after {got[3]:.2f}s {cors(got[1])}"


def before_any_route(status: int, data: bytes) -> bool:
    """An answer checked against what the contract declares before any route
    - and remembered, for the coverage."""
    shape = BEFORE["responses"].get(str(status))
    try:
        out = json.loads(data)
    except ValueError:
        out = None
    bad = [f"{status} is not declared before any route"] if shape is None \
        else V.check(out, shape, SHAPES)
    check(f"contract: {BEFORE['id']} -> {status}", not bad, "; ".join(bad))
    if not bad:
        BEFORE_SEEN.add((status, out["reason"]))
    return not bad


def refused(got, want: str) -> bool:
    """Refused before any route for *want*, its browser told nothing."""
    return (got[0] == 403 and before_any_route(got[0], got[2]) and reason_of(got[2]) == want
            and not cors(got[1]))


def ask_of(size: int, holder="app") -> bytes:
    """A ticket's ask of exactly *size* bytes: its label padded."""
    base = {"holder": holder, "actor": W, "models": [need("demo-7b")], "label": ""}
    base["label"] = "x" * (size - len(json.dumps(base).encode("utf-8")))
    data = json.dumps(base).encode("utf-8")
    if len(data) != size:
        raise AssertionError(f"an ask of {len(data)} bytes, not {size}")
    return data


def post_heard(url: str, data: bytes):
    """-> (status, reason) as a real client hears them (urllib), or the name of
    what cut it off - never an exception that ends the section."""
    req = urllib.request.Request(url + "/card/ask", data=data, method="POST",
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


def sent_late(port: int, method: str, path: str, headers: dict, body: bytes,
              late_s: float = 1.0):
    """A request whose head is sent at once and its body *late_s* seconds
    later, as a slow asker sends one. -> (what came back before a byte of the
    body was sent - b"" when nothing did -, the answer as `raw` gives it)."""
    t0 = time.monotonic()
    head = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5.0) as s:
            s.sendall((head + "\r\n").encode("latin-1"))
            s.settimeout(late_s)
            try:
                early = s.recv(1)
            except TimeoutError:
                early = b""
            if early:
                return early, (0, [], b"answered before its body was sent",
                               time.monotonic() - t0, False)
            s.settimeout(5.0)
            s.sendall(body)
            answer = HTTPResponse(s)
            try:
                answer.begin()
                data = answer.read()
            finally:
                answer.close()
            return b"", (answer.status, answer.getheaders(), data, time.monotonic() - t0, False)
    except (OSError, HTTPException) as e:
        return b"", (0, [], f"no answer: {type(e).__name__}: {e}".encode("utf-8"),
                     time.monotonic() - t0, False)


class Bare(BaseHTTPRequestHandler):
    """http.server with no method of its own, speaking as the coordinator does:
    what an OPTIONS was answered before the coordinator took the method."""

    def log_message(self, *a):
        pass


def web(tmp: Path) -> None:
    """A page from another address, a website whose name was made to point at
    this machine, and a body over the cap: each refused before any route,
    whatever it asks. A request with no Origin, one from the dashboard's own
    address, and one to any of the coordinator's own names are answered as
    before; a page it was started to allow is answered, and its browser told
    what it needs."""
    card = Card()
    c = Coordinator(tmp / "web", hardware=card, ttl_s=60)
    port = quiet_port()
    lines: list = []
    srv = C.serve(c, port, allow_origins=(PAGE,), log=lines.append)
    small_lines: list = []
    small = Coordinator(tmp / "web-small", hardware=Card(), ttl_s=60)
    sport = quiet_port(avoid={port})
    ssrv = C.serve(small, sport, max_body=CAP, log=small_lines.append)
    bport = quiet_port(avoid={port, sport})
    bare = ThreadingHTTPServer(("127.0.0.1", bport), Bare)
    LISTENED.append(("plain", bport, bare.server_address[1]))
    threading.Thread(target=bare.serve_forever, daemon=True, name="bare-http").start()
    url, own, rebound = f"http://127.0.0.1:{port}", f"http://127.0.0.1:{port}", f"{REBOUND}:{port}"
    host = {"Host": f"127.0.0.1:{port}"}
    others = []
    try:
        def in_line() -> list:
            return [t["holder"] for t in c.state()[1]["tickets"]]

        # --- no Origin, and the dashboard's own address: answered as before
        got = raw(port, "GET", "/state", host)
        check("a request with no Origin is answered as before, and says nothing of pages",
              got[0] == 200 and not conforms("coordinator/state", 200, json.loads(got[2]))
              and not cors(got[1]), show(got))
        got2 = raw(port, "GET", "/state", {**host, "Origin": own})
        check("...one from the dashboard's own address is answered the same, header for header",
              got2[0] == 200 and names(got2[1]) == names(got[1]), f"{show(got2)} {names(got2[1])}")
        got = raw(port, "GET", "/", host)
        check("...and the dashboard is served as ever",
              got[0] == 200 and got[2].startswith(b"<!doctype html>") and not cors(got[1]),
              show(got))
        ref = raw(bare.server_address[1], "OPTIONS", "/card/ask", {})
        for said, extra in (("with no Origin", {}), ("from the dashboard's own address",
                                                     {"Origin": own})):
            got = raw(port, "OPTIONS", "/card/ask", {**host, **extra})
            check(f"OPTIONS {said} is what it always was: 501, as http.server answers it",
                  ref[0] == 501 and (got[0], got[2]) == (ref[0], ref[2])
                  and names(got[1]) == names(ref[1]),
                  f"{show(got)} {names(got[1])}; http.server alone: {show(ref)} {names(ref[1])}")

        # --- a page from anywhere else: refused before any route
        before_seq = c.seq
        got = raw(port, "GET", "/state", {**host, "Origin": ELSEWHERE})
        check("a page from anywhere else is refused before any route, its browser told nothing",
              refused(got, "origin_not_allowed"), show(got))
        text = ask_of(200, holder="a page elsewhere")
        got = raw(port, "POST", "/card/ask", {**host, "Origin": ELSEWHERE,
                                              "Content-Type": "text/plain",
                                              "Content-Length": str(len(text))}, text)
        check("...not even a request its browser sends without asking first takes a ticket",
              refused(got, "origin_not_allowed") and in_line() == [], f"{show(got)} {in_line()}")
        early, got = sent_late(port, "POST", "/card/ask", {**host, "Origin": ELSEWHERE,
                                                           "Content-Type": "text/plain",
                                                           "Content-Length": str(len(text))},
                               text)
        check("...its body, within the cap, is read before it is answered - the connection is "
              "closed after every answer, and never with the body still coming: sent a second "
              "late, the answer waits for it", early == b"" and refused(got, "origin_not_allowed")
              and in_line() == [], f"{early!r} came before the body; then {show(got)}")
        s, b = c.request_port(W, "model")
        held_port = b["port"]["port"]
        text = json.dumps({"port": held_port, "actor": W}).encode("utf-8")
        got = raw(port, "POST", "/port/release", {**host, "Origin": ELSEWHERE,
                                                  "Content-Type": "text/plain",
                                                  "Content-Length": str(len(text))}, text)
        check("...nor gives a port back",
              refused(got, "origin_not_allowed")
              and [p["port"] for p in c.state()[1]["ports"]] == [held_port], show(got))
        got = raw(port, "OPTIONS", "/card/ask", {**host, "Origin": ELSEWHERE,
                                                 "Access-Control-Request-Method": "POST",
                                                 "Access-Control-Request-Headers": "content-type"})
        check("...its preflight is refused, its browser told nothing",
              refused(got, "origin_not_allowed"), show(got))
        got = [raw(port, "GET", path, {**host, "Origin": ELSEWHERE}) for path in ("/", "/nowhere")]
        check("...whatever it asks: the dashboard, and a path no route has",
              all(refused(g, "origin_not_allowed") for g in got), "; ".join(show(g) for g in got))
        got = raw(port, "GET", "/state", {**host, "Origin": "null"})
        check("...and a page that gives no address of its own (Origin: null) is refused too",
              refused(got, "origin_not_allowed"), show(got))
        got = raw(sport, "GET", "/state", {"Host": f"127.0.0.1:{sport}", "Origin": PAGE})
        check("a coordinator started to allow no page refuses that one too: a page is allowed "
              "by name", refused(got, "origin_not_allowed"), show(got))
        check("the refusal is said on the console, naming the page's address",
              f"GET /state -> 403 origin_not_allowed from {ELSEWHERE}" in lines, str(lines[-6:]))

        # --- the page the coordinator was started to allow
        got = raw(port, "GET", "/state", {**host, "Origin": PAGE})
        check("a page the coordinator was started to allow is answered, and its browser told it "
              "may read the answer", got[0] == 200
              and cors(got[1]) == {"access-control-allow-origin": PAGE, "vary": "Origin"},
              show(got))
        body = ask_of(200, holder="the allowed page")
        got = raw(port, "POST", "/card/ask", {**host, "Origin": PAGE,
                                              "Content-Type": "application/json",
                                              "Content-Length": str(len(body))}, body)
        check("...it acts: its ticket is taken, and the answer is its to read",
              got[0] == 200 and in_line() == ["the allowed page"]
              and cors(got[1]).get("access-control-allow-origin") == PAGE, show(got))
        if got[0] == 200:
            c.cancel(json.loads(got[2])["token"])
        body = json.dumps({"ticket_id": "t-0099-abcd", "token": "not its token"}).encode("utf-8")
        got = raw(port, "POST", "/card/claim", {**host, "Origin": PAGE,
                                                "Content-Type": "application/json",
                                                "Content-Length": str(len(body))}, body)
        check("...a refusal a route gives it is its to read too, in the coordinator's words",
              got[0] == 409 and reason_of(got[2]) == "unknown_ticket"
              and cors(got[1]).get("access-control-allow-origin") == PAGE, show(got))
        got = raw(port, "OPTIONS", "/card/ask", {**host, "Origin": PAGE,
                                                 "Access-Control-Request-Method": "POST",
                                                 "Access-Control-Request-Headers": "content-type"})
        pre = cors(got[1])
        check("its preflight is answered 204: the methods the routes take, and the header a JSON "
              "body needs", got[0] == 204 and got[2] == b""
              and pre.get("access-control-allow-origin") == PAGE
              and {m.strip() for m in pre.get("access-control-allow-methods", "").split(",")}
              == {"GET", "POST"}
              and pre.get("access-control-allow-headers", "").lower() == "content-type"
              and pre.get("vary") == "Origin", f"{show(got)} {pre}")

        # --- the name it was sent to
        got = raw(port, "GET", "/state", {"Host": rebound, "Origin": f"http://{rebound}"})
        check("a website whose name was made to point at this machine is refused before any route "
              "- that name is its Host, and its Origin too - its browser told nothing",
              refused(got, "host_not_allowed"), show(got))
        text = ask_of(200, holder="a rebound page")
        got = raw(port, "POST", "/card/ask", {"Host": rebound, "Origin": f"http://{rebound}",
                                              "Content-Type": "text/plain",
                                              "Content-Length": str(len(text))}, text)
        check("...not even a request its browser sends without asking first takes a ticket",
              refused(got, "host_not_allowed") and in_line() == [], f"{show(got)} {in_line()}")
        got = raw(port, "GET", "/state", {"Host": rebound})
        check("...and a request with no Origin that names a host the coordinator does not have "
              "is refused too - no longer answered as before", refused(got, "host_not_allowed"),
              show(got))
        got = [raw(port, method, path, {"Host": rebound})
               for method, path in (("OPTIONS", "/card/ask"), ("GET", "/"), ("GET", "/nowhere"))]
        check("...whatever it asks: its preflight, the dashboard, a path no route has",
              all(refused(g, "host_not_allowed") for g in got), "; ".join(show(g) for g in got))
        got = raw(port, "GET", "/state", {"Host": rebound, "Origin": ELSEWHERE})
        check("the name is checked before the page: a page from elsewhere asking by a name the "
              "coordinator does not have is refused for the name", refused(got, "host_not_allowed"),
              show(got))
        check("the refusal is said on the console, naming the host it was sent to",
              f"GET /state -> 403 host_not_allowed for {rebound}" in lines, str(lines[-6:]))
        for said, name in (("localhost", f"localhost:{port}"), ("[::1]", f"[::1]:{port}"),
                           ("127.0.0.1, with no port", "127.0.0.1"),
                           ("written in capitals, as a program may", f"LocalHost:{port}")):
            got = raw(port, "GET", "/state", {"Host": name})
            check(f"a request to one of the coordinator's own names is answered as before: {said}",
                  got[0] == 200 and not conforms("coordinator/state", 200, json.loads(got[2])),
                  show(got))
        got = [raw(port, "GET", "/state", {"Host": name})
               for name in (f"{REBOUND}@127.0.0.1:{port}", f"127.0.0.1:{port}/state",
                            f"127.0.0.1:{port}:{port}", "")]
        check("a Host that is not a name and a port is refused, never guessed around",
              all(refused(g, "host_not_allowed") for g in got), "; ".join(show(g) for g in got))
        got = raw(port, "GET", "/state", {}, version="HTTP/1.0")
        check("a request that names no host at all - a program speaking HTTP/1.0, never a browser "
              "- is answered as before", got[0] == 200, show(got))
        # since `before_seq`: the port this section asked for, the allowed page's
        # ticket - asked, offered at once, cancelled here - and nothing else
        check("a refusal before any route is never written to the record: a page cannot fill it "
              "by asking", [e["kind"] for e in c.events[before_seq:]]
              == ["port_granted", "ticket_asked", "ticket_offered", "ticket_cancelled"],
              str([e["kind"] for e in c.events[before_seq:]]))
        t0 = time.monotonic()
        s, b = call(f"http://localhost:{port}", "GET", "/state")
        took = time.monotonic() - t0
        check("a real client names the host it was sent to, and is answered: urllib, at localhost",
              s == 200 and not conforms("coordinator/state", s, b), f"{s} {str(b)[:120]}")
        measured.append(f"urllib asking localhost of a coordinator on 127.0.0.1 alone: "
                        f"{took:.2f}s (::1 is tried first)")
        h = Holder(url, "a real holder", "demo-7b", patience_s=10)
        try:
            rental = h.acquire()
        except Refused as e:
            rental = None
            refused_ = e
        else:
            refused_ = None
        back = h.release()
        check("...and a holder - the coordinator's own client - takes the card and gives it back",
              rental is not None and back is not None and back[0] == 200, f"{rental} {refused_}")
        aside_c = Coordinator(tmp / "web-aside", hardware=Card())
        aside_port = quiet_port(avoid={port, sport})
        aside = C.serve(aside_c, aside_port, host="127.0.0.2", log=lambda s: None)
        others.append(aside)
        s, b = call(f"http://127.0.0.2:{aside_port}", "GET", "/state")
        check("the address a coordinator listens on is one of its names: asked at 127.0.0.2, it "
              "answers", s == 200, f"{s} {str(b)[:120]}")
        got = raw(port, "GET", "/state", {"Host": f"127.0.0.2:{port}"})
        check("...and it is that coordinator's alone: the one on 127.0.0.1 does not answer to it",
              refused(got, "host_not_allowed"), show(got))
        got = [raw(aside_port, "GET", "/state", {"Host": f"{name}:{aside_port}"}, host="127.0.0.2")
               for name in ("127.0.0.1", "localhost", "[::1]")]
        check("...and every coordinator's own names are its too, wherever it listens: the one on "
              "127.0.0.2 answers to 127.0.0.1, localhost and [::1]",
              [g[0] for g in got] == [200, 200, 200], "; ".join(show(g) for g in got))
        named_c = Coordinator(tmp / "web-named", hardware=Card())
        named_port = quiet_port(avoid={port, sport, aside_port})
        named_ = C.serve(named_c, named_port,
                         allow_origins=("http://app.example:7867", "http://[::2]:7867"),
                         log=lambda s: None)
        others.append(named_)
        got = raw(named_port, "GET", "/state", {"Host": f"app.example:{named_port}"})
        check("the host of a page a coordinator was started to allow is one of its names",
              got[0] == 200, show(got))
        got = raw(named_port, "GET", "/state", {"Host": f"[::2]:{named_port}"})
        check("...an IPv6 one in its brackets, as a Host header writes it", got[0] == 200,
              show(got))
        got = raw(named_port, "GET", "/state", {"Host": f"other.example:{named_port}"})
        check("...and no other host is", refused(got, "host_not_allowed"), show(got))

        # --- its body, against a cap of CAP bytes
        shost = {"Host": f"127.0.0.1:{sport}", "Content-Type": "application/json"}
        got = raw(sport, "POST", "/card/ask", {**shost, "Content-Length": str(CAP + 1)},
                  ask_of(CAP + 1))
        check("a body one byte over the cap is refused 413 too_large before any route: no "
              "ticket is taken", got[0] == 413 and before_any_route(got[0], got[2])
              and reason_of(got[2]) == "too_large" and small.state()[1]["tickets"] == [],
              show(got))
        got = raw(sport, "POST", "/card/ask", {**shost, "Content-Length": str(CAP)}, ask_of(CAP))
        check("...and one of exactly the cap is read, and answered by its route: its ticket is taken",
              got[0] == 200 and [t["holder"] for t in small.state()[1]["tickets"]] == ["app"],
              show(got))
        got = raw(sport, "POST", "/card/ask", {**shost, "Content-Length": str(10 ** 12)},
                  until_closed=True)
        check("a body over the cap is never read: the answer comes at once, though not a byte of "
              "the body is ever sent", got[0] == 413 and reason_of(got[2]) == "too_large"
              and got[3] < 3.0, show(got))
        check("...it says Connection: close, and the connection is closed",
              header(got, "Connection") == "close" and got[4] is True,
              f"{header(got, 'Connection')!r}, closed {got[4]}")
        heard = [post_heard(f"http://127.0.0.1:{sport}", ask_of(8 * 1024 * 1024))
                 for _ in range(3)]
        check("a real client whose body is over the cap hears the answer, never a reset: urllib, "
              "8 MiB, three times", heard == [(413, "too_large")] * 3, str(heard))
        got = raw(sport, "POST", "/card/ask", {**shost, "Origin": ELSEWHERE,
                                               "Content-Length": str(10 ** 12)})
        check("a refused page's body over the cap is not read either: its 403 comes at once, "
              "though the body is never sent", got[0] == 403
              and reason_of(got[2]) == "origin_not_allowed" and got[3] < 3.0, show(got))
        got = raw(port, "POST", "/card/ask", {**host, "Origin": PAGE,
                                              "Content-Type": "application/json",
                                              "Content-Length": str(10 ** 12)})
        check("a page the coordinator allows is told it may read its 413, as every answer it is "
              "given", got[0] == 413 and cors(got[1]).get("access-control-allow-origin") == PAGE,
              show(got))
        empty = raw(sport, "POST", "/card/ask", {**shost, "Content-Length": "0"})
        # "²" (a superscript two) is a digit to str.isdigit, and never to int()
        for length in ("-1", "abc", "²"):
            got = raw(sport, "POST", "/card/ask", {**shost, "Content-Length": length})
            check(f"a Content-Length of {length!a} is not a number of bytes: it reads as no body - "
                  f"answered as an empty ask is, and nothing waits for a body",
                  empty[0] == 400 and (got[0], got[2]) == (empty[0], empty[2]) and got[3] < 3.0,
                  f"{show(got)}; an empty ask: {show(empty)}")
        check("the 413 is said on the console, with the body's length",
              f"POST /card/ask -> 413 too_large - a body of {CAP + 1} bytes" in small_lines,
              str(small_lines[-6:]))

        # --- a body in chunks: its length never said
        ask_ = ask_of(200)
        before_seq = c.seq
        got = raw(port, "POST", "/card/ask", {**host, "Content-Type": "application/json",
                                              "Transfer-Encoding": "chunked"},
                  in_chunks(ask_), until_closed=True)
        check("a body sent in chunks - its length never said - is refused 411 length_required "
              "before any route: no ticket is taken", got[0] == 411
              and before_any_route(got[0], got[2]) and reason_of(got[2]) == "length_required"
              and in_line() == [], f"{show(got)} {in_line()}")
        check("...it says Connection: close, and the connection is closed",
              header(got, "Connection") == "close" and got[4] is True,
              f"{header(got, 'Connection')!r}, closed {got[4]}")
        got = raw(port, "GET", "/state", {**host, "Transfer-Encoding": "chunked"}, in_chunks(b"x"))
        check("...whatever the method: a GET with a body in chunks is refused too",
              got[0] == 411 and reason_of(got[2]) == "length_required", show(got))
        got = raw(port, "POST", "/card/ask", {**host, "Content-Type": "application/json",
                                              "Transfer-Encoding": "chunked",
                                              "Content-Length": "100"})
        check("...and one naming a Content-Length too: nothing is read by the length - the "
              "answer comes at once, though no byte of a body is sent",
              got[0] == 411 and reason_of(got[2]) == "length_required" and got[3] < 3.0,
              show(got))
        for said, extra in (("its Host", {"Host": rebound}),
                            ("its page", {**host, "Origin": ELSEWHERE})):
            head_ = ("POST /card/ask HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in {
                **extra, "Content-Type": "application/json",
                "Transfer-Encoding": "chunked"}.items()) + "\r\n").encode()
            sent, closed = everything(port, head_ + in_chunks(ask_))
            check(f"a body in chunks refused for {said} is not read either: its 403 says "
                  f"Connection: close", sent.startswith(b"HTTP/1.0 403 ")
                  and b"\r\nConnection: close\r\n" in sent.split(b"\r\n\r\n")[0] + b"\r\n"
                  and closed, f"closed {closed}: {sent[:300]!r}")
        check("...the 411 is said on the console, naming how the body came",
              "POST /card/ask -> 411 length_required - Transfer-Encoding: chunked" in lines,
              str(lines[-6:]))
        check("...and none of it is written to the record", c.seq == before_seq,
              str(kinds(c, before_seq)))

        # --- every method goes through the checks
        got = [raw(port, method, "/card/ask", {"Host": rebound})
               for method in ("PUT", "DELETE", "PATCH", "BREW")]
        check("a method no route takes is checked as every request is: from a name the "
              "coordinator does not have, PUT, DELETE, PATCH and a made-up one are refused "
              "host_not_allowed - not 501", all(refused(g, "host_not_allowed") for g in got),
              "; ".join(show(g) for g in got))
        got = raw(port, "PUT", "/card/ask", {**host, "Origin": ELSEWHERE})
        check("...from a page elsewhere, origin_not_allowed", refused(got, "origin_not_allowed"),
              show(got))
        got = raw(port, "DELETE", "/card/ask", {**host, "Transfer-Encoding": "chunked"},
                  in_chunks(b"x"))
        check("...with a body in chunks, length_required",
              got[0] == 411 and reason_of(got[2]) == "length_required", show(got))
        got = raw(sport, "PUT", "/card/ask", {**shost, "Content-Length": str(10 ** 12)})
        check("...with a body over the cap, too_large - at once, not a byte of it read",
              got[0] == 413 and reason_of(got[2]) == "too_large" and got[3] < 3.0, show(got))
        check("...and the refusal is said on the console, naming the method",
              f"PUT /card/ask -> 403 host_not_allowed for {rebound}" in lines, str(lines[-6:]))
        for method in ("PUT", "DELETE", "HEAD", "BREW"):
            mine = raw(port, method, "/card/ask", host)
            ref = raw(bare.server_address[1], method, "/card/ask", {})
            check(f"{method} passing the checks is answered as http.server answers a method it "
                  f"does not take: 501, word for word",
                  ref[0] == 501 and (mine[0], mine[2]) == (ref[0], ref[2])
                  and names(mine[1]) == names(ref[1]),
                  f"{show(mine)} {names(mine[1])}; http.server alone: {show(ref)} "
                  f"{names(ref[1])}")
        roomy = Coordinator(tmp / "web-roomy", hardware=Card(), ttl_s=60)
        rport = quiet_port(avoid={port, sport})
        others.append(C.serve(roomy, rport, max_body=16 * 1024 * 1024, log=lambda s: None))
        heard = []
        for method, headers in (("PUT", {}), ("PUT", {}), ("PUT", {}), ("GET", {}), ("GET", {}),
                                ("GET", {}), ("PUT", {"Origin": ELSEWHERE}),
                                ("PUT", {"Origin": ELSEWHERE}), ("PUT", {"Origin": ELSEWHERE})):
            req = urllib.request.Request(f"http://127.0.0.1:{rport}/up",
                                         data=b"x" * (8 * 1024 * 1024), method=method,
                                         headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    heard.append((method, r.status))
            except urllib.error.HTTPError as e:
                with e:
                    heard.append((method, e.code))
            except OSError as e:
                heard.append((method, type(e).__name__))
        check("a body within the cap that no route reads - a PUT's, a GET's, a refused "
              "request's whatever its method - is read and set aside before the answer: a real "
              "client sending 8 MiB hears it, never a reset - urllib, three times each",
              heard == [("PUT", 501)] * 3 + [("GET", 200)] * 3 + [("PUT", 403)] * 3, str(heard))

        # --- what the contract says the coordinator answers before any route, word for word
        for ex in BEFORE["examples"]:
            said = {"Host": ex.get("host", f"127.0.0.1:{port}")}
            if "origin" in ex:
                said["Origin"] = ex["origin"]
            if "transfer_encoding" in ex:
                got = raw(port, "POST", "/card/ask", {**said, "Content-Type": "application/json",
                                                      "Transfer-Encoding": ex["transfer_encoding"]},
                          in_chunks(b"{}"))
            elif "length" in ex:
                got = raw(port, "POST", "/card/ask", {**said, "Content-Type": "application/json",
                                                      "Content-Length": str(ex["length"])})
            else:
                got = raw(port, "GET", "/state", said)
            try:
                out = json.loads(got[2])
            except ValueError:
                out = None
            check(f"the contract's example '{ex['name']}' before any route is answered word for "
                  f"word", (got[0], out) == (ex["status"], ex["response"]), show(got))
        want = {(int(status), reason) for status, shape in BEFORE["responses"].items()
                for reason in V.fields_of(shape, SHAPES)["reason"]["enum"]}
        check(f"every answer the contract declares before any route ({len(want)}), the "
              f"coordinator gave", bool(want) and want <= BEFORE_SEEN,
              f"declared {sorted(want)}; given {sorted(BEFORE_SEEN)}")
    finally:
        for o in others:
            C.stop(o)
        C.stop(srv)
        C.stop(ssrv)
        bare.shutdown()
        bare.server_close()


def parsed(argv: list):
    """-> (the arguments, None), or (None, what the refusal said): a command
    line refused is a check's answer, never the end of the suite."""
    said = io.StringIO()
    try:
        with contextlib.redirect_stderr(said):
            return C.parse_args(argv), None
    except SystemExit:
        return None, said.getvalue().strip()


def web_settings(tmp: Path) -> None:
    """--allow-origin and --max-body, read at the start as the server reads
    its own: a page's address as a browser writes it, a whole number of
    bytes - and anything that is not one refused there, never met at the
    first request."""
    data = ["--data", str(tmp / "settings")]
    a, said = parsed(data + ["--allow-origin", PAGE, "--allow-origin", "HTTP://LocalHost:80/",
                             "--allow-origin", "https://App.Example:443",
                             "--allow-origin", "http://[::1]:7867"])
    check("--allow-origin names pages as their browser writes them - lower case, no default "
          "port, no slash, an IPv6 host in brackets - and may be given again",
          a is not None and a.allow_origin == [PAGE, "http://localhost", "https://app.example",
                                               "http://[::1]:7867"],
          said or str(a.allow_origin))
    a, said = parsed(data)
    check("...and none given, none is allowed: applications never ask the coordinator from a "
          "browser", a is not None and not a.allow_origin, said or str(a.allow_origin))
    for wrong in ("127.0.0.1:7867", "http://127.0.0.1:7867/app", "null", "*",
                  "ftp://127.0.0.1", "http://127.0.0.1:7867?x=1", "http://127.0.0.1:7867#top",
                  "http://me@127.0.0.1:7867", "http://127.0.0.1:99999"):
        a, said = parsed(data + ["--allow-origin", wrong])
        check(f"...{wrong!r} is not a page's address: refused at the start, saying so",
              a is None and "not a page's address" in said,
              said[-160:] if said else f"taken, as {a.allow_origin}")
    a, said = parsed(data + ["--max-body", "4096"])
    check("--max-body sets the largest body the coordinator reads, in bytes",
          a is not None and a.max_body == 4096, said or str(a.max_body))
    a, said = parsed(data)
    check("...1 MiB when it is not given", a is not None and a.max_body == 1024 * 1024,
          said or str(a.max_body))
    for wrong in ("0", "-5", "1.5", "lots"):
        a, said = parsed(data + ["--max-body", wrong])
        check(f"...{wrong!r} is not a number of bytes, 1 or more: refused at the start, saying so",
              a is None and "--max-body" in said and "not a whole number of bytes" in said,
              said[-160:] if said else f"taken, as {a.max_body}")
    # True is refused though Python's True is an int: a cap is a number of bytes
    for i, (what, kw, needle) in enumerate((
            ("a page's address that is not one", {"allow_origins": ("null",)},
             "not a page's address"),
            ("a cap below 1", {"max_body": 0}, "max_body"),
            ("a cap of True", {"max_body": True}, "max_body"),
            ("a cap that is not a whole number of bytes", {"max_body": 1.5}, "max_body"))):
        try:
            C.stop(C.serve(Coordinator(tmp / f"settings-code-{i}"), quiet_port(), **kw))
            said = ""
        except ValueError as e:
            said = str(e)
        check(f"a coordinator made in code is refused {what} too", needle in said,
              said or "it started")
    port = quiet_port()
    proc, _ = start(["coordinator.py", "--hardware", "none", "--data", str(tmp / "settings-cli"),
                     "--port", str(port), "--allow-origin", PAGE, "--max-body", "64",
                     "--card-lock", str(tmp / "settings-cli-card.lock")],
                    "listening on")
    try:
        host = {"Host": f"127.0.0.1:{port}"}
        seen_ = wait_for(lambda: proc.poll() is None, 1)
        got = raw(port, "GET", "/state", {**host, "Origin": PAGE})
        got2 = raw(port, "GET", "/state", {**host, "Origin": ELSEWHERE})
        check("a coordinator started with --allow-origin answers that page, and refuses any other",
              seen_ is not None and got[0] == 200
              and cors(got[1]).get("access-control-allow-origin") == PAGE and got2[0] == 403,
              f"{show(got)}; {show(got2)}")
        over = json.dumps({"holder": "x" * 60}).encode("utf-8")        # 74 bytes
        got = raw(port, "POST", "/card/ask", {**host, "Content-Type": "application/json",
                                              "Content-Length": str(len(over))}, over)
        got2 = raw(port, "POST", "/card/ask", {**host, "Content-Type": "application/json",
                                               "Content-Length": "2"}, b"{}")
        check("...and one started with --max-body reads bodies up to it, and refuses one over it",
              len(over) > 64 and (got[0], reason_of(got[2])) == (413, "too_large")
              and (got2[0], reason_of(got2[2])) == (400, "bad_request"),
              f"{show(got)}; {show(got2)}")
    finally:
        killed(proc)
    a, said = parsed(data + ["--read-timeout", "2.5"])
    check("--read-timeout sets how long a request has to arrive whole, in seconds",
          a is not None and a.read_timeout == 2.5, said or str(a.read_timeout))
    a, said = parsed(data)
    check("...10 seconds when it is not given", a is not None and a.read_timeout == 10.0,
          said or str(a.read_timeout))
    for wrong in ("0", "-1", "abc", "nan", "inf"):
        a, said = parsed(data + ["--read-timeout", wrong])
        check(f"...{wrong!r} is not a number of seconds above 0: refused at the start, saying so",
              a is None and "--read-timeout" in said and "not a number of seconds" in said,
              said[-160:] if said else f"taken, as {a.read_timeout}")
    for i, wrong in enumerate((0, -1, True, "1", float("inf"), float("nan"))):
        try:
            C.stop(C.serve(Coordinator(tmp / f"settings-timeout-{i}"), quiet_port(),
                           read_timeout_s=wrong))
            said = ""
        except ValueError as e:
            said = str(e)
        check(f"a coordinator made in code is refused a read timeout that is not a number of "
              f"seconds above 0, too: {wrong!r}", "read_timeout_s" in said, said or "it started")
    port = quiet_port()
    # CHANGED ON PURPOSE (H4): a card lock of its own - it took the machine's
    proc, _ = start(["coordinator.py", "--hardware", "none", "--data",
                     str(tmp / "settings-timeout-cli"), "--port", str(port),
                     "--card-lock", str(tmp / "settings-timeout-cli-card.lock"),
                     "--read-timeout", "0.5"], "listening on")
    try:
        got, closed = held_open(port, (f"POST /card/ask HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                                       f"Content-Length: 100\r\n\r\n{{").encode())
        check("a coordinator started with --read-timeout closes a request that has not come "
              "whole within it", got == b"" and closed is not None and 0.4 <= closed < 3.0,
              f"{got[:80]!r}, closed after {closed}")
    finally:
        killed(proc)


#: the read timeout the rows about a request's time give a coordinator: short,
#: so a request cut off is seen soon
READ_S = 1.0


def reset_mid_request(port: int) -> None:
    """An asker that resets its connection mid-request, on purpose: a body
    promised and never sent whole, then the connection closed with SO_LINGER
    0 - a reset (RST), never a close - as a process ended mid-request does."""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall((f"POST /card/ask HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
               f"Content-Type: application/json\r\nContent-Length: 400\r\n\r\n"
               f'{{"holder": "re').encode())
    time.sleep(0.3)                                     # its handler is reading the body
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    s.close()


def a_reset_connection(tmp: Path) -> None:
    """R-B5: a coordinator once printed an unhandled ConnectionResetError
    traceback when a server under it was ended. A connection its asker resets
    mid-request is said in one line of its log - never a traceback - and
    the coordinator answers the next request as ever."""
    c = Coordinator(tmp / "reset", hardware=Card(), ttl_s=60)
    port = quiet_port()
    lines: list = []
    srv = C.serve(c, port, log=lines.append)
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            reset_mid_request(port)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not err.getvalue() \
                    and not any("reset" in line for line in lines):
                time.sleep(0.02)
            time.sleep(0.3)                             # a second word, if one comes
        s = wire(f"http://127.0.0.1:{port}", "coordinator/state")[0]
    finally:
        C.stop(srv)
    said = [line for line in lines if "reset" in line]
    check("a connection its asker reset mid-request is said in one line of the coordinator's "
          "log: that it was reset, by whom, and that nothing was answered",
          len(said) == 1 and "127.0.0.1" in said[0] and "nothing was answered" in said[0],
          str(lines))
    check("...never a traceback", "Traceback" not in err.getvalue() and not err.getvalue(),
          err.getvalue()[:600])
    check("...and the coordinator answers the next request", s == 200, str(s))


def a_request_s_time(tmp: Path) -> None:
    """A request must arrive whole - its line, its headers and its body -
    within the read timeout of its connection being made, or it is never
    answered and its connection is closed: before, a body within the cap that
    never finished arriving held a handler thread for ever. The bound is the
    whole request's, never each read's; a connection that never sent a byte
    is closed without a word; none of it is written to the record."""
    c = Coordinator(tmp / "read-timeout", hardware=Card(), ttl_s=60)
    port = quiet_port()
    lines: list = []
    srv = C.serve(c, port, log=lines.append, read_timeout_s=READ_S)
    host = f"127.0.0.1:{port}"
    post = (f"POST /card/ask HTTP/1.1\r\nHost: {host}\r\nContent-Type: application/json\r\n"
            f"Content-Length: 1000\r\n\r\n").encode()
    try:
        seq = c.seq
        body, heads, idle, drip = at_once(
            lambda: held_open(port, post + b'{"holder": "sl'),
            lambda: held_open(port, f"POST /card/ask HTTP/1.1\r\nHost: {host}\r\n".encode()),
            lambda: held_open(port, b""),
            lambda: held_open(port, post, drip=b"x" * 1000, every=0.1, limit=6.0))
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
        check("a request cut off part way is said on the console - and a connection that never "
              "sent a byte is not", len(cut) == 3 and sum(ln.startswith(
                  f"POST /card/ask -> its connection closed: not read whole within {READ_S:g}s")
                  for ln in cut) == 3, "\n".join(cut) or "\n".join(lines[-6:]))
        check("...and none of it is written to the record", c.seq == seq, str(kinds(c, seq)))
        t0 = time.monotonic()
        data = ask_of(300)
        with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
            s.sendall(f"POST /card/ask HTTP/1.1\r\nHost: {host}\r\nContent-Type: "
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
              "pieces it came", said[0] == 200 and [t["holder"] for t in
                                                   c.state()[1]["tickets"]] == ["app"],
              f"{said} after {time.monotonic() - t0:.2f}s")
    finally:
        C.stop(srv)


def its_port_alone(tmp: Path) -> None:
    """The coordinator's port is its alone. http.server binds SO_REUSEADDR,
    and on Windows a second program's socket that sets it too was let bind
    the port of a listening coordinator; and a coordinator started on a port
    such a program already listened on started, said it was listening - and
    of 20 requests heard none: the program took them all (measured before
    this was fixed). Each way round is provoked with a helper's socket, bound
    as http.server binds; the second at the command line too."""
    port = quiet_port()
    srv = C.serve(Coordinator(tmp / "alone"), port, log=lambda s: None)
    try:
        with reusing() as helper:
            try:
                helper.bind(("127.0.0.1", port))
                said = "it bound"
            except OSError as e:
                said = ""
                refusal = str(e)
        check("a program binding SO_REUSEADDR is refused the port a coordinator listens on",
              not said, said or refusal)
    finally:
        C.stop(srv)
    port = quiet_port()
    with reusing() as helper:
        helper.bind(("127.0.0.1", port))
        helper.listen(8)
        try:
            C.stop(C.serve(Coordinator(tmp / "alone-2"), port, log=lambda s: None))
            refused_ = ""
        except OSError as e:
            refused_ = str(e)
        check("a coordinator made on a port a program listens on - bound SO_REUSEADDR - is "
              "refused it, never shares it", bool(refused_), refused_ or "it started")
        # H3 R2: the command line's coordinator built itself - the record's
        # rebuild, a reclaim or release resumed, its coordinator_started line -
        # before it tried its port, so one started on a port somebody holds
        # acted on the record, then said it could not start. The record here
        # is a stopped coordinator's, and must be what it was, byte for byte.
        # CHANGED ON PURPOSE (H5): a lock of the row's own - it ran with none
        # named, and took the machine's own
        Coordinator(tmp / "alone-cli").close()
        record = tmp / "alone-cli" / "events.jsonl"
        kept = record.read_bytes()
        lock = tmp / "alone-cli-card.lock"
        code, out = cli_start(["--data", str(tmp / "alone-cli"), "--port", str(port),
                               "--card-lock", str(lock)])
        check("...and one started there says it cannot start, and never that it listens",
              code == 2 and "cannot start" in out and str(port) in out
              and "listening on" not in out, f"{code}: {out[-300:]}")
        check("...in the words of what it was: it cannot listen on its port",
              f"cannot start: cannot listen on 127.0.0.1:{port}: " in out, out[-300:])
        check("...and it acted on nothing first: its record is what it was, byte for byte",
              record.read_bytes() == kept,
              f"{len(kept)} bytes before, {len(record.read_bytes())} after: "
              f"{record.read_bytes()[len(kept):][:300]!r}")
        # CHANGED ON PURPOSE (H7, H6's register 2): the note is looked for
        # BEFORE lock_free, whose take and let-go deletes a note beside the
        # lock - after it, "no note" could never fail
        noted = M.MachineLock(lock).note_path.exists()
        check("...and the machine's lock it took is free again, with no note beside it",
              not noted and lock_free(lock), f"a note beside it: {noted}")
    # H3 R3: any OSError at the start was said to be the port. Here the port is
    # free, and the record cannot be kept: its folder is a file
    port = quiet_port()
    (tmp / "alone-file").write_bytes(b"not a folder")
    code, out = cli_start(["--data", str(tmp / "alone-file"), "--port", str(port),
                           "--card-lock", str(tmp / "alone-file-card.lock")])
    check("a coordinator whose record cannot be kept says so - what it was, and never that it "
          "cannot listen", code == 2 and "cannot start: " in out and "alone-file" in out
          and "cannot listen" not in out and "listening on" not in out and "Traceback" not in out,
          f"{code}: {out[-300:]}")


def every_port_its_own(tmp: Path) -> None:
    """Last: every coordinator this suite made listen, the plain server
    beside one, and the stand-in coordinator a holder waits on, listened on a
    port of this copy's block - never on port 0, a port the operating system
    picks (H6's register 3; H7's register 2: the stand-in was not recorded)."""
    first, last = port_block()
    stray = [(what, asked, had) for what, asked, had in LISTENED if not first <= had <= last]
    check("everything this suite made listen - a coordinator, the plain server beside one, the "
          "stand-in coordinator - listened on a port of its own block, never one the operating "
          "system picked",
          {what for what, _, _ in LISTENED} == {"coordinator", "plain", "stub"} and not stray,
          f"{len(stray)} of {len(LISTENED)} were not, e.g. {stray[:4]} (block {first}-{last})")
    # H8's register: the claims file this copy holds its block by is closed as
    # the copy ends, so no ResourceWarning is left for the collector to print
    out = subprocess.run([sys.executable, "-W", "always::ResourceWarning", "-c",
                          f"import sys; sys.path.insert(0, {str(HERE)!r}); "
                          f"import test_coordinator as T; print(T.port_block())"],
                         capture_output=True, text=True, timeout=120,
                         creationflags=NO_WINDOW)
    check("a copy of this suite closes the file it claims its block by as it ends: no "
          "ResourceWarning", out.returncode == 0 and "ResourceWarning" not in out.stderr
          and out.stdout.strip().startswith("("), (out.stdout + out.stderr)[-600:])


def cli_start(args: list) -> tuple:
    """coordinator.py run with *args* until it exits, within START_S: -> (exit
    code, what it printed). Held to a card lock of its own (`of_its_own`)."""
    try:
        done = subprocess.run(of_its_own([sys.executable, str(HERE / "coordinator.py"),
                                          "--hardware", "none", *args]),
                              capture_output=True, cwd=HERE, encoding="utf-8",
                              errors="replace", timeout=START_S)
        return done.returncode, done.stdout + done.stderr
    except subprocess.TimeoutExpired as e:
        return f"still running after {START_S:g}s", str(e.stdout or "")


def lock_free(path: Path) -> bool:
    """Could anybody take the card lock at *path* now? Taken and let go at once."""
    lock = M.MachineLock(path)
    if not lock.take():
        return False
    lock.release()
    return True


def bindable_here(port: int) -> bool:
    """Can a socket bind *port* on 127.0.0.1 now, as the coordinator binds it?"""
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


# ------------------------------------------------------ the holder's patience

class StubCoordinator:
    """A coordinator a holder is pointed at, answering from the contract's own
    examples - and misbehaving as a row asks: its ticket offered at once (or
    waiting), its claim answered for a ticket that ended (409 `unclaimed`) or
    never answered - the connection closed, as by a coordinator gone away -
    and its wait answered for a ticket that ended (409 `dropped`); a claim and
    a wait each after `delay_s`. Every request is written down, with when it
    came (monotonic)."""

    def __init__(self, ask="offered", claim="ended", delay_s=0.0):
        self.asked: list = []
        stub = self
        answers = {
            "/card/ask": (200, example("coordinator/card-ask", {
                "offered": "the card is free and nobody is ahead: offered at once",
                "waiting": "the card is held: waiting, and told nothing of who has it"}[ask])),
            "/card/claim": (409, example("coordinator/card-claim",
                                         "claimed too late: the offer passed on")),
            "/card/wait": (409, example("coordinator/card-wait", "nobody waited on it")),
            "/card/cancel": (200, example("coordinator/card-cancel", "given up while it waited"))}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                stub.asked.append((self.path, time.monotonic()))
                if self.path in ("/card/claim", "/card/wait"):
                    time.sleep(delay_s)
                    if self.path == "/card/claim" and claim == "hang up":
                        self.close_connection = True
                        return                          # no answer at all
                status, body = answers.get(self.path, (404, {"error": "no route",
                                                             "reason": "no_route"}))
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        asked = quiet_port()
        self.srv = ThreadingHTTPServer(("127.0.0.1", asked), Handler)
        LISTENED.append(("stub", asked, self.srv.server_address[1]))
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def times(self, path: str) -> list:
        return [t for p, t in self.asked if p == path]

    def stop(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


def acquired_within(h: Holder, bound: float = 5.0) -> tuple:
    """`h.acquire()` on a thread of its own, within *bound*: -> (the reason it
    was refused - or "held", or "still asking" - and how long it took)."""
    box: dict = {}

    def go():
        try:
            h.acquire()
            box["got"] = "held"
        except Refused as e:
            box["got"] = e.reason
    t0 = time.monotonic()
    asking = threading.Thread(target=go, daemon=True)
    asking.start()
    asking.join(bound)
    return box.get("got", "still asking"), time.monotonic() - t0


def holder_patience(tmp: Path) -> None:
    """S4 R1: past its patience a holder asks nothing more. A claim answered
    for a ticket that had ended sent it back to asking, and a claim that could
    not reach the coordinator back to claiming, neither checking the patience
    first - so a holder whose offers kept ending, or whose coordinator went
    away as it was offered the card, asked for ever; and a wait answered for a
    ticket that ended sent it to ask once more after its patience had run
    out."""
    for claim, what in (("ended", "a claim answered for a ticket that had ended"),
                        ("hang up", "a claim the coordinator never answered")):
        stub = StubCoordinator(claim=claim, delay_s=0.5)
        try:
            h = Holder(stub.url, "writer-app", "demo-7b", patience_s=0.3, retry_s=0.05)
            got, took = acquired_within(h)
            asks, claims = len(stub.times("/card/ask")), len(stub.times("/card/claim"))
        finally:
            stub.stop()
        check(f"{what}, its patience spent meanwhile, ends the holder's asking: card_patience, "
              f"one ask, one claim", (got, asks, claims) == ("card_patience", 1, 1)
              and took < 2.0, f"{got} after {asks} asks and {claims} claims in {took:.2f}s")
    stub = StubCoordinator(ask="waiting", delay_s=0.5)
    try:
        h = Holder(stub.url, "writer-app", "demo-7b", patience_s=0.3, retry_s=0.05)
        got, took = acquired_within(h)
        asks = len(stub.times("/card/ask"))
    finally:
        stub.stop()
    check("a wait answered for a ticket that had ended, its patience spent meanwhile, asks no "
          "more: card_patience, one ask", (got, asks) == ("card_patience", 1) and took < 2.0,
          f"{got} after {asks} asks in {took:.2f}s")
    stub = StubCoordinator(claim="ended", delay_s=0.05)
    try:
        h = Holder(stub.url, "writer-app", "demo-7b", patience_s=0.5, retry_s=0.05)
        t0 = time.monotonic()
        got, took = acquired_within(h)
        asks = stub.times("/card/ask")
    finally:
        stub.stop()
    check("within its patience, a holder whose ticket ended asks for another - and none after "
          "its patience ran out", got == "card_patience" and len(asks) >= 2
          and max(asks) - t0 < 0.5 + 0.2, f"{got}: {len(asks)} asks, the last "
          f"{(max(asks) - t0) if asks else 0:.2f}s in")
    stub = StubCoordinator(claim="ended")
    try:
        h = Holder(stub.url, "live-app", "whisper-x", patience_s=0, retry_s=0.05)
        got, took = acquired_within(h)
        asks = len(stub.times("/card/ask"))
    finally:
        stub.stop()
    check("a patience of 0 whose offered ticket ended asks no more: card_patience at once",
          (got, asks) == ("card_patience", 1) and took < 1.0,
          f"{got} after {asks} asks in {took:.2f}s")


# ------------------------------------------------------- the memory, verified

#: a card of 24 GiB, as the fake machine reads it
BIG = 24576
#: the contract's card lock: its file names, and the note its holder writes
LOCK_DOC = V.load_machine(CONTRACT)["card-lock"]


def memory_of(need_mib, free_mib, margin_mib=256, total_mib=BIG) -> dict:
    return {"need_mib": need_mib, "free_mib": free_mib, "margin_mib": margin_mib,
            "total_mib": total_mib}


def names_nobody_else(body, *others) -> bool:
    """Does an answer to one application say nothing of another - its name,
    its actor, its ticket?"""
    said = json.dumps(body)
    return not any(o in said for o in others)


def at_grant(tmp: Path) -> None:
    """PLAN-4 D1, D2: before any grant the coordinator reads the card itself -
    afresh, never an answer it kept - and checks that what the ticket declared
    it needs, and the margin kept free beside it, fit in the free memory. An
    ask that can never fit is refused at once, whatever its class; responsive
    work that does not fit now is refused at once, in numbers; scheduled work
    waits for the room. None of it names anybody else."""
    card = Card(used=1000, total=BIG)
    c = Coordinator(tmp / "grant-never", hardware=card, clock=Clock())
    p = Probe(c)
    for klass in ("responsive", "scheduled"):
        s, b = p.ask("replay-analyst", [need("huge", mib=BIG - 255)], actor=W, klass=klass)
        check(f"an ask that could never fit is refused at once, whatever its class ({klass}): "
              f"never_fits, in numbers",
              s == 409 and b.get("reason") == "never_fits"
              and b.get("memory") == memory_of(BIG - 255, BIG - 1000)
              and f"the whole card is {BIG} MiB" in b.get("error", ""), str(b))
    check("...and it takes no ticket: nobody is in line", not c.line and c.rental is None,
          str(c.line))
    refused = events_of(c, "ticket_refused")
    check("...and the record says so each time, with the numbers",
          len(refused) == 2 and all(e.get("memory") == memory_of(BIG - 255, BIG - 1000)
                                    for e in refused), str(refused))
    s, b = p.ask("app", [need("a", mib=12000), need("b", mib=12400)], actor=I)
    check("what a ticket's models need TOGETHER is judged: two that each fit, and together "
          "never", s == 409 and b.get("reason") == "never_fits"
          and b.get("memory", {}).get("need_mib") == 24400, str(b))
    s, b = p.ask("app", [need("fits-empty", mib=BIG - 256)], actor=W)
    check("...while an ask that would fit an empty card by the same rule is taken",
          s == 200 and b["ticket"]["state"] == "offered", str(b))

    # responsive, and no room now: refused at once, at its claim - the grant
    card = Card(used=1000, total=BIG)
    c = Coordinator(tmp / "grant-now", hardware=card, clock=Clock())
    p = Probe(c)
    t1, k1, _ = asked(p, W, holder="writer-app", models=[need("big", mib=20000)])
    t2, k2, _ = asked(p, I, holder="dictaphone", models=[need("small", "whisper", 4000)])
    card.readings = [8000]              # something outside the bootstrap took 7000 MiB since
    fresh = card.fresh
    s, b = p.claim(t1, k1)
    check("a responsive ticket that does not fit at its claim is refused at once: no_room, "
          "in numbers - read afresh, never the answer the ask was given",
          s == 409 and b.get("reason") == "no_room"
          and b.get("memory") == memory_of(20000, BIG - 8000) and card.fresh == fresh + 1,
          str(b))
    check("...its words say the numbers, and name nobody else",
          "20000 MiB" in b.get("error", "") and f"{BIG - 8000} MiB free" in b.get("error", "")
          and names_nobody_else(b, "dictaphone", I, t2), b.get("error", ""))
    check("...LOUDLY (H9, the owner's ruling of 2026-09-28): what programs outside the "
          "bootstrap hold - the whole card's use, at a claim no rental holds it - and what to do: "
          "close programs, or reboot; it never waits for room",
          "programs outside the bootstrap hold 8000 MiB" in b.get("error", "")
          and "Close programs that use the card, or reboot" in b.get("error", "")
          and "does not wait for room" in b.get("error", ""), b.get("error", ""))
    refused = [e for e in c.events if e["kind"] == "ticket_refused"]
    check("...and the record says the same words",
          bool(refused) and "programs outside the bootstrap hold 8000 MiB" in refused[-1]["detail"]
          and "or reboot" in refused[-1]["detail"], str(refused[-1:])[:400])
    s, b = p.wait(t1, k1, 0)
    check("...and the ticket is over: asked again, it is answered refused",
          (s, b.get("reason")) == (409, "refused"), str(b))
    check("...the record says why, with the numbers: ticket_refused",
          [e.get("memory") for e in events_of(c, "ticket_refused")
           if e.get("ticket_id") == t1] == [memory_of(20000, BIG - 8000)], str(kinds(c)))
    check("...and the card goes to the next ticket in line",
          c.line.get(t2, {}).get("state") == "offered", str(c.line))
    s, b = p.claim(t2, k2)
    granted_ = events_of(c, "rental_granted")
    check("...which fits, and is granted - the record saying the numbers it was granted on",
          s == 200 and [e.get("memory") for e in granted_] == [memory_of(4000, BIG - 8000)],
          str(b)[:300])
    for verb, args in (("claim", (t1, k1)), ("heartbeat", (k1,)), ("release", (k1,)),
                       ("cancel", (k1,)), ("amend", (k1, need("big", mib=20000)))):
        s, b = getattr(p, verb)(*args)
        check(f"a ticket refused at its claim is refused, asked by {verb}",
              (s, b.get("reason")) == (409, "refused") and "refused" in b.get("error", ""),
              str(b))
    again = Probe(Coordinator(tmp / "grant-now", hardware=Card(used=1000, total=BIG),
                              clock=Clock()))
    s, b = again.wait(t1, k1, 0)
    check("a coordinator restarted from its record knows a ticket refused at its claim is over",
          (s, b.get("reason")) == (409, "refused") and t1 not in again.c.line, str(b))

    # a margin is a setting: at its edge, it decides
    for margin, verdict in ((256, 200), (1000, 409)):
        card = Card(used=BIG - 9999, total=BIG)
        c = Coordinator(tmp / f"grant-margin-{margin}", hardware=card, clock=Clock(),
                        margin_mib=margin)
        p = Probe(c)
        tid, token, _ = asked(p, W, models=[need("m", mib=9000)])
        s, b = p.claim(tid, token)
        check(f"the margin kept free is the coordinator's setting: {margin} MiB beside 9000 on "
              f"9999 free is {verdict}", s == verdict
              and (s == 200 or b.get("memory") == memory_of(9000, 9999, margin)), str(b))

    # the coordinator's own client for an application hears each refusal, at
    # once, and never asks again for what the card cannot hold
    card = Card(used=BIG - 2000, total=BIG)
    c = Coordinator(tmp / "grant-holder", hardware=card, clock=Clock())
    port = quiet_port()
    srv = C.serve(c, port, log=lambda line_: None)
    try:
        for models, reason in (([need("huge", mib=BIG)], "never_fits"),
                               ([need("big", mib=8000)], "no_room")):
            h = Holder(f"http://127.0.0.1:{port}", "writer-app", models, patience_s=5)
            box: dict = {}

            def go(h=h, box=box):
                try:
                    h.acquire()
                    box["got"] = None
                except Refused as e:
                    box["got"] = e.reason
            # on a thread, within a bound: a Holder that asks again and again
            # fails the row - and stops once the coordinator below is stopped
            t0 = time.monotonic()
            asking = threading.Thread(target=go, daemon=True)
            asking.start()
            asking.join(5)
            got = box.get("got", "still asking")
            check(f"the coordinator's Holder is refused {reason} at once, and asks no more",
                  got == reason and h.asks == 1 and time.monotonic() - t0 < 2.0,
                  f"{got} after {h.asks} asks in {time.monotonic() - t0:.2f}s")
    finally:
        C.stop(srv)

    # a card not read when the ticket was asked, and read at its claim
    card = Card(total=BIG)
    c = Coordinator(tmp / "grant-never-at-claim", hardware=card, clock=Clock())
    p = Probe(c)
    tid, token, _ = asked(p, W, models=[need("huge", mib=BIG)], klass="scheduled")
    card.readings = [1000]
    s, b = p.claim(tid, token)
    check("a ticket found at its claim to need more than the whole card is refused "
          "never_fits, whatever its class: no wait could help it",
          s == 409 and b.get("reason") == "never_fits"
          and b.get("memory") == memory_of(BIG, BIG - 1000) and tid not in c.line, str(b))

    # a card that cannot be read decides nothing
    c = Coordinator(tmp / "grant-unknown", hardware=Card(), clock=Clock())
    p = Probe(c)
    tid, token, _ = asked(p, W, models=[need("huge", mib=10 ** 6)])
    s, b = p.claim(tid, token)
    check("a card that cannot be read decides nothing: the rental is granted, whatever it "
          "declared", s == 200 and not events_of(c, "ticket_refused"), str(b))

    # scheduled, and no room now: it waits for the room, and the card goes on
    card = Card(used=1000, total=BIG)
    c = Coordinator(tmp / "grant-wait", hardware=card, clock=Clock(), recheck_s=60)
    p = Probe(c)
    ta, ka, _ = asked(p, W, holder="replay-analyst", models=[need("qwen3-32b", mib=18842)],
                      klass="scheduled")
    tb, kb, _ = asked(p, I, holder="dictaphone", models=[need("small", "whisper", 1000)],
                      klass="scheduled")
    card.readings = [12000]
    s, b = p.claim(ta, ka)
    check("scheduled work with no room yet waits for it: answered not_offered, in numbers, "
          "still in line", s == 409 and b.get("reason") == "not_offered"
          and b.get("memory") == memory_of(18842, BIG - 12000)
          and c.line.get(ta, {}).get("state") == "waiting", str(b))
    check("...naming nobody else", names_nobody_else(b, "dictaphone", I, tb), str(b))
    check("...passed over for memory: the card is offered to the next ticket in line",
          c.line.get(tb, {}).get("state") == "offered", str(c.line))
    check("...the record says it was passed over, once, with its numbers",
          [e.get("memory") for e in events_of(c, "ticket_passed_over")]
          == [memory_of(18842, BIG - 12000)], str(kinds(c)))
    s, b = p.claim(tb, kb)
    check("...and the next, which fits, holds the card", s == 200, str(b))
    s, b = p.wait(ta, ka, 0)
    check("...while the one passed over is still waiting, as its wait says",
          s == 200 and b["ticket"]["state"] == "waiting", str(b))
    c.reap()
    check("...and while a rental holds the card, the card is not read again for it",
          c.line[ta]["state"] == "waiting", str(c.line[ta]))
    card.readings = [2000]              # outside, something let go; the rental gives back
    s, b = p.release(kb)
    check("a rental's end offers the card first to the ticket passed over for memory",
          s == 200 and c.line.get(ta, {}).get("state") == "offered", str(c.line))
    s, b = p.claim(ta, ka)
    check("...which, now that it fits, is granted",
          s == 200 and len(events_of(c, "ticket_passed_over")) == 1, str(b)[:300])

    # passed over while the card is free: read again every recheck_s, and not before
    card = Card(used=10000, total=BIG)
    c = Coordinator(tmp / "grant-recheck", hardware=card, clock=Clock(), recheck_s=0.3)
    p = Probe(c)
    tc, kc, _ = asked(p, W, models=[need("qwen", mib=18842)], klass="scheduled")
    card.readings = [10000]
    p.claim(tc, kc)
    reads = card.fresh
    for _ in range(3):
        c.reap()
    check("a ticket waiting for room on a free card: the card is not read again before "
          "recheck_s", card.fresh == reads, f"{card.fresh - reads} more reads")
    time.sleep(0.35)
    c.reap()
    check("...then read afresh, and not offered while it would not fit",
          card.fresh == reads + 1 and c.line[tc]["state"] == "waiting",
          f"{card.fresh - reads} more reads; {c.line[tc]['state']}")
    card.readings = [2000]
    time.sleep(0.35)
    c.reap()
    check("...and offered the card once a fresh read says it fits",
          c.line[tc]["state"] == "offered", str(c.line[tc]))
    s, b = p.claim(tc, kc)
    check("...and granted", s == 200 and len(events_of(c, "ticket_passed_over")) == 1, str(b))

    # that read is made OUTSIDE the lock: everyone else is answered meanwhile
    class Slow(Card):
        gate, inside = None, False

        def probe(self, fresh=False):
            if fresh and self.gate is not None:
                self.inside = True
                self.gate.wait(5)
            return super().probe(fresh)
    card = Slow(used=10000, total=BIG)
    c = Coordinator(tmp / "grant-recheck-lock", hardware=card, clock=Clock(), recheck_s=0.05)
    p = Probe(c)
    td, kd, _ = asked(p, W, models=[need("qwen", mib=18842)], klass="scheduled")
    card.readings = [10000]
    p.claim(td, kd)
    time.sleep(0.06)
    card.gate = threading.Event()
    reaper = threading.Thread(target=c.reap, daemon=True)
    reaper.start()
    until(lambda: card.inside, 2)
    # CHANGED ON PURPOSE (H6, H5's register 7): "meanwhile" is what is
    # asserted - the ask answered while the read is still held, its reaper
    # still inside it - never a second's bound on an in-process ask, which a
    # loaded machine took 1.72 s over
    t0 = time.monotonic()
    s, b = p.ask("dictaphone", [need("small", "whisper", 1000)], actor=I)
    took = time.monotonic() - t0
    reading = reaper.is_alive() and not card.gate.is_set()
    card.gate.set()
    reaper.join(5)
    check("...and the read is made outside the coordinator's lock: an ask meanwhile is answered "
          "while the read is still held", card.inside and s == 200 and reading,
          f"answered in {took:.2f}s, the read {'still held' if reading else 'over'}")


def scheduled_work(tmp: Path) -> None:
    """S18, the owner's second ruling of 2026-09-28: scheduled work whose
    models do not fit at its claim waits for room - or, asked to, fails at
    once, loudly - and may name a time it starts no earlier than: until then
    it is in line and holds back nobody, and at its time it is offered the
    card like any other. Responsive work is now: never a time to come, never a
    wait for room. The time is the coordinator's clock, looked at on every
    beat - a machine that slept through it is offered it when it wakes."""
    clk = Clock()
    later = clk.t + 9 * 3600                # tonight, asked in the morning
    card = Card(used=1000, total=BIG)
    c = Coordinator(tmp / "sched-time", hardware=card, clock=clk)
    p = Probe(c)
    s, b = p.ask("image-batch", [need("sdxl", mib=9800)], actor=W, klass="scheduled",
                 not_before=later)
    t1, k1 = b.get("ticket", {}).get("id"), b.get("token")
    check("a scheduled ticket planned for a time still to come is taken, and waits: the card is "
          "free and nobody is ahead, and it is not offered", s == 200
          and b["ticket"]["state"] == "waiting" and b["ticket"].get("not_before") == later
          and b["ticket"].get("if_no_room") == "wait" and b["ticket"].get("room") is None,
          str(b))
    asked_line = [e for e in events_of(c, "ticket_asked") if e.get("ticket_id") == t1]
    check("...its time on the record's line for it", len(asked_line) == 1
          and asked_line[0].get("not_before") == later
          and asked_line[0].get("if_no_room") == "wait", str(asked_line))
    t2, k2, st2 = asked(p, I, holder="notes", models=[need("small", mib=1000)],
                        klass="scheduled")
    check("...and holds back nobody: scheduled work asked after it is offered the card at once",
          st2 == "offered", st2)
    p.claim(t2, k2)
    s, b = p.heartbeat(k2, [])
    check("...and a rental is not told the card is wanted by a ticket whose time has not come",
          s == 200 and b.get("wanted") is False, str(b))
    p.release(k2)
    offered_early = False
    while clk.t < later - 10:                # the day goes by: its asker looks now and then
        clk.t = min(later - 10, clk.t + 100)
        c.reap()
        s, b = c.wait(t1, k1, 0)            # the coordinator's own: one contract row, not 300
        offered_early = offered_early or s != 200 or b["ticket"]["state"] != "waiting"
    check("...nor is it offered the card, free, before its time, however often the coordinator "
          "beats and its asker looks", not offered_early and c.line[t1]["state"] == "waiting",
          str(c.line[t1]))
    t3, k3, st3 = asked(p, X, holder="live", models=[need("small", mib=1000)])
    p.claim(t3, k3)
    s, b = p.heartbeat(k3, [])
    check("...and a rental held ten seconds before its time is still not told it is wanted",
          s == 200 and b.get("wanted") is False, str(b))
    clk.t = later
    s, b = p.heartbeat(k3, [])
    check("once its time has come - this very moment - a rental is told the card is wanted",
          s == 200 and b.get("wanted") is True, str(b))
    p.release(k3)
    c.reap()
    check("...and once the card is free, the first beat offers it the card",
          c.line[t1]["state"] == "offered", str(c.line[t1]))
    s, b = p.claim(t1, k1)
    check("...and it is granted", s == 200, str(b)[:300])
    p.release(k1)

    # A MACHINE THAT SLEPT THROUGH ITS TIME (it hibernates): nobody looked at
    # the ticket while it slept - its asker slept too - so the first beat after
    # waking drops it, as it drops any ticket nobody waits on; its asker, told
    # so at its first look, asks again naming the same time, now past: the
    # card is offered at once. The time is compared, never counted down
    clk2 = Clock()
    c = Coordinator(tmp / "sched-slept", hardware=Card(used=1000, total=BIG), clock=clk2)
    p = Probe(c)
    tonight = clk2.t + 3600
    ts, ks, _ = asked(p, W, holder="image-batch", models=[need("sdxl", mib=9800)],
                      klass="scheduled", not_before=tonight)
    clk2.t = tonight + 8 * 3600              # asleep from before its time until the morning
    s, b = p.wait(ts, ks, 0)
    check("a planned ticket nobody could look at while the machine slept is dropped at the "
          "first look after it wakes - its asker is told so", (s, b.get("reason"))
          == (409, "dropped"), str(b))
    s, b = p.ask("image-batch", [need("sdxl", mib=9800)], actor=W, klass="scheduled",
                 not_before=tonight)
    check("...and asked again for the same time, now past, it is offered the card at once: it "
          "starts when the machine wakes", s == 200 and b["ticket"]["state"] == "offered"
          and b["ticket"].get("not_before") == tonight, str(b))

    # a time already past is now; exactly now is now
    c = Coordinator(tmp / "sched-past", hardware=Card(used=1000, total=BIG), clock=Clock())
    p = Probe(c)
    for when, name in ((c.clock() - 60, "a minute ago"), (c.clock(), "this moment")):
        s, b = p.ask("app", [need("m", mib=1000)], actor=W, klass="scheduled", not_before=when)
        check(f"a time already past is now ({name}): offered at once on a free card",
              s == 200 and b["ticket"]["state"] == "offered", str(b))
        p.cancel(b["token"])
        s, b = p.ask("app", [need("m", mib=1000)], actor=W, not_before=when)
        check(f"...for responsive work too ({name}): live is now, and so is that time",
              s == 200 and b["ticket"]["state"] == "offered"
              and b["ticket"].get("if_no_room") == "fail", str(b))
        p.cancel(b["token"])

    # responsive work is now
    s, b = p.ask("dictation-app", [need("m", mib=1000)], actor=I, not_before=c.clock() + 60)
    check("responsive work at a time still to come is refused: live_is_now, in words",
          s == 400 and b.get("reason") == "live_is_now" and "live work is now" in b.get("error", "")
          and "60 s away" in b.get("error", ""), str(b))
    s, b = p.ask("dictation-app", [need("m", mib=1000)], actor=I, if_no_room="wait")
    check("responsive work asked to wait for room is refused: live_is_now, in words",
          s == 400 and b.get("reason") == "live_is_now"
          and "never waits for room" in b.get("error", ""), str(b))
    s, b = p.ask("dictation-app", [need("m", mib=1000)], actor=I, if_no_room="fail")
    check("...while responsive work that says it fails is taken: it always does", s == 200, str(b))
    p.cancel(b["token"])
    for bad in (float("inf"), float("nan"), "tonight"):
        s, b = p.ask("app", [need("m", mib=1000)], actor=W, klass="scheduled", not_before=bad)
        check(f"a not_before that is not a finite number ({bad!r}) is refused bad_request",
              s == 400 and b.get("reason") == "bad_request" and "not_before" in b.get("error", ""),
              str(b))
    s, b = p.ask("app", [need("m", mib=1000)], actor=W, klass="scheduled", if_no_room="later")
    check("an if_no_room that is not wait or fail is refused bad_request",
          s == 400 and b.get("reason") == "bad_request", str(b))
    check("...and none of them took a ticket", not c.line, str(c.line))

    # WAIT OR FAIL, AT THE CLAIM
    card = Card(used=1000, total=BIG)
    c = Coordinator(tmp / "sched-fail", hardware=card, clock=Clock(), recheck_s=60)
    p = Probe(c)
    ta, ka, _ = asked(p, W, holder="replay-analyst", models=[need("qwen", mib=18842)],
                      klass="scheduled", if_no_room="fail")
    tb, kb, _ = asked(p, I, holder="batch", models=[need("qwen", mib=18842)],
                      klass="scheduled")
    card.readings = [12000]
    s, b = p.claim(ta, ka)
    check("scheduled work that asked to fail, with no room at its claim: refused at once, "
          "no_room, in the loud words - what programs outside hold, and what to do",
          s == 409 and b.get("reason") == "no_room"
          and b.get("memory") == memory_of(18842, BIG - 12000)
          and "programs outside the bootstrap hold 12000 MiB" in b.get("error", "")
          and "does not wait for room" in b.get("error", "")
          and "or reboot" in b.get("error", "") and ta not in c.line, str(b))
    refused = [e for e in events_of(c, "ticket_refused") if e.get("ticket_id") == ta]
    check("...and the record says so, in the same words",
          len(refused) == 1 and "does not wait for room" in refused[0].get("detail", ""),
          str(refused))
    check("...and the card goes to the next in line", c.line[tb]["state"] == "offered",
          str(c.line[tb]))
    s, b = p.claim(tb, kb)
    check("scheduled work that waits (the default), with no room at its claim: passed over, "
          "not_offered, in the words a person reads while it waits - the numbers, what programs "
          "outside hold, and that it starts by itself",
          s == 409 and b.get("reason") == "not_offered"
          and "programs outside the bootstrap hold 12000 MiB" in b.get("error", "")
          and "starts by itself once it fits" in b.get("error", "")
          and "does not wait" not in b.get("error", ""), str(b))
    s, b = p.wait(tb, kb, 0)
    room = b.get("ticket", {}).get("room") or {}
    check("...its ticket says the room it waits on: the numbers, what programs outside hold, "
          "the words, and when the card was read", s == 200
          and room.get("memory") == memory_of(18842, BIG - 12000)
          and room.get("outside_mib") == 12000 and "starts by itself" in room.get("words", "")
          and room.get("at") == c.clock(), str(b))
    over = [e for e in events_of(c, "ticket_passed_over") if e.get("ticket_id") == tb]
    check("...and the record's line says what programs outside hold",
          len(over) == 1 and over[0].get("outside_mib") == 12000
          and "starts by itself" in over[0].get("detail", ""), str(over))
    c.clock.t += 7
    c.recheck_s = 0.001
    card.readings = [15000]
    time.sleep(0.01)
    c.reap()
    s, b = p.wait(tb, kb, 0)
    room = b.get("ticket", {}).get("room") or {}
    check("...refreshed each time the card is read again for it",
          room.get("memory") == memory_of(18842, BIG - 15000)
          and room.get("outside_mib") == 15000 and room.get("at") == c.clock()
          and b["ticket"]["state"] == "waiting", str(b))
    s, st = p.state()
    mine = [t for t in st["tickets"] if t["id"] == tb]
    check("...and the operator's /state says it", len(mine) == 1
          and (mine[0].get("room") or {}).get("outside_mib") == 15000, str(mine))
    card.readings = [1000]
    time.sleep(0.01)
    c.reap()
    s, b = p.claim(tb, kb)
    check("...then offered and granted once it fits", s == 200, str(b)[:300])

    # a restart keeps a ticket's time, and whether it waits
    clk = Clock()
    later = clk.t + 60                      # within the drop time: its asker keeps looking
    c = Coordinator(tmp / "sched-rebuild", hardware=Card(used=1000, total=BIG), clock=clk)
    p = Probe(c)
    tr, kr, _ = asked(p, W, holder="image-batch", models=[need("sdxl", mib=9800)],
                      klass="scheduled", not_before=later, if_no_room="fail")
    again = Probe(Coordinator(tmp / "sched-rebuild", hardware=Card(used=1000, total=BIG),
                              clock=clk))
    s, b = again.wait(tr, kr, 0)
    check("a coordinator restarted from its record keeps a planned ticket's time and whether it "
          "waits for room - and does not offer it before its time",
          s == 200 and b["ticket"].get("not_before") == later
          and b["ticket"].get("if_no_room") == "fail" and b["ticket"]["state"] == "waiting",
          str(b))
    clk.t = later + 1
    again.c.reap()
    check("...and offers it once its time has come", again.c.line[tr]["state"] == "offered",
          str(again.c.line[tr]))


def amends(tmp: Path) -> None:
    """PLAN-4 D2: before each model a rental adds, the server asks
    (`/card/amend`); the coordinator reads the card again and approves, or
    refuses in words and numbers. Right after the rental's own unload it waits
    for the memory to come back before it judges (the Dictaphone's H-3). And
    the rental's models follow what is on the card (S6 R5)."""
    card = Card(used=1000, total=BIG)
    clk = Clock()
    c = Coordinator(tmp / "amend", hardware=card, clock=clk, amend_settle_s=1.0)
    p = Probe(c)
    tid, token = granted(p, W, models=[need("alpha", mib=8000)])
    expires = c.state()[1]["rental"]["expires_at"]
    card.readings = [1000]
    fresh = card.fresh
    clk.t += 3
    s, b = p.amend(token, need("alpha", mib=8000), [])
    check("an amend whose model fits is approved, saying the numbers it was approved on",
          s == 200 and b.get("memory") == memory_of(8000, BIG - 1000), str(b))
    check("...read afresh for it, never an answer the coordinator kept",
          card.fresh == fresh + 1, f"{card.fresh - fresh} fresh reads")
    check("...and an amend renews nothing", b.get("rental", {}).get("expires_at") == expires,
          str(b.get("rental")))
    alpha = proc(501, "load-a2b3c4d5", model="alpha")
    p.heartbeat(token, [alpha])
    card.readings = [9000]
    s, b = p.amend(token, need("beta", mib=16000), [alpha])
    check("an amend whose model does not fit now is refused: no_room, in words and numbers",
          s == 409 and b.get("reason") == "no_room"
          and b.get("memory") == memory_of(16000, BIG - 9000)
          and "beta" in b.get("error", "") and "16000 MiB" in b.get("error", ""), str(b))
    check("...LOUDLY (H9): what programs outside the bootstrap hold - the card's use less the "
          "rental's own - and what to do: close programs, or reboot",
          "programs outside the bootstrap hold 1000 MiB" in b.get("error", "")
          and "Close programs that use the card, or reboot" in b.get("error", ""),
          b.get("error", ""))
    said = [e for e in c.events if e["kind"] == "amend_refused"]
    check("...and the record says the same words",
          bool(said) and "programs outside the bootstrap hold 1000 MiB" in said[-1]["detail"],
          str(said[-1:])[:400])
    check("...and the rental goes on, as it was",
          c.rental is not None and c.rental["id"] == tid and c.rental["state"] == "held"
          and [m["key"] for m in c.rental["models"]] == ["alpha"], str(c.rental))
    s, b = p.amend(token, need("gamma", mib=BIG), [alpha])
    check("an amend that could never fit is refused never_fits, saying the whole card",
          s == 409 and b.get("reason") == "never_fits"
          and f"the whole card is {BIG} MiB" in b.get("error", ""), str(b))
    check("...both on the record, with their numbers",
          [(e["models"][0]["key"], e.get("memory", {}).get("need_mib"))
           for e in events_of(c, "amend_refused")] == [("beta", 16000), ("gamma", BIG)],
          str(events_of(c, "amend_refused")))
    beta = proc(502, "load-f6g7h8j9", model="beta")
    card.readings = [2000]              # alpha has come off, and its memory is back
    s, b = p.amend(token, need("beta", mib=16000), [])
    check("a switch: the rental's models follow what is on the card - beta, and alpha no "
          "longer (S6 R5)", s == 200 and [m["key"] for m in b["rental"]["models"]] == ["beta"],
          str(b.get("rental", {}).get("models")))
    p.heartbeat(token, [beta])
    s, b = p.amend(token, need("gamma", "whisper", 1000), [beta])
    check("...and a model beside another: both", s == 200
          and [(m["key"], m["need_mib"]) for m in b["rental"]["models"]]
          == [("beta", 16000), ("gamma", 1000)], str(b.get("rental", {}).get("models")))
    check("...each approval on the record, the rental's models after it",
          [[m["key"] for m in e["models"]] for e in events_of(c, "rental_amended")]
          == [["alpha"], ["beta"], ["beta", "gamma"]], str(events_of(c, "rental_amended")))
    again = Coordinator(tmp / "amend", hardware=Card(used=1000, total=BIG), clock=Clock())
    check("...and a coordinator restarted from its record knows the rental's models as its last "
          "amend left them", again.rental is not None
          and [m["key"] for m in again.rental["models"]] == ["beta", "gamma"], str(again.rental))

    # right after the rental's own unload: the memory is coming back
    def settled(name, readings, drop, settle_s=2.0, need_mib=4840):
        card_ = Card(used=1000, total=BIG)
        c_ = Coordinator(tmp / f"amend-{name}", hardware=card_, clock=Clock(),
                         amend_settle_s=settle_s)
        p_ = Probe(c_)
        _, token_ = granted(p_, W, models=[need("large-v3", "whisper", 4840)])
        up = proc(601, "load-m4n8q2r5", model="large-v3", engine="whisper")
        p_.heartbeat(token_, [up])
        if drop in ("heartbeat", "long ago"):
            p_.heartbeat(token_, [])
        if drop == "long ago":
            time.sleep(settle_s + 0.1)
        card_.readings = list(readings)
        reads, t0 = card_.fresh, time.monotonic()
        s_, b_ = p_.amend(token_, need("nb-large", "whisper", need_mib),
                          [up] if drop is None else [])
        return s_, b_, card_.fresh - reads, time.monotonic() - t0

    s, b, reads, took = settled("h3", [BIG - 3000, BIG - 3000, 2000], "amend")
    check("an amend right after one of the rental's own processes went waits for its memory "
          "to come back, then approves", s == 200 and reads == 3 and took < 1.5,
          f"{s} {b.get('reason')}: {reads} reads in {took:.2f}s")
    s, b, reads, took = settled("h3-beat", [BIG - 3000, 2000], "heartbeat")
    check("...a process a heartbeat said had gone, just before, as well",
          s == 200 and reads == 2, f"{s} {b.get('reason')}: {reads} reads in {took:.2f}s")
    s, b, reads, took = settled("h3-never", [BIG - 3000], "amend", settle_s=0.6)
    check("...within its bound: memory that never comes back is refused once the bound has "
          "passed", s == 409 and b.get("reason") == "no_room" and 0.5 <= took < 1.5
          and reads >= 2, f"{s} {b.get('reason')}: {reads} reads in {took:.2f}s")
    s, b, reads, took = settled("h3-none", [BIG - 3000, 2000], None)
    check("...while an amend with nothing of the rental just gone is judged at once, on one "
          "read", s == 409 and b.get("reason") == "no_room" and reads == 1 and took < 0.5,
          f"{s} {b.get('reason')}: {reads} reads in {took:.2f}s")
    s, b, reads, took = settled("h3-long-ago", [BIG - 3000, 2000], "long ago", settle_s=0.4)
    check("...and so is one whose process went longer ago than the bound: nothing more is "
          "coming back", s == 409 and b.get("reason") == "no_room" and reads == 1 and took < 0.3,
          f"{s} {b.get('reason')}: {reads} reads in {took:.2f}s")
    s, b, reads, took = settled("h3-huge", [BIG - 3000, 2000], "amend", need_mib=BIG)
    check("...and a model that could never fit is refused at once, even right after an "
          "unload: no memory coming back could hold it",
          s == 409 and b.get("reason") == "never_fits" and reads == 1 and took < 0.3,
          f"{s} {b.get('reason')}: {reads} reads in {took:.2f}s")

    # an amend on anything but a rental held is refused, with the reason
    clk = Clock()
    c = Coordinator(tmp / "amend-over", ttl_s=15, hardware=Card(), clock=clk,
                    claim_s=5, drop_s=10)
    p = Probe(c)
    m = need("demo-7b", mib=100)
    _, holding = granted(p, X)
    _, waiting, _ = asked(p, W)
    s, b = p.amend(waiting, m)
    check("an amend by a ticket that holds no rental is refused not_held",
          (s, b.get("reason")) == (409, "not_held"), str(b))
    p.cancel(waiting)
    s, b = p.amend(waiting, m)
    check("...by a cancelled one, cancelled", (s, b.get("reason")) == (409, "cancelled"), str(b))
    s, b = p.amend("guessed", m)
    check("...by a token no ticket holds, unknown_ticket",
          (s, b.get("reason")) == (409, "unknown_ticket"), str(b))
    _, dropped, _ = asked(p, A1)
    clk.t += 11
    c.reap()
    s, b = p.amend(dropped, m)
    check("...by a dropped one, dropped", (s, b.get("reason")) == (409, "dropped"), str(b))
    p.release(holding)
    s, b = p.amend(holding, m)
    check("...by a released one, released", (s, b.get("reason")) == (409, "released"), str(b))
    _, offered, st = asked(p, A2)
    clk.t += 6
    c.reap()
    s, b = p.amend(offered, m)
    check("...by one whose offer passed on, unclaimed",
          st == "offered" and (s, b.get("reason")) == (409, "unclaimed"), str(b))
    _, lapsed = granted(p, A3)
    clk.t += 16
    c.reap()
    s, b = p.amend(lapsed, m)
    check("...by one taken back, reclaimed", (s, b.get("reason")) == (409, "reclaimed"), str(b))
    _, fresh_token = granted(p, A4)
    s, b = p.amend(fresh_token, m, [dict(PROC, proof="srv-k3x9m2p7")])
    check("a list an amend names is held to a heartbeat's rules: bad_processes",
          (s, b.get("reason")) == (400, "bad_processes"), str(b))
    s, b = p.amend(fresh_token, None)
    check("...and an amend names its model: bad_request, in the contract's words",
          (s, b.get("reason"), b.get("error")) == (400, "bad_request",
                                                   '$.model: expected object, got null'), str(b))
    s, b = p.amend(fresh_token, {"key": "", "engine": "llamacpp", "need_mib": 1})
    check("...by its key", (s, b.get("reason")) == (400, "bad_request"), str(b))
    s, b = p.amend(fresh_token, need("demo-7b", mib=-1))
    check("...with a need of 0 MiB or more", (s, b.get("reason")) == (400, "bad_request"), str(b))


# ------------------------------------------------------- the watch (PLAN-4 D3)

#: a line of llama-server's log that says the card's memory ran out - b10724's
#: own words (ggml-cuda.cu, ggml_backend_cuda_buffer_type_alloc_buffer)
OOM = ("ggml_backend_cuda_buffer_type_alloc_buffer: allocating 2304.00 MiB on device 0: "
       "cudaMalloc failed: out of memory")
HUNG = "it said it was working, and its progress had not moved for {:g}s"


def beats(p: Probe, clock: Clock, token, until_t: float, every: float = 10.0, **work):
    """Heartbeat *token* every *every* seconds of the fake clock until it reads
    *until_t* - each saying `work` (a callable of the time is called for each
    beat, for work that moves) - and -> the last answer."""
    s, b = None, None
    while clock.t + every <= until_t:
        clock.t += every
        said = {k: (v(clock.t) if callable(v) else v) for k, v in work.items()}
        s, b = p.heartbeat(token, [PROC], **said)
        if s != 200:
            break
    return s, b


def hangs(tmp: Path) -> None:
    """PLAN-4 D3, a hang: the heartbeat goes on, and the work does not. A
    heartbeat says whether the rental is working and its progress - tokens
    and answers, which only real work moves. A rental that says it is working,
    whose progress has not moved for the hang limit - counted from when it
    began saying so, and never before a deadline a one-piece answer declared -
    is taken back as for a crash, outside the lock, and the record says why."""
    clock, card = Clock(), Card(used=600)
    c = Coordinator(tmp / "hang", hardware=card, clock=clock, hang_s=60)
    p = Probe(c)
    tid, token = granted(p, W)
    t0 = clock.t
    s, b = p.heartbeat(token, [PROC], working=True, progress=10)
    check("a heartbeat may say the rental is working, and its progress: renewed, and held",
          s == 200 and b["rental"]["state"] == "held", str(b)[:300])
    s, b = beats(p, clock, token, t0 + 50, working=True, progress=10)
    clock.t = t0 + 59.9
    s, b = p.heartbeat(token, [PROC], working=True, progress=10)
    check("...one working whose progress has not moved is held until the hang limit",
          s == 200 and b["rental"]["state"] == "held" and not events_of(c, "rental_hung"),
          str(b)[:300])
    clock.t = t0 + 60
    s, b = p.heartbeat(token, [PROC], working=True, progress=10)
    check("...and at the hang limit it is taken back as for a crash: its holder is answered "
          "reclaimed, in words that say why",
          (s, b.get("reason"), b.get("error"))
          == (409, "reclaimed", f"{tid} was reclaimed: " + HUNG.format(60)), str(b))
    until(lambda: events_of(c, "rental_reclaimed"), 5)
    hung = events_of(c, "rental_hung")
    check("...the record says why, in a line of its own",
          len(hung) == 1 and hung[0].get("ticket_id") == tid
          and hung[0].get("detail") == HUNG.format(60), str(hung))
    reclaiming = events_of(c, "rental_reclaiming")
    check("...then its reclaim, as a lapsed rental's, saying it was a hang",
          len(reclaiming) == 1 and reclaiming[0]["detail"].startswith("hung: " + HUNG.format(60))
          and kinds(c).index("rental_hung") < kinds(c).index("rental_reclaiming"),
          str(reclaiming))
    check("...and every process it named is ended on its own proof",
          len(card.ended) == 1 and bare(card.ended[0]) == [PROC]
          and len(events_of(c, "process_ended")) == 1, str(card.ended))
    s, b = p.heartbeat(token, [PROC], working=True, progress=11)
    check("...and it stays taken back, saying why", (s, b.get("reason"), b.get("error"))
          == (409, "reclaimed", f"{tid} was reclaimed: " + HUNG.format(60)), str(b))
    s, b = p.release(token)
    check("...on every route its token is refused on", (s, b.get("reason"), b.get("error"))
          == (409, "reclaimed", f"{tid} was reclaimed: " + HUNG.format(60)), str(b))
    again = Probe(Coordinator(tmp / "hang", hardware=Card(used=600), clock=clock, hang_s=60))
    s, b = again.heartbeat(token)
    check("...and after a restart, from the record", (s, b.get("reason"), b.get("error"))
          == (409, "reclaimed", f"{tid} was reclaimed: " + HUNG.format(60)), str(b))

    # progress that moves keeps the rental, however long the work
    clock = Clock()
    c = Coordinator(tmp / "hang-moving", hardware=Card(used=600), clock=clock, hang_s=60)
    p = Probe(c)
    _, token = granted(p, W)
    t0 = clock.t
    s, b = beats(p, clock, token, t0 + 400, working=True, progress=lambda t: int(t - t0))
    check("a rental whose progress moves is never taken back, however long it works",
          s == 200 and not events_of(c, "rental_hung"), str(b)[:200])
    last = int(clock.t - t0)
    s, b = beats(p, clock, token, clock.t + 50, working=True, progress=last)
    s, b = beats(p, clock, token, clock.t + 5, every=5, working=True, progress=last)
    check("...its clock starts again from its last move: held 55 s after it",
          s == 200 and not events_of(c, "rental_hung"), str(b)[:200])
    s, b = beats(p, clock, token, clock.t + 5, every=5, working=True, progress=last)
    check("...and taken back 60 s after it", s == 409 and b.get("reason") == "reclaimed",
          str(b)[:200])

    # a rental that does not say it is working is never hung
    clock = Clock()
    c = Coordinator(tmp / "hang-idle", hardware=Card(used=600), clock=clock, hang_s=60)
    p = Probe(c)
    _, token = granted(p, W)
    t0 = clock.t
    s, b = beats(p, clock, token, t0 + 300, working=False, progress=10)
    s2, b2 = beats(p, clock, token, clock.t + 300)
    check("a rental that says it is not working - or says nothing of its work - is never "
          "taken back for its progress", (s, s2) == (200, 200)
          and not events_of(c, "rental_hung"), str(b2)[:200])
    # ...and its clock starts when it begins saying it is working
    begun = clock.t + 10
    s, b = beats(p, clock, token, begun + 50, working=True, progress=10)
    clock.t = begun + 59.9
    s, b = p.heartbeat(token, [PROC], working=True, progress=10)
    check("the hang's clock starts when it begins saying it is working - not at its last "
          "move, 10 minutes before", s == 200 and not events_of(c, "rental_hung"), str(b)[:200])
    clock.t = begun + 60
    s, b = p.heartbeat(token, [PROC], working=True, progress=10)
    check("...and it is taken back the hang limit after", s == 409, str(b)[:200])

    # a heartbeat without `working` says it is not working
    clock = Clock()
    c = Coordinator(tmp / "hang-silent", hardware=Card(used=600), clock=clock, hang_s=60)
    p = Probe(c)
    _, token = granted(p, W)
    t0 = clock.t
    p.heartbeat(token, [PROC], working=True, progress=10)
    s, b = beats(p, clock, token, t0 + 40, working=True, progress=10)
    s, b = beats(p, clock, token, t0 + 120)
    check("a heartbeat that does not say it is working says it is not: no hang is seen",
          s == 200 and not events_of(c, "rental_hung"), str(b)[:200])

    # a one-piece answer's deadline: no hang before it
    clock = Clock()
    c = Coordinator(tmp / "hang-deadline", hardware=Card(used=600), clock=clock, hang_s=60)
    p = Probe(c)
    tid, token = granted(p, W)
    t0 = clock.t
    s, b = p.heartbeat(token, [PROC], working=True, progress=3, deadline_s=150)
    s, b = beats(p, clock, token, t0 + 140, working=True, progress=3,
                 deadline_s=lambda t: 150 - (t - t0))
    clock.t = t0 + 149.9
    s, b = p.heartbeat(token, [PROC], working=True, progress=3, deadline_s=0.1)
    check("an answer that comes in one piece declares its deadline: its rental is not hung "
          "before it, long past the hang limit", s == 200 and not events_of(c, "rental_hung"),
          str(b)[:200])
    st = p.state()[1]["watch"]
    check("...and /state says the work: working, its progress, when it last moved, and the "
          "deadline", st is not None
          and (st["working"], st["progress"], st["moved_at"], st["read_at"])
          == (True, 3, t0, None) and abs((st["due_at"] or 0) - (t0 + 150)) < 1e-3, str(st))
    clock.t = t0 + 150.5
    s, b = p.heartbeat(token, [PROC], working=True, progress=3)
    check("...and past its deadline, still not moved, it is hung",
          s == 409 and b.get("error") == f"{tid} was reclaimed: " + HUNG.format(60), str(b))
    clock = Clock()
    c = Coordinator(tmp / "hang-short", hardware=Card(used=600), clock=clock, hang_s=60)
    p = Probe(c)
    _, token = granted(p, W)
    t0 = clock.t
    p.heartbeat(token, [PROC], working=True, progress=3, deadline_s=20)
    s, b = beats(p, clock, token, t0 + 50, working=True, progress=3,
                 deadline_s=lambda t: max(0.0, 20 - (t - t0)))
    clock.t = t0 + 59.9
    s, b = p.heartbeat(token, [PROC], working=True, progress=3, deadline_s=0.0)
    check("a deadline shorter than the hang limit shortens nothing: held past it, until the "
          "hang limit", s == 200 and not events_of(c, "rental_hung"), str(b)[:200])

    # a count below zero is refused, and renews nothing
    clock = Clock()
    c = Coordinator(tmp / "hang-bad", hardware=Card(used=600), clock=clock, hang_s=60)
    p = Probe(c)
    _, token = granted(p, W)
    before = c.rental["expires_at"]
    clock.t += 5
    for bad, field in (({"working": True, "progress": -1}, "progress"),
                       ({"working": True, "progress": 1, "deadline_s": -0.5}, "deadline_s"),
                       ({"working": True, "progress": 1, "deadline_s": float("inf")},
                        "deadline_s"),
                       ({"memory_errors": -1}, "memory_errors")):
        s, b = p.heartbeat(token, [PROC], **bad)
        check(f"a heartbeat whose {field} is {bad[field]} is refused bad_work, renewing nothing",
              (s, b.get("reason")) == (400, "bad_work") and f"$.{field}" in b.get("error", "")
              and c.rental["expires_at"] == before, str(b))
    s, b = p.heartbeat(token, [PROC], working="yes")
    check("...and one whose work is not in the contract's shape, bad_request",
          (s, b.get("reason")) == (400, "bad_request"), str(b))

    # none of it under the coordinator's lock: the ending waits on the machine
    # while everyone else is answered
    clock, card = Clock(), Card(used=600)
    c = Coordinator(tmp / "hang-lock", hardware=card, clock=clock, hang_s=60)
    p = Probe(c)
    _, token = granted(p, W)
    p.heartbeat(token, [PROC], working=True, progress=1)
    card.hold = threading.Event()
    clock.t += 14
    p.heartbeat(token, [PROC], working=True, progress=1)
    clock.t += 14
    p.heartbeat(token, [PROC], working=True, progress=1)
    clock.t += 14
    p.heartbeat(token, [PROC], working=True, progress=1)
    clock.t += 14
    p.heartbeat(token, [PROC], working=True, progress=1)
    clock.t += 5
    reaper = threading.Thread(target=c.reap, daemon=True)
    reaper.start()
    until(lambda: card.waiting, 2)
    # CHANGED ON PURPOSE (H6, H5's register 7, as the grant's read above):
    # answered while the ending still waits, never within a second
    t1 = time.monotonic()
    s, b = p.ask("dictaphone", [need("small", "whisper")], actor=I)
    took = time.monotonic() - t1
    ending = card.waiting and not card.hold.is_set()
    check("a hung rental's processes are ended outside the coordinator's lock: an ask while "
          "its ending waits is answered while it still waits",
          ending and s == 200 and c.rental["state"] == "reclaiming",
          f"answered in {took:.2f}s, the ending {'still waiting' if ending else 'over'}, "
          f"{c.rental and c.rental['state']}")
    card.hold.set()
    reaper.join(5)
    until(lambda: events_of(c, "rental_reclaimed"), 5)


def degraded_card(tmp: Path) -> None:
    """PLAN-4 D3, the card over-full: the coordinator reads the card during a
    rental every watch_s - never under its lock - and free memory under the
    floor, or a memory error its holder reported from a model's log, marks the
    rental degraded. Its holder is told on its next heartbeat, in words and
    numbers; nothing new goes on the card; the rental is NOT ended for it; and
    it clears when the card has room again."""
    clock, card = Clock(), Card(used=600, total=BIG)
    # a rental that lives long, and a hang limit far away: the watch alone
    c = Coordinator(tmp / "degraded", hardware=card, clock=clock, watch_s=30, floor_mib=256,
                    ttl_s=1000, hang_s=100000)
    p = Probe(c)
    tid, token = granted(p, W, models=[need("demo-7b", mib=5120)])
    asked(p, I, holder="dictaphone")
    p.heartbeat(token, [PROC], working=True, progress=1)
    fresh = card.fresh
    clock.t += 29.9
    c.reap()
    check("the card is not read for a rental before watch_s", card.fresh == fresh,
          f"{card.fresh - fresh} reads")
    card.readings = [24400]                                 # 176 MiB free
    clock.t += 0.1
    c.reap()
    check("...then it is read afresh", card.fresh == fresh + 1, f"{card.fresh - fresh} reads")
    s, b = p.heartbeat(token, [PROC], working=True, progress=2)
    d = b.get("rental", {}).get("degraded")
    check("free memory under the floor marks the rental degraded, and its holder is told on "
          "its next heartbeat - why, in words and numbers",
          s == 200 and isinstance(d, dict) and d.get("reason") == "under_floor"
          and (d.get("free_mib"), d.get("floor_mib"), d.get("total_mib"), d.get("error"))
          == (176, 256, BIG, None) and d.get("since") == clock.t - 0
          and "176 MiB free of 24576" in d.get("why", "")
          and "floor of 256 MiB" in d.get("why", ""), str(b)[:600])
    check("...the rental is not ended for it: still held, and renewed",
          b["rental"]["state"] == "held" and not events_of(c, "rental_reclaiming"), str(b)[:200])
    check("...and the words name nobody else", names_nobody_else(b, I, "dictaphone"), str(b))
    lines = events_of(c, "rental_degraded")
    check("...the record says so, once, with what the holder was told",
          len(lines) == 1 and lines[0].get("degraded") == d and lines[0].get("ticket_id") == tid,
          str(lines))
    clock.t += 30
    c.reap()
    clock.t += 30
    c.reap()
    check("...every watch_s it is read again - and never a line per look",
          card.fresh == fresh + 3 and len(events_of(c, "rental_degraded")) == 1,
          f"{card.fresh - fresh} reads, {len(events_of(c, 'rental_degraded'))} lines")
    s, b = p.amend(token, need("writer-24b", mib=1000))
    check("while degraded nothing new goes on the card: an amend is refused degraded, saying "
          "why", (s, b.get("reason")) == (409, "degraded") and d["why"] in b.get("error", "")
          and b.get("error", "").startswith("writer-24b may not go on the card now"), str(b))
    check("...without reading the card for it", card.fresh == fresh + 3,
          f"{card.fresh - fresh} reads")
    card.readings = [20000]
    clock.t += 30
    c.reap()
    s, b = p.heartbeat(token, [PROC], working=True, progress=3)
    check("it clears when a look at the card finds it has room again",
          s == 200 and b["rental"]["degraded"] is None, str(b)[:300])
    back = events_of(c, "rental_restored")
    check("...on the record", len(back) == 1 and "room again" in back[0].get("detail", "")
          and "4576 MiB free" in back[0].get("detail", ""), str(back))
    s, b = p.amend(token, need("writer-24b", mib=1000))
    check("...and the amend is approved again", s == 200, str(b)[:300])

    # a memory error its holder reported from a model's log
    fresh = card.fresh
    s, b = p.heartbeat(token, [PROC], working=False, progress=3, memory_errors=1,
                       memory_error=OOM)
    d = b.get("rental", {}).get("degraded")
    check("a memory error reported from a model's log marks the rental degraded - said on the "
          "same beat's answer, with the line",
          s == 200 and isinstance(d, dict) and d.get("reason") == "memory_error"
          and d.get("error") == OOM and OOM in d.get("why", "")
          and (d.get("free_mib"), d.get("total_mib")) == (4576, BIG), str(b)[:600])
    check("...without reading the card", card.fresh == fresh, f"{card.fresh - fresh} reads")
    s, b = p.heartbeat(token, [PROC], working=False, progress=3, memory_errors=1,
                       memory_error=OOM)
    check("...the same count again is no news: one line",
          len(events_of(c, "rental_degraded")) == 2 and b["rental"]["degraded"] == d, str(b)[:300])
    clock.t += 30
    c.reap()
    s, b = p.heartbeat(token, [PROC], working=False, progress=3, memory_errors=1,
                       memory_error=OOM)
    check("...and it clears at the next look that finds the card has room",
          b["rental"]["degraded"] is None and len(events_of(c, "rental_restored")) == 2,
          str(b)[:300])
    s, b = p.heartbeat(token, [PROC], working=False, progress=3, memory_errors=2,
                       memory_error=OOM)
    check("...a count that rose again is news again",
          b["rental"]["degraded"] is not None and len(events_of(c, "rental_degraded")) == 3,
          str(b)[:300])

    # a look begun before a memory error was reported does not clear it
    class During(Card):
        during = None

        def probe(self, fresh=False):
            got = super().probe(fresh)
            if fresh and self.during is not None:
                act, self.during = self.during, None
                act()
            return got
    clock, card = Clock(), During(used=600, total=BIG)
    c = Coordinator(tmp / "degraded-during", hardware=card, clock=clock, watch_s=30,
                    ttl_s=1000, hang_s=100000)
    p = Probe(c)
    _, token = granted(p, W)
    card.during = lambda: p.heartbeat(token, [PROC], memory_errors=1, memory_error=OOM)
    clock.t += 30
    c.reap()
    s, b = p.heartbeat(token, [PROC], memory_errors=1, memory_error=OOM)
    check("a look at the card begun before a memory error was reported does not clear it, "
          "though it found room", b["rental"]["degraded"] is not None
          and not events_of(c, "rental_restored"), str(b)[:300])
    clock.t += 30
    c.reap()
    s, b = p.heartbeat(token, [PROC], memory_errors=1, memory_error=OOM)
    check("...the next one does", b["rental"]["degraded"] is None, str(b)[:300])
    # degraded while an amend's look at the card was under way: refused all the same
    clock, card = Clock(), During(used=600, total=BIG)
    c = Coordinator(tmp / "degraded-amend-during", hardware=card, clock=clock, watch_s=30,
                    ttl_s=1000, hang_s=100000)
    p = Probe(c)
    _, token = granted(p, W)
    card.during = lambda: p.heartbeat(token, [PROC], memory_errors=1, memory_error=OOM)
    s, b = p.amend(token, need("beta", mib=1000))
    check("an amend whose look at the card was under way when the rental was degraded is "
          "refused all the same", (s, b.get("reason")) == (409, "degraded"), str(b))

    # a restart: a count of memory errors its holder said before is not news again
    clock, card = Clock(), Card(used=600, total=BIG)
    c = Coordinator(tmp / "degraded-restart", hardware=card, clock=clock, watch_s=30,
                    ttl_s=1000, hang_s=100000)
    p = Probe(c)
    _, token = granted(p, W)
    p.heartbeat(token, [PROC], memory_errors=1, memory_error=OOM)
    clock.t += 30
    c.reap()
    again = Probe(Coordinator(tmp / "degraded-restart", hardware=Card(used=600, total=BIG),
                              clock=clock, watch_s=30, ttl_s=1000, hang_s=100000))
    s, b = again.heartbeat(token, [PROC], memory_errors=1, memory_error=OOM)
    check("after a restart, a count of memory errors its holder said before is not news "
          "again", s == 200 and b["rental"]["degraded"] is None
          and len(events_of(c, "rental_restored")) == 1, str(b)[:300])
    s, b = again.heartbeat(token, [PROC], memory_errors=2, memory_error=OOM)
    check("...and one that rose is", s == 200 and b["rental"]["degraded"] is not None,
          str(b)[:300])
    check("...its count kept on the record's line, for the next restart",
          [e.get("memory_errors") for e in events_of(again.c, "rental_degraded")
           if e["degraded"]["reason"] == "memory_error"] == [1, 2],
          str(events_of(again.c, "rental_degraded"))[:400])

    # H8 (S5b R3): a restart BETWEEN a memory error and the heartbeat that
    # says it - the count the rental had said before is known from the
    # record (none since its grant), so the error is news, and marked
    clock, card = Clock(), Card(used=600, total=BIG)
    c = Coordinator(tmp / "degraded-restart-unheard", hardware=card, clock=clock, watch_s=30,
                    ttl_s=1000, hang_s=100000)
    p = Probe(c)
    _, token = granted(p, W)
    p.heartbeat(token, [PROC], memory_errors=0)
    again = Probe(Coordinator(tmp / "degraded-restart-unheard", hardware=Card(used=600, total=BIG),
                              clock=clock, watch_s=30, ttl_s=1000, hang_s=100000))
    s, b = again.heartbeat(token, [PROC], memory_errors=1, memory_error=OOM)
    check("a memory error said first after a restart, none said before it: marked at once",
          s == 200 and (b["rental"]["degraded"] or {}).get("reason") == "memory_error",
          str(b)[:300])
    # ...and one said before it, marked and cleared: the count its line kept
    # is what was said, and the next one after the restart is news
    clock, card = Clock(), Card(used=600, total=BIG)
    c = Coordinator(tmp / "degraded-restart-kept", hardware=card, clock=clock, watch_s=30,
                    ttl_s=1000, hang_s=100000)
    p = Probe(c)
    _, token = granted(p, W)
    p.heartbeat(token, [PROC], memory_errors=1, memory_error=OOM)
    clock.t += 30
    c.reap()
    again = Probe(Coordinator(tmp / "degraded-restart-kept", hardware=Card(used=600, total=BIG),
                              clock=clock, watch_s=30, ttl_s=1000, hang_s=100000))
    s, b = again.heartbeat(token, [PROC], memory_errors=2, memory_error=OOM)
    check("one said before a restart and cleared, and one more said first after it: marked "
          "at once", s == 200 and (b["rental"]["degraded"] or {}).get("reason") == "memory_error"
          and len(events_of(c, "rental_restored")) == 1, str(b)[:300])
    # ...and a record an older coordinator wrote, whose degraded line has no
    # count: the count is not known, and taken as known, as before
    clock, card = Clock(), Card(used=600, total=BIG)
    c = Coordinator(tmp / "degraded-restart-old", hardware=card, clock=clock, watch_s=30,
                    ttl_s=1000, hang_s=100000)
    p = Probe(c)
    _, token = granted(p, W)
    p.heartbeat(token, [PROC], memory_errors=1, memory_error=OOM)
    rec = tmp / "degraded-restart-old" / "events.jsonl"
    lines = [json.loads(x) for x in rec.read_text(encoding="utf-8").splitlines()]
    for e in lines:
        e.pop("memory_errors", None)
    rec.write_bytes("".join(json.dumps(e) + "\n" for e in lines).encode("utf-8"))
    clock.t += 30
    c.reap()
    again = Probe(Coordinator(tmp / "degraded-restart-old", hardware=Card(used=600, total=BIG),
                              clock=clock, watch_s=30, ttl_s=1000, hang_s=100000))
    s, b = again.heartbeat(token, [PROC], memory_errors=3, memory_error=OOM)
    check("a record with no count on its degraded line: the first count after a restart is "
          "taken as known", s == 200 and b["rental"]["degraded"] is None, str(b)[:300])

    # a card that cannot be read decides nothing
    clock, card = Clock(), Card(used=600, total=BIG)
    c = Coordinator(tmp / "degraded-unknown", hardware=card, clock=clock, watch_s=30,
                    ttl_s=1000, hang_s=100000)
    p = Probe(c)
    _, token = granted(p, W)
    card.readings = [None]
    clock.t += 30
    c.reap()
    s, b = p.heartbeat(token, [PROC])
    check("a card that cannot be read marks nothing degraded", b["rental"]["degraded"] is None,
          str(b)[:300])
    p.heartbeat(token, [PROC], memory_errors=1, memory_error=OOM)
    clock.t += 30
    c.reap()
    s, b = p.heartbeat(token, [PROC], memory_errors=1, memory_error=OOM)
    d = b["rental"]["degraded"]
    check("...and clears nothing: the numbers it could not read are null",
          d is not None and (d["free_mib"], d["total_mib"]) == (None, None), str(b)[:300])

    # the floor is a setting
    clock, card = Clock(), Card(used=600, total=BIG)
    c = Coordinator(tmp / "degraded-floor", hardware=card, clock=clock, watch_s=30,
                    floor_mib=5000, ttl_s=1000, hang_s=100000)
    p = Probe(c)
    _, token = granted(p, W)
    card.readings = [20000]                                 # 4576 MiB free
    clock.t += 30
    c.reap()
    s, b = p.heartbeat(token, [PROC])
    check("the floor is a setting: 4576 MiB free is under a floor of 5000",
          (b["rental"]["degraded"] or {}).get("floor_mib") == 5000, str(b)[:300])

    # what the operator sees: the card outside the bootstrap, the watch, a restart
    clock, card = Clock(), Card(used=600, total=BIG)
    c = Coordinator(tmp / "degraded-state", hardware=card, clock=clock, watch_s=30,
                    ttl_s=1000, hang_s=100000)
    p = Probe(c)
    _, token = granted(p, W, models=[need("demo-7b", mib=5120)])
    p.amend(token, need("beta", mib=2000), [PROC])
    two = [dict(PROC, mib=5000), proc(18350, "load-m4n8q2r5", model="beta")]
    p.heartbeat(token, two)
    card.readings = [24400]
    clock.t += 30
    c.reap()
    st = p.state()[1]
    check("/state says how much of the card is outside the bootstrap: its use, less what the "
          "rental's processes use - measured, or else the need each was approved with",
          (st["card"]["ours_mib"], st["card"]["outside_mib"]) == (7000, 24400 - 7000)
          and st["watch"]["read_at"] == clock.t, str(st["card"]) + str(st["watch"]))
    p.heartbeat(token, [dict(PROC, mib=None, model="gamma")])
    st = p.state()[1]
    check("...and says it is not known when one process has neither",
          (st["card"]["ours_mib"], st["card"]["outside_mib"]) == (None, None), str(st["card"]))
    was = p.state()[1]["rental"]["degraded"]
    again = Probe(Coordinator(tmp / "degraded-state", hardware=Card(used=24400, total=BIG),
                              clock=clock, watch_s=30))
    check("a restart keeps a degraded rental degraded, from the record",
          was is not None and again.state()[1]["rental"]["degraded"] == was,
          str(again.state()[1]["rental"]))
    st = Probe(Coordinator(tmp / "degraded-none", hardware=Card(used=900, total=BIG),
                           clock=Clock())).state()[1]
    check("...and with no rental, all of the card's use is outside it",
          (st["card"]["ours_mib"], st["card"]["outside_mib"], st["watch"]) == (0, 900, None),
          str(st["card"]))

    # the look at the card is made OUTSIDE the lock: everyone else is answered
    class Slow(Card):
        gate, inside = None, False

        def probe(self, fresh=False):
            if fresh and self.gate is not None:
                self.inside = True
                self.gate.wait(5)
            return super().probe(fresh)
    clock, card = Clock(), Slow(used=600, total=BIG)
    c = Coordinator(tmp / "degraded-lock", hardware=card, clock=clock, watch_s=30,
                    ttl_s=1000, hang_s=100000)
    p = Probe(c)
    _, token = granted(p, W)
    card.gate = threading.Event()
    clock.t += 30
    reaper = threading.Thread(target=c.reap, daemon=True)
    reaper.start()
    until(lambda: card.inside, 2)
    t1 = time.monotonic()
    s, b = p.heartbeat(token, [PROC])
    took = time.monotonic() - t1
    card.gate.set()
    reaper.join(5)
    check("...and the look at the card during a rental is made outside the coordinator's "
          "lock: its holder's heartbeat meanwhile is answered at once",
          card.inside and s == 200 and took < 1.0, f"{took:.2f}s")


def recovering(tmp: Path) -> None:
    """S5b, the card full recovered by itself (the owner, 2026-09-27: the
    person closes programs, and the held request runs again by itself): while
    a rental is degraded the card is read every recover_watch_s, not every
    watch_s, so room again is noticed within seconds; it clears on the first
    read with room for what the rental needs - its models' need, less what its
    processes hold now, plus the floor - never merely over the floor; and the
    degraded state says the numbers a person acts on, refreshed at each read,
    with a line of the record only when it is marked and when it clears."""
    clock, card = Clock(), Card(used=600, total=BIG)
    c = Coordinator(tmp / "recover", hardware=card, clock=clock, watch_s=30,
                    recover_watch_s=2, floor_mib=256, ttl_s=1000, hang_s=100000)
    p = Probe(c)
    _, token = granted(p, W, models=[need("demo-7b", mib=6000)])
    p.heartbeat(token, [PROC])
    card.readings = [24400]                                 # 176 MiB free
    clock.t += 30
    c.reap()
    s, b = p.heartbeat(token, [])                           # its model came off: gone
    d = b["rental"]["degraded"]
    check("degraded, it says the numbers a person acts on: the free memory, the room it waits "
          "for - its model on the card, the floor - and what is used outside the bootstrap",
          d is not None and (d.get("free_mib"), d.get("need_mib"), d.get("outside_mib"))
          == (176, 256, 24400 - 6000), str(d))
    fresh = card.fresh
    clock.t += 1.9
    c.reap()
    check("while degraded the card is not read again before recover_watch_s",
          card.fresh == fresh, f"{card.fresh - fresh} reads")
    card.readings = [24276]                                 # 300 MiB free: over the floor
    clock.t += 0.1
    c.reap()
    check("...then it is: every recover_watch_s, not every watch_s",
          card.fresh == fresh + 1, f"{card.fresh - fresh} reads")
    s, b = p.heartbeat(token, [])
    d = b["rental"]["degraded"]
    check("a card over the floor does not clear a rental whose model came off and needs 6000 "
          "MiB: 300 MiB free is not room for it - its numbers the newest read's, and no line "
          "of the record for the look",
          d is not None and (d.get("free_mib"), d.get("need_mib"), d.get("outside_mib"))
          == (300, 6256, 24276) and not events_of(c, "rental_restored")
          and len(events_of(c, "rental_degraded")) == 1, str(d))
    card.readings = [24450]                                 # 126 MiB free: under it again
    clock.t += 2
    c.reap()
    s, b = p.heartbeat(token, [])
    d = b["rental"]["degraded"]
    check("...and under the floor again, still one mark, its numbers the newest read's",
          d is not None and (d.get("free_mib"), d.get("reason")) == (126, "under_floor")
          and len(events_of(c, "rental_degraded")) == 1, str(d))
    card.readings = [18320]                                 # 6256 MiB free
    clock.t += 2
    c.reap()
    s, b = p.heartbeat(token, [])
    back = events_of(c, "rental_restored")
    check("it clears on the first read with room for what the rental needs, plus the floor",
          b["rental"]["degraded"] is None and len(back) == 1, str(b["rental"])[:300])
    check("...the record saying the numbers: the free memory, and the room it needed",
          back and "6256 MiB free" in back[0].get("detail", "")
          and "6256 MiB needed" in back[0].get("detail", ""), str(back))
    fresh = card.fresh
    clock.t += 2
    c.reap()
    check("...and, room again, the card is read every watch_s once more",
          card.fresh == fresh, f"{card.fresh - fresh} reads")
    clock.t += 28
    c.reap()
    check("...every watch_s", card.fresh == fresh + 1, f"{card.fresh - fresh} reads")

    # its model still on the card: the floor is room enough
    clock, card = Clock(), Card(used=600, total=BIG)
    c = Coordinator(tmp / "recover-held", hardware=card, clock=clock, watch_s=30,
                    recover_watch_s=2, floor_mib=256, ttl_s=1000, hang_s=100000)
    p = Probe(c)
    _, token = granted(p, W, models=[need("demo-7b", mib=6000)])
    p.heartbeat(token, [PROC])
    card.readings = [24400]
    clock.t += 30
    c.reap()
    card.readings = [24276]                                 # 300 MiB free
    clock.t += 2
    c.reap()
    s, b = p.heartbeat(token, [PROC])
    check("a rental whose model is still on the card needs only the floor free: 300 MiB "
          "clears it", b["rental"]["degraded"] is None
          and len(events_of(c, "rental_restored")) == 1, str(b["rental"])[:300])

    # a memory error marks it, and the card is read within recover_watch_s
    clock, card = Clock(), Card(used=600, total=BIG)
    c = Coordinator(tmp / "recover-oom", hardware=card, clock=clock, watch_s=30,
                    recover_watch_s=2, ttl_s=1000, hang_s=100000)
    p = Probe(c)
    _, token = granted(p, W, models=[need("demo-7b", mib=5120)])
    clock.t += 5
    p.heartbeat(token, [PROC], memory_errors=1, memory_error=OOM)
    fresh = card.fresh
    clock.t += 2
    c.reap()
    s, b = p.heartbeat(token, [PROC], memory_errors=1, memory_error=OOM)
    check("a memory error brings the next read to recover_watch_s from its mark, not the "
          "watch_s it was due at - and that read, with room, clears it",
          card.fresh == fresh + 1 and b["rental"]["degraded"] is None, str(b["rental"])[:300])

    # the setting, and what the operator sees
    check("recover_watch_s is on /state's limits",
          Probe(c).state()[1]["limits"].get("recover_watch_s") == 2.0,
          str(Probe(c).state()[1]["limits"]))
    for bad in (0, -1, True, "2"):
        try:
            Coordinator(tmp / "recover-bad", hardware=Card(), clock=Clock(),
                        recover_watch_s=bad)
            refused = False
        except ValueError:
            refused = True
        check(f"recover_watch_s={bad!r} is refused", refused)


SLOW = ("an answer of demo-7b was 4.2 times slower than its normal: 11.9 tokens a second, "
        "against 50.0")


def slow_answers(tmp: Path) -> None:
    """PLAN-4 D3 with D12 (S11b): an answer far slower than its model's own
    normal - its holder's runner judges each against its own statistics, and
    counts them on every heartbeat (`slow_answers`, the newest in words,
    `slow_answer`) - is the card over-full too, when the card as this
    coordinator last read it had less free than it counts as full
    (`full_mib`): the rental is marked degraded (`slow_answer`), and never
    ended for it. On a card with room, or one not read yet, a slow answer is
    not the card's doing, and is only counted."""
    clock, card = Clock(), Card(used=600, total=BIG)
    c = Coordinator(tmp / "slow", hardware=card, clock=clock, watch_s=30, floor_mib=256,
                    full_mib=1024, ttl_s=1000, hang_s=100000)
    p = Probe(c)
    _, token = granted(p, W, models=[need("demo-7b", mib=5120)])
    s, b = p.heartbeat(token, [PROC], working=False, progress=1, slow_answers=1,
                       slow_answer=SLOW)
    check("a slow answer said before the card was read for the rental is only counted",
          s == 200 and b["rental"]["degraded"] is None, str(b)[:300])
    card.readings = [BIG - 5000]                            # 5000 MiB free
    clock.t += 30
    c.reap()
    s, b = p.heartbeat(token, [PROC], working=False, progress=2, slow_answers=2,
                       slow_answer=SLOW)
    check("...and on a card with room - 5000 MiB free, over the 1024 it counts as full - it "
          "is only counted",
          s == 200 and b["rental"]["degraded"] is None
          and events_of(c, "rental_degraded") == [], str(b)[:300])
    # S11b R2: counted, and shown - /state's watch carries the count its
    # holder's last heartbeat said, never only the mark it may lead to
    st = p.state()[1]["watch"] or {}
    check("...and /state's watch shows the count: 2 answers far slower than their model's "
          "normal, though the card has room and nothing is marked",
          st.get("slow_answers") == 2 and c.rental["degraded"] is None, str(st))
    card.readings = [BIG - 800]                             # 800 MiB: over the floor, full
    clock.t += 30
    c.reap()
    s, b = p.heartbeat(token, [PROC], working=False, progress=3, slow_answers=2,
                       slow_answer=SLOW)
    check("a count that did not rise is no news, even on a full card",
          s == 200 and b["rental"]["degraded"] is None, str(b)[:300])
    s, b = p.heartbeat(token, [PROC], working=False, progress=3, slow_answers=3,
                       slow_answer=SLOW)
    d = b.get("rental", {}).get("degraded")
    check("a slow answer while the card, as last read, has less free than it counts as full "
          "marks the rental degraded - said on the same beat's answer, with its words and "
          "the card's numbers",
          s == 200 and isinstance(d, dict) and d.get("reason") == "slow_answer"
          and d.get("error") == SLOW and SLOW in d.get("why", "")
          and "1024 MiB" in d.get("why", "")
          and (d.get("free_mib"), d.get("total_mib")) == (800, BIG), str(b)[:600])
    marked = events_of(c, "rental_degraded")
    check("...on the record", len(marked) == 1
          and marked[0].get("degraded", {}).get("reason") == "slow_answer", str(marked))
    check("...and the rental is not ended for it", c.rental["state"] == "held",
          c.rental["state"])
    card.readings = [BIG - 5000]
    clock.t += 30
    c.reap()
    s, b = p.heartbeat(token, [PROC], working=False, progress=3, slow_answers=3,
                       slow_answer=SLOW)
    check("...it clears at the next look that finds the card has room",
          b["rental"]["degraded"] is None, str(b)[:300])
    s, b = p.heartbeat(token, [PROC], working=False, progress=3, slow_answers=4,
                       slow_answer=SLOW)
    check("...and a slow answer then is counted again, and only counted",
          b["rental"]["degraded"] is None and len(events_of(c, "rental_degraded")) == 1,
          str(b)[:300])
    st = p.state()[1]["watch"] or {}
    check("...its count on /state's watch the one the last heartbeat said: 4",
          st.get("slow_answers") == 4, str(st))
    before = c.rental["expires_at"]
    clock.t += 1
    s, b = p.heartbeat(token, [PROC], slow_answers=-1)
    check("a count of slow answers below zero is refused bad_work, renewing nothing",
          (s, b.get("reason")) == (400, "bad_work") and "$.slow_answers" in b.get("error", "")
          and c.rental["expires_at"] == before, str(b))
    for bad in ({"slow_answers": 1.5}, {"slow_answer": 7}):
        s, b = p.heartbeat(token, [PROC], **bad)
        check(f"...and {bad} is not in the contract's shape: bad_request",
              (s, b.get("reason")) == (400, "bad_request"), str(b))


def the_lock(tmp: Path) -> None:
    """PLAN-4 D5: the machine's lock says who holds the card at the top. The
    coordinator takes it as it starts and holds it while it runs, its note
    giving its address; started while an application runs on its own, it
    waits for it - granting nothing meanwhile - and its page says so. A
    coordinator built in code takes none."""
    s, st = Probe(Coordinator(tmp / "lock-none", clock=Clock())).state()
    check("a coordinator built in code holds no lock, and says so",
          st["lock"]["state"] == "none" and st["lock"]["path"] is None, str(st["lock"]))
    note_name = LOCK_DOC["note_file"][len(LOCK_DOC["file"]):]
    shipped = importlib.util.spec_from_file_location("machine_as_shipped", HERE / "machine.py")
    pristine = importlib.util.module_from_spec(shipped)
    shipped.loader.exec_module(pristine)
    check("the machine's own lock is the contract's, in the user's temporary folder",
          pristine.machine_lock() == Path(tempfile.gettempdir()) / LOCK_DOC["file"]
          and pristine.MachineLock(tmp / "x.lock").note_path.name == "x.lock" + note_name,
          str(pristine.machine_lock()))
    try:
        C.card_lock_of(C.parse_args(["--data", str(tmp / "lock-cli")]))
        asked_machine = False
    except AssertionError:
        asked_machine = True
    check("...which a coordinator started without --card-lock takes (this suite's tripwire "
          "said so)", asked_machine)
    check("...and --card-lock names another",
          C.card_lock_of(C.parse_args(["--data", str(tmp / "lock-cli"), "--card-lock",
                                       str(tmp / "mine.lock")])) == tmp / "mine.lock")

    path = tmp / "lock-a" / "card.lock"
    c = Coordinator(tmp / "lock-a-data", clock=Clock())
    port = quiet_port()
    srv = C.serve(c, port, card_lock=path, log=lambda line_: None)
    other = M.MachineLock(path)
    try:
        note = json.loads(other.note_path.read_text(encoding="utf-8"))
        check("served with a lock, the coordinator takes it at once, and its note gives its "
              "address and actor", note.get("coordinator") == f"http://127.0.0.1:{port}"
              and note.get("actor") == c.actor and note.get("pid") == os.getpid(), str(note))
        check("...a note in the contract's shape", not V.check(note, LOCK_DOC["note"], SHAPES),
              str(V.check(note, LOCK_DOC["note"], SHAPES)))
        check("...and it holds it: nobody else can take it", not other.take())
        st = state(f"http://127.0.0.1:{port}")
        check("...its page says it holds it, and where", st["lock"]["state"] == "held"
              and st["lock"]["path"] == str(path) and f"127.0.0.1:{port}" in st["lock"]["detail"],
              str(st["lock"]))
        check("...and its record", [e.get("detail", "") for e in events_of(c, "lock_taken")]
              and f"127.0.0.1:{port}" in events_of(c, "lock_taken")[0].get("detail", ""),
              str(kinds(c)))
        said = st["lock"]["detail"]
        check("...a lock of its own is said to be its own, never the machine's (S16a R4): where "
              "it is, and that a server finds the coordinator only through it or --coordinator",
              "the machine's lock" not in said and "every server on this machine" not in said
              and "its own lock" in said and str(path) in said and "--coordinator" in said
              and "card_lock" in said, said)
        words = getattr(C, "lock_taken_words", lambda p, a: "")
        machines = words(Path(tempfile.gettempdir()) / LOCK_DOC["file"], "http://127.0.0.1:1")
        check("...while the machine's own lock is said as the machine's, where every server on "
              "it finds the coordinator (its words only: this suite never takes that lock)",
              "holds the machine's lock" in machines
              and "every server on this machine finds it" in machines, machines)
    finally:
        C.stop(srv)
    check("stopped, it lets the lock go and takes its note away",
          other.take() and not other.note_path.exists())
    other.release()

    path = tmp / "lock-b" / "card.lock"
    app = M.MachineLock(path)
    check("an application run on its own holds the lock", app.take())
    # CHANGED ON PURPOSE (H6): the note names the process that holds the lock -
    # this suite's - as a holder's note does: a note whose pid is not a live
    # process that started before it is stale, and names nobody
    app.write_note({"pid": os.getpid(), "since": time.time(), "holder": "srv-k3x9m2p7",
                    "model": "demo-7b", "for": None})
    c = Coordinator(tmp / "lock-b-data", clock=Clock())
    port = quiet_port()
    srv = C.serve(c, port, card_lock=path, log=lambda line_: None)
    try:
        st = state(f"http://127.0.0.1:{port}")
        check("a coordinator started while an application runs on its own waits for the lock, "
              "and its page says so, and who holds it",
              st["lock"]["state"] == "waiting" and f"(pid {os.getpid()}, " in st["lock"]["detail"]
              and "srv-k3x9m2p7" in st["lock"]["detail"] and "stale" not in st["lock"]["detail"],
              str(st["lock"]))
        check("...and its record", len(events_of(c, "lock_waiting")) == 1, str(kinds(c)))
        s, b = wire(f"http://127.0.0.1:{port}", "coordinator/card-ask",
                    {"holder": "writer-app", "actor": W, "models": [need("demo-7b")]})
        check("...it takes tickets, and offers the card to none while it waits",
              s == 200 and b["ticket"]["state"] == "waiting", str(b))
        time.sleep(0.3)
        check("...not even a moment later", c.line[b["ticket"]["id"]]["state"] == "waiting")
        app.release()
        took = wait_for(lambda: c.lock["state"] == "held", 5)
        check("...once the application lets go, the coordinator takes the lock",
              took is not None, str(c.lock))
        check("...and the waiting ticket is offered the card",
              until(lambda: c.line.get(b["ticket"]["id"], {}).get("state") == "offered", 2),
              str(c.line))
        note = json.loads(app.note_path.read_text(encoding="utf-8"))
        check("...and the note is the coordinator's now",
              note.get("coordinator") == f"http://127.0.0.1:{port}", str(note))
    finally:
        C.stop(srv)

    # restarted from its record, a coordinator has an offer out before it has
    # tried the lock - and takes it back when the lock is an application's
    data = tmp / "lock-d-data"
    tid, token, before = asked(Probe(Coordinator(data, clock=Clock())), W)
    path = tmp / "lock-d" / "card.lock"
    app = M.MachineLock(path)
    app.take()
    app.write_note({"pid": os.getpid(), "since": time.time(), "holder": "srv-k3x9m2p7",
                    "model": None, "for": None})
    again = Coordinator(data, clock=Clock())
    rebuilt = again.line[tid]["state"]
    srv = C.serve(again, quiet_port(), card_lock=path, log=lambda line_: None)
    try:
        check("a coordinator rebuilt from its record, finding the machine's lock is an "
              "application's, takes back the offer it made as it rebuilt",
              (before, rebuilt, again.line[tid]["state"]) == ("offered", "offered", "waiting"),
              f"{before} -> {rebuilt} -> {again.line[tid]['state']}")
        s, b = Probe(again).claim(tid, token)
        check("...so nobody claims the card while somebody else has it",
              (s, b.get("reason")) == (409, "not_offered"), str(b))
    finally:
        C.stop(srv)
        app.release()

    # one coordinator per machine: a second one started on the same lock
    # says where the first is, and does not start
    path = tmp / "lock-c" / "card.lock"
    first_port = quiet_port()
    first = C.serve(Coordinator(tmp / "lock-c1", clock=Clock()), first_port, card_lock=path,
                    log=lambda line_: None)
    try:
        second = subprocess.run(of_its_own([sys.executable, str(HERE / "coordinator.py"),
                                            "--hardware", "none", "--data", str(tmp / "lock-c2"),
                                            "--port", str(quiet_port(avoid=(first_port,))),
                                            "--card-lock", str(path)]),
                                capture_output=True, encoding="utf-8", timeout=30, cwd=HERE)
        check("a second coordinator on the machine does not start, and says where the first is",
              second.returncode == 2 and f"http://127.0.0.1:{first_port}" in second.stdout,
              f"{second.returncode}: {second.stdout[-300:]}{second.stderr[-300:]}")
        check("...before it writes a line of its record",
              not (tmp / "lock-c2" / "events.jsonl").exists())
        third = None
        third_port = quiet_port(avoid=(first_port,))
        let_go = None
        try:
            third = C.serve(Coordinator(tmp / "lock-c3", clock=Clock()), third_port,
                            card_lock=path, log=lambda line_: None)
            raised = ""
        except C.AnotherCoordinator as e:
            raised = str(e)
            # asked while the refusal - and all it holds - is still here: the
            # port is let go as the start stops, never when the error is gone
            let_go = bindable_here(third_port)
        finally:
            if third is not None:
                C.stop(third)
        check("...and one served in code on that lock is refused too, saying where the first is",
              f"http://127.0.0.1:{first_port}" in raised, raised or "it served")
        check("...its port let go as it is refused", let_go is True, str(let_go))
    finally:
        C.stop(first)


def holder_work(tmp: Path) -> None:
    """S5's R5: holder.py sent no `working` or `progress`, so an application
    built on it was never watched for a hang - its heartbeat said nothing of
    its work, and one that hung held the card until its heartbeats stopped. A
    holder given its work's function says it on every heartbeat, counted
    from the grant, as the server's card source does - and one working whose
    progress does not move is taken back at the hang limit."""
    clock = Clock()
    # a lifetime longer than the hang limit, on the suite's clock: the hang is
    # what takes it back, never a lapse - each heartbeat is one the holder
    # sends at once, naming a process (`running`)
    c = Coordinator(tmp / "holder-work", hardware=Card(used=600), clock=clock, ttl_s=120,
                    hang_s=60)
    port = quiet_port()
    srv = C.serve(c, port, log=lambda line_: None)
    now = {"working": False, "progress": 1200, "deadline_s": None, "memory_errors": 2,
           "memory_error": "an older one"}
    h = Holder(f"http://127.0.0.1:{port}", "writer-app", "demo-7b", patience_s=10)
    h.work = lambda: dict(now)
    try:
        h.acquire()
        now.update(working=True, progress=1207)
        h.running(4242, "load-a2b3c4d5")                    # a heartbeat, at once
        watch = c.state()[1]["watch"] or {}
        check("a holder given its work says it on its heartbeat - working, and its progress "
              "counted from the grant", (watch.get("working"), watch.get("progress")) == (True, 7),
              str(watch))
        clock.t += 61                                       # its progress never moved
        h.running(4243, "load-a2b3c4d6")                    # its next heartbeat
        check("...and one working whose progress does not move is taken back at the hang limit: "
              "the holder hears it lost the card, and the record says it hung",
              h.lost == "reclaimed" and len(events_of(c, "rental_hung")) == 1,
              f"lost {h.lost}; {kinds(c)[-6:]}")
    finally:
        h.release()
        C.stop(srv)
    plain = Holder("http://127.0.0.1:9", "writer-app", "demo-7b")
    check("...a holder given no work says nothing of it: its heartbeat says it is not working",
          plain.said() == {}, str(plain.said()))


def a_dead_pid() -> tuple:
    """A holder that died: a process of this suite's own that has ended ->
    (its pid, a moment it was alive at) - what the note it left behind said."""
    child = subprocess.Popen([sys.executable, "-c", "pass"], creationflags=NO_WINDOW)
    since = time.time()
    killed(child)
    return child.pid, since


class JustTaken(M.MachineLock):
    """The machine's lock as a coordinator finds it in the instant after
    somebody took it: the first look at the note beside it reads what was
    there before - none, or a dead holder's - and the holder writes its own
    just after that look (`then`)."""

    def __init__(self, path, then):
        super().__init__(path)
        self.looks, self.then = 0, then

    def note(self):
        got = super().note()
        self.looks += 1
        if self.looks == 1:
            self.then()
        return got


def served_or_refused(c: Coordinator, port: int, lock) -> tuple:
    """`serve` on *lock*: -> (the server, or None; the refusal's words, or "")."""
    try:
        return C.serve(c, port, card_lock=lock, log=lambda line_: None), ""
    except C.AnotherCoordinator as e:
        return None, str(e)


def the_note(tmp: Path) -> None:
    """H5's register 6 and 3: the note beside the machine's lock is its
    holder's only while the process that wrote it lives. A holder that dies
    lets go of the lock and leaves its note: a dead test coordinator's note was
    found beside the machine's own lock. Read in the instant after the next
    holder took the lock - or beside one whose holder never says who it is -
    it named a coordinator that had gone: a coordinator started then was
    refused as a second one, and its page named the dead one as the holder.
    And a coordinator started in the instant after another took the lock,
    before its note was written, waited as if an application held it."""
    dead, when = a_dead_pid()
    dead_note = {"pid": dead, "since": when, "coordinator": "http://127.0.0.1:30087",
                 "actor": "crd-m4n8p2q6"}
    handed_on = {"pid": os.getpid(), "since": 1_000_000_000.0,
                 "coordinator": "http://127.0.0.1:30087", "actor": "crd-m4n8p2q6"}
    for name, note, why in (("a", dead_note, f"pid {dead}, which wrote it, has gone"),
                            ("b", handed_on, f"pid {os.getpid()} is a process started after "
                                             f"the note was written")):
        path = tmp / f"note-{name}" / "card.lock"
        app = M.MachineLock(path)
        app.take()                                  # somebody who says nothing holds it...
        app.write_note(note)                        # ...beside the note a dead holder left
        c = Coordinator(tmp / f"note-{name}-data", clock=Clock())
        port = quiet_port()
        srv, raised = served_or_refused(c, port, path)
        try:
            check(f"a note whose writer has gone ({why}) - beside a lock somebody else holds - "
                  f"names no coordinator: one started then is not refused as a second one",
                  srv is not None, raised)
            if srv is None:
                continue
            detail = state(f"http://127.0.0.1:{port}")["lock"]
            check("...it waits for the lock, and its page says the note is stale - why, and "
                  "what it claimed - never that the dead one holds the card",
                  detail["state"] == "waiting" and "stale" in detail["detail"]
                  and why in detail["detail"] and "http://127.0.0.1:30087" in detail["detail"]
                  and "holds it (pid" not in detail["detail"], str(detail))
            app.release()
            took = wait_for(lambda: c.lock["state"] == "held", 5)
            mine = json.loads(app.note_path.read_text(encoding="utf-8"))
            check("...and once the lock is let go it takes it, and the note is its own",
                  took is not None and mine.get("pid") == os.getpid()
                  and mine.get("coordinator") == f"http://127.0.0.1:{port}", str(mine))
        finally:
            if srv is not None:
                C.stop(srv)
            app.release()

    # the instant after another holder took the lock, its note not yet written
    live = C.lock_note("127.0.0.1", 7071, "crd-a2b3c4d5")
    for name, before, after, want in (
            ("c", None, live, "refused"), ("d", dead_note, live, "refused"),
            ("e", dead_note, {"pid": os.getpid(), "since": time.time(), "holder": W,
                              "model": None, "for": None}, "waits")):
        path = tmp / f"note-{name}" / "card.lock"
        first = M.MachineLock(path)
        first.take()
        if before is not None:
            first.write_note(before)
        c = Coordinator(tmp / f"note-{name}-data", clock=Clock())
        port = quiet_port()
        srv, raised = served_or_refused(c, port,
                                        JustTaken(path, lambda n=after: first.write_note(n)))
        try:
            if want == "refused":
                check(f"a coordinator started in the instant after another took the lock - its "
                      f"note {'none yet' if before is None else 'a dead one'} - gives it a moment "
                      f"to write its own, and is refused as the second coordinator it is, naming "
                      f"the live one", srv is None and "http://127.0.0.1:7071" in raised
                      and "30087" not in raised, raised or "it served, waiting for the lock")
            else:
                detail = state(f"http://127.0.0.1:{port}")["lock"]["detail"] if srv else ""
                check("...and one that finds an application's note written in that moment - a "
                      "dead coordinator's there before it - waits for the lock, naming the "
                      "application", srv is not None and W in detail and "30087" not in detail,
                      raised or detail)
        finally:
            if srv is not None:
                C.stop(srv)
            first.release()

    # a lock whose holder never says who it is: given the moment, and no more
    path = tmp / "note-f" / "card.lock"
    first = M.MachineLock(path)
    first.take()
    c = Coordinator(tmp / "note-f-data", clock=Clock())
    port = quiet_port()
    t0 = time.monotonic()
    srv, raised = served_or_refused(c, port, path)
    took = time.monotonic() - t0
    try:
        check("a lock held with no note at all is given the moment for its holder to say who it "
              "is, and no more: the coordinator waits for it",
              srv is not None and C.NOTE_WAIT_S <= took < C.NOTE_WAIT_S + 10
              and c.lock["state"] == "waiting", f"{raised or c.lock} after {took:.2f}s")
    finally:
        if srv is not None:
            C.stop(srv)
        first.release()

    # the command line writes its note the moment it has its port - before its
    # record is read: a long record's rebuild was a window in which a second
    # coordinator found the lock held and no note. Seen from inside the start,
    # where the record is about to be read; the start then stops for a setting
    # only the coordinator refuses (H5's register 2)
    spy = tmp / "note-spy.py"
    spy.write_bytes(b"import json, os, sys\n"
                    b"from pathlib import Path\n"
                    b"sys.path[:0] = [sys.argv[1], str(Path(sys.argv[1]).parent / "
                    b"'llm-bootstrap-contract')]\n"
                    b"import coordinator as C\n"
                    b"import machine as M\n"
                    b"lock = Path(sys.argv[sys.argv.index('--card-lock') + 1])\n"
                    b"real = C.coordinator_from\n"
                    b"def spy(*a, **kw):\n"
                    b"    seen = {'pid': os.getpid(), 'note': M.MachineLock(lock).note()}\n"
                    b"    print('NOTE AT THE RECORD: ' + json.dumps(seen), flush=True)\n"
                    b"    return real(*a, **kw)\n"
                    b"C.coordinator_from = spy\n"
                    b"sys.argv = [sys.argv[0]] + sys.argv[2:]\n"
                    b"raise SystemExit(C.main())\n")
    lock = tmp / "note-spy.lock"
    port = quiet_port()
    done = subprocess.run(of_its_own([sys.executable, str(spy), str(HERE), "--hardware", "none",
                                      "--data", str(tmp / "note-spy-data"), "--port", str(port),
                                      "--card-lock", str(lock), "--ttl", "0"]),
                          capture_output=True, cwd=HERE, encoding="utf-8", errors="replace",
                          timeout=START_S)
    out = done.stdout + done.stderr
    seen_ = json.loads(next((ln.split(": ", 1)[1] for ln in out.splitlines()
                             if ln.startswith("NOTE AT THE RECORD: ")), "{}"))
    note = seen_.get("note")
    check("the command line writes its note the moment it has its port - there when its record "
          "is about to be read: its pid, its address", isinstance(note, dict)
          and note.get("coordinator") == f"http://127.0.0.1:{port}"
          and note.get("pid") == seen_.get("pid")
          and not V.check(note, LOCK_DOC["note"], SHAPES), out[-400:])
    check("...a setting only the coordinator refuses stops the start in words - cannot start, "
          "naming it - never a traceback", done.returncode == 2
          and "cannot start: ttl_s must be a positive number of seconds" in out
          and "Traceback" not in out, f"{done.returncode}: {out[-400:]}")
    left = M.MachineLock(lock).note_path.exists()           # before a take takes it away
    check("...and the lock is let go, its note first: no note of a coordinator that never "
          "started is left behind", not left and lock_free(lock), f"a note left: {left}")


def cannot_start(tmp: Path) -> None:
    """H5's register 2: a setting only the coordinator itself refuses - a
    lifetime of 0, a port range upside down - ended the start in a traceback,
    never in "cannot start: ...", and left what it had taken to the process's
    end."""
    for flag, value, words in (("--ttl", "0", "ttl_s must be a positive number of seconds"),
                               ("--ports", "31999-31000", "a port range is 1024-65535")):
        lock = tmp / f"cannot-{flag[2:]}.lock"
        data = tmp / f"cannot-{flag[2:]}-data"
        code, out = cli_start(["--data", str(data), "--port", str(quiet_port()), "--card-lock",
                               str(lock), flag, value])
        check(f"a coordinator started with {flag} {value} - refused by the coordinator itself - "
              f"says it cannot start, in words naming the setting, never a traceback",
              code == 2 and f"cannot start: {words}" in out and "Traceback" not in out
              and "listening on" not in out, f"{code}: {out[-400:]}")
        # the note looked for BEFORE the lock is tried: a take and a let-go
        # (`lock_free`) takes the note away with it
        left = M.MachineLock(lock).note_path.exists()
        check("...it wrote no line of its record, and let the lock go with no note beside it",
              not (data / "events.jsonl").exists() and not left and lock_free(lock),
              f"a note left: {left}")


def the_record(tmp: Path) -> None:
    """H5's register 1: the record was written as text, so on Windows every
    line ended CRLF. It is written as bytes, LF alone - and a record an older
    coordinator wrote with CRLF is read as it was, and kept."""
    data = tmp / "record-bytes"
    p = Probe(Coordinator(data, clock=Clock()))
    tid, token = granted(p, W)
    p.release(token)
    raw = (data / "events.jsonl").read_bytes()
    lines_ = raw.count(b"\n")
    check("the record is written as bytes: every line ends LF, none CRLF",
          raw.endswith(b"\n") and b"\r" not in raw and lines_ >= 4, repr(raw[:160]))
    older = tmp / "record-crlf"
    older.mkdir()
    crlf = raw.replace(b"\n", b"\r\n")
    (older / "events.jsonl").write_bytes(crlf)
    again = Coordinator(older, clock=Clock())
    seqs = [e["seq"] for e in again.events]
    check("...a record an older coordinator wrote with CRLF is read whole: every line, in order "
          "(pinned: it was read before)", seqs[:lines_] == list(range(lines_))
          and again.seq > lines_ and again.closed.get(tid) == "released",
          f"{seqs}; {again.closed}")
    after = (older / "events.jsonl").read_bytes()
    check("...kept as it was, and what is added to it ends LF alone",
          after.startswith(crlf) and b"\r" not in after[len(crlf):]
          and after[len(crlf):].endswith(b"\n"), repr(after[len(crlf):][:160]))


def coverage() -> None:
    """Every answer the coordinator's routes declare - each status, each
    refusal reason - was given here at least once."""
    want = set()
    for r in OURS.values():
        for status, shape in r["responses"].items():
            reasons = V.fields_of(shape, SHAPES).get("reason", {}).get("enum", [None])
            for reason in (reasons if int(status) >= 400 else [None]):
                want.add((r["id"], int(status), reason))
    missing = sorted(want - SEEN, key=str)
    check(f"every answer the coordinator's routes declare ({len(want)}), the coordinator gave",
          {r for r, _, _ in want} == {r["id"] for r in OURS.values()} and not missing,
          "missing: " + "; ".join(f"{r} {s} {why}" for r, s, why in missing))


def section(name: str, fn, *a) -> None:
    """Run one section. One that dies is a FAIL naming where, never a suite
    that ends before its tally - a crashed suite reports no failures only
    because it never reported anything."""
    try:
        fn(*a)
    except Exception as e:                                  # noqa: BLE001
        tb = traceback.extract_tb(e.__traceback__)
        where = next((f"line {f.lineno} in {f.name}" for f in reversed(tb)
                      if f.filename == __file__), "?")
        check(f"the {name} section ran to its end", False, f"{type(e).__name__}: {e} ({where})")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    tmp = Path(tempfile.mkdtemp(prefix="coordinator-box-"))
    try:
        section("tickets", tickets, tmp)
        section("refusals", refusals, tmp)
        section("order", order, tmp)
        section("window", window, tmp)
        section("drop", drop, tmp)
        section("held waits", held, tmp)
        section("wanted", wanted, tmp)
        section("D4", d4, tmp)
        section("token", token, tmp)
        section("rebuild", rebuild, tmp)
        section("limits", limits, tmp)
        section("ports", ports, tmp)
        section("port rebuild", port_rebuild, tmp)
        section("processes", processes, tmp)
        section("ports over the wire", ports_over_the_wire, tmp)
        section("its ports", its_ports, tmp)
        section("processes named", named, tmp)
        section("reclaim", reclaiming, tmp)
        section("checked release", checked_release, tmp)
        section("bounds", bounded, tmp)
        section("up-and-version", up_and_version, tmp)
        section("this machine", this_machine, tmp)
        section("a busy machine", busy_machine, tmp)
        section("the sweep's reap", the_sweeps_reap, tmp)
        section("a late kill", a_late_kill, tmp)
        section("the holder's patience", holder_patience, tmp)
        section("memory at the grant", at_grant, tmp)
        section("scheduled work: wait or fail, and not before a time", scheduled_work, tmp)
        section("memory, amended", amends, tmp)
        section("the watch: a hang", hangs, tmp)
        section("the watch: a degraded card", degraded_card, tmp)
        section("the watch: the card full, recovered by itself", recovering, tmp)
        section("the watch: a slow answer", slow_answers, tmp)
        section("machine's lock", the_lock, tmp)
        section("a holder's work", holder_work, tmp)
        section("the lock's note", the_note, tmp)
        section("a start refused for a setting", cannot_start, tmp)
        section("the record's bytes", the_record, tmp)
        section("the dashboard", dashboard, tmp)
        section("before any route", web, tmp)
        section("before any route: the settings", web_settings, tmp)
        section("a request's time", a_request_s_time, tmp)
        section("a reset connection", a_reset_connection, tmp)
        section("its port, alone", its_port_alone, tmp)
        section("every port its own", every_port_its_own, tmp)
        section("coverage", coverage)
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
