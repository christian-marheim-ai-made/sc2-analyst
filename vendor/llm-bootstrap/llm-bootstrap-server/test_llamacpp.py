"""The llama.cpp backend's rows - against a stand-in llama-server, never a model.

    py -3.12 test_llamacpp.py

No GPU, no weights, no llama.cpp build. Every server this suite starts is
`fake_llama_server.py`: llama-server's command line and routes, with switches
of its own to misbehave on purpose.

TWO GUARDS, installed before anything runs:

  * THIS SUITE STARTS NOTHING BUT ITS OWN STAND-IN. `runner.llamacpp.SPAWN` -
    the one place a model server is started - refuses any command that is not
    this interpreter running `fake_llama_server.py`. A settings file that
    pointed at a real llama-server would fail the row, not load a model.
  * THIS SUITE KILLS ONLY WHAT IT STARTED. The manager's `kill_pid` refuses any
    process whose command line does not carry the load id of one of this
    suite's own launches - never a pid alone, which Windows hands out again
    constantly. The reclaim rows kill for real - a stand-in this suite started
    - and nothing else can be reached.

The job-object rows kill a real PARENT with `taskkill /F` (TerminateProcess, no
`/T`) and look for its real child by command line - never by start time.

Two kinds of rows, as in the other suites: CARRIED OVER BY MEANING from an
existing application's runner suites, and NATIVE ones, marked `Native`.
"""
from __future__ import annotations

import contextlib
import ctypes
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import runner.manager as M                                      # noqa: E402  (puts the contract on the path)
import runner.llamacpp as L                                     # noqa: E402
from _suites import KILL_WAIT_S, asked_again, killed, quiet_ports  # noqa: E402
import _suites                                                  # noqa: E402
#: THE REAL CARD'S READER, kept before the tripwire takes its place: one row
#: reads its parsing with `subprocess` stood in for, and asks the card nothing
_REAL_READER = M.ModelManager.card_memory_mib
_suites.forbid_the_card()        # a suite never asks the real card (nvidia-smi)
import ids                                                      # noqa: E402
from runner import (BackendError, Cancelled, Config, GenParams,  # noqa: E402
                    ModelManager, max_concurrency, orphaned_loads, read_events)
from runner.base import Backend, PromptTooLong                  # noqa: E402
from runner.config import ModelSpec                             # noqa: E402
from runner.gguf import (GGUFError, check_against_spec,         # noqa: E402
                         kv_cache_bytes, read_header)
from runner.jobobject import (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,  # noqa: E402
                              SUPPORTED, KillOnCloseJob,
                              _JOBOBJECT_EXTENDED_LIMIT_INFORMATION)
from runner.llamacpp import (OPTIONAL_FLAG_NAMES, LlamaCppBackend,  # noqa: E402
                             port_in_use)
from runner.cardlock import CardWaitRefused, MachineCard           # noqa: E402
from test_queue import every_parameter, same                    # noqa: E402

FAKE = HERE / "fake_llama_server.py"
PARENT = HERE / "_job_parent.py"
LAUNCHER = [sys.executable, str(FAKE)]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
W = "srv-w5r8t2hh"
#: a child is given this long to die after its parent is terminated - slack
#: for teardown and for the process query to see it, not a wait for anything
#: asynchronous
DEATH_S = 20.0
#: a control child is watched this long to show it does NOT die
SURVIVAL_S = 8.0

# ------------------------------------------------------------------ guards

#: every LAUNCH, in order: (pid, the load id its command line carries as
#: --alias). A launch is counted by this list, never by a pid nobody saw
#: before: Windows hands out pids again all the time - measured, 91 of 300
#: processes started in one run were given a pid an earlier one had had - so
#: "no new pid" does not mean "nothing was started", and two launches can
#: share one pid
SPAWNS: list = []
_real_spawn = L.SPAWN


def _alias(argv) -> str | None:
    return next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a in ("--alias", "-a")), None)


def launches_since(before: int) -> list:
    """The pids of the launches made since `before = len(SPAWNS)` was taken."""
    return [pid for pid, _ in SPAWNS[before:]]


def launched_since(before: int) -> list:
    """(pid, load id) of each launch made since `before = len(SPAWNS)` was
    taken: what is asked whether it is gone - a pid alone is not."""
    return list(SPAWNS[before:])


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _only_the_stand_in(args, *a, **k):
    argv = [str(x) for x in args]
    if not (len(argv) >= 2 and all(_same_path(x, y) for x, y in zip(argv[:2], LAUNCHER))):
        raise AssertionError(f"this suite starts nothing but its own stand-in llama-server, "
                             f"and was asked to start {argv[:2]}")
    proc = _real_spawn(args, *a, **k)
    SPAWNS.append((proc.pid, _alias(argv)))
    return proc


_real_kill = M.kill_pid


def _only_what_it_started(pid):
    """A pid is not an identity - Windows hands them out again constantly -
    so a process is this suite's only if its command line carries the load id
    one of this suite's launches gave a process of that pid. Found by a sweep:
    a fresh process was handed a pid this suite had once had, and a guard that
    asked only the pid let the kill through. On Windows the command line is
    read through a handle held across the kill (see `gone`): the pid cannot
    be handed on between the proof and the kill, and no process query that
    fails under load turns a kill of this suite's own into a refusal."""
    mine = {alias for p, alias in SPAWNS if p == int(pid) and alias}
    handle = open_process(pid)[0] if mine and sys.platform == "win32" else None
    try:
        if handle is not None:
            line = command_line_of(handle) or ""
        else:
            line = (M.process_info(pid).get("command_line") or "") \
                if mine and sys.platform != "win32" else ""
        if not any(alias in line for alias in mine):
            raise AssertionError(f"this suite kills only what it started, and was asked to "
                                 f"kill pid {pid}")
        return _real_kill(pid)
    finally:
        if handle is not None:
            close_process(handle)


L.SPAWN = _only_the_stand_in
M.kill_pid = _only_what_it_started

# WHAT THE PROCESS LIST COULD NOT ANSWER IS ASKED AGAIN. The runner's
# `process_info` asks the operating system's process list - on Windows one
# query, bounded at 30 s - and says `alive: None` when it could not be asked,
# which the runner reads, rightly, as "not provably ours": left alone, and
# asked again at the next start (the runner suite's rows prove that reading).
# Beside three sweeps one query did not answer in its bound, and the reclaim
# row read the stopped server it had just left running as none of its own.
# Every manager this suite builds - the adapter suite's too, which imports
# this one - is handed a process list that asks again within a bound.
_real_process_info = M.process_info
M.process_info = lambda pid: asked_again(lambda: _real_process_info(pid),
                                         lambda info: info.get("alive") is not None,
                                         f"whether pid {pid} is alive")

# THIS SUITE NEVER TAKES THE MACHINE'S CARD LOCK. A llama.cpp runner with no
# coordinator takes the card through a lock every server on the machine shares
# (runner/cardlock.py); a suite that took it could hold up a real server - or
# be held up by one, or by another copy of itself. So every runner built here
# gets a lock file of its own, and a row that means two runners to share one
# names it.
import atexit                                                   # noqa: E402
import itertools                                                # noqa: E402
import runner.cardlock as _CL                                   # noqa: E402
_LOCKS = Path(tempfile.mkdtemp(prefix="llmb-locks-"))
_LOCK_N = itertools.count()
_CL.machine_lock = lambda: _LOCKS / f"card-{next(_LOCK_N)}.lock"
atexit.register(shutil.rmtree, _LOCKS, True)


# ------------------------------------------------------------------- helpers

#: where a stand-in llama-server's port comes from: BELOW every common system's
#: range for passing connections (Windows from 49152, Linux from 32768), and
#: clear of every other suite's range. A port picked by the operating system
#: comes from that range, and between the pick and the stand-in binding it, any
#: connection on the machine could be handed it. And only from the block of it
#: this copy of the suite claimed (`_suites.QuietPorts`): another copy running
#: beside it once took 29445 between this one's bind-check and its launch.
QUIET_PORTS = (29000, 29499)
QUIET = quiet_ports(*QUIET_PORTS)


def quiet_port() -> int:
    """A port of this copy's own that nothing listens on now."""
    return QUIET.take()


#: A load whose settings name no port takes one from this copy's block too,
#: never one the operating system picks (`FreePorts`): between that pick and
#: the stand-in's bind any connection on the machine can be handed it, and the
#: stand-in binds exclusively. Every suite that imports this one gets it.
M.free_port = lambda host="127.0.0.1": QUIET.take()


def _gstr(text: str) -> bytes:
    raw = text.encode()
    return struct.pack("<Q", len(raw)) + raw


def _kv_str(key, value):
    return _gstr(key) + struct.pack("<I", 8) + _gstr(value)


def _kv_u32(key, value):
    return _gstr(key) + struct.pack("<I", 4) + struct.pack("<I", value)


def _kv_str_array(key, items):
    out = _gstr(key) + struct.pack("<I", 9) + struct.pack("<I", 8) + struct.pack("<Q", len(items))
    return out + b"".join(_gstr(i) for i in items)


def build_gguf(directory: Path, *, name="test.gguf", arch="llama", layers=40, kv_heads=8,
               key_len=128, ctx=131072, template=True, tensor_elems=(4096 * 4096,),
               bits_per_weight=None, tokens=200) -> Path:
    """A GGUF header with the fields that matter; `bits_per_weight` pads the
    file so the truncation check sees a plausible quantisation."""
    kvs = [_kv_str("general.architecture", arch), _kv_str("general.name", "Test Model"),
           _kv_u32("general.file_type", 17), _kv_u32(f"{arch}.block_count", layers),
           _kv_u32(f"{arch}.context_length", ctx), _kv_u32(f"{arch}.embedding_length", 5120),
           _kv_u32(f"{arch}.attention.head_count", 32),
           _kv_u32(f"{arch}.attention.head_count_kv", kv_heads),
           _kv_u32(f"{arch}.attention.key_length", key_len),
           _kv_str_array("tokenizer.ggml.tokens", [f"tok{i}" for i in range(tokens)])]
    if template:
        kvs.append(_kv_str("tokenizer.chat_template", "{% for m in messages %}{{m}}{% endfor %}"))
    tensors = b"".join(_gstr(f"t{i}") + struct.pack("<I", 1) + struct.pack("<Q", n)
                       + struct.pack("<I", 13) + struct.pack("<Q", 0)
                       for i, n in enumerate(tensor_elems))
    header = (b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", len(tensor_elems))
              + struct.pack("<Q", len(kvs)))
    body = header + b"".join(kvs) + tensors
    if bits_per_weight:
        body += b"\0" * max(0, int(sum(tensor_elems) * bits_per_weight / 8) - len(body))
    path = directory / name
    path.write_bytes(body)
    return path


def cmdlines_containing(needle: str) -> list | None:
    """(pid, command line) of every process whose command line holds *needle*
    - or None when the process list could not be asked: a query that did not
    answer, or answered with an error, is never "nothing found" (on a busy
    machine one read a live child as dead). BY COMMAND LINE, NEVER BY START
    TIME. A process whose pid a row knows is asked about through a handle to
    it (`gone`), never through this list."""
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "
             f"'*{needle}*' }} | ForEach-Object {{ \"$($_.ProcessId)`t$($_.CommandLine)\" }}"],
            capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
    except Exception:                                           # noqa: BLE001
        return None
    if done.returncode != 0:
        return None
    found = []
    for line in done.stdout.splitlines():
        pid, _, cmd = line.partition("\t")
        if pid.strip().isdigit() and "powershell" not in cmd.lower():
            found.append((int(pid.strip()), cmd.strip()))
    return found


def taskkill(pid: int) -> None:
    """TerminateProcess on ONE process. No /T: a tree kill would prove nothing."""
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=30,
                   creationflags=NO_WINDOW)


# --------------------------------------- is a process this suite started gone?
#
# NEVER BY ITS PID ALONE (GUIDE rule 9). Windows hands a pid on as soon as the
# last handle to its process closes - measured on this machine, a freed pid
# came back after 30 to 79 launches - and a row that asked the operating
# system about a stopped server's pid found it held by a churned child,
# another copy's own process query, a linker. A process is known by what only
# its starter put on its command line - the load's id - READ THROUGH A HANDLE
# to it: while the handle is open its pid cannot be handed on, so what is read
# and what is waited on are one process.

PROCESS_QUERY_LIMITED_INFORMATION, SYNCHRONIZE = 0x1000, 0x00100000
ERROR_ACCESS_DENIED, ERROR_INVALID_PARAMETER = 5, 87
#: how long a question the operating system could not answer is asked again
ASK_S = 10.0
_WIN: list = []


class _UnicodeString(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort),
                ("Buffer", ctypes.c_void_p)]


def _win():
    """kernel32 and ntdll, their prototypes declared once: a HANDLE returned as
    ctypes' default int would be cut to 32 bits."""
    if not _WIN:
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        nt = ctypes.WinDLL("ntdll")
        nt.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                 wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
        nt.NtQueryInformationProcess.restype = ctypes.c_long
        _WIN.extend((k, nt))
    return _WIN[0], _WIN[1]


def open_process(pid):
    """-> (a handle to the process of that pid, None), or (None, why not)."""
    k, _ = _win()
    handle = k.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    return (handle, None) if handle else (None, ctypes.get_last_error())


def close_process(handle) -> None:
    _win()[0].CloseHandle(handle)


def exited(handle, within_s: float = 0.0) -> bool:
    """Has the process ended - now, or within `within_s`? Its handle is
    signalled once its last thread has gone, whatever Popen says."""
    return _win()[0].WaitForSingleObject(handle, int(max(0.0, within_s) * 1000)) == 0


