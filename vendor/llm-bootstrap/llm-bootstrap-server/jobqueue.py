"""The job queue: one bound folder, one worker - each model one answer at a
time, different models at the same time.

    Standard library only. The application talks to the queue through HOOKS
    and never the other way round; the model talks to it through a BACKEND.

WHAT THE QUEUE OWNS, AND NOTHING ELSE

    answers at once  the worker starts each job on a thread of its own: jobs
                   on different models answer together, each model one at a
                   time, at most `answers_at_once` in all (S17)
    ordering       interactive before batch, then first come first served
    dependencies   `after`: a job runs only once every job it names has landed
    fans          `group`: jobs sharing a group are merged once all of them end
    cancel, give-up, retry, claim-exactly-once, forget, prune
    persistence    one JSON file in the bound folder, written whole and swapped
    recovery       what a stopped server left behind is settled before work starts
    stall watch    a generation that stops moving is failed, and says so
    supervision    a worker that dies is replaced; one that is wedged is retired

WHAT THE APPLICATION OWNS: everything a job MEANS. The queue never reads a
job's `input`. It hands it to the hooks, which are the application's whole
interface to the queue:

    build_request(job, after_results) -> request
        Called when the job's turn comes - after every job in `job["after"]`
        has landed - so a request can be built from its predecessors' ACTUAL
        output (`after_results` maps each of those ids to its result).
    land_result(job, result) -> None | result
        The application takes a finished result. Returning a dict replaces
        what the queue stores and offers (parsed fields, a report). Called
        before the job reads as `done`, so nothing is offered half-landed.
    sampling_for(kind, input) -> dict
        Generation parameters the application chooses per kind and input
        (sampler fields, a seed). They are merged over the request's `gen`,
        and held to the contract's list as `gen` is: a parameter it does not
        name fails the job before the backend is asked - and so does one of
        the queue's own three (`model`, `n`, `retries`), which the job's
        record names as it was submitted.

    and two more, because three was not enough - README.md says why each
    exists:

    merge(group, members) -> None
        A fan has SETTLED: every member has ended. Called once per settling,
        including when a member failed or was cancelled, because an
        application that is only told about successes can never learn that
        its fan stopped. `members` is a list of {"job", "result"} in the order
        the fan was submitted; a member that did not land has status failed
        or cancelled.
    judge(job, candidate) -> None | {"why": str|None, ...}
        The application's verdict on one attempt. None: not judged. A `why`:
        refused, and the queue asks again with a new seed, at most RETRY_MAX
        attempts. A refused LAST attempt fails the job, its error the judge's
        why and its reason `judge_refused`: an answer the application does
        not accept never stands, is never landed and nothing waiting on it
        runs. The request's record
        keeps every attempt, refused ones too.

    and a sixth, because `land_result` only ever hears about success:

    ended(job, result) -> None
        A job has ended - done, failed or cancelled - however it ended: by
        its worker, a cancel, a give-up, a stall, a restart, or a job it
        waited on failing. Told once per ending, in the order they ended,
        AFTER `land_result` for a job that succeeded, and before its fan's
        merge. AT LEAST ONCE: the debt is written with the ending and paid
        only once the hook has returned, so a server that stops in between
        tells it again when it starts - the hook must be idempotent by job
        id. `job` is the record as it is when told, so a job claimed before
        it was told reads `claimed`.

    and a seventh, for the record every job leaves:

    manifest(job, after_results) -> dict
        The inputs this request was built from, named - files, versions,
        hashes, whatever the application would need to tell later what the
        request was made of. Asked once per run, when the request is built.
        The default names none: the record's shape travels, its content is
        the application's.

THE REQUEST RECORD. Every job leaves one in the bound folder, named by its
correlation id - `requests/<correlation id>/`:

    manifest.json      the job as its run started, what it waited on, and the
                       inputs `manifest` names
    request.json       the request as built
    events.jsonl       the request's own story: submitted, started, each
                       attempt started and ended, ended - however it ended
    samples/<NN>/      one per attempt: `params.json` (what it was sent with,
                       its own seed), `candidate.json` (what came back, and the
                       judge's verdict), and whatever the backend kept about it
                       with `run.record(name, data)` - the runner keeps its
                       exact call there

The record outlives the job: forgetting or pruning a job takes it out of the
queue, never out of the record. Writing it never fails what it records - a
record that cannot be written is said in the log, and the job goes on.

A hook object is anything with those methods; `Hooks` below is a base with
harmless defaults. Hooks are never called while the store is locked, so a hook
may call back into the queue (a merge that submits the next level, say).

HOOKS RUN ON SEVERAL THREADS AT ONCE (S17). Jobs on different models answer at
the same time, each on a thread of its own, so two jobs' `build_request`,
`sampling_for`, `manifest`, `judge` and `land_result` may be called together
- and `ended` and `merge` may come from a cancel, a reaper or any job's
thread. A hook that keeps state of its own holds a lock of its own around
it. The defaults (`Hooks`) keep none, and are safe as they are.

ONE LIVE QUEUE PER FOLDER. A queue holds an operating-system lock on the
folder for as long as it is bound (`FolderLock`); a second queue - this
process's or another's - is refused with `FolderInUse` rather than left to
settle the first one's work as a stopped server's. The lock is let go by
`close()`, and by the operating system when the process dies, however it dies.

THE BACKEND is the model: `backend(request, params, run) -> candidate`, a dict
with at least "text". `run.handle` names this one generation, `run.cancelled`
is an Event the backend should watch, and `run.report(**fields)` publishes
progress (any change counts as movement for the stall watch). Optional:
`backend.cancel(handle)`, `backend.loading() -> bool` (a model load is not a
stall), `backend.recover(handle) -> candidate|None` (did this finish before the
server stopped?), `backend.check_request(request, params)` (asked at submit
when the request is the job's input - the default build hook's - it raises
`Refused` for a request its model can never answer, which is then never
stored). A backend raises `Cancelled` when it noticed the cancel and
`GenerationFailed` (or anything else) when the generation went wrong.

A BACKEND IS ASKED FROM SEVERAL THREADS AT ONCE (S17): one generation per model
at a time, generations of different models together. Optional, for that:
`backend.models_up() -> [model]` (the models up now: a job on any other waits
while answers run, since its load could need their room, and once it has
started nothing behind it starts until its model is up (S17b) - a backend
without it has every model start at once), `backend.loading_for(model) -> bool` (the
stall watch's question, for the quiet job's own model - asked before
`loading()`).

THE RECORD. `jobs.json` holds {"seq", "jobs": [row...], "groups", "rejected"}.
Every row is {"job": <the public job record>, "input", "gen", "result", "run",
"card_patience_s"}: `row["job"]` is exactly the contract's `job` shape, and the
rest is the queue's own. Rows the queue cannot read are kept verbatim under
"rejected" and never destroyed.

WHAT A JOB SAYS ABOUT THE CARD goes to the backend with every generation, on
`run`: its priority - an interactive job's card is needed now, a batch job's
can wait; a job that says none is interactive (S18b: only `batch`, said,
makes a job scheduled) - and its own patience (`card_patience_s`, None for
the server's).
A BATCH JOB MAY WAIT FOR ITS TIME AND FOR ROOM (S18), queued, holding back
nobody: `not_before`, and `if_no_room` on `run` - see *scheduled work* below
(`_held_back`, `_look`, `_park`).
A generation that failed for a reason of its own (`GenerationFailed.reason`)
ends its job with that `reason`, for a program to branch on.
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import struct
import sys
import threading
import time
from pathlib import Path

# The id grammar every part shares, and the validator whose words a refusal
# for a malformed id uses - both from the contract, beside this part.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "llm-bootstrap-contract"))
import ids                                                      # noqa: E402
import validate as _V                                           # noqa: E402
import memory as _MEM                                           # noqa: E402  (the loud words)

STATUSES = ("queued", "running", "held", "done", "failed", "cancelled", "claimed")
FINISHED = ("done", "failed", "cancelled", "claimed")
LANDED = ("done", "claimed")
PRIORITIES = ("interactive", "batch")
RETRY_MAX = 3
STORE_NAME = "jobs.json"
LOCK_NAME = "jobs.lock"
#: where each job's request record lives in the bound folder
REQUESTS_DIR = "requests"
#: a request's own story, one JSON line per event
RECORD_EVENTS = "events.jsonl"
#: the files of an attempt's folder that are the queue's own
QUEUE_SAMPLE_FILES = ("params.json", "candidate.json")
#: a name a backend may keep something under: a plain file name, nothing more
RECORD_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

#: What an untouched request's `gen` gets. What `gen` may carry at all is the
#: contract's list (`GEN_FIELDS`, below).
#: A TEMPERATURE IS SET ONLY WHEN ASKED FOR: a job that names none runs at its
#: model's own. A default stamped here would make every temperature a model's
#: settings name a setting that looks as if it took effect, and never does.
GEN_DEFAULTS = {"model": "", "n": 1, "max_tokens": 600, "seed": None}
GEN_IF_SET = ("temperature", "retries")
#: `gen`'s fields the queue reads itself: which model, how many answers, how
#: often an answer the judge refused is asked again. Every other field is the
#: model's - a parameter of the request, handed to the backend exactly as the
#: application named it.
GEN_OWN = ("model", "n", "retries")

DEFAULTS = {
    "stall_s": 300.0,           # no movement for this long: the generation failed
    "load_s": 600.0,            # ...unless the backend says it is loading a model
    "give_up_grace_s": 10.0,    # a give-up waits this long for the worker to let go
    "stale_head_s": 90.0,       # a submit takes the queue from a head this quiet
    "supervise_s": 2.0,         # the supervisor's pass
    "tick_s": 2.0,              # the worker's idle wake
    "recover_timeout_s": 5.0,   # boot asks the backend "did it finish?" this briefly
    "cancel_wait_s": 5.0,       # a cancel waits this long to report the worker let go
    "told_wait_s": 5.0,         # forget and prune wait this long for an ending being told
    "keep_finished": 200,       # finished rows kept; unfinished rows are never trimmed
    # S5b: an attempt the card's memory broke is run again, held for the card
    # to have room, at most this many times - a model too big for what the
    # other programs leave must not loop - then the job fails card_degraded,
    # naming each attempt
    "recover_attempts": 3,
    # S17: ANSWERS AT ONCE - each model answers one request at a time, and
    # different models answer together, at most this many in all. 8 is the
    # most models a runner keeps up at once (its `max_loads`: a rental names
    # at most 8 processes), so by default the one limit is one answer per
    # model, as the owner ruled; a machine whose card or processor is shared
    # may lower it - 1 is one answer at a time, as before
    "answers_at_once": 8,
    # S18: how often the queue looks for the scheduled job it waits to run -
    # planned for a time, or waiting for room on the card - and asks its
    # backend whether it may go (`room_look`). Each look compares the wall
    # clock with a job's time, so a machine that slept through it starts the
    # job at the first look after it wakes
    "room_look_s": 5.0,
}

#: what a batch job does when its model does not fit (S18): wait for room -
#: back in the queue, within its patience - or fail at once. A job that names
#: none has the server's own
IF_NO_ROOM = ("wait", "fail")

_ALPHABET = ids.ALPHABET


# ------------------------------------------------------------------ refusals

class Refused(Exception):
    """A request the queue will not carry out. `reason` is from a closed list
    for a program; `error` is a sentence for a person. Never a success."""

    def __init__(self, reason: str, error: str, code: int = 409, **extra):
        super().__init__(error)
        self.reason, self.error, self.code, self.extra = reason, error, code, extra

    def answer(self) -> dict:
        return {"error": self.error, "reason": self.reason, **self.extra}


class NoWorkspace(Refused):
    def __init__(self):
        super().__init__("no_workspace",
                         "no folder is bound, so there is no job queue to read "
                         "or write: choose a folder first", 409)


class FolderInUse(Refused):
    """Another live queue - in this process or another - has the folder."""

    def __init__(self, folder, holder):
        who = ""
        if holder:
            who = f" (pid {holder['pid']}" + (f", {holder['url']}" if holder.get("url")
                                               else "") + ")"
        super().__init__("folder_in_use", f"{folder} is bound by another server{who}",
                         409, holder=holder)


class StoreUnreadable(Exception):
    """The store exists and could not be read. Never answered as empty: every
    writer reads, edits and writes back, so an unreadable store answered as
    an empty one would be overwritten by the next write."""


class Cancelled(Exception):
    """Raised by a backend (or the queue) when a generation was asked to stop."""


class GenerationFailed(Exception):
    """A generation that went wrong. Never reported as a cancel. `reason`, when
    the failure has one of its own - one the contract declares for a job's
    `reason` (`JOB_REASONS`) - ends the job with it, for a program to branch
    on; any other is not kept."""

    def __init__(self, message: str = "", reason=None):
        super().__init__(message)
        self.reason = reason
        #: S5b: set by a backend when the card's memory broke this attempt and
        #: the job may be held for the card - the card's numbers as its source
        #: knew them ({} when none): the queue runs it again from the start,
        #: never counted as the model's failure, at most `recover_attempts`
        self.card_full = None
        #: S18: set by a backend when a batch job's model does not fit and the
        #: job waits for room - what it waits on ({"memory", "outside_mib",
        #: "words"}): the queue puts it back in line, queued, and runs it once
        #: the backend says it may (`room_look`), within its patience
        self.waits_for_room = None


_SHAPES, _ROUTES = _V.load(Path(__file__).resolve().parent.parent / "llm-bootstrap-contract")


def _job_reasons() -> tuple:
    """The reasons of its own a job may end with, as the contract declares
    them: `card_patience`, `card_busy`, `judge_refused`, `prompt_too_long`,
    `no_room`, `never_fits`, `card_degraded`."""
    return tuple(_SHAPES["job"]["object"]["reason"]["enum"])


JOB_REASONS = _job_reasons()

#: WHAT `gen` MAY NAME is the contract's list, never this file's: jobs-submit's
#: `gen` - the queue's own three, and every parameter llama-server's chat route
#: reads that is the application's to set. One list, read once, holds the
#: server's door, an application's own submit and its sampling hook alike.
GEN_SHAPE = _ROUTES["server/jobs-submit"]["request"]["object"]["gen"]
GEN_FIELDS = tuple(GEN_SHAPE["object"])
#: WHAT A SEND OF A HELD REQUEST MAY NAME (PLAN-4 D16): the submit's `gen`,
#: less the queue's own three - the contract's list, as a submit's is
SEND_GEN_SHAPE = _ROUTES["server/job-send"]["request"]["object"]["gen"]
SEND_FIELDS = tuple(SEND_GEN_SHAPE["object"])
#: what a held request says when its backend says nothing of it (no `hold`)
HELD_UNSAID = ("this server's backend says nothing more of a request than the queue knows: "
               "no engine, no count, no expected time")
#: a held request's description: what the backend says of it, and nothing more
HELD_PARTS = {"engine": None, "messages": None, "audio": None, "fields": None, "params": [],
              "count": None, "fits": None, "expect": None, "said": []}
#: nothing given: a send of the request exactly as it was built
AS_BUILT = object()


def _answers_a_wait(status: str) -> bool:
    """Is a job in *status* one a result's wait is answered at? Ended - or
    HELD for a person's check, when nothing more comes of it until it is
    sent (R-B3: a batch once waited out every wait on a held job)."""
    return status in FINISHED or status == "held"


def _engine_reads(gen: dict, reads) -> dict:
    """A held request's `gen`: only what its model's engine reads (R-B1) -
    *reads*, the backend's word of which of the job's parameters its engine
    reads (a whisper model reads none of them: its fields are its own), or
    None, every one."""
    return dict(gen) if reads is None else {k: v for k, v in gen.items() if k in reads}


def gen_problems(gen, at: str = "$.gen") -> list:
    """Every way *gen* is not a `gen` the contract names, in the validator's
    own words - the first is a refusal's. A value of None names nothing (it
    is unset), so it is not checked; a missing model is left to `no_model`."""
    if isinstance(gen, dict):
        gen = {k: v for k, v in gen.items() if v is not None}
    return [p for p in _V.check(gen, GEN_SHAPE, _SHAPES, at=at)
            if p != f'{at}: missing required field "model"']


#: the answer's lengths llama-server takes: -1 (no limit) to the largest it
#: reads (b10724: n_predict's hard limits) - any other it refuses, 400
MAX_TOKENS = (-1, 2 ** 31 - 1)


def length_problem(gen, at: str = "$.gen"):
    """How *gen*'s `max_tokens` is a length llama-server refuses, in words -
    or None. Its kind is the contract's to check (`gen_problems`), first."""
    most = gen.get("max_tokens") if isinstance(gen, dict) else None
    if isinstance(most, bool) or not isinstance(most, (int, float)):
        return None
    if MAX_TOKENS[0] <= most <= MAX_TOKENS[1]:
        return None
    return (f"{at}.max_tokens: llama-server takes an answer's length of {MAX_TOKENS[0]} "
            f"(no limit) to {MAX_TOKENS[1]}, not {most}")


