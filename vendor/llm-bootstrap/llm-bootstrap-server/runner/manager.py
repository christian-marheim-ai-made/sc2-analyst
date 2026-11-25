"""The model manager: models side by side on the card, inside a pipeline.

Every load and unload is a line in `runner_events.jsonl`. That file is the
instrument, not decoration: `max_concurrency` reads it and must never find two
loads of ONE model open at once in one process, `beside_faults` finds a load
whose line does not name exactly what else was up (`beside`), and
`orphaned_loads` finds a model a stopped server left running.

MODELS SIDE BY SIDE (PLAN-4 D7). The runner keeps several models on the card
at once - one load of each, at most `max_loads` (8: a coordinator's rental
names at most 8 processes; 1 when the settings name one port for every model):
  * LOADS RUN ONE AT A TIME, under the LOAD LOCK - never the manager's lock -
    so nothing that asks the manager waits for a load: a generation on a model
    already up, health, a bind, an unload. Stops take no load lock, and run
    together.
  * PINNED: the models an application's pipeline keeps - named at its open,
    or loaded by `load_model` (`POST /api/models/{key}/load`). One stays until
    it is unloaded (`unload_model`), the pipeline closes or is lost, or the
    server stops: the idle sweep never takes it, and no other load takes it
    off to make room.
  * ON DEMAND: a model a job needs that is not up - loaded for the job, and
    taken off by the idle sweep once unused for `idle_unload_s`.
  * MAKING ROOM: a load that does not fit - past the runner's limit, or
    refused by the card source's fit check (`_check_vram`) - takes off the
    on-demand models not answering a request, the one used longest ago first,
    asking again after each; a load nothing more can make room for is refused
    in the fit check's own words. A stop already under way - an unload, the
    idle sweep - is room being made: waited out, and asked again, never a
    second model off for it.
  * READY WITHOUT POLLING: `load_model` and `open_pipeline` take `wait_s` - the
    answer comes the moment the model is up (its health check answered), or
    its load failed, within that bound.

THE PIPELINE (PLAN-4 D6) is the card held from its open to its close: one
ticket's rental with a coordinator, the machine's lock without one, nothing to
ask of the stand-in. A model switch inside it never gives the card back, and a
pipeline with nothing loaded still holds it. The card goes back only when the
pipeline closes, once every model of it has come off. One per server:
  * an APPLICATION'S (`open_pipeline`), or the one the server opened as it
    started (the settings' `session_models`): its models are loaded once the
    card is had, and kept - the idle sweep never takes them - and it closes
    when it is closed, when it lapses (not renewed for its `keep_s`; 0 never
    lapses), or when the server stops;
  * the QUEUE'S OWN, opened by a load when no pipeline is open, with the
    terms of the request that opened it. It closes once its work is done - no
    generation, no load and no wait under way, and nothing the queue still
    owes (`work_pending`): at once when the card is wanted (a coordinator's
    heartbeat says so), otherwise once the idle sweep has taken its model off.
  An open that finds the queue's own takes it over, card and all. A card taken
  back (an answer said so) takes every model off; an application's pipeline
  then asks for the card again by itself, and nothing loads until it is had.

THE WORK, AS A COORDINATOR'S WATCH READS IT (PLAN-4 D3). Every answer goes
through `answer`, which counts the work where it really happens - each token
streamed and each answer's end - and says, while it streams, that a model is
answering; an answer that comes in one piece declares its deadline through
its backend (`Backend.answer_deadline_s`). After each answer, and after a load
that failed, the model's log is read for the lines that say the card's memory
ran out (`Backend.memory_errors`). `work` is all of it, read by the card
source on each heartbeat - never under the manager's lock. The card source
tells `_heard_degraded` when the coordinator says the card is DEGRADED: the
answers under way end, a load under way is given up, and no model work is
done - `Degraded`, the job's `card_degraded` - until it clears. THE CARD FULL
IS RECOVERED FROM BY ITSELF (S5b): on a source that says when the card has
room again (`recovers`), a job's work is HELD for the card's memory instead
(`_hold_while_full`), within its patience, the numbers said as it waits; the
source's word that it has room again (`_heard_restored`) loads again the
session's models that came off, and lets the held work through. The seam for
answers far slower than their model's normal on a full card (D3, with the
timing statistics - S11b) is `answer`, where each answer's tokens and time are
seen: reported as the memory errors are.

WHAT `acquire` DOES, in this order:

  1. on a DEGRADED card holds a job's acquire (`on_full`) until its card
     source says the card has room again, within the job's patience (S5b),
     and refuses any other (D3: `Degraded`, no model work until it has room
     again); then asks the LEASE before anything moves: a holder who
     does not hold the card cannot unload or load anything, and neither can a
     model nobody configured;
  2. hands back the model if it is up AND its process is still alive - the
     models beside it are not touched; a dead one is unloaded and loaded
     again, and the events file says so;
  3. holds the card through the pipeline - the open one's, or the queue's own
     opened for it - asking the card source only when no pipeline holds it;
  4. takes the LOAD LOCK - one load at a time - and looks again: a model that
     came up meanwhile is handed back, and one being stopped is waited out;
  5. asks again the stop of every model whose stop FAILED: while one still
     fails nothing loads (it may still be on the card) - looked at again as
     the load begins, since stops run beside loads;
  6. makes room - the runner's limit, then the card source's fit check - by
     waiting out a stop already under way, then taking off on-demand models
     not answering a request, the one used longest ago first; refuses a load
     nothing can make room for, before paying for it - and one whose room a
     stop that failed was making;
  7. takes the lease BEFORE the weights go up - a cold load takes minutes, and
     every one of them is time the card is spoken for;
  8. loads - holding the load lock alone - times it, and records the time and
     what else was up beside it - warning, in words a person at the machine
     can act on, when a load is far slower than its normal.

EVERY LOAD HAS AN IDENTITY: a `load-` actor id, minted here, on every line the
load writes. It is how an unload finds its load in the record, and how a
startup check tells a process this runner started from one that merely sits on
the same port.

THE LOADS ARE ONE TABLE (`LoadTable`), keyed by that id: the load whose
weights are going up and the loads on the card, each one entry from the moment
its id is minted until its model comes off. It holds ONE LOAD OF EACH MODEL,
and refuses a second.

WHERE IT WRITES is handed in (`data_dir`), never remembered: the bound folder's
`runner/`. `rebind` follows a new folder WITHOUT reloading a model, and every
load that is up goes with it on the record: the old folder is told each left,
the new one that each arrived. With nothing bound the manager writes nothing, and
keeps its last events in memory.

NOTHING HERE STARTS, STOPS OR KILLS A PROCESS IT DID NOT START, unless it is
asked to (`startup_check(reclaim=True)`), and even then only a process whose
command line proves it is one of this runner's own loads.
"""
from __future__ import annotations

import collections
import contextlib
import functools
import collections
import itertools
import json
import math
import os
import random
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import engines as _engines
from . import stats as _stats
from .base import Backend, BackendError, Cancelled
from .config import Config
from .mock import MockBackend, StandInCard

_CONTRACT = Path(__file__).resolve().parents[2] / "llm-bootstrap-contract"
if str(_CONTRACT) not in sys.path:
    sys.path.insert(0, str(_CONTRACT))
import ids                                                      # noqa: E402
import memory as MEM                                            # noqa: E402  (the one memory check)

EVENTS_NAME = "runner_events.jsonl"
LOADS_NAME = "model_loads.json"
#: the timing statistics, per model, in the bound folder's `runner/` (D12)
STATS_NAME = _stats.FILE
#: ...and the records they are summed from, a `timed` line each, beside it
STATS_RECORDS_NAME = "model_stats.jsonl"
#: the records kept in memory while nothing is bound, at most
TIMED_KEPT = 1000

#: WHAT THE STATISTICS MEASURE (PLAN-4 D12) - the contract's closed list
#: (`shapes/model_stats.json`), in its order. A load: its total, the card's
#: wait and claim when it took the card, the steps of its start, and the
#: card's memory it took - at ready and after its first answer; an answer: to
#: its first token and whole, its tokens and their pace, the prompt's pace,
#: its audio and real-time factor, and how many times its normal a far slower
#: one took; an unload: to its process gone and to the memory back; a job:
#: the queue's own share of it
MEASURES = ("load_s", "load_wait_s", "load_claim_s", "load_spawn_s", "load_port_s",
            "load_healthy_s", "load_identity_s", "ready_mib", "first_answer_mib",
            "first_token_s", "answer_s", "tokens", "tokens_per_s", "prompt_tokens_per_s",
            "audio_s", "real_time_factor", "slow_answer", "unload_s", "unload_memory_s",
            "queue_s")
#: the steps of a load's start an engine marks (`Backend.mark`), in order
LOAD_STEPS = ("spawn", "port", "healthy", "identity")
#: an unload's memory back: the card looked at this often once its process
#: has gone, for at most this long - its memory is back when the card has
#: the model's own measured memory free again, but for this slack (the
#: coordinator's settle slack)
MEMORY_POLL_S = 0.1
MEMORY_BACK_S = 5.0
MEMORY_SLACK_MIB = 256


class ConcurrencyViolation(RuntimeError):
    """Two models were about to be loaded at once. Should be unreachable."""


class LeaseHeld(RuntimeError):
    """The card is held by somebody else, and they have not let go."""


class NoCard(BackendError):
    """The card was not had, so nothing was loaded. `reason` is the request's
    own reason for ending, when the card source gave one (`card_patience`,
    `card_busy`)."""

    def __init__(self, message: str, reason=None):
        super().__init__(message)
        self.reason = reason


class WaitsForRoom(NoCard):
    """S18: scheduled work whose model does not fit, and which waits for room
    in its queue - a job's (its terms `parks`) - refused for now with the
    room it waits on (`room`: {"memory", "outside_mib", "words"}), so its
    queue keeps it, holding back nobody, and runs it once it may. Its reason
    is `no_room`: where nothing keeps it, it is what it ended for."""

    def __init__(self, message: str, room: dict):
        super().__init__(message, reason="no_room")
        self.room = dict(room)


def room_waited(verdict: dict, outside_mib) -> dict:
    """What scheduled work waits on while its model does not fit (S18): the
    numbers the memory check was made on, what programs outside the
    bootstrap hold, and the words a person reads while it waits."""
    numbers = {k: verdict.get(k) for k in ("need_mib", "free_mib", "margin_mib", "total_mib")}
    return {"memory": numbers, "outside_mib": outside_mib,
            "words": MEM.room_words({"verdict": "no_room", **numbers}, outside_mib)}


class Degraded(BackendError):
    """Model work refused, or ended, because the card is DEGRADED - over-full:
    its free memory under the coordinator's floor, or a memory error a
    model's log said (PLAN-4 D3). The rental is not ended for it: work fails,
    saying so, until the card has room again. `reason` is the job's own.

    S5b: `card` is what the card read when it was said - {free_mib, need_mib,
    outside_mib} as its source knew them, or {} - and `final` says the work
    was held for the card's memory already, for the whole of its patience:
    it is not held again."""

    reason = "card_degraded"

    def __init__(self, message: str = "", card=None, final: bool = False):
        super().__init__(message)
        self.card, self.final = dict(card or {}), bool(final)


#: S5b. A memory error this server's own model said is heard by its card
#: source a moment later - a coordinator on the heartbeat that goes at once,
#: the machine's lock at its watch's next look, which it wakes. Work held for
#: the card's memory waits this long, at most, for that word before it goes
#: on as if the card had room: a word that never comes never holds it longer
UNHEARD_S = 5.0


def lock_watch(cfg) -> dict:
    """The machine's lock's own watch, as the settings say it (S5b): how often
    it reads the card, how often while degraded, and its floor."""
    return {"watch_s": float(cfg.runner["card_watch_s"]),
            "recover_watch_s": float(cfg.runner["card_recover_watch_s"]),
            "floor_mib": int(cfg.runner["card_floor_mib"])}


def _stand_in_card(cfg):
    """The stand-in's own card (S16a R2), when its settings give it one - a
    stand-in runner with `[mock] card_mib` above 0 - else None."""
    if cfg.backend != "mock" or not int(cfg.mock.get("card_mib") or 0):
        return None
    return StandInCard(cfg.mock)


def full_words(free, need, outside) -> str:
    """What a person is told while work waits for the card's memory (S5b):
    how much is free, how much is needed, how much programs outside the
    bootstrap use - never which (D4) - and that it runs again by itself."""
    def gb(mib):
        return f"{mib / 1024:.1f} GB"
    if free is not None and need is not None:
        said = f"The card is full: {gb(free)} free, {gb(need)} needed."
    elif need is not None:
        said = f"The card is full: {gb(need)} needed."
    else:
        said = "The card is full."
    if outside:
        said += f" {gb(outside)} is used by programs outside the bootstrap."
    return said + " Close something, and this runs again by itself."


class PipelineRefused(Exception):
    """A pipeline was not opened, renewed or closed, for a reason of the
    contract's (`server.pipeline.*`): `status`, `reason`, and the words."""

    def __init__(self, status: int, reason: str, error: str):
        super().__init__(error)
        self.status, self.reason = int(status), str(reason)

    def answer(self) -> tuple:
        return self.status, {"error": str(self), "reason": self.reason}


class Terms:
    """A request's terms for the card, handed to the card source BY HAND with
    the take they are for - never left on a thread for it to find: a
    pipeline takes the card on a thread of its own. `klass` is the ticket's
    class (None: the source's default, responsive), `patience_s` how long the
    request waits (None: the server's own). `refused` is set when a take made
    with them was refused for a reason of the request's own. `waited_s` is
    how long the request has been held for the card's memory already, since
    the card first broke it (S5b): its patience counts from then. `label` is
    the pipeline's own, said on its ticket (R-B4), or None.

    S18, SCHEDULED WORK: `if_no_room` is what scheduled work whose model does
    not fit does - `wait` for room or `fail` at once; None, the server's own
    (`ModelManager.if_no_room` decides; live work always fails). `parks`:
    the request waits for room in its QUEUE, not here - a job's - so a take
    or a load that finds no room for it is refused with the room it waits
    on (`WaitsForRoom`), and nothing holds the card for it meanwhile.
    `on_room` hears the room a take waits on while it waits in place - a
    pipeline's, which says it as its why."""

    def __init__(self, klass=None, patience_s=None, waited_s=None, label=None,
                 if_no_room=None, parks=False, on_room=None):
        self.klass, self.patience_s, self.refused = klass, patience_s, None
        self.waited_s, self.label = waited_s, label
        self.if_no_room, self.parks, self.on_room = if_no_room, bool(parks), on_room

    def __repr__(self) -> str:
        return (f"Terms(klass={self.klass!r}, patience_s={self.patience_s!r}, "
                f"refused={self.refused!r}, waited_s={self.waited_s!r}, "
                f"if_no_room={self.if_no_room!r}, parks={self.parks!r})")


class Lease:
    """Who holds the card, since when, and for which model. In-process.

    AT MOST ONE HOLDER. A second is refused by `check`, which `acquire` asks
    BEFORE anything moves: `acquire` unloads what is on the card before loading
    another model, and the unload releases the lease - so a refusal placed only
    at the grant would let a second holder evict the first and then take the
    card cleanly.

    A HOLDER IS AN ACTOR ID, the same rule the coordinator keeps: a name can be
    shared, an actor cannot. A blank, a free-form name or a padded id is
    refused - a hold nobody can be asked about reads as compliance and means
    its opposite.
    """

    def __init__(self, clock=time.time):
        # its own lock, not the manager's: "who holds the card?" is asked by
        # `status()` while a load sits inside `acquire` for minutes
        self._lock = threading.RLock()
        self._clock = clock
        self._holder = self._since = self._model_key = None

    def check(self, holder) -> str:
        """Refuse *holder* if it is not an actor, or somebody else holds the
        card. Records nothing."""
        if not ids.matches("actor", holder):
            raise ValueError(f"a lease needs a holder: an actor id such as "
                             f"srv-k3x9m2p7, not {holder!r}")
        with self._lock:
            if self._holder is not None and self._holder != holder:
                raise LeaseHeld(f"the card is held by {self._holder}, taken "
                                f"{self._held_for():.0f}s ago for {self._model_key}; "
                                f"{holder} cannot take it")
        return holder

    def grant(self, holder, model_key=None) -> None:
        """Record that *holder* has the card. Asking again does not move
        `since`: a holder that asks once per generation has held the card
        since it first took it."""
        with self._lock:
            who = self.check(holder)
            if self._holder is None:
                self._since = self._clock()
            self._holder, self._model_key = who, model_key

    def release(self) -> bool:
        """Let the card go; True if anything was held. NO HOLDER ARGUMENT: the
        hold ends when the model comes off the card, and what takes it off -
        an unload, the idle sweep - is not the holder. A release that could
        refuse would raise inside `acquire`'s failure handler, where it would
        replace the load's own error."""
        with self._lock:
            held = self._holder is not None
            self._holder = self._since = self._model_key = None
            return held

    def public(self) -> dict:
        """None is not zero: an unheld card says nothing, never a holder
        called "" that has held it for 0.0 seconds. `held_seconds` is computed
        here because the reader's clock is not this machine's."""
        with self._lock:
            if self._holder is None:
                return {"holder": None, "since": None, "held_seconds": None,
                        "model_key": None}
            return {"holder": self._holder, "since": self._since,
                    "held_seconds": round(self._held_for(), 1),
                    "model_key": self._model_key}

    def _held_for(self) -> float:
        return 0.0 if self._since is None else max(0.0, self._clock() - self._since)


# ----------------------------------------------------------------- processes

#: WHERE A LOAD'S PORT COMES FROM WITH NO COORDINATOR: a range of the runner's
#: own, below every system's range for passing connections (32768 and up on
#: Linux, 49152 on Windows) and below every suite's. Never a port the
#: operating system picks: it may hand that one to any connection between the
#: pick and llama-server's bind, and the load would fail on a port nobody here
#: held. With no coordinator the card is the machine's lock, and loads run one
#: at a time under it, so no two of this project's picks can cross; a port
#: something else listens on is skipped.
QUIET_PORTS = (27000, 28499)
#: where the next pick starts: moved on by one each pick, from a random start
#: per process, so a port just given back - its connections still closing -
#: is the last to be picked again
_QUIET_NEXT = itertools.count(random.randrange(QUIET_PORTS[1] - QUIET_PORTS[0] + 1))
_QUIET_LOCK = threading.Lock()