def command_line_of(handle):
    """The command line of the process the handle holds - that process's and
    no other's - or None when it cannot be read."""
    nt = _win()[1]
    size = ctypes.c_ulong(0)
    nt.NtQueryInformationProcess(handle, 60, None, 0, ctypes.byref(size))  # its command line
    if not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if nt.NtQueryInformationProcess(handle, 60, buf, size.value, ctypes.byref(size)) != 0:
        return None
    text = _UnicodeString.from_buffer(buf)
    return ctypes.wstring_at(text.Buffer, text.Length // 2) if text.Buffer else ""


def gone(pid, load, within_s: float = 0.0) -> bool:
    """Is the process this suite started as pid `pid`, with `load` on its
    command line, gone - now, or within `within_s` seconds?

      * nothing has that pid, or its process has ended: gone;
      * the pid is held by a process whose command line does not carry the
        load - handed on to another program - or by one this suite could not
        even open, which is no process it started: gone, since the one asked
        about cannot run while its pid is somebody else's;
      * it carries the load: a wait on its handle, bounded by `within_s`;
      * what the operating system could not answer is asked again, for up to
        ASK_S seconds - and then said, never answered "not gone" in its place.
        A live process whose command line stays unreadable that long is none
        of this suite's: its own are ordinary processes of this user."""
    if not load:
        raise ValueError("a process is asked about by its load id, never by its pid alone")
    if sys.platform != "win32":
        return _gone_posix(pid, load, within_s)
    asked_until, said = time.monotonic() + ASK_S, "nothing"
    while True:
        handle, error = open_process(pid)
        if handle is None:
            if error in (ERROR_INVALID_PARAMETER, ERROR_ACCESS_DENIED):
                return True
            said = f"opening it failed with error {error}"
        else:
            try:
                if exited(handle):
                    return True
                line = command_line_of(handle)
                if line is not None:
                    return load not in line or exited(handle, within_s)
                if time.monotonic() >= asked_until:
                    return True                             # unreadable this long: not ours
            finally:
                close_process(handle)
        if time.monotonic() >= asked_until:
            raise AssertionError(f"could not tell whether pid {pid} ({load}) is gone: {said}")
        time.sleep(0.05)


def end_carrying(pid, mark: str) -> bool:
    """End the process of `pid` if its command line carries `mark` - read
    through a handle held across the kill, so the pid cannot be handed on
    between the proof and the kill; never a pid alone. -> whether it was
    ended. A command line the system could not read is asked again for ASK_S
    seconds, as `gone` asks - unreadable that long, it is none of this
    suite's. Windows: the rows that end processes this way are the job-object
    rows."""
    handle, _ = open_process(pid)
    if handle is None:
        return False                    # gone, or not a process this suite may open
    try:
        asked_until = time.monotonic() + ASK_S
        while not exited(handle):
            line = command_line_of(handle)
            if line is not None:
                if mark.lower() not in line.lower():
                    return False
                taskkill(pid)
                if not exited(handle, KILL_WAIT_S):
                    raise AssertionError(f"pid {pid} ({mark}) was killed and had not ended "
                                         f"{KILL_WAIT_S:g}s later")
                return True
            if time.monotonic() >= asked_until:
                return False
            time.sleep(0.05)
        return False
    finally:
        close_process(handle)


def end_left(left) -> list:
    """End each (pid, load) the last row found still running - on its proof,
    read through a handle held across the end (`end_carrying`), never by a
    pid alone: between the row's look (`gone`, whose handle is closed by
    then) and the end, a stand-in may end and its pid be handed on (H6's
    register 4). -> the pids ended."""
    return [pid for pid, load in left if end_carrying(pid, load)]


def _gone_posix(pid, load, within_s: float) -> bool:
    """The same question where a process has no handle to hold: its command
    line is read again until it answers, within the same bounds."""
    until, asked_until = time.monotonic() + within_s, time.monotonic() + ASK_S
    while True:
        info = M.process_info(pid)
        line = info.get("command_line")
        if info.get("alive") is False or (line is not None and load not in line):
            return True
        if line is not None and time.monotonic() >= until:
            return False
        if line is None and time.monotonic() >= asked_until:
            if info.get("alive") is None:
                raise AssertionError(f"could not tell whether pid {pid} ({load}) is gone")
            return True
        time.sleep(0.05)


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="llmb-llama-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.models = self.tmp / "models"
        self.models.mkdir()
        build_gguf(self.models, name="model.gguf", tensor_elems=(1024 * 1024,),
                   bits_per_weight=5.5)
        self.logs = self.tmp / "logs"
        self.record = self.tmp / "record.jsonl"

    def spec(self, *fake, key="fake", file="model.gguf", **kw) -> ModelSpec:
        return ModelSpec(key=key, label=key, file=file,
                         extra_args=["--fake-record", str(self.record), *fake], **kw)

    def cfg(self, **over) -> dict:
        return {"llama_server": LAUNCHER, "startup_timeout_s": 20.0, "health_poll_s": 0.05,
                "shutdown_timeout_s": 10.0, "flash_attn": "on", "request_timeout_s": 30.0,
                **over}

    def backend(self, *fake, start=True, cfg=None, log_dir="logs", **kw) -> LlamaCppBackend:
        b = LlamaCppBackend(self.spec(*fake, **kw), self.models, cfg or self.cfg(),
                            (self.tmp / log_dir) if log_dir else None, quiet_port())
        b.load_id = ids.mint_actor("load")
        self.addCleanup(self.stop_quietly, b)
        if start:
            b.start()
        return b

    @staticmethod
    def stop_quietly(b):
        try:
            b.stop()
        except Exception:                                       # noqa: BLE001
            b._hard_stop()

    def refused(self, fn, *a, **k) -> str:
        with self.assertRaises(BackendError) as caught:
            fn(*a, **k)
        return str(caught.exception)

    def requests(self) -> list:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def log_text(self, b) -> str:
        return b.log_path.read_text(encoding="utf-8", errors="replace")

    def new_pids(self, before: int) -> list:
        return launches_since(before)

    def gone(self, pid, load=None, within_s: float = 0.0) -> bool:
        return gone(pid, load, within_s)


# ====================================================== the command line ===

class CommandCase(Case):
    def cmd(self, **spec_kw) -> LlamaCppBackend:
        b = LlamaCppBackend(ModelSpec(key="m", file="model.gguf", **spec_kw), self.models,
                            self.cfg(), self.logs, 50123)
        b.load_id = "load-k3x9m2pq"
        return b


class CommandLineRows(CommandCase):
    """Carried over: the llama-server command line."""

    def test_flash_attention_always_gets_an_explicit_value(self):
        cmd = self.cmd().build_command()
        self.assertEqual(cmd[cmd.index("-fa") + 1], "on")

    def test_jinja_is_never_disabled(self):
        self.assertNotIn("--no-jinja", self.cmd().build_command())

    def test_reasoning_is_left_to_the_template_unless_a_model_says_otherwise(self):
        cmd = self.cmd(cache_type_k="q8_0", cache_type_v="q8_0").build_command()
        self.assertNotIn("--reasoning", cmd)
        self.assertEqual((cmd[cmd.index("-ctk") + 1], cmd[cmd.index("-ctv") + 1]), ("q8_0", "q8_0"))
        off = self.cmd(disable_thinking=True).build_command()
        self.assertEqual(off[off.index("--reasoning") + 1], "off")

    def test_the_reduced_command_drops_only_the_optional_flags(self):
        """CHANGED SHAPE, twice: the load's `--alias` stays (it is its
        identity), and so do the settings' own extra arguments - a build that
        refuses one of those says so, instead of running without it."""
        reduced = self.cmd(extra_args=["--threads", "8"]).build_command(drop_optional=True)
        for flag in OPTIONAL_FLAG_NAMES:
            self.assertNotIn(flag, reduced)
        for flag in ("-m", "--host", "--port", "-ngl", "-c", "--alias", "--threads"):
            self.assertIn(flag, reduced)


class NativeCommandLineRows(CommandCase):
    def test_the_load_is_named_on_the_command_line_and_carries_its_key(self):
        cmd = self.cmd().build_command(key="k-123")
        self.assertEqual(cmd[cmd.index("--alias") + 1], "load-k3x9m2pq")
        self.assertEqual(cmd[cmd.index("--api-key") + 1], "k-123")
        self.assertNotIn("--api-key", self.cmd().build_command())

    def test_the_port_is_the_one_the_load_was_given(self):
        cmd = self.cmd().build_command()
        self.assertEqual(cmd[cmd.index("--port") + 1], "50123")

    def test_llama_server_may_be_an_interpreter_and_a_script(self):
        self.assertEqual(self.cmd().build_command()[:2], LAUNCHER)
        b = LlamaCppBackend(ModelSpec(key="m", file="x.gguf"), self.models,
                            {"llama_server": "C:/llama/llama-server.exe"}, self.logs, 50123)
        b.load_id = "load-k3x9m2pq"
        self.assertEqual(b.build_command()[0], "C:/llama/llama-server.exe")


# ============================================================ the GGUF file ===

class GgufRows(unittest.TestCase):
    """Carried over: reading the GGUF header."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="llmb-gguf-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_reads_the_fields_that_matter(self):
        info = read_header(build_gguf(self.tmp))
        self.assertEqual((info["architecture"], info["block_count"], info["head_count_kv"],
                          info["key_length"], info["context_length"]),
                         ("llama", 40, 8, 128, 131072))
        self.assertTrue(info["has_chat_template"])
        self.assertEqual(info["parameters"], 4096 * 4096)

    def test_a_huge_token_array_is_counted_not_loaded(self):
        self.assertEqual(read_header(build_gguf(self.tmp))["vocab_size"], 200)

    def test_kv_cache_arithmetic_matches_the_formula(self):
        info = read_header(build_gguf(self.tmp))
        self.assertEqual(kv_cache_bytes(info, 16384, 2.0), 2 * 40 * 8 * 128 * 16384 * 2)
        self.assertAlmostEqual(kv_cache_bytes(info, 16384, 2.0) / 2 ** 30, 2.5)

    def test_a_small_window_grows_until_the_header_fits(self):
        """TIGHTENED - the carried row could not fail. The reader never reads
        less than 4 KiB, and the old test header was smaller than that, so the
        window never had to grow: with the growing taken out, the row stayed
        green (found by the sweep). A header of 3,000 tokens is ~40 KiB."""
        path = build_gguf(self.tmp, tensor_elems=(1024 * 1024,), tokens=3000)
        # no padding: the file IS the header, so its size is the header's
        self.assertGreater(path.stat().st_size, 2 * 4096,
                           "the header must outgrow the first window")
        info = read_header(path, initial_window=64)
        self.assertEqual((info["block_count"], info["vocab_size"]), (40, 3000))
        self.assertEqual(info, read_header(path))

    def test_a_non_gguf_is_rejected(self):
        """Tightened: rejected BY ITS MAGIC BYTES. The version check behind it
        refuses the zeros too, so a row that only expected an error could not
        tell whether the magic was read at all."""
        path = self.tmp / "x.gguf"
        path.write_bytes(b"NOPE" + b"\0" * 200)
        with self.assertRaises(GGUFError) as caught:
            read_header(path)
        self.assertIn("magic", str(caught.exception))

    def test_a_truncated_header_is_rejected(self):
        path = build_gguf(self.tmp)
        path.write_bytes(path.read_bytes()[: len(path.read_bytes()) // 2])
        with self.assertRaises(GGUFError):
            read_header(path)

    def test_it_complains_when_the_configured_context_exceeds_the_model(self):
        problems = check_against_spec(read_header(build_gguf(self.tmp)), ModelSpec("m", ctx=262144))
        self.assertTrue(any("ctx" in p for p in problems), problems)

    def test_it_complains_when_no_chat_template_is_embedded(self):
        problems = check_against_spec(read_header(build_gguf(self.tmp, template=False)),
                                      ModelSpec("m", ctx=16384))
        self.assertTrue(any("chat template" in p for p in problems), problems)

    def test_a_spec_within_the_model_passes_the_checker(self):
        """By meaning: the existing suite ran its own shipped models through
        the checker; here, an ordinary spec on a plausible file says nothing."""
        info = read_header(build_gguf(self.tmp, tensor_elems=(1024 * 1024,), bits_per_weight=5.5))
        self.assertAlmostEqual(info["bits_per_weight"], 5.5, places=1)
        self.assertEqual(check_against_spec(info, ModelSpec("m", ctx=16384)), [])

    def test_an_impossible_bits_per_weight_is_flagged_as_truncation(self):
        info = read_header(build_gguf(self.tmp, tensor_elems=(4096 * 4096,)))
        self.assertTrue(any("truncated" in p for p in check_against_spec(info, ModelSpec("m"))))


# ================================================================ launching ===

class NativeLaunchRows(Case):
    def test_a_load_waits_out_the_loading_and_then_is_alive(self):
        """Two seconds of loading: long enough that the stand-in is listening
        well inside it, so the 503s are certain, not a race with its start."""
        b = self.backend("--fake-load-s", "2")
        self.assertTrue(b.is_alive())
        log = self.log_text(b)
        self.assertIn("request: GET /health 503", log, "it never waited through a 503")
        self.assertLess(log.index("GET /health 503"), log.index("GET /health 200"))

    def test_it_listens_on_the_port_the_load_was_given(self):
        b = self.backend()
        self.assertTrue(port_in_use("127.0.0.1", b.port))
        self.assertIn(f"--port {b.port}", " ".join(b.command))

    def test_a_missing_model_file_is_refused_before_anything_starts(self):
        before = len(SPAWNS)
        msg = self.refused(self.backend, file="nope.gguf")
        self.assertIn("model file not found", msg)
        self.assertEqual(self.new_pids(before), [])

    def test_a_file_that_is_not_a_gguf_is_refused_before_anything_starts(self):
        (self.models / "junk.gguf").write_bytes(b"NOPE" + b"\0" * 64)
        before = len(SPAWNS)
        self.assertIn("not a usable GGUF", self.refused(self.backend, file="junk.gguf"))
        self.assertEqual(self.new_pids(before), [])

    def test_a_load_with_nothing_bound_is_refused_before_anything_starts(self):
        before = len(SPAWNS)
        self.assertIn("bound folder", self.refused(self.backend, log_dir=None))
        self.assertEqual(self.new_pids(before), [])

    def test_a_port_already_in_use_is_refused(self):
        with socket.socket() as squatter:
            squatter.bind(("127.0.0.1", QUIET.take()))           # of this copy's block
            squatter.listen()
            b = self.backend(start=False)
            b.port = squatter.getsockname()[1]
            before = len(SPAWNS)
            self.assertIn("already in use", self.refused(b.start))
            self.assertEqual(self.new_pids(before), [])

    def test_a_refused_flag_is_dropped_once_and_the_load_goes_on(self):
        b = self.backend("--fake-reject", "-fa")
        self.assertTrue(b.is_alive())
        self.assertEqual(b.dropped_flags, ["-fa", "-ctk", "-ctv"])
        # "-fa" is still there ONCE: as the value of the stand-in's own switch
        self.assertEqual(b.command.count("-fa"), 1)
        self.assertEqual(b.command[b.command.index("--fake-reject") + 1], "-fa")
        self.assertNotIn("-ctk", b.command)
        self.assertTrue(any("refused an argument" in n for n in b.notes), b.notes)
        self.assertEqual(self.log_text(b).count("launch attempt"), 2)

    def test_an_argument_of_the_settings_refused_twice_fails_with_the_log(self):
        """The settings' own extra arguments are not dropped on the retry, so
        a build that refuses one fails SAYING WHICH - it never runs without."""
        before = len(SPAWNS)
        msg = self.refused(self.backend, "--threads", "8")
        self.assertIn("invalid argument: --threads", msg)
        started = launched_since(before)
        self.assertEqual(len(started), 2, "one launch and one retry")
        for pid, load in started:
            self.assertTrue(self.gone(pid, load))

    def test_a_server_that_exits_before_it_is_healthy_fails_with_its_code(self):
        msg = self.refused(self.backend, "--fake-exit-code", "3")
        self.assertIn("exited with code 3", msg)
        self.assertIn("exiting with 3 as asked", msg, "the log tail travels with the error")

    def test_a_server_that_never_gets_healthy_is_stopped_at_the_limit(self):
        before = len(SPAWNS)
        t0 = time.monotonic()
        msg = self.refused(self.backend, "--fake-load-s", "60", cfg=self.cfg(startup_timeout_s=0.6))
        self.assertIn("did not become healthy within 0.6s", msg)
        self.assertLess(time.monotonic() - t0, 10)
        ((pid, load),) = launched_since(before)
        self.assertTrue(self.gone(pid, load), "a server that never came up was left running")

    def test_a_health_answer_that_is_not_loading_fails_at_once(self):
        t0 = time.monotonic()
        self.assertIn("/health answered HTTP 500",
                      self.refused(self.backend, "--fake-health-status", "500"))
        self.assertLess(time.monotonic() - t0, 10, "it waited out the whole limit")

    def test_the_log_holds_the_launch_and_never_the_key(self):
        b = self.backend()
        text = self.log_text(b)
        self.assertIn(b.load_id, text)
        self.assertIn("--api-key ***", text)
        self.assertNotIn(b._key, text)
        self.assertNotIn(b._key, " ".join(b.command))

    def test_a_problem_with_the_gguf_is_noted_and_does_not_stop_the_load(self):
        build_gguf(self.models, name="bare.gguf", template=False)
        b = self.backend(file="bare.gguf")
        self.assertTrue(b.is_alive())
        self.assertTrue(any("no chat template" in n for n in b.notes), b.notes)

    @unittest.skipUnless(SUPPORTED, "job objects are a Windows mechanism")
    def test_the_server_is_in_a_kill_on_close_job(self):
        self.assertTrue(self.backend().job_kill_on_close)

    def test_a_failed_launch_leaves_nothing_running_even_with_no_job(self):
        """Found by the sweep: with the kill taken out of the hard stop, only
        the JOB was killing a failed launch's server - letting the job go
        kills its members. The job is the second line; the hard stop must not
        lean on it. On POSIX there is no job at all, and here one can fail to
        be built. So: no job, a launch that fails, and nothing left running."""
        class Broken:
            def __init__(self):
                raise L.JobObjectError("CreateJobObjectW failed, GetLastError=5 - "
                                       "INJECTED by this row, on purpose")
        real = L.KillOnCloseJob
        L.KillOnCloseJob = Broken
        self.addCleanup(setattr, L, "KillOnCloseJob", real)
        before = len(SPAWNS)
        self.refused(self.backend, "--fake-load-s", "60", cfg=self.cfg(startup_timeout_s=0.6))
        ((pid, load),) = launched_since(before)
        self.assertTrue(self.gone(pid, load), "a failed launch left its server running")

    def test_a_job_that_cannot_be_built_is_said_and_the_load_goes_on(self):
        """Refusing a model because a second line of defence could not be
        built would trade a stranded card on an abnormal exit for no model on
        a normal one. It is said instead."""
        class Broken:
            def __init__(self):
                raise L.JobObjectError("CreateJobObjectW failed, GetLastError=5 - "
                                       "INJECTED by this row, on purpose")
        real = L.KillOnCloseJob
        L.KillOnCloseJob = Broken
        self.addCleanup(setattr, L, "KillOnCloseJob", real)
        b = self.backend()
        self.assertTrue(b.is_alive())
        self.assertFalse(b.job_kill_on_close)
        if SUPPORTED:
            self.assertTrue(any("could NOT put llama-server" in n for n in b.notes), b.notes)


# ================================================================ identity ===
    def test_a_load_given_up_ends_its_server_at_once_and_says_why(self):
        """Found on the real card: nothing could end a load under way. Given up,
        the process goes AT ONCE - the card is let go at once - and `start`
        fails in the words it was given up with, never retried as a build that
        refused a flag."""
        b = self.backend("--fake-load-s", "60", start=False)
        box = {}

        def start():
            try:
                b.start()
                box["got"] = "started"
            except Exception as exc:                        # noqa: BLE001
                box["got"] = exc
        t = threading.Thread(target=start, daemon=True)
        t.start()
        deadline = time.monotonic() + 10
        while b.proc is None:
            self.assertLess(time.monotonic(), deadline, "the server never started")
            time.sleep(0.01)
        proc = b.proc
        # CHANGED ON PURPOSE (H6, H4's R2): "at once" is the kill, asked the
        # moment the load is given up - timed where it is asked. How long the
        # operating system then takes to end the process is its own (a killed
        # process had not ended 15 s later on a busy machine): waited for
        # within the suite's kill bound, never one second
        kills = []
        real_kill = proc.kill

        def kill():
            kills.append(time.monotonic())
            real_kill()
        proc.kill = kill
        time.sleep(0.3)                                     # inside the health wait
        asked = time.monotonic()
        b.abandon("the card was taken back (reclaimed)")
        self.assertTrue(kills and kills[0] - asked < 1.0,
                        f"the process was not killed at once: {kills} after {asked}")
        proc.wait(timeout=KILL_WAIT_S)                      # gone, or TimeoutExpired
        t.join(KILL_WAIT_S)
        self.assertFalse(t.is_alive(), "start went on after the load was given up")
        self.assertIsInstance(box["got"], BackendError)
        self.assertIn("the card was taken back (reclaimed)", str(box["got"]))
        self.assertNotIn("log tail", str(box["got"]),
                         "a load given up was reported as a server that failed")
        self.assertEqual(self.log_text(b).count("launch attempt"), 1,
                         "a load given up was launched again")

    def test_a_give_up_ends_its_start_within_the_rule_every_engine_is_held_to(self):
        """S7 R3, made a rule (PLAN-4 S8): once a load is given up, its start
        ends within `base.GIVE_UP_S` - well inside the time a pipeline's close
        waits for a load under way - whatever the engine. llama.cpp's here;
        whisper's in its own suite, the stand-in model's in the runner's."""
        from runner import base
        b = self.backend("--fake-load-s", "60", start=False)
        box = {}

        def start():
            try:
                b.start()
            except Exception as exc:                        # noqa: BLE001
                box["got"] = exc
            box["ended"] = time.monotonic()
        t = threading.Thread(target=start, daemon=True)
        t.start()
        deadline = time.monotonic() + 10
        while b.proc is None:
            self.assertLess(time.monotonic(), deadline, "the server never started")
            time.sleep(0.01)
        time.sleep(0.3)                                     # inside the health wait
        asked = time.monotonic()
        b.abandon("the pipeline is closing")
        t.join(base.GIVE_UP_S + 10.0)
        self.assertFalse(t.is_alive(), "start went on after the load was given up")
        self.assertLess(box["ended"] - asked, base.GIVE_UP_S)
        self.assertIn("given up", str(box["got"]))

    def test_a_load_given_up_while_its_process_is_being_started_ends_at_once(self):
        """The give-up arrives while the process is being started, before
        there is a process to end: it is ended the moment it exists, and the
        start does not wait the load out."""
        # CHANGED ON PURPOSE (H6, H4's R2): the load lasts ten minutes, and
        # "at once" is the kill, timed where it is asked; the start's return
        # then waits for the operating system to end the process, within the
        # suite's kill bound - never three seconds of it
        b = self.backend("--fake-load-s", "600", start=False)
        real = L.SPAWN
        kills = []

        def spawn_given_up(*a, **k):
            b.abandon("the server is stopping")             # heard mid-start
            proc = real(*a, **k)
            real_kill = proc.kill

            def kill():
                kills.append(time.monotonic())
                real_kill()
            proc.kill = kill
            return proc
        L.SPAWN = spawn_given_up
        try:
            started = time.monotonic()
            said = self.refused(b.start)
        finally:
            L.SPAWN = real
        self.assertTrue(kills and kills[0] - started < 3.0,
                        f"the process was not killed at once: {kills} after {started}")
        self.assertLess(time.monotonic() - started, KILL_WAIT_S, "the start waited the load out")
        self.assertIn("the server is stopping", said)

    def test_a_load_given_up_before_it_began_starts_nothing(self):
        b = self.backend(start=False)
        b.abandon("the server is stopping")
        before = len(SPAWNS)
        self.assertIn("the server is stopping", self.refused(b.start))
        self.assertEqual(self.new_pids(before), [], "a load given up was started anyway")

    def test_giving_up_a_load_already_up_does_not_touch_it(self):
        """Giving up is for a load under way; a model that is up comes off only
        through `stop`."""
        b = self.backend()
        b.abandon("the server is stopping")
        time.sleep(0.3)
        self.assertTrue(b.is_alive(), "a load already up was killed by a give-up")


class NativeIdentityRows(Case):
    """D3: a load has an identity, and a port answered by anything else is
    refused - at the start, and at every request."""

    def test_a_server_that_answers_as_another_load_is_refused_and_stopped(self):
        before = len(SPAWNS)
        msg = self.refused(self.backend, "--fake-alias", "load-aaaaaaaa")
        self.assertIn("answers as load-aaaaaaaa", msg)
        ((pid, load),) = launched_since(before)
        self.assertTrue(self.gone(pid, load))

    def test_the_wrong_port_is_refused_401_and_never_answered_by_the_wrong_model(self):
        a, b = self.backend(), self.backend()
        own = a.port
        a.port = b.port                                     # a runner pointed at the wrong server
        try:
            msg = self.refused(lambda: list(a.stream("whose model are you?", GenParams())))
        finally:
            a.port = own
        self.assertIn("401", msg)
        self.assertIn(a.load_id, msg)
        self.assertEqual([r for r in self.requests() if r["event"] == "request"], [],
                         "the other load answered a request that was not its own")

    def test_every_request_names_the_load_it_is_for(self):
        b = self.backend()
        list(b.stream("hello", GenParams()))
        (req,) = [r for r in self.requests() if r["event"] == "request"]
        self.assertEqual(req["body"]["model"], b.load_id)


# ================================================================== stream ===

class NativeStreamRows(Case):
    def test_tokens_stream_and_the_end_reports_usage(self):
        events = list(self.backend().stream("Tell me about rain.", GenParams(max_tokens=500)))
        tokens = [e for e in events if e["type"] == "token"]
        self.assertGreater(len(tokens), 5)
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["finish_reason"], "stop")
        self.assertEqual(events[-1]["usage"]["completion_tokens"], len(tokens))

    def test_the_cap_ends_with_length(self):
        events = list(self.backend().stream("Tell me about rain.", GenParams(max_tokens=3)))
        self.assertEqual(sum(e["type"] == "token" for e in events), 3)
        self.assertEqual(events[-1]["finish_reason"], "length")

    def test_reasoning_streams_in_its_own_field(self):
        events = list(self.backend().stream("[[THINK]] go", GenParams(max_tokens=500)))
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds[0], "reasoning")
        self.assertLess(kinds.index("reasoning"), kinds.index("token"))

    def test_the_exact_call_is_kept_and_it_never_holds_the_key(self):
        b = self.backend()
        list(b.stream("hello", GenParams(temperature=0.3, seed=9, max_tokens=40)))
        (req,) = [r for r in self.requests() if r["event"] == "request"]
        self.assertEqual(b.last_call, req["body"])
        self.assertNotIn(b._key, json.dumps(b.last_call))
        self.assertEqual(b.last_call["messages"], [{"role": "user", "content": "hello"}])

    def test_a_model_told_not_to_think_says_so_in_every_request(self):
        b = self.backend(disable_thinking=True)
        list(b.stream("hello", GenParams()))
        (req,) = [r for r in self.requests() if r["event"] == "request"]
        self.assertIs(req["body"]["chat_template_kwargs"]["enable_thinking"], False)

    def test_a_cancel_mid_stream_closes_the_socket(self):
        b = self.backend()
        cancel, got = threading.Event(), []
        with self.assertRaises(Cancelled):
            for ev in b.stream("Tell me about rain.", GenParams(max_tokens=500), cancel):
                got.append(ev)
                if len(got) == 3:
                    cancel.set()
        self.assertEqual(len(got), 3)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any(r["event"] == "client_gone"
                                                      for r in self.requests()):
            time.sleep(0.05)
        self.assertTrue(any(r["event"] == "client_gone" for r in self.requests()),
                        "the server never saw the client go - the slot was not aborted")

    def test_a_cancel_before_the_first_token_is_honoured_at_once(self):
        """CHANGED: the existing backend looked at the cancel only between
        tokens, and a server still reading a long prompt sends none."""
        b = self.backend("--fake-first-token-s", "8")
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        t0 = time.monotonic()
        with self.assertRaises(Cancelled):
            list(b.stream("a long prompt", GenParams(), cancel))
        self.assertLess(time.monotonic() - t0, 3.0, "it waited for the first token")

    def test_a_stream_closed_cleanly_before_the_finish_is_a_failure_too(self):
        """The other way a server goes away: a clean close, no reset - the read
        just ends. Only the missing finish can tell it from a whole answer."""
        b = self.backend("--fake-cut-after", "3")
        got = []
        with self.assertRaises(BackendError) as caught:
            for ev in b.stream("Tell me about rain.", GenParams(max_tokens=500)):
                got.append(ev)
        self.assertIn("ended before the server said it was done", str(caught.exception))
        self.assertEqual([e["type"] for e in got], ["token"] * 3)

    def test_a_server_that_goes_away_mid_answer_is_a_failure_not_a_finish(self):
        """CHANGED: the existing backend reported a cut-off stream as a
        normal stop - half an answer, handed on as a whole one."""
        b = self.backend()
        got = []
        with self.assertRaises(BackendError) as caught:
            for ev in b.stream("Tell me about rain.", GenParams(max_tokens=500)):
                got.append(ev)
                if len(got) == 2:
                    killed(b.proc)
        self.assertIn("went away", str(caught.exception))
        self.assertNotIn("done", [e["type"] for e in got])

    def test_a_request_longer_than_the_context_is_refused_in_words_not_json(self):
        """llama-server refuses a request longer than its context with a JSON
        error (measured on the real card). The job reads a sentence - how long
        the request was, what the context is, what to change - and
        llama-server's own message, never its JSON.

        CHANGED (S10b): the request is 204 tokens, not 200 - the stand-in now
        counts it as llama-server does, its chat template's own tokens too
        (`stand_in_tokens`) - and the refusal is a PromptTooLong, which ends
        its job `prompt_too_long`."""
        b = self.backend(ctx=64)
        with self.assertRaises(PromptTooLong) as caught:
            list(b.stream(" ".join(["word"] * 200), GenParams()))
        said = str(caught.exception)
        self.assertEqual(stand_in_tokens([{"role": "user", "content": "word " * 200}]), 204)
        self.assertIn("the request is 204 tokens and fake's context is 64", said)
        self.assertIn("raise [models.fake] ctx", said)
        self.assertIn("exceeds the available context size (64 tokens)", said)
        self.assertNotIn("{", said)

    def test_any_other_refusal_says_llama_servers_own_message(self):
        self.assertEqual(L.refusal(500, '{"error": {"code": 500, "message": "no slot"}}', "m"),
                         "llama-server refused the request (HTTP 500): no slot")
        # the same fields on another kind of refusal say nothing about a context
        other = '{"error": {"message": "x", "type": "server_error", "n_prompt_tokens": 9, ' \
                '"n_ctx": 4}}'
        self.assertEqual(L.refusal(400, other, "m"), "llama-server refused the request "
                                                     "(HTTP 400): x")
        # a body that is not its error JSON is all there is to say
        self.assertEqual(L.refusal(502, "<html>bad gateway</html>", "m"),
                         "HTTP 502 from llama-server: <html>bad gateway</html>")