#: EVERY OTHER HARD LIMIT llama-server holds a text request's parameter to
#: (b10724, server-schema.cpp: each `set_hard_limits`) - a value outside it is
#: refused there, 400, after the job waited for the card and its model (H5's
#: register 4). An integer is read into 32 bits, so none is more than
#: 2147483647 - `t_max_predict_ms` into 64 - and `adaptive_decay` is a float,
#: compared in single precision (`_single`)
_INT32 = 2 ** 31 - 1
HARD_LIMITS = {"n_indent": (0, _INT32), "n_keep": (-1, _INT32), "n_discard": (0, _INT32),
               "n_cache_reuse": (0, _INT32), "t_max_predict_ms": (-1, 2 ** 63 - 1),
               "repeat_last_n": (0, _INT32), "dry_allowed_length": (0, _INT32),
               "dry_penalty_last_n": (0, _INT32), "min_keep": (0, _INT32),
               "reasoning_budget_tokens": (-1, _INT32), "adaptive_decay": (0.0, 0.99)}
_SINGLE = {"adaptive_decay"}


def _single(x: float) -> float:
    """*x* as llama-server holds a float: single precision - too large for it,
    infinite."""
    try:
        return struct.unpack("<f", struct.pack("<f", x))[0]
    except OverflowError:
        return math.copysign(math.inf, x)


def range_problem(gen, at: str = "$.gen"):
    """How a value in *gen* is outside one of llama-server's other hard limits
    (`HARD_LIMITS`), in words - or None. Kinds are the contract's to check
    (`gen_problems`), first; `max_tokens` is `length_problem`'s."""
    if not isinstance(gen, dict):
        return None
    for key, (low, high) in HARD_LIMITS.items():
        value = gen.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        seen = (_single(value), _single(low), _single(high)) if key in _SINGLE \
            else (value, low, high)
        if not seen[1] <= seen[0] <= seen[2]:
            return f"{at}.{key}: llama-server takes {key} of {low} to {high}, not {value}"
    return None


class Hooks:
    """Harmless defaults: the request is the input, nothing is judged. They
    keep no state, so two threads may call them at once (S17)."""

    def build_request(self, job, after_results):
        return job.get("input")

    def land_result(self, job, result):
        return None

    def sampling_for(self, kind, input):
        return {}

    def merge(self, group, members):
        return None

    def judge(self, job, candidate):
        return None

    def ended(self, job, result):
        return None

    def manifest(self, job, after_results):
        return {}


class Run:
    """What a backend is handed for one generation."""

    def __init__(self, queue, job_id, handle, cancelled, correlation_id=None, sample=None,
                 priority=None, card_patience_s=None, card_waited_s=None, if_no_room=None):
        self._queue, self.job_id, self.handle = queue, job_id, handle
        # S18: what a batch job does when its model does not fit - `wait`,
        # `fail`, or None: the server's own
        self.if_no_room = if_no_room
        self.cancelled = cancelled
        # the request this generation serves, and its sample: an id names, so
        # a backend may write them into its own record to join the two
        self.correlation_id, self.sample = correlation_id, sample
        # what the job says about waiting for the card: its priority (its
        # class) and its own patience - None: the server's
        self.priority, self.card_patience_s = priority, card_patience_s
        # S5b: how long it has been held for the card's memory already, since
        # the card first broke it - its patience counts from then; None: never
        self.card_waited_s = card_waited_s

    def stopped(self) -> bool:
        return self.cancelled.is_set()

    def report(self, **fields) -> None:
        self._queue._report(self.job_id, self.handle, fields)

    def record(self, name: str, data) -> bool:
        """Keep something about this attempt in the request's record, beside
        what the queue keeps: `samples/<NN>/<name>` - JSON for data, text for
        a string. A plain file name only, and never one of the queue's own.
        -> whether it was kept. Never raises: a record must not fail the
        generation it records."""
        return self._queue._record_attempt(self.correlation_id, self.sample, name, data)


# --------------------------------------------------------------------- store

