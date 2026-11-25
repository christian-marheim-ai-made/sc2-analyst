"""The card, watched from outside: what the runner's own record cannot prove.

    py -3.12 cardwatch.py            # the card now - its memory, and every model server

The runner's record says what IT did; this says what the MACHINE did - how
much of the card was in use, sampled all the time, and which model servers -
every engine's: llama-server, whisper-server (runner/engines.py) - were alive,
sampled all the time, each with its PARENT and its engine. So a suite
can hold a run to the one claim a card with room for one model cares about -
never two models on it at once - and to the card coming back when the model
came off.

IT ASKS AND NEVER ACTS. Nothing here starts or stops a process.

  memory      one long-lived `nvidia-smi ... -lms` reader: a line per sample,
              no process started per sample.
  processes   the kernel's snapshot (Toolhelp32): pid, PARENT pid and program
              of every process, in a few milliseconds. The parent is how a
              caller tells its own model servers from anybody else's - and it
              still holds after the parent was killed: Windows never re-parents
              an orphan.
  who         a command line is asked for (`Win32_Process`, slow) once, when a
              new model server appears - never per sample. Its key is blanked.

None is not zero: a card that cannot be read reads as UNKNOWN, never as free.
"""
from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from runner.engines import ENGINES, engine_of_process          # noqa: E402
from runner.manager import process_info                         # noqa: E402

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
#: llama.cpp's executable, as a process list names it (every engine's are in
#: runner/engines.py)
LLAMA_NAMES = ENGINES["llamacpp"].names
_KEY = re.compile(r"(--api-key[\s=]+)(\S+)")


# ------------------------------------------------------------------ processes

def processes() -> list:
    """(pid, parent pid, program) for every process now, from one snapshot."""
    if sys.platform == "win32":
        return _toolhelp()
    out = []
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            raw = stat.read_text(errors="replace")
        except OSError:
            continue
        name = raw[raw.find("(") + 1:raw.rfind(")")]
        rest = raw[raw.rfind(")") + 2:].split()
        try:
            out.append((int(stat.parent.name), int(rest[1]), name))
        except (IndexError, ValueError):
            continue
    return out


def _toolhelp() -> list:
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k.Process32FirstW.restype = wintypes.BOOL
    k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k.Process32NextW.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    snap = k.CreateToolhelp32Snapshot(0x00000002, 0)          # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        raise OSError(f"CreateToolhelp32Snapshot failed, GetLastError={ctypes.get_last_error()}")
    out = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID),
                        entry.szExeFile))
            ok = k.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k.CloseHandle(snap)
    return out


def model_servers(snapshot=None) -> list:
    """{"pid", "ppid", "engine"} of every model server alive now - every
    engine's - from one snapshot of the processes (or the one given)."""
    found = []
    for pid, ppid, name in (processes() if snapshot is None else snapshot):
        engine = engine_of_process(name)
        if engine is not None:
            found.append({"pid": pid, "ppid": ppid, "engine": engine})
    return found


def llama_servers() -> list:
    """{"pid", "ppid"} of every llama-server alive now."""
    return [{"pid": s["pid"], "ppid": s["ppid"]} for s in model_servers()
            if s["engine"] == "llamacpp"]


def command_line(pid):
    """The command line of *pid*, its key blanked; None when it cannot be read."""
    line = process_info(pid).get("command_line")
    return _KEY.sub(r"\1***", line) if line else None


def api_key(pid):
    """The `--api-key` on *pid*'s command line - a guard against a misdirected
    request, on the command line for anyone at this machine to read. For a
    caller that must ask a llama-server something itself; never printed."""
    line = process_info(pid).get("command_line") or ""
    m = _KEY.search(line)
    return m.group(2).strip("\"'") if m else None


# ----------------------------------------------------------------------- card

