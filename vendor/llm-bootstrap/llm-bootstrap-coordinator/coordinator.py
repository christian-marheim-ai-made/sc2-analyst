"""The coordinator: one owner of the card, applications taking tickets for it.

    python coordinator.py --data <folder> [--port 7071] [--ttl 15] [--claim 30]
                          [--drop 120] [--hold 30] [--settle 10] [--margin 256]
                          [--amend-settle 5] [--recheck 5] [--hang 300] [--watch 30]
                          [--floor 256] [--full 1024] [--recover-watch 2]
                          [--card-lock <path>]
                          [--ports 31000-31999] [--hardware this-machine|none]
                          [--allow-origin <page address> ...] [--max-body <bytes>]
                          [--read-timeout <seconds>]

It hands the card out BY TICKET, to one application at a time. An application
asks for a ticket - saying what it wants to run, and whether it is needed now
(`responsive`, the default) or can wait (`scheduled`) - and is answered with
the ticket and a secret token. It then WAITS on the ticket, never asking
again: a wait is held open, and answered the moment there is news. When the
card is free it is OFFERED to the next ticket in line - responsive first,
then scheduled, first come first served within a class - and the asker CLAIMS
it with the token, within the claim window: the claim is the RENTAL, kept by
heartbeating with the token and ended by a release. An offer nobody claims
passes to the next ticket; a waiting ticket nobody waits on is dropped, its
asker gone; nothing is ever taken from a holder.

Four rules make it survive its own crash and everyone else's:

1. THE CARD'S TRUTH IS THE CARD. This process is a record and a decider, never
   the source of truth about the hardware. On start it rebuilds from its own
   record and asks the hardware what is really there - and a rental it finds
   in its record gets a fresh lifetime, because a holder cannot be blamed for
   heartbeats that failed while the coordinator itself was down.
2. A HOLDER NEVER INFERS LOSS FROM SILENCE. It learns it lost the card from an
   answer - 409 reclaimed - never from failing to reach this process.
3. RECLAIM IS BY LIVENESS, NOT TRUST. A holder is alive because it said so
   before its rental expired, not because it once said so.
4. APPLICATIONS KNOW NOTHING OF EACH OTHER. Every answer to an application is
   about its own ticket alone: never who holds the card, what runs on it, or
   who else waits. A ticket is acted on with its TOKEN alone, which is in one
   answer - the ask's - and nowhere else: this process keeps only the token's
   sha256, in memory and in its record, so neither can give it away. The
   operator's /state, /events and the dashboard show everything - never a
   token.

A RENTAL NAMES EVERY PROCESS IT PUT ON THE CARD, on its heartbeats: each one's
pid, the proof its command line carries (the id of the load that started it),
its model, engine, port and memory. The heartbeat's list is the whole list and
replaces the one kept, so a beat lost on the way is healed by the next; the
record gets a line when the list changes, never one per beat; a restart keeps
it. At most MAX_PROCESSES, each proof a load's id.

WHAT A RENTAL LEFT ON THE CARD IS ENDED, ON PROOF, AND NEVER UNDER THE LOCK.
When a rental lapses it goes to `reclaiming`: its token is told `reclaimed`
at once, and the card is offered to nobody while every process it named is
ended - all at once, each only if it still carries its proof (`machine.py`) -
each outcome recorded, and the card's memory given its settle time to come
back. Only then does the card go to the next ticket: a holder that hangs with
its models up would otherwise have others loaded beside them. None of it
holds the lock, so everyone else is answered meanwhile. A RELEASE IS CHECKED
the same way: it is answered once every process the rental still names is
gone and, where the card can be read, its memory is back to what it read when
the rental was granted; what is still there after the settle time is ended on
proof, and the record names each. Started from its command line the
coordinator asks this machine (`--hardware this-machine`, the default); built
in code it asks nothing and ends nothing unless it is handed a machine that
does.

THE MEMORY IS VERIFIED (PLAN-4 D2) by the contract's one memory check
(`memory.py`), on the card read afresh - never an answer kept. An ask that
could never fit, more than the whole card, is refused at once, whatever its
class; the claim is the grant, and at it responsive work that does not fit
now is refused at once, in numbers, while scheduled work waits for the room -
or, asked to (`if_no_room: fail`, S18), is refused at once too - and a ticket
planned for later (`not_before`) is offered nothing before its time, and
holds back nobody meanwhile (`_due`);
and before each model a rental adds, an amend (`/card/amend`) is approved or
refused - right after one of the rental's own processes went, once its
memory has had its time to come back.

IT WATCHES EVERY RENTAL (PLAN-4 D3), and none of it holds the lock:
  * A HANG - the heartbeat goes on, and the work does not. A heartbeat says
    whether the rental is `working` - a model of it answering a request - and
    its `progress`: tokens and answers, which only real work moves. A rental
    that says it is working, whose progress has not moved for the hang limit
    - counted from when it began saying so, and never before the deadline an
    answer that comes in one piece declared (`deadline_s`) - is taken back as
    a lapsed one is: every process it names ended on its proof, outside the
    lock, and the record says why (`rental_hung`). A heartbeat that does not
    say it is working is not: a session with nothing to answer is never hung.
  * THE CARD OVER-FULL. The card is read during a rental every `watch_s`,
    outside the lock; free memory under the floor, or a memory error its
    holder reported from a model's log (a count that rose), marks the rental
    DEGRADED. The holder is told on its next heartbeat, in words and numbers;
    nothing new goes on the card (an amend is refused `degraded`); the rental
    is NOT ended for it - the operator closes something, or reboots - and it
    clears when a look at the card begun after the last mark finds room
    again. The page shows how much of the card is outside the bootstrap.
  * THE CARD FULL, RECOVERED BY ITSELF (S5b). While a rental is degraded the
    card is read every `recover_watch_s`, not every `watch_s`, and it clears
    on the first read with room for what the rental needs - its models'
    need, less what its processes hold on the card now, and the floor -
    never merely over the floor. The degraded state says the numbers a person
    acts on (`need_mib`, `outside_mib`), refreshed at each read; the record
    has a line when it is marked and one, with the numbers, when it clears.

THE MACHINE'S LOCK SAYS WHO HOLDS THE CARD AT THE TOP (D5). Started from its
command line, the coordinator takes it before anything else and holds it
while it runs, its note giving its address - written the moment its port is
had: every server finds it there, and a second coordinator does not start,
even one started in that moment (it gives the holder of a lock with no live
note a moment to write one). Started while an application runs on its own,
it waits for the lock, offering the card to nobody meanwhile. A note is
trusted only while the process that wrote it lives: a holder that died left
its note behind, and a stale note names nobody - the page says it is stale.
A start that stops - another coordinator, a port somebody holds, a setting it
refuses - says "cannot start: ..." and lets the lock go, its note first.

EVERY LIMIT IS A SETTING, with a starting default: a rental's lifetime between
heartbeats (`--ttl`, 15 s), the claim window once the card is offered
(`--claim`, 30 s), how long a waiting ticket nobody waits on is kept
(`--drop`, 120 s), the longest a wait is held open (`--hold`, 30 s), how long
the end of a rental waits for its processes to go and the card's memory to
come back (`--settle`, 10 s), the free memory kept beside a need (`--margin`,
256 MiB), how long an amend right after the rental's own unload waits for the
memory (`--amend-settle`, 5 s), how often the card is read again for
scheduled work waiting for room (`--recheck`, 5 s), how long a rental that
says it is working may go without its progress moving (`--hang`, 300 s), how
often the card is read during a rental (`--watch`, 30 s), and the free memory
under which a rental is degraded (`--floor`, 256 MiB), and the free memory
under which the card counts as full, where an answer far slower than its
model's normal marks a rental degraded too (`--full`, 1024 MiB), and how often
the card is read while a rental is degraded (`--recover-watch`, 2 s).

WHAT IT KEEPS OF THE TICKETS THAT ENDED is bounded: why each ended, and its
token's hash, for the newest KEEP_CLOSED - an older one's token is simply
unknown.

The record is `events.jsonl` in the data folder: one JSON object per line,
each ended by LF alone (written as bytes), append-only, sequence numbers
unbroken across restarts - a record an older coordinator wrote with CRLF is
read as well. The dashboard reads it
through `/events` and nothing else, so it cannot show a state the record does
not hold.

IT HANDS OUT PORTS TOO, with the same discipline: the thing that knows which
models are up also knows which ports are taken, so two applications on one
machine never pick the same one. An actor asks for a port for a purpose - its
own server, a model server - and holds it by heartbeating; one that goes quiet
loses it, and every grant, release and reclaim is a line of the same record.
A port anything else on the machine is using is never handed out: the port's
truth is the operating system's, as the card's truth is the card's.

WHAT IT ANSWERS BEFORE ANY ROUTE (the contract's `origins/coordinator.json`).
Its routes are asked by programs, never from a browser; its one page is the
dashboard, served from its own address. So, before any route, on every
request: a Host that is not one of its own names - a website whose name was
made to point at this machine - is refused; a page from another address is
refused unless it was started to allow it (`--allow-origin`, none by
default); a POST's body over the cap (`--max-body`, 1 MiB) is refused and
never read. A web page open in the same browser can never ask for the card,
give a port back or read the record. A refusal is said on the console, never
written to the record.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import io
import json
import math
import os
import re
import secrets
import socket
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

# The id grammar every part shares, and the validator that checks a request
# body against its route - both from the contract, beside this part.
CONTRACT = Path(__file__).resolve().parent.parent / "llm-bootstrap-contract"
sys.path.insert(0, str(CONTRACT))
import ids                                                      # noqa: E402
import machine                                                  # noqa: E402
import memory as MEM                                            # noqa: E402
import validate as V                                            # noqa: E402
from machine import MachineLock, NoHardware, ThisMachine        # noqa: E402

_SHAPES, _ALL = V.load(CONTRACT)
ROUTES = {r["path"]: r for key, r in _ALL.items() if key.startswith("coordinator/")}
#: A missing field a route refuses by a reason of its own, as the validator
#: words it; every other way a body is wrong is `bad_request`.
OWN_REFUSALS = {
    "coordinator.card.ask": {'$: missing required field "holder"'},
    "coordinator.port.request": {'$: missing required field "purpose"'},
    "coordinator.port.heartbeat": {'$: missing required field "port"'},
    "coordinator.port.release": {'$: missing required field "port"'},
}

#: a ticket's classes, in the order the card is offered to them
CLASSES = ("responsive", "scheduled")
#: what a scheduled ticket does when its models do not fit at its claim (S18):
#: waits in line for room - the default - or fails at once
IF_NO_ROOM = ("wait", "fail")
#: how a ticket is refused once it has ended - and why it ended
ENDED = {
    "cancelled": "{tid} was cancelled by its asker",
    "dropped": "{tid} was dropped: nobody waited on it for {drop_s:g}s",
    "unclaimed": "{tid} was offered the card and not claimed within {claim_s:g}s; "
                 "the offer passed on",
    "refused": "{tid} was refused: the card had no room for what it asked",
    "released": "{tid} was released",
    "reclaimed": "{tid} was reclaimed: its holder stopped heartbeating",
}

#: the most processes one rental may name - a 400 above it
MAX_PROCESSES = 8
#: how an ended process is said, and the record's line for each (process_<outcome>)
OUTCOMES = ("ended", "gone", "left_alone", "not_ended")
#: the tickets that ended whose reason - and token hash - are kept, the newest
KEEP_CLOSED = 1000
#: how often the end of a rental looks again at its processes and the card
SETTLE_POLL_S = 0.2
#: how far above its reading at the grant the card may read and still be
#: "back": the desktop's own use moves while a rental runs
SETTLE_SLACK_MIB = 256
#: how often a coordinator waiting for the machine's lock tries it again
LOCK_POLL_S = 0.25
#: how long a coordinator that found the machine's lock held, with no live
#: note beside it - none, or a stale one - gives its holder to write one: a
#: coordinator writes its note the moment it has the lock and its port, so
#: one started in that instant still finds the coordinator it is (H5's
#: register 3); after it, the holder is one that has not said who it is
NOTE_WAIT_S = 0.5
NOTE_STEP_S = 0.01
#: the most of a memory error's line - a model's log, as its holder said it -
#: that is kept, told and written: a log line is the model's, of any length
ERROR_CHARS = 500
#: THE CARD COUNTS AS FULL under this much free memory, in MiB (PLAN-4 D3,
#: D12; `--full`): an answer far slower than its model's normal, said by a
#: heartbeat, then marks the rental degraded - on a card with more free, a slow
#: answer is not the card's doing. A GiB: a model's buffers grow after its
#: first answer by 350-550 MiB (measured by the Dictaphone), so a card with
#: less than that free beside it is one where the next answer may not fit,
#: and the driver spills to the machine's memory - several times slower
FULL_MIB = 1024
#: WHILE A RENTAL IS DEGRADED the card is read this often, in seconds, instead
#: of every `watch_s` (S5b; `--recover-watch`): the person closes a program,
#: and the held work runs again within about this long. A read of the card is
#: a query of nvidia-smi - cheap, and only while the card is full
RECOVER_WATCH_S = 2.0
#: why a rental that stopped moving was taken back - the hang limit in it
HUNG_WORDS ="it said it was working, and its progress had not moved for {hang_s:g}s"


class AnotherCoordinator(Exception):
    """The machine's lock is another coordinator's: a machine never runs two."""


class CannotListen(OSError):
    """The coordinator's port could not be had: its address, and the system's
    words. Every other error at the start says what it was, never this."""

#: where the ports the coordinator hands out come from, by default: clear of
#: the ports tools usually take (8080, 8888, 8901, the 7070s), and BELOW every
#: common system's range for passing connections - Windows 49152-65535, Linux
#: 32768-60999 - where any outgoing connection could be given a granted port
#: before its holder has bound it (found by building: a range inside it was
#: handed the coordinator's own port by the operating system)
PORT_RANGE = (31000, 31999)


def bindable(port: int) -> bool:
    """Is nothing on this machine listening on *port*? Asked of the operating
    system itself, by binding it for a moment."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", int(port)))
            return True
        except OSError:
            return False


def hardware_for(name: str):
    """What `--hardware` names: this machine's card and processes, or nothing."""
    if name == "this-machine":
        return ThisMachine()
    if name == "none":
        return NoHardware()
    raise ValueError(f"--hardware is this-machine or none, not {name!r}")


def card_words(card: dict) -> str:
    """The card as the machine sees it, in the record's words."""
    if not card.get("known"):
        return "the card was not asked"
    return f"the card has {card['used_mib']} of {card['total_mib']} MiB in use"


def token_hash(token) -> str:
    """The only form a token is ever kept in, here or in the record."""
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def processes_word(n: int) -> str:
    """How many processes, in the record's words: no process, 1 process, 3 processes."""
    return "no process" if n == 0 else "1 process" if n == 1 else f"{n} processes"


def named_words(p: dict) -> str:
    """One named process, as the record says it."""
    return f"pid {p['pid']} ({p['model']}, {p['proof']})"