class Store:
    """One file, read whole and written whole. The write goes to a unique
    temporary name and is swapped in; the swap is retried, because on Windows
    another process holding the file for a moment makes it fail."""

    def __init__(self, path: Path, note_fault):
        self.path = Path(path)
        self._note = note_fault

    def read_bytes(self) -> bytes:
        return self.path.read_bytes()

    def replace(self, src, dst) -> None:
        os.replace(src, dst)

    @staticmethod
    def empty() -> dict:
        return {"seq": 0, "jobs": [], "groups": {}, "rejected": []}

    def load(self) -> dict:
        # A READ IS RETRIED LIKE A SWAP. On Windows a reader that opens the
        # file while another writer is replacing it is refused for a moment
        # (measured in this suite: a stopped instance's last pass swapping the
        # file while the next instance read it). Transient, so it is retried;
        # never answered as empty, so a refusal that lasts is a store fault.
        for attempt in range(8):
            try:
                raw = self.read_bytes()
                break
            except FileNotFoundError:
                return self.empty()          # a folder that never ran a job
            except PermissionError as e:
                if attempt == 7:
                    self._note(type(e).__name__, str(e))
                    raise StoreUnreadable(f"the job store could not be read: {e}") from e
                time.sleep(0.05 * (attempt + 1))
            except MemoryError as e:
                self._note("MemoryError", str(e))
                raise StoreUnreadable(f"the job store could not be read: {e}") from e
            except OSError as e:
                self._note(type(e).__name__, str(e))
                raise StoreUnreadable(f"the job store could not be read: {e}") from e
        try:
            doc = json.loads(raw.decode("utf-8"))
        except MemoryError as e:
            self._note("MemoryError", str(e))
            raise StoreUnreadable(f"the job store could not be read: {e}") from e
        except (ValueError, UnicodeDecodeError) as e:
            return self._set_aside(f"it is not JSON ({e})")
        if not isinstance(doc, dict):
            return self._set_aside(f"it is JSON but a {type(doc).__name__}, "
                                   "not a job store")
        jobs = doc.get("jobs", [])
        if not isinstance(jobs, list):
            return self._set_aside("its `jobs` is not a list")
        out = self.empty()
        out["groups"] = doc.get("groups") if isinstance(doc.get("groups"), dict) else {}
        out["rejected"] = list(doc.get("rejected") or [])
        for row in jobs:
            if _row_ok(row):
                out["jobs"].append(_normal_row(row))
            else:
                out["rejected"].append(row)
        seq = doc.get("seq")
        top = max((_id_number(r["job"]["id"]) for r in out["jobs"]), default=0)
        out["seq"] = max(seq if isinstance(seq, int) and not isinstance(seq, bool)
                         else 0, top)
        return out

    def _set_aside(self, why: str) -> dict:
        """A store that cannot be understood is MOVED, never overwritten, and
        the queue carries on empty. The bytes stay beside the new store."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = self.path.with_name(f"{self.path.name}.unreadable-{stamp}")
        k = 1
        while dest.exists():
            k += 1
            dest = self.path.with_name(f"{self.path.name}.unreadable-{stamp}-{k}")
        try:
            self.replace(self.path, dest)
        except OSError as e:
            self._note(type(e).__name__, f"{why}; and it could not be set aside: {e}")
            raise StoreUnreadable(f"the job store is unreadable ({why}) and "
                                  f"could not be set aside: {e}") from e
        self._note("unreadable", f"{why}; set aside as {dest.name}")
        return self.empty()

    def save(self, doc: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}."
            f"{random.randrange(16 ** 8):08x}.tmp")
        body = {"seq": doc["seq"], "jobs": doc["jobs"],
                "groups": doc.get("groups") or {},
                "rejected": doc.get("rejected") or []}
        tmp.write_bytes(json.dumps(body, indent=1).encode("utf-8"))    # write_text: CRLF on Windows
        for attempt in range(8):
            try:
                self.replace(tmp, self.path)
                return
            except PermissionError:
                if attempt == 7:
                    tmp.unlink(missing_ok=True)
                    raise
                time.sleep(0.05 * (attempt + 1))


def _row_ok(row) -> bool:
    return (isinstance(row, dict) and isinstance(row.get("job"), dict)
            and isinstance(row["job"].get("id"), str) and bool(row["job"]["id"]))


def _id_number(jid: str) -> int:
    tail = jid.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def _when(v) -> float:
    """A stamp that can be sorted: a number, or 0 for anything that is not."""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def _normal_row(row: dict) -> dict:
    row.setdefault("input", None)
    row.setdefault("gen", {})
    row.setdefault("result", None)
    row.setdefault("card_patience_s", None)
    if not isinstance(row.get("run"), dict):
        row["run"] = {}
    j = row["job"]
    for k, v in (("kind", ""), ("name", ""), ("status", "queued"),
                 ("priority", "interactive"), ("queue_pos", 0), ("after", []),
                 ("group", None), ("scope", None), ("correlation_id", ""),
                 ("correlation_source", "server"), ("client", None), ("claimed_by", None),
                 ("retry_of", None),
                 ("model", ""), ("n", 1), ("n_done", 0), ("progress", {}),
                 ("created", 0.0), ("finished", None), ("stalled_for", None),
                 ("error", None)):
        j.setdefault(k, v)
    return row


# --------------------------------------------------------------- the folder

class FolderLock:
    """An exclusive lock on `<folder>/jobs.lock`, held for as long as a queue is
    bound to the folder.

    THE OPERATING SYSTEM HOLDS IT, NOT THE FILE'S EXISTENCE. A lock that is a
    file being there outlives a server that crashed, and then needs a guess
    about whether an old pid still belongs to the same program. A lock the
    operating system holds on an open handle is let go when the process dies,
    however it dies - the one moment that matters.

    The file holds a note saying who has the folder. On Windows the lock is
    one byte far past the note, because a locked range cannot be read by
    anyone else; on POSIX `flock` is advisory, and a second `open` in the same
    process is refused like another process's."""

    FAR = 1 << 30

    def __init__(self, folder):
        self.path = Path(folder) / LOCK_NAME
        self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, note: dict) -> None:
        """Take the folder, or raise FolderInUse naming who has it. An OSError
        opening the file is the folder's own problem and is raised as it is.
        A folder that does not exist yet is made, as the store's first write
        would make it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            _lock(fd, self.FAR)
        except OSError:
            os.close(fd)
            raise FolderInUse(self.path.parent, self.read_note()) from None
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, json.dumps(note).encode("utf-8"))
        except OSError:
            pass                        # the note is courtesy; the lock is the fact
        self._fd = fd

    def read_note(self):
        """Who has the folder, or None when that cannot be read."""
        try:
            note = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(note, dict):
            return None
        pid, url, since = note.get("pid"), note.get("url"), note.get("since")
        if isinstance(pid, bool) or not isinstance(pid, int):
            return None
        if url is not None and not isinstance(url, str):
            url = None
        if isinstance(since, bool) or not isinstance(since, (int, float)):
            return None
        actor = note.get("actor")
        return {"pid": pid, "url": url, "since": float(since),
                "actor": actor if ids.matches("actor", actor) else None}

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd, self.FAR)
        except OSError:
            pass
        finally:
            os.close(fd)


def _lock(fd: int, far: int) -> None:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, far, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int, far: int) -> None:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, far, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


# --------------------------------------------------------------------- queue

class Queue:
    """A job queue bound to one folder, or to none (`folder=None`), in which
    case every question about jobs is refused with `no_workspace` and health
    still answers.

    Binding takes the folder's lock first, before anything else happens: a
    folder another live queue has raises FolderInUse and leaves nothing
    behind. `url` is who is asking, for the note a refused one reads.

    `actor` is the server's own id (a srv- actor id), minted when the server
    started. It names every generation this queue runs - the handle is
    `<actor>:<job>#<sample>` - so a model server two servers share can never
    mistake one's generation for the other's. A queue made without one mints
    its own."""

    def __init__(self, folder, hooks=None, backend=None, *, start: bool = True,
                 clock=time.time, log=None, url=None, actor=None, **config):
        unknown = set(config) - set(DEFAULTS)
        if unknown:
            raise TypeError(f"unknown queue settings: {sorted(unknown)}")
        if actor is not None and not (ids.matches("actor", actor)
                                      and actor.startswith("srv-")):
            raise ValueError(f"a queue's actor is a server's (srv-) actor id, not {actor!r}")
        self.actor = actor or ids.mint_actor("srv")
        self.cfg = {**DEFAULTS, **config}
        self.folder = Path(folder) if folder else None
        self._folder_lock = None
        if self.folder:
            lock = FolderLock(self.folder)
            lock.acquire({"pid": os.getpid(), "url": url, "since": time.time(),
                          "actor": self.actor})
            self._folder_lock = lock
        self.hooks = hooks if hooks is not None else Hooks()
        self.backend = backend
        #: job id -> [its seconds in the backend, the model it named], while
        #: the worker runs it: what its share is reckoned from (`_run`, D12)
        self._in_backend: dict = {}
        self._clock = clock
        self._log = log or (lambda s: print(s, flush=True))
        self._lock = threading.RLock()
        # told when an ending another thread was telling has been told
        self._told = threading.Condition(self._lock)
        # told when a job ends, however it ends, and when the queue stops: a
        # result asked for with a wait wakes on it
        self._ended = threading.Condition(self._lock)
        # a request's story file is appended to from more than one thread
        self._record_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._events: dict = {}             # job id -> the cancel Event of its run
        self._merging: set = set()
        self._telling: dict = {}            # job id -> the thread telling its ending now
        self._worker = None
        # a worker loop stays live while its generation is not older than the
        # newest one STARTED; starting a newer one retires every older loop
        self._live_from = 0
        # S17: each answer runs on a thread of its own - job id -> the worker
        # generation that picked it, while a thread is inside its run; and job
        # id -> that thread, until it has returned
        self._inside: dict = {}
        self._answering: dict = {}
        # which worker holds a job is stamped as <instance>:<generation>, so a
        # worker of another instance - a server that stopped, in one test
        # process - can never pass for the one that holds the job now
        self._instance = "".join(random.choice(_ALPHABET) for _ in range(8))
        # S18: when the queue next looks for the scheduled job it waits to run
        # (monotonic: an interval; what it compares is the wall clock)
        self._look_at = 0.0
        self._supervisor = None
        self._threads: list = []
        self._beat = {"gen": 0, "restarts": 0, "retired": 0,
                      "cycle_at": 0.0, "started_at": 0.0}
        self._store_fault = {"kind": "", "detail": "", "at": 0.0, "count": 0}
        self._restart_fault = {"kind": "", "detail": "", "at": 0.0, "count": 0}
        self._recovered = False
        self.store = Store(self.folder / STORE_NAME, self._note_store_fault) \
            if self.folder else None
        if start and self.folder:
            self.start()

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Settle what a stopped server left behind, THEN start the worker:
        the queue is never live while a stale `running` row could block it.
        A store that cannot be read at boot does not stop the start - health
        has to be able to say so - and the worker settles it once it can."""
        try:
            self.recover()
        except StoreUnreadable:
            pass                                 # recorded as a store fault already
        self._start_worker()
        self._supervisor = threading.Thread(target=self._supervise_loop,
                                            daemon=True, name="queue-supervisor")
        self._supervisor.start()

    def close(self, timeout: float = 5.0) -> None:
        """Stop the worker and the supervisor, then put the folder down. A
        shutdown is not a cancel: every generation in flight is left as it
        is, and the next start settles each.

        THE STORE IS LET GO BEFORE THE FOLDER. A generation that never came
        back keeps its thread; so the store is detached under the store's own
        lock - no write is half done, and none can start - and only then is
        the folder's lock released for the next queue to take.

        ONE TIMEOUT FOR ALL OF THEM, NOT ONE EACH. The worker is in the list
        twice - as the worker, and among every thread started - and a stop
        mid-answer once waited for it twice (found on the real card: ten
        seconds, for a timeout of five)."""
        self._stop.set()
        self._wake.set()
        with self._lock:
            self._ended.notify_all()            # a result that waits answers now
        deadline = time.monotonic() + timeout
        # every answer under way (S17) is waited for within the same one
        # timeout: a stop is not a cancel, and the next start settles each
        for t in [self._supervisor, self._worker] + self._threads \
                + list(self._answering.values()):
            if t is not None and t is not threading.current_thread():
                t.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            self.store = None
            self.folder = None
        if self._folder_lock is not None:
            self._folder_lock.release()
            self._folder_lock = None

    # ------------------------------------------------------------- the store

    def _need_workspace(self) -> None:
        if self.store is None:
            raise NoWorkspace()

    def _load(self) -> dict:
        self._need_workspace()
        return self.store.load()

    def _save(self, doc: dict) -> None:
        self._trim(doc)
        self._positions(doc)
        self.store.save(doc)

    def _note_store_fault(self, kind: str, detail: str) -> None:
        f = self._store_fault
        f.update(kind=kind, detail=str(detail)[:200], at=time.time(),
                 count=f["count"] + 1)
        self._say(f"THE JOB STORE COULD NOT BE READ AS A STORE: {kind}: "
                  f"{str(detail)[:200]} (#{f['count']})")

    def store_fault(self) -> dict:
        return dict(self._store_fault)

    def restart_fault(self) -> dict:
        return dict(self._restart_fault)

    def _say(self, line: str) -> None:
        try:
            self._log(line.encode("ascii", "replace").decode("ascii"))
        except Exception:                                   # noqa: BLE001
            pass

    # ---------------------------------------------------------- projections

    def public(self, row: dict) -> dict:
        """The contract's job record for a row, `stalled_for` computed now."""
        job = dict(row["job"])
        job["progress"] = dict(job.get("progress") or {})
        job["after"] = list(job.get("after") or [])
        job["stalled_for"] = self._stalled_for(row)
        return job

    def _stalled_for(self, row: dict):
        """How long a running job has not moved - as the stall watch counts
        it. None for a job that is not running, and for one answering in one
        piece before the deadline it declared: it is answering, not stalled
        (PLAN-4 D10). After that deadline it is counted from its last
        movement, as any quiet job's is."""
        if row["job"].get("status") != "running" or self._answering_once(row):
            return None
        at = ((row["job"].get("progress") or {}).get("at")
              or row["run"].get("started") or row["job"].get("created"))
        try:
            return max(0.0, round(self._clock() - float(at), 1))
        except (TypeError, ValueError):
            return None

    def _view(self, row: dict) -> dict:
        """What a hook is handed: the public record plus the application's own
        input and the stored `gen`. A copy - a hook cannot edit the store."""
        v = json.loads(json.dumps(self.public(row)))
        v["input"] = json.loads(json.dumps(row.get("input")))
        v["gen"] = dict(row.get("gen") or {})
        return v

    # ------------------------------------------------------------- ordering

    @staticmethod
    def _order(doc: dict) -> list:
        q = [r for r in doc["jobs"] if r["job"]["status"] == "queued"]
        q.sort(key=lambda r: (0 if r["job"]["priority"] == "interactive" else 1,
                              r["job"]["created"], _id_number(r["job"]["id"])))
        return q

    def _positions(self, doc: dict) -> None:
        for r in doc["jobs"]:
            r["job"]["queue_pos"] = 0
        for pos, r in enumerate(self._order(doc), start=1):
            r["job"]["queue_pos"] = pos

    @staticmethod
    def _find(doc: dict, jid: str):
        for r in doc["jobs"]:
            if r["job"]["id"] == jid:
                return r
        return None

    @staticmethod
    def _superseded(doc: dict) -> set:
        return {r["job"]["retry_of"] for r in doc["jobs"] if r["job"].get("retry_of")}

    def _runnable(self, doc: dict, row: dict) -> bool:
        for a in row["job"].get("after") or []:
            dep = self._find(doc, a)
            if dep is None or dep["job"]["status"] not in LANDED:
                return False
        return True

    # ------------------------------------------------------------- submitting

    @staticmethod
    def gen_for(gen) -> dict:
        """The one constructor for a stored `gen`: every field the contract
        names, the defaults for an untouched request, and nothing it does not
        name. 0 is a real answer for `retries` and for `temperature`, so
        presence is tested. Every other parameter is kept exactly as it was
        named - False and 0 included - and one named None is unset."""
        g = dict(gen or {})
        out = dict(GEN_DEFAULTS)
        out["model"] = str(g.get("model") or "").strip()
        if g.get("n") is not None:
            out["n"] = max(1, int(g["n"]))
        if g.get("max_tokens") is not None:
            out["max_tokens"] = int(g["max_tokens"])
        if g.get("temperature") is not None:
            out["temperature"] = float(g["temperature"])
        if g.get("seed") is not None:
            out["seed"] = int(g["seed"])
        if g.get("retries") is not None:
            out["retries"] = max(0, min(RETRY_MAX, int(g["retries"])))
        for name in GEN_FIELDS:
            if name not in out and name not in GEN_IF_SET and g.get(name) is not None:
                out[name] = json.loads(json.dumps(g[name]))
        return out

    def _check_request(self, input, gen: dict) -> None:
        """A REQUEST ITS MODEL CAN NEVER ANSWER IS REFUSED AT SUBMIT, when the
        request is the job's input - the default build hook's - and the
        backend can tell (`backend.check_request(request, params)`, raising
        `Refused` in its own reason and words): never stored, never after the
        job waited for the card. The queue reads nothing of it: the input
        goes to the backend as it would at the job's turn. With a build hook
        of the application's own the request is made at the job's turn, from
        whatever it is made from, so nothing is checked here."""
        check = getattr(self.backend, "check_request", None)
        if check is None or getattr(type(self.hooks), "build_request", None) \
                is not Hooks.build_request:
            return
        check(json.loads(json.dumps(input)), dict(gen))

    def _mint_correlation(self, doc: dict) -> str:
        """A server-minted id, for a request that came without one. One the
        folder already has is minted again: an id the server invented can
        always be invented again."""
        taken = {r["job"].get("correlation_id") for r in doc["jobs"]}
        while True:
            c = ids.mint_correlation(self._clock())
            if c not in taken:
                return c

    @staticmethod
    def _id_or_refuse(value, kind: str, at: str) -> None:
        """A malformed id is `bad_request`, in the validator's own words - the
        same sentence the server answers for a body with this field wrong."""
        if value is not None:
            wrong = _V.check(value, {"id": kind}, at=at)
            if wrong:
                raise Refused("bad_request", wrong[0], 400)

    def submit(self, kind, input=None, gen=None, *, name=None, priority="interactive",
               after=(), group=None, group_size=None, scope=None, correlation_id=None,
               client=None, retry_of=None, card_patience_s=None, check=False,
               not_before=None, if_no_room=None) -> dict:
        """Queue a job. -> {job_id, queue_pos, correlation_id, correlation_source}.

        Refused: no_kind, no_model, unknown_after, bad_patience, bad_max_tokens,
        out_of_range, bad_request (400) and no_workspace, correlation_taken (409); and, for a fan,
        bad_group_size (400) and group_full (409). A job whose `after` already
        failed is accepted and stopped at once, saying why - the record is the
        answer, see README.md.

        `card_patience_s` is how long this job waits for the card - its
        application's choice, per request; None is the server's own, and 0
        now or not at all. It goes to the backend with every generation, with
        the job's priority (`Run`).

        SCHEDULED WORK (S18, the owner's second ruling of 2026-09-28): a batch
        job's `if_no_room` - `wait` or `fail`, None the server's own - goes to
        the backend with its priority; one that waits for room comes back
        here (`GenerationFailed.waits_for_room`) and waits queued, holding back
        nobody (`_park`). `not_before`, seconds since the epoch on this
        queue's clock, is the time it starts no earlier than: until then it is
        queued and holds back nobody (`_held_back`), and from its time the
        queue looks for it (`_look`) - its patience counted from that first
        look, on the wall clock. Live work is now: a job that does not say it
        is batch that names a time still to come, or a wait for room, is
        refused `live_is_now`; a time that is not a finite number,
        `bad_request`.

        A JOB THAT DOES NOT SAY IT IS BATCH IS LIVE (S18b; PLAN-4 D0, "class
        default RESPONSIVE"; the owner: "none of my agents run scheduled
        batches, it is always ran right now and response asap"): `priority`
        is `interactive` unless the application says `batch`, and a job's
        priority is at once its place in the queue's order and its class at
        the card - an interactive job asks as responsive, never waits for
        room, and takes no time still to come. Only `batch`, said, makes a
        job scheduled work.

        THE CORRELATION ID NAMES THIS REQUEST EVERYWHERE IT GOES. The client
        that starts a request mints it. One that arrives without is minted
        here, and `correlation_source` says which, so a record never claims a
        client id it was not sent. A client's id this folder already has is
        REFUSED (`correlation_taken`), never re-minted: handing back an id the
        client did not send would leave it joining on its own id while the
        record wears another. `client` is the asking client's actor id,
        recorded so a client can find its own work (`jobs(client=)`).

        `scope` is a key of the application's own - a book, a project. The
        queue never reads it except to narrow `jobs` and `prune` to it.

        `group_size` IS HOW A FAN KNOWS IT IS WHOLE. The members of a fan
        arrive one submit at a time while the worker is already running, so
        without a size the first member can land before the second exists -
        and a fan of one has "all landed". With a size, a fan settles only
        once that many members exist and every one of them has ended.

        `check` MARKS THE JOB TO BE CHECKED BEFORE IT IS SENT (PLAN-4 D16): at
        its turn, once its request is built, it is held - `held`, with the
        exact request as it would be sent - until `send` or `cancel`. What
        the application named of `gen` is kept with it, so a held request
        can say where each parameter came from. A job not marked never
        waits for anyone."""
        self._need_workspace()
        kind = str(kind or "").strip()
        if not kind:
            raise Refused("no_kind", "a job needs a kind", 400)
        # a parameter the contract does not name - a misspelt one - is refused
        # here as at the server's door, never dropped and never sent
        wrong = gen_problems({} if gen is None else gen)
        if wrong:
            raise Refused("bad_request", wrong[0], 400)
        # a length llama-server refuses is refused where it is asked, never
        # after the job waited for the card and its model
        why = length_problem(gen)
        if why:
            raise Refused("bad_max_tokens", why, 400)
        # and so is a value outside another of its hard limits (H5's register 4)
        why = range_problem(gen)
        if why:
            raise Refused("out_of_range", why, 400)
        g = self.gen_for(gen)
        if not g["model"]:
            raise Refused("no_model", "a job needs a model", 400)
        self._check_request(input, g)
        self._id_or_refuse(correlation_id, "correlation", "$.correlation_id")
        self._id_or_refuse(client, "actor", "$.client")
        if card_patience_s is not None:
            wrong = _V.check(card_patience_s, "number", at="$.card_patience_s")
            if wrong:
                raise Refused("bad_request", wrong[0], 400)
            if card_patience_s < 0:
                raise Refused("bad_patience", "a patience is 0 or more seconds: 0 means now "
                              "or not at all", 400)
        self._scheduled_or_refuse(priority, not_before, if_no_room)
        after = [str(a) for a in (after or [])]
        size = None
        if group_size is not None:
            if not group or isinstance(group_size, bool) or not isinstance(group_size, int) \
                    or group_size < 1:
                raise Refused("bad_group_size", "a group size is a whole number of "
                              "members, given with the group it sizes", 400)
            size = group_size
        self._clear_stale_head(g["model"])
        with self._lock:
            doc = self._load()
            for a in after:
                if self._find(doc, a) is None:
                    raise Refused("unknown_after", f"after names {a}, which is not a job", 400)
            if group:
                rec = doc.setdefault("groups", {}).setdefault(
                    str(group), {"merged": None, "pending": [], "error": None})
                have = len(self._groups(doc).get(str(group), []))
                if size is not None:
                    if rec.get("size") not in (None, size):
                        raise Refused("bad_group_size", f"group {group} was sized "
                                      f"{rec['size']}, not {size}", 400)
                    rec["size"] = size
                if rec.get("size") is not None and have >= rec["size"]:
                    raise Refused("group_full", f"group {group} already has its "
                                  f"{rec['size']} members", 409)
            if correlation_id is not None:
                had = next((r["job"]["id"] for r in doc["jobs"]
                            if r["job"].get("correlation_id") == correlation_id), None)
                if had:
                    raise Refused("correlation_taken", f"correlation id {correlation_id} "
                                  f"is already {had}'s", 409)
                corr, source = correlation_id, "client"
            else:
                corr, source = self._mint_correlation(doc), "server"
            doc["seq"] += 1
            jid = f"j-{doc['seq']:04d}"
            row = {"job": {
                "id": jid, "kind": kind, "name": str(name or "").strip() or kind,
                "status": "queued",
                "priority": "batch" if priority == "batch" else "interactive",
                "queue_pos": 0, "after": after,
                "group": str(group) if group else None,
                "scope": (str(scope).strip() or None) if scope is not None else None,
                "correlation_id": corr, "correlation_source": source,
                "client": client, "claimed_by": None, "retry_of": retry_of,
                "model": g["model"], "n": g["n"], "n_done": 0, "progress": {},
                "created": self._clock(), "finished": None, "stalled_for": None,
                "error": None},
                "input": input, "gen": g, "result": None, "run": {"phase": "queued"},
                "card_patience_s": card_patience_s}
            if check:
                row["job"]["check"] = "marked"
                row["check"] = {"named": sorted(k for k, v in (gen or {}).items()
                                                if v is not None)}
            if not_before is not None:
                row["job"]["not_before"] = float(not_before)
            if if_no_room is not None:
                row["job"]["if_no_room"] = if_no_room
            doc["jobs"].append(row)
            self._resolve(doc)
            self._save(doc)
            pos = row["job"]["queue_pos"]
            self._record_submitted(doc, row)
        self._drain_hooks()
        self._wake.set()
        return {"job_id": jid, "queue_pos": pos, "correlation_id": corr,
                "correlation_source": source}

    def _scheduled_or_refuse(self, priority, not_before, if_no_room) -> None:
        """S18: a job's time and its wait for room, checked at submit. Live
        work - every job that does not say `batch` (S18b) - is now: a time
        still to come, or a wait for room, is refused `live_is_now`, in words
        that say what makes a job scheduled; a time already past is now. A time that is not a finite number, or an `if_no_room` that is
        neither, is `bad_request`."""
        if not_before is not None:
            wrong = _V.check(not_before, "number", at="$.not_before")
            if not wrong and not math.isfinite(not_before):
                wrong = [f"$.not_before: a time is seconds since the epoch, a finite number, "
                         f"not {not_before!r}"]
            if wrong:
                raise Refused("bad_request", wrong[0], 400)
        if if_no_room is not None:
            wrong = _V.check(if_no_room, {"enum": list(IF_NO_ROOM)}, at="$.if_no_room")
            if wrong:
                raise Refused("bad_request", wrong[0], 400)
        if priority == "batch":
            return
        if if_no_room == "wait":
            raise Refused("live_is_now", 'live work is now: a job that does not say priority '
                          '"batch" is live, and one whose model does not fit fails at once, '
                          'loudly, and never waits for room (if_no_room: wait); say priority '
                          '"batch" to let it wait', 400)
        away = None if not_before is None else float(not_before) - self._clock()
        if away is not None and away > 0:
            raise Refused("live_is_now", f'live work is now: a job that does not say priority '
                          f'"batch" is live, and starts at once or not at all, so it names no '
                          f'time still to come (not_before {float(not_before):.0f} is '
                          f'{math.ceil(away):.0f} s away); say priority "batch" to plan it for '
                          f'later', 400)

    # -------------------------------------------------------------- reading

    def get(self, job_id: str):
        with self._lock:
            row = self._find(self._load(), job_id)
            return self.public(row) if row else None

    def jobs(self, scope=None, client=None) -> list:
        """Every job the bound folder holds, in the order they were submitted;
        with a `scope`, only the jobs submitted with it; with a `client`, only
        the jobs that client asked for."""
        with self._lock:
            return [self.public(r) for r in self._load()["jobs"]
                    if (scope is None or r["job"].get("scope") == scope)
                    and (client is None or r["job"].get("client") == client)]

    def result(self, job_id: str, wait_s: float = 0.0) -> dict:
        """A job and whatever it has produced so far. Reading never claims,
        and nothing a candidate carries is dropped on the way out.

        With `wait_s`, the answer waits - at most that long - until the job
        has ended, however it ends, and then answers as without it: nothing
        has to ask again and again. A job that has ended, or is not there, is
        answered at once; so is every wait when the queue stops."""
        if wait_s > 0:
            self._await_ended(job_id, float(wait_s))
        with self._lock:
            row = self._find(self._load(), job_id)
            if row is None:
                raise Refused("no_such_job", f"no such job: {job_id}", 404)
            cands = (row.get("result") or {}).get("candidates") or []
            return {"job": self.public(row),
                    "candidates": json.loads(json.dumps(cands))}

    def _await_ended(self, job_id: str, wait_s: float) -> None:
        """Wait - at most *wait_s* - until *job_id* has ended, is not there,
        or the queue stops. It holds no lock while it waits: every ending
        goes through `_end`, which wakes it, and it looks again. A store that
        cannot be read, or no folder, ends the wait at once - the answer that
        follows says so."""
        deadline = time.monotonic() + wait_s
        with self._lock:
            while not self._stop.is_set():
                try:
                    row = self._find(self._load(), job_id)
                except (Refused, StoreUnreadable):
                    return
                if row is None or _answers_a_wait(row["job"]["status"]):
                    return
                left = deadline - time.monotonic()
                if left <= 0:
                    return
                self._ended.wait(left)

    def next_up(self):
        """The job the worker would take next, as things stand - the first
        queued job, in the queue's order, whose `after` have all landed - or
        None. A job waiting on the one running now is not it: that one has not
        landed. It reads; it takes nothing. A folder that is not bound, or a
        store that cannot be read, has no next job to name."""
        with self._lock:
            try:
                doc = self._load()
            except (Refused, StoreUnreadable):
                return None
            for r in self._order(doc):
                if self._runnable(doc, r):
                    return self.public(r)
            return None

    def unfinished(self, held: bool = True) -> list:
        """What the queue still owes an answer for: the running jobs first,
        then the queue in order, then the held jobs (PLAN-4 D16) - unless
        `held` is False: a held job waits on a person, and is work nothing
        keeps the card for; so is a scheduled job held back for its time or
        for room on the card (S18, `_held_back`): nothing keeps the card for
        it meanwhile. A status the queue does not know is owed."""
        with self._lock:
            doc = self._load()
            now = self._clock()
            running = [r for r in doc["jobs"] if r["job"]["status"] == "running"]
            waiting = [r for r in doc["jobs"] if r["job"]["status"] == "held"] if held else []
            odd = [r for r in doc["jobs"] if r["job"]["status"] not in STATUSES]
            queued = [r for r in self._order(doc) if held or not self._held_back(r, now)]
            return [self.public(r) for r in running + queued + waiting + odd]

    # ------------------------------------------------------------- claiming

    def claim(self, job_id: str, client=None) -> dict:
        """Exactly one claim succeeds. Each failure is its own fact. `client`
        names who claimed it, kept as `claimed_by` - recorded, never
        enforced: a result may be taken by another client on purpose."""
        self._id_or_refuse(client, "actor", "$.client")
        with self._lock:
            doc = self._load()
            row = self._find(doc, job_id)
            if row is None:
                raise Refused("no_such_job", f"no such job: {job_id}", 404)
            st = row["job"]["status"]
            if st == "claimed":
                raise Refused("claimed_elsewhere", f"{job_id} was already claimed",
                              409, status="claimed")
            if st in ("failed", "cancelled"):
                # not `not_done`: that reads as "try later", and waiting will
                # never make a job that ended this way claimable
                raise Refused("ended", f"{job_id} ended {st}: there is no result to "
                              "claim - retry it", 409, status=st)
            if st != "done":
                raise Refused("not_done", f"{job_id} is {st}, not done", 409, status=st)
            row["job"]["status"] = "claimed"
            row["job"]["claimed_by"] = client
            self._save(doc)
            self._record_event(row["job"].get("correlation_id"), "claimed", job=job_id,
                               by=client)
            return {"job": self.public(row)}

    # ------------------------------------------------------------ ending jobs

    def _end(self, doc: dict, row: dict, status: str, error=None, by=None,
             reason=None) -> bool:
        """The one door to a terminal status. A job that has ended stays ended:
        whatever arrives later - a worker that was taken over, a second reaper -
        is refused here rather than rewriting the record. `reason` is the
        ending's own reason, kept only when the contract declares it."""
        j = row["job"]
        if j["status"] in FINISHED:
            return False
        j["status"] = status
        j["finished"] = self._clock()
        j["error"] = error
        if reason in JOB_REASONS:
            j["reason"] = reason
        # every caller holds the lock, so a waiter wakes once it is let go -
        # after the ending is saved
        self._ended.notify_all()
        row["run"]["phase"] = "ended"
        # a request held or sent (PLAN-4 D16) is in the record; the store
        # keeps only that it was checked, and how
        if isinstance(row.get("check"), dict):
            row["check"].pop("held", None)
            row["check"].pop("send", None)
        # the application is owed this ending; the debt is written in the same
        # save as the ending itself, so a stop between the two cannot lose it
        row["run"]["ending_told"] = False
        if by:
            row["run"]["ended_by"] = by
        if status != "done":
            ev = self._events.get(j["id"])
            if ev is not None:
                ev.set()
        return True

    def _resolve(self, doc: dict) -> None:
        """Consequences, to a fixed point: a job waiting on one that failed or
        was cancelled is stopped, naming it; a fan with a member that did not
        land stops its queued members; a fan whose members have all ended is
        marked for its merge."""
        superseded = self._superseded(doc)
        changed = True
        while changed:
            changed = False
            by_id = {r["job"]["id"]: r for r in doc["jobs"]}
            for r in doc["jobs"]:
                if r["job"]["status"] != "queued":
                    continue
                for a in r["job"].get("after") or []:
                    dep = by_id.get(a)
                    if dep and dep["job"]["status"] in ("failed", "cancelled"):
                        self._end(doc, r, "cancelled",
                                  f"not run: it waits on {a}, which "
                                  f"{dep['job']['status']}", by="after")
                        r["run"]["stopped_by"] = a
                        changed = True
                        break
            for g, members in self._groups(doc, superseded).items():
                dead = [m for m in members
                        if m["job"]["status"] in ("failed", "cancelled")
                        and not m["run"].get("stopped_by")]
                if not dead:
                    continue
                for m in members:
                    if m["job"]["status"] in ("queued", "held"):
                        self._end(doc, m, "cancelled",
                                  f"not run: the group stopped when "
                                  f"{dead[0]['job']['id']} {dead[0]['job']['status']}",
                                  by="group")
                        m["run"]["stopped_by"] = dead[0]["job"]["id"]
                        changed = True
        groups = doc.setdefault("groups", {})
        for g, members in self._groups(doc, superseded).items():
            if any(m["job"]["status"] not in FINISHED for m in members):
                continue
            size = (groups.get(g) or {}).get("size")
            if size is not None and len(members) < size:
                continue                         # not every member exists yet
            # landed, not the exact status: a member CLAIMED after the merge
            # must not read as a new settling and merge the fan twice
            sig = [[m["job"]["id"],
                    "landed" if m["job"]["status"] in LANDED else m["job"]["status"]]
                   for m in members]
            # a LIST of settlings: a fan can settle (stopped), be revived by a
            # retry and settle again before the first merge has run, and both
            # settlings happened - neither may overwrite the other
            rec = groups.setdefault(g, {"merged": None, "pending": [], "error": None})
            pend = self._pending(rec)
            if sig != rec.get("merged") and sig not in pend:
                pend.append(sig)
            rec["pending"] = pend

    @staticmethod
    def _pending(rec: dict) -> list:
        p = list(rec.get("pending") or [])
        if p and p[0] and isinstance(p[0][0], str):
            p = [p]                              # one settling, the older form
        return p

    def _groups(self, doc: dict, superseded=None) -> dict:
        """Each fan's CURRENT members - a retried member is replaced by its
        retry - in the order the fan was first submitted."""
        superseded = self._superseded(doc) if superseded is None else superseded
        by_id = {r["job"]["id"]: r for r in doc["jobs"]}

        def root(r):
            seen = set()
            while r["job"].get("retry_of") in by_id and r["job"]["id"] not in seen:
                seen.add(r["job"]["id"])
                r = by_id[r["job"]["retry_of"]]
            return _id_number(r["job"]["id"])
        out: dict = {}
        for r in doc["jobs"]:
            g = r["job"].get("group")
            if g and r["job"]["id"] not in superseded:
                out.setdefault(g, []).append(r)
        for g in out:
            out[g].sort(key=root)
        return out

    def _drain_hooks(self) -> None:
        """Everything the application is owed, endings before merges: a fan's
        member ended before its fan settled."""
        self._drain_endings()
        self._drain_merges()

    def _drain_endings(self) -> None:
        """Tell the application about every job that has ended, outside the
        lock, oldest ending first. Paid only once the hook has returned, so a
        stop in between tells it again at the next start. A hook that raises
        is told as told - the error is kept on the row - because a debt that
        can never be paid would be retried for ever."""
        hook = getattr(self.hooks, "ended", None)
        while True:
            with self._lock:
                try:
                    doc = self._load()
                except (Refused, StoreUnreadable):
                    return
                owed = [r for r in doc["jobs"] if r["run"].get("ending_told") is False
                        and r["job"]["id"] not in self._telling]
                if not owed:
                    return
                owed.sort(key=lambda r: (_when(r["job"].get("finished")),
                                         _id_number(r["job"]["id"])))
                row = owed[0]
                jid = row["job"]["id"]
                self._telling[jid] = threading.get_ident()
                view = self._view(row)
                result = json.loads(json.dumps(row.get("result")))
                by = row["run"].get("ended_by")
                # S17: every job that ran beside it, at any moment of its run
                beside = list(row["run"].get("beside") or [])
            # when it ended, not when it was told; a job claimed before it was
            # told ended done
            self._record_event(view.get("correlation_id"), "ended", once=True,
                               ts=view.get("finished"), job=jid,
                               status="done" if view["status"] == "claimed" else view["status"],
                               error=view.get("error"), by=by, beside=beside)
            err, stop = None, False
            try:
                if hook is not None:
                    hook(view, result)
            except Exception as e:                           # noqa: BLE001
                err = f"{type(e).__name__}: {e}"[:300]
                self._say(f"the application's ended hook failed for {jid}: {err}")
            finally:
                with self._lock:
                    self._telling.pop(jid, None)
                    # a waiter wakes only once this block has let the lock go,
                    # so it reads the row with `ending_told` already written
                    self._told.notify_all()
                    try:
                        doc = self._load()
                        row = self._find(doc, jid)
                        if row is not None:
                            row["run"]["ending_told"] = True
                            if err:
                                row["run"]["ending_error"] = err
                            self._save(doc)
                    except (Refused, StoreUnreadable):
                        stop = True             # nothing to mark it on: stop draining
            # after the `finally`, never inside it: a `return` there would
            # swallow whatever the hook raised past its own handler
            if stop:
                return

    def _drain_merges(self) -> None:
        """Run every pending fan merge, outside the lock, once per settling."""
        merge = getattr(self.hooks, "merge", None)
        while True:
            with self._lock:
                try:
                    doc = self._load()
                except (Refused, StoreUnreadable):
                    return
                todo = None
                for g, rec in (doc.get("groups") or {}).items():
                    pend = self._pending(rec)
                    if pend and g not in self._merging:
                        todo = (g, pend[0])
                        break
                if todo is None:
                    return
                g, sig = todo
                self._merging.add(g)
                # the members AS THEY SETTLED, read off the settling itself: a
                # retry since may have revived the fan, and this report is
                # about the settling that happened, not about the fan now
                members = []
                for jid, _ in sig:
                    r = self._find(doc, jid)
                    if r is not None:
                        members.append({"job": self._view(r),
                                        "result": json.loads(json.dumps(r.get("result")))})
            err, stop = None, False
            try:
                if merge is not None:
                    merge(g, members)
            except Exception as e:                           # noqa: BLE001
                err = f"{type(e).__name__}: {e}"[:300]
                self._say(f"the merge of group {g} failed: {err}")
            finally:
                with self._lock:
                    self._merging.discard(g)
                    try:
                        doc = self._load()
                        rec = doc.setdefault("groups", {}).setdefault(
                            g, {"merged": None, "pending": [], "error": None})
                        pend = self._pending(rec)
                        if sig in pend:
                            pend.remove(sig)
                        rec["pending"] = pend
                        rec["merged"], rec["error"] = sig, err
                        self._save(doc)
                    except (Refused, StoreUnreadable):
                        stop = True             # nothing to mark it on: stop draining
            # after the `finally`, never inside it (see the endings)
            if stop:
                return

    # --------------------------------------------------------------- cancel

    def cancel(self, job_id: str) -> dict:
        """-> {job, was}. A queued job ends now. A running one is told to
        stop; if it never reached the backend it ends now too, otherwise the
        worker unwinds it and this waits up to `cancel_wait_s` to say so."""
        handles = []
        with self._lock:
            doc = self._load()
            row = self._find(doc, job_id)
            if row is None:
                raise Refused("no_such_job", f"no such job: {job_id}", 404)
            st = row["job"]["status"]
            if st in FINISHED:
                raise Refused("finished", f"{job_id} is {st}: there is nothing to stop",
                              409, status=st)
            if st in ("queued", "held"):
                # a held job (PLAN-4 D16) is as a queued one: nothing runs for it
                self._end(doc, row, "cancelled", by="cancel")
                self._resolve(doc)
                self._save(doc)
                pub = self.public(row)
                was = st
            else:
                row["run"]["cancel"] = True
                ev = self._events.get(job_id)
                if ev is not None:
                    ev.set()
                handles = list(row["run"].get("handles") or [])
                if not handles:
                    self._end(doc, row, "cancelled", by="cancel")
                    self._resolve(doc)
                self._save(doc)
                pub = self.public(row)
                was = "running"
        self._drain_hooks()
        if was == "running" and handles:
            self._tell_backend(handles[-1])
            pub = self._await_end(job_id) or pub
        return {"job": pub, "was": was}

    def give_up(self, job_id: str) -> dict:
        """Stronger than cancel: the queue is handed back whatever the worker
        is doing. The flag is set and a clock starts; if the job has not let go
        within `give_up_grace_s`, the SUPERVISOR ends it - a thread that does
        not need the worker to be healthy - and the job's model is free again,
        so the job behind it runs; the thread still stuck inside it is left
        to return (S17: the worker is never inside a job), and the other
        answers under way go on."""
        handles = []
        with self._lock:
            doc = self._load()
            row = self._find(doc, job_id)
            if row is None:
                raise Refused("no_such_job", f"no such job: {job_id}", 404)
            st = row["job"]["status"]
            if st in FINISHED:
                raise Refused("finished", f"{job_id} is {st}: there is nothing to give up",
                              409, status=st)
            if st in ("queued", "held"):
                self._end(doc, row, "cancelled", by="give_up")
                self._resolve(doc)
                self._save(doc)
                was = st
            else:
                row["run"]["cancel"] = True
                row["run"]["give_up_at"] = self._clock()
                ev = self._events.get(job_id)
                if ev is not None:
                    ev.set()
                handles = list(row["run"].get("handles") or [])
                self._save(doc)
                was = "running"
            pub = self.public(row)
        self._drain_hooks()
        if handles:
            self._tell_backend(handles[-1])
        return {"job": pub, "was": was, "grace_s": self.cfg["give_up_grace_s"]}

    def _tell_backend(self, handle) -> None:
        cancel = getattr(self.backend, "cancel", None)
        if cancel is None or not handle:
            return
        try:
            cancel(handle)
        except Exception:                                   # noqa: BLE001
            pass                                            # best effort, never blocking

    def _await_end(self, job_id: str):
        deadline = time.time() + float(self.cfg["cancel_wait_s"])
        while time.time() < deadline:
            try:
                with self._lock:
                    row = self._find(self._load(), job_id)
                    if row is None or row["job"]["status"] != "running":
                        return self.public(row) if row else None
            except (Refused, StoreUnreadable):
                return None
            time.sleep(0.02)
        return None

    # ---------------------------------------------------------------- retry

    def retry(self, job_id: str) -> dict:
        """Run a failed or cancelled job again, as a NEW job (`retry_of` names
        the old one, which keeps saying what happened to it). Whatever this
        job's end stopped - jobs waiting on it, the rest of its fan - is
        revived the same way, their `after` pointed at the new job, so a
        chain or a fan resumes where it stopped."""
        with self._lock:
            doc = self._load()
            row = self._find(doc, job_id)
            if row is None:
                raise Refused("no_such_job", f"no such job: {job_id}", 404)
            st = row["job"]["status"]
            if st not in ("failed", "cancelled"):
                raise Refused("not_retryable", f"{job_id} is {st}: only a failed or "
                              "cancelled job can be retried", 409, status=st)
            if job_id in self._superseded(doc):
                raise Refused("already_retried", f"{job_id} was already retried: "
                              "retry its latest attempt", 409, status=st)
            mapping = {}
            first = self._copy_as_retry(doc, row, mapping)
            revived, todo = [first], [job_id]
            while todo:
                old = todo.pop(0)
                for r in list(doc["jobs"]):
                    if r["run"].get("stopped_by") == old and \
                            r["job"]["id"] not in self._superseded(doc):
                        revived.append(self._copy_as_retry(doc, r, mapping))
                        todo.append(r["job"]["id"])
            self._resolve(doc)
            self._save(doc)
            for jid in revived:
                self._record_submitted(doc, self._find(doc, jid))
            out = {"job": self.public(self._find(doc, first)), "revived": revived[1:]}
        self._drain_hooks()
        self._wake.set()
        return out

    def _copy_as_retry(self, doc: dict, row: dict, mapping: dict) -> str:
        doc["seq"] += 1
        jid = f"j-{doc['seq']:04d}"
        mapping[row["job"]["id"]] = jid
        j = row["job"]
        new = {"job": {
            **{k: j[k] for k in ("kind", "name", "priority", "group", "scope", "client",
                                 "model", "n")},
            "id": jid, "status": "queued", "queue_pos": 0,
            "after": [mapping.get(a, a) for a in j.get("after") or []],
            "correlation_id": self._mint_correlation(doc), "correlation_source": "server",
            "claimed_by": None, "retry_of": j["id"],
            "n_done": 0, "progress": {}, "created": self._clock(), "finished": None,
            "stalled_for": None, "error": None},
            "input": json.loads(json.dumps(row.get("input"))),
            "gen": dict(row.get("gen") or {}), "result": None,
            "run": {"phase": "queued"}, "card_patience_s": row.get("card_patience_s")}
        if row.get("check") is not None:
            # marked to be checked: its retry is built again, and held again
            new["job"]["check"] = "marked"
            new["check"] = {"named": list(row["check"].get("named") or [])}
        # S18: its time - past by now, which is now - and whether it waits for
        # room, as it was submitted
        for k in ("not_before", "if_no_room"):
            if j.get(k) is not None:
                new["job"][k] = j[k]
        doc["jobs"].append(new)
        return jid

    # ------------------------------------------------------ forget and prune

    def _await_telling(self, jids=None) -> None:
        """Wait - at most `told_wait_s` - while ANOTHER thread tells the
        application about one of *jids* (any job, when None). `_drain_endings`
        leaves a job some other thread is telling to that thread, so without
        this a forget arriving mid-hook found the debt unpaid and refused a
        job that was one hook-return from forgettable.

        Never for a telling of this thread's own: a hook that forgets the job
        it is being told about is inside that telling, and would wait out the
        whole bound for itself before being refused anyway."""
        me = threading.get_ident()
        deadline = time.monotonic() + float(self.cfg["told_wait_s"])
        with self._lock:
            while any(t != me and (jids is None or j in jids)
                      for j, t in self._telling.items()):
                left = deadline - time.monotonic()
                if left <= 0:
                    return
                self._told.wait(left)

    def _needed(self, doc: dict, row: dict):
        """Why a finished row must stay: something still waits on it."""
        jid = row["job"]["id"]
        if row["run"].get("ending_told") is False:
            return "the application has not been told it ended yet"
        for r in doc["jobs"]:
            if r["job"]["status"] not in FINISHED and jid in (r["job"].get("after") or []):
                return f"{r['job']['id']} still waits on it"
        g = row["job"].get("group")
        if g:
            live = [m for m in self._groups(doc).get(g, [])
                    if m["job"]["status"] not in FINISHED]
            rec = (doc.get("groups") or {}).get(g) or {}
            if live or rec.get("pending"):
                return f"its group {g} has not been merged"
        return None

    def forget(self, job_id: str) -> dict:
        """Drop ONE finished job. Queued or running is refused: forgetting a
        job the worker holds would leave it running with nobody watching.
        An ending the application is still owed is told first, so a job
        forgotten the moment it ends is not refused for a debt about to be
        paid anyway - and one ANOTHER thread is telling right now is waited
        for, a bounded time, rather than refused as unpaid."""
        self._drain_endings()
        self._await_telling({job_id})
        with self._lock:
            doc = self._load()
            row = self._find(doc, job_id)
            if row is None:
                raise Refused("no_such_job", f"no such job: {job_id}", 404)
            st = row["job"]["status"]
            if st not in FINISHED:
                raise Refused("unfinished", f"{job_id} is {st}, not finished", 409, status=st)
            why = self._needed(doc, row)
            if why:
                raise Refused("needed", f"{job_id} cannot be forgotten: {why}", 409, status=st)
            doc["jobs"].remove(row)
            self._save(doc)
            return {"forgot": job_id}

    def prune(self, statuses=None, keep: int = 0, scope=None) -> dict:
        """Clear finished jobs, oldest first. `statuses` narrows it, `keep`
        holds back the newest N, `scope` narrows it to one key's jobs; nothing
        unfinished and nothing still needed is ever taken."""
        want = set(statuses or FINISHED) & set(FINISHED)
        keep = max(0, int(keep or 0))
        self._drain_endings()                    # owed endings first, as forget does
        self._await_telling()
        with self._lock:
            doc = self._load()
            cand = [r for r in doc["jobs"] if r["job"]["status"] in want
                    and (scope is None or r["job"].get("scope") == scope)
                    and not self._needed(doc, r)]
            cand.sort(key=lambda r: (r["job"]["created"], _id_number(r["job"]["id"])))
            drop = {id(r) for r in (cand[:-keep] if keep else cand)}
            before = len(doc["jobs"])
            doc["jobs"] = [r for r in doc["jobs"] if id(r) not in drop]
            if len(doc["jobs"]) != before:
                self._save(doc)
            return {"removed": before - len(doc["jobs"]), "left": len(doc["jobs"])}

    def _trim(self, doc: dict) -> None:
        cap = int(self.cfg["keep_finished"])
        done = [r for r in doc["jobs"] if r["job"]["status"] in FINISHED]
        if len(done) <= cap:
            return
        done.sort(key=lambda r: (r["job"]["created"], _id_number(r["job"]["id"])))
        extra = len(done) - cap
        drop = set()
        for r in done:
            if extra <= 0:
                break
            if not self._needed(doc, r):
                drop.add(id(r))
                extra -= 1
        doc["jobs"] = [r for r in doc["jobs"] if id(r) not in drop]

    # -------------------------------------------------------------- recovery

    def recover(self) -> dict:
        """What a stopped server left behind - every `running` row, however
        many answered at once. A `running` row is the lock that keeps every
        later job of its model queued, so none may survive this:

          * one whose cancel was asked for ends cancelled - the ask stands;
          * one whose result was being landed is landed now, not re-run;
          * one the backend can prove finished (every sample) is adopted;
          * any other goes back to the queue, keeping its place.

        Each backend question is bounded by `recover_timeout_s`: at boot the
        only question is "did it finish?", and a backend too slow to answer is
        answering no."""
        if self.store is None:
            return {"requeued": 0, "adopted": 0, "landed": 0, "cancelled": 0}
        counts = {"requeued": 0, "adopted": 0, "landed": 0, "cancelled": 0}
        to_land, settled = [], []
        with self._lock:
            doc = self._load()
            for r in doc["jobs"]:
                if r["job"]["status"] != "running":
                    continue
                run = r["run"]
                if run.get("cancel"):
                    self._end(doc, r, "cancelled", "cancelled before the server stopped",
                              by="recover")
                    counts["cancelled"] += 1
                    settled.append((r, "cancelled"))
                elif run.get("phase") == "landing" and r.get("result"):
                    to_land.append(r["job"]["id"])
                    settled.append((r, "landing"))
                else:
                    got = self._recover_samples(run.get("handles") or [], r["job"]["n"],
                                                r.get("gen"))
                    if got is not None:
                        r["result"] = {"candidates": got}
                        r["job"]["n_done"] = len(got)
                        run["phase"] = "landing"
                        to_land.append(r["job"]["id"])
                        counts["adopted"] += 1
                        settled.append((r, "adopted"))
                    else:
                        r["job"].update(status="queued", finished=None, error=None,
                                        n_done=0, progress={})
                        r["run"] = {"phase": "queued", "recovered": True,
                                    "samples": run.get("samples", 0)}
                        r["result"] = None
                        counts["requeued"] += 1
                        settled.append((r, "requeued"))
            self._resolve(doc)
            self._save(doc)
            self._recovered = True
            # what became of a run a stopped server left: its last attempt may
            # never have ended in the record, and this line says why
            for r, how in settled:
                self._record_event(r["job"].get("correlation_id"), "recovered",
                                   job=r["job"]["id"], settled_as=how)
        for jid in to_land:
            self._land(jid, None)
            counts["landed"] += 1
        self._drain_hooks()
        self._wake.set()
        return counts

    def _recover_samples(self, handles, n, gen=None):
        """Every sample's finished answer, as the backend proves it - or None
        when any one cannot be. A recovered answer is judged by the rule a
        fresh one is (`_candidate`): an empty one is nothing recovered -
        unless the job asked for none (`max_tokens` 0), as its `gen` says."""
        ask = getattr(self.backend, "recover", None)
        if ask is None or not handles or len(handles) != n:
            return None
        asked = {"max_tokens": (gen or {}).get("max_tokens")}
        got = []
        for h in handles:
            box = []
            t = threading.Thread(target=lambda h=h: box.append(_try(ask, h)),
                                 daemon=True, name="queue-recover")
            t.start()
            t.join(float(self.cfg["recover_timeout_s"]))
            cand = box[0] if box else None
            if not isinstance(cand, dict):
                return None
            try:
                got.append(self._candidate(cand, asked, None, None))
            except GenerationFailed:
                return None
        return got

    # ------------------------------------------------------ the worker engine

    def _start_worker(self) -> None:
        """Start a worker. Nothing is committed until the thread is running,
        so a start that raises leaves the record naming the one that ran."""
        old = self._worker
        if old is not None and old.is_alive():
            self._say(f"worker.two_engines: a second worker is starting while "
                      f"{old.name} is alive; the older one stops at its next wake")
        gen = self._beat["gen"] + 1
        t = threading.Thread(target=self._worker_loop, args=(gen,), daemon=True,
                             name=f"queue-worker-{gen}")
        t.start()
        self._worker, self._beat["gen"], self._live_from = t, gen, gen
        self._threads.append(t)

    def _worker_loop(self, gen: int) -> None:
        """Survives its own exceptions: a worker that skips a cycle is a
        hiccup, one that exits is an outage with no symptom. The deaths left
        possible are the BaseExceptions that are not Exceptions (SystemExit,
        KeyboardInterrupt) - and the supervisor replaces a worker they end."""
        self._beat["started_at"] = time.time()
        while not self._stop.is_set() and gen >= self._live_from:
            try:
                self._worker_cycle(gen)
            except Exception as e:                           # noqa: BLE001
                self._say(f"worker cycle failed, continuing: {type(e).__name__}: {e}")
                self._stop.wait(1.0)

    def _worker_cycle(self, gen: int) -> None:
        """One wake: start every job that can run now, each on a thread of its
        own (S17), until nothing more can."""
        self._wake.wait(timeout=float(self.cfg["tick_s"]))
        self._wake.clear()
        self._beat["cycle_at"] = time.time()
        if self._recovered:
            self._look()                         # S18: scheduled work that may go now
        while not self._stop.is_set() and gen >= self._live_from:
            picked = self._pick(gen)
            if picked is None:
                return
            self._launch(picked, gen)

    def _pick(self, gen: int):
        if not self._recovered:
            self.recover()                       # boot recovery could not read the store
        up = self._models_up()                   # S17: asked of the backend outside the lock
        with self._lock:
            if gen < self._live_from:
                return None                      # a retired worker picks nothing
            doc = self._load()
            r = self._next_to_run(doc, up)
            if r is None:
                return None
            r["job"].update(status="running", progress={}, n_done=0, error=None)
            r["run"].update(phase="building", started=self._clock(),
                            worker=self._me(gen), cancel=False, handles=[])
            beside = self._note_beside(doc, r)
            self._events[r["job"]["id"]] = threading.Event()
            self._save(doc)
            after = {a: json.loads(json.dumps(self._find(doc, a).get("result")))
                     for a in r["job"].get("after") or []}
            waited = [{"job": a, "correlation_id":
                       self._find(doc, a)["job"].get("correlation_id")}
                      for a in r["job"].get("after") or []]
            return {"id": r["job"]["id"], "view": self._view(r), "after": after,
                    "waited_on": waited, "gen": dict(r.get("gen") or {}),
                    "samples": int(r["run"].get("samples") or 0),
                    "check": json.loads(json.dumps(r.get("check"))),
                    "beside": beside}

    # ------------------------------------------------ answers at once (S17)
    #
    # The owner's ruling of 2026-09-28: different models answer at the same
    # time, each model one request at a time. The worker picks and starts;
    # every answer runs on a thread of its own (`_launch`), so a job whose
    # model is busy waits and holds back no job for another model.

    def _models_up(self):
        """The models the backend has up on the card now (`models_up()`), as a
        set; None for a backend with no such word - every model may then start
        beside the others. A backend that cannot say is answered as nothing
        up: no job then starts beside an answer - one at a time, as before
        S17. Asked outside the queue's lock, and it must answer at once."""
        ask = getattr(self.backend, "models_up", None)
        if ask is None:
            return None
        try:
            return {str(m) for m in (ask() or ())}
        except Exception:                                   # noqa: BLE001
            return set()

    def _next_to_run(self, doc: dict, up):
        """The job to start now, or None. In the queue's order - interactive
        first, then first come - the first whose `after` have all landed,
        whose model answers no other job now, while fewer than
        `answers_at_once` run. A job whose model is not up (`up`, the
        backend's word) waits while any answer runs: its load could need the
        room a running answer's model holds, and nothing in use comes off to
        make room - so it waits for those answers to end, never failing for
        room our own answers hold. Nothing behind it in the order starts
        meanwhile, so a load is never starved by answers that keep coming.

        NOTHING STARTS BEHIND A LOAD (S17b). A job that has started whose model
        is not up - its load under way, making its room - holds back every job
        not yet started, until its model is up or it has ended: a job started
        beside it could take in use the very model its load must take off, and
        the load then fails for room our own answers hold (found on the real
        card: a, b, a, b at `max_loads` 1 - the next a and the next b started
        in one pass, and a failed at once). "Behind" is every job not yet
        started, whatever its priority or its `after`: the load's room is
        decided against what is in use as it is made, and a load is never
        given up for a job that came later - so an interactive job waits one
        load at most. Jobs already running go on. On every card, not only a
        full one: the queue cannot know how much room the load needs."""
        running = [r for r in doc["jobs"] if r["job"]["status"] == "running"]
        if len(running) >= max(1, int(self.cfg["answers_at_once"])):
            return None
        if up is not None and any(r["job"].get("model") not in up for r in running):
            return None                          # S17b: a load under way - nothing behind it
        busy = {r["job"].get("model") for r in running}
        now = self._clock()
        for r in self._order(doc):
            if not self._runnable(doc, r) or self._held_back(r, now):
                continue                         # S18: held back, it holds back nobody
            model = r["job"].get("model")
            if model in busy:
                continue                         # its model answers now: it waits alone
            if running and up is not None and model not in up:
                return None                      # a load waits for the answers under way
            return r
        return None

    # ------------------------------------------------ scheduled work (S18)
    #
    # The owner's second ruling of 2026-09-28: scheduled work whose model does
    # not fit waits for room - or fails at once - by a setting, and a batch
    # may start no earlier than a time: "I can plan it earlier in the day and
    # just let the program run, then when i go to sleep it runs the task."
    # Both kinds wait HERE, queued - never on a thread holding the card, and
    # never counted as work the card is kept for - so they hold back nobody:
    # live work and other scheduled work go first. The queue looks for the
    # soonest of them every `room_look_s`, on the wall clock, and asks its
    # backend whether it may go (`room_look`): a coordinator's ticket
    # offered, or the card read with room for it.

    @staticmethod
    def _held_back(row: dict, now: float) -> bool:
        """Is queued *row* held back - never started now, and holding back
        nobody? A batch job planned for a time still to come; one whose time
        has come that the queue has not looked for since (its first look
        starts its patience, and asks its backend whether it may go); and one
        waiting - for room on the card, or for the card at its time - that its
        backend has not yet said may go (`_look`)."""
        if row["job"].get("status") != "queued":
            return False
        nb = row["job"].get("not_before")
        waits = row.get("waits")
        if nb is not None and (nb > now or waits is None):
            return True
        return waits is not None and not waits.get("due")

    def _patience_of(self, row: dict) -> float:
        """A job's patience for the card: its own, or its backend's - the
        server's (`card_patience_s`), an hour where it says none."""
        own = row.get("card_patience_s")
        if own is not None:
            return float(own)
        said = getattr(self.backend, "card_patience_s", None)
        if isinstance(said, bool) or not isinstance(said, (int, float)):
            return 3600.0
        return float(said)

    def _patience_left(self, row: dict):
        """What a job's generation is handed as its patience: its own - None,
        the server's - or, for one that has waited already (S18: at its time,
        or for room), what is left of it, counted on the wall clock from its
        first look (`waits.since`), across a restart too."""
        waits = row.get("waits")
        if not waits or waits.get("since") is None:
            return row.get("card_patience_s")
        return max(0.0, self._patience_of(row) - (self._clock() - float(waits["since"])))

    def _room_progress(self, row: dict, room: dict, now: float) -> dict:
        """What a job waiting for room says, as its progress: the words a
        person reads - the numbers, what programs outside the bootstrap hold,
        and that it starts by itself once it fits - the numbers alone, how
        long it has waited and how much patience is left."""
        waits = row.get("waits") or {}
        waited = max(0.0, now - float(waits.get("since", now)))
        left = max(0.0, self._patience_of(row) - waited)
        memory = room.get("memory") or {}
        return {"stage": "waiting for room on the card",
                "waiting_for": f"{row['job'].get('model')}: {room.get('words')}",
                "free_mib": memory.get("free_mib"), "need_mib": memory.get("need_mib"),
                "outside_mib": room.get("outside_mib"), "waited_s": int(waited),
                "patience_left_s": int(math.ceil(left)), "at": now}

    def _ran_out(self, row: dict, waited: float, patience: float) -> tuple:
        """A scheduled job whose patience ran out while it waited: -> (error,
        reason). Waiting for room, it fails `no_room`, in the loud words a
        live job's no room is said in - the numbers, what programs outside
        the bootstrap hold, what to do - and how long it waited: never only
        that its patience ran out. Waiting for the card at its time,
        `card_patience`, naming nobody."""
        model = row["job"].get("model")
        room = (row.get("waits") or {}).get("last")
        if isinstance(room, dict) and isinstance(room.get("memory"), dict):
            verdict = {"verdict": "no_room", **room["memory"]}
            return (f"{model} did not fit on the card within its patience of "
                    f"{_MEM.span(patience)}: "
                    f"{_MEM.no_room_words(verdict, room.get('outside_mib'), waited_s=waited)}",
                    "no_room")
        since = time.strftime("%H:%M", time.localtime(self._clock() - waited))
        return (f"no card within its patience of {_MEM.span(patience)} (card_patience): it "
                f"waited for the card from {since}, and the card was not had", "card_patience")

    def _park(self, jid: str, gen: int, room: dict) -> None:
        """A batch job whose model does not fit, waiting for room (S18): back
        in the queue - queued, its progress the room it waits on - and not
        started again until its backend says it may go (`_look`). Its patience
        counts from the first time it waited, kept across a restart."""
        with self._lock:
            doc = self._load()
            row = self._mine(doc, jid, gen)
            if row is None:
                return None
            now = self._clock()
            was = row.get("waits") or {}
            row["waits"] = {"since": was.get("since", now), "due": False, "for": "room",
                            "last": json.loads(json.dumps(room))}
            row["job"].update(status="queued", finished=None, error=None, n_done=0)
            row["job"]["progress"] = self._room_progress(row, room, now)
            row["run"] = {"phase": "queued", "samples": row["run"].get("samples", 0),
                          "parked": True}
            row["result"] = None
            self._positions(doc)
            self._save(doc)
            corr = row["job"].get("correlation_id")
        self._events.pop(jid, None)
        memory = room.get("memory") or {}
        self._record_event(corr, "waits_for_room", job=jid, free_mib=memory.get("free_mib"),
                           need_mib=memory.get("need_mib"), outside_mib=room.get("outside_mib"),
                           words=str(room.get("words") or "")[:500])
        # nothing holds the card for a job that waits: its backend is asked at
        # once whether the work it holds the card for is done (`settle`) -
        # never left to its next idle sweep, the card held meanwhile (found by
        # building: the owner's case retried its batch 141 times inside the
        # server's own rental before the sweep let it go)
        settle = getattr(self.backend, "settle", None)
        if settle is not None:
            try:
                settle()
            except Exception as e:                           # noqa: BLE001 - never fails the park
                self._say(f"the backend could not settle after {jid} went back to wait: "
                          f"{type(e).__name__}: {e}"[:300])
        return None

    def _look(self) -> None:
        """THE LOOK (S18), at most every `room_look_s`: every queued job held
        back for its time or for room, on this queue's wall clock - so a
        machine that slept through a job's time starts it at its first look
        after it wakes. A job whose time has come is looked for from now on
        (its patience starts here); one whose patience ran out while it
        waited fails, saying why (`_ran_out`). The soonest of them - one due
        by its time before one planned, then the queue's order - is the one
        its backend is asked about (`room_look(want)`, outside the lock; None
        when nothing waits, so a place kept in line for it is given up), and
        the answer says whether it may go now, and the room it waits on."""
        at = time.monotonic()
        if at < self._look_at:
            return
        self._look_at = at + float(self.cfg["room_look_s"])
        ended = False
        with self._lock:
            try:
                doc = self._load()
            except (Refused, StoreUnreadable):
                return
            now = self._clock()
            changed, waiting = False, []
            for pos, r in enumerate(self._order(doc)):
                nb, waits = r["job"].get("not_before"), r.get("waits")
                if nb is None and waits is None:
                    continue
                if waits is not None and waits.get("due"):
                    continue                    # it may go: the worker starts it
                if nb is None or nb <= now:
                    if waits is None:
                        r["waits"] = waits = {"since": now, "due": False, "for": "card",
                                              "last": None}
                        changed = True
                    patience = self._patience_of(r)
                    waited = now - float(waits["since"])
                    if waited >= patience:
                        error, reason = self._ran_out(r, waited, patience)
                        self._end(doc, r, "failed", error[:1000], by="worker", reason=reason)
                        changed = ended = True
                        continue
                    waiting.append(((0, 0.0, pos), r))
                else:
                    waiting.append(((1, float(nb), pos), r))
            head = min(waiting, key=lambda x: x[0])[1] if waiting else None
            want = None if head is None else {
                "job": head["job"]["id"], "model": head["job"].get("model"),
                "not_before": head["job"].get("not_before"),
                "for": head["job"].get("correlation_id"),
                "if_no_room": head["job"].get("if_no_room"),
                "waited": bool((head.get("waits") or {}).get("last"))}
            if changed:
                self._resolve(doc)
                self._save(doc)
        if ended:
            self._drain_hooks()
        ask = getattr(self.backend, "room_look", None)
        if ask is None:
            answer = None if want is None else {"due": True, "room": None}
        else:
            try:
                answer = ask(want)
            except Exception as e:                           # noqa: BLE001 - a look never fails
                self._say(f"the backend could not say whether a scheduled job may go: "
                          f"{type(e).__name__}: {e}"[:300])
                return
        if want is None or not isinstance(answer, dict):
            return
        with self._lock:
            doc = self._load()
            r = self._find(doc, want["job"])
            if r is None or r["job"]["status"] != "queued":
                return
            now = self._clock()
            nb = r["job"].get("not_before")
            if nb is not None and nb > now:
                return                          # before its time, it never goes
            waits = r.setdefault("waits", {"since": now, "due": False, "for": "card",
                                           "last": None})
            room = answer.get("room")
            if isinstance(room, dict) and isinstance(room.get("memory"), dict):
                waits["last"] = json.loads(json.dumps(room))
            if isinstance(waits.get("last"), dict):
                r["job"]["progress"] = self._room_progress(r, waits["last"], now)
            waits["due"] = bool(answer.get("due"))
            self._save(doc)
            go = waits["due"]
        if go:
            self._wake.set()

    def _scheduled(self, rows: list):
        """What health says of the scheduled work that waits (S18), or None
        when none does: how many are planned for a time still to come and the
        soonest time, how many wait for room, and the first one's words."""
        now = self._clock()
        planned, room = [], []
        for r in rows:
            if not isinstance(r, dict) or r.get("job", {}).get("status") != "queued":
                continue
            nb = r["job"].get("not_before")
            if nb is not None and nb > now:
                planned.append(float(nb))
            elif (r.get("waits") or {}).get("for") == "room":
                room.append(r)
        if not planned and not room:
            return None
        return {"planned": len(planned), "next_at": min(planned) if planned else None,
                "for_room": len(room),
                "room": (room[0]["job"].get("progress") or {}).get("waiting_for") if room
                else None}

    @staticmethod
    def _note_beside(doc: dict, row: dict) -> list:
        """*row* starts: what runs beside it now - [{job, model}] - kept on its
        run, and it added to theirs, so each job's record says every job that
        ran beside it (`beside`, on its `ended` line). -> what runs now."""
        me = {"job": row["job"]["id"], "model": row["job"].get("model")}
        now = []
        for r in doc["jobs"]:
            if r is row or r["job"]["status"] != "running":
                continue
            now.append({"job": r["job"]["id"], "model": r["job"].get("model")})
            r["run"]["beside"] = list(r["run"].get("beside") or []) + [me]
        row["run"]["beside"] = list(now)
        return now

    def _launch(self, picked: dict, gen: int) -> None:
        """Start *picked*'s run on a thread of its own. A thread that cannot
        be started fails its job, saying so - it is running, and nothing else
        would ever end it."""
        jid = picked["id"]
        t = threading.Thread(target=self._answer, args=(picked, gen), daemon=True,
                             name=f"queue-answer-{jid}")
        self._answering[jid] = t
        try:
            t.start()
        except BaseException as e:
            self._answering.pop(jid, None)
            self._finish(jid, gen, "failed", f"the queue could not start this job's run: "
                                             f"{type(e).__name__}: {e}"[:300])
            raise

    def _answer(self, picked: dict, gen: int) -> None:
        """One job's run, on its own thread; the worker is woken once it has
        returned, so the next job for its model starts."""
        jid = picked["id"]
        try:
            self._run(picked, gen)
        except BaseException as e:                           # noqa: BLE001
            self._say(f"the run of {jid} ended in the queue's own failure: "
                      f"{type(e).__name__}: {e}"[:300])
        finally:
            if self._answering.get(jid) is threading.current_thread():
                self._answering.pop(jid, None)
            self._wake.set()

    @property
    def _worker_job(self):
        """(worker generation, job id) of a run a thread is inside, or None:
        none is."""
        return next(((g, j) for j, g in list(self._inside.items())), None)

    def wants(self, model) -> bool:
        """Does a job that can run - queued, every `after` landed - name
        *model*? What a runner asks when a cancel lands while that model loads
        (E3): the load goes on for such a job, and is given up for none. It
        reads; it takes nothing. Nothing bound, or a store that cannot be
        read, wants nothing."""
        with self._lock:
            try:
                doc = self._load()
            except (Refused, StoreUnreadable):
                return False
            return any(r["job"].get("model") == model and self._runnable(doc, r)
                       for r in self._order(doc))

    def _me(self, gen: int) -> str:
        return f"{self._instance}:{gen}"

    def _mine(self, doc: dict, jid: str, gen: int):
        row = self._find(doc, jid)
        if row and row["job"]["status"] == "running" and row["run"].get("worker") == self._me(gen):
            return row
        return None

    def _run(self, picked: dict, gen: int) -> None:
        jid = picked["id"]
        self._inside[jid] = gen
        # THE QUEUE'S OWN SHARE of the job (PLAN-4 D12): from its pick to its
        # end written, less its time in the backend - told to a backend that
        # keeps it (`job_share`), under the model it named, once it ran one
        began = time.monotonic()
        self._in_backend[jid] = [0.0, None]
        try:
            self._run_body(picked, gen)
            inside, model = self._in_backend.pop(jid, (0.0, None))
            share = getattr(self.backend, "job_share", None)
            if share is not None and model is not None:
                try:
                    share(model, time.monotonic() - began - inside)
                except Exception as e:                      # noqa: BLE001 - never fails the job
                    self._say(f"the backend could not keep the job's share: {e}")
        except BaseException as e:
            with self._lock:
                try:
                    doc = self._load()
                    row = self._mine(doc, jid, gen)
                    if row is not None:
                        self._end(doc, row, "failed",
                                  f"the queue itself failed while running this job: "
                                  f"{type(e).__name__}: {e}"[:300], by="worker")
                        self._resolve(doc)
                        self._save(doc)
                except (Refused, StoreUnreadable):
                    pass
            raise
        finally:
            # the fan this job settled is merged BEFORE the worker reads as
            # free, so "idle" means the consequences have happened too
            try:
                self._drain_hooks()
            finally:
                # only OUR claim is cleared: a run of an older worker must not
                # erase the claim of a newer one's
                if self._inside.get(jid) == gen:
                    self._inside.pop(jid, None)

    def _finish(self, jid: str, gen: int, status: str, error=None, result=None,
                reason=None) -> None:
        with self._lock:
            doc = self._load()
            row = self._mine(doc, jid, gen)
            if row is None:
                self._say(f"a late answer for {jid} arrived after the queue had "
                          "taken it back; it was not written")
                return
            if result is not None:
                row["result"] = result
            self._end(doc, row, status, error, by="worker", reason=reason)
            self._resolve(doc)
            self._save(doc)
        self._events.pop(jid, None)

    def _run_body(self, picked: dict, gen: int) -> None:
        jid, view, g = picked["id"], picked["view"], picked["gen"]
        ev = self._events.get(jid) or threading.Event()
        if ev.is_set():
            return self._finish(jid, gen, "cancelled")
        # A HELD REQUEST THAT WAS SENT (PLAN-4 D16) runs what was sent - as it
        # was built, or changed for this one request - and is never built
        # again: what the person saw is what the model is asked
        sent = (picked.get("check") or {}).get("send")
        if sent is not None:
            request, params = sent["request"], dict(sent["params"])
        else:
            try:
                request = self.hooks.build_request(view, picked["after"])
            except Exception as e:                           # noqa: BLE001
                return self._finish(jid, gen, "failed",
                                    f"could not build the request: {type(e).__name__}: {e}"[:300])
            try:
                chosen = dict(self.hooks.sampling_for(view["kind"], view.get("input")) or {})
                params = {k: v for k, v in g.items() if k != "n"}
                params.update(chosen)
            except Exception as e:                           # noqa: BLE001
                return self._finish(jid, gen, "failed",
                                    f"could not choose the sampling: {type(e).__name__}: {e}"[:300])
            # THE QUEUE'S OWN ARE THE JOB'S, as it was submitted: its record
            # names the model it runs, how many answers and how often a
            # refused one is asked again. A sampling that named one would run
            # what the record does not say, so it fails the job, and nothing
            # runs
            own = [k for k in GEN_OWN if k in chosen]
            if own:
                return self._finish(jid, gen, "failed",
                                    f'could not choose the sampling: it names "{own[0]}", which '
                                    f"is the job's own, as it was submitted - a sampling names "
                                    f"the model's parameters, never the queue's")
            # what the application's sampling names is held to the contract's
            # list too: a parameter no model reads fails the job, never sent
            # - and to the lengths llama-server takes
            wrong = gen_problems(params)
            why = wrong[0] if wrong else length_problem(params) or range_problem(params)
            if why:
                return self._finish(jid, gen, "failed",
                                    f"could not choose the sampling: {why}"[:300])
            if picked.get("check") is not None:
                return self._hold(picked, gen, request, params, chosen, ev)
        with self._lock:
            doc = self._load()
            row = self._mine(doc, jid, gen)
            if row is None:
                return None
            row["run"]["phase"] = "generating"
            self._save(doc)
        # a sent request's record keeps what was built, written when it was
        # held: its run adds only that it started
        self._record_start(picked, request, built=sent is None)
        n = max(1, int(g.get("n") or 1))
        state = {"samples": picked["samples"], "spent": 0}
        cands, err, why, cancelled, room = [], None, None, False, None
        for v in range(1, n + 1):
            try:
                cand = self._generate_with_retries(jid, gen, view, request, params,
                                                   v, n, ev, state)
            except Cancelled:
                cancelled = True
                break
            except GenerationFailed as e:
                err, why = str(e) or "the generation failed", getattr(e, "reason", None)
                room = getattr(e, "waits_for_room", None)
                break
            except Exception as e:                           # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
                break
            cands.append(cand)
            state["spent"] += int(cand.get("tokens") or 0)
            with self._lock:
                doc = self._load()
                row = self._mine(doc, jid, gen)
                if row is None:
                    return None
                row["job"]["n_done"] = len(cands)
                row["job"]["progress"] = {**row["job"]["progress"],
                                          "spent": state["spent"]}
                row["result"] = {"candidates": cands}
                self._save(doc)
        if cancelled:
            return self._finish(jid, gen, "cancelled", None, {"candidates": cands})
        if err and isinstance(room, dict) and not cands:
            # S18: its model does not fit, and it waits for room - back in the
            # queue, holding back nobody, until its backend says it may go
            return self._park(jid, gen, room)
        if err:
            return self._finish(jid, gen, "failed", err[:500], {"candidates": cands},
                                reason=why)
        with self._lock:
            doc = self._load()
            row = self._mine(doc, jid, gen)
            if row is None:
                return None
            row["run"]["phase"] = "landing"
            row["result"] = {"candidates": cands}
            self._save(doc)
        self._land(jid, gen)
        return None

    # ------------------------------------------------ the request check (D16)

    def _hold(self, picked: dict, gen: int, request, params: dict, chosen: dict, ev) -> None:
        """A MARKED JOB'S TURN: its request built, it is described - by its
        backend (`hold`), which may load its model to count it as a turn
        does - and then held: `held`, with the exact request as it would be
        sent, until a person sends it or cancels it. It is not running from
        then on, so the stall watch never takes it back and the worker goes
        on to the next job: a held job waits for no one. Anything the turn
        itself would fail on - a request its model cannot be asked, a card
        refused - fails it here, in the same words and reason."""
        jid, view = picked["id"], picked["view"]
        corr = view.get("correlation_id")
        handle = ids.handle(self.actor, jid, 0)
        with self._lock:
            doc = self._load()
            row = self._mine(doc, jid, gen)
            if row is None:
                return None
            row["run"]["phase"] = "checking"
            row["job"]["progress"] = {"stage": "checking the request", "at": self._clock(),
                                      "handle": handle}
            terms = {"priority": row["job"].get("priority"),
                     "card_patience_s": self._patience_left(row),
                     "if_no_room": row["job"].get("if_no_room")}
            self._save(doc)
        named = sorted(set((picked.get("check") or {}).get("named") or []) | set(chosen))
        ask = getattr(self.backend, "hold", None)
        try:
            if ask is None:
                described = self._described(params, named)
            else:
                described = dict(ask(json.loads(json.dumps(request)), dict(params),
                                     Run(self, jid, handle, ev, corr, None, **terms), named))
        except Cancelled:
            return self._finish(jid, gen, "cancelled")
        except GenerationFailed as e:
            room = getattr(e, "waits_for_room", None)
            if isinstance(room, dict):
                # S18: its model does not fit, and it waits for room: back in
                # the queue, and checked when its turn comes again
                return self._park(jid, gen, room)
            return self._finish(jid, gen, "failed",
                                (str(e) or "the request could not be checked")[:500],
                                reason=getattr(e, "reason", None))
        except Exception as e:                               # noqa: BLE001
            return self._finish(jid, gen, "failed",
                                f"could not check the request: {type(e).__name__}: {e}"[:300])
        # a cancel the backend did not hear has ended the job already: it is
        # not ours below, and nothing is held
        held = {"request": request, "model": str(params.get("model") or ""),
                "gen": _engine_reads({k: v for k, v in params.items() if k not in GEN_OWN},
                                     described.get("reads")),
                **{k: described.get(k, v) for k, v in HELD_PARTS.items()},
                "held_at": self._clock()}
        with self._lock:
            doc = self._load()
            row = self._mine(doc, jid, gen)
            if row is None:
                return None
            row["check"]["held"] = held
            row["job"].update(status="held", check="held",
                              progress={"stage": "held", **({"count": held["count"]}
                                                           if held["count"] else {})})
            row["run"]["phase"] = "held"
            self._positions(doc)
            self._save(doc)
            pub = self.public(row)
            self._ended.notify_all()            # a result that waits answers at held (R-B3)
        self._events.pop(jid, None)
        self._record_start(picked, request, event=None)
        self._record_write(corr, "held.json", {"job": pub, **held})
        self._record_event(corr, "held", job=jid)
        return None

    @staticmethod
    def _described(params: dict, named) -> dict:
        """A held request as the queue alone can say it, for a backend that
        says nothing of one: each parameter it will be sent, the
        application's where it named it (the job, or its sampling hook),
        else the bootstrap's own - and that nothing more is known."""
        return {**HELD_PARTS, "said": [HELD_UNSAID], "params": [
            {"name": k, "value": v, "from": "application" if k in named else "bootstrap"}
            for k, v in sorted(params.items()) if k not in GEN_OWN and v is not None]}

    def held(self, job_id: str) -> dict:
        """A held job's request, exactly as it would be sent, and what is
        known of it (the contract's `held_request`). Refused `no_such_job`
        (404), and `not_held` (409, with its status) for any job that is not
        waiting to be checked."""
        with self._lock:
            doc = self._load()
            row = self._held_row(doc, job_id)
            return {"job": self.public(row), **json.loads(json.dumps(row["check"]["held"]))}

    def _held_row(self, doc: dict, job_id: str) -> dict:
        row = self._find(doc, job_id)
        if row is None:
            raise Refused("no_such_job", f"no such job: {job_id}", 404)
        st = row["job"]["status"]
        if st != "held" or not (row.get("check") or {}).get("held"):
            raise Refused("not_held", f"{job_id} is not held: it is {st}, and only a job "
                          f"submitted with check: true is held, once its request is built",
                          409, status=st)
        return row

    def send(self, job_id: str, request=AS_BUILT, gen=None, unset=None) -> dict:
        """SEND A HELD REQUEST - as it was built, or changed for this one
        request: `request` replaces it whole, `gen` sets parameters over the
        ones it was built with and `unset` leaves some unnamed again. Held to
        what a submit is, and refused the same way; never the queue's own
        three. The job goes back in the queue, in its place, and its turn
        runs what was sent - never built again. -> {job, tuned, changed}:
        tuned when what is sent differs from what was built, naming each
        thing that did. The record keeps both (`sent.json` beside
        `request.json` and `held.json`), and the job says `tuned` or `sent`
        for as long as it is kept."""
        gen = {} if gen is None else gen
        wrong = _V.check(gen, SEND_GEN_SHAPE, _SHAPES, at="$.gen")
        if wrong:
            raise Refused("bad_request", wrong[0], 400)
        unset = [] if unset is None else unset
        wrong = _V.check(unset, {"array": "string"}, _SHAPES, at="$.unset")
        if wrong:
            raise Refused("bad_request", wrong[0], 400)
        for i, name in enumerate(unset):
            if name not in SEND_FIELDS:
                raise Refused("bad_request", f"$.unset[{i}]: {json.dumps(name)} is not a "
                              f"parameter a send may leave unnamed - never one of the queue's "
                              f"own three (model, n, retries), which the job names as it was "
                              f"submitted", 400)
        with self._lock:
            doc = self._load()
            row = self._held_row(doc, job_id)
            held = json.loads(json.dumps(row["check"]["held"]))
            own = {k: row["gen"][k] for k in GEN_OWN if k != "n" and k in (row.get("gen") or {})}
        built = {**own, **held["gen"]}
        params = {k: v for k, v in built.items() if k not in unset}
        params.update(json.loads(json.dumps(gen)))
        wrong = gen_problems(params)
        if wrong:
            raise Refused("bad_request", wrong[0], 400)
        why = length_problem(params)
        if why:
            raise Refused("bad_max_tokens", why, 400)
        why = range_problem(params)
        if why:
            raise Refused("out_of_range", why, 400)
        asked = held["request"] if request is AS_BUILT else json.loads(json.dumps(request))
        check = getattr(self.backend, "check_sent", None)
        if check is not None:
            check(json.loads(json.dumps(asked)), dict(params))
        changed = (["request"] if asked != held["request"] else []) + sorted(
            k for k in set(built) | set(params) if built.get(k) != params.get(k))
        tuned = bool(changed)
        with self._lock:
            doc = self._load()
            # sent or cancelled meanwhile, it is no longer held: once sent, a
            # job is never held again
            row = self._held_row(doc, job_id)
            at = self._clock()
            row["check"].pop("held", None)
            row["check"]["send"] = {"request": asked, "params": params, "tuned": tuned,
                                    "changed": changed, "at": at}
            row["job"].update(status="queued", check="tuned" if tuned else "sent",
                              progress={}, error=None)
            row["run"] = {"phase": "queued", "samples": int(row["run"].get("samples") or 0)}
            self._resolve(doc)
            self._positions(doc)
            self._save(doc)
            pub = self.public(row)
            corr = row["job"].get("correlation_id")
        self._record_write(corr, "sent.json", {
            "tuned": tuned, "changed": changed, "request": asked,
            "gen": {k: v for k, v in params.items() if k not in GEN_OWN}, "at": at})
        self._record_event(corr, "sent", job=job_id, tuned=tuned, changed=changed)
        self._wake.set()
        return {"job": pub, "tuned": tuned, "changed": changed}

    def _land(self, jid: str, gen) -> None:
        """The application takes the result; then, and only then, it is done.
        A land hook that raises does not take the result away: the job is
        done and its progress says the landing failed."""
        with self._lock:
            doc = self._load()
            row = self._find(doc, jid)
            if row is None or row["job"]["status"] != "running":
                return
            if gen is not None and row["run"].get("worker") != self._me(gen):
                return
            view = self._view(row)
            result = json.loads(json.dumps(row.get("result") or {"candidates": []}))
        land_error = None
        try:
            out = self.hooks.land_result(view, result)
            if isinstance(out, dict):
                result = out
        except Exception as e:                               # noqa: BLE001
            land_error = f"{type(e).__name__}: {e}"[:300]
            self._say(f"the application could not land {jid}: {land_error}")
        with self._lock:
            doc = self._load()
            row = self._find(doc, jid)
            if row is None or row["job"]["status"] != "running":
                return
            row["result"] = result
            if land_error:
                row["job"]["progress"] = {**row["job"]["progress"], "land_error": land_error}
            self._end(doc, row, "done", by="worker" if gen is not None else "recover")
            self._resolve(doc)
            self._save(doc)
        self._events.pop(jid, None)

    def _generate_with_retries(self, jid, gen, view, request, params, variant, n,
                               ev, state) -> dict:
        """One variant, tried up to RETRY_MAX times while the application's
        judge refuses it. The seed moves on every attempt, so a retry is never
        a replay; sample numbers run on across attempts and variants.

        A REFUSED LAST ATTEMPT FAILS THE JOB: it raises, so the answer the
        judge refused is never handed on as one it accepted - never landed,
        never offered, never built on. Its verdict is in the request's record
        first, and the job's error is the judge's own why."""
        asked = params.get("retries")
        tries = RETRY_MAX if asked is None else max(1, min(RETRY_MAX, 1 + int(asked)))
        seed = params.get("seed")
        base = None if seed is None else int(seed) + (variant - 1)
        judge = getattr(self.hooks, "judge", None)
        corr = view.get("correlation_id")
        attempts, cand = [], None
        for attempt in range(1, tries + 1):
            s = None if base is None else base + 100 * (attempt - 1)
            p = {k: v for k, v in params.items() if k != "retries"}
            p["seed"] = s
            cand, sample = self._call_through_the_card(jid, gen, request, p, variant, n, ev,
                                                       state, corr)
            try:
                verdict = judge(view, cand) if judge is not None else None
                if verdict is not None:
                    verdict = dict(verdict)
            except Exception as e:
                self._record_event(corr, "attempt_ended", job=jid, sample=sample,
                                   outcome="failed",
                                   error=f"the judge failed: {type(e).__name__}: {e}"[:500])
                raise
            why = None if verdict is None else verdict.get("why")
            self._record_event(corr, "attempt_ended", job=jid, sample=sample,
                               outcome="refused" if why else "answered", verdict=verdict)
            if verdict is None:
                return cand
            attempts.append({"attempt": attempt, "sample": sample, "seed": s, "why": why})
            cand["verdict"] = verdict
            if not why:
                break
            if attempt == tries:
                of = f" of variant {variant} of {n}" if n > 1 else ""
                raise GenerationFailed(f"the judge refused the last attempt "
                                       f"({attempt} of {tries}){of}: {why}",
                                       reason="judge_refused")
        cand["attempts"] = attempts
        return cand

    def _call_through_the_card(self, jid, gen, request, params, variant, n, ev, state,
                               corr) -> tuple:
        """One attempt - and, S5b, THE CARD FULL RECOVERED BY ITSELF: an
        attempt the card's memory broke (the backend says so: `card_full` on
        its failure) is thrown away and run again from the start, with the
        same seed, as a sample of its own - never counted as the model's
        failure, against the job's `retries` or its judge. The backend holds
        the run again until the card has room (the runner: until its card
        source says so, within the job's patience, counted from the first
        break - `Run.card_waited_s`). The record says each: an
        `attempt_card_full` line - which attempt, when it ended, why, and what
        the card read - and the next attempt's own lines. A card that fills
        again each time is not a loop: after `recover_attempts` holds the job
        fails `card_degraded`, naming each attempt. -> (candidate, sample)."""
        broke, first = [], None
        while True:
            state["samples"] += 1
            sample = state["samples"]
            waited = None if first is None else time.monotonic() - first
            try:
                return self._call_backend(jid, gen, request, params, sample, variant, n, ev,
                                          state, card_waited_s=waited), sample
            except GenerationFailed as e:
                full = e.card_full
                if full is None:
                    raise
                if first is None:
                    first = time.monotonic()
                at = self._clock()
                broke.append((at, str(e)))
                most = int(self.cfg["recover_attempts"])
                if len(broke) > most:
                    each = "; ".join(
                        f"attempt {i} ended at {time.strftime('%H:%M:%S', time.localtime(t))}"
                        + (f" ({why[:160]})" if i == 1 else "")
                        for i, (t, why) in enumerate(broke, 1))
                    raise GenerationFailed(f"the card ran out of memory each time this request "
                                           f"ran, {len(broke)} times, and it is not held again "
                                           f"after {most}: {each}",
                                           reason="card_degraded") from None
                self._record_event(corr, "attempt_card_full", job=jid, sample=sample,
                                   attempt=len(broke), error=str(e)[:500],
                                   free_mib=full.get("free_mib"), need_mib=full.get("need_mib"),
                                   outside_mib=full.get("outside_mib"))

    def _call_backend(self, jid, gen, request, params, sample, variant, n, ev, state,
                      card_waited_s=None):
        if ev.is_set():
            raise Cancelled("cancelled before the backend was asked")
        handle = ids.handle(self.actor, jid, sample)
        with self._lock:
            doc = self._load()
            row = self._mine(doc, jid, gen)
            if row is None:
                raise Cancelled("the queue took this job back")
            row["run"].setdefault("handles", []).append(handle)
            row["run"]["samples"] = sample
            # THE BUDGET IS THE MOST ITS ANSWERS CAN TAKE: `max_tokens` for each
            # of its `n` - the bootstrap's own where none is named, as the
            # runner sends it. -1 names no limit, so there is no most: no
            # budget, never a negative one
            most = params.get("max_tokens")
            most = GEN_DEFAULTS["max_tokens"] if most is None else int(most)
            progress = {"variant": variant, "of": n, "spent": state["spent"]}
            if most >= 0:
                progress["budget"] = most * n
            progress.update(at=self._clock(), handle=handle)
            row["job"]["progress"] = progress
            corr = row["job"].get("correlation_id")
            terms = {"priority": row["job"].get("priority"),
                     "card_patience_s": self._patience_left(row),
                     "card_waited_s": card_waited_s,
                     "if_no_room": row["job"].get("if_no_room")}
            self._save(doc)
        self._record_write(corr, f"samples/{sample:02d}/params.json",
                           {"sample": sample, "handle": handle, "variant": variant,
                            "of": n, "params": params})
        self._record_event(corr, "attempt_started", job=jid, sample=sample, handle=handle,
                           seed=params.get("seed"))
        try:
            if self.backend is None:
                raise GenerationFailed("no backend is configured: nothing can generate")
            asked = time.monotonic()
            try:
                out = self.backend(request, params, Run(self, jid, handle, ev, corr, sample,
                                                        **terms))
            finally:
                timed = self._in_backend.get(jid)
                if timed is not None:
                    timed[0] += time.monotonic() - asked
                    timed[1] = params.get("model")
            if ev.is_set():
                raise Cancelled("cancelled while generating")
            cand = self._candidate(out, params, sample, handle)
        except BaseException as e:
            said = str(e) if isinstance(e, (Cancelled, GenerationFailed)) \
                else f"{type(e).__name__}: {e}"
            self._record_event(corr, "attempt_ended", job=jid, sample=sample,
                               outcome="cancelled" if isinstance(e, Cancelled) else "failed",
                               error=said[:500])
            raise
        self._record_write(corr, f"samples/{sample:02d}/candidate.json", cand)
        return cand

    @staticmethod
    def _candidate(out, params, sample, handle) -> dict:
        if not isinstance(out, dict):
            out = {"text": "" if out is None else str(out)}
        cand = dict(out)
        text = str(cand.get("text") or "")
        # AN EMPTY ANSWER IS A FAILURE - unless it is what the job asked for:
        # `max_tokens` 0 reads the prompt into the cache and asks for none. It
        # is the text engine's rule: a transcript of silence - no words, no
        # text - is an answer (PLAN-4 D10)
        if not text.strip() and params.get("max_tokens") != 0 and "transcript" not in cand:
            why = cand.get("finish_reason")
            raise GenerationFailed("the model returned nothing"
                                   + (f" ({why})" if why else ""))
        cand["text"] = text
        cand.setdefault("model", params.get("model") or "")
        cand.setdefault("tokens", None)
        cand.setdefault("finish_reason", None)
        if sample is not None:
            # THE QUEUE'S NAMES ARE THE QUEUE'S. It cancels and recovers a
            # generation by its handle and numbers its samples, so a backend
            # that sends fields of those names does not rename the generation.
            # The seed stays the backend's: it knows the one it really used.
            cand["sample"] = sample
            cand["handle"] = handle
            cand.setdefault("seed", params.get("seed"))
        return cand

    def _report(self, jid: str, handle: str, fields: dict) -> None:
        """Progress from the backend. ANY change counts as movement: `at` is
        when it last MOVED, never when it was last written."""
        with self._lock:
            try:
                doc = self._load()
            except (Refused, StoreUnreadable):
                return
            row = self._find(doc, jid)
            if row is None or row["job"]["status"] != "running" \
                    or row["job"]["progress"].get("handle") != handle:
                return
            prog = dict(row["job"]["progress"])
            moved = any(prog.get(k) != v for k, v in fields.items())
            if not prog.get("reported"):
                # the first real word from the backend ends the loading guess
                prog.pop("loading", None)
                if prog.get("stage") == "loading the model":
                    prog.pop("stage")
            prog.update(fields)
            prog["reported"] = True
            if moved:
                prog["at"] = self._clock()
            row["job"]["progress"] = prog
            self._save(doc)

    # ---------------------------------------------------- the request record

    def _record_dir(self, corr):
        """A request's folder in the record, or None: nothing is bound (a
        stopped queue writes nothing, as a killed process writes nothing), or
        the id is not a correlation id - so the record is never written
        anywhere but `requests/<correlation id>/`."""
        folder = self.folder if self.store is not None else None
        if folder is None or not ids.matches("correlation", corr):
            return None
        return folder / REQUESTS_DIR / corr

    def _record_write(self, corr, rel: str, data) -> bool:
        """One file of a request's record, written whole and swapped in: bytes
        as they are, a string as text unless the name says JSON, anything
        else as JSON. -> whether it was kept."""
        d = self._record_dir(corr)
        if d is None:
            return False
        path, tmp = d / rel, None
        try:
            if isinstance(data, (bytes, bytearray)):
                body = bytes(data)
            elif isinstance(data, str) and not rel.endswith(".json"):
                body = data.encode("utf-8")
            else:
                body = (json.dumps(data, indent=2, ensure_ascii=False, default=str)
                        + "\n").encode("utf-8")
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
            tmp.write_bytes(body)
            os.replace(tmp, path)
            return True
        except Exception as e:                               # noqa: BLE001
            self._record_fault(corr, rel, e)
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            return False

    def _record_event(self, corr, event: str, *, ts=None, once: bool = False,
                      **fields) -> bool:
        """One line of a request's own story. `once`: not if the story already
        has a line of this kind - an ending is told at least once, and the
        record says it once. -> whether it was written."""
        d = self._record_dir(corr)
        if d is None:
            return False
        try:
            line = json.dumps({"ts": self._clock() if ts is None else ts, "event": event,
                               **fields}, ensure_ascii=False, default=str) + "\n"
            path = d / RECORD_EVENTS
            with self._record_lock:
                if once and any(e.get("event") == event for e in read_jsonl(path)):
                    return False
                d.mkdir(parents=True, exist_ok=True)
                with open(path, "ab") as f:
                    f.write(line.encode("utf-8"))
            return True
        except Exception as e:                               # noqa: BLE001
            self._record_fault(corr, RECORD_EVENTS, e)
            return False

    def _record_fault(self, corr, what: str, e) -> None:
        self._say(f"the request record of {corr} could not keep {what}: "
                  f"{type(e).__name__}: {e}"[:300])

    def _record_submitted(self, doc: dict, row: dict) -> None:
        """A request's first line: the job as it was accepted. A retry names
        the request it runs again, by correlation id too - the old job may be
        forgotten, and its record is then found by nothing else."""
        j = row["job"]
        fields = {"job": j["id"], "record": self.public(row)}
        was = self._find(doc, j["retry_of"]) if j.get("retry_of") else None
        if was is not None:
            fields["retry_of_correlation"] = was["job"].get("correlation_id")
        self._record_event(j.get("correlation_id"), "submitted", **fields)

    def _record_start(self, picked: dict, request, built: bool = True,
                      event: str = "started") -> None:
        """The run has begun generating: what it was built from, and the
        request as built. The application's `manifest` names the inputs; a
        manifest that fails is written down as failed, never fails the job.
        A held request sent (`built` False) was written as built when it was
        held, and its run only says it started; a request being held says
        so (`event`)."""
        view = picked["view"]
        corr = view.get("correlation_id")
        if self._record_dir(corr) is None:
            return
        # S17: what ran beside it as it started, on its `started` line
        beside = {"beside": list(picked.get("beside") or [])} if event == "started" else {}
        if not built:
            self._record_event(corr, event, job=view["id"], **beside)
            return
        manifest = {"job": view, "waited_on": picked.get("waited_on") or [], "inputs": {}}
        hook = getattr(self.hooks, "manifest", None)
        if hook is not None:
            try:
                named = hook(view, picked["after"])
                manifest["inputs"] = {} if named is None else named
            except Exception as e:                           # noqa: BLE001
                manifest["inputs_error"] = f"{type(e).__name__}: {e}"[:300]
                self._say(f"the application's manifest hook failed for {view['id']}: "
                          f"{manifest['inputs_error']}")
        self._record_write(corr, "manifest.json", manifest)
        self._record_write(corr, "request.json", request)
        if event is not None:
            self._record_event(corr, event, job=view["id"], **beside)

    def _record_attempt(self, corr, sample, name, data) -> bool:
        """What a backend keeps about its attempt (`run.record`). Never raises."""
        try:
            if not (isinstance(name, str) and RECORD_NAME.fullmatch(name)
                    and name.lower() not in QUEUE_SAMPLE_FILES):
                self._say(f"the request record of {corr} refused a file named {name!r}: "
                          f"a backend keeps a plain file name, and never one of the "
                          f"queue's own ({', '.join(QUEUE_SAMPLE_FILES)})")
                return False
            if not isinstance(sample, int) or isinstance(sample, bool):
                # never a folder guessed for it: True would be numbered 1
                self._record_fault(corr, str(name), ValueError(
                    f"the attempt is not numbered ({sample!r})"))
                return False
            return self._record_write(corr, f"samples/{sample:02d}/{name}", data)
        except Exception as e:                               # noqa: BLE001
            self._record_fault(corr, str(name), e)
            return False

    # ------------------------------------------------ the queue takes it back

    def _take_back(self, doc: dict, row: dict, error: str, by: str) -> list:
        """End a running job over the worker's head. -> handles to cancel."""
        handles = list(row["run"].get("handles") or [])
        row["run"]["cancel"] = True
        self._end(doc, row, "failed", error, by=by)
        return handles

    def _retire_if_inside(self, jid: str) -> None:
        """A run still inside a job the queue has taken back: nothing can make
        a blocked call return. Since S17 the worker is never inside a job -
        each run has a thread of its own - so the worker goes on starting
        whatever can run, the job's model among them, and nothing is retired:
        the run's thread is left to return, and its late answer is refused by
        `_end` when (if ever) it arrives. Said, so the log shows it."""
        if jid not in self._inside or self._stop.is_set():
            return
        self._beat["retired"] += 1
        self._say(f"the run of {jid} is still inside its call; it is left to return, "
                  "and its late answer will not be written")

    def _clear_stale_head(self, model=None) -> int:
        """A new submission takes the queue from a head that has shown no
        progress for `stale_head_s`: asking again is what a person does when
        the app looks stuck, so it is the gesture that has to work. Since S17
        only a head of the new job's own *model* is taken: a quiet job holds
        back only its own model's jobs, and one of another model is left to
        go on (None: any model's)."""
        if self.store is None:
            return 0
        taken = []
        with self._lock:
            doc = self._load()
            for r in doc["jobs"]:
                if r["job"]["status"] != "running" \
                        or r["run"].get("phase") not in ("building", "generating"):
                    continue                     # a result being landed is not stuck
                if model is not None and r["job"].get("model") != model:
                    continue                     # it holds back no job of this model
                quiet = self._quiet(r)
                if quiet < float(self.cfg["stale_head_s"]) or self._answering_once(r):
                    continue
                hs = self._take_back(doc, r,
                                     f"no progress for {int(quiet)}s, and a new "
                                     "generation was asked for - the queue was given to it",
                                     by="stale_head")
                taken.append((r["job"]["id"], hs))
            if taken:
                self._resolve(doc)
                self._save(doc)
        for jid, hs in taken:
            self._tell_backend(hs[-1] if hs else None)
            self._retire_if_inside(jid)
        if taken:
            self._drain_hooks()
        return len(taken)

    def _quiet(self, row: dict) -> float:
        at = ((row["job"].get("progress") or {}).get("at")
              or row["run"].get("started") or row["job"].get("created") or 0.0)
        try:
            return max(0.0, self._clock() - float(at))
        except (TypeError, ValueError):
            return 0.0

    def _answering_once(self, row: dict) -> bool:
        """Is this generation answering in one piece, before the deadline it
        declared (PLAN-4 D10)? A model that answers once - a transcription -
        reports nothing between its start and its answer, and says as it
        begins when it is due: `due_s`, counted from that report. Before it, it
        is working, never quiet; after it, it is judged as any quiet one is."""
        prog = row["job"].get("progress") or {}
        try:
            return self._clock() < float(prog["at"]) + float(prog["due_s"])
        except (KeyError, TypeError, ValueError):
            return False

    def _reap_given_up(self) -> int:
        taken = []
        with self._lock:
            doc = self._load()
            now = self._clock()
            for r in doc["jobs"]:
                at = r["run"].get("give_up_at")
                if r["job"]["status"] != "running" or not at:
                    continue
                waited = now - float(at)
                if waited < float(self.cfg["give_up_grace_s"]):
                    continue
                hs = self._take_back(doc, r,
                                     f"given up on; the worker did not let go within "
                                     f"{int(waited)}s, so the queue was taken back over "
                                     "its head", by="give_up")
                taken.append((r["job"]["id"], hs))
            if taken:
                self._resolve(doc)
                self._save(doc)
        for jid, hs in taken:
            self._tell_backend(hs[-1] if hs else None)
            self._retire_if_inside(jid)
        if taken:
            self._drain_hooks()
            self._wake.set()
        return len(taken)

    def _backend_loading(self, model=None) -> bool:
        """Is the backend loading a model - since S17, *model*, the one the
        quiet job names, when the backend can say (`loading_for(model)`): an
        answer beside another model's load is not loading. Asked only while
        nothing has been reported. A backend that cannot say is treated as
        loading: a probe that fails while a big model loads is the patient
        case, and a truly dead backend still fails - at the longer deadline."""
        probe = getattr(self.backend, "loading_for", None)
        if probe is not None:
            probe = (lambda ask=probe: ask(model))
        else:
            probe = getattr(self.backend, "loading", None)
        if probe is None:
            return False
        box = []
        t = threading.Thread(target=lambda: box.append(_try_flag(probe)),
                             daemon=True, name="queue-loading-probe")
        t.start()
        t.join(1.0)                              # a probe that hangs is answering "loading"
        return True if not box else box[0]

    def _watch_stalls(self) -> int:
        """A generation that stops moving must FAIL, not hang: a hang is the
        worst failure a queue has, because it looks like patience. A deadline
        on movement, never on total time - a slow model is not a stuck one."""
        rows_quiet = []
        with self._lock:
            doc = self._load()
            for r in doc["jobs"]:
                if r["job"]["status"] == "running" and r["run"].get("phase") == "generating" \
                        and (r["job"]["progress"] or {}).get("handle") \
                        and not r["run"].get("give_up_at"):
                    rows_quiet.append((r["job"]["id"],
                                       bool((r["job"]["progress"] or {}).get("reported")),
                                       r["job"].get("model")))
        loading = {jid: (not reported) and self._backend_loading(model)
                   for jid, reported, model in rows_quiet}
        taken = []
        with self._lock:
            doc = self._load()
            dirty = False
            for jid, _, _ in rows_quiet:
                r = self._find(doc, jid)
                if r is None or r["job"]["status"] != "running":
                    continue
                quiet = self._quiet(r)
                load = loading.get(jid, False)
                deadline = float(self.cfg["load_s"] if load else self.cfg["stall_s"])
                prog = dict(r["job"]["progress"])
                prog["waited"] = int(quiet)
                if load:
                    prog["loading"] = True
                    prog.setdefault("stage", "loading the model")
                r["job"]["progress"] = prog
                dirty = True
                if quiet > deadline and not self._answering_once(r):
                    handle = prog.get("handle")
                    hs = self._take_back(
                        doc, r, f"no progress for {int(quiet)}s "
                                f"({'still loading' if load else 'stalled'}) - giving up "
                                f"on backend job {handle} (cancel sent)", by="stall")
                    taken.append((jid, hs))
            if taken:
                self._resolve(doc)
            if dirty:
                self._save(doc)
        for jid, hs in taken:
            self._tell_backend(hs[-1] if hs else None)
            self._retire_if_inside(jid)
        if taken:
            self._drain_hooks()
            self._wake.set()
        return len(taken)

    # ------------------------------------------------------------ supervision

    def _pause(self, seconds: float) -> None:
        self._stop.wait(seconds)

    def _supervise_loop(self) -> None:
        """The thread that outlives everything it watches, including its own
        pass: every pass is guarded, and a pass that fails is tried again."""
        while not self._stop.is_set():
            self._pause(float(self.cfg["supervise_s"]))
            try:
                self._supervise_pass()
            except BaseException as e:                       # noqa: BLE001
                self._note_restart_fault(type(e).__name__,
                                         f"the supervisor pass itself failed: {e}")

    def _supervise_pass(self) -> None:
        """Reap, watch, and replace the worker if it is gone. A reaper that
        fails must never cost the worker its restart."""
        if self.store is not None:
            for step in (self._reap_given_up, self._watch_stalls):
                try:
                    step()
                except BaseException as e:                   # noqa: BLE001
                    self._say(f"{step.__name__} failed: {type(e).__name__}: {e}")
        t = self._worker
        if self._stop.is_set() or (t is not None and t.is_alive()):
            return
        self._restart_worker()

    def _restart_worker(self) -> None:
        """Counted AFTER the start: a restart that raised is not a restart."""
        self._say("the queue worker died - starting a new one")
        try:
            self._start_worker()
        except BaseException as e:                           # noqa: BLE001
            self._note_restart_fault(type(e).__name__, str(e))
            return
        self._beat["restarts"] += 1

    def _note_restart_fault(self, kind: str, detail: str) -> None:
        f = self._restart_fault
        f.update(kind=kind, detail=str(detail)[:200], at=time.time(),
                 count=f["count"] + 1)
        self._say(f"THE QUEUE WORKER IS DEAD AND COULD NOT BE RESTARTED: {kind}: "
                  f"{str(detail)[:200]} (#{f['count']}) - the supervisor tries again")

    # ---------------------------------------------------------------- health

    @staticmethod
    def _aged(fault: dict, now: float) -> dict:
        out = dict(fault)
        out["age_s"] = round(now - fault["at"], 1) if fault.get("at") else None
        return out

    def health(self) -> dict:
        """Never raises: the one answer that must survive every fault."""
        now = time.time()
        t = self._worker
        worker = {"alive": bool(t is not None and t.is_alive()),
                  "restarts": self._beat["restarts"],
                  "idle_s": round(now - (self._beat["cycle_at"] or now), 1),
                  "uptime_s": round(now - (self._beat["started_at"] or now), 1),
                  "generation": self._beat["gen"],
                  "restart_fault": self._aged(self._restart_fault, now)}
        queue = {"queued": 0, "running": 0, "held": 0, "consecutive_failures": 0, "last": None,
                 "store_fault": None}
        if self.store is not None:
            try:
                with self._lock:
                    rows = self._load()["jobs"]
                queue.update(self._outcomes(rows))
                scheduled = self._scheduled(rows)
                if scheduled is not None:
                    queue["scheduled"] = scheduled
            except Exception:                               # noqa: BLE001
                pass
        queue["store_fault"] = self._aged(self._store_fault, now)
        return {"loaded": self.store is not None,
                "workspace": str(self.folder) if self.folder else None,
                "actor": self.actor,
                "worker": worker, "queue": queue}

    @staticmethod
    def _outcomes(rows: list) -> dict:
        """The honest evidence that generation works: what happened to the
        last job that RAN. Ordered by when it ENDED; a job that never started
        is not evidence, and a stamp that is not a number cannot be placed."""
        jobs = [r["job"] for r in rows if isinstance(r, dict) and isinstance(r.get("job"), dict)]
        ran = []
        for i, r in enumerate(rows):
            j = r["job"]
            f = j.get("finished")
            if j.get("status") in FINISHED and isinstance(f, (int, float)) \
                    and not isinstance(f, bool) and r["run"].get("started") is not None:
                ran.append((float(f), i, j))
        ran.sort(key=lambda x: (x[0], x[1]))
        fails = 0
        for _, _, j in reversed(ran):
            if j["status"] != "failed":
                break
            fails += 1
        last = ran[-1][2] if ran else None
        return {"queued": sum(1 for j in jobs if j.get("status") == "queued"),
                "running": sum(1 for j in jobs if j.get("status") == "running"),
                # held for a person's check (PLAN-4 D16): waiting on a person,
                # never on the card - counted apart, so a page that reads
                # health alone can say a request waits for its check
                "held": sum(1 for j in jobs if j.get("status") == "held"),
                "consecutive_failures": fails,
                "last": None if last is None else {
                    "status": last["status"], "error": last.get("error"),
                    "at": last.get("finished"), "job": last["id"],
                    "correlation_id": last.get("correlation_id")}}