def card_now():
    """{"used", "total", "free"} in MiB, or None when the card cannot be read."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=15,
                             creationflags=NO_WINDOW)
        used, total = (int(x) for x in out.stdout.strip().splitlines()[0].split(","))
    except Exception:                                       # noqa: BLE001 - unknown, never free
        return None
    return {"used": used, "total": total, "free": total - used}


# ---------------------------------------------------------------------- watch

class CardWatch:
    """The card and its model servers - every engine's - sampled until
    `stop()`.

    `ours` is a SET OF PARENT PIDS, held by reference: a model server whose
    parent is in it is the caller's, anything else is FOREIGN - somebody
    else's model on the card. A caller adds a process it starts before that
    process can start a model server."""

    def __init__(self, ours: set, mem_every_ms: int = 200, proc_every_s: float = 0.1,
                 on_foreign=None):
        self.ours = ours
        self.mem_every_ms, self.proc_every_s = int(mem_every_ms), float(proc_every_s)
        self.on_foreign = on_foreign
        self.samples: list = []             # (t, used MiB)
        self.total = None
        self.markers: list = []             # (t, text)
        self.seen: dict = {}                # pid -> {ppid, first, last, ours, cmd}
        self.alive: set = set()
        self.peak_together = 0              # the most of OUR model servers alive at once
        self.together_at = None             # when that peak (above one) was first seen
        self.foreign: list = []
        self.errors: list = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._smi = None
        self._threads: list = []

    # ------------------------------------------------------------ lifecycle

    def start(self) -> "CardWatch":
        self._smi = subprocess.Popen(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits", "-lms", str(self.mem_every_ms)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            text=True, creationflags=NO_WINDOW)
        for target, name in ((self._read_memory, "cardwatch-memory"),
                             (self._read_processes, "cardwatch-processes")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        deadline = time.monotonic() + 5
        while not self.samples and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self.samples:
            self.stop()
            raise RuntimeError("the card could not be read: nvidia-smi gave no sample in 5s")
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._smi is not None:
            try:
                self._smi.kill()
                self._smi.wait(timeout=5)
            except Exception:                               # noqa: BLE001
                pass
        for t in self._threads:
            t.join(timeout=3)
        if self._smi is not None and self._smi.stdout is not None:
            try:
                self._smi.stdout.close()
            except Exception:                               # noqa: BLE001
                pass

    # -------------------------------------------------------------- readers

    def _read_memory(self) -> None:
        for line in self._smi.stdout:
            if self._stop.is_set():
                return
            try:
                used, total = (int(x) for x in line.strip().split(","))
            except ValueError:
                continue
            with self._lock:
                self.total = total
                self.samples.append((time.time(), used))

    def _read_processes(self) -> None:
        while not self._stop.is_set():
            try:
                servers = model_servers()
                now = {s["pid"]: s["ppid"] for s in servers}
                engines = {s["pid"]: s["engine"] for s in servers}
            except Exception as exc:                        # noqa: BLE001
                self.errors.append(f"process snapshot failed: {exc}")
                self._stop.wait(self.proc_every_s)
                continue
            t = time.time()
            fresh = []
            with self._lock:
                for pid, ppid in now.items():
                    if pid not in self.seen:
                        mine = ppid in self.ours
                        self.seen[pid] = {"ppid": ppid, "first": t, "last": t,
                                          "ours": mine, "cmd": None,
                                          "engine": engines.get(pid)}
                        fresh.append(pid)
                        if not mine:
                            self.foreign.append(pid)
                    self.seen[pid]["last"] = t
                gone = self.alive - set(now)
                for pid in gone:
                    self.seen[pid]["gone"] = t
                self.alive = set(now)
                together = sum(1 for pid in now if self.seen[pid]["ours"])
                if together > self.peak_together:
                    self.peak_together = together
                    if together > 1 and self.together_at is None:
                        self.together_at = t
            for pid in fresh:
                threading.Thread(target=self._who, args=(pid,), daemon=True,
                                 name=f"cardwatch-who-{pid}").start()
                if not self.seen[pid]["ours"] and self.on_foreign is not None:
                    try:
                        self.on_foreign(pid, self.seen[pid]["ppid"])
                    except Exception:                       # noqa: BLE001
                        pass
            self._stop.wait(self.proc_every_s)

    def _who(self, pid) -> None:
        cmd = command_line(pid)
        with self._lock:
            if pid in self.seen:
                self.seen[pid]["cmd"] = cmd

    # ------------------------------------------------------------- readings

    def mark(self, text: str) -> None:
        with self._lock:
            self.markers.append((time.time(), text))

    def used(self):
        with self._lock:
            return self.samples[-1][1] if self.samples else None

    def peak_used(self, since=None):
        with self._lock:
            vals = [u for t, u in self.samples if since is None or t >= since]
        return max(vals) if vals else None

    def wait_used(self, at_most: int, timeout: float, since=None):
        """Seconds until the card's used memory is at most *at_most* - read off
        samples taken after *since* (default: now) - or None if it never was."""
        start = time.time() if since is None else since
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                hit = next((t for t, u in self.samples if t >= start and u <= at_most), None)
            if hit is not None:
                return max(0.0, hit - start)
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)

    def ours_alive(self) -> list:
        with self._lock:
            return sorted(p for p in self.alive if self.seen[p]["ours"])

    def first_seen(self, pid):
        with self._lock:
            return (self.seen.get(pid) or {}).get("first")

    def gone_at(self, pid):
        with self._lock:
            return (self.seen.get(pid) or {}).get("gone")

    def describe(self, pid) -> str:
        with self._lock:
            s = dict(self.seen.get(pid) or {})
        return f"pid {pid} (parent {s.get('ppid')}): {s.get('cmd') or 'command line not read'}"

    # ------------------------------------------------------------- timeline

    def timeline(self, since: float, extra=(), step_mib: int = 512) -> list:
        """What happened after *since*, in order: the card's memory each time it
        moved by *step_mib* or more (and its peak), every model server appearing
        and going, every marker, and any (t, text) pairs the caller adds - the
        runner's own record, say."""
        with self._lock:
            samples = [s for s in self.samples if s[0] >= since]
            seen = {p: dict(s) for p, s in self.seen.items()}
            marks = [m for m in self.markers if m[0] >= since]
        rows = []
        last = None
        peak = max(samples, key=lambda s: s[1]) if samples else None
        for t, u in samples:
            if last is None or abs(u - last) >= step_mib or (t, u) == peak:
                rows.append((t, f"card {u} MiB" + ("  <- peak" if (t, u) == peak else "")))
                last = u
        for pid, s in seen.items():
            who = "ours" if s["ours"] else "FOREIGN"
            if s["first"] >= since:
                rows.append((s["first"], f"{_server(s)} {pid} appears ({who}, "
                                         f"parent {s['ppid']})"))
            if s.get("gone") and s["gone"] >= since:
                rows.append((s["gone"], f"{_server(s)} {pid} gone"))
        rows += [(t, f">> {text}") for t, text in marks]
        rows += [(t, text) for t, text in extra if t >= since]
        rows.sort(key=lambda r: r[0])
        return [f"{t - since:8.2f}s  {text}" for t, text in rows]


def _server(seen: dict) -> str:
    """A model server's program, by its engine: llama-server, whisper-server."""
    engine = ENGINES.get(seen.get("engine") or "llamacpp")
    return engine.names[-1] if engine else "model server"


# ------------------------------------------------------------------------ now

def main() -> int:
    card = card_now()
    if card is None:
        print("card: UNKNOWN - nvidia-smi could not be read (unknown is never free)")
    else:
        print(f"card: {card['used']} MiB used of {card['total']} ({card['free']} free)")
    servers = model_servers()
    if not servers:
        print(f"model servers ({', '.join(e.names[-1] for e in ENGINES.values())}): none running")
    for s in servers:
        print(f"{_server(s)}: pid {s['pid']} (parent {s['ppid']}): "
              f"{command_line(s['pid']) or 'command line not read'}")
    return 0 if card is not None and not servers else 1


if __name__ == "__main__":
    raise SystemExit(main())
