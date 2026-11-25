"""What the coordinator asks of the machine it runs on - the card's memory,
whether the processes a rental named are still alive, and ending them, on
proof.

    hardware = ThisMachine()        # what `coordinator.py` uses when started
    hardware.probe()                # {"known": True, "used_mib": 2251, "total_mib": 24576}
    hardware.probe(fresh=True)      # the same, asked now rather than kept
    hardware.alive(processes)       # [True, False, ...]: which are still alive
    hardware.end(processes)         # [{"outcome", "detail"}, ...]: one per process

THE CARD'S TRUTH IS THE CARD. `probe` asks nvidia-smi how much of the card is
in use, and says so - on the dashboard, at start beside what the record says,
and when a rental ends: its release, or its reclaim, is done once the card's
memory is back. A card that cannot be asked is `known: False`, never an empty
card. The answer is kept for a couple of seconds, since the dashboard asks
every second - unless a fresh one is asked for.

ENDING IS ON PROOF, never by port and never by time. A rental names every
process it put on the card - each one's pid, and a PROOF: the id of the load
that started it, which its command line carries (llama-server as `--alias`,
whisper-server in `--request-path`). When a rental lapses - or a release
finds one of them still there after its settle time - they are ended ALL AT
ONCE:
  * every one is held open first - its process handle on Windows, a pidfd on
    Linux - so no pid can change hands between the reading and the ending;
  * their command lines are read in ONE query, not one each (on Windows one
    query takes about a second) - and what it could not read is asked again,
    a few times (QUERY_ROUNDS), before it is left alone;
  * each that carries its proof is ended on a thread of its own, and waited
    for with its own bound (END_WAIT_S), since a killed process can hold the
    card a moment longer - so the whole takes about the slowest one, not the
    sum of them.
Each outcome is said, for the record:
  ended       it carried its proof, was ended, and has gone;
  gone        it was already gone;
  left_alone  it is alive without its proof - its pid belongs to something
              else now - or its command line could not be read, or it could
              not be opened: never ended on a guess;
  not_ended   it could not be ended, or had not gone within its bound.

This is what the kernel's kill-on-close cannot cover: a holder that HANGS,
alive and silent, with its models still on the card - and, on POSIX, where no
such job exists, a holder that dies.

THE MACHINE'S CARD LOCK says who holds the card at the top (PLAN-4 D5; the
contract's `machine/card-lock.json`). A coordinator started from its command
line takes it as it starts and holds it while it runs (`MachineLock`), its
note giving its address - so a server finds it there, and a machine never
runs two coordinators. The lock is the operating system's, on the first byte
of a file in the user's temporary folder: the kernel lets it go when its
holder dies, however it dies.

A NOTE IS ITS HOLDER'S ONLY WHILE ITS WRITER LIVES. A holder that dies lets go
of the lock and leaves its note: one was found beside the machine's own lock,
a test coordinator's, long dead. So a note read beside a lock found held is
trusted only while the process it names (`pid`) is alive and started before
the note was written (`since`) - a pid that has gone, or one handed on to a
process started after it, makes the note STALE (`stale_note`): it names
nobody, and whoever reads it says so. A pid that cannot be asked about is
trusted: a note is called stale on proof, never on a guess.

Standard library only. The coordinator shares no code with the server: this
file's process questions, and its lock, are its own - the lock's name and
its note are the contract's.
"""
from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
#: the machine's card lock, as the contract names it: its file, and the note
#: its holder writes beside it
CARD_LOCK = json.loads((Path(__file__).resolve().parent.parent / "llm-bootstrap-contract"
                        / "machine" / "card-lock.json").read_text(encoding="utf-8"))