class Coordinator:
    """One owner of the card.

    A HOLDER IS ITS ACTOR AND ITS TOKEN, NEVER ITS NAME. Two applications that
    call themselves the same are two askers with two tickets, and only a
    ticket's token acts on it. A name was once the identity here, and then two
    copies of one application would both have believed they held the card.

    `actor` is the coordinator's own id (a crd- actor id), minted when it
    starts."""

    def __init__(self, data, ttl_s: float = 15.0, hardware=None, clock=time.time,
                 actor=None, ports=PORT_RANGE, port_free=None, claim_s: float = 30.0,
                 drop_s: float = 120.0, hold_s: float = 30.0, settle_s: float = 10.0,
                 keep_closed: int = KEEP_CLOSED, margin_mib: int = MEM.MARGIN_MIB,
                 amend_settle_s: float = 5.0, recheck_s: float = 5.0, hang_s: float = 300.0,
                 watch_s: float = 30.0, floor_mib: int = MEM.MARGIN_MIB,
                 full_mib: int = FULL_MIB, recover_watch_s: float = RECOVER_WATCH_S):
        for name, value in (("ttl_s", ttl_s), ("claim_s", claim_s), ("drop_s", drop_s),
                            ("hold_s", hold_s), ("settle_s", settle_s),
                            ("amend_settle_s", amend_settle_s), ("recheck_s", recheck_s),
                            ("hang_s", hang_s), ("watch_s", watch_s),
                            ("recover_watch_s", recover_watch_s)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be a positive number of seconds, not {value!r}")
        if amend_settle_s > MEM.AMEND_SETTLE_MAX_S:
            raise ValueError(f"amend_settle_s is at most {settle_bound()}, not {amend_settle_s!r}")
        if isinstance(margin_mib, bool) or not isinstance(margin_mib, int) or margin_mib < 0:
            raise ValueError(f"margin_mib is the free memory kept beside a need, a whole number "
                             f"of MiB, 0 or more, not {margin_mib!r}")
        if isinstance(floor_mib, bool) or not isinstance(floor_mib, int) or floor_mib < 0:
            raise ValueError(f"floor_mib is the free memory under which a rental is degraded, a "
                             f"whole number of MiB, 0 or more, not {floor_mib!r}")
        if isinstance(full_mib, bool) or not isinstance(full_mib, int) or full_mib < 0:
            raise ValueError(f"full_mib is the free memory under which the card counts as full "
                             f"for a slow answer, a whole number of MiB, 0 or more, not "
                             f"{full_mib!r}")
        if isinstance(keep_closed, bool) or not isinstance(keep_closed, int) or keep_closed < 1:
            raise ValueError(f"keep_closed is how many ended tickets - and closed ports - are "
                             f"kept, 1 or more, not {keep_closed!r}")
        if actor is not None and not (ids.matches("actor", actor) and actor.startswith("crd-")):
            raise ValueError(f"a coordinator's actor is a crd- actor id, not {actor!r}")
        lo, hi = (int(p) for p in ports)
        if not 1024 <= lo <= hi <= 65535:
            raise ValueError(f"a port range is 1024-65535 and low to high, not {lo}-{hi}")
        self.port_from, self.port_to = lo, hi
        #: is a port free of anything else on this machine? The operating
        #: system is asked; a suite can hand in its own
        self._port_free = port_free or bindable
        self.ports: dict = {}           # port -> lease
        self._port_ttl: dict = {}       # port -> the lifetime its holder asked for
        #: (port, actor) -> "released" | "reclaimed": the newest `keep_closed`
        #: ports closed, oldest first (`_port_closed`)
        self.port_closed: dict = {}
        self.actor = actor or ids.mint_actor("crd")
        self._lock = threading.RLock()
        #: what a held wait sleeps on: told whenever a ticket moves
        self._cond = threading.Condition(self._lock)
        #: set once the coordinator stops: no wait is held open any more
        self._closing = threading.Event()
        self.clock = clock
        self.ttl_s, self.claim_s = float(ttl_s), float(claim_s)
        self.drop_s, self.hold_s = float(drop_s), float(hold_s)
        self.settle_s, self.keep_closed = float(settle_s), keep_closed
        #: THE MEMORY (D2): the free memory kept beside what a ticket or an
        #: amend needs; how long an amend right after one of the rental's own
        #: processes went waits for its memory to come back; how often the
        #: card is read again for scheduled work waiting for room
        self.margin_mib, self.amend_settle_s = int(margin_mib), float(amend_settle_s)
        self.recheck_s = float(recheck_s)
        #: THE WATCH (D3): how long a rental that says it is working may go
        #: without its progress moving; how often the card is read during a
        #: rental, on the coordinator's clock; the free memory under which a
        #: rental is degraded
        self.hang_s, self.watch_s, self.floor_mib = float(hang_s), float(watch_s), int(floor_mib)
        #: the free memory under which the card counts as full: an answer far
        #: slower than its model's normal, said by a heartbeat, then marks the
        #: rental degraded too (D3, D12)
        self.full_mib = int(full_mib)
        #: how often the card is read while a rental is DEGRADED, instead of
        #: `watch_s` - so a card with room again is noticed within seconds of
        #: the person closing something (S5b)
        self.recover_watch_s = float(recover_watch_s)
        #: when the card was last read for the tickets passed over for memory
        #: (monotonic): the claim that passed one over read it
        self._rechecked = 0.0
        #: the machine's card lock (D5): `none` - built in code, holding no
        #: lock, offering as it always did; `waiting` - an application run on
        #: its own has the card, and nothing is offered; `held`. Set by `serve`
        self.lock = {"state": "none", "path": None,
                     "detail": "built in code: this coordinator holds no machine lock"}
        self.hardware = hardware or NoHardware()
        #: the version of the contract this coordinator runs, as /state says it
        self.contract_version = V.contract_version(CONTRACT)
        self.path = Path(data) / "events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        #: the card's one rental - the ticket that claimed it - or None. Held;
        #: or releasing or reclaiming, while its end is checked - the card goes
        #: to nobody else meanwhile
        self.rental: dict | None = None
        #: the tickets in line - waiting, or offered the card - by id
        self.line: dict = {}
        #: a ticket that ended -> why: cancelled, dropped, unclaimed, released,
        #: reclaimed. The newest `keep_closed` of them, no more
        self.closed: dict = {}
        #: the tickets that ended, oldest first, with their token's hash
        self._closed_order: collections.deque = collections.deque()
        #: a ticket that ended -> the words its holder is told, where they are
        #: its own: a rental taken back for a hang says so, not that it went
        #: quiet. Kept, and forgotten, with `closed`
        self._words: dict = {}
        #: a token's sha256 -> its ticket's id. The ONLY form a token is kept in
        self._by_hash: dict = {}
        self.events: list = []
        self.seq = 0
        self._ids = 0
        self.started_at = clock()
        self._rebuild(self._read_record())

    # ------------------------------------------------------------ the record

    def _read_record(self) -> list:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    def _log(self, kind: str, **fields) -> dict:
        ev = {"seq": self.seq, "at": self.clock(), "kind": kind,
              **{k: v for k, v in fields.items() if v is not None}}
        # WRITTEN AS BYTES: a file opened as text on Windows ends every line
        # CRLF (H5's register 1). A record an older coordinator wrote that way
        # is still read - its lines are split as lines, whatever ends them
        with self.path.open("ab") as f:
            f.write((json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8"))
        self.events.append(ev)
        self.seq += 1
        return ev

    @staticmethod
    def _about(t: dict) -> dict:
        """The record's fields for a ticket: who asked, and what for."""
        return {"ticket_id": t["id"], "holder": t["holder"], "actor": t["actor"],
                "class": t["class"], "models": [dict(m) for m in t["models"]],
                "label": t["label"], "for": t["for"]}

    def _rebuild(self, past: list) -> None:
        """Replay the previous run's record, then say what was found."""
        ports_held: dict = {}
        line: dict = {}
        rental = None
        before_tickets = None           # a turn held under the protocol tickets replaced
        shas: dict = {}                 # a ticket's id -> its token's sha256
        hung: dict = {}                 # a rental taken back for a hang -> what it is told
        for ev in past:
            p = ev.get("port")
            if isinstance(p, int) and not isinstance(p, bool):
                if ev["kind"] in ("port_granted", "state_rebuilt"):
                    # a restart's line for a port keeps its first grant's time:
                    # the record is append-only, so that grant came before it
                    was = ports_held.get(p) if ev["kind"] == "state_rebuilt" else None
                    ports_held[p] = {"port": p, "actor": ev.get("actor"),
                                     "purpose": ev.get("purpose"),
                                     "granted_at": was["granted_at"] if was else ev["at"]}
                elif ev["kind"] in ("port_released", "port_reclaimed"):
                    gone = ports_held.pop(p, None)
                    if gone:
                        self._port_closed(p, gone["actor"], ev["kind"][len("port_"):])
                continue
            tid = ev.get("ticket_id") or ev.get("turn_id")
            seq = ids.ticket_seq(tid)
            if seq is None and isinstance(tid, str) and re.fullmatch(r"t-[0-9]+", tid):
                seq = int(tid[2:])              # a record written before ids had tails
            if seq is not None:
                self._ids = max(self._ids, seq)
            kind = ev.get("kind")
            if kind == "ticket_asked" and tid:
                line[tid] = {"id": tid, "holder": ev.get("holder"), "actor": ev.get("actor"),
                             "class": ev.get("class") or "responsive",
                             "models": [dict(m) for m in ev.get("models") or []],
                             "label": ev.get("label"), "for": ev.get("for"),
                             "state": "waiting", "since": ev["at"], "offered_until": None,
                             "sha": ev.get("token_sha256"), "heard_at": ev["at"], "waits": 0,
                             # S18: its time and whether it waits for room, as
                             # asked - a line from before either says neither
                             "not_before": ev.get("not_before"),
                             "if_no_room": "fail" if (ev.get("class") or "responsive")
                             == "responsive" else ev.get("if_no_room") or "wait",
                             "room": None}
                if ev.get("token_sha256"):
                    self._by_hash[ev["token_sha256"]] = tid
                    shas[tid] = ev["token_sha256"]
            elif kind == "ticket_offered" and tid in line:
                line[tid]["state"] = "offered"
            elif kind == "rental_granted" and tid in line:
                rental = line.pop(tid)
                rental.update(state="held", granted_at=ev["at"], offered_until=None,
                              processes=[], card_at_grant=None, degraded=None,
                              memory_errors=0)
            elif kind == "rental_hung" and tid:
                # taken back for a hang: what its holder is told says so
                hung[tid] = f"{tid} was reclaimed: {ev.get('detail') or 'it hung'}"
            elif kind == "rental_degraded" and rental is not None and rental["id"] == tid \
                    and isinstance(ev.get("degraded"), dict):
                rental["degraded"] = dict(ev["degraded"])
                if ev["degraded"].get("reason") == "memory_error":
                    # how many memory errors its heartbeats had said since the
                    # grant (H8, S5b R3) - unknown on an older coordinator's line
                    n = ev.get("memory_errors")
                    rental["memory_errors"] = n if isinstance(n, int) \
                        and not isinstance(n, bool) and n >= 0 else None
            elif kind == "rental_restored" and rental is not None and rental["id"] == tid:
                rental["degraded"] = None
            elif kind in ("rental_processes", "rental_reclaiming", "rental_releasing") \
                    and rental is not None and rental["id"] == tid:
                rental["processes"] = [dict(x) for x in ev.get("processes") or []]
                if kind == "rental_reclaiming":
                    # taken back, and its processes were being ended when this
                    # coordinator stopped: they are ended again, on proof
                    rental["state"] = "reclaiming"
                    self._close({"id": tid, "sha": shas.get(tid), "over_words": hung.get(tid)},
                                "reclaimed")
                elif kind == "rental_releasing":
                    # given back, and its end was being checked when this
                    # coordinator stopped: it is checked again, and finished
                    rental["state"] = "releasing"
                    self._close({"id": tid, "sha": shas.get(tid)}, "released")
            elif kind == "rental_amended" and rental is not None and rental["id"] == tid:
                # an approved amend: the rental's models followed the card
                rental["models"] = [dict(m) for m in ev.get("models") or []]
            elif kind == "rental_process" and rental is not None and rental["id"] == tid:
                # a coordinator before the list named one process, and said
                # only its pid and proof: carried into the list, its model and
                # engine the rental's first, its port and memory unknown
                old, first = ev["process"], rental["models"][0]
                rental["processes"] = [{"pid": old["pid"], "proof": old["proof"],
                                        "model": first["key"], "engine": first["engine"],
                                        "port": None, "mib": None, "since": ev["at"]}]
            elif kind in ("ticket_unclaimed", "ticket_dropped", "ticket_cancelled",
                          "ticket_refused") and tid:
                line.pop(tid, None)
                self._close({"id": tid, "sha": shas.get(tid)}, kind[len("ticket_"):])
            elif kind in ("rental_released", "rental_reclaimed") and tid:
                self._close({"id": tid, "sha": shas.get(tid), "over_words": hung.get(tid)},
                            kind[len("rental_"):])
                if rental is not None and rental["id"] == tid:
                    rental = None
            elif kind == "turn_granted" and tid:
                before_tickets = {"id": tid, "holder": ev.get("holder"),
                                  "actor": ev.get("actor")}
            elif kind in ("turn_released", "turn_reclaimed") and before_tickets \
                    and before_tickets["id"] == tid:
                before_tickets = None
        # /events serves the record's lines in the event shape, and no other:
        # a line an older coordinator wrote in a shape since replaced - a turn,
        # a rental's one process - stays in the file, and is said once here
        self.events, older = [], {}
        for ev in past:
            if V.check(ev, {"ref": "event"}, _SHAPES):
                older[str(ev.get("kind"))] = older.get(str(ev.get("kind")), 0) + 1
            else:
                self.events.append(ev)
        self.seq = past[-1]["seq"] + 1 if past else 0
        now = self.clock()
        card = self.hardware.probe()
        n = sum(older.values())
        kept = "" if not n else (
            f"; {n} line{'s' if n != 1 else ''} of the record {'are' if n != 1 else 'is'} in a "
            f"shape since replaced ({', '.join(sorted(older))}), kept in the file and left out "
            f"of /events")
        self._log("coordinator_started", detail=card_words(card) + kept)
        # A port its holder held when the coordinator went down gets a fresh
        # lifetime, as a rental does: its holder cannot be blamed for
        # heartbeats that failed while this process was not there to hear them.
        for p, lease in sorted(ports_held.items()):
            lease["expires_at"] = now + self.ttl_s
            self.ports[p], self._port_ttl[p] = lease, self.ttl_s
            self._log("state_rebuilt", port=p, actor=lease["actor"], purpose=lease["purpose"],
                      detail=f"the record says port {p} is held by {lease['actor']} for "
                             f"{lease['purpose']}; its lifetime restarts now")
        if before_tickets is not None:
            # A turn granted before tickets has no token: nothing can prove who
            # holds it, and a holder nobody can prove is not carried over - it
            # asks again, as it would after any reclaim.
            old = before_tickets
            self._log("state_rebuilt", **{
                "ticket_id": old["id"] if ids.matches("ticket", old["id"]) else None,
                "holder": str(old["holder"]) if old.get("holder") else None,
                "actor": old["actor"] if ids.matches("actor", old.get("actor")) else None,
                "detail": f"the record says {old['id']} holds the card under a turn granted "
                          f"before tickets; no token can prove its holder, so it is not "
                          f"carried over"})
        resume = finish = None
        if rental is not None:
            heard = rental.get("memory_errors")
            rental.update(expires_at=now + self.ttl_s, life=self.ttl_s, **self._watching(now))
            # a count of memory errors it had said before this restart is not
            # news - and one it had not is (H8, S5b R3): the count the record
            # kept, on its last memory-error line (0 with none since the
            # grant), is what it has said; a record with no count leaves it
            # unknown, and its first heartbeat's count is taken as known
            # (`_heard`) - a degraded rental stays degraded, from the record
            rental["memory_errors"] = heard
            self.rental = rental
            named = rental["processes"]
            on_it = ("it names " + ", ".join(named_words(x) for x in named)) if named \
                else "it names no process on the card"
            if rental["state"] == "reclaiming":
                resume = [dict(x) for x in named]
                self._log("state_rebuilt", **{
                    **self._about(rental), "processes": [dict(x) for x in named],
                    "detail": f"the record says {rental['id']} was being taken back; "
                              f"{on_it}; they are ended again, on proof"})
            elif rental["state"] == "releasing":
                finish = [dict(x) for x in named]
                self._log("state_rebuilt", **{
                    **self._about(rental), "processes": [dict(x) for x in named],
                    "detail": f"the record says {rental['id']} was being given back; "
                              f"{on_it}; its end is checked again, and the card goes to "
                              f"nobody until it is"})
            else:
                self._log("state_rebuilt", **{
                    **self._about(rental), "processes": [dict(x) for x in named],
                    "detail": f"the record says {rental['id']} holds the card; {on_it}; "
                              f"{card_words(card)}; its lifetime restarts now"})
        self.line = line
        for t in self._in_line():
            t["heard_at"] = now
            if t["state"] == "offered":
                t["offered_until"] = now + self.claim_s
                why = "was offered the card; its claim window restarts now"
            else:
                why = (f"waits ({t['class']}), asked at {t['since']:.0f}; its asker is waited "
                       f"for afresh")
            self._log("state_rebuilt", ticket_id=t["id"], holder=t["holder"], actor=t["actor"],
                      detail=f"the record says {t['id']} {why}")
        if resume is not None:
            self._reclaiming(self.rental, "taken back before the coordinator restarted", resume)
        self._offer(now)
        if finish is not None:
            # last, once the rebuild has written all it writes: the check
            # waits outside the lock, at most the settle time - never in the
            # constructor - and writes the release's end under the lock
            threading.Thread(target=self._released, args=(self.rental, finish), daemon=True,
                             name=f"release-{self.rental['id']}").start()

    # ------------------------------------------------------------ the rules

    @staticmethod
    def _rank(t: dict) -> tuple:
        """Where a ticket stands in line: its class, then when it asked - its
        number is minted in the order tickets are asked for."""
        return CLASSES.index(t["class"]), ids.ticket_seq(t["id"]) or 0

    def _in_line(self) -> list:
        """The tickets in the order the card goes to them: an offer first."""
        return sorted(self.line.values(), key=lambda t: (t["state"] != "offered", self._rank(t)))

    def _changed(self) -> None:
        """A ticket moved: every wait held open looks again."""
        self._cond.notify_all()

    @staticmethod
    def _due(t: dict, now: float) -> bool:
        """Has ticket *t*'s time come (S18)? A ticket that names none, or a
        time already past - or this very moment - is due; one planned for a
        time still to come is in line and holds back nobody until then. The
        time is the coordinator's wall clock, looked at afresh each time: a
        machine that slept through it finds it past when it wakes."""
        return t.get("not_before") is None or t["not_before"] <= now

    def _offer(self, now: float) -> bool:
        """Offer the free card to the next ticket in line - one offer at a time.
        Never while the machine's lock is somebody else's (an application run
        on its own has the card), never to a ticket passed over for memory
        until the card is read again and has its room (`_recheck`), and never
        to one whose time has not come (S18) - asked on every beat (`_reap`),
        so it is offered the first beat after its time."""
        if self.rental is not None or any(t["state"] == "offered" for t in self.line.values()):
            return False
        if self.lock["state"] == "waiting":
            return False
        waiting = [t for t in self.line.values() if t["state"] == "waiting" and not t.get("short")
                   and self._due(t, now)]
        if not waiting:
            return False
        t = min(waiting, key=self._rank)
        t["state"], t["offered_until"] = "offered", now + self.claim_s
        self._log("ticket_offered", ticket_id=t["id"], holder=t["holder"], actor=t["actor"],
                  detail=f"the card is offered until it is claimed, for {self.claim_s:g}s")
        self._changed()
        return True

    # ------------------------------------------------------------ the memory

    @staticmethod
    def _need(models) -> int:
        """What a ticket's models need together, in MiB: a need nobody
        declared adds nothing."""
        return sum(int(m["need_mib"] or 0) for m in models)

    def _fit(self, need_mib, card: dict) -> dict:
        """The ONE memory check (the contract's `memory.fit`) against the card
        as the machine read it, keeping this coordinator's margin free."""
        known = card.get("known") and card.get("used_mib") is not None \
            and card.get("total_mib") is not None
        free = max(0, card["total_mib"] - card["used_mib"]) if known else None
        return MEM.fit(need_mib, free, card.get("total_mib") if known else None,
                       self.margin_mib)

    def _card_freed(self) -> None:
        """A rental ended: what the card holds changed, so every ticket passed
        over for memory is offered the card again in its turn, first among
        them the first in line - its claim reads the card afresh."""
        for t in self.line.values():
            t["short"] = False

    def _recheck_due(self) -> bool:
        """Is the card to be read again for the tickets passed over for memory?
        Only while it is free, nothing is offered, one of them waits, and
        `recheck_s` has passed since it was last read for them."""
        if self.rental is not None or self.lock["state"] == "waiting" \
                or any(t["state"] == "offered" for t in self.line.values()) \
                or not any(t.get("short") for t in self.line.values()):
            return False
        now = time.monotonic()
        if now - self._rechecked < self.recheck_s:
            return False
        self._rechecked = now
        return True

    def _recheck(self, card: dict) -> None:
        """The card read afresh for the tickets passed over for memory: each
        one whose needs fit now is offered the card again, in its turn - the
        first in line first, and its claim decides - and each that still does
        not fit has the room it waits on said again, in this read's numbers
        (S18)."""
        now = self.clock()
        for t in self.line.values():
            if not t.get("short"):
                continue
            verdict = self._fit(self._need(t["models"]), card)
            if verdict["verdict"] in ("fits", "unknown"):
                t["short"] = False
            elif verdict["verdict"] == "no_room":
                t["room"] = self._room_waited(verdict, now)
        self._offer(now)

    def _room_waited(self, verdict: dict, now: float) -> dict:
        """What a scheduled ticket passed over for memory waits on (S18): the
        numbers, what programs outside the bootstrap hold - the card's use,
        since no rental holds the card while a ticket is passed over - the
        words a person reads while it waits, and when the card was read."""
        free, total = verdict["free_mib"], verdict["total_mib"]
        outside = None if free is None or total is None else max(0, total - free)
        return {"memory": MEM.numbers(verdict), "outside_mib": outside,
                "words": MEM.room_words(verdict, outside), "at": now}

    def _close(self, t: dict, why: str) -> None:
        """A ticket ended, and why - kept for the newest `keep_closed` tickets,
        with their token's hash, and the words its holder is told when they are
        its own (`over_words`: a hang's); an older one is forgotten whole, its
        token then unknown. The only way anything is put in `closed`."""
        if t["id"] not in self.closed:
            self._closed_order.append((t["id"], t.get("sha")))
        self.closed[t["id"]] = why
        if t.get("over_words"):
            self._words[t["id"]] = t["over_words"]
        while len(self._closed_order) > self.keep_closed:
            tid, sha = self._closed_order.popleft()
            self.closed.pop(tid, None)
            self._words.pop(tid, None)
            if sha is not None and self._by_hash.get(sha) == tid:
                del self._by_hash[sha]

    def _end(self, t: dict, why: str, kind: str, **fields) -> None:
        self.line.pop(t["id"], None)
        self._close(t, why)
        self._log(kind, ticket_id=t["id"], holder=t["holder"], actor=t["actor"], **fields)
        self._changed()

    def _take_back(self, r: dict, why: str) -> None:
        """A rental that lapsed is taken back. What it named on the card is
        ended - never under this lock: it goes to `reclaiming`, its token is
        told `reclaimed` from now on, the card goes to nobody meanwhile, and
        the machine is asked on a thread of its own. A rental that named
        nothing, or a machine that ends nothing, frees the card at once."""
        named = [dict(x) for x in r.get("processes") or []]
        if named and getattr(self.hardware, "end", None) is not None:
            r["state"] = "reclaiming"
            self._close(r, "reclaimed")
            self._log("rental_reclaiming", ticket_id=r["id"], holder=r["holder"],
                      actor=r["actor"], processes=[dict(x) for x in named],
                      detail=f"{why}; ending its {processes_word(len(named))} on proof")
            self._changed()
        self._reclaiming(r, why, named)

    def _reclaiming(self, r: dict, why: str, named: list) -> None:
        """End what *r* named, then free the card: on a thread of its own when
        the machine ends anything; at once when it does not, or when *r* named
        nothing."""
        if named and getattr(self.hardware, "end", None) is not None:
            threading.Thread(target=self._reclaim, args=(r, why, named), daemon=True,
                             name=f"reclaim-{r['id']}").start()
            return
        if not named:
            done = "it named no process on the card"
        else:
            done = (f"no machine configured: its {processes_word(len(named))} "
                    f"{'was' if len(named) == 1 else 'were'} not ended, and the card is not "
                    f"touched")
        with self._lock:
            if self.rental is r:
                self.rental = None
                self._card_freed()
            self._end(r, "reclaimed", "rental_reclaimed", models=[dict(m) for m in r["models"]],
                      detail=f"{why}; {done}")

    def _reclaim(self, r: dict, why: str, named: list) -> None:
        """The machine's part of a reclaim, OUTSIDE the lock: every process
        ended at once, each on its own proof; the card's memory given its
        settle time to come back. Then, under the lock, each outcome on the
        record, and the card to the next ticket."""
        outcomes = self._end_on_proof(named)
        _, memory = self._settle(r, [])
        with self._lock:
            for x, o in zip(named, outcomes):
                self._log(f"process_{o['outcome']}", ticket_id=r["id"], actor=r["actor"],
                          process=dict(x), detail=o["detail"])
            if self.rental is r:
                self.rental = None
                self._card_freed()
            self._end(r, "reclaimed", "rental_reclaimed", models=[dict(m) for m in r["models"]],
                      detail="; ".join([why] + [o["detail"] for o in outcomes] + [memory]))
            self._offer(self.clock())

    def _end_on_proof(self, named: list) -> list:
        """The machine asked to end *named*, each on its own proof: -> one
        outcome each. A machine that fails ends nothing, and says why - it
        never keeps the card from the next ticket."""
        try:
            got = list(self.hardware.end([dict(x) for x in named]))
            if len(got) != len(named):
                raise ValueError(f"it answered {len(got)} outcomes for {len(named)} processes")
            return [{"outcome": o["outcome"] if o.get("outcome") in OUTCOMES else "not_ended",
                     "detail": str(o.get("detail") or "")} for o in got]
        except Exception as exc:                            # noqa: BLE001 - said, on the record
            return [{"outcome": "not_ended",
                     "detail": f"its process {x['pid']} ({x['proof']}) was not ended: the "
                               f"machine failed ({exc})"} for x in named]

    def _settle(self, r: dict, named: list):
        """Wait - OUTSIDE the lock, at most the settle time - until every one of
        *named* is gone and, where the card can be read, its memory is back to
        what it read when *r* was granted, give or take SETTLE_SLACK_MIB. A
        machine that cannot say what is alive is not asked. -> (the processes
        still there, the card in the record's words)."""
        alive = getattr(self.hardware, "alive", None)
        left = list(named) if alive is not None else []
        baseline = r.get("card_at_grant")
        deadline = time.monotonic() + self.settle_s
        while True:
            if left:
                try:
                    up = list(alive([dict(x) for x in left]))
                except Exception:                           # noqa: BLE001 - cannot tell: still there
                    up = [True] * len(left)
                left = [x for x, is_up in zip(left, up) if is_up]
            card = self.hardware.probe(fresh=True)
            back = None if not card.get("known") or baseline is None \
                else card["used_mib"] <= baseline + SETTLE_SLACK_MIB
            done = not left and back is not False
            if done or time.monotonic() >= deadline:
                if back is None:
                    return left, card_words(card)
                if back:
                    return left, (f"the card is back to {card['used_mib']} MiB in use "
                                  f"({baseline} when the rental was granted)")
                return left, (f"the card has {card['used_mib']} MiB in use {self.settle_s:g}s "
                              f"on, {card['used_mib'] - baseline} more than when the rental was "
                              f"granted")
            time.sleep(SETTLE_POLL_S)

    def _reap(self, now: float) -> None:
        """Take the card back from a holder whose rental expired; pass on an
        offer nobody claimed; drop a waiting ticket nobody waits on; take back
        quiet ports - then offer the card, if it is free."""
        r = self.rental
        if r is not None and r["state"] == "held" and now > r["expires_at"]:
            silent = now - (r["expires_at"] - r["life"])
            self._take_back(r, f"no heartbeat for {silent:.0f}s")
        elif r is not None and r["state"] == "held" and self._hung(r, now):
            self._hang(r)
        for t in list(self.line.values()):
            if t["state"] == "offered" and now > t["offered_until"]:
                self._end(t, "unclaimed", "ticket_unclaimed",
                          detail=f"offered the card and not claimed within {self.claim_s:g}s; "
                                 f"the offer passes on")
            elif t["state"] == "waiting" and not t["waits"] \
                    and now - t["heard_at"] > self.drop_s:
                self._end(t, "dropped", "ticket_dropped",
                          detail=f"nobody waited on it for {now - t['heard_at']:.0f}s: its "
                                 f"asker has gone")
        for p, lease in sorted(self.ports.items()):
            if now > lease["expires_at"]:
                silent = now - (lease["expires_at"] - self._port_ttl.get(p, self.ttl_s))
                del self.ports[p]
                self._port_ttl.pop(p, None)
                self._port_closed(p, lease["actor"], "reclaimed")
                self._log("port_reclaimed", port=p, actor=lease["actor"],
                          purpose=lease["purpose"], detail=f"no heartbeat for {silent:.0f}s")
        self._offer(now)

    def reap(self) -> None:
        """`_reap`, on the reaper's beat - and, while scheduled work waits for
        room on a free card, the card read afresh for it every `recheck_s`;
        while a rental is held, the card read for it every `watch_s` (D3) -
        each OUTSIDE the lock: nvidia-smi is not asked while everyone waits."""
        with self._lock:
            self._reap(self.clock())
            due = self._recheck_due()
            watch = self._watch_due()
        if due:
            card = self.hardware.probe(fresh=True)
            with self._lock:
                self._recheck(card)
        if watch is not None:
            card = self.hardware.probe(fresh=True)
            with self._lock:
                self._watched(*watch, card)

    # ------------------------------------------------------------ the watch

    def _watching(self, now: float) -> dict:
        """A rental's watch as it starts - at its grant, or found in the record
        by a restart: not working, its progress not yet said, its clock at
        *now*, the card next read `watch_s` from now."""
        return {"working": False, "progress": None, "moved_at": now, "due_at": None,
                "memory_errors": 0, "slow_answers": 0,
                "read": {"free_mib": None, "total_mib": None},
                "read_at": None, "watch_at": now + self.watch_s, "marks": 0}

    def _heard(self, t: dict, working, progress, deadline_s, errors, error, now: float,
               slow=None, slow_words=None) -> None:
        """What a heartbeat said of rental *t*'s work (D3). `working`: a model
        of it answers a request - a heartbeat that does not say so says it
        does not. `progress`: tokens and answers, which only real work moves;
        the hang's clock starts at its last move, or when the rental began
        saying it is working. `deadline_s`: an answer under way comes in one
        piece, due that long from now - no hang is seen before it. A count of
        memory errors its models' logs said that rose is news: the rental is
        degraded, with the newest line. A count this coordinator never saw -
        its first beat after a restart - is taken as known. A count of answers
        far slower than their model's normal that rose (`slow`, the newest in
        `slow_words`) is news only while the card, as last read, has less free
        than it counts as full (`full_mib`): the rental is degraded, with the
        words. On a card with room, or one not read since the grant or a
        restart, a slow answer is not the card's doing: it is only counted."""
        if slow is not None:
            free = t["read"]["free_mib"]
            if slow > t["slow_answers"] and free is not None and free < self.full_mib:
                said = str(slow_words or "an answer was far slower than its model's normal")
                self._degrade(t, "slow_answer", now, error=said[:ERROR_CHARS])
            t["slow_answers"] = int(slow)
        was = t["working"]
        t["working"] = bool(working)
        if progress is not None and progress != t["progress"]:
            t["progress"], t["moved_at"] = int(progress), now
        elif t["working"] and not was:
            t["moved_at"] = now
        t["due_at"] = now + float(deadline_s) if t["working"] and deadline_s is not None \
            else None
        if errors is None:
            return
        if t["memory_errors"] is not None and errors > t["memory_errors"]:
            line = str(error or "a model's log said the card's memory ran out")[:ERROR_CHARS]
            self._degrade(t, "memory_error", now, error=line, errors=int(errors))
        t["memory_errors"] = int(errors)

    def _hung(self, r: dict, now: float) -> bool:
        """Does held rental *r* say it is working, with its progress still for
        the hang limit, and past any deadline a one-piece answer declared?"""
        return bool(r["working"]) and now - r["moved_at"] >= self.hang_s \
            and (r["due_at"] is None or now >= r["due_at"])

    def _hang(self, r: dict) -> None:
        """A hang (D3): the heartbeat went on, and the work did not. Taken back
        as a lapsed rental is - every process it names ended on proof, never
        under this lock - with a line of the record saying why, and words of
        its own for its holder."""
        said = HUNG_WORDS.format(hang_s=self.hang_s)
        r["over_words"] = f"{r['id']} was reclaimed: {said}"
        self._log("rental_hung", ticket_id=r["id"], holder=r["holder"], actor=r["actor"],
                  detail=said)
        self._take_back(r, f"hung: {said}")

    def _degrade(self, t: dict, reason: str, now: float, error=None, errors=None) -> None:
        """Rental *t* is DEGRADED (D3) - and never ended for it: free memory
        under the floor, as the card last read (`under_floor`), or a memory
        error a model's log said (`memory_error`, the line; `errors`, how
        many its heartbeats have said since the grant, kept on the record's
        line for a restart). Said on the holder's next heartbeat in words and
        numbers - the card's last read - and on the record. Each mark is
        counted: a look at the card begun before it cannot clear it."""
        free, total = t["read"]["free_mib"], t["read"]["total_mib"]
        if reason == "under_floor":
            whole = f" of {total}" if total is not None else ""
            why = (f"the card has {free} MiB free{whole}, under the floor of {self.floor_mib} "
                   f"MiB a rental keeps free: nothing new goes on it until it has room again")
        elif reason == "slow_answer":
            why = (f"{error}, while the card had {free} MiB free, under the {self.full_mib} MiB "
                   f"it counts as full: nothing new goes on it until it has room again")
        else:
            why = (f"a model's log said the card's memory ran out ({error}): nothing new goes on "
                   f"it until it has room again")
        need, outside = self._room(t, free, total)
        t["degraded"] = {"reason": reason, "why": why, "since": now, "free_mib": free,
                         "floor_mib": self.floor_mib, "total_mib": total, "error": error,
                         "need_mib": need, "outside_mib": outside}
        t["marks"] += 1
        # DEGRADED, THE CARD IS READ EVERY recover_watch_s (S5b): the next read
        # is that long from the mark at the latest - never the watch_s the
        # rental was due at
        if "watch_at" in t:
            t["watch_at"] = min(t["watch_at"], now + self.recover_watch_s)
        self._log("rental_degraded", ticket_id=t["id"], holder=t["holder"], actor=t["actor"],
                  degraded=dict(t["degraded"]), memory_errors=errors, detail=why)

    def _watch_due(self):
        """Is the card to be read for the rental now? -> (the rental, its marks
        so far), or None: only a held rental, `watch_s` on the coordinator's
        clock after it was granted or last read."""
        r = self.rental
        if r is None or r["state"] != "held":
            return None
        now = self.clock()
        if now < r["watch_at"]:
            return None
        # degraded, every recover_watch_s: room again is noticed within seconds
        r["watch_at"] = now + (self.recover_watch_s if r["degraded"] else self.watch_s)
        return r, r["marks"]

    def _watched(self, r: dict, marks: int, card: dict) -> None:
        """The card as read for rental *r*, OUTSIDE the lock - judged under it.
        Free memory under the floor marks it degraded, once; room again clears
        it - unless it was marked while the card was being read. A card that
        cannot be read decides nothing."""
        if self.rental is not r or r["state"] != "held":
            return
        now = self.clock()
        r["read_at"] = now
        if not (card.get("known") and card.get("used_mib") is not None
                and card.get("total_mib") is not None):
            r["read"] = {"free_mib": None, "total_mib": None}
            return
        free = max(0, card["total_mib"] - card["used_mib"])
        r["read"] = {"free_mib": free, "total_mib": card["total_mib"]}
        if free < self.floor_mib and r["degraded"] is None:
            self._degrade(r, "under_floor", now)
            return
        need, outside = self._room(r, free, card["total_mib"])
        if r["degraded"] is None:
            return
        if free >= need and r["marks"] == marks:
            # CLEARED ON THE FIRST READ WITH ROOM FOR WHAT THE RENTAL NEEDS
            # (S5b) - never merely over the floor: a card with 300 MiB free
            # does not clear a rental whose 6 GB model came off it
            r["degraded"] = None
            # room again: the card is read every watch_s once more
            r["watch_at"] = now + self.watch_s
            self._log("rental_restored", ticket_id=r["id"], holder=r["holder"],
                      actor=r["actor"], free_mib=free, need_mib=need, outside_mib=outside,
                      detail=f"the card has room again: {free} MiB free of {card['total_mib']}, "
                             f"{need} MiB needed - the rental's models, less what its "
                             f"processes hold, and the floor of {self.floor_mib} MiB")
            return
        # still full: the numbers a person acts on are the newest read's, and
        # a look is never a line of the record
        r["degraded"] = dict(r["degraded"], free_mib=free, total_mib=card["total_mib"],
                             need_mib=need, outside_mib=outside)

    def _room(self, r: dict, free, total) -> tuple:
        """What rental *r* needs free before it is not degraded, and how much
        of the card is used outside the bootstrap, in MiB: -> (need,
        outside). The need is the rental's models - each one's approved need,
        or else what its process measured - less what its processes hold on
        the card now, and the floor beside it: a model still on the card needs
        no room again; one that came off needs all of its own. `outside` is
        the card's use less the rental's own, None when either is not known."""
        needs = {m["key"]: m["need_mib"] for m in r["models"]}
        needs.update({k: m["need_mib"] for k, m in (r.get("needs") or {}).items()})
        held = {}
        for x in r.get("processes") or []:
            mib = x["mib"] if x["mib"] is not None else needs.get(x["model"])
            held[x["model"]] = held.get(x["model"], 0) + int(mib or 0)
        wanted = sum(int(n) if n is not None else held.get(k, 0) for k, n in needs.items())
        need = self.floor_mib + max(0, wanted - sum(held.values()))
        ours = self._ours(r)
        outside = None if ours is None or free is None or total is None \
            else max(0, total - free - ours)
        return need, outside

    def _no_room_words(self, verdict: dict, r) -> str:
        """A model that does not fit on a card that is not over-full is refused
        at once, LOUDLY (H9; the owner, 2026-09-28: "Fail loudly. Memory
        almost never ever cleans itself up"): the numbers, what programs
        outside the bootstrap hold - the card's use less rental *r*'s own
        (None: no rental holds the card, and none of it is the bootstrap's) -
        and what to do, in the contract's one sentence."""
        ours = self._ours(r)
        free, total = verdict["free_mib"], verdict["total_mib"]
        outside = None if ours is None or free is None or total is None \
            else max(0, total - free - ours)
        return MEM.no_room_words(verdict, outside)

    def _ours(self, r):
        """What rental *r*'s processes use on the card, in MiB: each one's
        measured `mib`, or else the need its model was approved with - an
        estimate. 0 with no rental; None when one of them has neither."""
        if r is None:
            return 0
        needs = {m["key"]: m["need_mib"] for m in r["models"]}
        needs.update({k: m["need_mib"] for k, m in (r.get("needs") or {}).items()})
        total = 0
        for x in r.get("processes") or []:
            mib = x["mib"] if x["mib"] is not None else needs.get(x["model"])
            if mib is None:
                return None
            total += int(mib)
        return total

    @staticmethod
    def _ticket(t: dict) -> dict:
        """A ticket as an answer carries it - a copy, and never its token."""
        return {"id": t["id"], "holder": t["holder"], "actor": t["actor"], "class": t["class"],
                "models": [dict(m) for m in t["models"]], "label": t["label"], "for": t["for"],
                "state": t["state"], "since": t["since"],
                "offered_until": t["offered_until"] if t["state"] == "offered" else None,
                # S18: the time it starts no earlier than, whether it waits
                # for room, and - passed over - the room it waits on
                "not_before": t.get("not_before"),
                "if_no_room": t.get("if_no_room") or (
                    "wait" if t["class"] == "scheduled" else "fail"),
                "room": json.loads(json.dumps(t["room"])) if t.get("room") else None}

    @staticmethod
    def _rental(t: dict) -> dict:
        """A rental as an answer carries it - a copy, and never its token."""
        return {"id": t["id"], "holder": t["holder"], "actor": t["actor"], "class": t["class"],
                "models": [dict(m) for m in t["models"]], "label": t["label"], "for": t["for"],
                "granted_at": t["granted_at"], "expires_at": t["expires_at"],
                "state": t["state"], "processes": [dict(x) for x in t.get("processes") or []],
                "degraded": dict(t["degraded"]) if t.get("degraded") else None}

    def _over(self, tid: str, why: str):
        said = self._words.get(tid) or ENDED[why].format(tid=tid, drop_s=self.drop_s,
                                                         claim_s=self.claim_s)
        return 409, {"error": said, "reason": why}

    def _live(self, tid: str):
        """-> (the ticket, None) while it lives, or (None, the refusal saying
        why it cannot be used). A rental whose end is being checked - released,
        or taken back - is over for its holder from the moment it began."""
        if tid in self.closed:
            return None, self._over(tid, self.closed[tid])
        if self.rental is not None and self.rental["id"] == tid:
            return self.rental, None
        if tid in self.line:
            return self.line[tid], None
        return None, (409, {"error": f"no ticket {tid} holds that token",
                            "reason": "unknown_ticket"})

    def _named(self, tid: str, token):
        """The ticket *tid* - if *token* is its own. A token that is not the
        ticket's is answered as if there were no such ticket: nothing is told
        of another application's ticket."""
        if self._by_hash.get(token_hash(token)) != tid:
            return None, (409, {"error": f"no ticket {tid} holds that token",
                                "reason": "unknown_ticket"})
        return self._live(tid)

    def _held_by(self, token):
        """The ticket whose token is *token*."""
        tid = self._by_hash.get(token_hash(token))
        if tid is None:
            return None, (409, {"error": "no ticket holds that token",
                                "reason": "unknown_ticket"})
        return self._live(tid)

    @staticmethod
    def _wrong(*checks) -> list:
        return [p for c in checks for p in c]

    def ask(self, holder, models, actor=None, klass=None, label=None, for_=None,
            not_before=None, if_no_room=None):
        """Take a ticket. `actor` is who asks - the identity; `holder` is what
        it calls itself. `klass` is responsive (None) or scheduled; `models`
        what it wants to run, [{key, engine, need_mib}]. `for_` is the
        correlation id of the request the ticket serves.

        SCHEDULED WORK (S18, the owner's second ruling of 2026-09-28):
        `if_no_room` - `wait`, a scheduled ticket's default, or `fail` - says
        what it does when its models do not fit at its claim; `not_before`,
        seconds since the epoch, the time it starts no earlier than (`_due`).
        Responsive work is now: it always fails at once when it does not fit,
        and so a responsive ask that names a wait for room, or a time still to
        come, is refused `live_is_now`."""
        holder = str(holder or "").strip()
        if not holder:
            return 400, {"error": "a ticket needs a holder: name who is asking",
                         "reason": "no_holder"}
        klass = "responsive" if klass is None else klass
        wrong = self._wrong(V.check(actor, {"id": "actor"}, at="$.actor"),
                            V.check(klass, {"enum": list(CLASSES)}, at="$.class"),
                            V.check(models, {"array": {"ref": "need"}}, _SHAPES, at="$.models"),
                            [] if label is None else V.check(label, "string", at="$.label"),
                            [] if for_ is None else V.check(for_, {"id": "correlation"},
                                                            at="$.for"),
                            [] if not_before is None else V.check(not_before, "number",
                                                                  at="$.not_before"),
                            [] if if_no_room is None else V.check(
                                if_no_room, {"enum": list(IF_NO_ROOM)}, at="$.if_no_room"))
        if not wrong and not_before is not None and not math.isfinite(not_before):
            wrong = [f"$.not_before: a time is seconds since the epoch, a finite number, not "
                     f"{not_before!r}"]
        if wrong:
            return 400, {"error": wrong[0], "reason": V.BAD_REQUEST}
        if klass == "responsive" and if_no_room == "wait":
            return 400, {"error": "live work is now: a responsive ticket whose models do not fit "
                                  "fails at once, loudly, and never waits for room (if_no_room: "
                                  "wait); ask it as scheduled work to let it wait",
                         "reason": "live_is_now"}
        away = None if not_before is None else float(not_before) - self.clock()
        if klass == "responsive" and away is not None and away > 0:
            return 400, {"error": f"live work is now: a responsive ticket starts at once or not "
                                  f"at all, so it names no time still to come (not_before "
                                  f"{float(not_before):.0f} is {math.ceil(away):.0f} s away); "
                                  f"ask it as scheduled work to plan it for later",
                         "reason": "live_is_now"}
        for i, m in enumerate(models):
            why = None
            if not str(m["key"]).strip():
                why = "a model a ticket wants is named by its key"
            elif not str(m["engine"]).strip():
                why = "a model is run by an engine: name it"
            elif m["need_mib"] is not None and m["need_mib"] < 0:
                why = "a model's need_mib is a whole number of MiB, 0 or more"
            if why:
                return 400, {"error": f"models[{i}]: {why}", "reason": "bad_models"}
        # AN ASK THAT COULD NEVER FIT - more than the whole card, with the
        # margin - is refused at once, whatever its class: no wait can help it.
        # The card's whole is all this needs, and it never changes, so a
        # reading the machine kept will do; whether it fits NOW is its claim's
        card = self.hardware.probe()
        verdict = self._fit(self._need(models), card)
        with self._lock:
            now = self.clock()
            self._reap(now)
            if verdict["verdict"] == "never_fits":
                said = MEM.words(verdict)
                self._log("ticket_refused", holder=holder, actor=actor,
                          **{"class": klass}, label=label, **{"for": for_},
                          models=[dict(m) for m in models], memory=MEM.numbers(verdict),
                          detail=f"asked for more than the whole card: refused at once, "
                                 f"whatever its class; {said}")
                return 409, {"error": f"what {holder} asked for can never fit on this card: "
                                      f"{said}", "reason": "never_fits",
                             "memory": MEM.numbers(verdict)}
            self._ids += 1
            token = secrets.token_urlsafe(24)
            t = {"id": ids.mint_ticket(self._ids), "holder": holder, "actor": actor,
                 "class": klass, "label": label, "for": for_,
                 "models": [{"key": str(m["key"]), "engine": str(m["engine"]),
                             "need_mib": None if m["need_mib"] is None else int(m["need_mib"])}
                            for m in models],
                 "state": "waiting", "since": now, "offered_until": None,
                 "sha": token_hash(token), "heard_at": now, "waits": 0,
                 "not_before": None if not_before is None else float(not_before),
                 "if_no_room": "fail" if klass == "responsive" else (if_no_room or "wait"),
                 "room": None}
            self.line[t["id"]] = t
            self._by_hash[t["sha"]] = t["id"]
            self._log("ticket_asked", **self._about(t), token_sha256=t["sha"],
                      not_before=t["not_before"], if_no_room=t["if_no_room"])
            self._offer(now)
            return 200, {"ticket": self._ticket(t), "token": token}

    def wait(self, ticket_id, token, wait_s=None):
        """Wait on a ticket: held open - at most `wait_s`, and never longer
        than this coordinator's own hold - and answered the moment the ticket
        is offered, or can no longer be. A ticket a wait is held on is never
        dropped."""
        wrong = self._wrong(V.check(ticket_id, {"id": "ticket"}, at="$.ticket_id"),
                            V.check(token, "string", at="$.token"),
                            [] if wait_s is None else V.check(wait_s, "number", at="$.wait_s"))
        if wrong:
            return 400, {"error": wrong[0], "reason": V.BAD_REQUEST}
        hold = self.hold_s if wait_s is None else max(0.0, min(float(wait_s), self.hold_s))
        deadline = time.monotonic() + hold
        with self._cond:
            self._reap(self.clock())
            t, over = self._named(ticket_id, token)
            if over:
                return over
            t["heard_at"] = self.clock()
            if t["state"] != "waiting":
                return 200, {"ticket": self._ticket(t)}
            t["waits"] += 1
            try:
                while t["state"] == "waiting" and t["id"] not in self.closed:
                    left = deadline - time.monotonic()
                    if left <= 0 or self._closing.is_set():
                        break
                    self._cond.wait(left)
            finally:
                t["waits"] -= 1
                t["heard_at"] = self.clock()
            t, over = self._live(t["id"])
            if over:
                return over
            return 200, {"ticket": self._ticket(t)}

    def claim(self, ticket_id, token):
        """The card a ticket was offered, claimed with its token: the rental.
        THE CLAIM IS THE GRANT, AND THE MEMORY IS VERIFIED FIRST (D2): the card
        read afresh - never an answer kept - and what the ticket declared it
        needs, with the margin, checked against its free memory (the one
        memory check, the contract's). Not fitting now, a responsive ticket
        is refused at once, in numbers - the person waiting can act now - and
        a scheduled one waits for the room, passed over; one that could never
        fit is refused, whatever its class. A card that cannot be read decides
        nothing."""
        wrong = self._wrong(V.check(ticket_id, {"id": "ticket"}, at="$.ticket_id"),
                            V.check(token, "string", at="$.token"))
        if wrong:
            return 400, {"error": wrong[0], "reason": V.BAD_REQUEST}
        # what the card holds before this rental puts anything on it - what it
        # is granted on, and what its end is checked against - asked afresh,
        # and outside the lock
        card = self.hardware.probe(fresh=True)
        with self._lock:
            now = self.clock()
            self._reap(now)
            t, over = self._named(ticket_id, token)
            if over:
                return over
            if t["state"] == "held":                        # asked again: the same rental
                return 200, {"rental": self._rental(t)}
            if t["state"] == "waiting":
                return 409, {"error": f"{t['id']} is waiting: the card has not been offered "
                                      f"to it", "reason": "not_offered"}
            verdict = self._fit(self._need(t["models"]), card)
            # S18: scheduled work that asked to fail rather than wait for room
            # is refused at once, as responsive work always is
            if verdict["verdict"] == "never_fits" or (verdict["verdict"] == "no_room"
                                                      and t["if_no_room"] == "fail"):
                return self._refuse(t, verdict, now)
            if verdict["verdict"] == "no_room":
                return self._pass_over(t, verdict, now)
            del self.line[t["id"]]
            t.update(state="held", offered_until=None, granted_at=now,
                     expires_at=now + self.ttl_s, life=self.ttl_s, processes=[],
                     card_at_grant=card["used_mib"] if card.get("known") else None,
                     degraded=None, **self._watching(now))
            # the grant's read is the card's last, until the watch reads it
            if verdict["free_mib"] is not None:
                t["read"] = {"free_mib": verdict["free_mib"], "total_mib": verdict["total_mib"]}
            self.rental = t
            self._log("rental_granted", **self._about(t), memory=MEM.numbers(verdict),
                      detail=f"the card read afresh: {MEM.words(verdict)}")
            self._changed()
            return 200, {"rental": self._rental(t)}

    def _refuse(self, t: dict, verdict: dict, now: float):
        """A ticket whose needs do not fit at its claim, ended: refused at once
        - `never_fits` whatever its class, `no_room` for responsive work - in
        numbers, naming nobody. The card goes to the next in line."""
        reason = verdict["verdict"]
        said = self._no_room_words(verdict, None) if reason == "no_room" else MEM.words(verdict)
        how = ("can never fit on this card" if reason == "never_fits"
               else "does not fit on the card now")
        why = ("more than the whole card: refused at once, whatever its class"
               if reason == "never_fits" else
               "responsive, and it does not fit on the card now: refused at once"
               if t["class"] == "responsive" else
               "scheduled, asked to fail rather than wait for room, and it does not fit on the "
               "card now: refused at once")
        self._end(t, "refused", "ticket_refused", **{"class": t["class"]},
                  models=[dict(m) for m in t["models"]], memory=MEM.numbers(verdict),
                  detail=f"{why}; {said}")
        self._offer(now)
        return 409, {"error": f"{t['id']} was refused: what it asked for {how}: {said}",
                     "reason": reason, "memory": MEM.numbers(verdict)}

    def _pass_over(self, t: dict, verdict: dict, now: float):
        """Scheduled work whose needs do not fit now waits for the room, within
        its asker's patience: back in line, waiting, passed over until the card
        is read again and has its room - once on the record each time. The
        card goes to the next in line. S18: what it waits on is kept on the
        ticket (`room`) and said in the words a person reads while it waits -
        the numbers, what programs outside the bootstrap hold, and that it
        starts by itself once it fits - refreshed at each read (`_recheck`)."""
        t["room"] = room = self._room_waited(verdict, now)
        t["state"], t["offered_until"], t["short"] = "waiting", None, True
        self._rechecked = time.monotonic()                  # the claim read the card
        self._log("ticket_passed_over", **self._about(t), memory=MEM.numbers(verdict),
                  outside_mib=room["outside_mib"],
                  detail=f"scheduled: it waits for room on the card; {room['words']}")
        self._changed()
        self._offer(now)
        return 409, {"error": f"{t['id']} waits for room on the card: {room['words']}",
                     "reason": "not_offered", "memory": MEM.numbers(verdict)}

    @staticmethod
    def _bad_processes(processes):
        """Why a heartbeat's list cannot be a rental's, or None: more than
        MAX_PROCESSES, a proof that is not a load's id, a pid named twice."""
        if processes is None:
            return None
        if len(processes) > MAX_PROCESSES:
            return f"a rental names at most {MAX_PROCESSES} processes, not {len(processes)}"
        pids = set()
        for i, x in enumerate(processes):
            if not (ids.matches("actor", x["proof"]) and x["proof"].startswith("load-")):
                return (f"processes[{i}]: a proof is the id of the load that started it "
                        f"(load-...), not {json.dumps(x['proof'])}")
            if x["pid"] in pids:
                return f"processes[{i}]: pid {x['pid']} is named twice"
            pids.add(x["pid"])
        return None

    def _name(self, t: dict, processes: list, now: float) -> None:
        """The heartbeat's list, whole, in place of the one kept: a process it
        still names keeps its `since`; one it names anew is known from now. A
        line of the record when the list changed - never one per beat."""
        was = {(x["pid"], x["proof"]): x for x in t["processes"]}
        new = [{"pid": int(x["pid"]), "proof": str(x["proof"]), "model": str(x["model"]),
                "engine": str(x["engine"]), "port": x["port"], "mib": x["mib"],
                "since": was[(x["pid"], x["proof"])]["since"]
                if (x["pid"], x["proof"]) in was else now} for x in processes]
        now_named = {(x["pid"], x["proof"]) for x in new}
        added = [x for x in new if (x["pid"], x["proof"]) not in was]
        gone = [x for key, x in was.items() if key not in now_named]
        t["processes"] = new
        if gone:
            # one of the rental's own processes has just gone: its memory is
            # still coming back, and an amend now waits for it (`amend`)
            t["gone_at"] = time.monotonic()
        if added or gone:
            said = ([f"named pid {x['pid']} ({x['model']} on {x['engine']}, carrying "
                     f"{x['proof']})" for x in added]
                    + [f"no longer names {named_words(x)}" for x in gone]
                    + [f"it names {processes_word(len(new))} now"])
            self._log("rental_processes", ticket_id=t["id"], actor=t["actor"],
                      processes=[dict(x) for x in new], detail="; ".join(said))

    @staticmethod
    def _bad_work(progress, deadline_s, errors, slow=None):
        """Why what a heartbeat says of the work cannot be so, or None: a count
        or a deadline below zero, a deadline that is no number of seconds."""
        if progress is not None and progress < 0:
            return f"$.progress: a count of tokens and answers is 0 or more, not {progress}"
        if deadline_s is not None and (not math.isfinite(deadline_s) or deadline_s < 0):
            return f"$.deadline_s: a deadline is a number of seconds, 0 or more, not {deadline_s}"
        if errors is not None and errors < 0:
            return f"$.memory_errors: a count of lines is 0 or more, not {errors}"
        if slow is not None and slow < 0:
            return f"$.slow_answers: a count of answers is 0 or more, not {slow}"
        return None

    def heartbeat(self, token, processes=None, working=None, progress=None, deadline_s=None,
                  memory_errors=None, memory_error=None, slow_answers=None, slow_answer=None):
        """Keep the rental whose token this is. `processes` names every process
        the rental put on the card - its pid, the proof its command line
        carries, its model, engine, port and memory - the whole list, which
        replaces the one kept: a heartbeat lost on the way is healed by the
        next, and a heartbeat without one leaves it as it was. What it says of
        the WORK (D3) is watched: whether it is `working`, its `progress`, a
        one-piece answer's `deadline_s`, and the memory errors its models'
        logs said (`memory_errors`, the newest `memory_error`) - see `_heard`.
        The answer says whether the card is `wanted` - another ticket is in
        line for it, and its time has come (S18: a ticket planned for later
        holds back nobody until then) - and the rental, `degraded` when its
        card is over-full."""
        wrong = self._wrong(V.check(token, "string", at="$.token"),
                            [] if processes is None else
                            V.check(processes, {"array": {"ref": "process"}}, _SHAPES,
                                    at="$.processes"),
                            [] if working is None else V.check(working, "boolean",
                                                               at="$.working"),
                            [] if progress is None else V.check(progress, "integer",
                                                                at="$.progress"),
                            [] if deadline_s is None else V.check(deadline_s, "number",
                                                                  at="$.deadline_s"),
                            [] if memory_errors is None else V.check(memory_errors, "integer",
                                                                     at="$.memory_errors"),
                            [] if memory_error is None else V.check(memory_error, "string",
                                                                    at="$.memory_error"),
                            [] if slow_answers is None else V.check(slow_answers, "integer",
                                                                    at="$.slow_answers"),
                            [] if slow_answer is None else V.check(slow_answer, "string",
                                                                   at="$.slow_answer"))
        if wrong:
            return 400, {"error": wrong[0], "reason": V.BAD_REQUEST}
        why = self._bad_processes(processes)
        if why:
            return 400, {"error": why, "reason": "bad_processes"}
        why = self._bad_work(progress, deadline_s, memory_errors, slow_answers)
        if why:
            return 400, {"error": why, "reason": "bad_work"}
        with self._lock:
            now = self.clock()
            self._reap(now)
            t, over = self._held_by(token)
            if over:
                return over
            if t["state"] != "held":
                return 409, {"error": f"{t['id']} is {t['state']}: it holds no rental",
                             "reason": "not_held"}
            t["expires_at"] = now + t["life"]
            if processes is not None:
                self._name(t, processes, now)
            self._heard(t, working, progress, deadline_s, memory_errors, memory_error, now,
                        slow_answers, slow_answer)
            # WANTED: another ticket is in line - and not a word more: never
            # whose, for what or how many (D4). The line holds only tickets
            # still waiting or offered, and the rental's own left it at its claim
            return 200, {"rental": self._rental(t),
                         "wanted": any(self._due(x, now) for x in self.line.values())}

    def amend(self, token, model, processes=None):
        """May *model* go on the card during the rental whose token this is?
        (D2.) The card is read afresh - OUTSIDE the lock - and the model's need
        (`need_mib`, the application's estimate; None adds nothing), with the
        margin, checked against its free memory: approved, or refused in
        words and numbers - `no_room`, `never_fits` - naming nobody. A card
        that cannot be read decides nothing: approved.

        RIGHT AFTER THE RENTAL'S OWN UNLOAD, the memory is still coming back
        (the Dictaphone's H-3: a whisper-server gone in 476 ms, its memory
        back half a second later). So when one of the rental's processes went
        less than `amend_settle_s` ago, an amend that finds no room reads the
        card again until the model fits, or that long after the process went,
        and only then refuses. `processes`, when sent, is the rental's whole
        list, applied first as a heartbeat's is - so a process that has just
        gone is known to be gone before the card is judged. An amend renews
        nothing.

        Approved, the rental's `models` follow what is on the card: the models
        its named processes run, each with the need it was approved with, and
        this one."""
        wrong = self._wrong(V.check(token, "string", at="$.token"),
                            V.check(model, {"ref": "need"}, _SHAPES, at="$.model"),
                            [] if processes is None else
                            V.check(processes, {"array": {"ref": "process"}}, _SHAPES,
                                    at="$.processes"))
        if wrong:
            return 400, {"error": wrong[0], "reason": V.BAD_REQUEST}
        why = self._bad_processes(processes)
        if why:
            return 400, {"error": why, "reason": "bad_processes"}
        if not str(model["key"]).strip() or not str(model["engine"]).strip():
            return 400, {"error": "$.model: a model is named by its key, and run by an engine",
                         "reason": V.BAD_REQUEST}
        if model["need_mib"] is not None and model["need_mib"] < 0:
            return 400, {"error": "$.model.need_mib: a model's need is a whole number of MiB, "
                                  "0 or more", "reason": V.BAD_REQUEST}
        want = {"key": str(model["key"]), "engine": str(model["engine"]),
                "need_mib": None if model["need_mib"] is None else int(model["need_mib"])}
        with self._lock:
            now = self.clock()
            self._reap(now)
            t, over = self._held_by(token)
            if over:
                return over
            if t["state"] != "held":
                return 409, {"error": f"{t['id']} is {t['state']}: it holds no rental",
                             "reason": "not_held"}
            if processes is not None:
                self._name(t, processes, now)
            if t.get("degraded"):
                return self._while_degraded(t, want)
            gone_at = t.get("gone_at")
        until = None if gone_at is None else gone_at + self.amend_settle_s
        while True:
            verdict = self._fit(want["need_mib"], self.hardware.probe(fresh=True))
            if verdict["verdict"] in ("fits", "unknown", "never_fits") or until is None \
                    or time.monotonic() >= until:
                break
            time.sleep(min(SETTLE_POLL_S, max(0.0, until - time.monotonic())))
        said = MEM.words(verdict)
        with self._lock:
            t, over = self._held_by(token)
            if over:
                return over
            if t["state"] != "held":
                return 409, {"error": f"{t['id']} is {t['state']}: it holds no rental",
                             "reason": "not_held"}
            if t.get("degraded"):
                return self._while_degraded(t, want)
            if verdict["verdict"] in ("no_room", "never_fits"):
                how = ("can never fit on this card" if verdict["verdict"] == "never_fits"
                       else "does not fit on the card now")
                if verdict["verdict"] == "no_room":
                    said = self._no_room_words(verdict, t)
                self._log("amend_refused", ticket_id=t["id"], actor=t["actor"], models=[want],
                          memory=MEM.numbers(verdict), detail=f"{want['key']} {how}: {said}")
                return 409, {"error": f"{want['key']} {how}: {said}",
                             "reason": verdict["verdict"], "memory": MEM.numbers(verdict)}
            needs = {m["key"]: m for m in t["models"]}
            needs.update(t.get("needs") or {})
            needs[want["key"]] = want
            t["needs"] = needs
            on_card = []
            for x in t["processes"]:
                if x["model"] not in on_card:
                    on_card.append(x["model"])
            t["models"] = [dict(needs.get(k) or {"key": k, "engine": next(
                x["engine"] for x in t["processes"] if x["model"] == k), "need_mib": None})
                for k in on_card if k != want["key"]] + [dict(want)]
            self._log("rental_amended", ticket_id=t["id"], actor=t["actor"],
                      models=[dict(m) for m in t["models"]], memory=MEM.numbers(verdict),
                      detail=f"{want['key']} may go on the card: {said}; its models are "
                             f"{', '.join(m['key'] for m in t['models'])} now")
            return 200, {"rental": self._rental(t), "memory": MEM.numbers(verdict)}

    def _while_degraded(self, t: dict, want: dict):
        """An amend of a DEGRADED rental (D3): nothing new goes on the card
        until it has room again - refused `degraded`, saying why, and on the
        record; the card is not read for it."""
        said = f"{want['key']} may not go on the card now: the rental is degraded - " \
               f"{t['degraded']['why']}"
        self._log("amend_refused", ticket_id=t["id"], actor=t["actor"], models=[dict(want)],
                  detail=said)
        return 409, {"error": said, "reason": "degraded"}

    def release(self, token):
        """Give the card back. THE RELEASE IS CHECKED: answered once every
        process the rental still names is gone and, where the card can be
        read, its memory is back to what it read at the grant - waited for
        OUTSIDE the lock, at most the settle time. What is still there then
        is ended on proof, and the record names each. Only then is the card
        offered to the next in line."""
        wrong = V.check(token, "string", at="$.token")
        if wrong:
            return 400, {"error": wrong[0], "reason": V.BAD_REQUEST}
        with self._lock:
            now = self.clock()
            self._reap(now)
            t, over = self._held_by(token)
            if over:
                return over
            if t["state"] != "held":
                return 409, {"error": f"{t['id']} is {t['state']}: it holds no rental",
                             "reason": "not_held"}
            t["state"] = "releasing"
            self._close(t, "released")
            named = [dict(x) for x in t["processes"]]
            # on the record at once: a coordinator stopped in the middle of the
            # check rebuilds the rental as being given back, and finishes it
            self._log("rental_releasing", ticket_id=t["id"], holder=t["holder"],
                      actor=t["actor"], processes=[dict(x) for x in named],
                      detail="given back by its holder; " + (
                          f"its end is checked - its {processes_word(len(named))} gone, the "
                          f"card's memory back - before the card goes to anybody else"
                          if named else "it names no process on the card"))
        return self._released(t, named)

    def _released(self, t: dict, named: list):
        """The rest of a release, OUTSIDE the lock until its end: every one
        of *named* gone and the card's memory back, within the settle time;
        what is left ended on proof; then, under the lock, the record and the
        card to the next ticket. -> the release's answer. Also what a
        coordinator restarted in the middle of a release finishes it with."""
        left, memory = self._settle(t, named)
        # a straggler is ended on its proof - left is empty unless the machine
        # can say what is alive, and one that can ends too
        outcomes = self._end_on_proof(left) if left else []
        if outcomes:
            _, memory = self._settle(t, [])
        if not named:
            said = ["it named no process on the card"]
        elif getattr(self.hardware, "alive", None) is None:
            said = [f"no machine configured: whether its {processes_word(len(named))} had gone "
                    f"was not asked"]
        elif not left:
            said = [f"its {processes_word(len(named))} had gone"]
        else:
            said = [f"{len(named) - len(left)} of its {processes_word(len(named))} had gone"]
        with self._lock:
            for x, o in zip(left, outcomes):
                self._log(f"process_{o['outcome']}", ticket_id=t["id"], actor=t["actor"],
                          process=dict(x), detail=o["detail"])
            if self.rental is t:
                self.rental = None
                self._card_freed()
            t["state"] = "released"
            self._end(t, "released", "rental_released", models=[dict(m) for m in t["models"]],
                      detail="; ".join(said + [o["detail"] for o in outcomes] + [memory]))
            self._offer(self.clock())
            return 200, {"released": self._rental(t)}

    def cancel(self, token):
        """Give a ticket up before it is claimed. A wait held on it is answered
        at once; an offer it had passes on."""
        wrong = V.check(token, "string", at="$.token")
        if wrong:
            return 400, {"error": wrong[0], "reason": V.BAD_REQUEST}
        with self._lock:
            now = self.clock()
            self._reap(now)
            t, over = self._held_by(token)
            if over:
                return over
            if t["state"] == "held":
                return 409, {"error": f"{t['id']} holds the card: give it back with a release, "
                                      f"not a cancel", "reason": "claimed"}
            gone = self._ticket(t)
            self._end(t, "cancelled", "ticket_cancelled",
                      detail=f"its asker gave it up while it was {t['state']}")
            self._offer(now)
            return 200, {"cancelled": gone}

    def close(self) -> None:
        """Stop holding waits open: every wait held now is answered at once,
        and every one asked from now on too. What is held stays held - and in
        the record, for the next start."""
        with self._cond:
            self._closing.set()
            self._cond.notify_all()

    # ------------------------------------------------------------- the ports

    def request_port(self, actor, purpose, ttl_s=None):
        """A port for *actor* to run *purpose* on. One per actor per purpose:
        the same actor asking again renews the one it holds."""
        purpose = str(purpose or "").strip()
        if not purpose:
            return 400, {"error": "a port needs a purpose: say what will listen on it",
                         "reason": "no_purpose"}
        wrong = V.check(actor, {"id": "actor"}, at="$.actor")
        if wrong:
            return 400, {"error": wrong[0], "reason": V.BAD_REQUEST}
        if ttl_s is not None and (isinstance(ttl_s, bool) or not isinstance(ttl_s, (int, float))
                                  or ttl_s <= 0):
            return 400, {"error": "ttl_s must be positive", "reason": "bad_ttl"}
        ttl = float(ttl_s) if ttl_s is not None else self.ttl_s
        with self._lock:
            now = self.clock()
            self._reap(now)
            for p, lease in self.ports.items():
                if lease["actor"] == actor and lease["purpose"] == purpose:
                    lease["expires_at"], self._port_ttl[p] = now + ttl, ttl
                    return 200, {"port": dict(lease)}
            busy = 0
            for p in range(self.port_from, self.port_to + 1):
                if p in self.ports:
                    continue
                if not self._port_free(p):
                    busy += 1                   # the machine's truth: something else has it
                    continue
                lease = {"port": p, "actor": actor, "purpose": purpose,
                         "granted_at": now, "expires_at": now + ttl}
                self.ports[p], self._port_ttl[p] = lease, ttl
                self._log("port_granted", port=p, actor=actor, purpose=purpose)
                return 200, {"port": dict(lease)}
            return 409, {"error": f"no free port in {self.port_from}-{self.port_to}: "
                                  f"{len(self.ports)} held, {busy} in use by something else",
                         "reason": "exhausted"}

    def _port_closed(self, port: int, actor, why: str) -> None:
        """A port given back or taken back, and why - kept for the newest
        `keep_closed`, as the tickets that ended are: an older one's holder is
        then told it holds no such port. A port closed again is the newest
        once more. The only way anything is put in `port_closed`."""
        key = (port, actor)
        self.port_closed.pop(key, None)
        self.port_closed[key] = why
        while len(self.port_closed) > self.keep_closed:
            del self.port_closed[next(iter(self.port_closed))]

    def _port_over(self, port, actor):
        """Why *actor* cannot use *port* - naming no other application."""
        lease = self.ports.get(port)
        if lease is not None:
            return 409, {"error": f"port {port} is not {actor}'s: another actor holds it",
                         "reason": "not_yours"}
        why = self.port_closed.get((port, actor))
        if why == "reclaimed":
            return 409, {"error": f"port {port} was reclaimed: its holder stopped heartbeating",
                         "reason": "reclaimed"}
        if why == "released":
            return 409, {"error": f"port {port} was released", "reason": "released"}
        return 409, {"error": f"port {port} is not held by {actor}", "reason": "unknown_port"}

    def heartbeat_port(self, port, actor):
        if port is None:
            return 400, {"error": "a heartbeat names its port", "reason": "no_port"}
        with self._lock:
            now = self.clock()
            self._reap(now)
            lease = self.ports.get(port)
            if lease is not None and lease["actor"] == actor:
                lease["expires_at"] = now + self._port_ttl.get(port, self.ttl_s)
                return 200, {"port": dict(lease)}
            return self._port_over(port, actor)

    def release_port(self, port, actor):
        if port is None:
            return 400, {"error": "a release names its port", "reason": "no_port"}
        with self._lock:
            now = self.clock()
            self._reap(now)
            lease = self.ports.get(port)
            if lease is not None and lease["actor"] == actor:
                del self.ports[port]
                self._port_ttl.pop(port, None)
                self._port_closed(port, actor, "released")
                self._log("port_released", port=port, actor=actor, purpose=lease["purpose"])
                return 200, {"released": lease}
            return self._port_over(port, actor)

    def up(self):
        """Is the coordinator up? An application's server asks, and is told
        that, and the version of the contract this coordinator runs - so it
        can tell whether the two speak one contract - and nothing more: the
        rest is the operator's (`state`)."""
        return 200, {"up": True, "contract_version": self.contract_version}

    def state(self):
        card = self.hardware.probe()
        with self._lock:
            self._reap(self.clock())
            r = self.rental
            # how much of the card is OUTSIDE the bootstrap: its use, less what
            # the rental's processes use (measured, or their approved needs)
            ours = self._ours(r)
            used = card.get("used_mib") if card.get("known") else None
            outside = None if used is None or ours is None else max(0, int(used) - ours)
            return 200, {"actor": self.actor, "contract_version": self.contract_version,
                         "rental": self._rental(r) if r else None,
                         "watch": None if r is None else {
                             "working": bool(r["working"]), "progress": r["progress"],
                             "moved_at": r["moved_at"], "due_at": r["due_at"],
                             "read_at": r["read_at"],
                             # counted even where it marks nothing (S11b R2)
                             "slow_answers": int(r["slow_answers"])},
                         "tickets": [self._ticket(t) for t in self._in_line()],
                         "ports": [dict(self.ports[p]) for p in sorted(self.ports)],
                         "port_range": {"from": self.port_from, "to": self.port_to},
                         "limits": {"ttl_s": self.ttl_s, "claim_s": self.claim_s,
                                    "drop_s": self.drop_s, "hold_s": self.hold_s,
                                    "settle_s": self.settle_s, "margin_mib": self.margin_mib,
                                    "amend_settle_s": self.amend_settle_s,
                                    "recheck_s": self.recheck_s, "hang_s": self.hang_s,
                                    "watch_s": self.watch_s, "floor_mib": self.floor_mib,
                                    "full_mib": self.full_mib,
                                    "recover_watch_s": self.recover_watch_s},
                         "started_at": self.started_at,
                         "card": {"known": bool(card.get("known")),
                                  "used_mib": card.get("used_mib"),
                                  "total_mib": card.get("total_mib"),
                                  "ours_mib": ours, "outside_mib": outside},
                         "lock": dict(self.lock)}

    # ------------------------------------------------------ the machine's lock

    def lock_waiting(self, path, detail: str) -> None:
        """An application run on its own has the machine's lock: nothing is
        offered until this coordinator has it. Said on the record when what
        holds it changes, never once a look."""
        with self._lock:
            said = self.lock["state"] == "waiting" and self.lock["detail"] == detail
            self.lock = {"state": "waiting", "path": str(path), "detail": detail}
            if not said:
                self._log("lock_waiting", detail=detail)
            for t in self.line.values():
                if t["state"] == "offered":
                    # an offer the record's rebuild made before the lock was
                    # tried: nobody claims the card while somebody else has it
                    t["state"], t["offered_until"] = "waiting", None
                    self._changed()

    def lock_taken(self, path, detail: str) -> None:
        """The machine's lock is this coordinator's: the card goes to the next
        ticket in line, on the reaper's next beat."""
        with self._lock:
            self.lock = {"state": "held", "path": str(path), "detail": detail}
            self._log("lock_taken", detail=detail)

    def events_since(self, since: int = 0):
        with self._lock:
            return 200, {"events": [dict(e) for e in self.events if e["seq"] >= since],
                         "next": self.seq}


# ---------------------------------------------------------------- the wire

DASHBOARD = """<!doctype html><meta charset="utf-8"><title>coordinator</title>
<style>body{font:14px system-ui;margin:1.5rem;background:#15171c;color:#e6e6e6}
h2{font-size:1rem;margin:1.2rem 0 .4rem}td{padding:.15rem .8rem .15rem 0}
.dim{color:#8a8f98}.held{color:#f0b35a}.free{color:#6fcf8f}.warn{color:#ef6b5e}</style>
<h1 style="font-size:1.1rem">the card</h1><div id="card"></div><table id="procs"></table>
<div id="work" class="dim"></div>
<div id="mem" class="dim"></div>
<div id="lock" class="dim"></div>
<div id="limits" class="dim"></div>
<h2>in line</h2><table id="line"></table>
<h2>ports</h2><table id="ports"></table>
<h2>record</h2><table id="events"></table>
<script>
const $=id=>document.getElementById(id), ago=t=>Math.round(Date.now()/1000-t)+'s ago';
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const keys=ms=>(ms||[]).map(m=>m.key).join(', ');
const clock=t=>{const d=new Date(t*1000),p=n=>String(n).padStart(2,'0');
  return p(d.getHours())+':'+p(d.getMinutes())};
const how={held:'held',releasing:'being given back - its processes checked',
  reclaiming:'being taken back - its processes ended on proof'};
async function tick(){try{
  const s=await (await fetch('/state')).json(), e=await (await fetch('/events')).json();
  const r=s.rental, w=s.watch;
  $('card').innerHTML=r?`<b class="held">${esc(how[r.state]||r.state)}</b>${r.degraded
    ?` - <b class="warn">degraded</b> since ${ago(r.degraded.since)}: ${esc(r.degraded.why)}${
      r.degraded.need_mib!=null?`; it clears with ${r.degraded.need_mib} MiB free`:''} -`
    :''} by ${esc(r.holder)}
    <span class="dim">${esc(r.actor)}</span> for ${esc(keys(r.models))}
    <span class="dim">(${esc(r.class)})</span>
    ${r.label?`<span class="dim">- ${esc(r.label)}</span>`:''}
    ${r.for?`<span class="dim">- serving ${esc(r.for)}</span>`:''}
    <span class="dim">- granted ${ago(r.granted_at)}, expires in
    ${Math.max(0,Math.round(r.expires_at-Date.now()/1000))}s</span>`:'<b class="free">free</b>';
  $('procs').innerHTML=r?((r.processes||[]).map(p=>`<tr><td>pid ${esc(p.pid)}</td>
    <td>${esc(p.model)}</td><td class="dim">${esc(p.engine)}</td>
    <td>${p.mib==null?'<span class="dim">memory not measured</span>':esc(p.mib)+' MiB'}</td>
    <td class="dim">port ${esc(p.port??'-')}</td><td class="dim">${esc(p.proof)}</td>
    <td class="dim">since ${ago(p.since)}</td></tr>`).join('')
    ||'<tr><td class="dim">no process on the card</td></tr>'):'';
  $('work').textContent=r&&w?(w.working?`working: ${w.progress??0} tokens and answers so far, `
    +`moved ${ago(w.moved_at)}`+(w.due_at?`; an answer in one piece is due in `
    +`${Math.max(0,Math.round(w.due_at-Date.now()/1000))}s`:'')
    :`not working${w.progress!=null?` (${w.progress} tokens and answers so far)`:''}`)
    +(w.read_at?`; the card read for it ${ago(w.read_at)}`:'')
    +(w.slow_answers?`; ${w.slow_answers} of its answers far slower than their model's normal`
    :''):'';
  const m=s.card, l=s.limits;
  $('mem').textContent=m.known?`${m.used_mib} of ${m.total_mib} MiB in use, as the machine sees it`
    +(m.outside_mib!=null?`; ${m.outside_mib} MiB outside the bootstrap - the rental's own `
    +`taken as ${m.ours_mib} MiB (each process's measured memory, or the need its model was `
    +`approved with)`:'')
    :'the card was not asked';
  $('lock').textContent=s.lock.detail;
  $('limits').textContent=`a rental lives ${l.ttl_s}s between heartbeats; an offer ${l.claim_s}s; `
    +`a ticket nobody waits on ${l.drop_s}s; a wait is held at most ${l.hold_s}s; `
    +`a release settles within ${l.settle_s}s; ${l.margin_mib} MiB is kept free beside what is `
    +`asked; an amend waits ${l.amend_settle_s}s for the memory after an unload; the card is `
    +`read again every ${l.recheck_s}s for work waiting for room; a rental working with no `
    +`progress is taken back after ${l.hang_s}s; the card is read every ${l.watch_s}s during a `
    +`rental, and a rental is degraded under ${l.floor_mib} MiB free - read every `
    +`${l.recover_watch_s}s while it is, until it has room for what it needs`;
  $('line').innerHTML=s.tickets.map(t=>`<tr><td>${esc(t.state)}</td><td>${esc(t.class)}</td>
    <td>${esc(t.holder)}</td><td class="dim">${esc(t.actor)}</td><td>${esc(keys(t.models))}</td>
    <td class="dim">${esc(t.label)}</td><td class="dim">since ${ago(t.since)}</td>
    <td>${t.room?`<span class="warn">waits for room: ${esc(t.room.words)}</span>`
      :t.not_before!=null?`starts at ${esc(clock(t.not_before))} at the earliest`:''}</td>
    </tr>`).join('')
    ||'<tr><td class="dim">nobody</td></tr>';
  $('ports').innerHTML=s.ports.map(p=>`<tr><td>${esc(p.port)}</td><td>${esc(p.purpose)}</td>
    <td class="dim">${esc(p.actor)}</td><td class="dim">held since ${ago(p.granted_at)}</td>
    </tr>`).join('')||`<tr><td class="dim">none held (${esc(s.port_range.from)}-${esc(s.port_range.to)})</td></tr>`;
  $('events').innerHTML=e.events.slice(-40).reverse().map(v=>`<tr><td class="dim">${v.seq}</td>
    <td>${esc(v.kind)}</td><td>${esc(v.holder??v.purpose)}</td><td class="dim">${esc(v.actor)}</td>
    <td>${esc(v.models?keys(v.models):v.process?'pid '+v.process.pid
      :v.processes?v.processes.map(p=>'pid '+p.pid).join(', '):v.port)}${v.memory
      ?esc(` - needs ${v.memory.need_mib} of ${v.memory.free_mib??'?'} MiB free`):''}${
      v.not_before!=null?esc(` - starts at ${clock(v.not_before)} at the earliest`):''}</td>
    <td class="dim">${esc(v.class)}</td>
    <td class="dim">${esc(v.for)}</td><td class="dim">${esc(v.detail)}</td></tr>`).join('');
}catch(err){$('card').textContent='the coordinator is not answering'}}
tick();setInterval(tick,1000);
</script>"""


#: the names every coordinator on this machine answers to - as a Host header
#: writes them - besides the address it listens on and the host of each page
#: it was started to allow
OWN_NAMES = ("127.0.0.1", "localhost", "[::1]")
#: a Host header: a name - an IPv6 address in its brackets - and a port
HOST = re.compile(r"(\[[0-9A-Fa-f:.]+\]|[^\s:@/?#\[\]]+)(?::[0-9]+)?")
#: the most of a request's body the coordinator reads, unless `--max-body`
#: says otherwise. Its bodies are a ticket's ask, a wait, a claim, a
#: heartbeat, a port's request - under a KiB each; an ask naming a hundred
#: models is some 10 KiB - and 1 MiB is a hundred times that, while a body of
#: gigabytes, sent by mistake or on purpose, is never read.
MAX_BODY = 1024 * 1024
#: how long a connection whose body was never read is kept once its answer is
#: out, before it is closed - so an asker still sending hears the answer, not
#: a reset. Measured on this machine (urllib, bodies of 1 KiB to 64 MiB over a
#: cap of 1 KiB, 20 of each): closed at once, not one body of 8 MiB or more was
#: answered, and half of those of 1 MiB; kept 0.02 s, every one was. A quarter
#: of a second is that twelve times over.
UNREAD_GRACE_S = 0.25
#: how long a request has to arrive whole - its line, its headers and its body
#: - from its connection being made, unless `--read-timeout` says otherwise;
#: one that has not is never answered, and its connection is closed. Its
#: askers are programs - an application's server, a holder - each sending a
#: request whole, at once, over this machine's loopback, a body of under a
#: KiB; measured on the server, a body of 16 MiB arrived and was answered in
#: 24 to 49 ms. Ten seconds is the server's own bound, kept the same here.
READ_TIMEOUT_S = 10.0
#: what an allowed page's browser is told the page may send: the methods the
#: routes take, and the one header a JSON body needs
ALLOW_METHODS = "GET, POST"
ALLOW_HEADERS = "Content-Type"


class _Reads(io.RawIOBase):
    """A connection's reads, each allowed only what is left of the time its
    request has to arrive whole: `until`, on the monotonic clock, set when the
    coordinator starts waiting for the request (`start`). None left:
    TimeoutError, and http.server closes the connection. The reads alone: the
    socket is blocking again after each, so an answer - a wait held open
    included - is written as it always was. `came` counts the bytes since the
    wait began: a request cut off part way has some; a connection that never
    sent a byte has none. (The server has its own: the parts share no code.)"""

    def __init__(self, sock) -> None:
        super().__init__()
        self._sock = sock
        self.until = None
        self.came = 0
        self.expired = False

    def readable(self) -> bool:
        return True

    def start(self, seconds: float) -> None:
        self.until, self.came, self.expired = time.monotonic() + seconds, 0, False

    def readinto(self, buf) -> int:
        if self.until is not None:
            left = self.until - time.monotonic()
            if left <= 0:
                self.expired = True
                raise TimeoutError("the request did not arrive whole within the read timeout")
            self._sock.settimeout(left)
        try:
            n = self._sock.recv_into(buf)
        except TimeoutError:
            self.expired = True
            raise
        finally:
            try:
                self._sock.settimeout(None)
            except OSError:
                pass                                    # closed: nothing more is read of it
        self.came += n
        return n


class Listener(ThreadingHTTPServer):
    """The coordinator's listening socket - its port its alone. http.server
    binds SO_REUSEADDR, and on Windows a second program's socket that sets it
    too is let bind a port the first one holds. Measured against this
    coordinator, before: a helper's bind beside it succeeded; and a
    coordinator started on a port a helper already listened on started, said
    it was listening, and of 20 requests heard none - the helper took them
    all. On Windows it binds SO_EXCLUSIVEADDRUSE instead: a port somebody
    holds refuses the bind, and nobody's bind shares it after. Elsewhere
    SO_REUSEADDR stays, as http.server sets it."""
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self) -> None:
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address) -> None:
        """A connection its asker reset or dropped mid-request - a server
        under this coordinator ended while it asked - is said in one line of
        the log, never a traceback (R-B5: one was printed once). Anything
        else is said as http.server says it."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            say = getattr(self, "say", None) or (lambda line: print(line, flush=True))
            try:
                say(f"a connection from {client_address[0]}:{client_address[1]} was reset by its "
                    f"asker mid-request ({type(exc).__name__}): nothing was answered")
            except Exception:                               # noqa: BLE001 - a word never fails
                pass
            return
        super().handle_error(request, client_address)


def listen(port: int, host: str = "127.0.0.1") -> Listener:
    """The coordinator's port, had - its alone (`Listener`) - and not yet
    served: CannotListen when somebody holds it, or it cannot be had."""
    try:
        return Listener((host, port), Handler)
    except OSError as e:
        raise CannotListen(f"cannot listen on {host}:{port}: {e}") from None


def origin_of(address) -> str:
    """*address* as a browser writes a page's address in the Origin header it
    puts on a request: `scheme://host[:port]`, lower case, no default port, no
    path - so a page named at the start is the header its browser sends.
    ValueError, saying why, for anything that is not a page's address: `null`
    and `*` included - either would let in pages of anybody's. (The server
    reads its `--allow-origin` the same way; the parts share no code.)"""
    text = str(address)
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as e:
        raise ValueError(f"{text!r} is not a page's address: {e}") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{text!r} is not a page's address: one is written "
                         "http://<host>:<port>, as the browser shows it")
    if (parts.path not in ("", "/") or parts.query or parts.fragment
            or parts.username is not None or parts.password is not None):
        raise ValueError(f"{text!r} is not a page's address: that is its scheme, host "
                         "and port alone - no path, query or name")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    default = {"http": 80, "https": 443}[parts.scheme]
    return f"{parts.scheme}://{host}" + ("" if port in (None, default) else f":{port}")


def host_name(host: str):
    """The name a Host header names, in lower case and without its port - an
    IPv6 address in its brackets - or None: a Host that is not a name and a
    port is not guessed around."""
    hit = HOST.fullmatch(str(host).strip())
    return hit.group(1).lower() if hit else None


def as_named(host: str) -> str:
    """A host - an address listened on, a page's host - as a Host header names
    it: lower case, an IPv6 address in brackets."""
    host = str(host).lower().strip("[]")
    return f"[{host}]" if ":" in host else host


def own_names(host: str, allowed) -> frozenset:
    """Every name a coordinator answers to: every coordinator's own
    (OWN_NAMES), the address it listens on, and the host of each page it was
    started to allow."""
    return frozenset(OWN_NAMES) | {as_named(host)} | {as_named(urlsplit(o).hostname)
                                                     for o in allowed}


class Handler(BaseHTTPRequestHandler):
    """The routes, over HTTP. Before any route, on every request whatever its
    method (the contract's `origins/coordinator.json`), these are checked in
    this order, and the first that refuses answers (`_refused`): the NAME it
    was sent to - a Host that is not one of the coordinator's own names, a
    website whose name was made to point at this machine; the PAGE that sent
    it - an Origin that is neither the coordinator's own (the dashboard) nor
    a page it was started to allow; and its BODY - one sent in chunks (a
    Transfer-Encoding), or over the cap, never read. Then a body no route
    reads - a GET's - is read and set aside, and a method no route takes is
    answered 501, as http.server answers one: all of it in one place
    (`parse_request`), before any method of the handler runs. A request must
    arrive whole within the read timeout (`handle_one_request`). A request
    with no Origin, sent to one of its own names, and one from the
    dashboard, are answered exactly as before the checks."""

    def log_message(self, *a):   # the record is the log; stderr stays quiet
        pass

    def setup(self) -> None:
        """The connection, its reads bounded in time (`_Reads`) in place of
        http.server's own reader of the same socket."""
        super().setup()
        self.rfile.close()
        self._reads = _Reads(self.connection)
        self.rfile = io.BufferedReader(self._reads)

    def handle_one_request(self) -> None:
        """One request, which must arrive whole - its line, its headers and
        its body - within the read timeout of its connection being made. One
        that has not is never answered: its connection is closed
        (http.server closes one whose read timed out). A request cut off part
        way is said on the console, never written to the record; a
        connection that never sent a byte is closed without a word."""
        self.command = self.path = None
        self._reads.start(self.server.read_timeout_s)
        super().handle_one_request()
        if self._reads.expired and self._reads.came:
            what = f"{self.command} {self.path}" if self.command else "a request"
            try:
                self.server.say(f"{what} -> its connection closed: not read whole within "
                                f"{self.server.read_timeout_s:g}s ({self._reads.came} bytes came)")
            except Exception:                               # noqa: BLE001
                pass

    def parse_request(self) -> bool:
        """http.server's own reading of the request line and headers - then,
        on every request whatever its method, the checks before any route
        (`_refused`); a body no route reads, read and set aside; and a method
        no route takes, answered 501 (`_not_taken`). -> True only for a
        request a method of this handler answers; False when it is answered
        already - http.server's own 400 for a request it cannot read, too."""
        if not super().parse_request() or self._refused():
            return False
        if self.command != "POST":
            self._set_aside()
        if not hasattr(self, f"do_{self.command}"):
            self._not_taken()
            return False
        return True

    def _set_aside(self) -> None:
        """A body within the cap that no route reads, read and never used - so
        the connection is never closed with the body still coming, and the
        asker hears its answer, not a reset."""
        n = self._length()
        if n:
            self.rfile.read(n)

    def _not_taken(self) -> None:
        """A method no route takes: 501, as http.server answers one - after
        the checks every request goes through, its body within the cap read
        and set aside."""
        self.send_error(HTTPStatus.NOT_IMPLEMENTED, f"Unsupported method ({self.command!r})")

    def _from(self):
        """Which page sent the request, by the Origin header its browser put on
        it: None - no Origin: not a page, or the dashboard asking a GET; "own"
        - the dashboard: the Origin is the address the request was sent to,
        `http://` and its Host, however that is written; "allowed" - a page
        the coordinator was started to allow; "refused" - any other."""
        origin = self.headers.get("Origin")
        if origin is None:
            return None
        host = self.headers.get("Host")
        if host is not None and origin == f"http://{host}":
            return "own"
        return "allowed" if origin in self.server.allowed_origins else "refused"

    def _allow(self) -> None:
        """What an allowed page's browser must be told to hand the page the
        answer - every answer it is given. Nothing for any other request."""
        if self._from() == "allowed":
            self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
            self.send_header("Vary", "Origin")

    def _host_refused(self) -> bool:
        """Was the request sent to a name that is not the coordinator's? One
        that names no Host at all was not: every browser names one."""
        host = self.headers.get("Host")
        return host is not None and host_name(host) not in self.server.own_names

    def _length(self) -> int:
        """The body's length, as its Content-Length says: a whole number of
        bytes, in digits. Anything else reads as no body - never as a length
        below 0, which `read()` would take for "until the connection closes"."""
        said = (self.headers.get("Content-Length") or "").strip()
        return int(said) if said.isascii() and said.isdigit() else 0

    def _refused(self) -> bool:
        """Refused before any route - its Host, its Origin, its body, in that
        order: True when it was, and there is nothing more to do."""
        if self._host_refused():
            host = self.headers["Host"]
            return self._before(403, {
                "error": f"this coordinator does not answer to {host}: it answers to its own "
                         "names - 127.0.0.1, localhost, [::1] and the address it listens on - "
                         "and to the host of each page it was started to allow (--allow-origin)",
                "reason": "host_not_allowed"}, f" for {host}")
        if self._from() == "refused":
            origin = self.headers["Origin"]
            return self._before(403, {
                "error": f"a page at {origin} may not use this coordinator: it answers only its "
                         "own page - the dashboard - and the pages it was started to allow "
                         "(--allow-origin)",
                "reason": "origin_not_allowed"}, f" from {origin}")
        chunks = self.headers.get("Transfer-Encoding")
        if chunks is not None:
            # a body is read by its Content-Length alone: one in chunks is
            # never read, whatever the method and whatever length it names
            return self._before(411, {
                "error": f"a body sent with Transfer-Encoding: {chunks} is never read by this "
                         "coordinator: send it whole, its length in Content-Length",
                "reason": "length_required"}, f" - Transfer-Encoding: {chunks}")
        n = self._length()
        if n > self.server.max_body:
            return self._before(413, {
                "error": f"a body of {n} bytes is more than this coordinator reads: "
                         f"{self.server.max_body} at most (--max-body)",
                "reason": "too_large"}, f" - a body of {n} bytes")
        return False

    def _unread(self) -> bool:
        """Is its body one the coordinator never reads: in chunks, or over the cap?"""
        return (self.headers.get("Transfer-Encoding") is not None
                or self._length() > self.server.max_body)

    def _before(self, status: int, out: dict, said: str) -> bool:
        """An answer before any route, said on the console - never written to
        the record, which a page must not be able to fill by asking. The
        connection is closed after every answer, and closed with the body
        still coming the asker is sent a reset in place of the answer. So a
        body within the cap is read and set aside before the answer; one in
        chunks or over the cap is never read: the answer says `Connection:
        close`, and this returns - and http.server closes the connection - a
        moment after the answer is out (UNREAD_GRACE_S), reading nothing."""
        unread = self._unread()
        if not unread:
            self._set_aside()
        self._send(status, out, closing=unread)
        try:
            self.server.say(f"{self.command} {self.path} -> {status} {out['reason']}{said}")
        except Exception:                                   # noqa: BLE001
            pass
        if unread:
            time.sleep(UNREAD_GRACE_S)
        return True

    def _send(self, status: int, body, ctype="application/json", closing: bool = False):
        """One answer. `closing`: the connection is closed after it, and the
        answer says so."""
        raw = body.encode("utf-8") if isinstance(body, str) else \
            json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            if closing:
                self.send_header("Connection", "close")
            self._allow()
            self.end_headers()
            self.wfile.write(raw)
        except OSError:
            # the asker went away while its wait was held open: nothing to
            # tell, and nobody to tell it to
            pass

    def _body(self) -> dict:
        """A body that is not a JSON object reads as an empty one, so the
        route's own refusal for the missing field answers it. It is never in
        chunks or over the cap: those were refused before any route."""
        n = self._length()
        try:
            b = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except (ValueError, UnicodeDecodeError):
            return {}
        return b if isinstance(b, dict) else {}

    def do_OPTIONS(self):
        """The preflight: what a browser asks before it sends a request a page
        could not send from a form - one with a JSON body. Answered for a page
        the coordinator was started to allow; refused before any route, as
        every request is, for any other page; and with no Origin, or from the
        dashboard, what it always was - a method the coordinator does not
        take, answered as http.server answers one."""
        if self._from() != "allowed":
            self._not_taken()
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", ALLOW_METHODS)
        self.send_header("Access-Control-Allow-Headers", ALLOW_HEADERS)
        self.send_header("Content-Length", "0")
        self._allow()
        self.end_headers()

    def do_GET(self):
        c = self.server.coordinator
        path, _, query = self.path.partition("?")
        if path == "/state":
            return self._send(*c.state())
        if path == "/events":
            try:
                since = int(parse_qs(query).get("since", ["0"])[0])
            except ValueError:
                since = 0
            return self._send(*c.events_since(since))
        if path == "/up":
            return self._send(*c.up())
        if path in ("/", "/dashboard"):
            return self._send(200, DASHBOARD, "text/html")
        return self._send(404, {"error": f"no route {path}", "reason": "no_route"})

    def do_POST(self):
        c, b = self.server.coordinator, self._body()
        route = ROUTES.get(self.path)
        if route is not None and route["method"] == "POST" and route["request"] is not None:
            # The body is checked against its route with the contract's own
            # validator, as the server does: `bad_request` names the first
            # problem in the contract's words, and a missing field the route
            # refuses by name is left to that refusal.
            wrong = V.refuse_body(route, b, _SHAPES, OWN_REFUSALS.get(route["id"], ()))
            if wrong:
                return self._send(400, {"error": wrong, "reason": V.BAD_REQUEST})
        if self.path == "/card/ask":
            return self._send(*c.ask(b.get("holder"), b.get("models"), actor=b.get("actor"),
                                     klass=b.get("class"), label=b.get("label"),
                                     for_=b.get("for"), not_before=b.get("not_before"),
                                     if_no_room=b.get("if_no_room")))
        if self.path == "/card/wait":
            return self._send(*c.wait(b.get("ticket_id"), b.get("token"), b.get("wait_s")))
        if self.path == "/card/claim":
            return self._send(*c.claim(b.get("ticket_id"), b.get("token")))
        if self.path == "/card/heartbeat":
            return self._send(*c.heartbeat(b.get("token"), b.get("processes"),
                                           working=b.get("working"), progress=b.get("progress"),
                                           deadline_s=b.get("deadline_s"),
                                           memory_errors=b.get("memory_errors"),
                                           memory_error=b.get("memory_error"),
                                           slow_answers=b.get("slow_answers"),
                                           slow_answer=b.get("slow_answer")))
        if self.path == "/card/release":
            return self._send(*c.release(b.get("token")))
        if self.path == "/card/cancel":
            return self._send(*c.cancel(b.get("token")))
        if self.path == "/card/amend":
            return self._send(*c.amend(b.get("token"), b.get("model"), b.get("processes")))
        if self.path == "/port/request":
            return self._send(*c.request_port(b.get("actor"), b.get("purpose"), b.get("ttl_s")))
        if self.path == "/port/heartbeat":
            return self._send(*c.heartbeat_port(b.get("port"), b.get("actor")))
        if self.path == "/port/release":
            return self._send(*c.release_port(b.get("port"), b.get("actor")))
        return self._send(404, {"error": f"no route {self.path}", "reason": "no_route"})


def lock_note(host: str, port: int, actor: str) -> dict:
    """The note a coordinator writes beside the machine's lock once it has
    it: its pid and when - what a reader holds against the process, to tell a
    live note from a stale one - its address, and its actor."""
    return {"pid": os.getpid(), "since": time.time(),
            "coordinator": f"http://{as_named(host)}:{port}", "actor": actor}


def _claims(note) -> str:
    """What a note in the contract's shape says of its writer, in the
    operator's words."""
    if "coordinator" in note:
        return f"the coordinator at {note['coordinator']}"
    what = note["model"] if note["model"] is not None else "its session"
    return f"an application run on its own, {note['holder']}, for {what}"


def holder_words(note, stale=None) -> str:
    """Who holds the machine's lock, as its note says it - the operator's
    words, naming everyone; never an application's. A STALE note - its
    writer gone (`holder_of`) - is said to be, and what it claimed with it:
    it names nobody."""
    if stale:
        return (f"something holds it that has not said who it is - the note beside it is "
                f"stale: {stale}")
    if not isinstance(note, dict) or V.check(note, machine.CARD_LOCK["note"], _SHAPES):
        return "something holds it that says nothing this coordinator can read"
    if "coordinator" in note:
        return f"the coordinator at {note['coordinator']} holds it (pid {note['pid']})"
    what = note["model"] if note["model"] is not None else "its session"
    return (f"an application run on its own holds the card (pid {note['pid']}, "
            f"{note['holder']}, for {what})")


def holder_of(lock: MachineLock, wait_s: float = 0.0):
    """Whoever holds the lock this could not take, as the note beside it says:
    -> (note, stale). The note, when it is in the contract's shape and the
    process that wrote it lives - (note, None); a note whose writer has gone
    names nobody - (None, why, with what it claimed); none, or none that can
    be read - (None, None). A NOTE IS TRUSTED ONLY WHILE ITS WRITER HOLDS THE
    LOCK: a holder that died left its note, and the next holder writes its own
    a moment after it takes the lock - so a lock with no live note beside it
    is given *wait_s* for its holder to write one, then read as it is."""
    until = time.monotonic() + max(0.0, wait_s)
    while True:
        note = lock.note()
        stale = None
        if isinstance(note, dict) and not V.check(note, machine.CARD_LOCK["note"], _SHAPES):
            why = machine.stale_note(note)
            if why is None:
                return note, None
            stale = f"{why} (it named {_claims(note)})"
        if time.monotonic() >= until:
            return None, stale
        time.sleep(NOTE_STEP_S)


def _another(lock: MachineLock, wait_s: float = None) -> None:
    """Raise AnotherCoordinator when the lock this could not take is another
    coordinator's, as its live note says - given a moment (NOTE_WAIT_S) when
    there is none yet: a coordinator writes its note just after it takes the
    lock, and one found in that instant is still another coordinator (H5's
    register 3). A stale note names nobody (H5's register 6)."""
    other, _ = holder_of(lock, NOTE_WAIT_S if wait_s is None else wait_s)
    if other is not None and "coordinator" in other:
        raise AnotherCoordinator(
            f"another coordinator runs on this machine, at {other['coordinator']} (pid "
            f"{other['pid']}): it holds the machine's lock at {lock.path}, and a machine runs "
            f"one coordinator")


def serve(coordinator: Coordinator, port: int = 7071, host: str = "127.0.0.1",
          allow_origins=(), max_body: int = MAX_BODY, log=None, card_lock=None,
          read_timeout_s: float = READ_TIMEOUT_S, listener=None):
    """Start serving; -> the server. A reaper thread takes back expired
    rentals, passes on unclaimed offers and drops tickets nobody waits on even
    when nobody is asking, so a crashed holder's card comes back on time. A
    held wait takes a thread of its own for as long as it is held.
    `allow_origins` names the pages, besides the dashboard, that may use it -
    none by default: applications never ask the coordinator from a browser;
    the host of each is one of its own names, beside the address it listens
    on (`own_names`). `max_body` is the most of a request's body it reads, in
    bytes; `read_timeout_s` how long a request has to arrive whole. `log`
    hears each refusal before any route, and each request cut off (the
    console, unless given). Each setting is read before the port is taken,
    so a page's address that is not one, a cap below 1, or a timeout that is
    not a number of seconds above 0, stops the start. The port is its alone
    (`Listener`): one somebody holds raises OSError.

    `card_lock` is where the machine's card lock is (D5): taken before the
    first request is answered, and held until `stop`, its note giving the
    address served here and the coordinator's actor - where every server on
    the machine finds it. Held by an application run on its own, it is waited
    for, on a thread of its own: the routes answer, tickets are taken, and
    none is offered the card until the lock is had - `/state` and the page
    say so. Held by another coordinator, the start stops: AnotherCoordinator,
    saying where it is. None - a coordinator built in code, as every suite
    builds one - takes no lock.

    `listener` is the port already had (`listen`): the command line has it
    before the coordinator is built - the record read, a reclaim or a release
    resumed - so one started on a port somebody holds acts on nothing. A start
    that stops after the port is had lets it go."""
    allowed = frozenset(origin_of(o) for o in allow_origins)
    if isinstance(max_body, bool) or not isinstance(max_body, int) or max_body < 1:
        raise ValueError(f"max_body is a whole number of bytes, 1 or more, not {max_body!r}")
    if isinstance(read_timeout_s, bool) or not isinstance(read_timeout_s, (int, float)) \
            or not math.isfinite(read_timeout_s) or read_timeout_s <= 0:
        raise ValueError(f"read_timeout_s is a number of seconds above 0, not {read_timeout_s!r}")
    srv = listener if listener is not None else listen(port, host)
    try:
        _serving(srv, coordinator, host, allowed, max_body, read_timeout_s, log, card_lock)
    except BaseException:
        srv.server_close()
        raise
    return srv


def lock_taken_words(path, address: str) -> str:
    """What a coordinator that took the lock at *path* says it holds (S16a
    R4): the machine's lock, where every server on this machine finds it - or
    a lock of its own (`--card-lock`), which a server finds only when its
    settings name it, or when it is started with `--coordinator`."""
    if machine.is_machine_lock(path):
        return (f"this coordinator holds the machine's lock, and its note gives its address, "
                f"{address}: every server on this machine finds it there")
    return (f"this coordinator holds its own lock, {path} - not the machine's - and its note "
            f"there gives its address, {address}: a server finds it only when its settings "
            f"name this lock ([runner] card_lock), or when it is started with --coordinator "
            f"{address}")


def _serving(srv, coordinator: Coordinator, host: str, allowed, max_body: int,
             read_timeout_s: float, log, card_lock) -> None:
    """`serve`, once its port is had: the settings, the lock, the reaper and
    the answering."""
    srv.coordinator = coordinator
    srv.daemon_threads = True
    srv.allowed_origins = allowed
    srv.own_names = own_names(host, allowed)
    srv.max_body = max_body
    srv.read_timeout_s = float(read_timeout_s)
    srv.say = log or (lambda line: print(line, flush=True))
    stop_ = threading.Event()
    srv.stop_reaper = stop_
    srv.card_lock = None
    if card_lock is not None:
        lock = card_lock if isinstance(card_lock, MachineLock) else MachineLock(card_lock)
        note = lock_note(host, srv.server_address[1], coordinator.actor)
        address = note["coordinator"]
        taken = lock_taken_words(lock.path, address)

        def take() -> bool:
            if not lock.take():
                return False
            lock.write_note(note)
            coordinator.lock_taken(lock.path, taken)
            return True
        if not take():
            _another(lock)

            def waiting() -> str:
                # read only once the lock was found held: a note beside a free
                # lock is nobody's, and a stale one is said to be (the page's)
                return (f"waiting for the machine's lock: {holder_words(*holder_of(lock))}; no "
                        f"ticket is offered the card until this coordinator has it")

            def wait_for_the_lock():
                while not stop_.is_set():
                    coordinator.lock_waiting(lock.path, waiting())
                    if stop_.wait(LOCK_POLL_S) or take():
                        return
            coordinator.lock_waiting(lock.path, waiting())
            srv.lock_waiter = threading.Thread(target=wait_for_the_lock, daemon=True,
                                               name="coordinator-card-lock")
            srv.lock_waiter.start()
        srv.card_lock = lock

    def reaper():
        while not stop_.wait(max(0.02, min(1.0, coordinator.ttl_s / 4))):
            coordinator.reap()
        coordinator.close()                     # a coordinator that stops holds no wait open
    threading.Thread(target=reaper, daemon=True).start()
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def stop(srv) -> None:
    """Stop serving: the reaper, then every wait held open - answered at once -
    then the port, and then the machine's lock, its note first."""
    srv.stop_reaper.set()
    srv.coordinator.close()
    srv.shutdown()
    srv.server_close()
    waiter = getattr(srv, "lock_waiter", None)
    if waiter is not None:
        waiter.join(timeout=LOCK_POLL_S * 8)    # a take under way ends first
    if getattr(srv, "card_lock", None) is not None:
        srv.card_lock.release()


def _page_address(text: str) -> str:
    """`--allow-origin`, read as the browser will send it - or refused at the
    start, saying why, never met at the first request."""
    try:
        return origin_of(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _bytes(text: str) -> int:
    """`--max-body`: a whole number of bytes, 1 or more - or refused at the
    start, saying why."""
    try:
        n = int(text)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number of bytes, 1 or more")
    return n


def _seconds(text: str) -> float:
    """`--read-timeout`: a number of seconds above 0 - or refused at the
    start, saying why. Never a bound that is none: not infinity, not nan."""
    try:
        s = float(text)
    except ValueError:
        s = 0.0
    if not math.isfinite(s) or s <= 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of seconds above 0")
    return s


def settle_bound() -> str:
    """The most an amend may wait for a memory just given back, and why - the
    contract's own (`memory.py`)."""
    return (f"{MEM.AMEND_SETTLE_MAX_S:g}s: a server waits {MEM.AMEND_ANSWER_S:g}s for an "
            f"amend's answer, one it never hears decides nothing, and the card is read once "
            f"more after the wait")


def _amend_settle(text: str) -> float:
    """`--amend-settle`: a number of seconds above 0, and at most the
    contract's AMEND_SETTLE_MAX_S - or refused at the start, saying why."""
    try:
        s = float(text)
    except ValueError:
        s = 0.0
    if not math.isfinite(s) or s <= 0 or s > MEM.AMEND_SETTLE_MAX_S:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of seconds above 0 and at "
                                         f"most {settle_bound()}")
    return s


def parse_args(argv=None):
    """The command line, checked: -> the arguments, with `ports` as (low, high)."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", required=True, help="folder for the record")
    ap.add_argument("--port", type=int, default=7071)
    ap.add_argument("--ttl", type=float, default=15.0,
                    help="seconds a rental lives between heartbeats")
    ap.add_argument("--claim", type=float, default=30.0,
                    help="seconds a ticket offered the card has to claim it")
    ap.add_argument("--drop", type=float, default=120.0,
                    help="seconds a waiting ticket nobody waits on is kept")
    ap.add_argument("--hold", type=float, default=30.0,
                    help="the longest a wait is held open, in seconds")
    ap.add_argument("--settle", type=float, default=10.0,
                    help="seconds the end of a rental waits for its processes to go, and the "
                         "card's memory to come back, before it ends what is left on proof")
    ap.add_argument("--margin", type=int, default=MEM.MARGIN_MIB, metavar="<MiB>",
                    help=f"the free memory kept beside what a ticket or an amend needs, in MiB - "
                         f"{MEM.MARGIN_MIB} unless given")
    ap.add_argument("--amend-settle", type=_amend_settle, default=5.0, metavar="<seconds>",
                    help=f"how long an amend right after one of the rental's own processes went "
                         f"waits for its memory to come back, before it refuses - at most "
                         f"{MEM.AMEND_SETTLE_MAX_S:g}s, the contract's, so a server hears its "
                         f"answer")
    ap.add_argument("--recheck", type=float, default=5.0, metavar="<seconds>",
                    help="how often the card is read again for scheduled work waiting for room")
    ap.add_argument("--hang", type=float, default=300.0, metavar="<seconds>",
                    help="how long a rental that says it is working may go without its progress "
                         "moving before it is taken back, as a lapsed rental is")
    ap.add_argument("--watch", type=float, default=30.0, metavar="<seconds>",
                    help="how often the card is read during a rental, to see it over-full")
    ap.add_argument("--floor", type=int, default=MEM.MARGIN_MIB, metavar="<MiB>",
                    help=f"the free memory, in MiB, under which a rental is degraded - "
                         f"{MEM.MARGIN_MIB} unless given")
    ap.add_argument("--full", type=int, default=FULL_MIB, metavar="<MiB>",
                    help=f"the free memory, in MiB, under which the card counts as full: an "
                         f"answer far slower than its model's normal then marks a rental "
                         f"degraded too - {FULL_MIB} unless given")
    ap.add_argument("--recover-watch", type=float, default=RECOVER_WATCH_S, metavar="<seconds>",
                    help=f"how often the card is read while a rental is degraded, instead of "
                         f"--watch, so room again is noticed within seconds - "
                         f"{RECOVER_WATCH_S:g} unless given")
    ap.add_argument("--card-lock", default=None, metavar="<path>",
                    help="the machine's card lock this coordinator takes as it starts and holds "
                         "while it runs, its note giving its address - the machine's own, in the "
                         "user's temporary folder, unless given")
    ap.add_argument("--ports", default=f"{PORT_RANGE[0]}-{PORT_RANGE[1]}",
                    help="the range the ports it hands out come from, as LOW-HIGH")
    ap.add_argument("--hardware", default="this-machine", choices=("this-machine", "none"),
                    help="ask this machine's card (nvidia-smi) and end what a lapsed rental "
                         "left on it, on proof - or neither")
    ap.add_argument("--allow-origin", action="append", default=None, type=_page_address,
                    metavar="<page address>",
                    help="a page, besides the dashboard, that may use the coordinator - written "
                         "as the browser shows its address: http://127.0.0.1:7867. Repeatable; "
                         "none by default, as applications never ask it from a browser. A page "
                         "from any other address is refused before any route")
    ap.add_argument("--max-body", type=_bytes, default=MAX_BODY, metavar="<bytes>",
                    help=f"the most of a request's body the coordinator reads, in bytes - "
                         f"{MAX_BODY} (1 MiB) unless given; a larger body is refused "
                         f"(413 too_large) and never read")
    ap.add_argument("--read-timeout", type=_seconds, default=READ_TIMEOUT_S, metavar="<seconds>",
                    help=f"how long a request has to arrive whole - its line, its headers and "
                         f"its body - {READ_TIMEOUT_S:g}s unless given; one that has not is "
                         f"never answered, and its connection is closed")
    a = ap.parse_args(argv)
    try:
        lo, hi = (int(x) for x in a.ports.split("-"))
    except ValueError:
        ap.error(f"--ports is LOW-HIGH, like {PORT_RANGE[0]}-{PORT_RANGE[1]}, not {a.ports!r}")
    a.ports = (lo, hi)
    return a


def coordinator_from(a, actor=None) -> Coordinator:
    """The coordinator the command line asks for, with every setting it named
    - and the actor its note already gave, when it gave one."""
    return Coordinator(a.data, ttl_s=a.ttl, claim_s=a.claim, drop_s=a.drop, hold_s=a.hold,
                       settle_s=a.settle, ports=a.ports, hardware=hardware_for(a.hardware),
                       margin_mib=a.margin, amend_settle_s=a.amend_settle, recheck_s=a.recheck,
                       hang_s=a.hang, watch_s=a.watch, floor_mib=a.floor, full_mib=a.full,
                       recover_watch_s=a.recover_watch, actor=actor)


def card_lock_of(a) -> Path:
    """The machine's card lock a coordinator started from its command line
    takes: the one `--card-lock` names, or the machine's own."""
    return Path(a.card_lock) if a.card_lock else machine.machine_lock()


def main() -> int:
    a = parse_args()
    # the machine's lock is tried before anything else, and then the port -
    # both before the record: a second coordinator, or one started on a port
    # somebody holds (refused, never shared: `Listener`), must not write a line
    # of it, nor resume a reclaim or a release, before it stops. Its note goes
    # beside the lock the moment the port is had - before the record is read:
    # a coordinator started meanwhile finds this one, never an application
    # that says nothing (H5's register 3). Whatever stops the start is said as
    # what it was: CannotListen names the port; a setting the coordinator
    # refuses, the setting (H5's register 2) - and the lock is let go, its
    # note first, so no note of a coordinator that never started is left
    lock = MachineLock(card_lock_of(a))
    try:
        if not lock.take():
            _another(lock)
        port = listen(a.port)
        actor = ids.mint_actor("crd")
        if lock.held:
            lock.write_note(lock_note("127.0.0.1", port.server_address[1], actor))
        srv = serve(coordinator_from(a, actor), a.port, allow_origins=a.allow_origin or (),
                    max_body=a.max_body, read_timeout_s=a.read_timeout, card_lock=lock,
                    listener=port)
    except (AnotherCoordinator, OSError, ValueError) as e:
        lock.release()
        print(f"cannot start: {e}", flush=True)            # and the process ends, letting go
        return 2
    print(f"listening on 127.0.0.1:{srv.server_address[1]}", flush=True)
    print(srv.coordinator.lock["detail"], flush=True)
    if srv.allowed_origins:
        print(f"pages at {', '.join(sorted(srv.allowed_origins))} may use it", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