#: a line of llama-server's log that says the card's memory ran out - b10724's
#: own words (ggml-cuda.cu, ggml_backend_cuda_buffer_type_alloc_buffer)
OOM = ("ggml_backend_cuda_buffer_type_alloc_buffer: allocating 2304.00 MiB on device 0: "
       "cudaMalloc failed: out of memory")


class NativeMemoryErrorRows(Case):
    """PLAN-4 D3: the runner reads the log it keeps for each load for the
    lines that say the card's memory ran out - llama.cpp b10724's own
    (runner/memlog.py names them, and where each is written) - and says each
    line once, from the load's own launch on."""

    def test_the_lines_looked_for_are_llama_cpps_own_and_no_others(self):
        from runner import memlog
        said = [OOM, "CUDA error: out of memory",
                "ggml_gallocr_reserve_n_impl: failed to allocate CUDA0 buffer of size 2415919104"]
        not_said = ["ggml_gallocr_reserve_n_impl: failed to allocate CUDA_Host buffer of size 1024",
                    "alloc_tensor_range: failed to allocate CPU buffer of size 1024",
                    "decode: failed to find a memory slot for batch of size 512",
                    "graph_reserve: failed to allocate compute buffers",
                    "CUDA error: an illegal memory access was encountered",
                    "srv  log_server_r: request: POST /v1/chat/completions 127.0.0.1 200",
                    "load_tensors: offloaded 41/41 layers to GPU"]
        self.assertEqual([x for x in said + not_said if memlog.says_out_of_memory(x)], said)

    def test_a_loads_own_log_is_read_from_its_launch_each_whole_line_once(self):
        self.logs.mkdir()
        log = self.logs / "llama-server-fake.log"
        log.write_bytes(b"CUDA error: out of memory\n")          # an earlier load's line
        b = self.backend()
        self.assertEqual(b.memory_errors(), [], "an earlier load's line is not this load's")
        with open(log, "ab") as fh:
            fh.write(b"srv  update_slots: all slots are idle\n" + OOM.encode("utf-8")
                     + b"\nCUDA error: out of mem")
        self.assertEqual(b.memory_errors(), [OOM], "a line still being written waits")
        with open(log, "ab") as fh:
            fh.write(b"ory\n")
        self.assertEqual(b.memory_errors(), ["CUDA error: out of memory"])
        self.assertEqual(b.memory_errors(), [], "each line is said once")
        never = self.backend(start=False, log_dir=None)
        self.assertEqual(never.memory_errors(), [], "a load with no log says nothing")

    def test_the_stand_in_runs_out_of_memory_mid_answer_as_ggml_does(self):
        b = self.backend("--fake-oom-after", "2")
        got = []
        with self.assertRaises(BackendError):
            for ev in b.stream("Tell me about rain.", GenParams(max_tokens=500)):
                got.append(ev)
        self.assertEqual([e["type"] for e in got], ["token"] * 2)
        self.assertIn("CUDA error: out of memory", self.log_text(b))
        self.assertEqual(b.memory_errors(), ["CUDA error: out of memory"])


# ====================================================== the text request ===

#: a system message and the turns before this one
TALK = [{"role": "system", "content": "Answer in one sentence."},
        {"role": "user", "content": "What is rain?"},
        {"role": "assistant", "content": "Water that falls from clouds."},
        {"role": "user", "content": "And snow?"}]


def stand_in_tokens(messages, generation_prompt=True, thinking_off=False) -> int:
    """What the stand-in llama-server's template and tokenizer make of a
    conversation, by their documented rule (fake_llama_server.py), counted
    here on its own: its first token (`<s>`); for each message its role's
    marker, its words and `<|end|>`; then the generation prompt's marker -
    and, for a model told not to think, the empty thinking block its template
    writes (`<think>`, `</think>`)."""
    n = 1 + sum(2 + len(str(m["content"]).split()) for m in messages)
    if generation_prompt:
        n += 1 + (2 if thinking_off else 0)
    return n


class NativeTextRequestRows(Case):
    """Every text parameter, and messages with roles, at llama-server's
    door - read off what the stand-in RECEIVED, never off what was meant to
    be sent."""

    def body(self) -> dict:
        (req,) = [r for r in self.requests() if r["event"] == "request"]
        return req["body"]

    def test_a_conversation_goes_as_it_was_built_in_its_order(self):
        list(self.backend().stream(TALK, GenParams(max_tokens=30)))
        self.assertEqual(self.body()["messages"], TALK)

    def test_a_prompt_is_one_user_message_as_it_always_was(self):
        list(self.backend().stream("hello", GenParams(max_tokens=30)))
        self.assertEqual(self.body()["messages"], [{"role": "user", "content": "hello"}])

    def test_every_parameter_a_job_named_is_in_the_body_as_it_was_named(self):
        named = every_parameter()
        list(self.backend().stream("hello", GenParams(extra=named)))
        body = self.body()
        for name, value in named.items():
            with self.subTest(parameter=name):
                self.assertTrue(same(body.get(name), value), f"sent {body.get(name)!r}")

    def test_nothing_is_sent_that_llama_server_does_not_read(self):
        """The reading of llama-server b10724's source (runner/llama_fields.py)
        holds every field its chat route reads: a body with a field outside it
        carries something no model would take."""
        from runner.llama_fields import CHAT_FIELDS
        list(self.backend(disable_thinking=True).stream(TALK, GenParams(extra=every_parameter())))
        self.assertEqual(sorted(set(self.body()) - set(CHAT_FIELDS)), [])

    def test_a_job_that_names_enable_thinking_is_sent_its_own_over_the_models_setting(self):
        """The model's settings apply only where the job names nothing: its
        `disable_thinking` is the default for `enable_thinking`, not a lid."""
        b = self.backend(disable_thinking=True)
        list(b.stream("hello", GenParams(chat_template_kwargs={"enable_thinking": True,
                                                               "effort": "high"})))
        self.assertEqual(self.body()["chat_template_kwargs"],
                         {"enable_thinking": True, "effort": "high"})

    def test_the_stand_in_counts_the_whole_conversation_against_its_context(self):
        """As llama-server does: the request is the whole conversation, so the
        turns before the last count against the context too.

        CHANGED (S10b): 107 tokens, not 101 - the conversation as its chat
        template renders it, the template's own tokens counted too."""
        b = self.backend(ctx=64)
        long_talk = [{"role": "system", "content": " ".join(["word"] * 100)},
                     {"role": "user", "content": "short"}]
        said = self.refused(lambda: list(b.stream(long_talk, GenParams())))
        self.assertEqual(stand_in_tokens(long_talk), 107)
        self.assertIn("the request is 107 tokens and fake's context is 64", said)


