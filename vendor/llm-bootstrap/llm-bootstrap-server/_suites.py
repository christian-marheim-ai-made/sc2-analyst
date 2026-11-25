"""What this part's suites share, and nothing else here imports.

    report(program)         the line a unittest suite ends with: its `tally`,
                            counted in rows
    quiet_ports(lo, hi)     ports of this copy's own, from a suite's range: a
                            port source, as the runner's manager takes one
    quiet_port(lo, hi)      one of them
    asked_again(ask, ...)   a question the operating system could not answer,
                            asked again within a bound - never read as an answer
    killed(proc)            a process the row started, killed and waited for
                            until it has ended, within a bound for a kill that
                            did not take - never one for a busy machine
    START_S                 how long a process a row starts is given to say it
                            is up
"""
from __future__ import annotations

import atexit
import os
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


def tally(program) -> tuple:
    """(passed, failed, total) for a finished `unittest.main(exit=False)`,
    counted in ROWS - what a sweep compares with its controls:

      * the total is every row the suite loaded, run or not;
      * a row is failed once, however many of its parts (`subTest`) failed;
      * a row that never ran - its class or its module could not be set up -
        is failed. unittest counts such a class as ONE error and its rows
        nowhere: a class whose stand-in could not start read as one failure,
        and the total came out short by every row it had;
      * a class or module that could not tear down fails one row more: its
        rows passed, and what they left behind did not go.

    A skipped row did not fail."""
    result = program.result
    total = program.test.countTestCases()
    rows, teardowns = set(), 0
    for test, _ in result.failures + result.errors:
        if isinstance(test, unittest.TestCase):
            rows.add(getattr(test, "test_case", test).id())     # a part is its row's
        elif not str(test).startswith(("setUpClass", "setUpModule")):
            teardowns += 1                                   # set-ups: their rows never ran
    rows.update(t.id() for t in result.unexpectedSuccesses)
    failed = min(total, len(rows) + (total - result.testsRun) + teardowns)
    return total - failed, failed, total


#: every time a suite reached the real card's reader (`forbid_the_card`)
CARD_ASKED: list = []


def forbid_the_card() -> None:
    """A suite never asks the real card: `nvidia-smi` is the owner's to allow.
    A manager built with no card reader of its own falls back to the real
    one (`ModelManager.card_memory_mib`), so that fallback is a tripwire from
    here on: it raises, and it is written down - a reader that swallows the
    error (a statistic's look, which records a failed read and goes on) still
    fails the suite at its tally (`report`). Found after wave 4: the machine
    lock's memory check read the real card for the stand-in."""
    import traceback
    import runner.manager as M

    def tripwire(*_a, **_k):
        CARD_ASKED.append("".join(traceback.format_stack(limit=6)))
        raise AssertionError("a suite asked the real card (nvidia-smi): give the manager a "
                             "card reader of its own (vram=)")
    M.ModelManager.card_memory_mib = staticmethod(tripwire)


def report(program) -> None:
    """Print the tally, and exit 1 when anything failed - or when any row
    reached the real card's reader (`forbid_the_card`)."""
    passed, failed, total = tally(program)
    if CARD_ASKED:
        print(f"FAIL  the real card was asked {len(CARD_ASKED)} time(s); the first:\n"
              f"{CARD_ASKED[0]}")
        failed, passed = failed + 1, max(0, passed - 1)
    print(f"{passed} passed, {failed} failed, {total} total")
    sys.exit(1 if failed else 0)


# ---------------------------------------------------------------- a busy machine
#
# THE MACHINE THESE SUITES RUN ON IS SOMEBODY'S. Its owner builds on it and
# plays on it while agents' sweeps run beside each other, and in wave 3 rows
# went red on code nobody had touched: one ask of the operating system's
# process list, about half a second alone, took 14 s; one did not answer
# within its own 30 s bound and was read as an answer - a live child as dead,
# a stopped server a row had just left running as none of its own; a plain
# sleeping Python process had not ended 15 s after it was killed; a holder
# did not say "holding" within 10 s of its start. A row asks again what could
# not be answered, and waits for what a busy machine does late - each within
# a bound, which is there for what did not work, never for a slow machine.