def read_jsonl(path) -> list:
    """Every line of a JSON-lines file that reads as an object, in order; a
    line that does not is skipped, and a file that is not there is empty."""
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return []
    out = []
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            got = json.loads(line)
        except ValueError:
            continue
        if isinstance(got, dict):
            out.append(got)
    return out


def request_record(folder, correlation_id) -> dict:
    """A request's record, read back: {"dir", "manifest", "request", "held",
    "sent", "events", "samples": {NN: {name: content}}} - `held` and `sent`
    for a request that was checked before it was sent (PLAN-4 D16). A file that is not there is
    None; a JSON file is parsed, anything else is text."""
    d = Path(folder) / REQUESTS_DIR / correlation_id

    def load(path):
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".json":
            try:
                return json.loads(text)
            except ValueError:
                return text
        return text

    samples = {}
    if (d / "samples").is_dir():
        for s in sorted(x for x in (d / "samples").iterdir() if x.is_dir()):
            samples[s.name] = {f.name: load(f) for f in sorted(s.iterdir())
                               if f.is_file() and not f.name.startswith(".")}
    return {"dir": d, "manifest": load(d / "manifest.json"),
            "request": load(d / "request.json"),
            "held": load(d / "held.json"), "sent": load(d / "sent.json"),
            "events": read_jsonl(d / RECORD_EVENTS), "samples": samples}


def _try(fn, *a):
    try:
        return fn(*a)
    except Exception:                                       # noqa: BLE001
        return None


def _try_flag(fn) -> bool:
    try:
        return bool(fn())
    except Exception:                                       # noqa: BLE001
        return True