class NativeCountRows(Case):
    """THE COUNT: a request counted by its model before it is asked - the
    stand-in's routes as llama-server b10724 answers them (their shapes read
    from its source: /apply-template, /tokenize, /props), and the backend's
    count through them."""

    def ask(self, b, path, doc=None, key=True):
        """One of the stand-in's routes, asked directly: -> (status, answer)."""
        headers = {"Content-Type": "application/json"}
        if key:
            headers = b._headers(**headers)
        req = urllib.request.Request(f"http://127.0.0.1:{b.port}{path}", headers=headers,
                                     method="GET" if doc is None else "POST",
                                     data=None if doc is None else json.dumps(doc).encode())
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    # --------------------------------------------- the stand-in, as b10724

    def test_the_stand_in_renders_a_conversation_with_its_template_as_apply_template_does(self):
        b = self.backend()
        self.assertEqual(self.ask(b, "/apply-template", {"messages": TALK}), (200, {"prompt": (
            "<|system|>\nAnswer in one sentence.<|end|>\n<|user|>\nWhat is rain?<|end|>\n"
            "<|assistant|>\nWater that falls from clouds.<|end|>\n<|user|>\nAnd snow?<|end|>\n"
            "<|assistant|>\n")}))
        _, out = self.ask(b, "/apply-template", {"messages": TALK, "add_generation_prompt": False})
        self.assertTrue(out["prompt"].endswith("And snow?<|end|>\n"), out)
        _, out = self.ask(b, "/apply-template", {"messages": TALK, "chat_template_kwargs":
                                                 {"enable_thinking": False}})
        self.assertTrue(out["prompt"].endswith("<|assistant|>\n<think>\n\n</think>\n\n"), out)
        status, out = self.ask(b, "/apply-template", {"prompt": "hi"})
        self.assertEqual((status, out["error"]["message"], out["error"]["type"]),
                         (400, "'messages' is required", "invalid_request_error"))

    def test_the_stand_in_tokenizes_a_text_as_tokenize_does(self):
        """b10724's defaults: no first token unless `add_special`, special
        tokens parsed unless `parse_special` is false - then one is read as
        the text it is spelled with."""
        b = self.backend()
        text = "<|user|>\nhello world<|end|>"

        def tokens(**kw):
            return self.ask(b, "/tokenize", {"content": text, **kw})[1]["tokens"]
        self.assertEqual(len(tokens()), 4)
        self.assertEqual(len(tokens(add_special=True)), 5)
        self.assertEqual(tokens(add_special=True)[1:], tokens(), "the same text, the same ids")
        self.assertEqual(len(tokens(parse_special=False)), 8)
        pieces = tokens(with_pieces=True)
        self.assertEqual([p["piece"] for p in pieces], ["<|user|>", "hello", "world", "<|end|>"])
        self.assertEqual([p["id"] for p in pieces], tokens())
        self.assertEqual(self.ask(b, "/tokenize", {}), (200, {"tokens": []}))

    def test_the_stand_in_says_the_context_it_answers_in_as_props_does(self):
        """Its slot's context: the `-c` it was started with, unless it is told
        its slot has another (`--fake-n-ctx`), as a model's training context
        or parallel slots make llama-server's."""
        status, out = self.ask(self.backend(ctx=4096), "/props")
        self.assertEqual((status, out["default_generation_settings"]["n_ctx"]), (200, 4096))
        _, out = self.ask(self.backend("--fake-n-ctx", "100", ctx=4096), "/props")
        self.assertEqual(out["default_generation_settings"]["n_ctx"], 100)

    def test_the_counting_routes_need_the_loads_key_as_every_route_but_health_does(self):
        b = self.backend()
        for path, doc in (("/props", None), ("/apply-template", {"messages": TALK}),
                          ("/tokenize", {"content": "hi"})):
            with self.subTest(path=path):
                status, out = self.ask(b, path, doc, key=False)
                self.assertEqual((status, out["error"]["type"]), (401, "authentication_error"))

    def test_the_models_route_needs_the_key_and_says_the_context_as_b10724s_does(self):
        """S10b RG1: b10724 lets only /health through without the key - its
        /v1/models too needs it - and says there, as `meta.n_ctx`, the context
        its slot answers in, the one /props says. The stand-in answered
        /v1/models to anybody, and said no context."""
        b = self.backend("--fake-n-ctx", "100", ctx=4096)
        status, out = self.ask(b, "/v1/models", key=False)
        self.assertEqual((status, out["error"]["type"]), (401, "authentication_error"))
        status, out = self.ask(b, "/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in out["data"]], [b.load_id], "not this load")
        self.assertEqual(out["data"][0]["meta"]["n_ctx"], 100, "not the slot's context")
        self.assertEqual(out["data"][0]["meta"]["n_ctx"],
                         self.ask(b, "/props")[1]["default_generation_settings"]["n_ctx"])

    def test_while_it_loads_every_route_says_so_before_the_key_is_asked(self):
        """S10b RG1: b10724 answers every request 503 "Loading model" while
        its model loads - the loading state is checked before the key. The
        stand-in's chat route checked the key first, and its /v1/models never
        said it was loading."""
        b = self.backend("--fake-load-s", "60", start=False)
        box = {}
        t = threading.Thread(target=lambda: box.update(got=self.suppress(b.start)), daemon=True)
        t.start()
        self.addCleanup(t.join, 5)
        self.addCleanup(b.abandon, "the row is over")
        deadline = time.monotonic() + 10
        while self.loading(b) is None:
            self.assertLess(time.monotonic(), deadline, "the stand-in never answered")
            time.sleep(0.05)
        for path, doc in (("/v1/chat/completions", {"messages": TALK}), ("/v1/models", None),
                          ("/props", None), ("/tokenize", {"content": "hi"})):
            with self.subTest(path=path):
                status, out = self.ask(b, path, doc, key=False)
                self.assertEqual((status, out["error"]["message"], out["error"]["type"]),
                                 (503, "Loading model", "unavailable_error"))

    @staticmethod
    def suppress(fn):
        try:
            return fn()
        except Exception as exc:                            # noqa: BLE001
            return exc

    @staticmethod
    def loading(b):
        """Is the stand-in of *b* up and loading? -> its /health status, or
        None while nothing answers yet."""
        if b.port is None or b.proc is None:
            return None
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{b.port}/health", timeout=2):
                return 200
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code
        except OSError:
            return None

    def test_the_stand_in_refuses_a_prompt_of_its_whole_context_and_ends_an_answer_at_its_end(
            self):
        """b10724: a prompt of the whole context is refused (n_prompt >=
        n_ctx); one position short, it is answered - and the answer stops
        where the context ends, finish "length", whatever its max_tokens."""
        prompt = " ".join(["word"] * 20)
        n = stand_in_tokens([{"role": "user", "content": prompt}])
        said = self.refused(lambda: list(self.backend("--fake-n-ctx", str(n)).stream(
            prompt, GenParams(max_tokens=-1))))
        self.assertIn(f"the request is {n} tokens", said)
        events = list(self.backend("--fake-n-ctx", str(n + 1)).stream(
            prompt, GenParams(max_tokens=-1)))
        self.assertEqual(([e["type"] for e in events], events[-1]["finish_reason"]),
                         (["token", "done"], "length"))

    def test_max_tokens_is_read_as_llama_server_reads_it(self):
        """-1 names no limit, 0 reads the prompt into the cache and writes
        nothing (llama-server's README), and a length cuts the answer there.
        The stand-in read -1 as a limit and 0 as none (wave 2's register)."""
        b = self.backend()

        def answer(most):
            events = list(b.stream("Tell me about rain.", GenParams(max_tokens=most)))
            return ([e["text"] for e in events if e["type"] == "token"],
                    events[-1]["finish_reason"])
        whole, why = answer(-1)
        self.assertEqual((why, len(whole) > 3), ("stop", True))
        self.assertEqual(answer(3), (whole[:3], "length"))
        self.assertEqual(answer(0), ([], "length"))

    # ------------------------------------------------- the backend's count

    def test_the_count_is_the_models_own_count_of_the_request_it_will_be_asked(self):
        """Rendered with the model's own template and counted by its own
        tokenizer, from exactly the body its answer is asked with: the count
        is the number the model reads the request as - its own
        `usage.prompt_tokens` when it answers."""
        b = self.backend()
        params = GenParams(max_tokens=30, extra={"top_k": 20})
        counted = b.count(TALK, params)
        events = list(b.stream(TALK, params))
        self.assertEqual(counted, {"prompt_tokens": stand_in_tokens(TALK), "context": 8192})
        self.assertEqual(events[-1]["usage"]["prompt_tokens"], counted["prompt_tokens"])
        asked = {r["event"]: r["body"] for r in self.requests()
                 if r["event"] in ("apply-template", "request")}
        self.assertEqual(asked["apply-template"], asked["request"],
                         "the count rendered a body its answer was not asked with")

    def test_the_count_renders_the_models_own_defaults_as_its_answer_does(self):
        """A model told not to think has `enable_thinking` false in every
        request that names none, and its template writes an empty thinking
        block for it: the count holds those tokens too."""
        b = self.backend(disable_thinking=True)
        counted = b.count("hello", GenParams(max_tokens=30))
        events = list(b.stream("hello", GenParams(max_tokens=30)))
        want = stand_in_tokens([{"role": "user", "content": "hello"}], thinking_off=True)
        self.assertEqual((counted["prompt_tokens"], events[-1]["usage"]["prompt_tokens"]),
                         (want, want))

    def test_the_context_is_the_models_own_word_never_its_settings(self):
        """llama-server's context for a request is its slot's - capped by the
        model's training context, shared among parallel slots - which is not
        always the `ctx` its settings name: the count asks the model."""
        b = self.backend("--fake-n-ctx", "100", ctx=8192)
        self.assertEqual(b.count("hello", GenParams())["context"], 100)

    def test_a_cancelled_request_is_never_counted(self):
        """A cancel is heard before each of the count's calls: a job
        cancelled as its turn came asks the model nothing."""
        b = self.backend()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            b.count("hello", GenParams(), cancel)
        self.assertEqual([r["event"] for r in self.requests()], [])

    def test_a_request_the_model_will_not_count_is_refused_in_its_words(self):
        """A body the model's template refuses is refused by the count, in
        llama-server's own words: never a count that was skipped."""
        b = self.backend("--fake-template-refuses", "Cannot use both json_schema and grammar")
        said = self.refused(lambda: b.count("hello", GenParams()))
        self.assertEqual(said, "llama-server refused the request (HTTP 400): Cannot use both "
                               "json_schema and grammar")


class NativeRequestFieldRows(unittest.TestCase):
    """runner/llama_fields.py: every field llama-server b10724's chat route
    reads, as its source was read - and the contract's `gen` held to it."""

    #: the reading, as its three sources gave it: the chat layer, the
    #: completion's schema (with the names it also answers to) and the route's
    #: documented options
    SOURCE = {
        "the completion's schema": (
            "verbose", "timings_per_token", "stream", "stream_options", "cache_prompt",
            "return_tokens", "return_progress", "sse_ping_interval", "n_predict", "n_indent",
            "n_keep", "n_discard", "n_cmpl", "n_cache_reuse", "t_max_predict_ms",
            "response_fields", "top_k", "top_p", "min_p", "top_n_sigma", "xtc_probability",
            "xtc_threshold", "typical_p", "temperature", "dynatemp_range", "dynatemp_exponent",
            "repeat_last_n", "repeat_penalty", "frequency_penalty", "presence_penalty",
            "dry_multiplier", "dry_base", "dry_allowed_length", "dry_penalty_last_n",
            "mirostat", "mirostat_tau", "mirostat_eta", "adaptive_target", "adaptive_decay",
            "seed", "n_probs", "min_keep", "backend_sampling", "post_sampling_probs", "lora",
            "dry_sequence_breakers", "json_schema", "grammar_lazy", "chat_format",
            "reasoning_format", "generation_prompt", "parse_tool_calls", "chat_parser",
            "continue_final_message", "echo", "preserved_tokens", "grammar_triggers",
            "reasoning_control", "reasoning_budget_tokens", "reasoning_budget_start_tag",
            "reasoning_budget_end_tags", "reasoning_budget_message", "logit_bias", "ignore_eos",
            "stop", "samplers"),
        "the names it also answers to": (
            "max_completion_tokens", "max_tokens", "n", "logprobs", "grammar",
            "reasoning_budget_end_tag"),
        "the chat layer": (
            "messages", "tools", "tool_choice", "top_logprobs", "chat_template_kwargs",
            "reasoning_effort", "thinking_budget_tokens", "add_generation_prompt",
            "parallel_tool_calls", "response_format", "model"),
        "the route's documented options": ("id_slot",),
    }

    def test_the_reading_is_the_one_its_source_gave(self):
        from runner import llama_fields as LF
        read = [f for fields in self.SOURCE.values() for f in fields]
        self.assertEqual(len(self.SOURCE["the completion's schema"]), 66)
        self.assertEqual(sorted(LF.CHAT_FIELDS), sorted(read))
        self.assertEqual(len(set(LF.CHAT_FIELDS)), len(LF.CHAT_FIELDS), "a field read twice")

    def test_every_field_read_is_offered_or_says_why_it_is_not(self):
        from runner import llama_fields as LF
        kept = [*LF.OFFERED, *LF.OWN, *LF.NOT_YET, *LF.SET_BY_THE_SERVER]
        self.assertEqual(sorted(kept), sorted(LF.CHAT_FIELDS), "a field in two places, or none")
        for why in (*LF.OWN.values(), *LF.NOT_YET.values()):
            self.assertTrue(why.strip(), "a field kept back without a why")

    def test_the_contract_offers_exactly_what_the_reading_offers(self):
        from runner import llama_fields as LF
        import validate as V
        _, routes = V.load()
        gen = routes["server/jobs-submit"]["request"]["object"]["gen"]["object"]
        self.assertEqual(sorted(set(gen) - {"model", "n", "retries"}), sorted(LF.OFFERED))
        self.assertEqual(len(LF.OFFERED), 51)


# ================================================================ liveness ===

class LivenessRows(Case):
    """Carried over: llama.cpp's answer to "alive?" comes from its own
    process. The existing suite read the source for "no request"; here the
    stand-in counts the requests."""

    def test_the_answer_comes_from_the_process_and_asking_sends_nothing(self):
        b = self.backend()
        before = self.log_text(b).count("request:")
        for _ in range(20):
            self.assertTrue(b.is_alive())
        time.sleep(0.2)
        self.assertEqual(self.log_text(b).count("request:"), before)
        killed(b.proc)
        self.assertFalse(b.is_alive(), "a killed server still reads as alive")


# ==================================================================== stop ===

class NativeStopRows(Case):
    def test_stop_returns_only_once_the_process_is_gone_and_the_port_is_free(self):
        b = self.backend()
        pid, port, load = b.pid, b.port, b.load_id
        b.stop()
        self.assertFalse(b.is_alive())
        self.assertFalse(port_in_use("127.0.0.1", port))
        self.assertTrue(self.gone(pid, load))

    def test_stop_waits_for_the_process_itself_not_for_popens_word(self):
        """Measured on the real card: a llama-server killed underneath takes
        about two seconds to exit, and all that time Python's Popen already
        reports its exit code - `terminate` finds it terminating and records
        the code, and `wait` then returns at once. The stop returned while the
        process was still there, and the next load started beside it (the card
        watch saw two). Played here on a process that takes a second and a half
        to go, with Popen's word already given."""
        b = self.backend()
        real = b.proc
        self.addCleanup(killed, real)
        slow, load = self.slow_process()
        b.proc, slow.returncode = slow, 1                   # Popen's word: "it has exited"
        started = time.monotonic()
        b.stop()
        took = time.monotonic() - started
        self.assertGreater(took, 0.8, f"the stop took Popen's word for it ({took:.2f}s)")
        self.assertTrue(self.gone(slow.pid, load),
                        "the stop returned with the process still there")

    def slow_process(self) -> tuple:
        """A process that takes a second and a half to go, carrying a load id
        of its own on its command line - what it is asked about by."""
        load = ids.mint_actor("load")
        slow = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)", load],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        self.addCleanup(killed, slow)
        return slow, load

    def test_a_load_that_ends_badly_waits_for_its_process_to_go_too(self):
        """The same on the road a failed or given-up load takes: a give-up has
        already ended the process, so the end that follows finds it terminating
        - and Popen's word again."""
        b = self.backend()
        real = b.proc
        self.addCleanup(killed, real)
        slow, load = self.slow_process()
        b.proc, slow.returncode = slow, 1
        started = time.monotonic()
        b._hard_stop()
        took = time.monotonic() - started
        self.assertGreater(took, 0.8, f"the end took Popen's word for it ({took:.2f}s)")
        self.assertTrue(self.gone(slow.pid, load), "a failed load's process was left behind")

    @unittest.skipUnless(SUPPORTED, "the wait is on a Windows process handle")
    def test_a_process_that_cannot_even_be_opened_reads_as_gone(self):
        self.assertTrue(L.wait_exited(0, 1.0))              # pid 0 is never a process to open

    def test_stopping_twice_is_harmless(self):
        b = self.backend()
        b.stop()
        b.stop()
        self.assertIsNone(b.proc)

    @unittest.skipUnless(SUPPORTED, "job objects are a Windows mechanism")
    def test_a_stop_lets_the_job_go_after_the_process(self):
        b = self.backend()
        self.assertTrue(b.job_kill_on_close)
        b.stop()
        self.assertFalse(b.job_kill_on_close)
        self.assertIsNone(b._job)


