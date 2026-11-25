"""The machine's card lock: who holds the card at the top (PLAN-4 D5).

A REAL card is taken through a lock the OPERATING SYSTEM holds: a file lock on
`llm-bootstrap-card.lock` in the temporary folder - the contract's
`machine/card-lock.json` names it, and the note its holder writes beside it.
A server looks at it EACH TIME IT OPENS A SESSION (a pipeline), and nothing
needs configuring:

  * FREE: no coordinator runs, and the server takes the lock itself - held
    from before its first load's memory check until its session closes. Two
    servers run on their own take turns through it.
  * HELD, its note naming a COORDINATOR: the coordinator took it as it
    started, and holds it while it runs. The server goes through that
    coordinator, at the address the note gives (`through`): a rental, had by
    ticket - the coordinator is the only way onto the card while it runs.
  * HELD otherwise: an application run on its own has the card. The server
    waits, its request's patience applying - 0 is `card_busy` at once, and a
    patience that runs out `card_patience` - saying only that it waits, and
    since when, never who has the card (D4).
  * HELD WITH NO NOTE YET: a holder writes its note just after it takes the
    lock. A request that would not wait gives it a moment (`NOTE_WAIT_S`) and
    looks again, once - so a coordinator that has only just started is gone
    through, never refused as an application's card.
  * A NOTE IS ITS HOLDER'S ONLY WHILE ITS WRITER LIVES. A holder that dies
    lets go of the lock and leaves its note; the next holder writes its own a
    moment after it takes the lock, or - a program that says nothing - never.
    So a note is trusted only while the process it names (`pid`) is alive and
    started before the note was written (`since`): one whose writer has gone
    is STALE (`stale_note`) - never a coordinator's address to go through, it
    is read as no note at all, and a refusal says the note was stale. A pid
    that cannot be asked about is trusted: stale on proof, never on a guess.

  * A holder that dies lets go: the kernel releases a dead process's locks,
    however it died. Nothing has to run in the dying process.
  * What the lock does NOT do, and the coordinator does: take the card back
    from a holder that hangs, and order the applications waiting.

RUN ON ITS OWN, A SERVER KEEPS THE SAME MEMORY RULE (D2, D5): before each
load it reads the card itself and judges the model's need by the contract's
one memory check (`fit`), keeping its own margin free - what a coordinator
does before each model a rental adds.

AND IT WATCHES THE CARD AS A COORDINATOR DOES (S5b; the owner, 2026-09-27:
"same behaviour as coordinator"): while this source holds the lock, and the
runner has handed it a way to read the card (`read`), the card is read every
`watch_s` - every `recover_watch_s` while DEGRADED; free memory under its
floor, or a memory error its models' logs said (the runner's `work`), marks
the session degraded, told to the runner (`on_degraded`) in a coordinator's
words and numbers; nothing new goes on the card meanwhile (`fit`'s
`degraded`); and the first read with room for what the session needs - its
models' need, less what its processes hold, plus the floor - clears it. The
state is the session's: it ends when the lock is given back.

The temporary folder is the user's, so the lock is per user on a machine
several people use. Standard library only: `msvcrt.locking` on Windows,
`fcntl.flock` elsewhere.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_CONTRACT = Path(__file__).resolve().parents[2] / "llm-bootstrap-contract"
if str(_CONTRACT) not in sys.path:
    sys.path.insert(0, str(_CONTRACT))
import memory as MEM                                            # noqa: E402  (the one memory check)
import validate as _V                                           # noqa: E402

#: the machine's card lock, as the contract names it
_LOCK = json.loads((_CONTRACT / "machine" / "card-lock.json").read_text(encoding="utf-8"))
_SHAPES = _V.load(_CONTRACT)[0]
LOCK_NAME = _LOCK["file"]
#: who holds it, written by the holder once it has the lock; read by whoever
#: waits, to learn whether a coordinator holds it - and where. Never trusted
#: for anything else, and never repeated to an application
NOTE_SUFFIX = _LOCK["note_file"][len(LOCK_NAME):]
HOLDER_SUFFIX = NOTE_SUFFIX
#: how long a request that would not wait gives a lock with no note beside it,
#: for its holder to say who it is: it writes the note just after it takes
#: the lock, and a coordinator that has only just started is gone through,
#: never taken for an application
NOTE_WAIT_S = 0.2
NOTE_STEP_S = 0.01
#: how much later than its note's `since` the process a note names may have
#: started and still be the one that wrote it: both are this machine's clock,
#: read a moment apart - a step of the clock between them is all this allows for
NOTE_CLOCK_SLACK_S = 2.0
#: how long a session's end waits for its watch to stop: a look under way is
#: a read of the card - nvidia-smi answers within its own 10 s bound - and the
#: watch is a daemon thread, which never outlives what it looked for
WATCH_JOIN_S = 12.0
#: what a refusal says when the note beside the lock was stale - never whose
#: it was: an application is told nothing of another (D4)
STALE_WORDS = ("the note beside its lock is stale - whoever wrote it has gone - and whoever "
               "holds it now has not said who it is")


def process_started(pid):
    """When process *pid* started, in seconds since the epoch - or "gone": no
    process has that pid, or the one that had it has ended - or None: it is
    alive, and when it started cannot be told, or it cannot be asked about.
    The server's own (the parts share no code): a handle on Windows, /proc on
    Linux, and elsewhere whether it lives."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if not 1 <= pid <= 0xFFFFFFFF:
        return "gone"                                       # no process has such a pid
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class FileTime(ctypes.Structure):
            _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(FileTime)] * 4
        k.GetProcessTimes.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
        handle = k.OpenProcess(0x00100000 | 0x1000, False, pid)
        if not handle:
            return "gone" if ctypes.get_last_error() == 87 else None   # no such process
        try:
            if k.WaitForSingleObject(handle, 0) == 0:
                return "gone"                               # ended; only a handle keeps it
            t = [FileTime() for _ in range(4)]
            if not k.GetProcessTimes(handle, *(ctypes.byref(x) for x in t)):
                return None
            return ((t[0].high << 32) | t[0].low) / 1e7 - 11644473600   # 1601 -> the epoch
        finally:
            k.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "gone"
    except OSError:
        return None
    try:
        after = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if after[0] == "Z":
            return "gone"                                   # a zombie holds nothing
        boot = next(float(x.split()[1]) for x in Path("/proc/stat").read_text().splitlines()
                    if x.startswith("btime "))
        return boot + int(after[19]) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def stale_note(note) -> bool:
    """Is *note* - read beside a lock found held - not its holder's? The
    process it names has gone, or its pid was handed on to a process started
    after the note was written. A process that lives and started before it,
    or one that cannot be asked about, is its writer: stale on proof, never
    on a guess."""
    if not isinstance(note, dict):
        return False
    started = process_started(note.get("pid"))
    if started == "gone":
        return True
    since = note.get("since")
    return isinstance(started, float) and isinstance(since, (int, float)) \
        and not isinstance(since, bool) and started > since + NOTE_CLOCK_SLACK_S