#: how long a question the operating system could not answer is asked again
#: before a row says it could not tell: four times the process list's own
#: 30 s bound
ASK_AGAIN_S = 120.0
#: how long a process a row killed is waited for: six times the 15 s after
#: which a killed process had not ended. A kill the system accepted ends the
#: process - the bound is for one that did not take
KILL_WAIT_S = 90.0
#: how long a process a row starts is given to say it is up - a server's
#: "listening on", a holder's "holding": about a second alone, and a start
#: that took ten was one on a busy machine, not one that failed
START_S = 60.0


def asked_again(ask, answered, what: str, within_s: float = ASK_AGAIN_S, pause_s: float = 0.5,
                clock=time.monotonic, sleep=time.sleep):
    """`ask()` until `answered(what it said)`: -> the first answer that is
    one. What the operating system could not answer - its process list asked
    while the machine is busy - is asked again, `pause_s` apart, never read
    as an answer; asked for `within_s` seconds in vain, the row says so -
    "could not tell <what>" - and never guesses."""
    deadline = clock() + within_s
    while True:
        said = ask()
        if answered(said):
            return said
        if clock() >= deadline:
            raise AssertionError(f"could not tell {what}: the operating system gave no answer "
                                 f"in {within_s:g}s of asking (its last: {said!r})")
        sleep(pause_s)


def killed(proc, within_s: float = KILL_WAIT_S):
    """*proc* - a process the row started, by the handle its Popen holds -
    killed, and waited for until it has ended: -> its exit code. One that has
    not ended within `within_s` is said, by its pid - never left running for
    the collector to warn of."""
    if proc.poll() is None:
        proc.kill()
    try:
        return proc.wait(within_s)
    except subprocess.TimeoutExpired:
        raise AssertionError(f"process {proc.pid} was killed and had not ended {within_s:g}s "
                             f"later") from None


# ------------------------------------------------------------- ports of its own
#
# A suite that needs a port takes it from a range of its own, below every common
# system's range for passing connections, and bind-checks it (GUIDE.md). But
# EVERY COPY of a suite on the machine takes it from that same range - a sweep's
# copies, a gate beside another agent's - and two copies that bind-checked one
# free port in the same moment both launched on it: another copy took 29445
# between one's check and its launch. So each copy CLAIMS a block of the range
# for as long as it runs, and hands out only ports of its own block:
#
#   * the claim is the operating system's lock on one byte of a file every copy
#     on the machine shares (`claims_file`): the byte of the block's number.
#     The kernel lets a dead process's locks go, however it died - a copy that
#     is killed leaves no claim behind, and nothing has to run in it;
#   * the file is held open by name for as long as the process lives: a lock
#     file the collector closes lets its lock go;
#   * a port is handed out only while nothing listens on it: the block's ports
#     in turn, each bind-checked.

#: ports in a block: enough for the stand-ins one copy has up at once, and a
#: range of 500 has 50 blocks - more copies of one suite than run at once
#: (six agents' gates and their sweeps, each copy of the llama.cpp suite and
#: each that imports it claiming one)
BLOCK = 10
#: how long a copy waits for a block when every block is claimed
CLAIM_PATIENCE_S = 300.0
_FILES: dict = {}
_PORTS: dict = {}
_OPEN = threading.Lock()


def claims_file(lo: int, hi: int) -> Path:
    """Where every copy of a suite on this machine claims its block of lo-hi."""
    return Path(tempfile.gettempdir()) / f"llmb-ports-{lo}-{hi}.claims"