# ============================================ the manager, on llama.cpp ===

class ManagerCase(Case):
    def settings(self, **runner) -> Config:
        rec = self.record.as_posix()
        extra = f'extra_args = ["--fake-record", "{rec}"]'
        # a runner here waits at most 5 s for the card: a lock that is never
        # given back must fail its row, not hold the suite for ten minutes
        lines = ["[runner]", 'backend = "llamacpp"',
                 f'llama_server = ["{Path(LAUNCHER[0]).as_posix()}", "{FAKE.as_posix()}"]',
                 f'models_dir = "{self.models.as_posix()}"', "startup_timeout_s = 20.0",
                 "health_poll_s = 0.05", "shutdown_timeout_s = 10.0", "card_patience_s = 5.0"]
        lines += [f"{k} = {json.dumps(v)}" for k, v in runner.items()]
        lines += ["", "[models.alpha]", 'file = "model.gguf"', extra,
                  "", "[models.beta]", 'file = "model.gguf"', extra,
                  "", "[models.bare]", 'file = "bare.gguf"', extra]
        path = self.tmp / "runner.toml"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return Config.load(path)

    def manager(self, folder="bound", vram=lambda: None, **kw) -> ModelManager:
        m = ModelManager(self.settings(), (self.tmp / folder) if folder else None,
                         vram=vram, **kw)
        self.addCleanup(m.shutdown)
        return m


class NativeManagerRows(ManagerCase):
    def test_a_load_listens_on_a_port_of_its_own_and_is_named_on_the_record(self):
        m = self.manager()
        b = m.acquire("alpha", W)
        events = read_events(m.events_path)
        start = next(e for e in events if e["event"] == "load_start")
        ok = next(e for e in events if e["event"] == "load_ok")
        self.assertEqual((ok["pid"], ok["port"], ok["load"]), (b.pid, b.port, b.load_id))
        self.assertEqual(start["port"], b.port, "the load listens on a port the manager did not choose")
        self.assertTrue(ok["gguf"].endswith("model.gguf"))
        self.assertTrue(port_in_use("127.0.0.1", b.port))

    def test_two_servers_side_by_side_and_room_made_before_the_next_starts(self):
        """CHANGED ON PURPOSE (S7; was "a swap stops the first server before
        the second starts"): a second model goes up BESIDE the first - two
        stand-in llama-servers, each its own process on its own port, and the
        record never two loads of one model. When the next does not fit, the
        first - on demand, answering nothing - is stopped BEFORE the next
        starts: asked by its load's id, never its pid alone (under load its pid
        was found held by another copy's own process query)."""
        m = self.manager()
        first = m.acquire("alpha", W)
        pid, load = first.pid, first.load_id
        second = m.acquire("beta", W)
        self.assertTrue(first.is_alive() and second.is_alive())
        self.assertNotEqual(first.port, second.port, "two servers on one port")
        events = read_events(m.events_path)
        self.assertEqual((max_concurrency(events), M.side_by_side(events)), (1, 2))
        m.unload("beta")

        def room_for_one(key):
            if m._loads.on_card():
                raise BackendError(f"not enough free VRAM for {key}: one model fills the card")
        m._check_vram = room_for_one
        third = m.acquire("beta", W)
        self.assertTrue(self.gone(pid, load))
        self.assertTrue(third.is_alive())
        kinds = [(e["event"], e.get("load")) for e in read_events(m.events_path)]
        self.assertLess(kinds.index(("unload_ok", load)), kinds.index(("load_start",
                                                                       third.load_id)),
                        "the next model started before the first was off")

    def test_a_warm_reuse_starts_nothing(self):
        m = self.manager()
        first = m.acquire("alpha", W)
        before = len(SPAWNS)
        self.assertIs(m.acquire("alpha", W), first)
        self.assertEqual(self.new_pids(before), [])

    def test_a_server_killed_underneath_is_loaded_again(self):
        m = self.manager()
        first = m.acquire("alpha", W)
        killed(first.proc)
        second = m.acquire("alpha", W)
        self.assertIsNot(second, first)
        self.assertTrue(second.is_alive())
        self.assertIn("stale_backend", [e["event"] for e in read_events(m.events_path)])

    def test_a_load_the_card_cannot_hold_is_refused_before_it_is_paid_for(self):
        m = self.manager(vram=lambda: 1024)                 # 1 GiB free
        before = len(SPAWNS)
        with self.assertRaises(BackendError) as caught:
            m.acquire("alpha", W)
        self.assertIn("not enough free VRAM", str(caught.exception))
        self.assertEqual(self.new_pids(before), [], "a refused load was started anyway")
        check = next(e for e in read_events(m.events_path) if e["event"] == "vram_check")
        self.assertGreater(check["need_gb"], check["free_gb"])
        self.assertIsNone(m.status()["lease"]["holder"])

    def test_a_card_with_room_lets_the_load_through(self):
        m = self.manager(vram=lambda: 24 * 1024)
        self.assertTrue(m.acquire("alpha", W).is_alive())
        self.assertIn("vram_check", [e["event"] for e in read_events(m.events_path)])

    def test_a_card_that_cannot_be_asked_never_gates(self):
        m = self.manager(vram=lambda: None)
        self.assertTrue(m.acquire("alpha", W).is_alive())
        self.assertIn("vram_unknown", [e["event"] for e in read_events(m.events_path)])

    def test_a_load_with_nothing_bound_is_refused_and_starts_nothing(self):
        m = self.manager(folder=None)
        before = len(SPAWNS)
        with self.assertRaises(BackendError) as caught:
            m.acquire("alpha", W)
        self.assertIn("bound folder", str(caught.exception))
        self.assertEqual(self.new_pids(before), [])
        self.assertIsNone(m.status()["lease"]["holder"])

    def test_what_the_load_had_to_say_is_on_its_line_in_the_record(self):
        build_gguf(self.models, name="bare.gguf", template=False)
        m = self.manager()
        m.acquire("bare", W)
        ok = next(e for e in read_events(m.events_path) if e["event"] == "load_ok")
        self.assertTrue(any("no chat template" in n for n in ok.get("notes", [])), ok)

    def test_a_load_bigger_than_the_whole_card_says_so(self):
        """Found on the real card: a context no card this size could hold was
        refused with "Something else holds it" - on a card that was empty. What
        cannot fit at all says so, and says what to change."""
        m = self.manager(vram=lambda: {"free": 1000, "total": 1100})
        with self.assertRaises(BackendError) as caught:
            m.acquire("alpha", W)
        said = str(caught.exception)
        self.assertIn("cannot fit on this card", said)
        self.assertIn("ctx", said)
        self.assertNotIn("Something else", said)
        check = next(e for e in read_events(m.events_path) if e["event"] == "vram_check")
        self.assertEqual(check["card_gb"], round(1100 / 1024, 2))

    def test_a_load_that_would_fit_an_idle_card_says_something_else_holds_it(self):
        m = self.manager(vram=lambda: {"free": 1024, "total": 24 * 1024})
        with self.assertRaises(BackendError) as caught:
            m.acquire("alpha", W)
        said = str(caught.exception)
        self.assertIn("not enough free VRAM", said)
        # CHANGED ON PURPOSE (H9, the owner's ruling of 2026-09-28, "Fail
        # loudly"): "Something else holds it" is said in numbers now - what
        # programs outside the bootstrap hold - and with what to do
        self.assertIn("programs outside the bootstrap hold 23552 MiB", said)
        self.assertIn("Close programs that use the card, or reboot", said)
        self.assertNotIn("cannot fit on this card", said)

    def test_the_card_is_asked_for_its_free_and_its_total(self):
        """The card's own words, read the way nvidia-smi says them - and
        anything else read as unknown, never as a number."""
        real = M.subprocess.run

        class Out:
            def __init__(self, code, text):
                self.returncode, self.stdout = code, text
        cases = [((0, "22000, 24576\n"), {"free": 22000, "total": 24576}),
                 ((1, "22000, 24576\n"), None),
                 ((0, "No devices were found\n"), None)]
        asked = []
        try:
            for (code, text), want in cases:
                with self.subTest(text=text, code=code):
                    M.subprocess.run = lambda argv, *a, **k: (asked.append(argv),
                                                              Out(code, text))[1]
                    # CHANGED ON PURPOSE (H7): the reader kept before the
                    # tripwire - the suite's own is the tripwire now
                    self.assertEqual(_REAL_READER(), want)
        finally:
            M.subprocess.run = real
        self.assertIn("--query-gpu=memory.free,memory.total", asked[0])

    def test_a_server_that_stopped_answering_is_not_reused_while_its_process_lingers(self):
        """Measured on the real card: a llama-server killed underneath takes
        about two seconds to finish exiting, and answers nothing the whole time.
        A reuse that asked only whether its PROCESS was there handed the next
        job that corpse, and the job failed. The stand-in goes deaf the same
        way: it stops listening, and lives on."""
        m = self.manager()
        first = m.acquire("alpha", W)
        req = urllib.request.Request(f"http://127.0.0.1:{first.port}/fake/deaf", data=b"",
                                     method="POST",
                                     headers={"Authorization": f"Bearer {first._key}"})
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
        deadline = time.monotonic() + 5
        while port_in_use("127.0.0.1", first.port):
            self.assertLess(time.monotonic(), deadline, "the stand-in never went deaf")
            time.sleep(0.02)
        self.assertIsNone(first.proc.poll(), "the deaf stand-in must still be running")
        pid, load = first.pid, first.load_id
        second = m.acquire("alpha", W)
        self.assertIsNot(second, first, "a server that answers nothing was reused")
        self.assertTrue(second.is_alive())
        self.assertIn("stale_backend", [e["event"] for e in read_events(m.events_path)])
        # CHANGED: asked by its load's id - its pid alone, freed by the stop,
        # was found held by a linker once, under load
        self.assertTrue(self.gone(pid, load), "the lingering process was left running")


#: a process that takes the machine's card through a lock file, says so, and
#: holds it until it is killed - by TerminateProcess, so nothing runs in it.
#: It keeps the card by name: a card nobody refers to is collected and its
#: lock file closed, which lets the card go at once
HOLD = ("import sys, time\n"
        "sys.path.insert(0, '.')\n"
        "from runner.cardlock import MachineCard\n"
        "card = MachineCard(sys.argv[1])\n"
        "card.take('srv-k3x9m2p7', 'someone-elses')\n"
        "print('held', flush=True)\n"
        "time.sleep(600)\n")


class NativeMachineCardRows(ManagerCase):
    """With no coordinator, a real card is taken through the machine's lock
    (runner/cardlock.py). Without it, two servers starting a load in the same
    instant both passed the free-memory check - and a coordinator is exactly
    what the machine did not have."""

    def shared(self, **kw) -> MachineCard:
        return MachineCard(self.tmp / "card.lock", **kw)

    def load_in_thread(self, m, key, **kw):
        box, heard = {}, []

        def load():
            try:
                box["got"] = m.acquire(key, W, on_wait=heard.append, **kw)
            except Exception as exc:                        # noqa: BLE001
                box["got"] = exc
        t = threading.Thread(target=load, daemon=True)
        t.start()
        deadline = time.monotonic() + 10
        while not heard and t.is_alive():
            self.assertLess(time.monotonic(), deadline, "the load never waited")
            time.sleep(0.02)
        return t, box, heard

    def free(self, path) -> bool:
        """Could anybody take the card now? The lock itself is asked - never
        through a wait, so a wait that is broken cannot hang the row asking."""
        fh = _CL._try_lock(Path(path))
        if fh is None:
            return False
        _CL._unlock(fh)
        return True

    def test_two_runners_with_no_coordinator_take_turns_on_the_card(self):
        first = self.manager(card=self.shared())
        second = self.manager(folder="other", card=self.shared())
        first.acquire("alpha", W)
        t, box, heard = self.load_in_thread(second, "beta")
        # CHANGED ON PURPOSE (PLAN-4 D4, S6 R2): the waiting server says that
        # it waits, and since when - never who holds the card, nor for what
        self.assertTrue(heard[-1].startswith("waiting for the card, since "), heard[-1])
        for other in (f"pid {os.getpid()}", "alpha"):
            self.assertNotIn(other, heard[-1])
        time.sleep(0.5)
        self.assertTrue(t.is_alive(), "the second loaded beside the first")
        self.assertNotIn("load_start", [e["event"] for e in read_events(second.events_path)])
        first.unload()
        t.join(20)
        self.assertTrue(box["got"].is_alive(), box["got"])
        unloaded = next(e["ts"] for e in read_events(first.events_path)
                        if e["event"] == "unload_ok")
        mine = read_events(second.events_path)
        self.assertGreaterEqual(next(e["ts"] for e in mine if e["event"] == "load_start"),
                                unloaded, "the second load began before the first came off")
        waited = next(e for e in mine if e["event"] == "card_waited")
        self.assertTrue(waited["waiting_for"].startswith("waiting for the card, since ")
                        and "alpha" not in waited["waiting_for"], waited["waiting_for"])
        # the record shows the lock itself, not only the loads around it
        theirs = read_events(first.events_path)
        took = [(e["source"], e["model_key"]) for e in theirs if e["event"] == "card_granted"]
        gave = [e for e in theirs if e["event"] == "card_given_back"]
        self.assertEqual(took, [("machine lock", "alpha")], "the first's take is not on its record")
        self.assertEqual([(e["source"], e["model_key"]) for e in gave], [("machine lock", "alpha")],
                         "the first's give-back is not on its record")
        mine_took = [e for e in mine if e["event"] == "card_granted"]
        self.assertEqual([(e["source"], e["model_key"]) for e in mine_took],
                         [("machine lock", "beta")], "the second's take is not on its record")
        self.assertGreaterEqual(mine_took[0]["ts"], gave[0]["ts"],
                                "the second took the lock before the first gave it back")

    def test_a_holder_that_is_killed_lets_go_of_the_card(self):
        """Nothing runs in a process that is killed; the kernel lets its lock go."""
        path = self.tmp / "card.lock"
        child = subprocess.Popen([sys.executable, "-c", HOLD, str(path)], cwd=HERE,
                                 stdout=subprocess.PIPE, text=True, creationflags=NO_WINDOW)
        self.addCleanup(lambda: (killed(child), child.stdout.close()))
        self.assertEqual(child.stdout.readline().strip(), "held")
        m = self.manager(card=MachineCard(path))
        t, box, heard = self.load_in_thread(m, "alpha")
        # CHANGED ON PURPOSE (PLAN-4 D4): it waits, and names nobody
        self.assertTrue(heard[-1].startswith("waiting for the card, since ")
                        and f"pid {child.pid}" not in heard[-1], heard[-1])
        killed(child)
        t.join(20)
        self.assertTrue(box["got"].is_alive(), box["got"])

    def test_a_cancel_ends_the_wait_for_the_card(self):
        holder = self.shared()
        holder.take(W, "alpha")
        self.addCleanup(holder.give_back)
        m = self.manager(card=self.shared())
        cancel = threading.Event()
        t, box, heard = self.load_in_thread(m, "beta", cancel=cancel)
        cancel.set()
        t.join(3)
        self.assertIsInstance(box["got"], Cancelled, box["got"])
        self.assertNotIn("load_start", [e["event"] for e in read_events(m.events_path)])

    def test_the_patience_bounds_the_wait_and_the_refusal_names_no_one(self):
        """CHANGED ON PURPOSE (PLAN-4 D4, S6 R2): the refusal named who held
        the card - its pid, its actor and its model. It names nobody now, and
        says its own reason, as a coordinator's does."""
        holder = self.shared()
        holder.take("srv-k3x9m2p7", "alpha")
        self.addCleanup(holder.give_back)
        m = self.manager(card=self.shared(patience_s=0.6))
        t, box, heard = self.load_in_thread(m, "beta")
        t.join(5)
        self.assertFalse(t.is_alive(), "the wait for the card was not bounded")
        self.assertIsInstance(box["got"], BackendError, box["got"])
        said = str(box["got"])
        self.assertIn("no card within its patience of 0.6s (card_patience)", said)
        self.assertEqual(getattr(box["got"], "reason", None), "card_patience")
        for other in (f"pid {os.getpid()}", "srv-k3x9m2p7", "alpha"):
            self.assertNotIn(other, said)

    def test_a_stopping_server_ends_the_wait_for_the_card(self):
        holder = self.shared()
        holder.take(W, "alpha")
        self.addCleanup(holder.give_back)
        m = self.manager(card=self.shared())
        t, box, heard = self.load_in_thread(m, "beta")
        started = time.monotonic()
        m.stopping("the server is stopping")
        t.join(3)
        self.assertFalse(t.is_alive(), "the stop waited for the card")
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIsInstance(box["got"], BackendError, box["got"])

    def test_the_card_goes_back_on_every_way_a_load_ends(self):
        path = self.tmp / "card.lock"
        m = self.manager(card=MachineCard(path))
        m.acquire("alpha", W)
        self.assertFalse(self.free(path), "a loaded model's card was free")
        m.unload()
        self.assertTrue(self.free(path), "an unload kept the card")
        refused = self.manager(folder="refused", card=MachineCard(path), vram=lambda: 1024)
        with self.assertRaises(BackendError):
            refused.acquire("alpha", W)
        self.assertTrue(self.free(path), "a refused load kept the card")
        said = [e["event"] for e in read_events(refused.events_path) if e["event"].startswith("card_")]
        self.assertEqual(said, ["card_granted", "card_given_back"],
                         "a refused load's take and give-back are not both on its record")
        idle = self.manager(folder="idle", card=MachineCard(path))
        idle.shutdown()
        self.assertNotIn("card_given_back", [e["event"] for e in read_events(idle.events_path)],
                         "a server that never held the lock says it gave it back")
        m.acquire("alpha", W)
        m.shutdown()
        self.assertTrue(self.free(path), "a shutdown kept the card")

    def test_a_card_kept_after_an_unload_that_failed_goes_back_when_the_server_stops(self):
        """Kept on purpose - the model may still be on the card - but only
        until the stop; with nothing coordinating it, nothing else ever gives
        it back while the process lives."""
        path = self.tmp / "card.lock"
        m = self.manager(card=MachineCard(path))
        up = m.acquire("alpha", W)
        real_stop = up.stop
        up.stop = lambda: (_ for _ in ()).throw(BackendError("it would not stop"))
        self.addCleanup(real_stop)                          # the process, and its log file
        with self.assertRaises(BackendError):
            m.unload()
        self.assertFalse(self.free(path), "the card went back while the model may be on it")
        self.assertNotIn("card_given_back", [e["event"] for e in read_events(m.events_path)],
                         "the record says the card went back while it was kept")
        m.shutdown()
        self.assertTrue(self.free(path), "the stop kept the card")
        self.assertEqual([e["event"] for e in read_events(m.events_path)].count("card_given_back"), 1,
                         "the stop's give-back is not on the record, once")

    def test_a_runner_with_no_coordinator_takes_the_machines_lock_by_default(self):
        m = self.manager()
        self.assertIsInstance(m._card, MachineCard)
        self.assertEqual(m._card.path.parent, _LOCKS, "this suite took the machine's own lock")
        named = ModelManager(self.settings(card_lock=str(self.tmp / "named.lock")),
                             self.tmp / "named", vram=lambda: None)
        self.addCleanup(named.shutdown)
        self.assertEqual(named._card.path, self.tmp / "named.lock")
        # and the machine's own is one place every server of this user meets -
        # asked of a copy of the module no suite has patched (another suite in
        # this process may have patched this one before this suite did)
        spec = importlib.util.spec_from_file_location("cardlock_as_shipped",
                                                      HERE / "runner" / "cardlock.py")
        shipped = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(shipped)
        self.assertEqual(shipped.machine_lock(),
                         Path(tempfile.gettempdir()) / "llm-bootstrap-card.lock")