def _bindable(host: str, port: int) -> bool:
    """Could a server listen on *port* now? Asked by binding it, alone -
    Windows lets a second socket share a port another holds with
    SO_REUSEADDR, and SO_EXCLUSIVEADDRUSE refuses that - and letting go."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if sys.platform == "win32":
            s.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", -5), 1)
        try:
            s.bind((host, int(port)))
        except OSError:
            return False
        return True


def free_port(host: str = "127.0.0.1", ports=QUIET_PORTS) -> int:
    """A port nothing is listening on, from *ports* (the runner's own range),
    bind-checked. Raises OSError when every one is in use."""
    lo, hi = int(ports[0]), int(ports[1])
    n = hi - lo + 1
    with _QUIET_LOCK:
        start = next(_QUIET_NEXT)
    for i in range(n):
        port = lo + (start + i) % n
        if _bindable(host, port):
            return port
    raise OSError(f"no free port in {lo}-{hi}: something listens on every one")


class FreePorts:
    """Where a load's port comes from when nothing hands ports out: the
    configured one, or a free one from the runner's own range for each load
    (`free_port`). A configured port serves one load at a time: the runner
    then keeps one model on the card at a time (`ModelManager._limit`).

    A PORT SOURCE is anything with `take(model_key, load) -> int` and
    `give_back(port)`. The manager takes one per load - never per reuse -
    naming the load it is for, and gives it back when the load comes off the
    card, or fails. A source backed by the coordinator's port registry fits
    the same two calls, and holds each load's port under that load's name."""

    def __init__(self, configured: int = 0):
        self.configured = int(configured or 0)

    def take(self, model_key: str, load=None) -> int:
        return self.configured if self.configured > 0 else free_port()

    def give_back(self, port: int) -> None:
        """Nothing to tell: a free port needs no giving back."""


class LocalCard:
    """Where the card comes from when nothing coordinates it: this process
    alone. There is nothing to ask and nothing to tell - the in-process lease
    is the whole rule, as it always was.

    A CARD SOURCE is anything with `take(holder, model_key, for_=None,
    cancel=None, on_wait=None, terms=None)`, `give_back()`, `lost` (the reason
    an answer gave when the card was taken back, else None) and `status()`.
    The manager takes the card when a pipeline opens - before any weights go
    up - and gives it back when the pipeline closes. `terms` are the request's
    (`Terms`), handed over only when there are some. A source backed by the
    coordinator's rentals fits the same calls (`coordination.CoordinatorCard`),
    holds its rental where the manager reads it (`_rental`), and may say the
    card is `wanted` (`on_wanted`)."""

    lost = None

    def take(self, holder, model_key, for_=None, cancel=None, on_wait=None, terms=None):
        return None

    def give_back(self) -> None:
        """Nothing to tell: nobody else was asked."""

    def status(self):
        return None


def process_info(pid) -> dict:
    """{"alive", "command_line"} for *pid*. Never raises.

    `alive` is None when it could not be asked, and a command line is None
    when it could not be read - neither ever reads as "ours"."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return {"alive": False, "command_line": None}
    if sys.platform == "win32":
        query = (f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'; "
                 f"if ($p) {{ 'ALIVE'; $p.CommandLine }} else {{ 'GONE' }}")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", query],
                                 capture_output=True, text=True, timeout=30,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:                                   # noqa: BLE001
            return {"alive": None, "command_line": None}
        lines = out.stdout.splitlines()
        if not lines or lines[0].strip() not in ("ALIVE", "GONE"):
            return {"alive": None, "command_line": None}
        if lines[0].strip() == "GONE":
            return {"alive": False, "command_line": None}
        line = "\n".join(lines[1:]).strip()
        return {"alive": True, "command_line": line or None}
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError:
        return {"alive": False, "command_line": None}
    except OSError:
        return {"alive": None, "command_line": None}
    line = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
    return {"alive": True, "command_line": line or None}


def kill_pid(pid) -> bool:
    """End *pid* by force. True if the operating system accepted it."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(["taskkill", "/F", "/PID", str(int(pid))],
                                 capture_output=True, timeout=30,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return out.returncode == 0
        os.kill(int(pid), 9)
        return True
    except Exception:                                       # noqa: BLE001
        return False


# Is a process this runner's? THE ENGINE'S OWN QUESTION (PLAN-4 D9): its
# program, the model's file, the port and the load's id, carried by that
# engine's proof - `engines.is_ours`, llama.cpp's unless an engine is named.
is_ours = _engines.is_ours


# ------------------------------------------------------------------ the loads

class _Load:
    """One load: from the moment its id is minted - once the card is had -
    until its model comes off. Under way until its weights are up; then on
    the card, until its stop begins (`stopping`) and returns."""

    __slots__ = ("load_id", "model_key", "backend", "port", "up", "loaded_at",
                 "load_seconds", "cold", "baseline", "slow", "pinned", "last_used",
                 "used", "stopping", "before", "mib", "answered", "asked_off")

    def __init__(self, load_id, model_key, backend, port):
        self.load_id, self.model_key, self.backend = load_id, model_key, backend
        self.port = port                                    # the port its source gave it
        self.up = False
        self.loaded_at = None
        self.load_seconds = 0.0
        #: its class and its normal, once `_note_load` has judged it
        self.cold = self.baseline = None
        self.slow = False
        #: a model its pipeline keeps: the idle sweep never takes it off, and
        #: no other load takes it off to make room
        self.pinned = False
        #: its own idle clock, on the manager's clock - and the order of its
        #: last use among the loads (`used`): a clock a row holds still cannot
        #: tell which of two loads was used last
        self.last_used = None
        self.used = 0
        #: its stop has begun: it is no model to hand out or to take off again
        self.stopping = False
        #: THE CARD'S MEMORY IT TOOK (PLAN-4 D12): the free MiB its fit check
        #: read before it, and the table's moves then - or None, where the
        #: card is not read; the most it was measured to take (`ready_mib`,
        #: then `first_answer_mib`), or None; and whether its first answer
        #: has been looked at
        self.before = None
        self.mib = None
        self.answered = False
        #: when its stop was asked for (monotonic), or None: an unload is
        #: timed from its asking
        self.asked_off = None


#: what is read where nothing is up: every field as it is before any load -
#: None, 0.0 and False, never a zero standing in for "nothing"
_NO_LOAD = _Load(None, None, None, None)


class LoadTable:
    """The runner's loads, keyed by load id: the one whose weights are going
    up, and the ones on the card - side by side.

    ONE LOAD OF EACH MODEL. A second entry for a model the table holds -
    under way, up, or being stopped - is refused (`ConcurrencyViolation`)
    whoever asks: two loads of one model would be two processes answering for
    it. `acquire` never asks for one - it looks again under the load lock, and
    waits out a stop - so this is the rule itself, not the first line of
    defence.

    Beside the entries, what is no one load's:
      live                the models up, by the manager's own count: every
                          line of the record carries it (`live_after`)
      loads_this_session  every load that came up in this process
      last_used           the card's idle clock: the last use of any model;
                          it outlives the load it last timed, as health's
                          `last_used` always did. Each load has its own too
      in_use              generations between `acquire` and their end, on
                          whatever model - and how many hold each model
                          (`uses`): a generation holds its model from before
                          its load exists
      waits for the card  one per wait under way, each naming the model it
                          waits for (None: a pipeline's own wait): a load has
                          no id until the card is had, so it is no entry yet.
                          `card_waiting` names the first; each wait counts
                          until its own has ended

    READ WITHOUT THE MANAGER'S LOCK - `status()` is asked while a load is under
    way - so the entries are replaced whole, never edited in place: a reader
    sees one table or the next, never half of one."""

    def __init__(self):
        self._by_id: dict = {}
        self.live = 0
        self.loads_this_session = 0
        self.last_used = None
        self.in_use = 0
        #: generations by model: replaced whole, under the manager's lock
        self._uses: dict = {}
        self._used = 0
        #: the waits for the card under way, replaced whole under their own
        #: lock: they begin and end on the waiting threads, outside the
        #: manager's lock
        self._waiting: tuple = ()
        self._waiting_lock = threading.Lock()
        #: HOW OFTEN THE CARD'S LOADS HAVE MOVED - a load begun, come up, its
        #: stop begun, ended - under the manager's lock: a measure of the
        #: card's memory holds only when nothing but its own load moved
        #: between its two looks at the card (PLAN-4 D12)
        self.moves = 0

    @property
    def card_waiting(self):
        """The model the first wait for the card under way is for, or None."""
        waiting = self._waiting
        return waiting[0] if waiting else None

    @property
    def waiting(self) -> int:
        """How many waits for the card are under way."""
        return len(self._waiting)

    def wait_begins(self, model_key) -> None:
        with self._waiting_lock:
            self._waiting = self._waiting + (model_key,)

    def wait_ends(self, model_key) -> None:
        """One wait for *model_key* has ended - its own, and no other."""
        with self._waiting_lock:
            left = list(self._waiting)
            if model_key in left:
                left.remove(model_key)
            self._waiting = tuple(left)

    def __len__(self) -> int:
        return len(self._by_id)

    def ids(self) -> tuple:
        return tuple(self._by_id)

    def entries(self) -> tuple:
        """Every load, in the order they began."""
        return tuple(self._by_id.values())

    def up(self) -> tuple:
        """The loads on the card that can be handed out: up, and no stop
        begun - in the order they began."""
        return tuple(e for e in self._by_id.values() if e.up and not e.stopping)

    def on_card(self) -> tuple:
        """Every load up, a stop under way included: what the record holds
        open."""
        return tuple(e for e in self._by_id.values() if e.up)

    def of(self, model_key):
        """The load of *model_key* - under way, up or being stopped - or None."""
        return next((e for e in self._by_id.values() if e.model_key == model_key), None)

    def loaded(self):
        """Of the loads on the card - one being stopped included: its process
        is still there - the one used last, or None. What health shows;
        never what is handed out (`of`, and its `stopping`, say that)."""
        up = self.on_card()
        return max(up, key=lambda e: e.used) if up else None

    def loading(self):
        """The load whose weights are going up, or None: one at a time."""
        return next((e for e in self._by_id.values() if not e.up), None)

    def uses(self, model_key) -> int:
        """How many generations hold *model_key* now."""
        return self._uses.get(model_key, 0)

    def use_begins(self, model_key) -> None:
        self.in_use += 1
        if model_key is not None:
            self._uses = {**self._uses, model_key: self._uses.get(model_key, 0) + 1}

    def use_ends(self, model_key, at) -> None:
        self.in_use = max(0, self.in_use - 1)
        self.last_used = at
        if model_key is not None:
            left = self._uses.get(model_key, 0) - 1
            self._uses = ({**self._uses, model_key: left} if left > 0 else
                          {k: v for k, v in self._uses.items() if k != model_key})
            entry = self.of(model_key)
            if entry is not None and entry.up:
                self.used_now(entry, at)

    def used_now(self, entry: _Load, at) -> None:
        """*entry* was just used: its idle clock starts again, and it is the
        one used last."""
        self._used += 1
        entry.used, entry.last_used = self._used, at
        self.last_used = at

    def begin(self, load_id: str, model_key: str, backend, port) -> _Load:
        """A load under way: its id minted, its backend made, its weights about
        to go up. Refused while the table holds a load of the same model."""
        held = self.of(model_key)
        if held is not None:
            raise ConcurrencyViolation(f"refusing to load {model_key}: the table holds "
                                       f"{held.load_id} of it already, and holds one load of "
                                       f"each model")
        entry = _Load(load_id, model_key, backend, port)
        self._by_id = {**self._by_id, load_id: entry}
        self.moves += 1
        return entry

    def came_up(self, entry: _Load, seconds: float, at: float) -> None:
        """Its weights are up: the same entry is now on the card."""
        entry.load_seconds, entry.loaded_at = seconds, at
        entry.up = True
        self.moves += 1
        self.live += 1
        self.loads_this_session += 1
        self.used_now(entry, at)

    def end(self, entry: _Load) -> None:
        """Its model came off, or never went up: out of the table - and out of
        the live count, if it had come up. Ending it twice changes nothing."""
        if self._by_id.get(entry.load_id) is not entry:
            return
        self._by_id = {k: e for k, e in self._by_id.items() if k != entry.load_id}
        self.moves += 1
        if entry.up:
            self.live = max(0, self.live - 1)


# --------------------------------------------------------------- the pipeline

#: who opened a pipeline, in the record's words and health's
OPENERS = {"application": "the application", "start": "the server as it started",
           "queue": "the queue, for its own work"}

#: health's pipeline when none was ever opened: every field but the state and
#: the models is null - never a pipeline called "" held for 0.0 seconds
NO_PIPELINE = {"id": None, "state": "closed", "why": None, "since": None, "label": None,
               "models": [], "up": [], "class": None, "keep_s": None, "opened_by": None}


class _Pipeline:
    """One pipeline: the card held from its open to its close. Its fields are
    written under the manager's pipeline lock, and read under it whole.

      state       waiting (for the card), holding it, degraded (held, and the
                  card over-full: the coordinator's word, heard on a
                  heartbeat's answer - no model work until it clears), lost
                  (the card was taken back), closed
      open        until it is closed - the queue's own also ends when it is
                  lost, since the queue's next work opens another
      closing     a close is under way: nothing more is loaded for it
      stop        set by a close: its own wait for the card ends, and a load
                  of its own is given up
      renewed_at  on the manager's clock: a pipeline with a keep_s lapses once
                  it has not been renewed for that long
      last_model  the model its last load was for: named when the card goes
                  back
      models      the models it keeps (pinned), in the order they were named
                  - at its open, or by `load_model` since
      failed      why each of its models last failed to load, by model: what
                  a wait for its models reads as "failed", until a load of it
                  begins again"""

    __slots__ = ("id", "opened_by", "holder", "klass", "label", "models", "keep_s", "state",
                 "why", "since", "renewed_at", "open", "closing", "stop", "thread",
                 "last_model", "failed", "if_no_room")

    def __init__(self, pid, opened_by, holder, klass, label, models, keep_s, now,
                 if_no_room=None):
        self.id, self.opened_by, self.holder = pid, opened_by, holder
        self.klass, self.label, self.models, self.keep_s = klass, label, tuple(models), keep_s
        #: S18: what it does when its first model does not fit, as its open
        #: said - None, the server's own
        self.if_no_room = if_no_room
        self.state, self.why, self.since = "waiting", None, now
        self.renewed_at = now
        self.open, self.closing = True, False
        self.stop = threading.Event()
        self.thread = None
        self.last_model = self.models[0] if self.models else None
        self.failed: dict = {}

    def public(self, up=()) -> dict:
        """Its answer; *up* is every model on the card and answering, while
        it is open."""
        return {"id": self.id, "state": self.state, "why": self.why, "since": self.since,
                "label": self.label, "models": list(self.models),
                "up": list(up) if self.open else [], "class": self.klass,
                "keep_s": self.keep_s, "opened_by": self.opened_by}

    def opened(self) -> dict:
        """Its record line's fields, as it opened."""
        return {"pipeline": self.id, "opened_by": self.opened_by, "class": self.klass,
                "label": self.label, "models": list(self.models), "keep_s": self.keep_s}


# ------------------------------------------------------------------ manager

class _Either:
    """Set when any of its events is: a pinned load's own end - its model
    unloaded - beside its pipeline's close. Read as an event is read."""

    def __init__(self, *events):
        self.events = events

    def is_set(self) -> bool:
        return any(e.is_set() for e in self.events)

    def wait(self, timeout=None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while not self.is_set():
            left = 0.05 if deadline is None else min(0.05, deadline - time.monotonic())
            if left <= 0:
                return False
            self.events[0].wait(left)
        return True


class _LoadWatch:
    """A load's watch on its caller's cancel, while the weights go up.

    THE RULE: a load whose caller is cancelled is given up at once - its
    process ended, and the card, the lease and the port given back as for any
    failed load - UNLESS `keep_loading()` says the job that runs next wants the
    same model. Then the load goes on for that job, and the question is asked
    again every half second while it does: a next job cancelled meanwhile
    gives the load up too. A question that cannot be answered keeps nothing.

    It gives up ITS OWN backend, never "the load under way": a watch that woke
    a moment late must not give up the next job's load.

    Nothing here takes the manager's lock, nor the load lock, which the load
    holds for as long as the weights take to go up: the record is written by
    `acquire`, from what the watch saw."""

    KEEP_ASKING_S = 0.5

    def __init__(self, backend, model_key, cancel, keep_loading):
        self.backend, self.model_key = backend, model_key
        self.cancel, self._keep = cancel, keep_loading
        #: the words the load was given up in, once it was
        self.given_up: str | None = None
        self._done = threading.Event()
        threading.Thread(target=self._watch, daemon=True,
                         name=f"load-cancel-{model_key}").start()

    def keep(self) -> bool:
        """Does the job that runs next want this model?"""
        if self._keep is None:
            return False
        try:
            return bool(self._keep())
        except Exception:                                   # noqa: BLE001
            return False

    def _watch(self) -> None:
        while not self._done.is_set():
            if not self.cancel.wait(0.05):
                continue
            if self._done.is_set():
                return
            if not self.keep():
                self.given_up = (f"its job was cancelled while {self.model_key} loaded, and "
                                 f"the job that runs next does not want it")
                give_up = getattr(self.backend, "abandon", None)
                if give_up is not None:
                    give_up(self.given_up)
                return
            if self._done.wait(self.KEEP_ASKING_S):
                return

    def end(self) -> None:
        self._done.set()


class ModelManager:
    #: how many times a load takes the card again when the pipeline holding it
    #: closed while the load waited for it - a close and a load crossing
    RETAKES = 3
    #: how long a close waits for its pipeline's own thread, once it was told
    #: to stop: a wait for the card ends at once, a load is given up
    CLOSE_JOIN_S = 15.0

    def __init__(self, cfg: Config, data_dir=None, *, backend_factory=None,
                 clock=time.time, processes=None, kill=None, vram=None, ports=None,
                 card=None, read_card=False):
        self.cfg = cfg
        self.backend_kind = cfg.backend
        self._clock = clock
        self._lock = threading.RLock()
        #: the loads - the one going up, the ones on the card - and what they
        #: share, in one table (see LoadTable): one load of each model
        self._loads = LoadTable()
        #: ONE LOAD AT A TIME: held for the whole of a load - its making room,
        #: its weights going up - and never the manager's lock with it, so
        #: nothing that asks the manager waits for a load. Stops never take it
        self._load_lock = threading.Lock()
        #: the loads whose stop FAILED: off the table - their model may never
        #: be used again - but their process may still be on the card. Each
        #: stays named to the card source, and its stop is asked again before
        #: anything else loads and when a pipeline closes: nothing goes up
        #: while one will not go. Asked again one caller at a time
        self._stuck: list = []
        self._stuck_lock = threading.Lock()
        #: told whenever a model comes up, fails to load or comes off, and
        #: whenever the pipeline moves: what a wait for a model reads. Taken
        #: with no other lock held, and its waiters read without one
        self._news = threading.Condition(threading.Lock())
        #: the pipeline - open, or the last one to close - or None. Its own
        #: lock, taken last and never held across a wait, a write of the
        #: record or the manager's lock: health reads it while a load is
        #: under way for minutes
        self._pipe: _Pipeline | None = None
        self._pipe_lock = threading.Lock()
        #: one close at a time - taken BEFORE the manager's lock, never while
        #: holding it (`settle` only tries it)
        self._close_lock = threading.RLock()
        #: the acquires under way: work, for the queue's own pipeline
        self._acquiring = 0
        #: generations that named no model (`in_use()`): each holds every
        #: model against the idle sweep and against making room
        self._anon_uses = 0
        #: why each model's last load failed, by model - replaced whole - until
        #: a load of it begins again: what a wait for a model reads as failed
        self._failed: dict = {}
        #: the starts of a whisper model that launched its server and failed,
        #: by model: (when, what it said) - kept here, since they outlive the
        #: load that failed, and read by its engine, which does not start one
        #: again within the model's `retry_after_s` (PLAN-4 D11)
        self._starts: dict = {}
        #: by model, what ends its pinned loads not yet up: set by its unload
        self._pin_cancels: dict = {}
        #: does the queue still owe work? Set by whoever runs the queue (the
        #: adapter asks the server); None: nothing to ask, so nothing is owed.
        #: A question that cannot be answered owes nothing
        self.work_pending = None
        #: why nothing more is loaded, once the server has begun to stop
        self._stopping: str | None = None
        #: the model keys this PROCESS has loaded once - the whole of the
        #: cold/warm decision. `unload` does not clear it: a model on the card
        #: ten minutes ago was read off this disk ten minutes ago.
        self._loaded_keys: set = set()
        #: read-speed measures: one at a time; the last per model; and the
        #: stretches this process has read plainly - a repeat is the cache
        self._measuring = threading.Lock()
        self._measures: dict = {}
        self._read_before: set = set()
        #: each model file's header, read once while the file is unchanged
        self._headers: dict = {}
        self._lease = Lease(clock)
        self._recent = collections.deque(maxlen=64)
        self._factory = backend_factory or self._make
        # looked up when the manager is built, not when the module is: a suite
        # that puts a tripwire on `kill_pid` gets it
        self._processes = processes or process_info
        self._kill = kill or kill_pid
        #: the card's memory - {"free", "total"} in MiB, or the free MiB alone,
        #: or None when it cannot be asked
        #: THE STAND-IN'S CARD (S16a R2) - the stand-in's settings give it one
        #: (`[mock] card_mib`) - read in place of any other, never the real card
        stand_in = _stand_in_card(cfg) if card is None else None
        self._vram = stand_in.read if stand_in is not None else (vram or self.card_memory_mib)
        #: MAY THE STATISTICS LOOK AT THE CARD (PLAN-4 D12)? Only where this
        #: manager was handed a way to read it (`vram`, a suite's stand-in; the
        #: stand-in's own card) or told the card is real (`read_card`, a
        #: server's start): never nvidia-smi from a suite whose card source
        #: merely says numbers
        self._reads_card = vram is not None or bool(read_card) or stand_in is not None
        #: where a real load's port comes from (see FreePorts)
        self._ports = ports or FreePorts(cfg.runner.get("port") or 0)
        #: may this process have the card at all (see LocalCard) - asked before
        #: a load; the table's `card_waiting` names the model while the answer
        #: is "wait". A real card with nothing coordinating it is the machine's
        #: lock: two servers on one machine must not both pass the free-memory
        #: check
        self._card = card or self._default_card(cfg, stand_in is not None)
        #: THE WORK, as a coordinator's watch reads it (PLAN-4 D3), under a
        #: lock of its own - never the manager's: the heartbeat's thread asks.
        #: The answers under way, each -> the monotonic time a one-piece
        #: answer is due by, or None; the tokens streamed and answers ended by
        #: this process; the memory errors its models' logs said, and the
        #: newest
        self._work_lock = threading.Lock()
        self._answers: dict = {}
        #: S17: each answer under way -> [its model, every model that answered
        #: beside it at any moment of it], for its timed line (`beside`)
        self._beside: dict = {}
        self._progress = 0
        self._memory_errors, self._memory_error = 0, None
        #: the card's word that the rental is DEGRADED - (the rental's id, or
        #: the lock's session, the words, the numbers) - or None; and what ends
        #: the answers under way when it comes
        self._degraded = None
        self._degraded_now = threading.Event()
        #: S5b: when a memory error this server's model said was last noted,
        #: while its card source had not yet said the card degraded (monotonic)
        self._unheard = None
        #: the answers far slower than their model's normal (D3, D12), counted
        #: under the work lock with the newest in words: said on each heartbeat
        self._slow_answers, self._slow_answer = 0, None
        #: THE STATISTICS (D12): one `timed` line and its fold at a time, so
        #: the summary is the fold of the lines in their order; the take of
        #: the card each model's next load carries (its wait and its claim);
        #: and the folders whose summary could not be read, said once each
        self._stats_lock = threading.Lock()
        self._took: dict = {}
        #: model -> (the free MiB its last fit check read, the table's moves
        #: then), until its load begins and carries it (`_Load.before`)
        self._fit_read: dict = {}
        self._stats_unreadable: set = set()
        #: the statistics' records while nothing is bound - the newest only
        self._timed_lines = collections.deque(maxlen=TIMED_KEPT)
        if getattr(self._card, "on_lost", False) is None:
            self._card.on_lost = self._card_lost
        if getattr(self._card, "on_wanted", False) is None:
            self._card.on_wanted = self._heard_wanted
        if getattr(self._card, "work", False) is None:
            self._card.work = self.work
        if getattr(self._card, "on_degraded", False) is None:
            self._card.on_degraded = self._heard_degraded
        if self._reads_card and getattr(self._card, "read", False) is None:
            # the machine's lock watches the card itself (S5b) - read this
            # runner's own way, and only when this manager reads the card
            self._card.read = self._vram
        if getattr(self._card, "needs", False) is None and hasattr(self._card, "margin_mib"):
            # S18: the machine's lock judges a scheduled pipeline's first model
            # before it takes the lock - by this runner's own estimate, and
            # the margin its settings keep free
            self._card.needs = lambda key: self.need(key)[0]
            self._card.margin_mib = int(cfg.runner["card_margin_mib"])
        if stand_in is not None:
            # S18: the stand-in's card holds what the stand-in's models on it
            # need (`[mock] need_mib`) - its own use, never counted as outside
            stand_in.held = self._stand_in_held
        if getattr(self._ports, "on_lost", False) is None:
            self._ports.on_lost = self._port_lost
        #: where the server's log is - a line - or None: what a person must
        #: hear at once is said there (`_say_no_room`), beside the record
        self.on_say = None
        self.data_dir = Path(data_dir) if data_dir else None
        if self.data_dir:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        # A process boundary: whatever the record says was loaded went with the
        # process that loaded it - or was left running, an orphan.
        self._event("session_start", pid=os.getpid())

    def _default_card(self, cfg, stand_in: bool = False):
        if self.backend_kind == "mock" and not stand_in:
            return LocalCard()                              # a stand-in touches no card
        # a real card - or the stand-in's own (S16a R2), watched as a real one
        # is, on the lock of its own its settings name (never the machine's:
        # the settings refuse it)
        # a real card, whatever each model's engine: two servers on one
        # machine must not both pass the free-memory check
        from .cardlock import MachineCard
        # the settings' own patience, 0 included: 0 is now or not at all, and
        # was once read as "none given", and waited ten minutes (S2 R1)
        return MachineCard(cfg.runner.get("card_lock") or None,
                           patience_s=float(cfg.runner["card_patience_s"]),
                           **lock_watch(cfg))

    # ------------------------------------- the names of the slots the table replaced
    #
    # Rows written before the table read these three, and one sets the count:
    # kept, so that every such row runs exactly as it was written.

    @property
    def _backend(self):
        """The backend of the load on the card used last, or None."""
        return (self._loads.loaded() or _NO_LOAD).backend

    @property
    def _loading_backend(self):
        """The backend of the load whose weights are going up, or None."""
        return (self._loads.loading() or _NO_LOAD).backend

    @property
    def _live(self) -> int:
        """The table's count of the models up."""
        return self._loads.live

    @_live.setter
    def _live(self, n: int) -> None:
        self._loads.live = n

    @property
    def _kept(self) -> bool:
        """Is the card kept for a model whose stop failed? Kept while any may
        still be on it: until its stop, asked again, works - or the server
        stops."""
        return bool(self._stuck)

    # ------------------------------------------------------------ the record

    @property
    def events_path(self):
        return self.data_dir / EVENTS_NAME if self.data_dir else None

    @property
    def loads_path(self):
        return self.data_dir / LOADS_NAME if self.data_dir else None

    @property
    def stats_path(self):
        return self.data_dir / STATS_NAME if self.data_dir else None

    @property
    def logs_dir(self):
        return self.data_dir / "logs" if self.data_dir else None

    def rebind(self, data_dir) -> None:
        """Follow a new folder, or none. NO MODEL IS RELOADED: a model already
        up keeps the log file it was started with, and the next load follows.
        EVERY load on the card moves with it on the record - one being stopped
        included, whose unload the new folder will hear, and one whose stop
        failed, which may still be on the card - the old folder's file says
        each left (so no later reader calls it an orphan there), and the new
        folder's says each arrived (so its unload pairs with something, and a
        server that dies with it still there leaves its orphan where it was
        bound). LETTING THE FOLDER GO - no folder after it, the last thing a
        server does as it stops - leaves a load whose stop failed open in it:
        that record is the only one the next start there can find it by.
        Never waits for a load: the manager's lock is never held across
        one."""
        with self._lock:
            new = Path(data_dir) if data_dir else None
            if new == self.data_dir:
                return
            if new is not None:
                new.mkdir(parents=True, exist_ok=True)       # nothing moves until this worked
            up = self._loads.on_card() + (tuple(self._stuck) if new is not None else ())
            for entry in up:
                self._event("rebound_away", **self._load_fields(entry))
            self.data_dir = new
            self._event("session_start", pid=os.getpid(), rebound=True)
            for entry in up:
                self._event("load_carried", **self._load_fields(entry))

    def _load_fields(self, entry: _Load) -> dict:
        """What a load's lines say of it. A real load's lines name its engine
        and its model file (`file`, whatever the engine): what a startup check
        reads its process against. A llama.cpp load's say the file as `gguf`
        too - the name the field had before engines were named, when every
        model was one - so a reader of that field still finds it; a whisper
        load's file is not a GGUF, and is never called one. A stand-in load's
        lines name no engine and no file (`gguf` null, as ever)."""
        b = entry.backend
        fields = {"model_key": entry.model_key, "load": entry.load_id,
                  "pid": getattr(b, "pid", None), "port": getattr(b, "port", None)}
        if self.backend_kind == "mock" or entry.model_key is None:
            return {**fields, "gguf": None}
        engine = self.cfg.engine(entry.model_key)
        path = self._model_file(entry.model_key)
        return {**fields, **({"gguf": path} if engine == "llamacpp" else {}), "file": path,
                "engine": engine}

    def _model_file(self, model_key) -> str:
        """The model file of a real load, whatever its engine."""
        return str(self.cfg.model(model_key).gguf_path(self.cfg.models_dir))

    def _say_no_room(self, said: str) -> None:
        """FAIL LOUDLY (H9; the owner, 2026-09-28: "Memory almost never ever
        cleans itself up"): a model that does not fit on a card that is not
        over-full is refused at once, and the server's log says it - in the
        card source's words: the numbers, what programs outside the bootstrap
        hold, and what to do. A word that cannot be said never fails the
        refusal."""
        say = self.on_say
        if say is None:
            return
        try:
            say(f"runner: {said}")
        except Exception:                                   # noqa: BLE001 - a word never fails
            pass

    def _event(self, event: str, **fields) -> dict:
        with self._lock:
            record = {"ts": self._clock(), "event": event, "backend": self.backend_kind,
                      "live_after": self._loads.live, **fields}
            self._recent.append(record)
            path = self.events_path
            if path is not None:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            return record

    # ------------------------------------------------ the statistics (D12)

    def _timed(self, model_key, what: str, measures: dict, **fields):
        """What was measured of one thing - a load, an answer, an unload, the
        card's memory, a job's share: `{measure: value}` - kept. ONE LINE of
        the statistics' own record (`runner/model_stats.jsonl`, a `timed`
        line), which is what the statistics are summed from, and the same
        values folded, in the same order, into the model's statistics in the
        bound folder (`runner/model_stats.json`): the line and its fold under
        one lock, so the summary is always the fold of the lines in the order
        they were written (`stat_records`). Their own file, beside the
        runner's record: a line per answer would bury the loads and unloads
        that record is read for. With nothing bound, the line is kept in
        memory and nothing is folded. A summary that cannot be read is never
        written over: the line stays, and the runner's record says so, once.
        Never fails what it measures. Never called under the manager's lock.
        -> the line, or None: nothing was measured."""
        clean = {k: v for k, v in measures.items()
                 if isinstance(v, (int, float)) and not isinstance(v, bool)
                 and math.isfinite(v)}
        if not clean or model_key is None:
            return None
        with self._stats_lock:
            with self._lock:
                line = {"ts": self._clock(), "event": "timed", "model_key": model_key,
                        "what": what, "measures": clean, **fields}
                folder = self.data_dir
            if folder is None:
                self._timed_lines.append(line)
                return line
            try:
                folder.mkdir(parents=True, exist_ok=True)
                with open(folder / STATS_RECORDS_NAME, "a", encoding="utf-8",
                          newline="\n") as fh:
                    fh.write(json.dumps(line, ensure_ascii=False) + "\n")
            except Exception as exc:                        # noqa: BLE001 - said, never fails
                self._event("stats_failed", model_key=model_key, detail=str(exc)[:500])
                return line
            try:
                _stats.ModelStats(folder / STATS_NAME).note_many(model_key, clean, at=line["ts"],
                                                                 beside=_beside_count(line))
            except _stats.StatsUnreadable as exc:
                if str(folder) not in self._stats_unreadable:
                    self._stats_unreadable.add(str(folder))
                    self._event("stats_unreadable", detail=str(exc)[:500])
                    print(f"[runner] {exc}: its statistics are not kept until it is set right, "
                          f"and their records are, in {STATS_RECORDS_NAME}", flush=True)
            except Exception as exc:                        # noqa: BLE001 - said, never fails
                self._event("stats_failed", model_key=model_key, detail=str(exc)[:500])
        return line

    def kept_stats(self):
        """Every model's statistics as kept in the bound folder - or None:
        nothing bound, or a summary that cannot be read. Public: the
        adapter reads a held request's expected time from it (S15 RS3)."""
        folder = self.data_dir
        if folder is None:
            return None
        try:
            return _stats.ModelStats(folder / STATS_NAME).read()["models"]
        except Exception:                                   # noqa: BLE001 - read as none
            return None

    def _free_mib(self):
        """The card's free memory, in MiB - one look, through this manager's
        own way of reading it - or None: it cannot be read, or the statistics
        may not look at it (`_reads_card`). A look that failed is said on the
        record, and is no answer."""
        if not self._reads_card:
            return None
        try:
            got = self._vram()
        except Exception as exc:                            # noqa: BLE001 - said, never fails
            self._event("card_read_failed", detail=str(exc)[:300])
            return None
        free = got.get("free") if isinstance(got, dict) else got
        if isinstance(free, bool) or not isinstance(free, (int, float)) \
                or not math.isfinite(free):
            return None
        return int(free)

    def note_job(self, model_key, seconds) -> None:
        """A job ran on *model_key*, and the queue's own share of it - its
        time outside the model - was *seconds* (`queue_s`): kept."""
        self._timed(model_key, "job", {"queue_s": seconds})

    def need(self, model_key) -> tuple:
        """What *model_key* needs on the card, as `needs.need_mib` reckons it
        - measured, once its statistics hold three measured loads (its memory
        after its first answer), else the runner's estimate for its engine.
        -> (MiB or None, how, hint)."""
        from . import needs
        kept = self.kept_stats() or {}
        measured = kept.get(model_key, {}).get("first_answer_mib")
        return needs.need_mib(self.cfg, model_key, self.cfg.engine(model_key), measured=measured)

    def _load_timed(self, entry: _Load, asked, started, ready, took) -> None:
        """A load came up: its start timed (D12) - the card's wait and claim,
        when this load took the card (`took`, or the take its pipeline made
        for it); each step its engine marked, from the step before it, the
        first from its start; and its total, from its asking (or its take's,
        when that began first) to ready - and then the card's memory it took
        (`_memory_measured`). Never fails the load."""
        try:
            with self._lock:
                carried = self._took.pop(entry.model_key, None)
            took = took or carried
            got = {}
            if took:
                got.update({k: took[k] for k in ("load_wait_s", "load_claim_s") if k in took})
                asked = min(asked, took["started"])
            before, steps = started, dict(getattr(entry.backend, "steps", None) or {})
            for step in LOAD_STEPS:
                at = steps.get(step)
                if at is not None:
                    got[f"load_{step}_s"] = at - before
                    before = at
            got["load_s"] = ready - asked
            self._timed(entry.model_key, "load", got, load=entry.load_id)
            self._memory_measured(entry, "ready_mib")
        except Exception as exc:                            # noqa: BLE001 - said, never fails
            self._event("stats_failed", model_key=entry.model_key, detail=str(exc)[:500])

    def _memory_measured(self, entry: _Load, measure: str) -> None:
        """The card's memory *entry* took, looked at now (`ready_mib`, or
        `first_answer_mib`): the free memory its fit check read before it,
        less the free memory now. A measure only when the card was read both
        times, when nothing but its own load moved in between - begun and
        come up, and nothing since - and when it took some. Kept, and said
        on the heartbeat as its process's `mib`."""
        if entry.before is None:
            return
        free = self._free_mib()
        if free is None:
            return
        was, moves = entry.before
        with self._lock:
            if self._loads.moves != moves + 2 or entry.stopping \
                    or self._loads.of(entry.model_key) is not entry:
                return
            mib = int(round(was - free))
            if mib <= 0:
                return
            entry.mib = mib
            # told under the lock: its stop cannot begin, and be told, between
            pid = getattr(entry.backend, "pid", None)
            if pid is not None:
                self._card_running(pid, entry.load_id, entry.model_key, entry.port, mib=mib)
        self._timed(entry.model_key, "memory", {measure: mib}, load=entry.load_id)

    def _memory_back(self, entry: _Load, ref) -> None:
        """On its own thread, once *entry*'s process has gone: the card looked
        at every `MEMORY_POLL_S` until the memory it was measured to take is
        free again - over the look before its stop (*ref*), but for the settle
        slack - and the time from the stop's asking kept (`unload_memory_s`).
        No measure when the card cannot be read, when anything else came or
        went meanwhile, or when it is not back within `MEMORY_BACK_S`."""
        try:
            free0, moves = ref
            want = free0 + entry.mib - MEMORY_SLACK_MIB
            deadline = time.monotonic() + MEMORY_BACK_S
            while True:
                free = self._free_mib()
                now = time.monotonic()
                with self._lock:
                    moved = self._loads.moves != moves + 1
                if free is None or moved:
                    return
                if free >= want:
                    self._timed(entry.model_key, "memory",
                                {"unload_memory_s": now - entry.asked_off}, load=entry.load_id)
                    return
                if now >= deadline:
                    return
                time.sleep(MEMORY_POLL_S)
        except Exception as exc:                            # noqa: BLE001 - said, never fails
            try:
                self._event("stats_failed", model_key=entry.model_key, detail=str(exc)[:500])
            except Exception:                               # noqa: BLE001 - its folder has gone
                pass

    def _answer_timed(self, backend, asked, first, done, done_at, pieces, beside=None) -> None:
        """An answer ended: timed (D12) - to its first token (for an answer in
        one piece, the whole answer), whole, its tokens (the model's count,
        else the pieces streamed), their pace once it began, the prompt's
        pace (its tokens over the time to the first), and for audio its
        seconds and the real-time factor - and judged against its model's
        normal (`answer_slowness`, D3): one far slower is flagged. A model's
        first answer on a load has the card's memory looked at after it, on a
        thread of its own. Never fails the answer."""
        model_key = getattr(backend, "model_key", None)
        try:
            end = time.monotonic() if done_at is None else done_at
            done = done if isinstance(done, dict) else {}
            usage = done.get("usage") if isinstance(done.get("usage"), dict) else {}
            once = getattr(backend, "answers", "stream") == "once"
            got = {}
            if once:
                got["first_token_s"] = end - asked
            elif first is not None:
                got["first_token_s"] = first - asked
            got["answer_s"] = end - asked
            tokens = usage.get("completion_tokens", done.get("tokens"))
            if not _stats._number(tokens):
                tokens = pieces
            got["tokens"] = int(tokens)
            if once:
                audio = done.get("audio_s")
                if _stats._number(audio) and audio > 0:
                    got["audio_s"] = float(audio)
                    got["real_time_factor"] = got["answer_s"] / float(audio)
            else:
                if first is not None and tokens >= 2 and end > first:
                    got["tokens_per_s"] = (tokens - 1) / (end - first)
                prompt = usage.get("prompt_tokens")
                if first is not None and _stats._number(prompt) and prompt > 0 and first > asked:
                    got["prompt_tokens_per_s"] = prompt / (first - asked)
            pace = "real_time_factor" if once else "tokens_per_s"
            normal = self._normal(model_key, pace)
            times = answer_slowness(got, normal, float(self.cfg.runner["slow_answer_factor"]),
                                    float(self.cfg.runner["slow_answer_floor_s"]))
            if times is not None:
                got["slow_answer"] = times
            # S17: the models that answered beside it, so a lone answer is
            # told from a shared one (`beside`; [] alone)
            self._timed(model_key, "answer", got, load=getattr(backend, "load_id", None),
                        **({} if beside is None else {"beside": list(beside)}))
            if times is not None:
                self._slow_heard(backend, model_key, times, got, normal)
            self._after_first_answer(backend)
        except Exception as exc:                            # noqa: BLE001 - said, never fails
            self._event("stats_failed", model_key=model_key, detail=str(exc)[:500])

    def _normal(self, model_key, measure):
        """*model_key*'s normal for *measure*: the median of its kept samples,
        with three of them at least - else None, no normal."""
        entry = (self.kept_stats() or {}).get(model_key, {}).get(measure)
        if not entry or len(entry["samples"]) < 3:
            return None
        return _stats.describe(entry)["median"]

    def _slow_heard(self, backend, model_key, times, got, normal) -> None:
        """An answer far slower than its model's normal (D3): on the record,
        counted with the newest in words for the heartbeat, said at once."""
        if "real_time_factor" in got:
            words = (f"an answer of {model_key} was {times:g} times slower than its normal: "
                     f"{got['real_time_factor']:.3f} s a second of audio, against "
                     f"{normal:.3f} s")
        else:
            words = (f"an answer of {model_key} was {times:g} times slower than its normal: "
                     f"{got['tokens_per_s']:.1f} tokens a second, against {normal:.1f}")
        with self._work_lock:
            self._slow_answers += 1
            self._slow_answer = words
        self._event("answer_slow", model_key=model_key, load=getattr(backend, "load_id", None),
                    times=times, normal=normal, words=words)
        self._beat_now()

    def _beat_now(self) -> None:
        """The card source's next heartbeat goes at once, when it has one."""
        beat = getattr(self._card, "beat_now", None)
        if beat is not None:
            try:
                beat()
            except Exception:                               # noqa: BLE001
                pass

    def _after_first_answer(self, backend) -> None:
        """Its load's first answer ended: the card's memory looked at after it
        (`first_answer_mib`), once per load, on a thread of its own."""
        with self._lock:
            entry = next((e for e in self._loads.entries() if e.backend is backend), None)
            if entry is None or entry.answered:
                return
            entry.answered = True
            if entry.before is None:
                return
        def look():
            try:
                self._memory_measured(entry, "first_answer_mib")
            except Exception as exc:                        # noqa: BLE001 - said, never fails
                try:
                    self._event("stats_failed", model_key=entry.model_key, detail=str(exc)[:500])
                except Exception:                           # noqa: BLE001 - its folder has gone
                    pass
        threading.Thread(target=look, daemon=True, name=f"first-answer-{entry.model_key}").start()

    def stat_lines(self) -> list:
        """The statistics' records (`timed` lines), in the order written: the
        bound folder's (`runner/model_stats.jsonl`), or - nothing bound -
        the newest kept in memory."""
        with self._stats_lock:
            if self.data_dir is None:
                return list(self._timed_lines)
            return read_events(self.data_dir / STATS_RECORDS_NAME)

    def recent_events(self) -> list:
        """The last events, bound or not - the only record there is while
        nothing is bound."""
        return list(self._recent)

    # -------------------------------------------------------------- the state

    @property
    def loading(self) -> bool:
        """Is a load under way? A model load is not a stalled generation - and
        neither is waiting for the card, which is the first part of one."""
        return self._loads.loading() is not None or self._loads.waiting > 0

    def loading_for(self, model_key) -> bool:
        """S17: is *model_key*'s load under way - its weights going up, or its
        take waiting for the card? Answers to other models run beside a load,
        and one of theirs quiet before its first token is not loading."""
        entry = self._loads.loading()
        return (entry is not None and entry.model_key == model_key) \
            or model_key in self._loads._waiting

    def up_keys(self) -> list:
        """S17: every model up on the card and handed out - loaded, and no
        stop begun - in the order they began. A job on one of these answers
        beside the others with no load."""
        return self._up_keys()

    def status(self) -> dict:
        """Health's `runner`. Of the models up side by side, the one used
        last - its load, its timing, its process; `loading` the load under
        way, one at a time; `in_use` the generations on any model, and
        `in_use_models` which models they hold (S17: answers to different
        models run at once)."""
        up = self._loads.loaded() or _NO_LOAD
        b = up.backend
        return {
            "backend": self.backend_kind,
            "loaded": up.model_key,
            "loaded_label": b.describe() if b else None,
            "load": up.load_id,
            "loading": (self._loads.loading() or _NO_LOAD).model_key,
            "in_use": self._loads.in_use,
            # S17: answers to different models run at once - every model a
            # generation holds now, each once, by name
            "in_use_models": sorted(self._loads._uses),
            "loaded_at": up.loaded_at,
            "last_used": self._loads.last_used,
            "load_seconds": round(up.load_seconds, 2),
            "loads_this_session": self._loads.loads_this_session,
            "pid": getattr(b, "pid", None),
            "port": getattr(b, "port", None),
            "idle_unload_s": float(self.cfg.runner["idle_unload_s"]),
            # None is not zero: an unmeasured baseline is None, never 0.0
            "load_cold": up.cold,
            "load_baseline_seconds": (round(up.baseline, 2)
                                      if up.baseline is not None else None),
            "load_slow": up.slow,
            # who holds the card is not the same question as what is loaded:
            # during a cold load `loaded` is still None while the lease already
            # names the holder and the model the card was taken for
            "lease": self._lease.public(),
        }

    def card_status(self) -> dict:
        """Health's `card`: where this process stands on the card. With a
        coordinator, its source says - holding its rental. Without one, the
        card is held by the pipeline - the machine's lock, or the stand-in's
        lease - so `holding` is the pipeline's, whether or not a job is
        running: a model up while the queue is idle still holds the card."""
        said = self._card.status()
        if said is not None:
            return said
        return {"coordinator": "none", "holding": self._pipe_holds(), "waiting_since": None}

    @property
    def card_lost(self):
        """The reason an answer gave when the card was taken back from this
        process, until it is granted again; None otherwise."""
        return getattr(self._card, "lost", None)

    # -------------------------------------- the work, as the watch reads it (D3)

    def work(self) -> dict:
        """What the rental's work says, on each heartbeat its card source makes
        (PLAN-4 D3): `working` - a model is answering a request; `progress` -
        the tokens streamed and the answers ended by this process, which only
        real work moves; `deadline_s` - of the answers under way that come in
        one piece, the latest deadline, in seconds from now, or None; the
        memory errors its models' logs said: how many, and the newest; and
        the answers far slower than their model's normal (D3, D12): how many,
        and the newest, in words. Read under its own lock, never the
        manager's: the heartbeat's thread asks, and must never wait on a
        load."""
        now = time.monotonic()
        with self._work_lock:
            dues = [d for d in self._answers.values() if d is not None]
            return {"working": bool(self._answers), "progress": self._progress,
                    "deadline_s": round(max(0.0, max(dues) - now), 3) if dues else None,
                    "memory_errors": self._memory_errors, "memory_error": self._memory_error,
                    "slow_answers": self._slow_answers, "slow_answer": self._slow_answer}

    def _answer_begins(self, me, model_key) -> None:
        """Under the work lock: answer *me* on *model_key* begins (S17) - every
        answer under way now answers beside it, and it beside each of them."""
        mine = set()
        for other, (theirs, beside) in self._beside.items():
            mine.add(theirs)
            beside.add(model_key)
        self._beside[me] = (model_key, mine)

    def _beside_of(self, me) -> list:
        """Every model that answered beside answer *me* at any moment of it,
        by name - [] for an answer alone."""
        with self._work_lock:
            _, beside = self._beside.get(me, (None, set()))
            return sorted(m for m in beside if m is not None)

    @property
    def degraded(self):
        """The words the card is DEGRADED in - while the rental it was said of
        is the one held - or None: the card has room, or that rental is over
        (a new one is not the degraded one)."""
        said = self._said()
        return None if said is None else said[1]

    def _said(self):
        """The card's word that it is DEGRADED - (key, words, numbers) - while
        the rental, or the lock's session, it was said of is the one held;
        None otherwise."""
        said = self._degraded
        if said is None:
            return None
        key = self._session_key()
        if key is None or getattr(self._card, "lost", None) or key != said[0]:
            return None
        return said

    def _session_key(self):
        """What the card is held as: a coordinator's rental, by its id - or,
        on the machine's own lock, the session its card source names (S5b)."""
        rental = self._rental()
        return rental.get("id") if rental else getattr(self._card, "session", None)

    @property
    def card_full(self):
        """S5b: the card's numbers while it is DEGRADED - {free_mib, need_mib,
        outside_mib, total_mib}, each None when not known - or None."""
        said = self._said()
        return None if said is None else dict(said[2])

    def _recovers(self) -> bool:
        """Does the card source watch the card and say when it has room again
        (S5b) - a coordinator's rental, the machine's lock with a way to read
        the card? Only then is work held for the card's memory: held on a
        source that never says, it would wait out its whole patience."""
        return bool(getattr(self._card, "recovers", False))

    def patience(self, terms=None) -> float:
        """How long work waits for the card: the request's own patience, or
        the server's (`card_patience_s`)."""
        asked = getattr(terms, "patience_s", None)
        return max(0.0, float(self.cfg.runner["card_patience_s"] if asked is None else asked))

    def if_no_room(self, terms=None) -> str:
        """S18: what work of these terms does when its model does not fit -
        `wait` for room, or `fail` at once. Live work - responsive, or no
        class - always fails (the owner's first ruling of 2026-09-28);
        scheduled work does what its request said, else the server's setting
        (`scheduled_if_no_room`)."""
        if getattr(terms, "klass", None) != "scheduled":
            return "fail"
        asked = getattr(terms, "if_no_room", None)
        return asked if asked in ("wait", "fail") else self.cfg.runner["scheduled_if_no_room"]

    def _parks(self, terms) -> bool:
        """S18: does work of these terms wait for room in its queue - a job's
        that waits for room - rather than fail now?"""
        return bool(getattr(terms, "parks", False)) and self.if_no_room(terms) == "wait"

    def _stand_in_held(self) -> int:
        """What the stand-in's models on its card hold, in MiB, as its settings
        say each needs (S18): the stand-in's card reads that much less free."""
        needs = self.cfg.mock.get("need_mib") or {}
        return sum(int(needs.get(e.model_key) or 0) for e in self._loads.on_card())

    def room_look(self, want, holder):
        """THE QUEUE'S LOOK (S18) for the scheduled job it waits to run -
        *want*: {job, model, not_before, for, if_no_room, waited}, or None
        when none waits. -> {"due": whether it may go now, "room": what it
        waits on, or None}, or None. A card source that keeps a place in line
        (a coordinator's rental) answers for itself - a ticket asked from its
        morning, offered at its time, passed over while it has no room.
        Otherwise the runner looks: it may go at once, unless it waited for
        room - then the card is read (never on a manager that may not read
        it) and the model's need judged by the contract's one memory check,
        with the margin its settings keep. The job's time is its queue's to
        keep: the queue never starts a job before it, whatever this says."""
        if want is not None and want.get("if_no_room") is None:
            want = dict(want, if_no_room=self.if_no_room(Terms(klass="scheduled")))
        look = getattr(self._card, "look", None)
        if look is not None:
            got = look(want, holder)
            if got is not None or want is None:
                return got
        if want is None:
            return None
        if not want.get("waited") or not self._reads_card:
            return {"due": True, "room": None}
        need = self.need(want["model"])[0]
        try:
            got = self._vram()
        except Exception as exc:                            # noqa: BLE001 - a look never fails
            self._event("card_read_failed", detail=str(exc)[:300])
            return {"due": True, "room": None}
        free, total = (got.get("free"), got.get("total")) if isinstance(got, dict) else (got, None)
        verdict = MEM.fit(need, free, total, int(self.cfg.runner["card_margin_mib"]))
        if verdict["verdict"] != "no_room":
            return {"due": True, "room": None}
        return {"due": False, "room": room_waited(verdict, self._outside(free, total))}

    def _outside(self, free, total):
        """What programs outside the bootstrap hold, in MiB - the card's use
        less what this runner's models on it hold (the stand-in's say, a real
        one its measured memory, or else its need) - or None: not known."""
        if free is None or total is None:
            return None
        ours = 0
        for e in self._loads.on_card():
            mib = self.need(e.model_key)[0]
            ours += int(mib or 0)
        return max(0, int(total) - int(free) - ours)

    def holds_while_full(self, terms=None) -> bool:
        """S5b: is work of these terms held for the card's memory, rather than
        failed, when the card is full? On a source that says when it has room
        again, with patience left - 0 is now or not at all."""
        waited = float(getattr(terms, "waited_s", None) or 0.0)
        return self._recovers() and self.patience(terms) - waited > 0

    def _full_now(self):
        """What holds work for the card's memory now: the card's numbers while
        it is degraded; {} while a memory error this server's model said has
        not yet been heard by its source (`UNHEARD_S` at most); else None."""
        full = self.card_full
        if full is not None:
            return full
        at = self._unheard
        if at is not None and time.monotonic() - at < UNHEARD_S:
            return {}
        return None

    def _hold_while_full(self, model_key, terms, cancel, on_full) -> None:
        """S5b, DEGRADED MEANS WAITING, NOT FAILING: model work is held while
        the card is full - the numbers said to `on_full` as it waits: the
        words, the free memory, the room needed, what programs outside the
        bootstrap use, how long it has waited and how much patience is left -
        and goes on once its card source says the card has room again. Its
        patience bounds the wait, counted from the first time the card broke
        this request (`terms.waited_s`); run out, it fails `card_degraded`,
        saying how long it waited and what the card read at the end - and so
        does a patience of 0, at once. A cancel ends it; so does a stop."""
        full = self._full_now() if on_full is not None and self._recovers() else None
        if full is None:
            return
        patience = self.patience(terms)
        before = float(getattr(terms, "waited_s", None) or 0.0)
        started = time.monotonic()
        while full is not None:
            waited = before + time.monotonic() - started
            left = patience - waited
            free, need = full.get("free_mib"), full.get("need_mib")
            if left <= 0:
                read = (f"it read {free} MiB free at the end, and {need} MiB was needed"
                        if free is not None and need is not None
                        else self.degraded or "the card could not be read")
                raise Degraded(f"the card stayed full for the whole of this request's patience "
                               f"of {patience:g}s: {read}", card=full, final=True)
            if on_full is not None:
                try:
                    on_full({"words": full_words(free, need, full.get("outside_mib")),
                             "free_mib": free, "need_mib": need,
                             "outside_mib": full.get("outside_mib"),
                             "waited_s": int(waited), "patience_left_s": int(math.ceil(left))})
                except Exception:                           # noqa: BLE001 - a report never fails
                    pass
            if cancel is not None and cancel.is_set():
                raise Cancelled(f"cancelled while {model_key} waited for the card's memory")
            if self._stopping:
                raise BackendError(f"no model is loaded now: {self._stopping}")
            with self._news:
                self._news.wait(min(left, 0.25))
            full = self._full_now()
        self._event("card_full_waited", model_key=model_key,
                    waited_s=round(time.monotonic() - started, 2))

    def answer(self, backend, messages, params, cancel=None):
        """One answer from *backend*: its stream, yielded as the backend yields
        it - WHERE THE WORK REALLY HAPPENS, so the work is counted here (PLAN-4
        D3). While it streams a model is answering (`work`'s `working`); each
        token, each piece of reasoning and the answer's end move the progress
        by one; an answer the backend says comes in one piece declares its
        deadline (`Backend.answer_deadline_s`). Its model's log is read after
        it for the lines that say the card's memory ran out.

        ON A DEGRADED CARD no answer begins, and the one under way ends at once
        - its stream is cancelled - raising `Degraded`, the card's own reason:
        never an answer left to crawl, nor one called cancelled that nobody
        cancelled. An answer that broke while its model's log said the card's
        memory ran out is the card over-full too: `Degraded`, with the line.

        EACH ANSWER THAT ENDED IS TIMED (PLAN-4 D12) - to its first token and
        whole, its tokens, their pace, the prompt's (`_answer_timed`) - and
        judged against its model's normal: one far slower is flagged, and
        counted on the heartbeat (D3). A model's first answer on a load has
        the card's memory looked at after it."""
        why = self.degraded
        if why:
            raise Degraded(f"no model work now: {why}", card=self.card_full)
        due = backend.answer_deadline_s(messages, params)
        me = object()
        with self._work_lock:
            ends = self._degraded_now
            self._answers[me] = None if due is None else time.monotonic() + float(due)
            self._answer_begins(me, getattr(backend, "model_key", None))
        stop = _Either(ends) if cancel is None else _Either(cancel, ends)
        asked, first, done, done_at, pieces = time.monotonic(), None, None, None, 0
        try:
            try:
                for ev in backend.stream(messages, params, stop):
                    kind = ev.get("type")
                    if kind in ("token", "reasoning", "done"):
                        with self._work_lock:
                            self._progress += 1
                    if kind in ("token", "reasoning"):
                        pieces += 1
                        if first is None:
                            first = time.monotonic()
                    elif kind == "done":
                        done, done_at = ev, time.monotonic()
                    yield ev
                self._answer_timed(backend, asked, first, done, done_at, pieces,
                                   beside=self._beside_of(me))
            except Cancelled:
                if ends.is_set() and not (cancel is not None and cancel.is_set()):
                    raise Degraded(f"the answer was ended: "
                                   f"{self.degraded or 'the card is degraded'}",
                                   card=self.card_full) from None
                raise
            except BackendError as exc:
                said = [] if isinstance(exc, Degraded) else self._note_memory_errors(backend)
                if said:
                    raise Degraded(f"{exc} - its log said the card's memory ran out: "
                                   f"{said[-1]}", card=self.card_full) from None
                raise
        finally:
            with self._work_lock:
                self._answers.pop(me, None)
                self._beside.pop(me, None)
            self._note_memory_errors(backend)

    def _note_memory_errors(self, backend, model_key=None) -> list:
        """The lines *backend*'s log said since it was last asked, that the
        card's memory ran out (`Backend.memory_errors`): counted, the newest
        kept, each on the record, and said to the card source at once - a
        heartbeat now, not an interval from now. Never fails what it follows.
        -> the lines."""
        ask = getattr(backend, "memory_errors", None)
        if ask is None:
            return []
        try:
            lines = [str(x)[:500] for x in (ask() or [])]
        except Exception:                                   # noqa: BLE001 - a log never fails work
            return []
        if not lines:
            return []
        with self._work_lock:
            self._memory_errors += len(lines)
            self._memory_error = lines[-1]
            if self._recovers() and self._said() is None:
                # S5b: work held for the card's memory waits for the source's
                # word on it - a moment - before it goes on (`UNHEARD_S`)
                self._unheard = time.monotonic()
        for line in lines:
            self._event("memory_error", model_key=model_key or getattr(backend, "model_key", None),
                        load=getattr(backend, "load_id", None), line=line)
        beat = getattr(self._card, "beat_now", None)
        if beat is not None:
            try:
                beat()
            except Exception:                               # noqa: BLE001
                pass
        return lines

    def _heard_degraded(self, degraded) -> None:
        """The card source heard, on a heartbeat's answer, that the rental is
        DEGRADED - the coordinator's word: why, and the numbers - or, None,
        that it is not any more. Heard on the heartbeat's thread, which must
        never wait on the manager's lock: the answers under way end at once,
        a load under way is given up, the pipeline says so, and the record is
        written on a thread of its own. Until it clears no model work is done
        and nothing new loads (`degraded`, asked by every acquire and
        answer).

        S5b: the same word again, with the newest read's numbers, only
        refreshes them - what a held job says as it waits; nothing more is
        ended. The card's word that it has room again is `_heard_restored`."""
        key, now = self._session_key(), self._clock()
        if not degraded:
            return self._heard_restored(now)
        why = f"the card is degraded: {degraded.get('why')}"[:500]
        numbers = {k: degraded.get(k) for k in ("free_mib", "need_mib", "outside_mib",
                                                "total_mib")}
        # the session says it to a person: on a source that says when the card
        # has room again, what to do - close something - and that its work
        # runs again by itself, in numbers; and why, in the source's words
        told = why if not self._recovers() else \
            f"{full_words(numbers['free_mib'], numbers['need_mib'], numbers['outside_mib'])} " \
            f"({why})"[:500]
        with self._work_lock:
            again = self._said() is not None and self._degraded[1] == why
            self._degraded = (key, why, numbers)
            self._unheard = None
            if not again:
                ends, self._degraded_now = self._degraded_now, threading.Event()
        if again:
            with self._pipe_lock:
                pipe = self._pipe
                if pipe is not None and pipe.open and pipe.state == "degraded":
                    pipe.why = told
            self._notify()
            return
        ends.set()
        self.abandon(why)
        with self._pipe_lock:
            pipe = self._pipe
            if pipe is not None and pipe.open and not pipe.closing \
                    and pipe.state == "holding":
                pipe.state, pipe.why, pipe.since = "degraded", told, now
        self._notify()
        threading.Thread(target=self._event, args=("card_degraded",),
                         kwargs={"reason": degraded.get("reason"),
                                 "why": str(degraded.get("why"))[:500], **numbers},
                         daemon=True, name="card-degraded").start()

    def _heard_restored(self, now) -> None:
        """S5b: the card source said the card has room again. The work held
        for its memory goes on - THE MODEL COMES BACK FIRST: every model the
        application's open pipeline keeps that came off the card - its
        process died, or its load failed - is loaded again, through the usual
        memory check, while each held job's own acquire loads its model the
        same way before it runs. On the record (`card_restored`)."""
        with self._work_lock:
            self._degraded = None
            self._unheard = None
        with self._pipe_lock:
            pipe = self._pipe
            if pipe is not None and pipe.open and pipe.state == "degraded":
                pipe.state, pipe.why, pipe.since = "holding", None, now
            kept = [] if pipe is None or not pipe.open or pipe.closing \
                or pipe.opened_by == "queue" or self._stopping else list(pipe.models)
        # asked outside the pipeline's lock: a process is asked whether it lives
        again = []
        for key in kept:
            entry = self._loads.of(key)
            if key in self._failed or entry is None \
                    or (entry.up and not entry.stopping and not entry.backend.is_alive()):
                again.append(key)
        self._notify()
        threading.Thread(target=self._event, args=("card_restored",),
                         kwargs={"again": list(again)} if again else {}, daemon=True,
                         name="card-restored").start()
        for key in again:
            self._failed = {k: v for k, v in self._failed.items() if k != key}
            threading.Thread(target=self._load_pinned, args=(pipe, key, pipe.holder, pipe.klass),
                             daemon=True, name=f"load-{key}").start()

    # ---------------------------------------------------------- model health

    def models(self) -> dict:
        """`GET /api/models`: every model the settings name, what can be
        known of each without loading it (runner/modelhealth.py) - and where
        it stands: its `state`, the word a load of it answers
        (`model_state`), and whether the open pipeline keeps it (`pinned`:
        health's `pipeline.models`); a model up and not pinned was loaded on
        demand, for a job."""
        from .modelhealth import model_health
        path = self.loads_path
        buckets = read_loads(path) if path else None
        loaded = {e.model_key for e in self._loads.on_card()}
        measures = dict(self._measures)
        # its statistics (D12), each measure described - none with nothing
        # bound, or with a summary that cannot be read
        kept = self.kept_stats()

        def stats(key):
            if kept is None:
                return None
            return {k: _stats.describe(e) for k, e in kept.get(key, {}).items()}
        with self._pipe_lock:
            pipe = self._pipe
            pinned = set(pipe.models) if pipe is not None and pipe.open else set()
            stands = {key: self.model_state(key) for key in self.cfg.models}
        return {"runner": self.backend_kind, "models_dir": str(self.cfg.models_dir),
                "loads_from": str(path) if path else None,
                # what a slow answer is judged by (D3, D12): the settings the
                # judging reads, so a page shows the server's thresholds
                "slow_answer": {"factor": float(self.cfg.runner["slow_answer_factor"]),
                                "floor_s": float(self.cfg.runner["slow_answer_floor_s"])},
                "models": [{**model_health(spec, self.cfg.models_dir, self.backend_kind,
                                           buckets, loaded=key in loaded,
                                           measure=measures.get(key), headers=self._headers,
                                           stats=stats(key)),
                            "state": stands[key], "pinned": key in pinned}
                           for key, spec in sorted(self.cfg.models.items())]}

    def read_speed(self, key: str) -> dict:
        """`POST /api/models/{key}/read-speed`: one stretch of the model's
        file, measured (runner/diskread.py). KeyError for a model the settings
        do not name; ModelRefused for one with no file, while a load is under
        way - the measure would measure the load as well - while another
        measure runs, and when the file cannot be read."""
        from .diskread import disk_of, measure
        from .modelhealth import ModelRefused
        spec = self.cfg.model(key)
        if not spec.file:
            raise ModelRefused(409, "no_file", f"{key} is a stand-in: it has no file to read")
        path = spec.gguf_path(self.cfg.models_dir)
        if not path.is_file():
            raise ModelRefused(409, "no_file", f"{key}'s file is not there: {path}")
        if not self._measuring.acquire(blocking=False):
            raise ModelRefused(409, "busy", "a measure is already running")
        try:
            if self.loading:
                raise ModelRefused(409, "busy", "a model is loading: a measure now would "
                                                "measure the load as well")
            try:
                got = measure(path, seen=self._read_before)
            except OSError as exc:
                raise ModelRefused(409, "unreadable", f"cannot read {path}: {exc}") from None
        finally:
            self._measuring.release()
        self._measures[key] = got
        return {"key": key, "path": str(path), "disk": disk_of(path), "measure": got}

    # ----------------------------------------------------------------- VRAM

    #: headroom over the estimate, which is a floor: llama.cpp allocates
    #: compute buffers and a CUDA context on top of the weights and the cache
    #: (the estimate itself is `needs.need_mib`'s, and says it in MiB)
    VRAM_SLACK_GB = 1.5

    @staticmethod
    def card_memory_mib():
        """The card's memory per nvidia-smi - {"free", "total"} in MiB - or None
        when it cannot be asked. NONE IS NOT ZERO AND NEVER GATES: a missing
        instrument must not become an outage."""
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free,memory.total",
                                  "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=10,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if out.returncode != 0:
                return None
            free, total = (int(x) for x in out.stdout.strip().splitlines()[0].split(","))
            return {"free": free, "total": total}
        except Exception:                                   # noqa: BLE001
            return None

    def _check_vram(self, model_key: str) -> None:
        """Refuse a load the card cannot hold, before paying for it. THE CARD
        SOURCE DECIDES (PLAN-4 D2) - by the contract's one memory check, on
        the runner's estimate of what the model needs (`needs.need_mib`):
          * a coordinator's rental asks the coordinator (an amend), which
            reads the card afresh, and approves or refuses in its own words;
          * the machine's lock - a server run on its own - reads the card
            here (`vram`, this runner's own way) and keeps the settings'
            margin free (`card_margin_mib`);
          * a source with no memory check - the stand-in's - decides nothing.
        Refused, the load is refused in the source's words, which say the
        numbers and WHICH: a model no card this size could hold says so, and
        what to change. The record's `vram_check` says the numbers either way,
        and `vram_unknown` that the card could not be read - which never
        gates: a missing instrument must not become an outage."""
        decide = getattr(self._card, "fit", None)
        if decide is None:
            return
        engine = self.cfg.engine(model_key)                 # the model's own, on the card
        # measured, once it has three measured loads (D2, D12)
        need, how, hint = self.need(model_key)
        got = decide(model_key, engine, need, read=self._vram,
                     margin_mib=int(self.cfg.runner["card_margin_mib"]), how=how, hint=hint)
        if got is None:
            return
        free = got.get("free_mib")
        if got["verdict"] == "fits" and self._reads_card and _stats._number(free):
            # the card's free memory before the load, for what it took (D12),
            # as the table stood when it was read
            with self._lock:
                self._fit_read[model_key] = (float(free), self._loads.moves)
        source = getattr(self._card, "kind", None)
        if got["verdict"] == "unknown":
            self._event("vram_unknown", model_key=model_key, source=source,
                        need_mib=got.get("need_mib"), detail=str(got.get("words", ""))[:300])
            return
        card = got.get("total_mib")
        self._event("vram_check", model_key=model_key, source=source, verdict=got["verdict"],
                    need_mib=got.get("need_mib"), free_mib=got.get("free_mib"),
                    margin_mib=got.get("margin_mib"), card_mib=card,
                    need_gb=round((got.get("need_mib") or 0) / 1024, 2),
                    free_gb=round((got.get("free_mib") or 0) / 1024, 2),
                    **({"card_gb": round(card / 1024, 2)} if card else {}))
        if got["verdict"] != "fits":
            said = str(got.get("words") or f"{model_key} does not fit on the card")
            # the refusal's own reason, for its job to end with: the memory's
            # verdict, or the card degraded - nothing new goes on it (D3)
            refused = Degraded(said) if got["verdict"] == "degraded" else BackendError(said)
            if got["verdict"] in ("no_room", "never_fits"):
                refused.reason = got["verdict"]
            # which refusal it is: making room (`_make_room`) takes nothing off
            # for a model no card this size could hold, nor on a degraded card
            refused.verdict = got["verdict"]
            # ...and its numbers, for scheduled work that waits for room (S18)
            refused.got = dict(got)
            raise refused

    # -------------------------------------------------------------- orphans

    def startup_check(self, reclaim: bool = False) -> dict:
        """What a stopped server of this folder left running.

        FOUND FROM THE RECORD, NEVER BY PORT: a load the events file says was
        made and never unloaded, with the pid, port and load id it ran with.
        Each is asked about by pid; one that is gone is nobody's problem.

        RECLAIM IS OPT-IN, and even then a process is killed only if it is
        still OURS (`is_ours`), by its own engine's proof: its command line
        names the engine's program in these settings, the model file, the
        port and the load. The engine is the one the load's line names - a
        line naming none is from before engines were named, and was
        llama.cpp's. Never a process merely found on a port, never by when it
        started - a port or a pid can belong to anything by now. A reclaim
        grants nothing: killing what was left running is not the same as
        being handed the card."""
        orphans = orphaned_loads(read_events(self.events_path)) if self.events_path else []
        found, killed = [], []
        for ev in orphans:
            pid = ev.get("pid")
            if not pid:
                continue                                    # a stand-in load: no process
            info = self._processes(pid)
            if info.get("alive") is False:
                continue
            engine = ev.get("engine") or "llamacpp"
            known = _engines.ENGINES.get(engine)
            # its model file: `file`, whatever the engine - or, on a line from
            # before that name, `gguf`, which every model's line had then
            model = ev.get("file") or ev.get("gguf")
            ours = bool(info.get("alive")) and known is not None and is_ours(
                info.get("command_line"), self.cfg.runner.get(known.program),
                model, ev.get("port"), ev.get("load"), engine=engine)
            entry = {"pid": pid, "port": ev.get("port"), "model_key": ev.get("model_key"),
                     "load": ev.get("load"), "alive": info.get("alive"), "ours": ours}
            found.append(entry)
            self._event("orphan_found", **entry)
            if reclaim and ours:
                if self._kill(pid):
                    killed.append(pid)
                    self._event("orphan_reclaimed", pid=pid, port=ev.get("port"),
                                model_key=ev.get("model_key"), load=ev.get("load"))
                else:
                    self._event("orphan_reclaim_failed", pid=pid, load=ev.get("load"))
        return {"orphans": found, "reclaimed": killed}

    # --------------------------------------------------------------- loading

    def _make(self, model_key: str, port) -> Backend:
        """The backend for one load of *model_key*: the stand-in, answering in
        its engine's shape - or, on the real card, the model's own engine."""
        spec = self.cfg.model(model_key)
        if self.backend_kind == "mock":
            return MockBackend(model_key, spec.label, self.cfg.mock, engine=spec.engine,
                               ctx=spec.ctx)
        if spec.engine == "llamacpp":
            from .llamacpp import LlamaCppBackend
            return LlamaCppBackend(spec, self.cfg.models_dir, self.cfg.runner,
                                   self.logs_dir, port)
        if spec.engine == "whisper":
            from .whisper import WhisperBackend
            return WhisperBackend(spec, self.cfg.models_dir, self.cfg.runner,
                                  self.logs_dir, port, starts=self._starts)
        raise BackendError(f"no engine called {spec.engine!r}")

    def _port_for_load(self, model_key: str, load_id: str):
        """The port a load will listen on: none for the stand-in; for a real
        server, one from the port source, taken in the load's own name - by
        default the configured one, or a free one of the runner's own range
        for each load."""
        if self.backend_kind == "mock":
            return None
        return int(self._ports.take(model_key, load_id))

    def _give_port_back(self, port, model_key) -> None:
        """A load's port goes back when the load does. Never fails what it
        follows: a port that cannot be given back is recorded, and its source
        takes it back in its own time (a lease that is not heartbeated
        expires)."""
        if port is None or self.backend_kind == "mock":
            return
        try:
            self._ports.give_back(port)
        except Exception as exc:                            # noqa: BLE001
            self._event("port_give_back_failed", model_key=model_key, port=port,
                        detail=str(exc)[:500])

    def _limit(self) -> int:
        """How many loads the card may hold at once: `max_loads` - a rental
        names at most 8 processes - or one, when the settings name one port
        for every model: a port serves one load at a time."""
        if self.backend_kind != "mock" and int(self.cfg.runner.get("port") or 0) > 0:
            return 1
        return int(self.cfg.runner["max_loads"])

    def _pinned(self, entry: _Load) -> bool:
        """Does a pipeline keep *entry*'s model - pinned by its load, or named
        by the open pipeline? Read without a lock: a pipeline's `models` is
        replaced whole."""
        if entry.pinned:
            return True
        pipe = self._pipe
        return pipe is not None and pipe.open and entry.model_key in pipe.models

    def acquire(self, model_key: str, holder: str, *, for_=None, cancel=None,
                on_wait=None, keep_loading=None, terms=None, pinned=False,
                on_full=None) -> Backend:
        """A live backend for *model_key*, loading it if necessary - beside
        the models already up. *holder* is required and has no default: a
        default would be somebody's guess written into the one field that
        says who holds the card.

        Before a load the card is HELD, through the pipeline: the open one's,
        or the queue's own, opened now with the request's `terms`. The CARD
        SOURCE is asked only when no pipeline holds it - and while it answers
        "wait", `on_wait` hears that it waits, `cancel` ends the wait, and
        nothing moves: never a load without a grant. `for_` is the request the
        load is for, named on the ticket. `pinned`: the pipeline keeps this
        model, and the idle sweep never takes it off; a model the open
        pipeline names is kept whoever loads it.

        LOADS RUN ONE AT A TIME, under the load lock, which a cancel ends and
        the server stopping ends; the manager's lock is never held across a
        load, so a generation on a model already up never waits for one.

        A CANCEL DURING THE LOAD gives the load up at once, unless
        `keep_loading()` says the job that runs next wants the same model -
        then the load goes on for it (see `_LoadWatch`). Either way a
        cancelled caller is never handed the model: it gets `Cancelled`, and
        the model is on the card only if the next job still wants it once it
        is up.

        An acquire is WORK for the queue's own pipeline while it lasts; when it
        ends, the pipeline is asked whether its work is done (`settle`).

        ON A FULL CARD (S5b), an acquire given `on_full` - a job's - is HELD
        until its card source says the card has room again, within the
        request's patience, `on_full` hearing the numbers as it waits
        (`_hold_while_full`); then its model is loaded again if it came off,
        through the usual memory check. Without it - a pipeline's own loads,
        a row's - a full card refuses at once, `Degraded`."""
        with self._lock:
            self._acquiring += 1
        asked = time.monotonic()                            # its load is timed from here (D12)
        try:
            # every load line names the request it served, when it has one
            served = {"for": for_} if for_ else {}
            for _ in range(self.RETAKES):
                self._hold_while_full(model_key, terms, cancel, on_full)
                backend = self._warm(model_key, holder, served, pinned)
                if backend is not None:
                    return backend
                # THE WAIT FOR THE CARD HOLDS NO LOCK. It can last the whole
                # patience, and a shutdown, a rebind or a line of the record
                # must never queue behind it - found by a mutant, when a wait
                # that held the lock hung a suite's cleanup (and would have hung
                # a server's stop the same way).
                took = self._take_card(holder, model_key, for_, cancel, on_wait, terms)
                backend = self._load(model_key, holder, served, cancel, keep_loading, pinned,
                                     asked, took, terms)
                if backend is not None:
                    return backend
                # the pipeline holding the card closed while this waited: the
                # card is taken again, never loaded onto without a pipeline
            raise NoCard(f"no card for {model_key}: the pipeline holding it closed while this "
                         f"load waited, {self.RETAKES} times over")
        finally:
            with self._lock:
                self._acquiring -= 1
            self._settle_quietly()

    def _warm(self, model_key, holder, served, pinned=False):
        """*model_key*'s backend if it is up and alive: handed back, and the
        models beside it are not touched. Asked with the manager's lock - the
        lease before anything moves, a model nobody configured refused - and
        whatever must come off comes off outside it: every model, when the
        card was taken back under them; this one, when its process is gone
        (loaded again, and the record says why) or `warm_keep` is off.
        -> the backend, or None: it must be loaded."""
        warm = bool(self.cfg.runner.get("warm_keep", True))
        while True:
            with self._lock:
                if self._stopping:
                    raise BackendError(f"no model is loaded now: {self._stopping}")
                said = self.degraded
                if said:
                    # the card is over-full (D3): no model work until it has
                    # room again - the next as much as the one under way
                    raise Degraded(f"no model work now: {said}")
                self._lease.check(holder)                       # before anything moves
                self.cfg.model(model_key)                       # and so is a model nobody configured
                if self._card.lost and self._loads.up():
                    # the card was taken back under them: none may be used
                    # again, whatever the model is
                    off = self._loads.up()
                else:
                    up = self._loads.of(model_key)
                    if up is None or not up.up or up.stopping:
                        return None
                    if not warm:
                        off = (up,)
                    elif up.backend.is_alive():
                        self._loads.used_now(up, self._clock())
                        self._lease.grant(holder, model_key)
                        up.pinned = up.pinned or bool(pinned)
                        self._event("reuse", model_key=model_key, load=up.load_id, **served)
                        return up.backend
                    else:
                        # A warm backend is not necessarily a live one: its
                        # process was killed underneath it. A reload beats
                        # handing out a corpse, and the record says so - "the
                        # model reloaded itself" and "the model was never
                        # running" look the same in a record that is silent.
                        self._event("stale_backend", model_key=model_key, load=up.load_id,
                                    reason="its process is gone; loading it again")
                        off = (up,)
            self._take_off(off)

    def _take_load_lock(self, model_key, cancel) -> None:
        """Wait for the load under way to end: one load at a time. Bounded by
        that load - itself bounded by its start's own timeout - and ended at
        once by a cancel or by the server stopping. What it waits for is on
        health: `loading` names the model going up."""
        while not self._load_lock.acquire(timeout=0.05):
            if cancel is not None and cancel.is_set():
                raise Cancelled(f"cancelled while another model loaded, before {model_key} "
                                f"could")
            if self._stopping:
                raise BackendError(f"no model is loaded now: {self._stopping}")

    def _wait_out_stop(self, model_key, cancel, anyone=False) -> None:
        """A load of *model_key* whose stop is under way is waited out before
        the model loads again: never two loads of one model, and its process
        may still hold the card. `anyone`: every stop under way, of any
        model - the room a load that does not fit waits for (`_make_room`).
        Bounded by each stop's own bound; a cancel and the server stopping
        end it."""
        while True:
            under_way = [e for e in self._loads.entries()
                         if e.stopping and (anyone or e.model_key == model_key)]
            if not under_way:
                return
            if cancel is not None and cancel.is_set():
                raise Cancelled(f"cancelled while {under_way[0].model_key} was being taken off, "
                                f"before {model_key} could load")
            if self._stopping:
                raise BackendError(f"no model is loaded now: {self._stopping}")
            with self._news:
                self._news.wait(0.05)

    def _load(self, model_key, holder, served, cancel, keep_loading, pinned, asked=None,
              took=None, terms=None):
        """Load *model_key*, holding the load lock - and only it - for the
        whole load. -> its backend; None when the pipeline holding the card
        closed while this waited for it (the card is taken again)."""
        self._take_load_lock(model_key, cancel)
        try:
            try:
                # looked at again: the model may have come up while this waited
                backend = self._warm(model_key, holder, served, pinned)
                if backend is not None:
                    return backend
                self._wait_out_stop(model_key, cancel)
                # a model whose stop failed may still be on the card: its stop
                # is asked again, and while it fails nothing loads (the old
                # runner loaded beside it, and its record read two at once)
                stuck = self._retry_stuck()
                if stuck is not None:
                    entry, exc = stuck
                    raise BackendError(f"refusing to load {model_key}: {entry.model_key} "
                                       f"({entry.load_id}) did not come off the card, and may "
                                       f"still be on it - its stop failed again: "
                                       f"{str(exc)[:300]}")
                self._make_room(model_key, cancel, terms)
                with self._lock:
                    if not self._may_begin(model_key):
                        return None
                # the port and the backend OUTSIDE the manager's lock: a port
                # from the coordinator's registry is a question over HTTP, and
                # nothing that asks the manager waits for it
                load_id = ids.mint_actor("load")
                try:
                    port = self._port_for_load(model_key, load_id)
                except Exception as exc:                    # noqa: BLE001 - refused, said
                    raise BackendError(f"no port for {model_key}: {exc}") from None
                try:
                    backend = self._factory(model_key, port)
                except Exception:
                    self._give_port_back(port, model_key)
                    raise
                backend.load_id = load_id
                # what a real load puts on the card is named to the card source
                # the moment it exists - with its model, engine and port - and a
                # coordinator ends it on that proof if this server hangs while it
                # holds the card
                backend.on_spawn = functools.partial(self._card_running, model_key=model_key,
                                                     port=port)
                try:
                    with self._lock:
                        # looked at again, and begun in the same hold: a close
                        # that began meanwhile is seen, and one that begins
                        # after sees this load (`_close_locked`)
                        began = self._may_begin(model_key)
                        if began:
                            self._lease.grant(holder, model_key)    # before the weights go up
                            # the load is the table's from here - under way until it is up
                            entry = self._loads.begin(load_id, model_key, backend, port)
                            entry.pinned = bool(pinned)
                            # the card's free memory its fit check read (D12)
                            entry.before = self._fit_read.pop(model_key, None)
                            self._failed = {k: v for k, v in self._failed.items()
                                            if k != model_key}
                except Exception:
                    self._give_port_back(port, model_key)
                    raise
                if not began:
                    self._give_port_back(port, model_key)
                    return None
            except (Cancelled, WaitsForRoom):
                raise                               # S18: waiting for room is no failure
            except Exception as exc:
                self._load_failed(model_key, exc)
                raise
            return self._go_up(entry, served, cancel, keep_loading, asked, took)
        finally:
            self._load_lock.release()

    def _may_begin(self, model_key) -> bool:
        """May a load of *model_key* begin now? Under the manager's lock.
        False when the pipeline holding the card closed (the card is taken
        again); refused when the server is stopping, when a model whose stop
        failed may still be on the card - stops run beside loads, so one can
        fail after this load last asked - and, a guard, when the table holds
        a load of the model already."""
        if self._stopping:
            # granted as the server stops: nothing goes up, and the queue's
            # own pipeline gives the card straight back as this ends (`settle`)
            raise BackendError(f"no model is loaded now: {self._stopping}")
        said = self.degraded
        if said:
            # degraded while this load waited for the load lock: nothing new
            # loads (D3)
            raise Degraded(f"nothing new is loaded now: {said}")
        if not self._pipe_holds():
            return False
        if self._stuck:
            stuck = self._stuck[0]
            raise BackendError(f"refusing to load {model_key}: {stuck.model_key} "
                               f"({stuck.load_id}) did not come off the card, and may "
                               f"still be on it - its stop failed as this load began")
        held = self._loads.of(model_key)
        if held is not None:
            # unreachable while the table keeps itself - a model up was handed
            # back, one being stopped waited out - which is why it is a guard:
            # before the lease, the port and a backend, never two loads of one
            # model
            raise ConcurrencyViolation(f"refusing to load {model_key}: the table holds "
                                       f"{held.load_id} of it already - never two loads of "
                                       f"one model")
        return True

    def _go_up(self, entry: _Load, served, cancel, keep_loading, asked=None,
               took=None) -> Backend:
        """The weights go up - under the load lock alone: nothing that asks
        the manager waits for them. -> the backend, up."""
        model_key, load_id, backend, port = (entry.model_key, entry.load_id, entry.backend,
                                             entry.port)
        watch = None
        try:
            if self._card.lost:
                # taken back between the grant and here: another holder may be
                # loading onto the card already
                raise BackendError(f"the card was taken back ({self._card.lost}) before "
                                   f"{model_key} went up")
            self._event("load_start", model_key=model_key, load=load_id, port=port, **served)
            started = time.monotonic()
            if cancel is not None:
                watch = _LoadWatch(backend, model_key, cancel, keep_loading)
            try:
                backend.start()
                ready = time.monotonic()
            except Exception as exc:                        # noqa: BLE001 - reported, re-raised
                self._event("load_failed", model_key=model_key, load=load_id,
                            detail=str(exc)[:500], **served)
                raise
            finally:
                if watch is not None:
                    watch.end()
        except Exception as exc:
            # A load that failed put nothing on the card - the models beside it
            # keep the card and the lease, and when none is left the hold ends
            # with it - and the load's own error is what the caller sees, never
            # a lease error in its place. Its port goes back too; the card
            # stays with the pipeline.
            with self._lock:
                self._loads.end(entry)
                self._release_lease_if_empty()
            self._give_port_back(port, model_key)
            # a load that failed for the card's memory says so in its log (D3)
            self._note_memory_errors(backend, model_key)
            given_up = watch is not None and watch.given_up
            self._load_failed(model_key, exc, cancelled=bool(given_up))
            if given_up:
                raise Cancelled(f"cancelled while {model_key} loaded: the load was given up, "
                                f"and nothing is on the card") from None
            raise
        with self._lock:
            self._loads.came_up(entry, time.monotonic() - started, self._clock())
            # COLD is the first load of this model by this process; WARM a
            # later one. Decided before the key is remembered.
            cold = model_key not in self._loaded_keys
            self._loaded_keys.add(model_key)
            notes = list(getattr(backend, "notes", None) or [])
            # what else was on the card as it came up - a load beside others
            # is timed beside them - named as the record holds them open
            beside = [{"model_key": e.model_key, "load": e.load_id}
                      for e in self._loads.on_card() + tuple(self._stuck) if e is not entry]
            self._event("load_ok", **self._load_fields(entry), cold=cold,
                        load_seconds=round(entry.load_seconds, 3), beside=beside,
                        **({"notes": notes} if notes else {}), **served)
        self._note_load(entry, cold, entry.load_seconds)
        self._load_timed(entry, started if asked is None else asked, started, ready, took)
        with self._pipe_lock:
            if self._pipe is not None:
                self._pipe.last_model = model_key
        self._notify()
        if watch is not None and cancel.is_set():
            # cancelled while the weights went up, and the load was kept for
            # the next job - asked once more, now that the model is up
            if watch.keep():
                self._event("load_kept", model_key=model_key, load=load_id,
                            reason="its job was cancelled while it loaded; the job that runs "
                                   "next wants it", **served)
                raise Cancelled(f"cancelled while {model_key} loaded; it stays up for the job "
                                f"that runs next")
            self._take_off((entry,))
            raise Cancelled(f"cancelled while {model_key} loaded; no job waiting wants it now, "
                            f"so it came off")
        return backend

    def _load_failed(self, model_key, exc, cancelled=False) -> None:
        """A load of *model_key* failed, or was refused: what a wait for it
        reads (`failed`) - a cancel is no failure of the model's - until a
        load of it begins again."""
        if not cancelled and not isinstance(exc, Cancelled):
            self._failed = {**self._failed, model_key: str(exc)[:500]}
        self._notify()

    def _room_to_wait_for(self, refusal, terms):
        """S18: the room scheduled work waits for, when the card source found
        no room for its model - or None: it fails now. It waits only when its
        terms wait in their queue (`_parks`) and nothing of this
        application's is left on the card: its own models holding the room
        is no wait anybody else can end - that fails at once, loudly, as
        ever ("unload one it keeps"). What programs outside the bootstrap hold
        is the card source's word, or - nothing of ours on the card - the
        card's whole use."""
        if not self._parks(terms):
            return None
        with self._lock:
            ours = bool(self._loads.entries() or self._stuck)
        if ours:
            return None
        got = getattr(refusal, "got", None) or {}
        free, total = got.get("free_mib"), got.get("total_mib")
        outside = got.get("outside_mib")
        if outside is None and free is not None and total is not None:
            outside = max(0, int(total) - int(free))
        return room_waited({"need_mib": got.get("need_mib"), "free_mib": free,
                            "margin_mib": got.get("margin_mib"), "total_mib": total}, outside)

    def _make_room(self, model_key, cancel=None, terms=None) -> None:
        """Room on the card for *model_key*, under the load lock: within the
        runner's limit, and the card source's fit check (`_check_vram`)
        passed. A load that does not fit takes off an on-demand model not
        answering a request - the one used longest ago first - and asks
        again, until it fits or nothing more may come off: then it is refused
        in the last refusal's own words. A pinned model never comes off for
        another.

        A STOP UNDER WAY - whoever began it: an unload, the idle sweep - is
        room being made already. A load that does not fit waits it out and
        is measured again: never a second model off for that room, nor the
        load refused for it. A stop that failed meanwhile refuses the load:
        its model may still be on the card, and nothing loads while one may
        be - as before any load (`_retry_stuck`)."""
        while True:
            with self._lock:
                stuck = self._stuck[0] if self._stuck else None
                held, limit = len(self._loads) + len(self._stuck), self._limit()
            if stuck is not None:
                raise BackendError(f"refusing to load {model_key}: {stuck.model_key} "
                                   f"({stuck.load_id}) did not come off the card, and may "
                                   f"still be on it - its stop failed while this load made "
                                   f"room")
            if held >= limit:
                one_port = limit == 1 and self.backend_kind != "mock" \
                    and int(self.cfg.runner.get("port") or 0) > 0
                refusal = BackendError(
                    f"no room for {model_key}: {held} model(s) on the card, and this runner "
                    f"keeps at most {limit} at once"
                    + (" - its settings name one port for every model ([runner] port)"
                       if one_port else ""))
            else:
                try:
                    self._check_vram(model_key)
                    return
                except BackendError as exc:
                    if getattr(exc, "verdict", None) in ("never_fits", "degraded"):
                        # more than the whole card - or a card degraded, where
                        # nothing new goes on: no model taken off could make
                        # the room, so none is
                        raise
                    refusal = exc
            if any(e.stopping for e in self._loads.entries()):
                self._wait_out_stop(model_key, cancel, anyone=True)
                continue
            with self._lock:
                idle = [e for e in self._loads.up()
                        if e.model_key != model_key and not self._pinned(e)
                        and not self._loads.uses(e.model_key) and not self._anon_uses]
                victim = min(idle, key=lambda e: e.used) if idle else None
                if victim is not None:
                    self._event("room_made", model_key=victim.model_key, load=victim.load_id,
                                for_model=model_key, detail=str(refusal)[:300])
                    fields = self._stop_begun(victim)
            if victim is None:
                if getattr(refusal, "verdict", None) == "no_room":
                    room = self._room_to_wait_for(refusal, terms)
                    if room is not None:
                        # S18: scheduled work that waits for room, with
                        # nothing of this application's on the card: its
                        # queue keeps it, and nothing is said loudly
                        raise WaitsForRoom(f"{model_key} waits for room on the card: "
                                           f"{room['words']}", room)
                    # nothing more may come off: refused, and said at once
                    self._say_no_room(str(refusal))
                raise refusal
            if fields is not None:
                self._stop(victim, fields)

    def _stop_begun(self, entry: _Load):
        """Under the manager's lock: *entry*'s stop begins - it is handed out
        no more, and no other stop is asked of it - and the record says so.
        -> its record's fields, or None when it is not on the card, or its
        stop has begun already."""
        if entry.stopping or not entry.up or self._loads.of(entry.model_key) is not entry:
            return None
        entry.stopping = True
        entry.asked_off = time.monotonic()
        self._loads.moves += 1
        fields = self._load_fields(entry)
        self._event("unload_start", **fields)
        return fields

    def _stop(self, entry: _Load, fields: dict) -> None:
        """Outside the manager's lock: *entry*'s process is stopped - the stops
        of different models run together, and nothing waits on the manager
        while one does - and the record says how it went. The load leaves the
        table either way, and the lease with the last of them. A stop that
        fails raises: the model may still be on the card, so the record keeps
        it open and says why, its port is NOT given back - what still listens
        on it may be the process that would not stop - and neither is the
        card: it is kept while any such model may still be on it. Its process
        stays named to the card source, and its stop is asked again before
        anything else loads and when a pipeline closes (`_retry_stuck`).

        ITS UNLOAD IS TIMED (PLAN-4 D12): from its asking to its process gone
        (`unload_s`); and for a load whose memory was measured, on to that
        memory back on the card (`_memory_back`), against a look at the card
        before its stop."""
        ref = None
        if entry.mib is not None:
            free = self._free_mib()
            if free is not None:
                with self._lock:
                    ref = (free, self._loads.moves)
        try:
            entry.backend.stop()
        except Exception as exc:
            with self._lock:
                # said while it still counts: it may still be on the card
                self._event("unload_failed", **fields, detail=str(exc)[:500])
                self._loads.end(entry)
                self._stuck.append(entry)
                if self._rental() is not None:
                    self._event("card_kept", model_key=entry.model_key,
                                detail="the model may still be on the card")
                self._release_lease_if_empty()
            self._notify()
            raise
        gone = time.monotonic()
        with self._lock:
            self._loads.end(entry)
            self._release_lease_if_empty()
            self._event("unload_ok", **fields)
        if entry.asked_off is not None:
            self._timed(entry.model_key, "unload", {"unload_s": gone - entry.asked_off},
                        load=entry.load_id)
            if ref is not None:
                threading.Thread(target=self._memory_back, args=(entry, ref), daemon=True,
                                 name=f"memory-back-{entry.model_key}").start()
        self._card_stopped(entry)
        self._give_port_back(entry.port, entry.model_key)
        self._notify()

    def _take_off(self, entries) -> None:
        """Stop each of *entries* - each begun under the manager's lock, each
        stopped outside it. Every stop is asked; the first that failed is
        raised once all were."""
        with self._lock:
            begun = [(e, f) for e in entries for f in (self._stop_begun(e),) if f is not None]
        failed = None
        for entry, fields in begun:
            try:
                self._stop(entry, fields)
            except Exception as exc:                        # noqa: BLE001 - raised below
                failed = failed or exc
        if failed is not None:
            raise failed

    def _release_lease_if_empty(self) -> None:
        """The hold dies with the last model: once the table holds no load,
        the lease goes - even when a stop threw. Under the manager's lock."""
        if not len(self._loads):
            self._lease.release()

    def _retry_stuck(self):
        """Ask again the stop of every model whose stop failed - the record
        saying each is asked `again`. One that goes is off the card source's
        list, and its port goes back. -> None when none is left; else (the
        load, its error) of the first that still will not go: it stays named,
        and the card kept. One caller at a time; each stop outside the
        manager's lock."""
        with self._stuck_lock:
            for entry in list(self._stuck):
                fields = {**self._load_fields(entry), "again": True}
                self._event("unload_start", **fields)
                try:
                    entry.backend.stop()
                except Exception as exc:                    # noqa: BLE001 - answered
                    self._event("unload_failed", **fields, detail=str(exc)[:500])
                    return entry, exc
                with self._lock:
                    self._stuck.remove(entry)
                    self._event("unload_ok", **fields)
                self._card_stopped(entry)
                self._give_port_back(entry.port, entry.model_key)
                self._notify()
        return None

    def _notify(self) -> None:
        """Wake every wait for a model or a pipeline: something moved. Never
        called with the pipeline's lock held."""
        with self._news:
            self._news.notify_all()

    def _wait_until(self, done, wait_s: float, stop_ends: bool = True) -> None:
        """Wait until `done()`, for at most *wait_s* seconds - or, when
        `stop_ends` (an answer's wait), until the server stops. `done` reads
        without a lock."""
        deadline = time.monotonic() + max(0.0, float(wait_s))
        with self._news:
            while not done() and not (stop_ends and self._stopping):
                left = deadline - time.monotonic()
                if left <= 0:
                    return
                self._news.wait(min(left, 0.25))

    def _take_card(self, holder, model_key, for_, cancel, on_wait, terms=None,
                   pipe=None) -> None:
        """Hold the card for a load - or for pipeline *pipe*'s own thread. A
        pipeline that holds it already is enough: nothing is asked. Otherwise
        the card source is asked, and never gone on without: with no pipeline
        open, the queue's own is opened for the work first, with the terms of
        the request that opens it. The terms go to the source by hand, when
        there are some; a refusal's own reason comes back on them and on the
        error. A cancel is a cancel, whatever the source raised when it saw
        it. A wait is on the record once the card is had: how long, and what
        it was told while it waited."""
        opened = None
        while True:
            with self._pipe_lock:
                cur = self._pipe
                closing = pipe is None and cur is not None and cur.open and cur.closing
                if not closing:
                    if pipe is None:
                        if self._holds_now(cur):
                            return
                        if cur is None or not cur.open:
                            klass = (terms.klass if terms is not None else None) or "responsive"
                            opened = cur = _Pipeline(ids.mint_pipeline(), "queue", holder, klass,
                                                     None, (), None, self._clock())
                            self._pipe = cur
                    target = pipe if pipe is not None else cur
                    break
            # a close is under way, and gives the card back: wait for it to have
            # - the source may still answer with the rental it is releasing -
            # and take the card afresh. A cancel ends this wait too: the close
            # may be the very one this load was given up for
            self._after_close(model_key, cancel)
        # a close may be under way still - the one before a pipeline opened in
        # its moment, which is no longer the pipeline this sees: its stops run
        # outside the manager's lock now, and the card it is about to give back
        # must not answer this take. Waited out before the card is asked
        self._after_close(model_key, cancel)
        if opened is not None:
            self._event("pipeline_opened", **opened.opened())
        what = model_key or f"the pipeline {target.id}"
        self._loads.wait_begins(model_key)
        waited = {"first": None, "last": None}

        def heard(text):
            if waited["first"] is None:
                waited["first"] = time.monotonic()
            waited["last"] = text
            if on_wait is not None:
                on_wait(text)
        failure = None
        asked = time.monotonic()
        if terms is not None and terms.klass == "scheduled":
            # S18: what it does when its model does not fit, said to the card
            # source as decided here - its own, else the server's setting
            terms.if_no_room = self.if_no_room(terms)
        try:
            rental = self._card.take(holder, model_key, for_=for_, cancel=cancel, on_wait=heard,
                                     **({"terms": terms} if terms is not None else {}))
            had = time.monotonic()
        except Exception as exc:                            # noqa: BLE001 - refused, said
            # the request's own reason, when the source refused for one: on
            # the error, and on the terms the request handed over
            reason = getattr(exc, "reason", None)
            if terms is not None:
                terms.refused = reason
            room = getattr(exc, "room", None)
            if cancel is not None and cancel.is_set():
                self._event("card_wait_cancelled", model_key=model_key, pipeline=target.id)
                failure = Cancelled(f"cancelled while waiting for the card for {what}")
            elif isinstance(room, dict) and self._parks(terms):
                # S18: passed over for room, and it waits in its queue: its
                # place in line kept by the card source, nothing loud said
                self._event("card_waits_for_room", model_key=model_key, pipeline=target.id,
                            detail=str(room.get("words"))[:500])
                failure = WaitsForRoom(f"no card for {what} yet: {exc}", room)
            else:
                self._event("card_refused", model_key=model_key, pipeline=target.id,
                            detail=str(exc)[:500])
                failure = NoCard(f"no card for {what}: {exc}", reason=reason)
                if reason == "no_room":
                    # its ticket refused at its claim for the card's memory
                    self._say_no_room(str(failure))
        finally:
            self._loads.wait_ends(model_key)
        if failure is not None:
            # the queue's own pipeline that never held the card, and that
            # nothing else waits on, closes with the refusal
            self._never_held(target, str(failure))
            raise failure
        if waited["first"] is not None:
            self._event("card_waited", model_key=model_key, pipeline=target.id,
                        waited_s=round(time.monotonic() - waited["first"], 2),
                        waiting_for=str(waited["last"])[:300], **({"for": for_} if for_ else {}))
        # every take of a real card is on the record - a rental names its
        # ticket, a lock does not, and a take nothing waited for left no line
        # at all (found by the real-card rerun: the record could not show the
        # machine's lock was ever taken). The stand-in's source has no kind:
        # nothing was taken.
        source = getattr(self._card, "kind", None)
        if rental is not None or source:
            self._event("card_granted", model_key=model_key, source=source, pipeline=target.id,
                        **({"ticket": rental.get("id")} if rental is not None else {}),
                        **({"for": for_} if for_ else {}))
        with self._pipe_lock:
            if target is self._pipe and target.open and not target.closing \
                    and target.state not in ("holding", "degraded"):
                # held - and degraded, when the rental it holds is (D3)
                said = self.degraded
                target.state, target.why, target.since = \
                    ("degraded", said, self._clock()) if said else ("holding", None, self._clock())
        return self._take_timed(asked, had) if source else None

    def _take_timed(self, asked, had) -> dict:
        """What a take of a real card measured, for the load it was made for
        (PLAN-4 D12): when it began (`started`), its claim - as long as the
        source says its claim took (`claim_s`, a coordinator's) - and its
        wait, the rest of it. The stand-in's source has no kind: nothing was
        taken, and nothing waited for."""
        claim = getattr(self._card, "claim_s", None)
        if isinstance(claim, bool) or not isinstance(claim, (int, float)) \
                or not math.isfinite(claim) or not 0 <= claim <= had - asked:
            claim = None
        took = {"started": asked, "load_wait_s": had - asked - (claim or 0.0)}
        if claim is not None:
            took["load_claim_s"] = claim
        return took

    def _after_close(self, model_key, cancel) -> None:
        """Wait until no close is under way - the close lock free - or the
        cancel is set. A close joins its pipeline's own thread after setting
        its stop, so that thread's wait here ends at once."""
        while not self._close_lock.acquire(timeout=0.05):
            if cancel is not None and cancel.is_set():
                raise Cancelled(f"cancelled while waiting for the card for "
                                f"{model_key or 'a pipeline'}")
        self._close_lock.release()

    def _holds_now(self, pipe) -> bool:
        """Does *pipe* hold the card this moment - open, not closing, holding
        it, and the card not taken back? Asked under the pipeline lock."""
        return (pipe is not None and pipe.open and not pipe.closing
                and pipe.state in ("holding", "degraded") and not self._card.lost)

    def _pipe_holds(self) -> bool:
        """Does the open pipeline hold the card, as far as this server knows?
        A load goes up only then. A loss the source has heard, and the
        pipeline not yet, is the load's own check."""
        with self._pipe_lock:
            p = self._pipe
            return (p is not None and p.open and not p.closing
                    and p.state in ("holding", "degraded"))

    def _never_held(self, pipe, why: str) -> None:
        """A take was refused. The queue's own pipeline that never held the
        card, with nothing else waiting on it, is over: closed, saying why.
        An application's pipeline waits on, by its own thread."""
        with self._pipe_lock:
            if (pipe is None or pipe is not self._pipe or pipe.opened_by != "queue"
                    or not pipe.open or pipe.closing or pipe.state != "waiting"
                    or self._loads.waiting):
                return
            pipe.open = False
            pipe.state, pipe.why, pipe.since = "closed", why[:500], self._clock()
        self._event("pipeline_closed", pipeline=pipe.id, why=pipe.why)

    def _card_running(self, pid, proof, model_key=None, port=None, mib=None) -> None:
        """A load's process exists: tell the card source - its pid, the proof
        its command line carries, the model, the model's own engine and the
        port, and once it is measured the memory it took (`mib`, D12) - which
        may say so to a coordinator. Nothing here may fail the load."""
        tell = getattr(self._card, "running", None)
        if tell is None:
            return
        try:
            engine = self.cfg.engine(model_key) if model_key is not None else self.backend_kind
            tell(pid, proof, model_key, engine, port, **({"mib": mib} if mib is not None else {}))
        except Exception as exc:                            # noqa: BLE001
            self._event("card_running_failed", pid=pid, detail=str(exc)[:500])

    def _card_stopped(self, entry: _Load) -> None:
        """A load's process has gone - its stop returned: tell the card
        source, which takes it off the list it names. Never told for a stop
        that failed. Nothing here may fail what it follows."""
        tell = getattr(self._card, "stopped", None)
        if tell is None:
            return
        try:
            tell(entry.load_id)
        except Exception as exc:                            # noqa: BLE001
            self._event("card_stopped_failed", load=entry.load_id, detail=str(exc)[:500])

    def _rental(self):
        """The rental the card source holds, if it holds one - read by the
        name the coordinator's source keeps it under; a lock, and the
        stand-in, hold none."""
        return getattr(self._card, "turn", None)

    def _give_card_back(self, model_key, pipe=None) -> None:
        """The card goes back as its pipeline closes, named by the model it
        last loaded. Never fails what it follows. A rental taken back was not
        given back; a lock not held gives nothing."""
        rental, lost = self._rental(), getattr(self._card, "lost", None)
        source, held = getattr(self._card, "kind", None), getattr(self._card, "held", False)
        named = {"pipeline": pipe.id} if pipe is not None else {}
        try:
            self._card.give_back()
        except Exception as exc:                            # noqa: BLE001
            self._event("card_give_back_failed", model_key=model_key, detail=str(exc)[:500],
                        **named)
            return
        if rental is not None and not lost:
            self._event("card_given_back", model_key=model_key, source=source,
                        ticket=rental.get("id"), **named)
        elif rental is None and source and not lost and held:
            self._event("card_given_back", model_key=model_key, source=source, **named)

    def abandon(self, reason: str, model_key=None) -> bool:
        """Give up every load under way - or *model_key*'s alone - where it
        stands, WITHOUT THE LOCK. A stop, or a card taken back, must never wait
        out a load nobody will use (found on the real card: a stop waited
        10.2 s for a load of 8.2, and a card the coordinator had taken back
        stayed loaded 9.4 s). The load then fails in these words through
        `acquire`'s own failure path, which gives the port back as for any
        failed load. A model already up is not touched: taking it off is
        `unload`'s. -> whether a load under way was given up."""
        given = False
        for entry in self._loads.entries():
            if entry.up or (model_key is not None and entry.model_key != model_key):
                continue
            give_up = getattr(entry.backend, "abandon", None)
            if give_up is not None:
                give_up(str(reason))
                given = True
        return given

    def stopping(self, reason: str) -> None:
        """The server is going away: from now on nothing is loaded - `acquire`
        refuses in these words, a wait for the card or for the load lock
        included - a load under way is given up at once, and every wait for a
        model is answered. A model already up stays until `shutdown` takes it
        off."""
        self._stopping = str(reason)
        stop_asking = getattr(self._card, "stop_asking", None)
        if stop_asking is not None:
            stop_asking()                                   # a wait for the card ends
        self.abandon(reason)
        self._notify()

    def _port_lost(self, port, reason) -> None:
        """The registry took a port back. Said - naming the load that held
        it, under way or up - and nothing more: what still listens on it keeps
        it from being handed out again."""
        held = next((e for e in self._loads.entries() if e.port == port), _NO_LOAD)
        self._event("port_lost", port=port, reason=reason, model_key=held.model_key)

    def _card_lost(self, reason, error) -> None:
        """An ANSWER said the card was taken back. Another holder may be
        loading onto it now, so a load under way is given up at once - here,
        on the heartbeat's thread, since giving up takes no lock - and every
        model already up comes off at once, on a thread of its own: taking one
        off takes the lock, which the heartbeat must never wait on. The
        pipeline is lost: the queue's own is over, and an application's asks
        for the card again by itself - nothing loads until it is had."""
        with self._pipe_lock:
            pipe = self._pipe
            if pipe is not None and pipe.open and not pipe.closing:
                pipe.state, pipe.since = "lost", self._clock()
                pipe.why = f"the card was taken back ({reason}): {error}"[:500]
                if pipe.opened_by == "queue":
                    pipe.open = False                       # its next work opens another
            else:
                pipe = None
        self.abandon(f"the card was taken back ({reason})")
        self._notify()
        threading.Thread(target=self._after_card_lost, args=(reason, error, pipe), daemon=True,
                         name="card-lost").start()

    def _after_card_lost(self, reason, error, pipe=None) -> None:
        named = {"pipeline": pipe.id} if pipe is not None else {}
        self._event("card_lost", model_key=(self._loads.loaded() or _NO_LOAD).model_key,
                    reason=reason, detail=str(error)[:500], **named)
        if pipe is not None:
            self._event("pipeline_lost", pipeline=pipe.id, reason=reason, detail=str(error)[:500])
        try:
            self.unload()
        except Exception as exc:                            # noqa: BLE001
            self._event("unload_after_card_lost_failed", detail=str(exc)[:500])
        if pipe is not None and pipe.opened_by != "queue":
            self._ask_again(pipe)

    def _ask_again(self, pipe) -> None:
        """An application's pipeline, lost, asks for the card again by itself,
        on a thread of its own - and loads its models once it has it."""
        with self._pipe_lock:
            if pipe is not self._pipe or not pipe.open or pipe.closing or self._stopping:
                return
            pipe.thread = threading.Thread(target=self._run_pipeline, args=(pipe, None, False),
                                           daemon=True, name=f"pipeline-{pipe.id}")
            thread = pipe.thread
        thread.start()

    def _note_load(self, entry: _Load, cold: bool, seconds: float) -> None:
        """Record the load; say so out loud when it was slow. Nothing here may
        fail a load that already succeeded: a failure is an event. Its normal
        and its flag stay None and False when nothing could judge it."""
        model_key = entry.model_key
        entry.cold = cold
        if self.loads_path is None:
            return
        try:
            note = note_load(self.loads_path, model_key, cold, seconds, self._clock())
        except Exception as exc:                            # noqa: BLE001
            self._event("load_record_failed", model_key=model_key, detail=str(exc)[:500])
            return
        entry.baseline, entry.slow = note["baseline"], bool(note["slow"])
        if entry.slow:
            self._event("load_slow", model_key=model_key, cold=cold,
                        load_seconds=round(seconds, 3),
                        baseline_seconds=round(note["baseline"], 3), samples=note["n"])
            # printed as well: the launch console is where a person watches a
            # start, and the event is what survives nobody watching
            print(f"[runner] {SLOW_LOAD_WARNING} - {model_key} took {seconds:.0f}s and "
                  f"normally takes about {note['baseline']:.0f}s here", flush=True)

    def touch(self, model_key=None) -> None:
        """A generation just used a model - *model_key*, or every model up
        (None): its idle clock starts again."""
        with self._lock:
            now = self._clock()
            for entry in self._loads.up():
                if model_key is None or entry.model_key == model_key:
                    self._loads.used_now(entry, now)
            self._loads.last_used = now

    @contextlib.contextmanager
    def in_use(self, model_key=None):
        """Hold *model_key* against the idle sweep, and against making room,
        from before `acquire` until the generation has ended - a sweep between
        the two, or during a long silence before the first token, would take
        the model out from under it. A use that names no model holds every
        model. The model's idle clock starts again when the use ends. A use is
        WORK for the queue's own pipeline; when it ends, the pipeline is asked
        whether its work is done."""
        with self._lock:
            self._loads.use_begins(model_key)
            if model_key is None:
                self._anon_uses += 1
        try:
            yield
        finally:
            with self._lock:
                if model_key is None:
                    self._anon_uses -= 1
                self._loads.use_ends(model_key, self._clock())
            self._settle_quietly()

    def unload(self, model_key=None) -> None:
        """Take models off the card: *model_key*'s, or every one (None). The
        models beside it stay, and the card stays with the pipeline - a switch
        never gives it back; the queue's own pipeline is then asked whether
        its work is done (`settle`), which gives it back when nothing is
        loaded and nothing is owed. Every stop is asked, each outside the
        manager's lock; the first that failed is raised once all were."""
        try:
            with self._lock:
                entries = [e for e in self._loads.up()
                           if model_key is None or e.model_key == model_key]
            self._take_off(entries)
        finally:
            self._settle_quietly()

    def maybe_idle_unload(self) -> bool:
        """Take off each model loaded on demand and unused for `idle_unload_s`
        - on its own idle clock. The lease's second promise - a holder that
        stops using the card releases it - needs no code of its own: it is
        this, through the stops. A model its pipeline keeps is never taken,
        and neither is one a generation holds. -> whether one came off."""
        limit = float(self.cfg.runner.get("idle_unload_s") or 0)
        if limit <= 0:
            return False
        taken, failed = 0, None
        try:
            while True:
                # one at a time: each chosen, and its stop begun, under one
                # hold of the manager's lock - a job cannot take it between
                with self._lock:
                    if self._anon_uses:
                        break
                    now = self._clock()
                    idle = next((e for e in self._loads.up()
                                 if not self._pinned(e) and not self._loads.uses(e.model_key)
                                 and e.last_used is not None and now - e.last_used >= limit),
                                None)
                    if idle is None:
                        break
                    self._event("idle_unload", model_key=idle.model_key, load=idle.load_id,
                                idle_seconds=round(now - idle.last_used, 1))
                    fields = self._stop_begun(idle)
                if fields is None:
                    break
                taken += 1
                try:
                    self._stop(idle, fields)
                except Exception as exc:                    # noqa: BLE001 - raised below
                    failed = failed or exc
            if failed is not None:
                raise failed
        finally:
            self._settle_quietly()
        return bool(taken)

    def shutdown(self) -> None:
        """Take every model off the card, and close the pipeline. Every load
        under way is given up first: a shutdown never waits one out. Then the
        card goes back, whatever the unload said: a card kept after an unload
        that failed is kept "until its stop works, or the server stops" - and
        this is the stop."""
        self.abandon("the runner is shutting down")
        with self._pipe_lock:
            pipe = self._pipe if self._pipe is not None and self._pipe.open else None
        closed = False
        if pipe is not None:
            with self._close_lock:
                closed = self._close_locked(pipe, "the server stopped", "pipeline_closed",
                                            force=True)
        if closed:
            return
        try:
            self.unload()
        except Exception as exc:                            # noqa: BLE001 - shutdown must not raise
            self._event("shutdown_error", detail=str(exc)[:500])
        with self._lock:
            if not self._loads.on_card():
                self._give_card_back(None)                  # nothing is up: no model to name

    # ------------------------------------------------ one model, asked by name

    def model_state(self, key) -> dict:
        """Where *key* stands (`shapes/model_state.json`): `up` with its load;
        `loading` - its weights going up, or waiting for another load, or for
        its stop to end; `waiting` for the card; `failed` with why its last
        load failed; `off`."""
        entry = self._loads.of(key)
        if entry is not None and entry.up and not entry.stopping:
            return {"key": key, "state": "up", "why": None, "load": entry.load_id}
        if key in self._failed:
            return {"key": key, "state": "failed", "why": self._failed[key], "load": None}
        pipe = self._pipe
        wanted = entry is not None or (pipe is not None and pipe.open and key in pipe.models)
        if not wanted:
            return {"key": key, "state": "off", "why": None, "load": None}
        held = pipe is not None and pipe.open and pipe.state in ("holding", "degraded")
        return {"key": key, "state": "loading" if held or entry is not None else "waiting",
                "why": None, "load": None}

    def load_model(self, holder, key, wait_s: float = 0.0) -> dict:
        """`POST /api/models/{key}/load`: load *key* and keep it (pinned) in the
        application's open pipeline, beside the models already up - on a
        thread of its own, as the pipeline's own models are - and answer where
        it stands once it is up, or its load failed, or *wait_s* has passed
        (`model_state`), with the pipeline. KeyError for a model the settings
        do not name; PipelineRefused: `no_pipeline` (none of the
        application's is open - the queue's own keeps no model),
        `too_many_models` (it keeps as many as the card holds at once),
        `stopping`."""
        self.cfg.model(key)
        with self._pipe_lock:
            if self._stopping:
                raise PipelineRefused(409, "stopping", f"no model is loaded now: {self._stopping}")
            pipe = self._pipe
            if pipe is None or not pipe.open or pipe.closing or pipe.opened_by == "queue":
                raise PipelineRefused(409, "no_pipeline",
                                      f"no pipeline is open to keep {key} in: open one "
                                      f"(/api/pipeline/open), and the models it keeps stay on "
                                      f"the card until it closes")
            if key not in pipe.models:
                limit = self._limit()
                if len(pipe.models) >= limit:
                    raise PipelineRefused(409, "too_many_models",
                                          f"{pipe.id} keeps {len(pipe.models)} models already, as "
                                          f"many as this runner keeps on the card at once: "
                                          f"unload one first")
                pipe.models = pipe.models + (key,)
            klass = pipe.klass
        self._failed = {k: v for k, v in self._failed.items() if k != key}
        self._event("model_pinned", model_key=key, pipeline=pipe.id)
        threading.Thread(target=self._load_pinned, args=(pipe, key, holder, klass), daemon=True,
                         name=f"load-{key}").start()
        self._wait_until(lambda: self.model_state(key)["state"] in ("up", "failed")
                         or not pipe.open or pipe.state == "lost", wait_s)
        with self._pipe_lock:
            return {"model": self.model_state(key), "pipeline": pipe.public(self._up_keys())}

    def _load_pinned(self, pipe, key, holder, klass) -> None:
        """A pinned model's load, on its thread: as the pipeline's own are -
        its close gives it up, and so does an unload of the model; a failure
        is said on the pipeline."""
        try:
            self.acquire(key, holder, cancel=_Either(pipe.stop, self._pin_cancel(key)),
                         terms=Terms(klass=klass, label=pipe.label,
                                     if_no_room=getattr(pipe, "if_no_room", None)), pinned=True)
        except Cancelled:
            pass
        except Exception as exc:                            # noqa: BLE001 - said on the pipeline
            with self._pipe_lock:
                if pipe.open and not pipe.closing:
                    pipe.why = f"{key} could not be loaded: {exc}"[:500]
            self._event("pipeline_load_failed", pipeline=pipe.id, model_key=key,
                        detail=str(exc)[:500])
        finally:
            self._notify()

    def unload_model(self, key) -> dict:
        """`POST /api/models/{key}/unload`: take *key* off the card, pinned or
        not - the pipeline keeps it no more, a load of it under way is given
        up - and answer once its process has gone (`model_state`, `off`). The
        models beside it, and the card, stay. KeyError for a model the
        settings do not name; PipelineRefused: `in_use` while a generation
        uses it, `unload_failed` when its stop failed - it may still be on the
        card, and stays named."""
        self.cfg.model(key)
        with self._lock:
            if self._loads.uses(key) or (self._anon_uses and self._loads.of(key) is not None):
                raise PipelineRefused(409, "in_use", f"{key} is answering a request: cancel its "
                                                     f"job, or wait for it, then unload it")
            with self._pipe_lock:
                pipe = self._pipe
                if pipe is not None and pipe.open and key in pipe.models:
                    pipe.models = tuple(k for k in pipe.models if k != key)
                    unpinned = pipe.id
                else:
                    unpinned = None
            entry = self._loads.of(key)
            if entry is not None:
                entry.pinned = False
            self._failed = {k: v for k, v in self._failed.items() if k != key}
        if unpinned is not None:
            self._event("model_unpinned", model_key=key, pipeline=unpinned)
        # a pinned load of it not yet begun ends; one under way is given up
        with self._lock:
            ending = self._pin_cancels.pop(key, None)
        if ending is not None:
            ending.set()
        self.abandon(f"{key} was unloaded", model_key=key)
        try:
            self.unload(key)
        except Exception as exc:                            # noqa: BLE001 - answered
            load = entry.load_id if entry is not None else "its load"
            raise PipelineRefused(409, "unload_failed",
                                  f"{key} ({load}) would not come off: {str(exc)[:300]}; it may "
                                  f"still be on the card, and its stop is asked again before "
                                  f"any load") from None
        self._wait_until(lambda: self._loads.of(key) is None, self.CLOSE_JOIN_S)
        # a load of it given up by this unload is no failure of the model's
        self._failed = {k: v for k, v in self._failed.items() if k != key}
        return self.model_state(key)

    def _pin_cancel(self, key) -> threading.Event:
        """The event that ends the pinned loads of *key* not yet up - set when
        it is unloaded. A fresh one once it was."""
        with self._lock:
            event = self._pin_cancels.get(key)
            if event is None or event.is_set():
                event = self._pin_cancels[key] = threading.Event()
            return event

    def _up_keys(self) -> list:
        """Every model on the card and answering, in the order they began."""
        return [e.model_key for e in self._loads.up()]

    # ------------------------------------------------------------ the pipeline

    def pipeline_status(self) -> dict:
        """Health's `pipeline`: the open one, or the last to close - or none
        ever, every field null but its state and its lists - with `up`, every
        model on the card and answering. Read under its own lock only: a load
        is under way for minutes."""
        up = self._up_keys()
        with self._pipe_lock:
            return self._pipe.public(up) if self._pipe is not None else dict(NO_PIPELINE)

    def open_pipeline(self, holder, *, models=(), klass=None, label=None, keep_s=None,
                      opened_by="application", wait_s: float = 0.0, if_no_room=None) -> dict:
        """Open the pipeline: the card held from now until it closes, and its
        `models` loaded as soon as it is had - one at a time, side by side -
        and kept. The answer comes at once - the pipeline `waiting` until it
        holds the card; its own thread asks, and loads - unless *wait_s* says
        to wait: then once every model it names is up or failed to load
        (`holding` the card, when it names none), or once *wait_s* has passed.
        `keep_s` (None: the settings') is how long it stays open without a
        renewal; 0, until it is closed. The queue's own pipeline, open, is
        taken over - card and all - and the answer says `holding`. -> the
        pipeline, as it stands. Raises PipelineRefused."""
        models = [str(k) for k in (models or [])]
        if if_no_room == "wait" and (klass or "responsive") != "scheduled":
            # S18: live work is now - it fails at once, and never waits
            raise PipelineRefused(400, "live_is_now",
                                  "live work is now: a responsive pipeline whose model does not "
                                  "fit fails at once, loudly, and never waits for room "
                                  "(if_no_room: wait); open it as scheduled work to let it wait")
        for key in models:
            if key not in self.cfg.models:
                raise PipelineRefused(400, "unknown_model",
                                      f"no model {key!r} here; the runner knows "
                                      f"{', '.join(sorted(self.cfg.models)) or 'none'}")
        twice = next((k for i, k in enumerate(models) if k in models[:i]), None)
        if twice is not None:
            raise PipelineRefused(400, "repeated_model", f"{twice} is named twice: a model is "
                                                         f"loaded once, and kept once")
        limit = self._limit()
        if len(models) > limit:
            raise PipelineRefused(400, "too_many_models",
                                  f"this runner keeps at most {limit} models on the card at once: "
                                  f"a pipeline keeps {limit} at most, not {len(models)}")
        if keep_s is None:
            keep_s = self.cfg.runner["pipeline_keep_s"]
        if isinstance(keep_s, bool) or not isinstance(keep_s, (int, float)) \
                or not math.isfinite(keep_s) or keep_s < 0:
            shown = f"{keep_s:g}" if isinstance(keep_s, (int, float)) \
                and not isinstance(keep_s, bool) else repr(keep_s)
            raise PipelineRefused(400, "bad_keep", f"keep_s is 0 - held until it is closed - "
                                                   f"or a number of seconds above 0, not {shown}")
        carried = None
        with self._pipe_lock:
            if self._stopping:
                raise PipelineRefused(409, "stopping", f"no pipeline opens now: {self._stopping}")
            cur = self._pipe
            if cur is not None and cur.open and cur.opened_by != "queue":
                raise PipelineRefused(409, "already_open",
                                      f"a pipeline is open here already: {cur.id}, opened by "
                                      f"{OPENERS[cur.opened_by]}; renew it, close it, or send "
                                      f"the work to it")
            now = self._clock()
            pipe = _Pipeline(ids.mint_pipeline(), opened_by, holder, klass or "responsive", label,
                             models, float(keep_s), now, if_no_room)
            if cur is not None and cur.open and not cur.closing:
                # the queue's own: carried over, card and all - the card never
                # goes back in between
                pipe.state, pipe.since, pipe.last_model = cur.state, cur.since, cur.last_model
                cur.open, cur.state, cur.since = False, "closed", now
                cur.why = f"carried into {pipe.id}: the application opened a pipeline of its own"
                carried = cur
            self._pipe = pipe
            pipe.thread = threading.Thread(target=self._run_pipeline, args=(pipe, carried, True),
                                           daemon=True, name=f"pipeline-{pipe.id}")
            thread = pipe.thread
        # a model it names that failed before is not failed for this open
        self._failed = {k: v for k, v in self._failed.items() if k not in models}
        thread.start()
        if wait_s:
            self._wait_until(lambda: self._ready(pipe), wait_s)
        up = self._up_keys()
        with self._pipe_lock:
            return pipe.public(up)

    def _ready(self, pipe) -> bool:
        """Is a wait on *pipe*'s open over? Every model it keeps is up or its
        load failed - or, keeping none, it holds the card - or it closed, or
        was lost. Read without a lock."""
        if not pipe.open or pipe.closing or pipe.state == "lost":
            return True
        if not pipe.models:
            return pipe.state in ("holding", "degraded")
        up = set(self._up_keys())
        return all(k in up or k in self._failed or k in pipe.failed for k in pipe.models)

    def renew_pipeline(self, pipeline_id) -> dict:
        """The application is still there: its pipeline's keep_s starts again.
        Writes nothing to the record. -> the pipeline. Raises PipelineRefused
        (`not_open`)."""
        up = self._up_keys()
        with self._pipe_lock:
            pipe = self._pipe
            if pipe is None or pipe.id != pipeline_id or not pipe.open or pipe.closing:
                raise PipelineRefused(409, "not_open", self._not_open(pipeline_id, pipe))
            pipe.renewed_at = self._clock()
            return pipe.public(up)

    def close_pipeline(self, pipeline_id, why: str = "closed by the application") -> dict:
        """Close the pipeline *pipeline_id* names: its ticket given up if it
        still waits, a load under way given up, every model off - a model
        whose stop failed before asked again - and only then the card back.
        -> the pipeline, closed. Raises PipelineRefused: `not_open`, or
        `unload_failed` when a model would not come off - the pipeline stays
        open, and holds the card it may still be on."""
        with self._pipe_lock:
            pipe = self._pipe
            if pipe is None or pipe.id != pipeline_id or not pipe.open:
                raise PipelineRefused(409, "not_open", self._not_open(pipeline_id, pipe))
        with self._close_lock:
            if not self._close_locked(pipe, why, "pipeline_closed"):
                raise PipelineRefused(409, "not_open", self._not_open(pipeline_id, pipe))
        with self._pipe_lock:
            return pipe.public()

    @staticmethod
    def _not_open(pipeline_id, pipe) -> str:
        if pipe is not None and pipe.id == pipeline_id:
            return f"{pipeline_id} is not open: {pipe.why or 'it is closing'}"
        return f"no pipeline {pipeline_id} is open here"

    def maybe_lapse(self) -> bool:
        """Close a pipeline its application stopped renewing: not renewed for
        its keep_s (0 never lapses; the queue's own has none). A lapse is a
        close, with a line of its own in the record. -> whether one lapsed."""
        now = self._clock()
        with self._pipe_lock:
            pipe = self._pipe
            if (pipe is None or not pipe.open or pipe.closing or pipe.opened_by == "queue"
                    or not pipe.keep_s):
                return False
            silent = now - pipe.renewed_at
            if silent <= pipe.keep_s:
                return False
        why = f"lapsed: not renewed for {silent:.0f}s (its keep_s is {pipe.keep_s:g})"
        with self._close_lock:
            try:
                return self._close_locked(pipe, why, "pipeline_lapsed",
                                          silent_s=round(silent, 1), keep_s=pipe.keep_s)
            except PipelineRefused as exc:
                self._event("pipeline_lapse_failed", pipeline=pipe.id, detail=str(exc)[:500])
                return False

    def settle(self) -> bool:
        """Is the queue's own pipeline's work done? Asked whenever work ends
        - an acquire, a generation, an unload - when a heartbeat says the card
        is wanted, and by the idle sweep. Done is: no generation, no acquire,
        no load, no stop and no wait for the card under way, and nothing the
        queue still owes (`work_pending`). Then it closes: at once when
        nothing is loaded, or when the card is wanted - its models come off
        first - and otherwise it keeps its models warm until the idle sweep
        takes them off. Never a pipeline an application opened, and never
        while a model that would not stop may still be on the card. A
        degraded pipeline holds the card as much as one holding it (D3): its
        work done and the card wanted, it lets it go the same way. ->
        whether it closed."""
        with self._pipe_lock:
            pipe = self._pipe
            if (pipe is None or not pipe.open or pipe.closing or pipe.opened_by != "queue"
                    or pipe.state not in ("holding", "degraded")):
                return False
        if self._working():
            return False
        up = self._loads.on_card()
        if up and not getattr(self._card, "wanted", False):
            return False                                    # warm until the idle sweep
        if self._work_owed():
            return False
        why = ("its work is done, and the card is wanted" if up
               else "its work is done, and nothing is loaded")
        if not self._close_lock.acquire(blocking=False):
            return False                                    # a close is under way already
        try:
            return self._close_locked(pipe, why, "pipeline_closed", idle_only=True)
        except PipelineRefused:
            return False
        finally:
            self._close_lock.release()

    def _working(self) -> bool:
        """Is anything under way on the card - a generation, an acquire, a
        load or a wait for the card - or may a model that would not stop
        still be on it? Each is counted under the manager's lock. (A stop
        under way is not asked here: a close waits every stop out before the
        card goes back, whoever began it.)"""
        return bool(self._kept or self._acquiring or self._loads.in_use or self._loads.waiting
                    or self._loads.loading() is not None)

    def _settle_quietly(self) -> None:
        """`settle`, where nothing may fail what it follows."""
        try:
            self.settle()
        except Exception as exc:                            # noqa: BLE001
            self._event("settle_failed", detail=str(exc)[:500])

    def _heard_wanted(self) -> None:
        """A heartbeat's answer said the card is wanted. Heard on the
        heartbeat's thread, which must never wait on the manager: the
        question goes to a thread of its own."""
        threading.Thread(target=self._settle_quietly, daemon=True, name="card-wanted").start()

    def _work_owed(self) -> bool:
        """Does the queue still owe work? A question nobody can answer is a
        no: nothing is kept on the card for work nobody can name."""
        ask = self.work_pending
        if ask is None:
            return False
        try:
            return bool(ask())
        except Exception:                                   # noqa: BLE001
            return False

    def _close_locked(self, pipe, why: str, kind: str, force: bool = False,
                      idle_only: bool = False, **fields) -> bool:
        """Close *pipe*, under the close lock: stop its own thread, give up a
        load under way, take every model off - a model whose stop failed
        before asked again - and only then give the card back, once no stop
        is under way. `force` (the server stopping) gives it back whatever
        the stops said. `idle_only` (the queue's own, its work found done):
        nothing is given up, and the close is dropped if work began after it
        was decided - looked at again under the manager's lock, which a
        generation, an acquire and a load each count themselves under, and
        under which every model's stop begins: a model is never handed to a
        job once its close has begun (found by a control of the mutation run:
        a job's generation took the warm model in the moment between, and the
        close took it off under it). -> False when it was not open, or was
        dropped. Raises PipelineRefused (`unload_failed`) when a model would
        not come off: it stays open."""
        with self._pipe_lock:
            if pipe is not self._pipe or not pipe.open or pipe.closing:
                return False
            pipe.closing = True
            pipe.stop.set()
        if not idle_only:
            self.abandon(f"the pipeline {pipe.id} is closing: {why}")
        thread = pipe.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self.CLOSE_JOIN_S)
        if not idle_only:
            # each load given up ends - and one that looked before the close
            # began, and began after the give-up, or cannot be given up, comes
            # up - before the models come off: never a load going up on a card
            # given back
            self._wait_until(lambda: self._loads.loading() is None, self.CLOSE_JOIN_S,
                             stop_ends=False)
        with self._lock:
            if idle_only and self._working():
                with self._pipe_lock:
                    pipe.closing = False
                    pipe.stop = threading.Event()
                return False
            begun = [(e, f) for e in self._loads.up() for f in (self._stop_begun(e),)
                     if f is not None]
        # a model whose stop failed before is asked again - first, so a stop
        # that fails in this close is not asked twice in it: once it goes, the
        # pipeline can close (S6 R8: before, only the next load asked it)
        stuck = self._retry_stuck()
        failed = None
        for entry, fields_ in begun:
            try:
                self._stop(entry, fields_)
            except Exception as exc:                        # noqa: BLE001 - said below
                failed = failed or f"{entry.model_key} would not come off ({exc})"
        if failed is None and stuck is not None:
            failed = (f"{stuck[0].model_key} ({stuck[0].load_id}) would not come off "
                      f"({stuck[1]})")
        # a stop another caller began is waited out: the card goes back only
        # once every model has come off
        self._wait_until(lambda: not any(e.stopping for e in self._loads.entries()),
                         self.CLOSE_JOIN_S, stop_ends=False)
        if failed is None and self._kept:
            failed = "a model that would not stop may still be on the card"
        if failed is not None and not force:
            with self._pipe_lock:
                pipe.closing = False
                pipe.stop = threading.Event()               # its own thread may run again
            self._notify()
            raise PipelineRefused(409, "unload_failed",
                                  f"{pipe.id} is not closed: {failed}; the card is kept "
                                  f"while it may still be on it")
        with self._lock:
            if failed is not None:
                self._event("shutdown_error", pipeline=pipe.id, detail=failed[:500])
            self._give_card_back(pipe.last_model, pipe)
            with self._pipe_lock:
                pipe.open, pipe.closing = False, False
                pipe.state, pipe.why, pipe.since = "closed", why, self._clock()
            self._event(kind, pipeline=pipe.id, why=why, **fields)
        self._notify()
        return True

    def _pipe_waits_for_room(self, pipe, room) -> None:
        """S18: a scheduled pipeline's take waits for room - its why says the
        room it waits on, in the words a person reads while it waits."""
        with self._pipe_lock:
            if pipe.open and not pipe.closing and pipe.state == "waiting":
                first = pipe.models[0] if pipe.models else "its model"
                pipe.why = f"{first}: {room.get('words')}"[:500]
        self._notify()

    def _run_pipeline(self, pipe, carried=None, first=True) -> None:
        """A pipeline's own work, on a thread of its own: the card, asked for
        once as any load's is - its wait bounded by the server's patience -
        then the models it keeps, one by one, side by side. Its close stops it
        (`stop`): the wait ends, a load of its own is given up; so does an
        unload of a model it was about to load. A card never had closes it,
        saying why."""
        if first:
            if carried is not None:
                self._event("pipeline_closed", pipeline=carried.id, why=carried.why)
            self._event("pipeline_opened", **pipe.opened(),
                        **({"took_over": carried.id} if carried is not None else {}))
        terms = Terms(klass=pipe.klass, label=pipe.label, if_no_room=pipe.if_no_room,
                      on_room=lambda room: self._pipe_waits_for_room(pipe, room))
        try:
            with self._pipe_lock:
                had = self._holds_now(pipe)
            if not had:
                model = pipe.models[0] if pipe.models else None
                took = self._take_card(pipe.holder, model, None, pipe.stop, None, terms,
                                       pipe=pipe)
                if took is not None and model is not None:
                    # the wait and the claim are its first model's load's (D12)
                    with self._lock:
                        self._took[model] = took
        except Exception as exc:                            # noqa: BLE001 - said on the pipeline
            if pipe.stop.is_set() or self._stopping:
                return                                      # its close says why
            with self._close_lock:
                try:
                    self._close_locked(pipe, f"no card: {exc}"[:500], "pipeline_closed")
                except PipelineRefused:
                    pass
            return
        finally:
            self._notify()
        for key in list(pipe.models):
            if pipe.stop.is_set() or self._stopping:
                return
            if key not in pipe.models:
                continue                                    # unloaded before it was loaded
            try:
                self.acquire(key, pipe.holder, cancel=_Either(pipe.stop, self._pin_cancel(key)),
                             terms=Terms(klass=pipe.klass, label=pipe.label,
                                         if_no_room=pipe.if_no_room), pinned=True)
            except Cancelled:
                if pipe.stop.is_set():
                    return
            except Exception as exc:                        # noqa: BLE001 - said on the pipeline
                with self._pipe_lock:
                    if pipe.open and not pipe.closing:
                        pipe.why = f"{key} could not be loaded: {exc}"[:500]
                        pipe.failed = {**pipe.failed, key: str(exc)[:500]}
                self._event("pipeline_load_failed", pipeline=pipe.id, model_key=key,
                            detail=str(exc)[:500])
            finally:
                self._notify()


# ------------------------------------------------------------ the instrument

OPENS = ("load_ok", "load_carried")
CLOSES = ("unload_ok", "rebound_away")


def read_events(path) -> list:
    if path is None or not Path(path).exists():
        return []
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def stat_records(events) -> list:
    """The records the statistics are summed from (PLAN-4 D12): each `timed`
    line of the record, one `{"model", "measure", "value", "at"}` for each of
    its measures, in the order they were written - what `stats.rebuild` folds
    into the summary again."""
    return [{"model": e["model_key"], "measure": k, "value": v, "at": e["ts"],
             **({} if _beside_count(e) is None else {"beside": _beside_count(e)})}
            for e in events if e.get("event") == "timed"
            for k, v in (e.get("measures") or {}).items()]


def _beside_count(line: dict):
    """S17: how many other answers a `timed` line says ran beside what it
    measured - the count of the models it names - or None: it says none."""
    beside = line.get("beside")
    return len(beside) if isinstance(beside, list) else None


def answer_slowness(measures: dict, normal, factor: float, floor_s: float):
    """Was this answer FAR SLOWER than its model's *normal* (PLAN-4 D3, D12)?
    -> how many times its normal it took, or None. An answer in one piece is
    judged by its real-time factor - seconds taken per second of audio - and
    *normal* is its model's; a stream by its tokens a second once it began,
    and *normal* is its model's. Far slower is both: at least *factor* times
    its normal, AND at least *floor_s* seconds lost over what its normal
    would have taken. No normal - or one that is no speed - is no judgement,
    and neither is a stream of fewer than two tokens: it has no speed."""
    if isinstance(normal, bool) or not isinstance(normal, (int, float)) \
            or not math.isfinite(normal) or normal <= 0:
        return None
    rtf = measures.get("real_time_factor")
    if rtf is not None:
        times = rtf / normal
        lost = measures["answer_s"] - normal * measures["audio_s"]
    else:
        speed, tokens = measures.get("tokens_per_s"), measures.get("tokens")
        if speed is None or not tokens or tokens < 2 or speed <= 0:
            return None
        times = normal / speed
        lost = (measures["answer_s"] - measures["first_token_s"]) - (tokens - 1) / normal
    if times >= factor and lost >= floor_s:
        return round(times, 3)
    return None


def _close(open_loads: list, ev: dict) -> None:
    """Close the load *ev* names - by its id, or the latest when it names
    none."""
    load = ev.get("load")
    for i in range(len(open_loads) - 1, -1, -1):
        if load is None or open_loads[i].get("load") == load:
            del open_loads[i]
            return


def max_concurrency(events: list) -> int:
    """The most loads of ONE MODEL open at once within one writer of this
    record - never more than 1: models run side by side, but two loads of
    one model would be two processes answering for it. A `session_start`
    begins a new writer: a new process cannot inherit a loaded model, only an
    orphan, which `orphaned_loads` counts - and a load a rebind carried in
    says so on its own line. `side_by_side` is the most loads of any models."""
    open_loads, peak = [], 0
    for ev in events:
        kind = ev.get("event")
        if kind == "session_start":
            open_loads = []
        elif kind in OPENS:
            open_loads.append(ev)
            mine = [o for o in open_loads if o.get("model_key") == ev.get("model_key")]
            peak = max(peak, len(mine))
        elif kind in CLOSES:
            _close(open_loads, ev)
    return peak


def side_by_side(events: list) -> int:
    """The most loads open at once within one writer, whatever their models:
    how many models the card held side by side."""
    open_loads, peak = [], 0
    for ev in events:
        kind = ev.get("event")
        if kind == "session_start":
            open_loads = []
        elif kind in OPENS:
            open_loads.append(ev)
            peak = max(peak, len(open_loads))
        elif kind in CLOSES:
            _close(open_loads, ev)
    return peak


def beside_faults(events: list) -> list:
    """Every `load_ok` whose `beside` does not name exactly the loads the
    record held open as it came up - one sentence each; [] when every load
    names what else was up. A load that names nothing beside it names []."""
    open_loads, faults = [], []
    for ev in events:
        kind = ev.get("event")
        if kind == "session_start":
            open_loads = []
        elif kind in OPENS:
            if kind == "load_ok":
                said = ev.get("beside")
                held = sorted(o.get("load") for o in open_loads)
                named = (sorted(b.get("load") for b in said if isinstance(b, dict))
                         if isinstance(said, list) else None)
                if named != held:
                    faults.append(f"{ev.get('load')} ({ev.get('model_key')}) names "
                                  f"{'nothing' if named is None else named} beside it; the "
                                  f"record held {held} open")
            open_loads.append(ev)
        elif kind in CLOSES:
            _close(open_loads, ev)
    return faults


def orphaned_loads(events: list) -> list:
    """Loads whose writer ended without unloading them or taking them away."""
    open_loads, orphans = [], []
    for ev in events:
        kind = ev.get("event")
        if kind == "session_start":
            orphans.extend(open_loads)
            open_loads = []
        elif kind in OPENS:
            open_loads.append(ev)
        elif kind in CLOSES:
            _close(open_loads, ev)
    return orphans


# -------------------------------------------------------------- load timing
#
# How long a load NORMALLY takes on this machine is measured, never assumed:
# per model and per class (cold - the first load of it by a process - or warm),
# from this machine's own history. A load far above that normal says so.

#: samples kept per (model, class): weeks of loads, and a CAP - a record read
#: whole on every load must not grow without end
LOAD_SAMPLES_KEPT = 50
#: below this many samples there is no baseline, so no warning: at three, a
#: median ignores one outlier
LOAD_BASELINE_MIN = 3
#: "far above normal" is a factor AND a floor, both required: the factor alone
#: warns on a stand-in whose loads take a second; the floor alone warns on an
#: ordinary slow morning
LOAD_SLOW_FACTOR = 2.0
LOAD_SLOW_FLOOR_S = 10.0
#: worded for the person at the machine, on purpose: it names what to check,
#: not a number to interpret
SLOW_LOAD_WARNING = "Model loading is slow, check computer"


def load_bucket(model_key: str, cold: bool) -> str:
    return f"{model_key}::{'cold' if cold else 'warm'}"


def load_baseline(samples):
    """The median of the samples - never the mean, which one pathological
    load would drag up for ever - or None below the minimum count. None is
    not 0.0: a zero baseline would make every load slow."""
    values = sorted(float(s) for s in (samples or []))
    if len(values) < LOAD_BASELINE_MIN:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2.0


def load_is_slow(seconds, baseline) -> bool:
    if baseline is None:
        return False
    seconds, baseline = float(seconds), float(baseline)
    return seconds >= baseline * LOAD_SLOW_FACTOR and seconds - baseline >= LOAD_SLOW_FLOOR_S


def read_loads(path) -> dict:
    """The record, by bucket; {} for missing, unreadable or corrupt. Never
    raises: a timing record must never stop a model loading."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    buckets = raw.get("buckets") if isinstance(raw, dict) else None
    return buckets if isinstance(buckets, dict) else {}


#: one fold of the load record at a time in this process: two loads beside
#: each other would read, edit and write it back together, and one would lose
#: the other's sample
_NOTE_LOCK = threading.Lock()


def note_load(path, model_key: str, cold: bool, seconds: float, at=None) -> dict:
    """Fold one load into the record. -> {bucket, cold, seconds, baseline,
    slow, n}, where the BASELINE IS THE NORMAL BEFORE THIS LOAD: a load is
    judged against its history, never against itself. The record holds times
    and model keys only - no path, nothing a folder's owner wrote. Read,
    edited and written back under one lock, through a temporary file of its
    own - never a name another write could be using."""
    with _NOTE_LOCK:
        return _note_load_locked(Path(path), model_key, cold, seconds, at)


def _note_load_locked(path: Path, model_key: str, cold: bool, seconds: float, at) -> dict:
    buckets = read_loads(path)
    key = load_bucket(model_key, cold)
    entry = buckets.get(key) if isinstance(buckets.get(key), dict) else {}
    samples = [s for s in (entry.get("samples") or [])
               if isinstance(s, (int, float)) and not isinstance(s, bool)]
    baseline = load_baseline(samples)
    slow = load_is_slow(seconds, baseline)
    over = len(samples)
    samples = (samples + [round(float(seconds), 3)])[-LOAD_SAMPLES_KEPT:]
    buckets[key] = {"model": model_key, "cold": bool(cold), "samples": samples,
                    "n": int(entry.get("n") or 0) + 1, "last": round(float(seconds), 3),
                    "last_at": float(at if at is not None else time.time())}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.name}.{os.getpid()}.{threading.get_ident()}.{os.urandom(4).hex()}.tmp"
    try:
        tmp.write_bytes(json.dumps({"version": 1, "buckets": buckets}, ensure_ascii=False,
                                   indent=1).encode("utf-8"))
        os.replace(tmp, path)               # whole, never half: a half file reads as empty
    finally:
        if tmp.exists():
            tmp.unlink()
    return {"bucket": key, "cold": bool(cold), "seconds": float(seconds),
            "baseline": baseline, "slow": slow, "n": over}