NOTE_SUFFIX = CARD_LOCK["note_file"][len(CARD_LOCK["file"]):]
#: how long an ended process is waited for before the record says it did not go
END_WAIT_S = 15.0
#: how much longer than that the ends of one rental's processes, together,
#: are waited for - an end that does not keep its own bound is given up then
END_JOIN_MARGIN_S = 5.0
#: the longest the one query for the command lines may take
QUERY_S = 30.0
#: how often that query is asked, in all, for the processes it could not read
#: - held open all the while, so no pid changes hands: on a busy machine one
#: query did not answer within its bound, and what it could not read was left
#: on the card for good (H4's R1). Asked in vain this often, a process is left
#: alone, and said to be: never ended on a guess
QUERY_ROUNDS = 3
#: the pause before the query is asked again
QUERY_PAUSE_S = 1.0
#: how much later than its note's `since` the process a note names may have
#: started and still be the one that wrote it: both are this machine's clock,
#: read a moment apart - a step of the clock between them is all this allows for
NOTE_CLOCK_SLACK_S = 2.0

QUERY = ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"]


def ask_nvidia_smi() -> str | None:
    """nvidia-smi's answer, or None when it could not be asked."""
    try:
        out = subprocess.run(QUERY, capture_output=True, text=True, timeout=10,
                             creationflags=NO_WINDOW)
    except Exception:                                       # noqa: BLE001
        return None
    return out.stdout if out.returncode == 0 else None


class NoHardware:
    """What the coordinator asks when nothing about the machine is to be asked:
    it knows nothing about the card, cannot say which process is alive, and
    ends nothing - it has no `alive` and no `end`. The record is updated and
    decisions are by liveness alone. Every coordinator built in code starts
    here, so no suite asks a card or ends a process unless it hands in a
    machine that does."""

    def probe(self, fresh: bool = False) -> dict:
        return {"known": False, "used_mib": None, "total_mib": None}


class ThisMachine:
    """The card as nvidia-smi sees it, and the processes of this machine."""

    def __init__(self, query=None, end=None, alive=None, cache_s: float = 2.0,
                 clock=time.monotonic):
        self._query = query or ask_nvidia_smi
        self._end = end or end_all_on_proof
        self._alive = alive or processes_alive
        self.cache_s, self._clock = float(cache_s), clock
        self._last, self._last_at = None, None

    def probe(self, fresh: bool = False) -> dict:
        now = self._clock()
        if not fresh and self._last is not None and now - self._last_at < self.cache_s:
            return dict(self._last)
        got = {"known": False, "used_mib": None, "total_mib": None}
        text = self._query()
        try:
            used, total = (int(x) for x in str(text).strip().splitlines()[0].split(","))
            got = {"known": True, "used_mib": used, "total_mib": total}
        except (ValueError, IndexError, AttributeError):
            pass
        self._last, self._last_at = got, now
        return dict(got)

    def alive(self, processes) -> list:
        """Which of *processes* are still alive: one answer each, in order."""
        return self._alive(processes)

    def end(self, processes) -> list:
        """End *processes*, each on its own proof: one outcome each, in order."""
        return self._end(processes)


# -------------------------------------------------------------- the processes

def end_on_proof(pid: int, proof: str, wait_s: float = END_WAIT_S) -> str:
    """End *pid* if it is alive and its command line carries *proof*; wait for
    it to go. -> what happened, as a sentence for the record."""
    return end_all_on_proof([{"pid": pid, "proof": proof}], wait_s)[0]["detail"]