class NativeReclaimRows(ManagerCase):
    """D4 end to end: what a stopped server left running is found from the
    record and reclaimed only on proof - with a REAL kill, of a stand-in this
    suite started."""

    def test_a_load_a_stopped_server_left_running_is_found_and_reclaimed(self):
        dead = self.manager()
        b = dead.acquire("alpha", W)                        # ...and its server "dies" here
        pid = b.pid
        heir = self.manager()
        found = heir.startup_check()
        self.assertEqual([(o["pid"], o["load"], o["ours"]) for o in found["orphans"]],
                         [(pid, b.load_id, True)])
        self.assertEqual(found["reclaimed"], [], "a report reclaimed")
        self.assertFalse(self.gone(pid, b.load_id), "reported only - it must still be running")
        taken = heir.startup_check(reclaim=True)
        self.assertEqual(taken["reclaimed"], [pid])
        self.assertTrue(self.gone(pid, b.load_id, within_s=DEATH_S), "the reclaim killed nothing")
        self.assertIsNone(heir.status()["lease"]["holder"], "a reclaim granted the card")

    def test_what_the_process_list_could_not_answer_about_it_is_asked_again(self):
        """Provoked: the first time the stopped server is asked about, the
        process list does not answer - as beside three sweeps (wave 3), when
        the row above read the server it had just left running as none of its
        own. Asked again, it is found, and reclaimed on proof."""
        module, busy = sys.modules[__name__], []
        real = module._real_process_info

        def not_answering_once(pid):
            if not busy:
                busy.append(pid)
                return {"alive": None, "command_line": None}
            return real(pid)
        self.addCleanup(setattr, module, "_real_process_info", real)
        module._real_process_info = not_answering_once
        self.test_a_load_a_stopped_server_left_running_is_found_and_reclaimed()
        self.assertEqual(len(busy), 1, "the process list was never asked")

    def test_a_server_the_record_does_not_know_is_never_touched(self):
        stranger = self.backend()                           # started, but on no record
        heir = self.manager()
        self.assertEqual(heir.startup_check(reclaim=True), {"orphans": [], "reclaimed": []})
        self.assertTrue(stranger.is_alive())

    def test_a_recorded_pid_that_is_now_another_program_is_left_alone(self):
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 creationflags=NO_WINDOW)
        self.addCleanup(killed, other)
        folder = self.tmp / "bound"
        folder.mkdir()
        rows = [{"event": "session_start", "pid": 1},
                {"event": "load_ok", "model_key": "alpha", "pid": other.pid, "port": 50123,
                 "gguf": str(self.models / "model.gguf"), "load": "load-k3x9m2pq"}]
        (folder / M.EVENTS_NAME).write_text("".join(json.dumps(r) + "\n" for r in rows),
                                            encoding="utf-8")
        heir = self.manager()
        out = heir.startup_check(reclaim=True)
        self.assertEqual([(o["pid"], o["ours"]) for o in out["orphans"]], [(other.pid, False)])
        self.assertIsNone(other.poll(), "another program was killed")


class NativeManagerMemoryRows(ManagerCase):
    """PLAN-4 D3, end to end on the stand-in: a llama-server that runs out of
    the card's memory mid-answer writes ggml's line to its log and ends, as
    b10724's does; the runner reads it from the load's own log, says it on
    the work its card source sends, and the answer fails as the card over-full
    - never merely as a server that went away."""

    def test_a_model_that_ran_out_of_memory_fails_its_answer_saying_the_line(self):
        m = self.manager()
        spec = m.cfg.model("alpha")
        spec.extra_args = [*spec.extra_args, "--fake-oom-after", "1"]
        b = m.acquire("alpha", W)
        with self.assertRaises(M.Degraded) as caught:
            list(m.answer(b, "Tell me about rain.", GenParams(max_tokens=200)))
        self.assertEqual(caught.exception.reason, "card_degraded")
        self.assertIn("CUDA error: out of memory", str(caught.exception))
        said = m.work()
        self.assertEqual((said["memory_errors"], said["memory_error"]),
                         (1, "CUDA error: out of memory"))
        lines = [e for e in read_events(m.events_path) if e["event"] == "memory_error"]
        self.assertEqual([(e["model_key"], e["load"], e["line"]) for e in lines],
                         [("alpha", b.load_id, "CUDA error: out of memory")])


class NativeCardFullRecoveryRows(ManagerCase):
    """S5b, end to end on the stand-in, a server run on its own (the owner,
    2026-09-27: "same behaviour as coordinator"; and his example: VRAM was
    full, he closed a few programs, and the request went through): the queue,
    the runner and the machine's lock watching the card through a reader the
    row sets. The llama stand-in runs out of memory mid-answer; the job is
    held, saying so; the row frees the card; the job runs again by itself -
    or it fails `card_degraded` at once with patience 0, at its patience on a
    card that never frees, and after `recover_attempts` on one that fills
    again each time."""

    def setUp(self):
        super().setUp()
        self.free = [20000]

    def read(self):
        return {"free": self.free[0], "total": 24576}

    def queue(self, *fake, **queue_kw):
        import jobqueue as Q
        from runner.adapter import RunnerBackend
        card = MachineCard(self.tmp / "card.lock", watch_s=30.0, recover_watch_s=0.05)
        m = self.manager(card=card, vram=self.read)
        spec = m.cfg.model("alpha")
        spec.extra_args = [*spec.extra_args, "--fake-oom-after", "1", *fake]
        a = RunnerBackend(m, log=lambda s: None, idle_poll_s=3600)
        a.attach(W)
        self.addCleanup(a.close)
        folder = self.tmp / "desk"
        folder.mkdir(exist_ok=True)
        q = Q.Queue(folder, Q.Hooks(), a, log=lambda s: None, actor=W, **queue_kw)
        self.addCleanup(q.close, 5.0)
        m.acquire("alpha", W)                               # up, on a card with room
        self.free[0] = 100                                  # ...and now the card is full
        return Q, q, m, folder

    def job(self, q, **kw):
        return q.submit("generate", {"prompt": "Tell me about rain."},
                        {"model": "alpha", "max_tokens": 20}, priority="interactive",
                        **kw)["job_id"]

    def ended(self, q, jid, timeout=30.0):
        deadline = time.monotonic() + timeout
        while q.get(jid)["status"] not in ("done", "failed", "cancelled"):
            self.assertLess(time.monotonic(), deadline, f"{jid} never ended: {q.get(jid)}")
            time.sleep(0.05)
        return q.get(jid)

    def story(self, Q, q, folder, jid):
        return Q.request_record(folder, q.get(jid)["correlation_id"])["events"]

    def test_the_card_freed_the_job_runs_again_by_itself(self):
        Q, q, m, folder = self.queue("--fake-oom-once", str(self.tmp / "broke-once"))
        jid = self.job(q)
        deadline = time.monotonic() + 20
        while (q.get(jid)["progress"] or {}).get("stage") != "waiting for the card's memory":
            self.assertLess(time.monotonic(), deadline, f"it never said it waits: {q.get(jid)}")
            time.sleep(0.05)
        prog = q.get(jid)["progress"]
        self.assertEqual(q.get(jid)["status"], "running")
        self.assertIn("The card is full: 0.1 GB free", prog["waiting_for"])
        self.assertIn("Close something, and this runs again by itself.", prog["waiting_for"])
        self.assertEqual(prog["free_mib"], 100)
        time.sleep(0.5)
        self.assertEqual(q.get(jid)["status"], "running", "it failed instead of waiting")
        self.free[0] = 20000                                # the programs are closed
        j = self.ended(q, jid)
        self.assertEqual((j["status"], j.get("error")), ("done", None))
        events = self.story(Q, q, folder, jid)
        self.assertEqual(len([e for e in events if e["event"] == "attempt_card_full"]), 1)
        self.assertEqual([e["sample"] for e in events if e["event"] == "attempt_started"], [1, 2])
        self.assertIn("card_full_waited", [e["event"] for e in read_events(m.events_path)])

    def test_with_patience_0_it_fails_at_once(self):
        Q, q, m, folder = self.queue()
        jid = self.job(q, card_patience_s=0)
        j = self.ended(q, jid)
        self.assertEqual((j["status"], j.get("reason")), ("failed", "card_degraded"))
        self.assertIn("CUDA error: out of memory", j["error"])
        self.assertEqual([e["event"] for e in self.story(Q, q, folder, jid)
                          if e["event"] in ("attempt_started", "attempt_card_full")],
                         ["attempt_started"])

    def test_on_a_card_that_never_frees_it_fails_at_its_patience(self):
        Q, q, m, folder = self.queue("--fake-oom-once", str(self.tmp / "broke-once"))
        started = time.monotonic()
        jid = self.job(q, card_patience_s=1.5)
        j = self.ended(q, jid)
        self.assertGreaterEqual(time.monotonic() - started, 1.5)
        self.assertEqual((j["status"], j.get("reason")), ("failed", "card_degraded"))
        self.assertIn("the card stayed full for the whole of this request's patience of 1.5s: "
                      "it read 100 MiB free at the end", j["error"])

    def test_on_a_card_that_fills_again_each_time_it_fails_after_recover_attempts(self):
        Q, q, m, folder = self.queue(recover_attempts=2)
        self.free[0] = 20000                                # room each time it is read
        jid = self.job(q)
        j = self.ended(q, jid, timeout=60.0)
        self.assertEqual((j["status"], j.get("reason")), ("failed", "card_degraded"))
        self.assertIn("3 times, and it is not held again after 2", j["error"])
        self.assertEqual(len([e for e in self.story(Q, q, folder, jid)
                              if e["event"] == "attempt_card_full"]), 2)


class NativeStartStepRows(ManagerCase):
    """PLAN-4 D12 (S11b), on the stand-in: a load's start is timed in the
    steps llama-server's start shows - its process started, its port
    answering, healthy, its identity checked - each from the one before it,
    on the statistics' record; and its health is asked every tenth of a
    second by default, so a start is timed to a tenth, never to a second."""

    def test_a_loads_start_is_timed_in_its_steps_each_from_the_one_before(self):
        m = self.manager()
        m.cfg.runner["health_poll_s"] = Config.default().runner["health_poll_s"]
        spec = m.cfg.model("alpha")
        spec.extra_args = [*spec.extra_args, "--fake-load-s", "1"]
        m.acquire("alpha", W)
        got = next(e["measures"] for e in m.stat_lines() if e["what"] == "load")
        # the machine's lock taken for it first: its wait, and no claim
        self.assertEqual(list(got), ["load_wait_s", "load_spawn_s", "load_port_s",
                                     "load_healthy_s", "load_identity_s", "load_s"])
        self.assertGreaterEqual(got["load_healthy_s"], 0.25,
                                "healthy once its second of loading had passed, timed "
                                "from its port's first answer")
        self.assertLess(got["load_healthy_s"], 1.0,
                        "asked every tenth of a second: a second's poll is a second late")
        self.assertGreaterEqual(got["load_s"], sum(v for k, v in got.items() if k != "load_s"))


# ============================================================== the job ===