def _claims(lo: int, hi: int):
    with _OPEN:
        if (lo, hi) not in _FILES:
            _FILES[(lo, hi)] = open(claims_file(lo, hi), "a+b", buffering=0)
            if len(_FILES) == 1:
                atexit.register(_close_claims)
        return _FILES[(lo, hi)]


def _close_claims() -> None:
    """As the copy ends: its claims files closed - held open for as long as
    it lives, and let go by the kernel as it ends either way - so no file is
    left for the collector to warn of (S13b R1: that warning joined one a
    suite's own leak printed)."""
    with _OPEN:
        for fh in _FILES.values():
            try:
                fh.close()
            except OSError:
                pass
        _FILES.clear()


def try_block(lo: int, hi: int, index: int) -> bool:
    """Claim block `index` of lo-hi for this process, for as long as it lives,
    unless another process holds it. -> whether it did."""
    fh = _claims(lo, hi)
    try:
        if sys.platform == "win32":
            import msvcrt
            fh.seek(int(index))
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.lockf(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB, 1, int(index), os.SEEK_SET)
        return True
    except OSError:
        return False


def block_count(lo: int, hi: int, size: int = BLOCK) -> int:
    return (int(hi) - int(lo) + 1) // int(size)


def bindable(port: int) -> bool:
    """Nothing listens on the port now: it can be bound, for a moment."""
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", int(port)))
            return True
        except OSError:
            return False


class QuietPorts:
    """Ports of this copy's own, from lo-hi - a port source (`take`,
    `give_back`), as the runner's manager takes one."""

    def __init__(self, lo: int, hi: int, size: int = BLOCK):
        self.lo, self.hi, self.size = int(lo), int(hi), int(size)
        self.count = block_count(lo, hi, size)
        #: the block this copy claimed, once it has
        self.index: int | None = None
        self._next = 0
        self._lock = threading.Lock()

    @property
    def block(self) -> tuple:
        """(first, last) of this copy's block - claimed the first time it is
        asked for, and kept until the process ends."""
        with self._lock:
            if self.index is None:
                self.index = self._claim()
            first = self.lo + self.index * self.size
            return first, first + self.size - 1

    def _claim(self) -> int:
        order = list(range(self.count))
        random.shuffle(order)
        deadline, said = time.monotonic() + CLAIM_PATIENCE_S, False
        while True:
            for i in order:
                if try_block(self.lo, self.hi, i):
                    return i
            if time.monotonic() >= deadline:
                raise RuntimeError(f"every block of ports {self.lo}-{self.hi} has been claimed "
                                   f"by other copies of this suite for {CLAIM_PATIENCE_S:g}s")
            if not said:
                print(f"[ports] every block of {self.lo}-{self.hi} is claimed by another copy "
                      f"of this suite; waiting for one", flush=True)
                said = True
            time.sleep(0.5)

    def take(self, model_key: str = "") -> int:
        """A port of this copy's block that nothing listens on now: the
        block's ports in turn, so the port handed out last comes round last."""
        first, last = self.block
        with self._lock:
            for _ in range(self.size):
                port = first + self._next % self.size
                self._next += 1
                if bindable(port):
                    return port
        raise RuntimeError(f"every port of this copy's block {first}-{last} is in use")

    def give_back(self, port: int) -> None:
        """Nothing to give: the block stays this copy's until it ends."""


def quiet_ports(lo: int, hi: int, size: int = BLOCK) -> QuietPorts:
    """This process's own ports in lo-hi: one block per range per process."""
    with _OPEN:
        ports = _PORTS.get((lo, hi))
        if ports is None:
            ports = _PORTS[(lo, hi)] = QuietPorts(lo, hi, size)
    if ports.size != size:
        raise ValueError(f"{lo}-{hi} is claimed here in blocks of {ports.size}, not {size}")
    return ports


def quiet_port(lo: int, hi: int) -> int:
    """A port of this copy's own in lo-hi that nothing listens on now."""
    return quiet_ports(lo, hi).take()