def end_all_on_proof(processes, wait_s: float = END_WAIT_S, read=None, hold=None) -> list:
    """End every one of *processes* ({pid, proof, ...}) that is alive and whose
    command line carries its proof: each held open first, their command lines
    read in ONE query - what it could not read asked again, up to
    QUERY_ROUNDS asks in all - each ended on a thread of its own and waited
    for with its own bound. -> [{"outcome", "detail"}], one per process, in order.
    `read` (the query) and `hold` (a process held open) are this machine's own
    unless a suite hands in its stand-ins."""
    hold = hold or Held
    read = read or command_lines
    held = [hold(int(p["pid"])) for p in processes]
    try:
        out: list = [None] * len(processes)
        asked = []
        for i, (p, h) in enumerate(zip(processes, held)):
            pid = int(p["pid"])
            if h.state == "gone":
                out[i] = _said("gone", f"its process {pid} was already gone")
            elif h.state == "unopened":
                out[i] = _said("left_alone", f"its process {pid} could not be opened ({h.why}); "
                                             f"left alone")
            else:
                asked.append(i)
        lines = dict(read([int(processes[i]["pid"]) for i in asked])) if asked else {}
        # WHAT THE QUERY COULD NOT READ IS ASKED AGAIN, never read as "not
        # ours": a query that did not answer - on a busy machine one took 14 s,
        # and one did not answer within its bound - left the rental's
        # processes on the card for good (H4's R1). Each is still held open,
        # so its pid cannot change hands between the asks
        rounds = 1
        while rounds < QUERY_ROUNDS:
            unread = [int(processes[i]["pid"]) for i in asked
                      if lines.get(int(processes[i]["pid"])) is None]
            if not unread:
                break
            time.sleep(QUERY_PAUSE_S)
            rounds += 1
            lines.update({p: text for p, text in dict(read(unread)).items() if text is not None})
        workers = []
        for i in asked:
            pid, proof = int(processes[i]["pid"]), str(processes[i]["proof"])
            line = lines.get(pid)
            if line is None:
                out[i] = _said("left_alone", f"the command line of its process {pid} could not "
                                             f"be read; left alone (asked {rounds} "
                                             f"time{'s' if rounds != 1 else ''})")
            elif proof not in line:
                out[i] = _said("left_alone", f"pid {pid} does not carry {proof} on its command "
                                             f"line - it is not the process the rental named; "
                                             f"left alone")
            else:
                worker = threading.Thread(target=_end_one, args=(held[i], pid, proof, wait_s, out, i),
                                          daemon=True, name=f"end-{pid}")
                workers.append(worker)
                worker.start()
        # ONE deadline for them all: joined one after another with a bound
        # each, ends that outlast their own bound would add up to the sum
        deadline = time.monotonic() + wait_s + END_JOIN_MARGIN_S
        for worker in workers:
            worker.join(max(0.0, deadline - time.monotonic()))
        return [said if said is not None else
                _said("not_ended", f"its process {int(p['pid'])} ({p['proof']}) was not ended "
                                   f"within {wait_s + END_JOIN_MARGIN_S:g}s")
                for said, p in zip(out, processes)]
    finally:
        for h in held:
            h.close()


def _said(outcome: str, detail: str) -> dict:
    return {"outcome": outcome, "detail": detail}


def _end_one(h, pid: int, proof: str, wait_s: float, out: list, i: int) -> None:
    """End one process held open, and wait for it to go - on its own thread,
    with its own bound."""
    why = h.terminate()
    if why:
        out[i] = _said("not_ended", f"its process {pid} ({proof}) could not be ended ({why})")
    elif not h.wait(wait_s):
        out[i] = _said("not_ended", f"ended its process {pid} ({proof}), and it had not gone "
                                    f"{wait_s:g}s later")
    else:
        out[i] = _said("ended", f"ended its process {pid} ({proof})")


def processes_alive(processes) -> list:
    """Which of *processes* ({pid, ...}) are still alive, by pid, in order. A
    pid that cannot be asked about reads as alive - the side that waits, and
    then asks its command line before anything is ended."""
    out = []
    for p in processes:
        h = Held(int(p["pid"]))
        try:
            out.append(h.state != "gone")
        finally:
            h.close()
    return out