@unittest.skipUnless(SUPPORTED, "job objects are a Windows mechanism")
class JobObjectRows(unittest.TestCase):
    """Carried over: the child must not survive its parent - killed for real,
    looked for for real. And the two struct facts no behaviour can see.

    The child is looked for through a handle to it, by the marker on its
    command line (`gone`) - NEVER THROUGH THE PROCESS LIST, whose query,
    answering nothing on a busy machine, read the control's live child as
    dead (wave 3). A tripwire holds these rows to it."""

    def setUp(self):
        self.marker = f"llmb-job-{ids.mint_actor('load')[5:]}"
        self.tmp = Path(tempfile.mkdtemp(prefix="llmb-job-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.parent = None
        self.child = None
        module = sys.modules[__name__]

        def tripwire(needle):
            raise AssertionError("a job-object row asks about its child through a handle to "
                                 "it, never through the process list")
        self.addCleanup(setattr, module, "cmdlines_containing", module.cmdlines_containing)
        module.cmdlines_containing = tripwire

    def tearDown(self):
        # the control leaves an orphan ON PURPOSE; ended here, by the marker on
        # its command line read through a handle to it
        if self.child is not None:
            end_carrying(self.child, f"{self.marker}-child")
        if self.parent is not None:
            killed(self.parent)

    def start_parent(self, mode: str) -> int:
        out = self.tmp / "child.pid"
        self.parent = subprocess.Popen([sys.executable, str(PARENT), mode, self.marker, str(out)],
                                       creationflags=NO_WINDOW)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            said = out.read_bytes() if out.exists() else b""
            if said.strip():
                self.child = int(said.decode("utf-8").strip())
                # a file this repository writes is LF, never CRLF - as Path.write_text
                # writes one on Windows
                self.assertEqual(said, f"{self.child}\n".encode("utf-8"),
                                 "the parent's report is not one LF line")
                return self.child
            if self.parent.poll() is not None:
                self.fail(f"the {mode} parent exited before reporting a child")
            time.sleep(0.1)
        self.fail(f"the {mode} parent never reported its child")

    def alive(self) -> bool:
        return not gone(self.child, f"{self.marker}-child")

    def test_the_child_carries_its_tree_on_its_command_line(self):
        """H4's R7: a job child a run left behind - a sleep of 900 s - carried
        only its marker, and no path of the tree it came from: a sweep's
        reap, which finds what outlived a run by the run's folder on its
        command line, could not find it. It carries its tree's folder."""
        self.start_parent("bare")
        handle, why = open_process(self.child)
        self.assertIsNotNone(handle, f"the child could not be opened: {why}")
        try:
            line = asked_again(lambda: command_line_of(handle), lambda got: got is not None,
                               "the child's command line")
        finally:
            close_process(handle)
        self.assertIn(f"{self.marker}-child", line)
        self.assertIn(str(HERE).lower(), line.lower(), "the child names no folder of its tree")

    def test_a_terminated_parent_takes_its_job_child_with_it(self):
        self.start_parent("job")
        self.assertTrue(self.alive(), "the child never started")
        taskkill(self.parent.pid)
        deadline = time.monotonic() + DEATH_S
        while time.monotonic() < deadline:
            if not self.alive():
                return
            time.sleep(0.25)
        self.fail(f"the child (pid {self.child}) outlived a TerminateProcess on its parent")

    def test_WITHOUT_the_job_the_same_kill_leaves_the_child_running(self):
        """The control: without it the row above could be measuring Windows
        killing the tree, not the job."""
        self.start_parent("bare")
        self.assertTrue(self.alive(), "the child never started")
        taskkill(self.parent.pid)
        deadline = time.monotonic() + SURVIVAL_S
        while time.monotonic() < deadline:
            self.assertTrue(self.alive(), "the unprotected child died anyway - the kill "
                                          "is taking the tree, and the row above proves nothing")
            time.sleep(0.5)

    def test_the_extended_limit_struct_is_the_size_windows_expects(self):
        self.assertEqual(ctypes_sizeof(), 144 if sys.maxsize > 2 ** 32 else 112)

    def test_the_handle_is_released_and_the_job_says_so(self):
        job = KillOnCloseJob()
        self.assertTrue(job.open)
        job.close()
        self.assertFalse(job.open)
        job.close()                                         # a stop may reach it twice
        self.assertEqual(JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, 0x2000)


def ctypes_sizeof() -> int:
    import ctypes
    return ctypes.sizeof(_JOBOBJECT_EXTENDED_LIMIT_INFORMATION)


class BackendJobCase(Case):
    """A real parent holding the runner's own backend MID-LOAD.

    The stand-in its parent reports is asked about through a handle to it,
    by its load id (`gone`) - never through the process list, whose query,
    answering nothing on a busy machine, read a live stand-in as dead. The
    list is asked only by the reap, for what the parent may have started
    that it never reported; a guard holds these rows to it."""

    def setUp(self):
        super().setUp()
        module = sys.modules[__name__]
        real = module.cmdlines_containing

        def only_the_reap(needle):
            if needle != "fake_llama_server.py":
                raise AssertionError("a backend job row asks about the stand-in its parent "
                                     "reported through a handle to it, never through the "
                                     "process list")
            return real(needle)
        self.addCleanup(setattr, module, "cmdlines_containing", real)
        module.cmdlines_containing = only_the_reap

    def run_parent(self, mode: str):
        out = self.tmp / "load.txt"
        # Cleanups run last-first: the reap is registered FIRST so it runs
        # AFTER the parent is dead - a retry the parent spawned as it died is
        # then already there to be found.
        self.addCleanup(self.reap)
        # the stand-in's port is of this copy's own block: one the parent picked
        # from the whole range could be another copy's - three copies running,
        # a stand-in the parent had reported was gone before the row saw it
        # start (the cause unsaid: its log went with the row's folder)
        port = quiet_port()
        self.parent = subprocess.Popen([sys.executable, str(PARENT), mode, str(out),
                                        str(self.models), "model.gguf", str(self.tmp / "plogs"),
                                        str(port)], creationflags=NO_WINDOW)
        self.addCleanup(killed, self.parent)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            said = out.read_bytes() if out.exists() else b""
            text = said.decode("utf-8").strip()
            if "\t" in text:
                load, pid = text.split("\t")
                # a file this repository writes is LF, never CRLF - as Path.write_text
                # writes one on Windows
                self.assertEqual(said, f"{load}\t{pid}\n".encode("utf-8"),
                                 "the parent's report is not one LF line")
                line = self.stand_in_line(int(pid))
                self.assertIn(f"--port {port}", line, "the stand-in is not on the port the row "
                                                     f"gave it: {line}")
                # the parent reports its stand-in the moment it exists, and the
                # backend puts it in the job right after: before the row kills
                # the parent, that assignment is given a second - the time the
                # process query once asked in between gave it
                time.sleep(1.0)
                return load, int(pid)
            if self.parent.poll() is not None:
                self.fail(f"the {mode} parent exited before its llama-server started")
            time.sleep(0.1)
        self.fail(f"the {mode} parent never reported its llama-server")

    def stand_in_line(self, pid: int) -> str:
        """The command line of the stand-in the parent reported, read through
        a handle to it - or, when it has ended already, its log's last words:
        a row that finds it gone says why."""
        handle, _ = open_process(pid)
        if handle is not None:
            try:
                if not exited(handle):
                    return command_line_of(handle) or ""
            finally:
                close_process(handle)
        return f"it has ended; its log: {self.stand_in_log()}"

    def stand_in_log(self) -> str:
        path = self.tmp / "plogs" / "llama-server-fake.log"
        return path.read_bytes().decode("utf-8", "replace")[-800:] if path.exists() else "none"

    #: how many rounds the reap may find what the row's parent left before it
    #: says its ends did not take: a parent starts one stand-in, and one more
    #: when a build refuses a flag
    REAP_ROUNDS = 5

    def reap(self) -> None:
        """Every stand-in this row's parent started, found by this row's own
        models folder - on the command line of each whatever else changed -
        until none is found twice running. BY COMMAND LINE, never by time:
        each ended on that proof, read again through a handle held across the
        kill. A listing the process list could not give is asked for again -
        never read as "none found". One that keeps finding them - its ends did
        not take - says so after REAP_ROUNDS rounds, never loops for ever."""
        quiet, rounds = 0, 0
        while quiet < 2:
            listed = asked_again(lambda: cmdlines_containing("fake_llama_server.py"),
                                 lambda got: got is not None,
                                 "which stand-ins this row's parent left running")
            found = [pid for pid, cmd in listed if str(self.models).lower() in cmd.lower()]
            if found:
                rounds += 1
                if rounds > self.REAP_ROUNDS:
                    raise AssertionError(f"the reap still found {found} after ending them "
                                         f"{self.REAP_ROUNDS} times: an end that does not take")
            for pid in found:
                end_carrying(pid, str(self.models))
            quiet = 0 if found else quiet + 1
            time.sleep(0.3)

    def server_alive(self, load: str, pid: int) -> bool:
        """Asked through a handle to it, by its load id - never through the
        process list, which answers nothing on a busy machine."""
        return not gone(pid, load)


@unittest.skipUnless(SUPPORTED, "job objects are a Windows mechanism")
class BackendJobRows(BackendJobCase):
    """Carried over, by behaviour: the backend puts llama-server in the job
    AT THE SPAWN, before the health wait. The existing suite read the source
    for that order; here a real parent holding a real backend MID-LOAD is
    terminated, and its stand-in llama-server must die with it."""

    def test_a_parent_killed_mid_load_takes_its_loading_llama_server_with_it(self):
        load, pid = self.run_parent("backend")
        self.assertTrue(self.server_alive(load, pid),
                        f"the stand-in never started; its log: {self.stand_in_log()}")
        taskkill(self.parent.pid)
        deadline = time.monotonic() + DEATH_S
        while time.monotonic() < deadline:
            if not self.server_alive(load, pid):
                return
            time.sleep(0.25)
        self.fail(f"llama-server (pid {pid}) outlived its parent, killed mid-load: the job "
                  f"is not applied at the spawn, or not before the health wait")


@unittest.skipUnless(SUPPORTED, "job objects are a Windows mechanism")
class NativeBackendJobControlRows(BackendJobCase):
    def test_WITHOUT_the_job_the_same_kill_leaves_it_running(self):
        """The control for the row above, on the backend itself."""
        load, pid = self.run_parent("backend-bare")
        self.assertTrue(self.server_alive(load, pid),
                        f"the stand-in never started; its log: {self.stand_in_log()}")
        taskkill(self.parent.pid)
        deadline = time.monotonic() + SURVIVAL_S
        while time.monotonic() < deadline:
            self.assertTrue(self.server_alive(load, pid),
                            "without the job it died anyway - the kill takes the tree")
            time.sleep(0.5)


@unittest.skipUnless(SUPPORTED, "a handle to a process is Windows'")
class NativeBackendJobReapRows(BackendJobCase):
    def left_running(self):
        """A stand-in, as the row's parent would leave one: its command line
        carries the row's models folder. -> (pid, its load id)"""
        load, before = ids.mint_actor("load"), len(SPAWNS)
        proc = _only_the_stand_in([*LAUNCHER, "--alias", load, "--port", str(quiet_port()),
                                   "-m", str(self.models / "model.gguf")],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  creationflags=NO_WINDOW)
        self.addCleanup(killed, proc)
        ((pid, _),) = launched_since(before)
        return pid, load

    def test_the_reap_asks_again_what_the_process_list_could_not_answer(self):
        """Provoked: the reap's listing does not answer, twice running. Read
        as "none found", the reap stopped there and left what the row's
        parent had started running; asked again, it is found, and ended on
        its proof."""
        pid, load = self.left_running()
        module, refused = sys.modules[__name__], []
        guarded = module.cmdlines_containing

        def not_answering_twice(needle):
            if len(refused) < 2:
                refused.append(needle)
                return None
            return guarded(needle)
        module.cmdlines_containing = not_answering_twice    # set back by setUp's cleanup
        self.reap()
        self.assertEqual(refused, ["fake_llama_server.py"] * 2, "the reap never listed")
        self.assertTrue(gone(pid, load, within_s=DEATH_S), "the reap left the stand-in running")

    def test_a_reap_whose_ends_do_not_take_says_so_and_never_loops_for_ever(self):
        """Provoked: every end the reap asks for does not take. It finds the
        stand-in round after round; after REAP_ROUNDS it says so."""
        self.left_running()
        module, box = sys.modules[__name__], {}
        self.addCleanup(setattr, module, "end_carrying", module.end_carrying)
        module.end_carrying = lambda pid, mark: True        # said ended, still running

        def reaping():
            try:
                self.reap()
                box["said"] = "it ended"
            except AssertionError as e:
                box["said"] = str(e)
        t = threading.Thread(target=reaping, daemon=True)
        t.start()
        # its rounds, each one listing of the process list (bounded at 60 s)
        t.join((self.REAP_ROUNDS + 2) * 60)
        self.assertFalse(t.is_alive(), "the reap looped on, finding what it could not end")
        self.assertIn("an end that does not take", box.get("said", ""))


class JobDocRows(unittest.TestCase):
    """Carried over: the reason is written beside the mechanism, where the
    next cleanup will read it - a bare mechanism gets deleted as noise."""

    def test_the_job_module_says_what_it_prevents(self):
        doc = (HERE / "runner" / "jobobject.py").read_text(encoding="utf-8")
        for words in ("TerminateProcess", "KILL_ON_JOB_CLOSE", "strands the card"):
            self.assertIn(words, doc)


# ================================================ ports of this copy's own ===

#: another copy of this suite, as far as its ports go: a process of its own
#: that asks the same range, the way a sweep's copies and another agent's gate
#: do - `take` N times, and print every port it was handed
OTHER_COPY_TAKES = ("import json, sys\n"
                    "sys.path.insert(0, sys.argv[1])\n"
                    "from _suites import quiet_ports\n"
                    "ports = quiet_ports(int(sys.argv[2]), int(sys.argv[3]))\n"
                    "print(json.dumps(sorted({ports.take() for _ in range(int(sys.argv[4]))})))\n")
#: ...or tries to claim one block of the range, by its number, and says
#: whether it got it
OTHER_COPY_CLAIMS = ("import json, sys\n"
                     "sys.path.insert(0, sys.argv[1])\n"
                     "import _suites\n"
                     "lo, hi, ours = int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])\n"
                     "got_ours = _suites.try_block(lo, hi, ours)\n"
                     "got_one = any(_suites.try_block(lo, hi, i)\n"
                     "              for i in range(_suites.block_count(lo, hi)) if i != ours)\n"
                     "print(json.dumps([got_ours, got_one]))\n")


def other_copy(code: str, *args) -> list:
    out = subprocess.run([sys.executable, "-c", code, str(HERE), *[str(a) for a in args]],
                         capture_output=True, text=True, timeout=120, creationflags=NO_WINDOW)
    if out.returncode != 0:
        raise AssertionError(f"the other copy failed: {out.stderr[-2000:]}")
    return json.loads(out.stdout)


class NativeQuietPortsRows(unittest.TestCase):
    """Every copy of this suite on the machine takes its ports from one range
    (QUIET_PORTS) - a sweep's copies, a gate beside another agent's - and two
    copies that bind-checked the same free port in one moment both launched on
    it: another copy took 29445 between this one's check and its launch. Now
    each copy claims a block of the range for as long as it runs, and hands
    out only ports of its own block."""

    def test_no_suite_listens_on_a_port_the_operating_system_picks(self):
        """H8's register: two rows bound a socket to port 0 - a port the
        operating system picks, which may be anybody's the moment after. No
        suite of this part binds one: each takes a port of its copy's own
        block, as every server and coordinator it starts does."""
        zero = re.compile(r"\.bind\(\(\s*[\"'][^\"']*[\"']\s*,\s*0\s*\)\)")
        found = [f"{p.name}:{i}" for p in sorted(HERE.glob("test_*.py"))
                 for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
                 if zero.search(line)]
        self.assertEqual(found, [], "a socket bound to port 0")

    def test_another_copy_never_hands_out_a_port_this_copy_does(self):
        mine = {quiet_port() for _ in range(300)}
        theirs = set(other_copy(OTHER_COPY_TAKES, *QUIET_PORTS, 300))
        self.assertEqual(mine & theirs, set(), "two copies of the suite hand out one port")

    def test_the_block_this_copy_claimed_is_refused_to_every_other_copy(self):
        """And the refusal is not vacuous: the same other copy, in the same
        moment, claims a block that is nobody's. (In one process: a spare
        found free by one and claimed by the next could be another copy's in
        between - a sweep starts copies all the time.)"""
        QUIET.block                                         # claimed, if no row has yet
        self.assertEqual(other_copy(OTHER_COPY_CLAIMS, *QUIET_PORTS, QUIET.index), [False, True])

    def test_a_copy_that_claimed_a_block_ends_without_a_resource_warning(self):
        """S13b R1: the claims file is held for as long as the copy lives -
        and closed as it ends, so it never adds a ResourceWarning to one a
        suite's own leak printed (the gate fails on either)."""
        out = subprocess.run([sys.executable, "-W", "always::ResourceWarning", "-c",
                              OTHER_COPY_TAKES, str(HERE), *[str(a) for a in QUIET_PORTS], "3"],
                             capture_output=True, text=True, timeout=120, creationflags=NO_WINDOW)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertEqual(len(json.loads(out.stdout)), 3, out.stdout)
        self.assertNotIn("ResourceWarning", out.stderr)

    def test_a_port_somebody_listens_on_is_never_handed_out(self):
        """Every port of the block that can be bound is listened on, but one.
        One that cannot is somebody's already - left by a row before, or taken
        by a copy of an older tree, which claims nothing: a control of a sweep
        was red when the row assumed it could bind them all."""
        first, last = QUIET.block
        held = []
        try:
            for p in range(first, last + 1):
                s = socket.socket()
                try:
                    s.bind(("127.0.0.1", p))
                except OSError:
                    s.close()
                    continue
                s.listen()
                held.append(s)
            self.assertGreaterEqual(len(held), 2, "the block is in use: the row could not fail")
            spare = held.pop()
            port = spare.getsockname()[1]
            spare.close()
            taken = {quiet_port() for _ in range(10)}
            self.assertEqual(taken & {s.getsockname()[1] for s in held}, set(),
                             "a port somebody listens on was handed out")
            self.assertIn(port, taken, "the port nobody listens on was never handed out")
        finally:
            for s in held:
                s.close()


class NativeManagerPortRows(ManagerCase):
    def test_a_load_whose_settings_name_no_port_takes_one_of_this_copys_block(self):
        """Never one the operating system picks: between that pick and the
        stand-in's bind, any connection on the machine can be handed it - and
        the stand-in binds it exclusively, so it would fail its load."""
        m = self.manager()
        b = m.acquire("alpha", W)
        first, last = QUIET.block
        self.assertTrue(first <= b.port <= last,
                        f"port {b.port} is not of this copy's block {first}-{last}")


class NativeExclusivePortRows(Case):
    """The stand-in holds its port alone. It bound with SO_REUSEADDR (the
    standard library's http.server sets it), and on Windows a second socket
    that sets it too is let bind a port a first one listens on - measured on
    this machine: a listener with it, then a second bind with it, of the same
    address: bound. Two stand-ins on one port, and a row's requests answered
    by another copy's. Each way round is provoked here with a helper's
    socket."""

    def helper(self) -> socket.socket:
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.addCleanup(s.close)
        return s

    def test_nobody_binds_a_port_the_stand_in_listens_on(self):
        b = self.backend()
        with self.assertRaises(OSError, msg=f"a helper bound port {b.port}, where the "
                                            f"stand-in listens"):
            self.helper().bind(("127.0.0.1", b.port))

    def test_the_stand_in_never_shares_a_port_somebody_has_bound(self):
        """Bound and not yet listening - as another stand-in is while it
        starts - so the runner's own check before the launch (a connection)
        finds the port free, and the stand-in's bind is all that is left."""
        squatter, port = self.helper(), quiet_port()
        squatter.bind(("127.0.0.1", port))
        b = LlamaCppBackend(self.spec(), self.models, self.cfg(startup_timeout_s=5.0),
                            self.logs, port)
        b.load_id = ids.mint_actor("load")
        self.addCleanup(self.stop_quietly, b)
        msg = self.refused(b.start)
        self.assertIn("exited with code 1", msg, "the stand-in came up on a port somebody holds")
        self.assertIn("couldn't bind", msg)


# ========================================== is a process this suite started gone? ===

class NativeGoneRows(unittest.TestCase):
    """`gone(pid, load)`: how every row here asks whether a process it started
    is gone - never by its pid alone, which Windows hands on as soon as the
    last handle to the process closes (measured on this machine: a freed pid
    came back after 30 to 79 launches; beside process churn, the swap row
    found its first server's pid held by another copy's own process query,
    and the deaf row found a linker). Each way the old question went wrong is
    provoked here, on purpose."""

    def start(self, *marks, sleep: float = 60.0) -> subprocess.Popen:
        """A process this row started, with *marks on its command line."""
        p = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({sleep})",
                              *marks], creationflags=NO_WINDOW)
        self.addCleanup(killed, p)
        return p

    def test_a_process_carrying_its_load_is_not_gone_until_it_ends(self):
        load = ids.mint_actor("load")
        p = self.start(load)
        self.assertFalse(gone(p.pid, load), "a running process read as gone")
        killed(p)
        t0 = time.monotonic()
        self.assertTrue(gone(p.pid, load), "a process that ended read as running")
        self.assertLess(time.monotonic() - t0, ASK_S / 2,
                        "an ended process was not seen as ended: it was asked about until "
                        "the question gave up")

    def test_a_pid_handed_on_to_another_program_is_gone(self):
        """This row's own process stands in for the program the pid was
        handed to: it carries none of the load's id."""
        other = self.start("somebody-else")
        self.assertTrue(gone(other.pid, ids.mint_actor("load")),
                        "a pid handed on to another program read as the load still running")

    def test_a_process_still_ending_is_waited_for_within_its_bound(self):
        load = ids.mint_actor("load")
        p = self.start(load, sleep=3.0)
        self.assertFalse(gone(p.pid, load), "a process still running read as gone")
        t0 = time.monotonic()
        self.assertTrue(gone(p.pid, load, within_s=20.0),
                        "a process that ended within the bound was not waited for")
        self.assertLess(time.monotonic() - t0, 20.0)

    def test_an_answer_the_system_could_not_give_is_asked_for_again(self):
        """A question the operating system could not answer for a moment is
        asked again - never answered "not gone" in its place."""
        load = ids.mint_actor("load")
        p = self.start(load)
        killed(p)                   # ended - and its handle, held here, keeps its pid
        module, failed = sys.modules[__name__], []
        real = module.open_process

        def refusing(pid):
            if len(failed) < 2:
                failed.append(pid)
                return None, 1450                           # ERROR_NO_SYSTEM_RESOURCES
            return real(pid)
        module.open_process = refusing
        self.addCleanup(setattr, module, "open_process", real)
        self.assertTrue(gone(p.pid, load))
        self.assertEqual(failed, [p.pid, p.pid], "it was not asked again")

    def test_an_answer_the_system_never_gives_is_said_never_guessed(self):
        load = ids.mint_actor("load")
        p = self.start(load)
        module = sys.modules[__name__]
        for name, value in (("open_process", lambda pid: (None, 1450)), ("ASK_S", 0.3)):
            self.addCleanup(setattr, module, name, getattr(module, name))
            setattr(module, name, value)
        with self.assertRaises(AssertionError) as caught:
            gone(p.pid, load)
        self.assertIn("could not tell", str(caught.exception))

    def test_a_pid_alone_is_never_asked_about(self):
        p = self.start("somebody")
        with self.assertRaises(ValueError):
            gone(p.pid, None)

    def swap(self, name, value):
        module = sys.modules[__name__]
        self.addCleanup(setattr, module, name, getattr(module, name))
        setattr(module, name, value)

    @unittest.skipUnless(SUPPORTED, "a handle to a process is Windows'")
    def test_a_pid_held_by_a_process_this_suite_may_not_open_is_gone(self):
        """This suite's own processes are ordinary processes of its user: one
        it is refused the right to open (a service, another user's) is none
        of them - the pid was handed on."""
        p = self.start(ids.mint_actor("load"))
        self.swap("open_process", lambda pid: (None, ERROR_ACCESS_DENIED))
        self.swap("ASK_S", 0.3)
        self.assertTrue(gone(p.pid, "load-notitsown"))

    @unittest.skipUnless(SUPPORTED, "a handle to a process is Windows'")
    def test_a_live_process_whose_command_line_stays_unreadable_is_none_of_its_own(self):
        load = ids.mint_actor("load")
        p = self.start(load)
        self.swap("command_line_of", lambda handle: None)
        self.swap("ASK_S", 0.3)
        self.assertTrue(gone(p.pid, load))