def machine_lock() -> Path:
    """Where every server and every coordinator on this machine, for this
    user, meet."""
    return Path(tempfile.gettempdir()) / LOCK_NAME


class CardWaitRefused(Exception):
    """The card was not taken: the patience ran out (`card_patience`), the
    request would not wait (`card_busy`), the wait was cancelled, or the
    server is stopping. `reason` is the request's own, when it has one."""

    def __init__(self, message: str, reason=None):
        super().__init__(message)
        self.reason = reason


def _try_lock(path: Path):
    """The open lock file, locked by this handle - or None when somebody holds
    it. Windows locks per handle, so a second handle in this same process is
    refused like any other process: one card, one holder."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")                                  # never truncated, never read
    try:
        if sys.platform == "win32":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


def _unlock(fh) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass                                                # closing lets it go anyway
    finally:
        fh.close()


class MachineCard:
    """The runner's card source for a real card with no coordinator named:
    the machine's lock, and through it the coordinator that holds it, if one
    does. The same calls as any card source (see `LocalCard`).

    `through(url, holder)` makes the card source of a coordinator found
    through the lock - its rental, had by ticket, for this server's actor;
    the server hands it in (`server.build_backend`). Without it, a lock a
    coordinator holds is a card in use. One made is used for its session,
    and let go with it: the next session looks at the lock again. Two takes
    at once - a pipeline's thread and a load - go through ONE card source,
    so one ticket: the second waits for the first's answer, by that card
    source's own rule, and a card source with a take under way is never let
    go under it (found by building: it was, and the second asked a ticket of
    its own)."""

    #: an operating system's lock is never taken back; a coordinator's rental
    #: can be, and says so on its own card source (`lost`) - and a
    #: coordinator watches its rental (D3): the work it says, and the card
    #: degraded, pass to the card source of the coordinator gone through
    on_lost = None
    on_wanted = None
    work = None
    on_degraded = None
    #: S5b: how this source reads the card for its own watch - the runner's
    #: own way, {"free", "total"} in MiB, the free MiB alone, or None - handed
    #: in by the runner when it reads the card; None: no watch, and a full
    #: card is not recovered from here
    read = None
    #: S18: what a model needs on the card, in MiB - the runner's estimate,
    #: handed in by the runner - and the margin its settings keep free: how a
    #: scheduled pipeline that waits for room is judged BEFORE the lock is
    #: taken (`take`), so it holds back nobody while it waits
    needs = None
    margin_mib = MEM.MARGIN_MIB

    def __init__(self, path=None, *, patience_s: float = 600.0, retry_s: float = 0.25,
                 clock=time.monotonic, through=None, watch_s: float = 30.0,
                 recover_watch_s: float = 2.0, floor_mib: int = MEM.MARGIN_MIB, read=None):
        self.path = Path(path) if path else machine_lock()
        self.patience_s, self.retry_s = float(patience_s), float(retry_s)
        self._clock = clock
        self._through = through
        self._fh = None
        #: THE LOCK'S OWN WATCH (S5b; the owner, 2026-09-27: a server run on its
        #: own degrades "same behaviour as coordinator"): the card read every
        #: `watch_s` while this source holds the lock, every `recover_watch_s`
        #: while it is DEGRADED; free memory under `floor_mib`, or a memory
        #: error its models' logs said, marks it degraded; the first read with
        #: room for what its session needs clears it
        for name, value in (("watch_s", watch_s), ("recover_watch_s", recover_watch_s)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be a positive number of seconds, not {value!r}")
        self.watch_s, self.recover_watch_s = float(watch_s), float(recover_watch_s)
        self.floor_mib = int(floor_mib)
        if read is not None:
            self.read = read
        self._sessions = 0
        #: this session's: the need each model was judged by, what it runs,
        #: the degraded state and its marks, the memory errors counted so far
        self._needs: dict = {}
        self._running: dict = {}
        self.degraded = None
        self._marks = 0
        self._errors = None
        self._read = (None, None)
        self._watch_stop = None
        self._watch_wake = threading.Event()
        self._watcher = None
        #: the card source of the coordinator this session goes through, or None
        self._via = None
        #: the takes under way through it - never let go under one
        self._taking = 0
        self._wanted = False
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # ---------------------------------------------- what the runner reads

    @property
    def kind(self) -> str:
        """What the record calls this source now: the lock, or the rental of
        the coordinator it goes through."""
        via = self._via
        return via.kind if via is not None else "machine lock"

    @property
    def turn(self):
        """The rental held through a coordinator, or None: the lock has none."""
        via = self._via
        return via.turn if via is not None else None

    @property
    def lost(self):
        via = self._via
        return via.lost if via is not None else None

    @property
    def claim_s(self):
        """How long the last take's claim took, through a coordinator (D12);
        None on the lock, whose take is all wait."""
        via = self._via
        return getattr(via, "claim_s", None) if via is not None else None

    @property
    def wanted(self) -> bool:
        """Is the card wanted - through a coordinator, as its last renewal
        said; on the lock, as whoever set it said (nobody on this machine can
        ask a lock's holder for it)."""
        via = self._via
        return bool(getattr(via, "wanted", False)) if via is not None else self._wanted

    @wanted.setter
    def wanted(self, value) -> None:
        self._wanted = bool(value)

    @property
    def session(self):
        """What the lock's session is called while this source holds the lock
        itself - a degraded state is said of it, and ends with it (S5b) - or
        None."""
        return f"lock-{self._sessions}" if self._fh is not None else None

    @property
    def recovers(self) -> bool:
        """Does this source say when a full card has room again (S5b)? Through
        a coordinator, as it does; on the lock, when it can read the card."""
        via = self._via
        if via is not None:
            return bool(getattr(via, "recovers", False))
        return self.read is not None

    @property
    def holder_path(self) -> Path:
        return self.path.with_name(self.path.name + NOTE_SUFFIX)

    @property
    def held(self) -> bool:
        """Does this hold the card now: the lock, or a coordinator's rental?"""
        via = self._via
        return self._fh is not None or (via is not None and via.turn is not None
                                        and not via.lost)

    # ------------------------------------------------------------ the take

    def take(self, holder, model_key, for_=None, cancel=None, on_wait=None, terms=None):
        """Hold the card for a session: -> the rental a coordinator granted,
        or None for the lock. The lock is looked at afresh (the module's
        docstring): free - taken; a coordinator's - gone through; anybody
        else's - waited for, the request's patience applying (`terms`; None:
        the server's own), 0 now or not at all."""
        asked = getattr(terms, "patience_s", None)
        patience = self.patience_s if asked is None else max(0.0, float(asked))
        started = self._clock()
        since = time.time()
        looked_again = False
        # S18: a scheduled pipeline that waits for room is judged before the
        # lock is taken - as a coordinator judges a ticket at its claim, and
        # passes it over - so while it waits for room it holds back nobody
        judge = self._judges_room(terms, model_key)
        while True:
            if cancel is not None and cancel.is_set():
                raise CardWaitRefused("cancelled while waiting for the card")
            if self._stop.is_set():
                raise CardWaitRefused("the server is stopping: the card is not taken")
            if judge and self._fh is None and self._via is None and self._coordinator() is None:
                room = self._no_room_for(model_key)
                if room is not None:
                    self._waits_for_room(model_key, room, terms, on_wait, started, patience,
                                         cancel)
                    continue
            with self._lock:
                if self._fh is not None:
                    return None                             # this source holds it already
                via = self._via
                if via is not None and via.turn is not None and not via.lost:
                    return dict(via.turn)                   # ...through its coordinator
                # a take under way through a coordinator: this one goes through
                # it too - one card source, one ticket, one place in line; and
                # so does one while it keeps a place in line for waiting work
                # (S18): the take that work makes claims it
                share = via is not None and not via.lost and (
                    self._taking > 0 or getattr(via, "parked", None) is not None)
                if share:
                    self._taking += 1
                else:
                    self._let_go_of(via)
                    fh = _try_lock(self.path)
                    if fh is not None:
                        self._fh = fh
                        self._say_who(holder, model_key, for_)
                        self._watch_begins()
                        return None
            if share:
                return self._take_through(via, holder, model_key, for_, cancel, on_wait, terms)
            url = self._coordinator()
            if url is not None and self._through is not None:
                return self._go_through(url, holder, model_key, for_, cancel, on_wait, terms)
            if patience <= 0:
                if not looked_again:
                    # A HOLDER WRITES ITS NOTE JUST AFTER IT TAKES THE LOCK: held,
                    # with no live note yet - none, or one a dead holder left -
                    # its holder may be a coordinator that has only just taken
                    # it. Given a moment to say who it is, then looked at again
                    # - once (S4 R5; H5's register 6)
                    looked_again = True
                    self._await_note(cancel)
                    continue
                raise CardWaitRefused(f"the card is busy{self._stale_words()}, and this request "
                                      f"would not wait for it (card_busy)", "card_busy")
            words = self.waiting_for(since)
            if on_wait is not None:
                try:
                    on_wait(words)
                except Exception:                           # noqa: BLE001 - a report never fails a wait
                    pass
            if self._clock() - started >= patience:
                raise CardWaitRefused(f"no card within its patience of {patience:g}s "
                                      f"(card_patience): {words}{self._stale_words()}",
                                      "card_patience")
            if cancel is not None:
                cancel.wait(self.retry_s)
            else:
                self._stop.wait(self.retry_s)

    # --------------------------------------------- scheduled work (S18)

    def _judges_room(self, terms, model_key) -> bool:
        """Is this take one whose model is judged before the lock is taken?
        Scheduled work that waits for room in place - a pipeline's, its terms
        saying `wait` and not waiting in a queue - with a model named, and a
        way to read the card and to know the model's need."""
        return (getattr(terms, "klass", None) == "scheduled"
                and getattr(terms, "if_no_room", None) == "wait"
                and not getattr(terms, "parks", False) and model_key is not None
                and self.read is not None and self.needs is not None)

    def _no_room_for(self, model_key):
        """The card read now, without the lock, and *model_key*'s need judged by
        the contract's one memory check: -> {"verdict", "outside"} when it
        does not fit, else None - it fits, or nothing could be judged. No
        session of this source holds the card, so what it holds is all
        outside the bootstrap."""
        try:
            need = self.needs(model_key)
            got = self.read()
        except Exception:                                   # noqa: BLE001 - a look decides nothing
            return None
        if need is None:
            return None
        free, total = (got.get("free"), got.get("total")) if isinstance(got, dict) else (got, None)
        verdict = MEM.fit(need, free, total, self.margin_mib)
        if verdict["verdict"] != "no_room":
            return None
        outside = None if free is None or total is None else max(0, int(total) - int(free))
        return {"verdict": verdict, "outside": outside}

    def _waits_for_room(self, model_key, room, terms, on_wait, started, patience, cancel):
        """One look at a card with no room for a scheduled pipeline's model: the
        room it waits on said (`terms.on_room`, and to `on_wait`), and a wait
        of `recover_watch_s` - the lock never taken meanwhile - or, its
        patience run out, the take refused `no_room` in the loud words, saying
        how long it waited (the owner's second ruling of 2026-09-28)."""
        verdict, outside = room["verdict"], room["outside"]
        waited = self._clock() - started
        if waited >= patience:
            raise CardWaitRefused(
                f"{model_key} did not fit on the card within its patience of "
                f"{MEM.span(patience)}: {MEM.no_room_words(verdict, outside, waited_s=waited)}",
                "no_room")
        said = {"memory": MEM.numbers(verdict), "outside_mib": outside,
                "words": MEM.room_words(verdict, outside)}
        for hear in (getattr(terms, "on_room", None), on_wait):
            if hear is None:
                continue
            try:
                hear(said if hear is not on_wait else f"{model_key}: {said['words']}")
            except Exception:                               # noqa: BLE001 - a report never fails
                pass
        pause = min(self.recover_watch_s, max(0.0, patience - waited))
        if cancel is not None:
            cancel.wait(pause)
        else:
            self._stop.wait(pause)

    def look(self, want, holder=None):
        """THE QUEUE'S LOOK (S18) where a coordinator holds the machine's lock:
        through that coordinator - a card source of its own, which keeps the
        waiting work's place in line, and which the work's take goes through
        (`take`). -> its answer; None where no coordinator is gone through:
        the runner looks for itself."""
        via = self._via
        if want is not None and (via is None or via.lost) and self._fh is None \
                and self._through is not None:
            url = self._coordinator()
            if url is not None:
                with self._lock:
                    via = self._via
                    if via is None or via.lost:
                        via = self._via_to(url, holder)
        ask = getattr(via, "look", None)
        return None if ask is None else ask(want, holder)

    def _via_to(self, url, holder):
        """The card source of the coordinator at *url*, made for this server's
        actor, and hearing what the runner hears. Under the lock."""
        self._let_go_of(self._via)
        via = self._through(url, holder)
        via.on_lost, via.on_wanted = self.on_lost, self.on_wanted
        via.work, via.on_degraded = self.work, self.on_degraded
        self._via = via
        return via

    def _note(self):
        """The note beside the lock, read once the lock was found held: ->
        (note, stale). The note in the contract's shape while the process
        that wrote it lives - (note, False); one whose writer has gone names
        nobody - (None, True); none, or none that can be read - (None, False)."""
        try:
            note = json.loads(self.holder_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, False
        if not isinstance(note, dict) or _V.check(note, _LOCK["note"], _SHAPES):
            return None, False
        if stale_note(note):
            return None, True
        return note, False

    def _coordinator(self):
        """The address of the coordinator holding the lock, as its live note
        gives it - or None: the note names none, cannot be read, or is stale -
        a dead coordinator's address is never gone through."""
        note, _ = self._note()
        return note["coordinator"] if note is not None and "coordinator" in note else None

    def _stale_words(self) -> str:
        """What a refusal adds when the note beside the lock is stale - never
        whose it was (D4)."""
        return f" - {STALE_WORDS}" if self._note()[1] else ""

    def _await_note(self, cancel) -> None:
        """Before the lock is looked at again: a lock held with no live note
        beside it - none, or a stale one - has its holder's note waited for, a
        moment: NOTE_WAIT_S on the real clock, a file write's time many times
        over, and never longer. One with a live note is looked at again at
        once. A cancel or a stop ends the wait; the look after it says why."""
        until = time.monotonic() + NOTE_WAIT_S
        while self._note()[0] is None and time.monotonic() < until:
            if (cancel is not None and cancel.is_set()) or self._stop.is_set():
                return
            time.sleep(NOTE_STEP_S)

    def _go_through(self, url, holder, model_key, for_, cancel, on_wait, terms):
        """This session goes through the coordinator at *url*: a card source
        of its own, which hears what the runner hears, takes by the request's
        terms, and is let go with the session - made once: a take that found
        the coordinator in the same moment goes through the one made."""
        with self._lock:
            via = self._via
            if via is None or via.lost:
                via = self._via_to(url, holder)
            self._taking += 1
        return self._take_through(via, holder, model_key, for_, cancel, on_wait, terms)

    def _take_through(self, via, holder, model_key, for_, cancel, on_wait, terms):
        """A take through *via*, counted while it is under way: a card source
        with a take under way is never let go under it. Its own rule keeps its
        one place in line - a second take waits for the first's answer."""
        try:
            return via.take(holder, model_key, for_=for_, cancel=cancel, on_wait=on_wait,
                            **({"terms": terms} if terms is not None else {}))
        finally:
            with self._lock:
                self._taking -= 1

    def _let_go_of(self, via) -> None:
        """A coordinator's card source whose session is over - given back,
        taken back, or refused with no take under way - is closed, and the
        lock is looked at afresh. Under the lock."""
        if via is None:
            return
        self._via = None
        close = getattr(via, "close", None)
        if close is not None:
            try:
                close()
            except Exception:                               # noqa: BLE001
                pass

    def give_back(self) -> None:
        """The session is over: the lock let go, its note first - or the
        coordinator's rental released, and its card source closed."""
        with self._lock:
            fh, self._fh = self._fh, None
            via = self._via
            watcher = self._watch_ends()
        if watcher is not None:
            watcher.join(WATCH_JOIN_S)
        if via is not None:
            try:
                via.give_back()
            finally:
                with self._lock:
                    # S18: one that keeps a place in line for waiting work is
                    # kept - the work's take claims it
                    if self._via is via and getattr(via, "parked", None) is None:
                        self._let_go_of(via)
        if fh is None:
            return
        try:
            self.holder_path.unlink()
        except OSError:
            pass
        _unlock(fh)

    def stop_asking(self) -> None:
        """The server is stopping: a wait under way ends at its next look -
        through a coordinator too - and nothing more is waited for. What is
        held stays held until given back."""
        self._stop.set()
        via = self._via
        client = getattr(via, "client", None)
        if client is not None and hasattr(client, "stop_asking"):
            client.stop_asking()

    def status(self):
        """Health's `card`: a coordinator's, when this session goes through
        one; none for the lock."""
        via = self._via
        return via.status() if via is not None else None

    # --------------------------------------- the lock's own watch (S5b)

    def _watch_begins(self) -> None:
        """The lock was taken: a session begins, not degraded, its memory
        errors counted from now, and - when this source can read the card -
        its watch starts. Under the lock."""
        self._sessions += 1
        self._needs, self._running, self.degraded, self._marks = {}, {}, None, 0
        self._read = (None, None)
        self._errors = self._work_errors()[0]
        if self.read is None:
            return
        stop = self._watch_stop = threading.Event()
        self._watch_wake.clear()
        self._watcher = threading.Thread(target=self._watch_loop, args=(self.session, stop),
                                         daemon=True, name="card-lock-watch")
        self._watcher.start()

    def _watch_ends(self):
        """The session is over: its watch stops, and its degraded state ends
        with it. Under the lock. -> the watch's thread, to be joined outside
        it, or None."""
        if self._watch_stop is not None:
            self._watch_stop.set()
        self._watch_wake.set()
        watcher, self._watcher, self._watch_stop = self._watcher, None, None
        self.degraded = None
        return watcher

    def _work_errors(self) -> tuple:
        """What the runner's work says of its models' memory errors: (the
        count, the newest line) - (None, None) when it says nothing."""
        ask = self.work
        if ask is None:
            return None, None
        try:
            said = ask() or {}
        except Exception:                                   # noqa: BLE001 - a look never fails
            return None, None
        return said.get("memory_errors"), said.get("memory_error")

    def _watch_loop(self, session, stop) -> None:
        """Every `watch_s` - every `recover_watch_s` while degraded - and at
        once when the runner says a memory error (`beat_now`): a look."""
        while not stop.is_set():
            self._watch_wake.wait(self.recover_watch_s if self.degraded else self.watch_s)
            self._watch_wake.clear()
            if stop.is_set():
                return
            try:
                self._look(session)
            except Exception:                               # noqa: BLE001 - the watch must live
                pass

    def _look(self, session) -> None:
        """One look, as a coordinator's watch looks: a memory-error count that
        rose marks the session degraded, with the newest line; then the card
        is read - OUTSIDE the lock - and free memory under the floor marks
        it; room for what the session needs, on a read begun after the last
        mark, clears it, the numbers said; still full, the numbers are the
        newest read's. A card that cannot be read decides nothing."""
        count, line = self._work_errors()
        tell = []
        with self._lock:
            if self.session != session:
                return
            if count is not None and self._errors is not None and count > self._errors:
                tell.append(self._mark("memory_error", time.time(), str(line or
                            "a model's log said the card's memory ran out")[:500]))
            if count is not None:
                self._errors = count
            marks = self._marks
        self._tell(tell)
        got = self.read() if self.read is not None else None
        free, total = (got.get("free"), got.get("total")) if isinstance(got, dict) else (got, None)
        if not isinstance(free, (int, float)) or isinstance(free, bool):
            return
        free = int(free)
        total = int(total) if isinstance(total, (int, float)) and not isinstance(total, bool) \
            else None
        tell = []
        with self._lock:
            if self.session != session:
                return
            self._read = (free, total)
            if free < self.floor_mib and self.degraded is None:
                tell.append(self._mark("under_floor", time.time()))
            elif self.degraded is not None:
                need, outside = self._room(free, total)
                if free >= need and self._marks == marks:
                    self.degraded = None
                    tell.append(None)
                else:
                    was = self.degraded
                    self.degraded = dict(was, free_mib=free, total_mib=total,
                                         need_mib=need, outside_mib=outside)
                    if self.degraded != was:
                        # told when the numbers moved, as a coordinator's
                        # rental is heard only when it changes
                        tell.append(dict(self.degraded))
        self._tell(tell)

    def _mark(self, reason: str, now: float, error=None) -> dict:
        """The session is DEGRADED, in a coordinator's words and numbers - the
        card as last read. Under the lock. -> what the runner is told."""
        free, total = self._read
        whole = f" of {total}" if total is not None else ""
        if reason == "under_floor":
            why = (f"the card has {free} MiB free{whole}, under the floor of {self.floor_mib} "
                   f"MiB this server keeps free: nothing new goes on it until it has room again")
        else:
            why = (f"a model's log said the card's memory ran out ({error}): nothing new goes on "
                   f"it until it has room again")
        need, outside = self._room(free, total)
        self.degraded = {"reason": reason, "why": why, "since": now, "free_mib": free,
                         "floor_mib": self.floor_mib, "total_mib": total, "error": error,
                         "need_mib": need, "outside_mib": outside}
        self._marks += 1
        return dict(self.degraded)

    def _room(self, free, total) -> tuple:
        """What this session needs free before it is not degraded - the need
        its models were judged by, less what its processes hold on the card
        now, and the floor - and how much of the card programs outside the
        bootstrap use: -> (need, outside), in MiB. Under the lock."""
        held = sum(int(mib if mib is not None else self._needs.get(model) or 0)
                   for model, mib in self._running.values())
        need = self.floor_mib + max(0, sum(self._needs.values()) - held)
        outside = None if free is None or total is None else max(0, total - free - held)
        return need, outside

    def _tell(self, said: list) -> None:
        """The runner hears each change - the degraded state, or None: room
        again - outside the lock."""
        hear = self.on_degraded
        for one in said:
            if hear is not None:
                try:
                    hear(one)
                except Exception:                           # noqa: BLE001 - a word never fails
                    pass

    # ------------------------------------------- what the rental names

    def running(self, pid, proof, model, engine=None, port=None, mib=None) -> None:
        """A process the session put on the card: named to the coordinator it
        goes through. The lock names nothing - the kernel's kill-on-close is
        its whole answer to a holder that dies - and keeps it for its own
        watch: what its session holds on the card (S5b)."""
        via = self._via
        tell = getattr(via, "running", None)
        if tell is not None:
            tell(pid, proof, model, engine, port, mib)
        elif via is None:
            with self._lock:
                self._running[proof] = (model, mib)

    def stopped(self, proof) -> None:
        via = self._via
        tell = getattr(via, "stopped", None)
        if tell is not None:
            tell(proof)
        elif via is None:
            with self._lock:
                self._running.pop(proof, None)

    def named(self) -> list:
        via = self._via
        ask = getattr(via, "named", None)
        return ask() if ask is not None else []

    def beat_now(self) -> None:
        """Through a coordinator, its next heartbeat goes at once; on the
        lock, its own watch looks at once - a memory error a model's log said
        is heard now, not a watch from now (S5b)."""
        via = self._via
        beat = getattr(via, "beat_now", None)
        if beat is not None:
            beat()
        elif via is None:
            self._watch_wake.set()

    # ------------------------------------------------------- the memory

    def fit(self, model, engine, need_mib, *, read=None, margin_mib=MEM.MARGIN_MIB, how="",
            hint=""):
        """May *model* go on the card? Through a coordinator, it decides (an
        amend). On the lock, the card is read HERE - `read`, the runner's own
        way: {"free", "total"} in MiB, the free MiB alone, or None - and
        judged by the contract's one memory check, keeping *margin_mib* free.
        -> the verdict, its numbers, its words; `unknown` decides nothing."""
        via = self._via
        if via is not None:
            decide = getattr(via, "fit", None)
            return None if decide is None else decide(model, engine, need_mib, read=read,
                                                      margin_mib=margin_mib, how=how, hint=hint)
        if need_mib is None:
            # nothing to judge - the stand-in, or a model with no estimate -
            # so the card is not read: a look at it for nothing is still a look
            # (nvidia-smi), and a suite never asks the real card
            return None
        with self._lock:
            # what the session needs, for its own watch (S5b)
            self._needs[model] = int(need_mib)
            said = self.degraded
        if said is not None and self._fh is not None:
            # degraded (S5b): nothing new goes on the card, as a coordinator's
            # amend is refused - without reading the card for it
            return {"verdict": "degraded", "need_mib": int(need_mib),
                    "free_mib": said.get("free_mib"), "margin_mib": margin_mib,
                    "total_mib": said.get("total_mib"),
                    "words": f"{model} may not go on the card now: it is degraded - "
                             f"{said['why']}"}
        got = read() if read is not None else None
        free, total = (got.get("free"), got.get("total")) if isinstance(got, dict) else (got, None)
        verdict = MEM.fit(need_mib, free, total, margin_mib)
        said = MEM.words(verdict) + (f" ({how})" if how else "")
        if verdict["verdict"] == "never_fits":
            said = f"{model} cannot fit on this card at all: {said}." + (f" {hint}" if hint else "")
        elif verdict["verdict"] == "no_room":
            # FAIL LOUDLY (H9; the owner, 2026-09-28): what programs outside
            # the bootstrap hold - the card's use less this session's own -
            # and what to do, in the contract's one sentence
            with self._lock:
                outside = self._room(free, total)[1]
            said = f"not enough free VRAM for {model}" + (f" ({how})" if how else "") + \
                f": {MEM.no_room_words(verdict, outside)}"
            return {**verdict, "outside_mib": outside, "words": said}
        return {**verdict, "words": said}

    # --------------------------------------------------------- the words

    def waiting_for(self, since=None) -> str:
        """What a waiting request says: that it waits, and since when - never
        who has the card, nor for what (D4), as a coordinator's rental says."""
        at = time.localtime(since or time.time())
        return f"waiting for the card, since {time.strftime('%H:%M', at)}"

    def _say_who(self, holder, model_key, for_) -> None:
        """The note, in the contract's shape: best effort - a holder that
        cannot say who it is still holds."""
        who = {"pid": os.getpid(), "since": time.time(), "holder": holder, "model": model_key,
               "for": for_}
        tmp = self.holder_path.with_name(f"{self.holder_path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_bytes(json.dumps(who).encode("utf-8"))
            os.replace(tmp, self.holder_path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