def command_lines(pids) -> dict:
    """The command lines of *pids*, read in ONE query: {pid: line}, None where
    it could not be read."""
    pids = [int(p) for p in pids]
    out = {pid: None for pid in pids}
    if not pids:
        return out
    if sys.platform != "win32":
        for pid in pids:
            try:
                raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            except OSError:
                continue
            out[pid] = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip() or None
        return out
    where = " OR ".join(f"ProcessId={pid}" for pid in pids)
    query = (f"Get-CimInstance Win32_Process -Filter '{where}' | "
             "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
    try:
        got = subprocess.run(["powershell", "-NoProfile", "-Command", query],
                             capture_output=True, text=True, timeout=QUERY_S,
                             creationflags=NO_WINDOW)
    except Exception:                                       # noqa: BLE001
        return out
    current = None
    for line in got.stdout.splitlines():
        head, tab, rest = line.partition("\t")
        if tab and head.strip().isdigit() and int(head) in out:
            current = int(head)
            out[current] = rest.strip() or None
        elif current is not None and out[current] is not None:
            out[current] += "\n" + line                     # a command line of several lines
    return out


def command_line(pid: int):
    """*pid*'s command line, or None when it cannot be read."""
    return command_lines([pid])[int(pid)]


# Windows: a process is OPENED first, and the handle held while its command
# line is read and it is ended - a pid is never reused while a handle to its
# process is open.
_SYNCHRONIZE, _TERMINATE, _QUERY_LIMITED = 0x00100000, 0x0001, 0x1000
_WAIT_OBJECT_0 = 0x0
_ERROR_INVALID_PARAMETER = 87
_K32 = None


def _k32():
    global _K32
    if _K32 is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = ctypes.c_void_p
        k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k.WaitForSingleObject.restype = ctypes.c_uint32
        k.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        times = ctypes.POINTER(_FILETIME)
        k.GetProcessTimes.argtypes = [ctypes.c_void_p, times, times, times, times]
        k.GetProcessTimes.restype = ctypes.c_int
        _K32 = k
    return _K32


class _FILETIME(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


#: from 1601-01-01, where Windows counts a process's start from, to the epoch
_FILETIME_EPOCH_S = 11644473600


class Held:
    """A process held open while it is dealt with: its handle on Windows, a
    pidfd where Linux has one. `state` is alive, gone, or unopened (`why` says
    why it could not be opened)."""

    def __init__(self, pid: int):
        self.pid, self.state, self.why = int(pid), "alive", None
        self._h = self._fd = None
        if sys.platform == "win32":
            k = _k32()
            h = k.OpenProcess(_SYNCHRONIZE | _TERMINATE | _QUERY_LIMITED, False, self.pid)
            if not h:
                err = ctypes.get_last_error()
                if err == _ERROR_INVALID_PARAMETER:
                    self.state = "gone"
                else:
                    self.state, self.why = "unopened", f"error {err}"
                return
            self._h = h
            if k.WaitForSingleObject(h, 0) == _WAIT_OBJECT_0:
                self.state = "gone"
            return
        try:
            self._fd = os.pidfd_open(self.pid)
        except ProcessLookupError:
            self.state = "gone"
            return
        except (AttributeError, OSError):
            self._fd = None
        if _gone_posix(self.pid):
            self.state = "gone"

    def terminate(self):
        """End it at once. -> None, or why it could not be ended."""
        if sys.platform == "win32":
            if not _k32().TerminateProcess(self._h, 1):
                return f"error {ctypes.get_last_error()}"
            return None
        try:
            if self._fd is not None:
                signal.pidfd_send_signal(self._fd, signal.SIGKILL)
            else:
                os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            return None                                     # gone already: the wait says so
        except OSError as exc:
            return str(exc)
        return None

    def wait(self, seconds: float) -> bool:
        """Has it gone within *seconds*?"""
        if sys.platform == "win32":
            return _k32().WaitForSingleObject(self._h, int(max(0.0, seconds) * 1000)) \
                == _WAIT_OBJECT_0
        deadline = time.monotonic() + seconds
        while True:
            if _gone_posix(self.pid):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def close(self) -> None:
        if self._h is not None:
            _k32().CloseHandle(self._h)
            self._h = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def _gone_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    try:                                                    # a zombie holds no memory
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] == "Z"
    except OSError:
        return False


# ------------------------------------------------------------ the card lock

def process_started(pid):
    """When process *pid* started, in seconds since the epoch - or "gone": no
    process has that pid, or the one that had it has ended - or None: it is
    alive, and when it started cannot be told, or it cannot be asked about."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if not 1 <= pid <= 0xFFFFFFFF:
        return "gone"                                       # no process has such a pid
    if sys.platform == "win32":
        k = _k32()
        h = k.OpenProcess(_SYNCHRONIZE | _QUERY_LIMITED, False, pid)
        if not h:
            return "gone" if ctypes.get_last_error() == _ERROR_INVALID_PARAMETER else None
        try:
            if k.WaitForSingleObject(h, 0) == _WAIT_OBJECT_0:
                return "gone"                               # ended; only a handle keeps it
            t = [_FILETIME() for _ in range(4)]
            if not k.GetProcessTimes(h, *(ctypes.byref(x) for x in t)):
                return None
            return ((t[0].high << 32) | t[0].low) / 1e7 - _FILETIME_EPOCH_S
        finally:
            k.CloseHandle(h)
    if _gone_posix(pid):
        return "gone"
    try:
        after = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        boot = next(float(x.split()[1]) for x in Path("/proc/stat").read_text().splitlines()
                    if x.startswith("btime "))
        return boot + int(after[19]) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def stale_note(note):
    """Why *note* - read beside a lock found held - is not its holder's, in
    the operator's words: the process it names has gone, or its pid was handed
    on to a process started after the note was written. None when it may be
    its holder's: that process lives and started before the note, or cannot
    be asked about - a note is stale on proof, never on a guess."""
    if not isinstance(note, dict):
        return None
    pid, since = note.get("pid"), note.get("since")
    started = process_started(pid)
    if started == "gone":
        return f"pid {pid}, which wrote it, has gone"
    if isinstance(started, float) and isinstance(since, (int, float)) \
            and not isinstance(since, bool) and started > since + NOTE_CLOCK_SLACK_S:
        return (f"pid {pid} is a process started after the note was written - the one that "
                f"wrote it has gone")
    return None


def machine_lock() -> Path:
    """Where every coordinator, and every server run on its own, on this
    machine meet - for this user: the contract's lock file in the user's
    temporary folder. A suite hands in a lock of its own, and never asks
    this."""
    return _the_machines_lock()


def _the_machines_lock() -> Path:
    return Path(tempfile.gettempdir()) / CARD_LOCK["file"]


def is_machine_lock(path) -> bool:
    """Is *path* the machine's own lock (S16a R4)? Its name alone is compared:
    the lock is neither opened nor asked."""
    return Path(path) == _the_machines_lock()


class MachineLock:
    """The machine's card lock, as the coordinator holds it. Made, it touches
    nothing; `take` tries it once, never waiting - whoever waits for it asks
    again. The note is written once the lock is had, and taken away before
    it is let go, so a note beside a held lock is its holder's - while the
    process that wrote it lives: a holder that died left its note behind, and
    until the next holder writes its own, that note is stale (`stale_note`)."""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else machine_lock()
        self.note_path = self.path.with_name(self.path.name + NOTE_SUFFIX)
        self._fh = None
        self._lock = threading.Lock()

    @property
    def held(self) -> bool:
        return self._fh is not None

    def take(self) -> bool:
        """Take the lock if nobody holds it: -> whether this holds it now.
        Windows locks per handle, so a second lock in this same process is
        refused like any other process's."""
        with self._lock:
            if self._fh is not None:
                return True
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.path, "a+b")                     # never truncated, never read
            try:
                if sys.platform == "win32":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                return False
            self._fh = fh
            return True

    def write_note(self, note: dict) -> None:
        """The holder's note, whole or not at all: written under a name of its
        own, then put in place. Best effort: a holder that cannot say who it
        is still holds."""
        tmp = self.note_path.with_name(f"{self.note_path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_bytes(json.dumps(note).encode("utf-8"))
            os.replace(tmp, self.note_path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass

    def note(self):
        """The note beside the lock, as its holder wrote it - or None: there
        is none, or it cannot be read. Read, never trusted for anything but
        saying who holds the card - and for that only once `stale_note` finds
        nothing against it."""
        try:
            got = json.loads(self.note_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return got if isinstance(got, dict) else None

    def release(self) -> None:
        """Let go: the note first, then the lock. Nothing, when not held."""
        with self._lock:
            fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            self.note_path.unlink()
        except OSError:
            pass
        try:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass                                            # closing lets it go anyway
        finally:
            fh.close()