# ===================================================== a machine that is busy ===

class FakeProc:
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
            raise subprocess.TimeoutExpired(["fake"], timeout)
        return self.returncode


class NativeBusyMachineRows(unittest.TestCase):
    """What a row does on a busy machine - the owner's builds and a game
    beside three agents' sweeps, when wave 3's rows went red on code nobody
    had touched. A question the operating system could not answer is asked
    again within a bound, never read as an answer; a kill that ends late is
    waited for. Each is provoked here, on purpose, the system stood in for."""

    def test_a_question_the_system_could_not_answer_is_asked_again(self):
        answers, asked = iter([None, None, "the answer"]), []
        got = asked_again(lambda: asked.append(1) or next(answers), lambda a: a is not None,
                          "a question", pause_s=0.01)
        self.assertEqual((got, len(asked)), ("the answer", 3))

    def test_one_it_never_answers_is_said_never_guessed(self):
        with self.assertRaises(AssertionError) as caught:
            asked_again(lambda: None, lambda a: a is not None, "whether it is up",
                        within_s=0.2, pause_s=0.01)
        self.assertIn("could not tell whether it is up", str(caught.exception))

    def test_it_is_asked_again_for_two_minutes(self):
        """The bound is the machine's, measured: one ask of the process list
        takes half a second alone; beside three sweeps one took 14 s, and one
        did not answer within its own 30 s (wave 3). A question the system
        answers only 100 s after it was first asked is answered."""
        now = [0.0]
        got = asked_again(lambda: "yes" if now[0] >= 100.0 else None, lambda a: a is not None,
                          "a question", clock=lambda: now[0],
                          sleep=lambda s: now.__setitem__(0, now[0] + s))
        self.assertEqual(got, "yes")

    def test_a_kill_that_ends_late_is_waited_for(self):
        """A kill the busy machine finishes 16 s after it was asked: later
        than the 15 s a row waited, the time one went red (wave 3)."""
        p = FakeProc(ends_s=16.0)
        self.assertEqual((killed(p), p.kills), (1, 1))

    def test_one_that_has_not_ended_within_its_bound_is_said_by_its_pid(self):
        p = FakeProc(ends_s=1e9)
        with self.assertRaises(AssertionError) as caught:
            killed(p, within_s=0.2)
        self.assertIn(f"process {p.pid} was killed and had not ended", str(caught.exception))

    def test_one_already_ended_is_not_killed_again(self):
        p = FakeProc(ended=True)
        self.assertEqual((killed(p), p.kills), (1, 0))

    def test_a_process_killed_for_real_has_ended_when_it_is_answered(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             creationflags=NO_WINDOW)
        self.addCleanup(lambda: p.poll() is None and (p.kill(), p.wait(KILL_WAIT_S)))
        code = killed(p)
        self.assertIsNotNone(p.poll(), "it was answered while the process still ran")
        self.assertEqual(code, p.returncode)

    def listing_when(self, run, went: str):
        """`cmdlines_containing`, its query answered by *run* - which must go
        the way the row provokes (`went`): a stand-in that itself failed
        reads as a query that did not answer, and a row proves nothing (one
        did, until a sweep's mutant survived it)."""
        module, real, said = sys.modules[__name__], subprocess, []

        class Busy:
            def __getattr__(self, name):
                return getattr(real, name)

        def asked(args, *a, **k):
            try:
                got = run(real, args, *a, **k)
            except real.TimeoutExpired:
                said.append("did not answer")
                raise
            said.append("answered")
            return got
        busy = Busy()
        busy.run = asked
        self.addCleanup(setattr, module, "subprocess", real)
        module.subprocess = busy
        listed = cmdlines_containing("llmb-busy")
        self.assertEqual(said, [went], "the query was not asked as the row provoked")
        return listed

    def test_a_listing_the_process_list_could_not_give_is_none_never_empty(self):
        def not_answering(real, args, *a, **k):
            raise real.TimeoutExpired(args, k.get("timeout"))
        self.assertIsNone(self.listing_when(not_answering, "did not answer"),
                          "a query that did not answer read as nothing found")

    def test_a_listing_answered_with_an_error_is_none_never_empty(self):
        self.assertIsNone(self.listing_when(lambda real, args, *a, **k: real.CompletedProcess(
            args, 1, "", "Get-CimInstance : Call cancelled"), "answered"),
            "an error read as nothing found")

    @unittest.skipUnless(SUPPORTED, "a handle to a process is Windows'")
    def test_a_process_carrying_the_mark_is_ended_on_it(self):
        mark = f"llmb-busy-{ids.mint_actor('load')[5:]}"
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", mark],
                             creationflags=NO_WINDOW)
        self.addCleanup(killed, p)
        self.assertTrue(end_carrying(p.pid, mark))
        self.assertIsNotNone(p.poll(), "it was said ended while it ran")

    @unittest.skipUnless(SUPPORTED, "a handle to a process is Windows'")
    def test_what_the_last_row_found_left_is_ended_on_its_proof_never_by_its_pid(self):
        """H6's register 4: the last row looks (`gone`) and then ends what it
        found still running - and between the look and the end a stand-in
        may end and its pid be handed on. Provoked: the pid of a live process
        of this row's that does not carry the load is named among what was
        left. It is left alone; the one that carries it is ended."""
        mark = f"llmb-left-{ids.mint_actor('load')[5:]}"
        handed_on, still = (subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                              str(HERE), *words], creationflags=NO_WINDOW)
                            for words in (("somebody-else",), (mark,)))
        self.addCleanup(killed, handed_on)
        self.addCleanup(killed, still)
        self.assertEqual(end_left([(handed_on.pid, mark), (still.pid, mark)]), [still.pid])
        self.assertIsNone(handed_on.poll(), "a process its pid was handed on to was ended")
        self.assertIsNotNone(still.poll(), "the leftover that carries its load still runs")

    @unittest.skipUnless(SUPPORTED, "a handle to a process is Windows'")
    def test_one_that_does_not_carry_it_is_left_alone(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                              "somebody-else"], creationflags=NO_WINDOW)
        self.addCleanup(killed, p)
        self.assertFalse(end_carrying(p.pid, f"llmb-busy-{ids.mint_actor('load')[5:]}"))
        self.assertIsNone(p.poll(), "a process without the mark was ended")


class NativeAskedByTheLoadRows(Case):
    def test_what_every_row_asks_after_a_stop_reads_a_pid_handed_on_as_gone(self):
        """`self.gone`, which every row here asks after a stop, with the
        stopped process's pid handed on to another program - provoked: a live
        process of this row's that carries none of the load's id. Asked by the
        pid alone it read "still running": the swap row, red under load, found
        its first server's pid held by another copy's process query, and the
        deaf row found it held by a linker."""
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                  "somebody-else"], creationflags=NO_WINDOW)
        self.addCleanup(killed, other)
        self.assertTrue(self.gone(other.pid, ids.mint_actor("load")),
                        "a pid handed on to another program read as the stopped server")


# ============================================================ the guards ===

class NativeGuardRows(unittest.TestCase):
    def test_the_suite_starts_nothing_but_its_own_stand_in(self):
        with self.assertRaises(AssertionError):
            L.SPAWN(["llama-server", "-m", "x.gguf"])

    def test_the_suite_kills_only_what_it_started(self):
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 creationflags=NO_WINDOW)
        try:
            with self.assertRaises(AssertionError):
                M.kill_pid(other.pid)
            # a pid this suite once had, handed now to somebody else - Windows
            # does that constantly: the guard asks the command line, never the
            # pid alone
            once = (other.pid, "load-hadthispid")
            SPAWNS.append(once)
            try:
                with self.assertRaises(AssertionError):
                    M.kill_pid(other.pid)
            finally:
                SPAWNS.remove(once)
            self.assertIsNone(other.poll())
        finally:
            killed(other)

    @unittest.skipUnless(SUPPORTED, "a handle to a process is Windows'")
    def test_the_suite_kills_its_own_even_when_the_process_list_cannot_be_asked(self):
        """The guard read the command line through the operating system's
        process list - a query of up to 30 s, which answers nothing when the
        machine is loaded - and so refused to kill a process this suite
        started. It reads it through a handle to the process, held across the
        kill: the pid cannot be handed on between the proof and the kill."""
        load = ids.mint_actor("load")
        mine = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", load],
                                creationflags=NO_WINDOW)
        self.addCleanup(killed, mine)
        SPAWNS.append((mine.pid, load))
        self.addCleanup(SPAWNS.remove, (mine.pid, load))
        real = M.process_info
        M.process_info = lambda pid: {"alive": None, "command_line": None}
        self.addCleanup(setattr, M, "process_info", real)
        self.assertTrue(M.kill_pid(mine.pid), "the kill was refused")
        self.assertIsNotNone(mine.wait(KILL_WAIT_S))


class NativeLaunchCountRows(unittest.TestCase):
    def test_a_launch_is_counted_by_its_launch_never_by_a_new_pid(self):
        """Two launches given one pid - Windows does that - are two launches."""
        global _real_spawn

        class Proc:
            pid = 424242
        real, before = _real_spawn, len(SPAWNS)
        _real_spawn = lambda *a, **k: Proc()                # noqa: E731
        try:
            for load in ("load-aaaaaaaa", "load-bbbbbbbb"):
                _only_the_stand_in([*LAUNCHER, "--alias", load])
        finally:
            _real_spawn = real
        self.assertEqual(launches_since(before), [424242, 424242])
        self.assertEqual([a for _, a in SPAWNS[before:]], ["load-aaaaaaaa", "load-bbbbbbbb"])
        del SPAWNS[before:]


class ZZNothingIsLeftRunning(unittest.TestCase):
    """Last, by name: every stand-in this suite started has been stopped."""

    def test_no_stand_in_this_suite_started_is_still_running(self):
        """A stand-in is this suite's by the load id on its command line, never
        by its pid alone: another copy of this suite, running beside it, has
        stand-ins too, and one of them may hold a pid this suite once had.
        Every launch is asked about through a handle to its pid (`gone`) -
        never through the process list, whose query, answering nothing on a
        busy machine, read as "nothing left running"; a tripwire holds the row
        to it."""
        module = sys.modules[__name__]

        def tripwire(needle):
            raise AssertionError("what this suite launched is asked about through a handle to "
                                 "each launch, never through the process list")
        self.addCleanup(setattr, module, "cmdlines_containing", module.cmdlines_containing)
        module.cmdlines_containing = tripwire
        left = [(pid, alias) for pid, alias in SPAWNS if alias and not gone(pid, alias)]
        # CHANGED ON PURPOSE (H7, H6's register 4): ended on proof through a
        # handle held across the end - never by the pid `gone` looked at
        end_left(left)
        self.assertEqual(left, [], "a stand-in outlived its row")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from _suites import report                             # the tally, counted in rows
    report(unittest.main(exit=False, verbosity=1))
