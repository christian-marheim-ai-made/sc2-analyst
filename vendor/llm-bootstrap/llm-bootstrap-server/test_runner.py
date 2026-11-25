"""The runner's rows: the stand-in model, the settings, and the manager.

    py -3.12 test_runner.py

No model, no GPU, no network. Every load is the stand-in or a fake backend
built in this file; no port is opened for either.

THIS SUITE CAN KILL NOTHING. The module's `kill_pid` is replaced by a tripwire
before any manager is built, so a row that reached a real kill fails instead
of killing. The startup-check rows hand the manager a fake instead, and say
what it would have killed.

WHY THE MANAGER IS BUILT HERE, when the existing application's suites were
forbidden to build theirs: that one called `startup_check(reclaim=True)` in
its constructor and killed whatever held its port - a model somebody was
using, more than once. This one checks nothing until asked, reclaims only when
asked, and kills only a process whose command line proves it is one of its own
loads (`NativeStartupCheckRows`). So the rows the existing application had to
write against the SOURCE are written here against the BEHAVIOUR, and the few
source rows kept are the ones that pin a door shut ("the lease is granted in
exactly two places").

Two kinds of rows, as in test_queue.py:

  * ROWS CARRIED OVER BY MEANING from an existing application's runner suites
    (a lease suite, a liveness suite, a runner suite, a load-timing suite),
    restated against this runner. Where a row changed shape, its docstring
    says how.
  * NATIVE ROWS - what this runner decides for itself: a holder is an actor,
    every load has an id, the data root is handed in and follows a rebind,
    orphans come from the record and a reclaim proves identity first. Marked
    `Native` in the class name.
"""
from __future__ import annotations

import ast
import contextlib
import inspect
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _suites import asked_again, killed, tally                  # noqa: E402
import runner                                                   # noqa: E402
import runner.manager as M                                      # noqa: E402
import _suites                                                  # noqa: E402
_suites.forbid_the_card()        # a suite never asks the real card (nvidia-smi)
from runner import (Backend, BackendError, Cancelled, Config,   # noqa: E402
                    GenParams, Lease, LeaseHeld, MockBackend,
                    ModelManager, max_concurrency, orphaned_loads,
                    read_events, strip_thinking)
from runner.manager import (LOAD_BASELINE_MIN, LOAD_SAMPLES_KEPT,  # noqa: E402
                            LOAD_SLOW_FACTOR, LOAD_SLOW_FLOOR_S,
                            SLOW_LOAD_WARNING, is_ours, load_baseline,
                            load_bucket, load_is_slow, note_load, read_loads)
from runner.cardlock import MachineCard                         # noqa: E402

# the pipeline (S6): its refusals and a request's terms. Looked up by name so
# the rest of this suite runs whether or not the manager has them yet
PipelineRefused = getattr(M, "PipelineRefused", RuntimeError)
Terms = getattr(M, "Terms", None)

import ids                                                      # noqa: E402  (the manager put the contract on the path)
import validate as V                                            # noqa: E402


def _no_kill(pid):
    raise AssertionError(f"this suite kills nothing, and something reached a "
                         f"real kill of pid {pid}")


M.kill_pid = _no_kill

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

MANAGER_SRC = (HERE / "runner" / "manager.py").read_text(encoding="utf-8")

W = "srv-w5r8t2hh"          # the holder these rows load under
X = "srv-k3x9m2pq"          # somebody else
SETTINGS = """
[runner]
backend = "mock"

[mock]
tokens_per_second = 0
load_seconds = 0
unload_seconds = 0

[models.alpha]
label = "Alpha"

[models.beta]
label = "Beta"
"""


# ------------------------------------------------------------------ doubles

class Clock:
    """A clock that only moves when a row moves it. A lease row that ran on
    `time.time()` granted twenty-four times in a tight loop and could not tell
    a `since` that reset on every grant from one that held: the clock did not
    move either way. The movement is the assertion."""

    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, seconds=60.0):
        self.t += seconds
        return self.t


class FakeBackend(Backend):
    """Enough backend to be loaded, reused, stopped and killed underneath."""
    kind = "fake"

    def __init__(self, key, *, alive=True, fail=None, stop_fails=None, on_start=None):
        super().__init__(key, key)
        self.alive, self.fail, self.stop_fails = alive, fail, stop_fails
        self.on_start = on_start
        self.starts = self.stops = 0

    def start(self):
        self.starts += 1
        if self.on_start is not None:
            self.on_start(self)
        if self.fail:
            raise BackendError(self.fail)
        # what a real server has once it is up; no process of that number is
        # ever asked about or touched by this suite
        self.pid, self.port = 40000 + self.starts, 50000 + self.starts

    def stop(self):
        self.stops += 1
        if self.stop_fails:
            raise RuntimeError(self.stop_fails)

    def is_alive(self):
        return self.alive


class RecordingLease(Lease):
    """A real lease that writes down the ORDER it was asked in: grant before
    load, release before re-grant on a swap - asserted on the sequence, never
    on a clock."""

    def __init__(self, clock):
        super().__init__(clock)
        self.calls = []

    def check(self, holder):
        out = super().check(holder)
        self.calls.append("check")
        return out

    def grant(self, holder, model_key=None):
        super().grant(holder, model_key)
        self.calls.append("grant")

    def release(self):
        held = super().release()
        self.calls.append("release")
        return held


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="llmb-runner-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.clock = Clock()
        self.made = []

    def config(self, text=SETTINGS, name="runner.toml") -> Config:
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return Config.load(path)

    def factory(self, **opts):
        def make(key, port):
            b = FakeBackend(key, **opts)
            self.made.append(b)
            return b
        return make

    def manager(self, cfg=None, folder="bound", *, fake=True, **kw) -> ModelManager:
        cfg = cfg or self.config()
        if fake and "backend_factory" not in kw:
            kw["backend_factory"] = self.factory()
        m = ModelManager(cfg, (self.tmp / folder) if folder else None,
                         clock=self.clock, **kw)
        self.addCleanup(m.shutdown)
        return m

    def events(self, m_or_path):
        path = getattr(m_or_path, "events_path", m_or_path)
        return read_events(path)

    def kinds(self, m_or_path):
        return [e["event"] for e in self.events(m_or_path)]


# ================================================= the lease, on its own ===

class TheLeaseKnowsWhoHoldsIt(Case):
    """Carried over: the lease on its own."""

    def setUp(self):
        super().setUp()
        self.lease = Lease(self.clock)

    def test_a_fresh_lease_is_held_by_nobody_and_says_None_rather_than_zero(self):
        self.assertEqual(self.lease.public(), {"holder": None, "since": None,
                                               "held_seconds": None, "model_key": None})

    def test_granting_records_who_and_when_and_what_for(self):
        self.lease.grant(W, "alpha")
        pub = self.lease.public()
        self.assertEqual((pub["holder"], pub["model_key"], pub["since"]),
                         (W, "alpha", self.clock.t))

    def test_the_same_holder_asking_again_does_not_move_since(self):
        self.lease.grant(W, "alpha")
        first = self.lease.public()["since"]
        for _ in range(24):
            self.clock.tick(50)                     # twenty minutes of generating
            self.lease.grant(W, "alpha")
        self.assertEqual(self.lease.public()["since"], first)
        self.assertEqual(self.lease.public()["held_seconds"], 1200.0)

    def test_but_since_DOES_move_when_the_hold_was_actually_broken(self):
        """The control: a `grant` that never set `since` at all would pass
        the row above."""
        self.lease.grant(W, "alpha")
        first = self.lease.public()["since"]
        self.clock.tick(300)
        self.lease.release()
        self.lease.grant(W, "alpha")
        self.assertEqual(self.lease.public()["since"], first + 300)

    def test_a_second_holder_is_refused(self):
        self.lease.grant(W, "alpha")
        with self.assertRaises(LeaseHeld):
            self.lease.grant(X, "beta")

    def test_the_refusal_names_the_holder_it_could_not_take_it_from(self):
        self.lease.grant(W, "alpha")
        with self.assertRaises(LeaseHeld) as caught:
            self.lease.check(X)
        for part in (W, "alpha", X):
            self.assertIn(part, str(caught.exception))

    def test_a_refused_check_changes_nothing(self):
        self.lease.grant(W, "alpha")
        before = self.lease.public()
        with self.assertRaises(LeaseHeld):
            self.lease.grant(X, "beta")
        self.assertEqual(self.lease.public(), before)

    def test_a_blank_holder_is_refused(self):
        for blank in ("", "   ", None):
            with self.subTest(blank=blank):
                with self.assertRaises(ValueError):
                    self.lease.grant(blank, "alpha")
        self.assertIsNone(self.lease.public()["holder"])

    def test_a_blank_holder_is_refused_by_check_too(self):
        with self.assertRaises(ValueError):
            self.lease.check("")

    def test_a_padded_id_is_refused_so_one_holder_is_never_two(self):
        """CHANGED SHAPE. The existing application STRIPPED the name, so
        `" app-worker "` and `"app-worker"` were one holder. An actor id is exact - the
        contract refuses a padded one - so here the padding is refused, which
        closes the same hole from the other side."""
        self.lease.grant(W, "alpha")
        since = self.lease.public()["since"]
        for padded in (f"  {W}  ", f"{W}\n"):
            with self.subTest(padded=padded):
                with self.assertRaises(ValueError):
                    self.lease.grant(padded, "alpha")
        self.assertEqual((self.lease.public()["holder"], self.lease.public()["since"]),
                         (W, since))

    def test_release_lets_go_and_says_whether_it_held_anything(self):
        self.lease.grant(W, "alpha")
        self.assertIs(self.lease.release(), True)
        self.assertEqual(self.lease.public()["holder"], None)
        self.assertEqual(self.lease.public()["held_seconds"], None)

    def test_releasing_an_unheld_lease_is_not_an_error(self):
        self.assertIs(self.lease.release(), False)

    def test_after_a_release_somebody_else_can_take_it(self):
        self.lease.grant(W, "alpha")
        self.lease.release()
        self.lease.grant(X, "beta")
        self.assertEqual(self.lease.public()["holder"], X)

    def test_held_seconds_is_computed_here_rather_than_by_the_reader(self):
        self.lease.grant(W, "alpha")
        self.clock.tick(292.4)
        self.assertEqual(self.lease.public()["held_seconds"], 292.4)

    def test_held_seconds_never_goes_backwards_on_a_clock_that_does(self):
        self.lease.grant(W, "alpha")
        self.clock.tick(-5)
        self.assertEqual(self.lease.public()["held_seconds"], 0.0)


class NativeHolderIsAnActorRows(Case):
    """D1: the holder is the server's actor id - the coordinator's rule, kept
    here too. A name can be shared; an actor cannot."""

    def test_a_name_that_is_not_an_actor_id_is_refused(self):
        lease = Lease(self.clock)
        for name in ("app-worker", "srv-short", "SRV-W5R8T2HH", "srv-w5r8t2h1",
                     "job-w5r8t2hh", 7, ["srv-w5r8t2hh"]):
            with self.subTest(name=name):
                with self.assertRaises(ValueError) as caught:
                    lease.check(name)
                self.assertIn("actor id", str(caught.exception))
        self.assertIsNone(lease.public()["holder"])

    def test_every_actor_role_may_hold_it(self):
        for role in ids.ROLES:
            lease = Lease(self.clock)
            who = ids.mint_actor(role)
            lease.grant(who, "alpha")
            self.assertEqual(lease.public()["holder"], who)


# ============================================ the manager takes the lease ===

class TheManagerTakesTheLeaseOnAcquire(Case):
    """Carried over: the manager takes the lease on acquire - on a REAL
    manager, which the existing application could only reach through a
    stand-in `self`."""

    def test_acquiring_grants_the_lease_to_the_named_holder(self):
        m = self.manager()
        m.acquire("alpha", W)
        self.assertEqual((m.status()["lease"]["holder"], m.status()["lease"]["model_key"]),
                         (W, "alpha"))

    def test_the_hold_is_taken_BEFORE_the_weights_go_up(self):
        """The load is the window that matters - minutes for a cold model -
        and a lease granted after `start()` returned would report the card
        free for exactly that window."""
        seen = []
        m = self.manager(backend_factory=self.factory(
            on_start=lambda b: seen.append((m.status(), m.loading))))
        m.acquire("alpha", W)
        self.assertEqual(len(seen), 1, "start() was never reached")
        mid, loading = seen[0]
        self.assertEqual(mid["lease"]["holder"], W, "the card read as free mid-load")
        self.assertEqual(mid["lease"]["model_key"], "alpha")
        self.assertIsNone(mid["loaded"], "the load had not finished")
        self.assertEqual((mid["loading"], loading), ("alpha", True))
        self.assertEqual((m.status()["loading"], m.loading), (None, False),
                         "a finished load still reads as loading")

    def test_a_load_that_FAILED_holds_nothing(self):
        m = self.manager(backend_factory=self.factory(fail="llama-server did not come up"))
        with self.assertRaises(BackendError):
            m.acquire("alpha", W)
        st = m.status()
        self.assertIsNone(st["lease"]["holder"])
        self.assertIsNone(st["loaded"])
        self.assertIsNone(st["loading"])
        self.assertIn("load_failed", self.kinds(m))

    def test_a_REFUSED_VRAM_CHECK_holds_nothing_either(self):
        """CHANGED ON PURPOSE (S7): the fit check is asked as room is made for
        the load, before any backend is made - so a refused load makes
        nothing, where it once made a backend it never started."""
        m = self.manager()

        def refuse(_key):
            raise BackendError("not enough free VRAM ... 3.9 GB free")
        m._check_vram = refuse
        with self.assertRaises(BackendError):
            m.acquire("alpha", W)
        self.assertIsNone(m.status()["lease"]["holder"])
        self.assertEqual(self.made, [], "a refused load made a backend")

    def test_the_load_error_is_what_reaches_the_caller_not_a_lease_error(self):
        m = self.manager()

        def refuse(_key):
            raise BackendError("not enough free VRAM ... 3.9 GB free")
        m._check_vram = refuse
        with self.assertRaises(BackendError) as caught:
            m.acquire("alpha", W)
        self.assertIn("VRAM", str(caught.exception))

    def test_a_warm_reuse_keeps_the_hold_it_already_had(self):
        m = self.manager()
        m._lease = RecordingLease(self.clock)
        first = m.acquire("alpha", W)
        since = m.status()["lease"]["since"]
        self.clock.tick(120)
        m._lease.calls.clear()
        got = m.acquire("alpha", W)
        self.assertIs(got, first)
        self.assertEqual(m.status()["lease"]["since"], since)
        self.assertNotIn("release", m._lease.calls)

    def test_unloading_releases_the_hold(self):
        m = self.manager()
        b = m.acquire("alpha", W)
        m.unload()
        self.assertIsNone(m.status()["lease"]["holder"])
        self.assertEqual(b.stops, 1)

    def test_a_stop_that_THROWS_still_releases(self):
        m = self.manager(backend_factory=self.factory(stop_fails="the process would not die"))
        m.acquire("alpha", W)
        with self.assertRaises(RuntimeError):
            m.unload()
        self.assertIsNone(m.status()["lease"]["holder"])
        self.assertIsNone(m.status()["loaded"])

    def test_unloading_when_nothing_was_loaded_does_not_raise(self):
        m = self.manager()
        m.unload()
        self.assertIsNone(m.status()["lease"]["holder"])

    def test_a_model_BESIDE_keeps_the_hold_it_already_had(self):
        """CHANGED ON PURPOSE (S7; was "a model SWAP releases and takes the
        lease again"): a second model goes up beside the first, nothing comes
        off, so the hold is never released - asked before anything moves and
        again under the load lock, granted once before beta's weights go up
        (the grant asks once more itself), and held since the first load."""
        m = self.manager()
        m._lease = RecordingLease(self.clock)
        m.acquire("alpha", W)
        since = m.status()["lease"]["since"]
        self.clock.tick(60)
        m._lease.calls.clear()
        m.acquire("beta", W)
        self.assertEqual(m._lease.calls, ["check", "check", "check", "grant"])
        self.assertEqual((m.status()["lease"]["model_key"], m.status()["lease"]["since"]),
                         ("beta", since))

    def test_a_SECOND_holder_is_refused_before_anything_is_unloaded(self):
        """The hole `check` exists to close: the unload releases the lease, so
        a refusal that lived only at the grant would let a second holder
        EVICT the first and then take the card cleanly."""
        m = self.manager()
        loaded = m.acquire("alpha", W)
        with self.assertRaises(LeaseHeld):
            m.acquire("beta", X)
        self.assertEqual(loaded.stops, 0, "somebody else's model was unloaded")
        self.assertEqual(m.status()["loaded"], "alpha")
        self.assertEqual(m.status()["lease"]["holder"], W)

    def test_a_second_holder_cannot_reuse_the_warm_one_either(self):
        m = self.manager()
        m.acquire("alpha", W)
        with self.assertRaises(LeaseHeld):
            m.acquire("alpha", X)

    def test_an_anonymous_acquire_is_refused_before_anything_moves(self):
        """The carried shape: a model on the card and NOBODY holding it, so
        no second holder is there to refuse - only the blank name is."""
        m = self.manager()
        loaded = m.acquire("alpha", W)
        m._lease.release()
        with self.assertRaises(ValueError):
            m.acquire("beta", "")
        self.assertEqual(loaded.stops, 0)
        self.assertEqual(m.status()["loaded"], "alpha")


class NativeLeaseDoorRows(Case):
    """Two doors the carried rows cannot reach: the warm path takes the lease
    too, and a live count that drifted is refused before anything is taken."""

    def test_a_warm_reuse_takes_the_lease_when_nobody_held_it(self):
        """There is no way to USE the card without holding it: the warm path
        grants too. In every ordinary case the grant is a no-op - which is why
        only this state, a model up and nobody holding it, can show it."""
        m = self.manager()
        b = m.acquire("alpha", W)
        m._lease.release()
        self.assertIs(m.acquire("alpha", W), b)
        self.assertEqual(m.status()["lease"]["holder"], W)

    def test_no_second_load_of_a_model_the_table_holds(self):
        """CHANGED ON PURPOSE (S7; was "nothing loads while anything is still
        live"): models go up side by side, so a live count says nothing any
        more - the rule is one load of each model. Unreachable while the table
        keeps itself - which is why it is a guard: a table that drifted
        (a load of alpha under way that nothing is loading) would otherwise
        put a second load of alpha on the card. Refused before the lease is
        taken and before a backend exists; another model still loads."""
        m = self.manager()
        m._loads.begin("load-zzzzzzzz", "alpha", FakeBackend("alpha"), None)   # drifted
        with self.assertRaises(M.ConcurrencyViolation) as caught:
            m.acquire("alpha", W)
        self.assertIn("load-zzzzzzzz", str(caught.exception))
        self.assertEqual((self.made, m.status()["lease"]["holder"]), ([], None))
        m.acquire("beta", W)
        self.assertEqual([b.model_key for b in self.made], ["beta"])


class NativeUnknownModelRows(Case):
    def test_a_model_nobody_configured_is_refused_before_anything_moves(self):
        """CHANGED BEHAVIOUR, on purpose. The existing `acquire` unloaded the
        model on the card and only THEN failed to build the unknown one, so a
        misspelt key cost the loaded model. Here the key is checked with the
        holder, before anything moves."""
        m = self.manager()
        loaded = m.acquire("alpha", W)
        with self.assertRaises(KeyError) as caught:
            m.acquire("alhpa", W)
        self.assertIn("alpha, beta", str(caught.exception))
        self.assertEqual(loaded.stops, 0)
        self.assertEqual(m.status()["loaded"], "alpha")
        self.assertEqual(m.status()["lease"]["holder"], W)


class HealthSaysWhoHoldsTheCard(Case):
    """Carried over: health says who holds the card."""

    def test_status_carries_the_lease(self):
        m = self.manager()
        m.acquire("alpha", W)
        self.clock.tick(3)
        lease = m.status()["lease"]
        self.assertEqual((lease["holder"], lease["model_key"], lease["held_seconds"]),
                         (W, "alpha", 3.0))
        self.assertIsInstance(lease["since"], float)

    def test_an_unheld_card_reports_None_rather_than_a_missing_key(self):
        m = self.manager()
        self.assertEqual(m.status()["lease"], {"holder": None, "since": None,
                                               "held_seconds": None, "model_key": None})

    def test_the_lease_names_the_model_while_loaded_is_still_None(self):
        m = self.manager()
        m._lease.grant(W, "alpha")
        st = m.status()
        self.assertIsNone(st["loaded"])
        self.assertEqual(st["lease"]["model_key"], "alpha")


def _manager_fn(name, cls=None):
    tree = ast.parse(MANAGER_SRC)
    scope = tree
    if cls:
        scope = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    return next(n for n in ast.walk(scope)
                if isinstance(n, ast.FunctionDef) and n.name == name)


class TheSeamIsWhereThePlanSaysItIs(Case):
    """Carried over: the seam is where it belongs. The rows that pin a DOOR SHUT stay
    source rows - behaviour cannot say "there is no other way in" - and each
    is paired with the behaviour it protects."""

    def test_acquire_requires_a_holder_and_has_no_default_for_it(self):
        sig = inspect.signature(ModelManager.acquire)
        self.assertIs(sig.parameters["holder"].default, inspect.Parameter.empty)

    def test_the_idle_sweep_releases_through_the_stops(self):
        """CHANGED ON PURPOSE (S7; was "...through unload"): the sweep takes
        each idle model off through the one stop every model comes off by
        (`_stop`), which lets the hold go with the last model - never by
        touching the lease itself."""
        code = ast.unparse(_manager_fn("maybe_idle_unload"))
        self.assertIn("self._stop(", code)
        self.assertNotIn("_lease", code)

    def test_the_RECLAIM_grants_nothing(self):
        code = ast.unparse(_manager_fn("startup_check"))
        self.assertNotIn("_lease.grant", code)
        self.assertNotIn("_lease.release", code)

    def test_the_lease_is_granted_in_two_places_and_released_in_one(self):
        """CHANGED ON PURPOSE (S7; was "...in exactly two methods"): acquire is
        split in three - a reuse (`_warm`) grants, a load (`_load`) grants
        before its weights go up - and the hold ends in one place
        (`_release_lease_if_empty`), once the last load has left the table:
        models side by side hold the card together."""
        cls = next(n for n in ast.parse(MANAGER_SRC).body
                   if isinstance(n, ast.ClassDef) and n.name == "ModelManager")
        holders = {}
        for fn in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
            body = ast.unparse(fn)
            if "_lease.grant" in body or "_lease.release" in body:
                holders[fn.name] = body
        self.assertEqual(sorted(holders), ["_load", "_release_lease_if_empty", "_warm"])
        self.assertEqual((holders["_warm"].count("_lease.grant("),
                          holders["_load"].count("_lease.grant(")), (1, 1))
        self.assertEqual(holders["_release_lease_if_empty"].count("_lease.release()"), 1)
        self.assertNotIn("_lease.release", holders["_warm"] + holders["_load"])

    def test_the_audit_log_gains_no_lease_line(self):
        """The lease's live answer is `status()`; a durable one is a future
        consumer's decision, not this runner's."""
        for node in ast.walk(ast.parse(MANAGER_SRC)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_event" and node.args
                    and isinstance(node.args[0], ast.Constant)):
                self.assertNotIn("lease", str(node.args[0].value))

    def test_the_seam_is_exported_from_the_package(self):
        for name in ("Lease", "LeaseHeld", "ModelManager"):
            with self.subTest(name=name):
                self.assertIn(name, runner.__all__)
                self.assertTrue(hasattr(runner, name))

    def test_nothing_books_schedules_or_queues_the_card(self):
        """SHAPE ONLY. Waiting holders, priorities, fairness: those are the
        coordinator's, across processes, and not this class's."""
        cls = next(n for n in ast.parse(MANAGER_SRC).body
                   if isinstance(n, ast.ClassDef) and n.name == "Lease")
        self.assertEqual(sorted(n.name for n in cls.body if isinstance(n, ast.FunctionDef)),
                         ["__init__", "_held_for", "check", "grant", "public", "release"])


# ============================================== a warm backend may be dead ===

class TheWarmPathChecksLiveness(Case):
    """Carried over: a warm backend is not necessarily a live one. The
    existing application measured the corpse three times in one night -
    status reporting a model with no process behind it, every generation
    failing, and no recovery short of a restart."""

    def seed(self, alive):
        m = self.manager()
        b = m.acquire("alpha", W)
        b.alive = alive
        self.made.clear()
        return m, b

    def test_a_LIVE_warm_backend_is_still_reused(self):
        m, live = self.seed(True)
        self.assertIs(m.acquire("alpha", W), live)
        self.assertEqual(self.made, [], "a live warm backend was reloaded")
        self.assertIn("reuse", self.kinds(m))

    def test_a_DEAD_warm_backend_is_NOT_handed_out(self):
        m, dead = self.seed(False)
        self.assertIsNot(m.acquire("alpha", W), dead)

    def test_and_it_RELOADS_rather_than_raising(self):
        m, _ = self.seed(False)
        got = m.acquire("alpha", W)
        self.assertEqual(len(self.made), 1, "no fresh backend was loaded")
        self.assertIs(got, self.made[0])
        self.assertEqual(got.starts, 1)

    def test_the_dead_one_is_unloaded_so_the_live_count_stays_honest(self):
        m, dead = self.seed(False)
        m.acquire("alpha", W)
        self.assertEqual(dead.stops, 1, "the dead backend was never stopped")
        self.assertEqual(m._live, 1, "one live backend, not two and not zero")
        self.assertEqual(max_concurrency(self.events(m)), 1)

    def test_it_SAYS_it_reloaded_rather_than_doing_it_silently(self):
        m, dead = self.seed(False)
        m.acquire("alpha", W)
        stale = [e for e in self.events(m) if e["event"] == "stale_backend"]
        self.assertEqual(len(stale), 1, self.kinds(m))
        self.assertEqual(stale[0]["load"], dead.load_id)

    def test_a_DIFFERENT_model_goes_up_beside_it(self):
        """CHANGED ON PURPOSE (S7; was "a different model still swaps as
        before"): the live one stays, and the other goes up beside it."""
        m, old = self.seed(True)
        got = m.acquire("beta", W)
        self.assertIsNot(got, old)
        self.assertEqual(old.stops, 0, "a different model took the first off")
        self.assertEqual(m.status()["loaded"], "beta", "not the one used last")
        self.assertEqual([x["key"] for x in m.models()["models"] if x["loaded"]],
                         ["alpha", "beta"])

    def test_with_warm_keep_OFF_nothing_changes_either(self):
        m, old = self.seed(True)
        m.cfg.runner["warm_keep"] = False
        self.assertIsNot(m.acquire("alpha", W), old, "warm_keep off must still reload")


class TheDefaultIsTheSafeDirection(Case):
    def test_a_backend_that_cannot_answer_reports_ALIVE(self):
        """A wrong TRUE costs one failed generation with a clear error; a
        wrong FALSE costs a reload of the whole model."""
        class Bare(Backend):
            pass
        self.assertIs(Bare("m").is_alive(), True)


# ============================================= one model, and the record ===

class ManagerRows(Case):
    """Carried over: models on the stand-in - side by side since S7, one
    load of each."""

    def test_models_side_by_side_with_warm_keep(self):
        """CHANGED ON PURPOSE (S7; was "one model at a time with warm_keep"):
        alpha, alpha, beta, alpha loads twice - beta beside alpha, which is
        reused - where it once loaded three times, swapping. Never two loads
        of one model; two models side by side."""
        m = self.manager(fake=False)
        for key in ("alpha", "alpha", "beta", "alpha"):
            m.acquire(key, W)
        m.shutdown()
        events = self.events(m)
        self.assertEqual(max_concurrency(events), 1)
        self.assertEqual(runner.side_by_side(events), 2)
        self.assertEqual(sum(e["event"] == "load_ok" for e in events), 2)
        self.assertEqual(sum(e["event"] == "reuse" for e in events), 2)

    def test_a_session_boundary_resets_the_count_and_flags_the_orphan(self):
        m1 = self.manager(fake=False)
        m1.acquire("alpha", W)                     # never unloaded: a killed process
        m2 = self.manager(fake=False)              # a fresh process, the same folder
        m2.acquire("beta", W)
        m2.shutdown()
        events = self.events(m2)
        self.assertEqual(max_concurrency(events), 1)
        orphans = orphaned_loads(events)
        self.assertEqual([o["model_key"] for o in orphans], ["alpha"])


class ManagerSurfaceRows(Case):
    """Carried over: the manager's facts are answered
    through `status()`."""

    def test_the_load_time_is_answered_through_status(self):
        # a MEASURABLE load: at 0.0 s both sides round to 0.0 and the row
        # would pass on a manager that never wrote the time down
        cfg = self.config()
        cfg.mock["load_seconds"] = 0.05
        m = self.manager(cfg, fake=False)
        m.acquire("alpha", W)
        st = m.status()
        self.assertEqual(st["loaded"], "alpha")
        self.assertGreaterEqual(st["load_seconds"], 0.05, st)
        self.assertEqual(st["loads_this_session"], 1)


class IdleSweepRows(Case):
    """Carried over: the idle sweep. The existing application watched the model
    leave the card in real time; here the clock is the row's, so the window
    is crossed exactly and nothing sleeps. (That the SERVER's loop calls the
    sweep is the wiring slice's row.)"""

    def test_an_idle_model_leaves_the_card_and_takes_the_lease_with_it(self):
        m = self.manager()
        m.cfg.runner["idle_unload_s"] = 600
        m.acquire("alpha", W)
        self.clock.tick(600)
        self.assertIs(m.maybe_idle_unload(), True)
        st = m.status()
        self.assertIsNone(st["loaded"])
        self.assertIsNone(st["lease"]["holder"], st["lease"])
        sweeps = [e for e in self.events(m) if e["event"] == "idle_unload"]
        self.assertEqual(len(sweeps), 1, sweeps)
        self.assertEqual(sweeps[0]["idle_seconds"], 600.0)

    def test_a_model_inside_its_idle_window_is_left_alone(self):
        """The control: a sweep that unloaded unconditionally would pass the
        row above exactly as well."""
        m = self.manager()
        m.cfg.runner["idle_unload_s"] = 600
        m.acquire("alpha", W)
        self.clock.tick(599)
        self.assertIs(m.maybe_idle_unload(), False)
        self.assertEqual(m.status()["loaded"], "alpha")
        self.assertEqual(m.status()["lease"]["holder"], W)
        self.assertNotIn("idle_unload", self.kinds(m))


class NativeIdleRows(Case):
    """What counts as a use, and a window that means never."""

    def test_a_use_starts_the_idle_clock_again(self):
        m = self.manager()
        m.cfg.runner["idle_unload_s"] = 600
        m.acquire("alpha", W)
        self.clock.tick(500)
        m.touch()
        self.clock.tick(500)                        # 1000 s since the load, 500 since the use
        self.assertIs(m.maybe_idle_unload(), False)
        self.assertEqual(m.status()["loaded"], "alpha")

    def test_a_reuse_starts_the_idle_clock_again(self):
        m = self.manager()
        m.cfg.runner["idle_unload_s"] = 600
        m.acquire("alpha", W)
        self.clock.tick(500)
        m.acquire("alpha", W)                       # a warm reuse is a use
        self.clock.tick(500)
        self.assertIs(m.maybe_idle_unload(), False)
        self.assertEqual(m.status()["loaded"], "alpha")

    def test_a_window_of_zero_never_unloads(self):
        m = self.manager()
        m.cfg.runner["idle_unload_s"] = 0
        m.acquire("alpha", W)
        self.clock.tick(10 ** 6)
        self.assertIs(m.maybe_idle_unload(), False)
        self.assertEqual(m.status()["loaded"], "alpha")


# =========================================================== the stand-in ===

class MockRows(Case):
    """Carried over: the stand-in model. The existing application's also wrote
    to a word budget and echoed section headings from the prompt: that was
    that application's own format, and it stays behind."""

    def mock(self, key="alpha", **over):
        cfg = dict(self.config().mock, **over)
        return MockBackend(key, key, cfg)

    def test_the_stand_in_streams_and_reports_a_natural_stop(self):
        events = list(self.mock().stream("Write a scene.", GenParams(max_tokens=5000)))
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["finish_reason"], "stop")
        self.assertGreater(sum(e["type"] == "token" for e in events), 10)

    def test_the_stand_in_reports_length_when_it_hits_the_cap(self):
        events = list(self.mock().stream("Write a scene.", GenParams(max_tokens=12)))
        self.assertEqual(events[-1]["finish_reason"], "length")
        self.assertEqual(sum(e["type"] == "token" for e in events), 12)
        self.assertEqual(events[-1]["usage"]["completion_tokens"], 12)

    def test_the_stand_in_reads_max_tokens_as_llama_server_reads_it(self):
        """S10b RG2: -1 names no limit, and 0 reads the prompt and writes
        nothing (llama-server's README) - the stand-in model read both as one
        token, the misreading S10b took out of the llama.cpp stand-in."""
        def answer(most):
            events = list(self.mock().stream("Write a scene.", GenParams(max_tokens=most)))
            return ([e["text"] for e in events if e["type"] == "token"],
                    events[-1]["finish_reason"], events[-1]["usage"]["completion_tokens"])
        whole, why, n = answer(5000)
        self.assertEqual((why, n == len(whole), len(whole) > 10), ("stop", True, True))
        self.assertEqual(answer(-1), (whole, "stop", len(whole)), "-1 was read as a limit")
        self.assertEqual(answer(0), ([], "length", 0), "0 wrote something")
        self.assertEqual(answer(3), (whole[:3], "length", 3))

    def test_an_injected_failure_raises_BackendError(self):
        with self.assertRaises(BackendError):
            list(self.mock(fail_models=["alpha"]).stream("anything", GenParams()))

    def test_cancellation_stops_the_stream(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            list(self.mock().stream("Write something long.", GenParams(max_tokens=500), cancel))

    def test_thinking_is_stripped_including_an_unterminated_block(self):
        self.assertEqual(strip_thinking("<think>plan</think>Answer."), "Answer.")
        self.assertEqual(strip_thinking("Answer.<think>cut off"), "Answer.")
        self.assertEqual(strip_thinking("<THINK>x</THINK> hi"), "hi")
        self.assertEqual(strip_thinking("no tags"), "no tags")


class NativeMockRows(Case):
    def mock(self, key="alpha", **over):
        return MockBackend(key, key, dict(self.config().mock, **over))

    def text(self, backend, prompt, **gen):
        return "".join(e["text"] for e in backend.stream(prompt, GenParams(max_tokens=5000, **gen))
                       if e["type"] == "token")

    def test_the_stand_in_counts_a_request_against_its_models_context(self):
        """S16a R1: the stand-in counted nothing, so `prompt_too_long` never
        happened on it and its `usage.prompt_tokens` ran past its context. It
        counts now as it answers - each word of what it is asked is a token,
        the number its `usage` says - against the context its settings give
        the model (`ctx`). A transcription is not counted: its length is its
        audio."""
        b = MockBackend("alpha", "alpha", self.config().mock, ctx=64)
        asked = [{"role": "system", "content": "Be brief."},
                 {"role": "user", "content": "Tell me about the rain on the roof."}]
        got = b.count(asked, GenParams(max_tokens=5))
        done = [e for e in b.stream(asked, GenParams(max_tokens=5)) if e["type"] == "done"][0]
        self.assertEqual(got, {"prompt_tokens": done["usage"]["prompt_tokens"], "context": 64})
        self.assertGreater(got["prompt_tokens"], 0)
        ears = MockBackend("ears", "ears", self.config().mock, engine="whisper", ctx=64)
        self.assertIsNone(ears.count({"audio": "x.wav", "fields": {}}, GenParams(max_tokens=5)))

    def test_the_same_request_gives_the_same_text_and_a_different_one_does_not(self):
        """Compared WITHOUT the first line: it names the model, so two models'
        replies always differed there - and the row was green with the model
        taken out of what the reply is drawn from (found by the sweep)."""
        def body(text):
            head, rest = text.split("\n\n", 1)
            self.assertTrue(head.startswith("(stand-in "), head)
            return rest
        a = body(self.text(self.mock(), "Tell me about rain.", seed=3))
        self.assertEqual(a, body(self.text(self.mock(), "Tell me about rain.", seed=3)))
        self.assertNotEqual(a, body(self.text(self.mock(), "Tell me about snow.", seed=3)))
        self.assertNotEqual(a, body(self.text(self.mock(), "Tell me about rain.", seed=4)))
        self.assertNotEqual(a, body(self.text(self.mock("beta"), "Tell me about rain.", seed=3)))

    def test_it_says_no_model_was_involved(self):
        self.assertIn("no model was involved", self.text(self.mock(), "hello"))

    def test_the_fail_marker_fails_and_fail_first_for_fails_exactly_once(self):
        with self.assertRaises(BackendError):
            list(self.mock().stream("please [[MOCK_FAIL]]", GenParams()))
        key = "once-" + ids.mint_actor("load")[5:]      # a key no other row has spent
        flaky = self.mock(key, fail_first_for=key)
        with self.assertRaises(BackendError):
            list(flaky.stream("first", GenParams()))
        self.assertTrue(self.text(flaky, "second"))

    def test_a_model_named_in_fail_load_models_fails_its_load_and_says_so(self):
        """S14b R3: a load that fails, from the settings alone - so a model's
        `failed` state, and its why, can be shown by the stand-in server from
        its command line. The load takes its time first, as a load that fails
        at its end does; a model the list does not name loads."""
        broken = self.mock("alpha", fail_load_models=["alpha"], load_seconds=0.2)
        began = time.monotonic()
        with self.assertRaises(BackendError) as caught:
            broken.start()
        self.assertGreaterEqual(time.monotonic() - began, 0.2, "it failed before its load time")
        self.assertIn("fail_load_models", str(caught.exception))
        self.assertIn("alpha", str(caught.exception))
        self.assertFalse(broken.started)
        fine = self.mock("beta", fail_load_models=["alpha"], load_seconds=0)
        fine.start()
        self.assertTrue(fine.started)

    def test_the_think_marker_thinks_first(self):
        events = list(self.mock().stream("[[MOCK_THINK]] go", GenParams(max_tokens=5000)))
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds[0], "reasoning")
        self.assertLess(kinds.index("reasoning"), kinds.index("token"))
        text = "".join(e["text"] for e in events if e["type"] == "token")
        self.assertTrue(text.startswith("<think>"), text[:40])
        self.assertTrue(strip_thinking(text).startswith("(stand-in alpha"))

    def test_the_last_call_is_the_exact_request_body(self):
        b = self.mock()
        params = GenParams(temperature=0.3, seed=9, max_tokens=7)
        list(b.stream("hi", params))
        self.assertEqual(b.last_call, {"model": "alpha",
                                       "messages": [{"role": "user", "content": "hi"}],
                                       **params.as_body()})

    def test_a_conversation_is_sent_as_it_was_built_and_all_of_it_is_answered(self):
        """Messages with roles: the stand-in sends a conversation as it
        came, and answers the whole of it - a system message changes the
        answer, as it would a model's."""
        def body(text):
            return text.split("\n\n", 1)[1]
        talk = [{"role": "system", "content": "Answer as a sailor would."},
                {"role": "user", "content": "Tell me about rain."}]
        b = self.mock()
        told = self.text(b, talk, seed=3)
        self.assertEqual(b.last_call["messages"], talk)
        self.assertNotEqual(body(told), body(self.text(self.mock(), "Tell me about rain.", seed=3)),
                            "the system message was not part of what it answered")
        self.assertEqual(body(self.text(self.mock(), [talk[1]], seed=3)),
                         body(self.text(self.mock(), "Tell me about rain.", seed=3)),
                         "one user message is the prompt it always was")

    def test_a_prompt_is_answered_from_its_own_words_as_it_always_was(self):
        """A prompt - one user message - is what the reply is drawn from,
        word for word, as before there were conversations: no role is added
        to what it says, so no answer the stand-in gives a prompt changed.
        Read off its reply and its usage (found by the sweep: a stand-in
        that added the role to every prompt passed every row)."""
        b = self.mock()
        params = GenParams(max_tokens=5000, seed=3)
        events = list(b.stream("Tell me about rain.", params))
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "token"),
                         b.compose("Tell me about rain.", params))
        self.assertEqual(events[-1]["usage"]["prompt_tokens"], 4)

    def test_the_call_recorded_is_its_own_never_the_applications_list(self):
        """The call is kept as it was made: an application that goes on to
        change its conversation - the next turn appended, a message edited -
        changes nothing in the record of a call already made."""
        talk = [{"role": "system", "content": "Answer as a sailor would."},
                {"role": "user", "content": "Tell me about rain."}]
        b = self.mock()
        self.text(b, talk)
        talk[1]["content"] = "EDITED AFTER THE CALL"
        talk.append({"role": "assistant", "content": "APPENDED AFTER THE CALL"})
        self.assertEqual([m["content"] for m in b.last_call["messages"]],
                         ["Answer as a sailor would.", "Tell me about rain."])

    def test_a_cancel_mid_stream_stops_it_there(self):
        cancel = threading.Event()
        got = []
        with self.assertRaises(Cancelled):
            for ev in self.mock().stream("Write something long.", GenParams(max_tokens=500),
                                         cancel):
                got.append(ev)
                if len(got) == 3:
                    cancel.set()
        self.assertEqual(len(got), 3)


@contextlib.contextmanager
def early_waits(fraction: float = 0.2):
    """Every timed wait the stand-in makes ends EARLY the first time it is
    made - after `fraction` of its time - as the first timed wait of a fresh
    process can on this machine (measured: `Event.wait(0.05)` ended under
    50 ms in 34 of 200 fresh processes, under 45 in 14; the second wait, and
    `time.sleep`, never). Laid on the stand-in's module alone - its
    `time.sleep`, and every Event it makes - and taken off on the way out."""
    import runner.mock as mock_module
    real_time, real_threading = mock_module.time, mock_module.threading

    class EarlyEvent(real_threading.Event):
        def __init__(self):
            super().__init__()
            self.first = True

        def wait(self, timeout=None):
            if timeout is not None and self.first:
                self.first = False
                return super().wait(timeout * fraction)
            return super().wait(timeout)

    class Time:
        def __init__(self):
            self.first = True

        def __getattr__(self, name):
            return getattr(real_time, name)

        def sleep(self, seconds):
            if self.first:
                self.first = False
                return real_time.sleep(seconds * fraction)
            return real_time.sleep(seconds)

    class Threading:
        Event = EarlyEvent

        def __getattr__(self, name):
            return getattr(real_threading, name)

    mock_module.time, mock_module.threading = Time(), Threading()
    try:
        yield
    finally:
        mock_module.time, mock_module.threading = real_time, real_threading


class NativeStandInTimeRows(Case):
    """The stand-in's timed waits take AT LEAST their time, by the monotonic
    clock. A timed wait can end before its time - the first one of a fresh
    process, on this machine - and the row that reads a 0.05 s load through
    `status()` read 0.04 (`ManagerSurfaceRows`, red under load). Here every
    wait is made to end early, on purpose."""

    def mock(self, **over):
        return MockBackend("alpha", "alpha", dict(self.config().mock, **over))

    def test_a_load_takes_at_least_its_time_when_a_wait_ends_early(self):
        with early_waits():
            b = self.mock(load_seconds=0.05)
            t0 = time.monotonic()
            b.start()
            took = time.monotonic() - t0
        self.assertGreaterEqual(took, 0.05)

    def test_the_load_time_read_through_status_is_whole_when_a_wait_ends_early(self):
        """`ManagerSurfaceRows`' row, with its condition provoked."""
        cfg = self.config()
        cfg.mock["load_seconds"] = 0.05
        m = self.manager(cfg, fake=False)
        with early_waits():
            m.acquire("alpha", W)
        self.assertGreaterEqual(m.status()["load_seconds"], 0.05, m.status())

    def test_an_unload_takes_at_least_its_time_when_a_wait_ends_early(self):
        b = self.mock(unload_seconds=0.05)
        b.start()
        with early_waits():
            t0 = time.monotonic()
            b.stop()
            took = time.monotonic() - t0
        self.assertGreaterEqual(took, 0.05)

    def test_each_token_takes_at_least_its_pace_when_a_wait_ends_early(self):
        with early_waits():
            b = self.mock(tokens_per_second=20)
            t0 = time.monotonic()
            events = list(b.stream("Write a scene.", GenParams(max_tokens=5)))
            took = time.monotonic() - t0
        self.assertEqual(sum(e["type"] == "token" for e in events), 5)
        self.assertGreaterEqual(took, 5 / 20)

    def test_a_load_given_up_still_ends_at_once(self):
        """The wait is on a deadline, and still hears a give-up: a load of
        half a minute given up after a tenth of a second ends then."""
        b = self.mock(load_seconds=30)
        threading.Timer(0.1, b.abandon, args=("the server is stopping",)).start()
        t0 = time.monotonic()
        with self.assertRaises(BackendError) as caught:
            b.start()
        self.assertLess(time.monotonic() - t0, 5.0, "a load given up waited out its time")
        self.assertIn("the server is stopping", str(caught.exception))


class GenParamsRows(unittest.TestCase):
    """Carried over: the llama-server rows - the half about the
    request body. The command line is the llama.cpp slice's."""

    def test_the_body_spells_what_llama_server_accepts(self):
        body = GenParams(temperature=0.85, min_p=0.05, seed=7, max_tokens=100).as_body()
        self.assertEqual((body["temperature"], body["min_p"], body["seed"]), (0.85, 0.05, 7))
        self.assertNotIn("repetition_penalty", body)
        self.assertEqual(GenParams(repeat_penalty=1.1).as_body()["repeat_penalty"], 1.1)

    def test_anti_repeat_sampling_travels_only_when_it_is_asked_for(self):
        # CHANGED (S10b): DRY travels in `extra`, as every parameter a job
        # names does - GenParams no longer has fields of its own for it,
        # which nothing filled since the job's parameters travel as named
        self.assertNotIn("dry_multiplier", GenParams().as_body())
        body = GenParams(extra={"dry_multiplier": 0.8, "dry_base": 1.75,
                                "dry_allowed_length": 4}).as_body()
        self.assertEqual((body["dry_multiplier"], body["dry_base"], body["dry_allowed_length"]),
                         (0.8, 1.75, 4))

    def test_dry_turned_explicitly_off_is_sent_rather_than_dropped(self):
        # 0.0 is a CHOICE, and it is falsy. CHANGED (S10b): named in `extra`
        self.assertEqual(GenParams(extra={"dry_multiplier": 0.0}).as_body()["dry_multiplier"],
                         0.0)


class NativeGenParamsRows(unittest.TestCase):
    """The template switches a request can carry, and what a request's
    length means."""

    def test_the_parameters_have_one_field_each_and_no_field_nothing_fills(self):
        """The fields are what the model's settings or the bootstrap fill in
        where a job names nothing; every other parameter travels in `extra`.
        A field nothing fills is a second way to send one parameter."""
        from dataclasses import fields
        self.assertEqual([f.name for f in fields(GenParams)],
                         ["temperature", "top_p", "min_p", "repeat_penalty", "seed",
                          "max_tokens", "chat_template_kwargs", "extra"])

    def test_the_length_a_request_gets_unless_it_names_one_is_the_bootstraps_own(self):
        """One number, the queue's (`jobqueue.GEN_DEFAULTS`), never a second
        one here: a request built without a length asks for what a job that
        names none asks for."""
        import jobqueue as Q
        self.assertEqual(GenParams().max_tokens, Q.GEN_DEFAULTS["max_tokens"])
        self.assertEqual(GenParams().as_body()["max_tokens"], 600)

    def test_the_room_an_answer_needs_is_its_length_and_never_less_than_one_token(self):
        """What the count holds a request to: the prompt and this room must fit
        the model's context. A length is its own room; -1 names no limit, and
        the answer needs room to begin; 0 writes nothing, and the prompt must
        still leave llama-server the position after it (b10724 refuses a
        prompt of the whole context)."""
        from runner.base import answer_room
        self.assertEqual([answer_room(m) for m in (600, 2, 1, 0, -1)], [600, 2, 1, 1, 1])

    def test_template_switches_travel_only_when_given(self):
        self.assertNotIn("chat_template_kwargs", GenParams().as_body())
        body = GenParams(chat_template_kwargs={"enable_thinking": False}).as_body()
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})

    def test_every_other_parameter_a_job_named_travels_as_it_was_named(self):
        """Every text parameter: what the job named beyond the model's
        own defaults goes into the body unchanged - False and 0 included,
        which are choices, not absences."""
        named = {"top_k": 20, "cache_prompt": False, "n_keep": 0, "stop": ["\n"],
                 "json_schema": {"type": "object", "required": ["verdict"]},
                 "reasoning_budget_tokens": 512}
        body = GenParams(extra=named).as_body()
        self.assertEqual({k: body.get(k) for k in named}, named)
        self.assertIs(body["cache_prompt"], False)
        self.assertNotIn("top_k", GenParams().as_body(), "a parameter nobody named is sent")


# ============================================================ the settings ===

class ConfigRows(Case):
    """Carried over: the settings."""

    def test_an_unknown_model_setting_is_rejected_rather_than_ignored(self):
        with self.assertRaises(ValueError) as caught:
            self.config('[models.x]\nlabel = "x"\nnonsense = 1\n')
        self.assertIn("nonsense", str(caught.exception))

    def test_a_missing_settings_file_is_a_readable_error(self):
        """CHANGED SHAPE: the existing application exited the process (SystemExit); a
        package must not, so it raises and names the path."""
        with self.assertRaises(FileNotFoundError) as caught:
            Config.load(self.tmp / "nope.toml")
        self.assertIn("nope.toml", str(caught.exception))


class NativeConfigRows(Case):
    """What the settings file refuses, and what no file at all gives."""

    def test_fail_load_models_names_models_of_the_settings_or_is_refused(self):
        """S14b R3: `[mock] fail_load_models` - the stand-in models whose every
        load fails - is none by default, and a list of model keys the settings
        name: a misspelt key would fail nothing, and read as a setting that
        took effect."""
        self.assertEqual(Config.default().mock["fail_load_models"], [])
        models = '\n[models.alpha]\nlabel = "Alpha"\n'
        self.assertEqual(self.config('[mock]\nfail_load_models = ["alpha"]\n' + models)
                         .mock["fail_load_models"], ["alpha"])
        for given, word in (('["alpah"]', "alpah"), ('"alpha"', "a list of model keys"),
                            ('[3]', "a list of model keys")):
            with self.subTest(given=given):
                with self.assertRaises(ValueError) as caught:
                    self.config(f"[mock]\nfail_load_models = {given}\n" + models)
                self.assertIn("fail_load_models", str(caught.exception))
                self.assertIn(word, str(caught.exception))

    def test_the_stand_in_card_is_a_setting_on_a_lock_of_its_own_or_is_refused(self):
        """S16a R2: `[mock] card_mib` gives the stand-in a card of its own -
        none by default - and `card_full_s` makes it read full, [after_s,
        for_s] from the server's start. It is watched as a real card is on
        the machine's lock, so a lock of its own is required: never the
        machine's, which another application may hold."""
        self.assertEqual((Config.default().mock["card_mib"], Config.default().mock["card_full_s"]),
                         (0, []))
        mine = (self.tmp / "stand-in.lock").as_posix()
        ok = self.config(f'[runner]\ncard_lock = "{mine}"\n[mock]\ncard_mib = 24576\n'
                         f'card_full_s = [5, 10]\n')
        self.assertEqual((ok.mock["card_mib"], ok.mock["card_full_s"]), (24576, [5, 10]))
        machines = (Path(tempfile.gettempdir()) / _CL.LOCK_NAME).as_posix()
        for text, word in (
                ("[mock]\ncard_mib = 24576\n", "card_lock"),
                (f'[runner]\ncard_lock = "{machines}"\n[mock]\ncard_mib = 24576\n',
                 "the machine's own"),
                (f'[runner]\ncard_lock = "{mine}"\n[mock]\ncard_full_s = [5, 10]\n', "card_mib"),
                (f'[runner]\ncard_lock = "{mine}"\n[mock]\ncard_mib = -1\n', "card_mib"),
                (f'[runner]\ncard_lock = "{mine}"\n[mock]\ncard_mib = 24576\ncard_full_s = [5]\n',
                 "card_full_s"),
                (f'[runner]\ncard_lock = "{mine}"\n[mock]\ncard_mib = 24576\n'
                 f'card_full_s = [5, -1]\n', "card_full_s")):
            with self.subTest(text=text):
                with self.assertRaises(ValueError) as caught:
                    self.config(text)
                self.assertIn(word, str(caught.exception))

    def test_an_unknown_section_or_setting_or_backend_is_refused(self):
        for text, word in (("[paths]\ndata_dir = 'x'\n", "paths"),
                           ("[runner]\ndata_dir = 'x'\n", "data_dir"),
                           ("[runner]\nbackend = 'vllm'\n", "vllm"),
                           ("[mock]\nspeed = 3\n", "speed"),
                           ("[defaults]\nctxx = 3\n", "ctxx")):
            with self.subTest(text=text):
                with self.assertRaises(ValueError) as caught:
                    self.config(text)
                self.assertIn(word, str(caught.exception))

    def test_no_file_is_the_stand_in_and_one_model_with_the_defaults_answering(self):
        cfg = Config.default()
        self.assertEqual(cfg.backend, "mock")
        self.assertEqual(sorted(cfg.models), ["demo"])
        self.assertEqual((cfg.mock["tokens_per_second"], cfg.mock["load_seconds"],
                          cfg.mock["unload_seconds"], cfg.mock["seed"],
                          cfg.mock["fail_models"], cfg.mock["fail_first_for"]),
                         (45.0, 1.2, 0.4, 7, [], ""))
        self.assertEqual((cfg.runner["idle_unload_s"], cfg.runner["port"],
                          cfg.runner["warm_keep"]), (600.0, 0, True))

    def test_a_session_opens_with_no_model_unless_the_settings_name_one(self):
        """PLAN-4 D0 and D6: the models a session opens with as the server
        starts - none by default - and how long an application's pipeline
        stays open unrenewed (120 s; 0 is until it is closed)."""
        cfg = Config.default()
        self.assertEqual((cfg.runner["session_models"], cfg.runner["pipeline_keep_s"]),
                         ([], 120.0))
        named = self.config(SETTINGS.replace('backend = "mock"', 'backend = "mock"\n'
                                             'session_models = ["beta"]\npipeline_keep_s = 0'),
                            name="named.toml")
        self.assertEqual((named.runner["session_models"], named.runner["pipeline_keep_s"]),
                         (["beta"], 0))
        self.assertEqual(Config.default().runner["session_models"], [],
                         "a file's session models leaked into the defaults")

    def test_a_session_model_the_settings_cannot_run_is_refused_at_the_start(self):
        # CHANGED ON PURPOSE (S7): two models are a session side by side; a
        # model named twice, and more than max_loads, are refused instead
        for line, words in (('session_models = ["gamma"]', "gamma"),
                            ('session_models = ["alpha", "alpha"]', "alpha twice"),
                            ('session_models = ["alpha", "beta"]\nmax_loads = 1',
                             "at most 1 on the card at once"),
                            ("max_loads = 0", "max_loads is a whole number from 1 to 8"),
                            ("max_loads = 9", "max_loads is a whole number from 1 to 8"),
                            ("max_loads = 2.5", "max_loads is a whole number from 1 to 8"),
                            ("max_loads = true", "max_loads is a whole number from 1 to 8"),
                            ('session_models = "alpha"', "a list of model keys"),
                            ('session_models = [1]', "a list of model keys"),
                            ("pipeline_keep_s = -1", "pipeline_keep_s"),
                            ("pipeline_keep_s = nan", "pipeline_keep_s"),
                            ("pipeline_keep_s = inf", "pipeline_keep_s"),
                            ("pipeline_keep_s = true", "pipeline_keep_s")):
            with self.subTest(line=line):
                with self.assertRaises(ValueError) as caught:
                    self.config(SETTINGS.replace('backend = "mock"', f'backend = "mock"\n{line}'),
                                name="refused.toml")
                self.assertIn(words, str(caught.exception))

    def test_a_session_of_several_models_side_by_side_is_accepted(self):
        """S7: the session the server opens as it starts may name several
        models - at most `max_loads` - loaded side by side."""
        cfg = self.config(SETTINGS.replace('backend = "mock"',
                                           'backend = "mock"\nsession_models = ["alpha", "beta"]'),
                          name="two.toml")
        self.assertEqual((cfg.runner["session_models"], cfg.runner["max_loads"]),
                         (["alpha", "beta"], 8))

    def test_a_session_of_several_models_on_one_configured_port_is_refused(self):
        """A configured port serves one load at a time, so a real runner keeps
        one model on the card at a time there (`_limit`), whatever max_loads
        says: a session of two is refused as the settings load, in words that
        say why - never a server that starts, and cannot open its own session
        (found by review: it only said so in its log). One model on the port,
        and two with no port, are sessions it can open."""
        def llama(extra, name):
            return self.config(LLAMA_SETTINGS.replace('backend = "llamacpp"',
                                                      f'backend = "llamacpp"\n{extra}'), name=name)
        with self.assertRaises(ValueError) as caught:
            llama('port = 8901\nsession_models = ["alpha", "beta"]', "one-port.toml")
        self.assertIn("session_models names 2 models", str(caught.exception))
        self.assertIn("one port for every model ([runner] port)", str(caught.exception))
        self.assertEqual(llama('port = 8901\nsession_models = ["alpha"]', "one.toml")
                         .runner["session_models"], ["alpha"])
        self.assertEqual(llama('session_models = ["alpha", "beta"]', "two.toml")
                         .runner["session_models"], ["alpha", "beta"])

    def test_defaults_apply_to_every_model_and_a_model_s_own_setting_wins(self):
        cfg = self.config("[defaults]\nctx = 4096\n[models.a]\n[models.b]\nctx = 2048\n")
        self.assertEqual((cfg.model("a").ctx, cfg.model("b").ctx), (4096, 2048))
        self.assertEqual(cfg.model("a").label, "a")

    def test_an_unknown_model_names_the_known_ones(self):
        with self.assertRaises(KeyError) as caught:
            self.config().model("gamma")
        self.assertIn("alpha, beta", str(caught.exception))

    def test_a_settings_file_cannot_name_a_data_root(self):
        for text in ("[paths]\ndata_dir = 'x'\n", "[runner]\ndata_dir = 'x'\n",
                     "[runner]\nevents_path = 'x'\n"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    self.config(text)


# ============================================= an engine per model (S8) ===

#: a whisper model's file, as whisper.cpp reads it: the ggml magic, then the
#: eleven numbers of large v3 (runner/ggml.py names them)
GGML_HEADER = (0x67676D6C, 51866, 1500, 1280, 20, 32, 448, 1280, 20, 32, 128, 1)
#: ...as `GET /api/models` says it (the contract's ggml_header): its shape in
#: words, and the eleven numbers under whisper.cpp's own names
LARGE_V3_HEADER = {"shape": "large v3", "n_vocab": 51866, "n_audio_ctx": 1500,
                   "n_audio_state": 1280, "n_audio_head": 20, "n_audio_layer": 32,
                   "n_text_ctx": 448, "n_text_state": 1280, "n_text_head": 20,
                   "n_text_layer": 32, "n_mels": 128, "ftype": 1}
WHISPER = "C:/whisper/bin/whisper-server.exe"
BIN = "D:/models/ggml-large-v3.bin"
WLOAD = "load-w7h2s9pr"
WPORT = 50133
#: a whisper-server this runner started, as the operating system shows it
WOURS = (f'"C:\\whisper\\bin\\whisper-server.exe" -m "D:\\models\\ggml-large-v3.bin" '
         f'--host 127.0.0.1 --port {WPORT} --request-path /{WLOAD}')
ENGINE_SETTINGS = SETTINGS + '\n[models.ears]\nlabel = "Ears"\nengine = "whisper"\n'


def _wav(path: Path, seconds: float = 1.5, sound: bool = True, rate: int = 16000) -> Path:
    """Mono 16-bit PCM: a square wave, or nothing but silence."""
    import struct
    import wave
    n = int(round(seconds * rate))
    step = struct.pack("<h", 1200) + struct.pack("<h", -1200)
    frames = (step * (n // 2 + 1))[: 2 * n] if sound else b"\0\0" * n
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return path


def give_up_timed(backend, wait_s: float = 0.3) -> tuple:
    """Start *backend* on a thread of its own, give its load up once it is
    under way, and time how long its `start` takes to end after that ->
    (seconds, what the start raised). Bounded: a start that never ends is
    None seconds, never a hung suite."""
    from runner import base
    got: dict = {}

    def run():
        try:
            backend.start()
            got["raised"] = None
        except BaseException as exc:                    # noqa: BLE001 - the row reads it
            got["raised"] = exc
        got["ended"] = time.monotonic()
    t = threading.Thread(target=run, daemon=True, name="give-up-timed")
    t.start()
    t.join(wait_s)
    asked = time.monotonic()
    backend.abandon("the pipeline is closing")
    t.join(base.GIVE_UP_S + 10.0)
    if "ended" not in got:
        return None, None
    return got["ended"] - asked, got["raised"]


class NativeEngineRows(Case):
    """PLAN-4 D9 and D10, an engine per model: a model names the engine it
    runs on - llama.cpp, unless it says whisper - `real` runs each model on
    its own, the stand-in answers in each engine's shape, and a process is
    proven ours by its own engine's proof. Nothing here starts a process: the
    engines' own processes are proven against their stand-in servers, in the
    llama.cpp and whisper suites."""

    def settings(self, text=ENGINE_SETTINGS, name="engines.toml") -> Config:
        return self.config(text, name=name)

    def real_settings(self) -> Config:
        return self.settings(f"[runner]\nbackend = 'real'\nllama_server = '{LLAMA}'\n"
                             f"whisper_server = '{WHISPER}'\n\n"
                             "[models.story]\nfile = 'story-12b.Q5_K_M.gguf'\n\n"
                             "[models.ears]\nengine = 'whisper'\nfile = 'ggml-large-v3.bin'\n",
                             name="real.toml")

    # ---------------------------------------------------------- the settings

    def test_a_model_that_names_no_engine_runs_on_llama_cpp_and_one_may_name_whisper(self):
        """A model that names no engine is llama.cpp's: every settings file
        written before engines were named ran its models there, and runs as
        it did. The defaults may name one for every model."""
        cfg = self.settings()
        self.assertEqual((cfg.model("alpha").engine, cfg.model("ears").engine),
                         ("llamacpp", "whisper"))
        shared = self.settings('[defaults]\nengine = "whisper"\n[models.a]\n'
                               '[models.b]\nengine = "llamacpp"\n', name="shared.toml")
        self.assertEqual((shared.model("a").engine, shared.model("b").engine),
                         ("whisper", "llamacpp"))

    def test_an_engine_off_the_list_is_refused_and_there_is_no_cpu_engine(self):
        for value in ('"cpu"', '"vllm"', '"Whisper"', "3", '""'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    self.settings(f"[models.a]\nengine = {value}\n", name="off.toml")
                said = str(caught.exception)
                self.assertIn("[models.a]", said)
                self.assertIn("llamacpp or whisper", said)
                self.assertIn("no CPU models", said)

    def test_a_setting_its_engine_never_reads_is_refused_and_a_default_passes_it_by(self):
        """A setting nothing reads is refused, never ignored (GUIDE): a whisper
        model's own `ctx` or sampler would read as one that took effect. A
        default is every model's that reads it: a whisper model passes a
        llama.cpp default by, and every llama.cpp model still has it."""
        for line in ("ctx = 4096", "temperature = 0.2", "n_gpu_layers = 40",
                     'cache_type_k = "q8_0"', "disable_thinking = true"):
            with self.subTest(line=line):
                with self.assertRaises(ValueError) as caught:
                    self.settings(f'[models.ears]\nengine = "whisper"\nfile = "e.bin"\n{line}\n',
                                  name="unread.toml")
                said = str(caught.exception)
                self.assertIn("[models.ears]", said)
                self.assertIn(line.split(" = ")[0], said)
                self.assertIn("whisper", said)
        # an engine the defaults name is the one a model's own settings are
        # held to (found by the sweep: S8M01 read the defaults' engine into
        # the model and held its settings to llama.cpp's)
        with self.assertRaises(ValueError) as caught:
            self.settings('[defaults]\nengine = "whisper"\n[models.ears]\nctx = 4096\n',
                          name="default-engine.toml")
        self.assertIn("[models.ears]: engine whisper never reads ctx", str(caught.exception))
        cfg = self.settings('[defaults]\nctx = 4096\ntemperature = 0.3\n[models.alpha]\n'
                            '[models.ears]\nengine = "whisper"\n', name="shared.toml")
        self.assertEqual((cfg.model("alpha").ctx, cfg.model("alpha").temperature), (4096, 0.3))
        fresh = runner.ModelSpec("x")
        self.assertEqual((cfg.model("ears").ctx, cfg.model("ears").temperature),
                         (fresh.ctx, fresh.temperature),
                         "a llama.cpp default reached a whisper model")

    def test_a_model_set_up_to_run_on_the_cpu_is_refused(self):
        """No CPU models (the owner's ruling, PLAN-4 D10): an application that
        wants a CPU path keeps it outside the bootstrap. Each engine's own way
        onto the CPU is refused as the settings load - llama.cpp with no layer
        on the card, or no device to put one on; whisper-server told to use
        no GPU - and a model on the card is not."""
        cpu = {"n_gpu_layers = 0": "llamacpp",
               'extra_args = ["-ngl", "0"]': "llamacpp",
               'extra_args = ["--n-gpu-layers=0"]': "llamacpp",
               'extra_args = ["--gpu-layers", "0"]': "llamacpp",
               'extra_args = ["-dev", "none"]': "llamacpp",
               'extra_args = ["--device=none"]': "llamacpp",
               'extra_args = ["-ng"]': "whisper",
               'extra_args = ["-t", "4", "--no-gpu"]': "whisper"}
        for line, engine in cpu.items():
            with self.subTest(line=line):
                with self.assertRaises(ValueError) as caught:
                    self.settings(f'[models.m]\nengine = "{engine}"\n{line}\n', name="cpu.toml")
                self.assertIn("no CPU models", str(caught.exception))
                self.assertIn("[models.m]", str(caught.exception))
        cfg = self.settings('[models.a]\nn_gpu_layers = 40\nextra_args = ["-ngl", "40", "-dev", '
                            '"CUDA0"]\n[models.b]\nengine = "whisper"\nthreads = 8\n'
                            'extra_args = ["-fa"]\n', name="card.toml")
        self.assertEqual((cfg.model("a").n_gpu_layers, cfg.model("b").threads,
                          cfg.model("b").extra_args), (40, 8, ["-fa"]))

    def test_a_whisper_models_own_flags_in_its_extra_args_are_refused_naming_the_setting(self):
        """PLAN-4 D11: the whisper engine sets its model, where it listens,
        its proof, its threads and its voice detector itself. Given again in
        `extra_args`, whisper-server would take the last one - and the
        setting would read as one that took effect when it did not. Refused
        as the settings load, saying which setting to use; llama.cpp's
        extra arguments are as they were."""
        cases = {"-m": "[models.m] file", "--model=x.bin": "[models.m] file",
                 "--host": "the runner sets it for each load",
                 "--port": "the runner sets it for each load",
                 "--request-path": "the runner sets it for each load",
                 "-t": "[models.m] threads", "--threads=8": "[models.m] threads",
                 "--vad": "[models.m] vad", "-vm": "[models.m] vad",
                 "--vad-model": "[models.m] vad"}
        for flag, said in cases.items():
            with self.subTest(flag=flag):
                with self.assertRaises(ValueError) as caught:
                    self.settings(f'[models.m]\nengine = "whisper"\nextra_args = ["-fa", '
                                  f'"{flag}", "8"]\n', name="own.toml")
                self.assertIn(f"extra_args '{flag}' is whisper's own flag", str(caught.exception))
                self.assertIn(said, str(caught.exception))
        cfg = self.settings('[models.m]\nextra_args = ["-t", "8", "--port", "1"]\n',
                            name="llama.toml")
        self.assertEqual(cfg.model("m").extra_args, ["-t", "8", "--port", "1"])

    def test_a_whisper_models_own_settings_default_as_whisper_server_and_are_held_to_sense(self):
        """Its threads (4, whisper-server 1.9.1's own default on a machine of
        four threads or more), no voice detector, the runner's start timeout,
        a failed start not tried again for 60 s, and an answer due within
        30 s and real time. A value no run could use is refused, in words; a
        llama.cpp model reads none of them."""
        spec = self.settings('[models.m]\nengine = "whisper"\n', name="d.toml").model("m")
        self.assertEqual((spec.threads, spec.vad, spec.startup_timeout_s, spec.retry_after_s,
                          spec.due_base_s, spec.due_per_audio_s), (4, "", None, 60.0, 30.0, 1.0))
        spec = self.settings('[models.m]\nengine = "whisper"\nthreads = 2\nvad = "v.bin"\n'
                             'startup_timeout_s = 90\nretry_after_s = 0\ndue_base_s = 5\n'
                             'due_per_audio_s = 0\n', name="own.toml").model("m")
        self.assertEqual((spec.threads, spec.vad, spec.startup_timeout_s, spec.retry_after_s,
                          spec.due_base_s, spec.due_per_audio_s), (2, "v.bin", 90, 0, 5, 0))
        wrong = {"threads = 0": "threads is a whole number", "threads = 2.5": "threads is a whole",
                 "threads = true": "threads is a whole", "vad = 3": "vad names a voice detector",
                 "startup_timeout_s = 0": "startup_timeout_s is a number of seconds above 0",
                 "startup_timeout_s = inf": "startup_timeout_s is a number",
                 "retry_after_s = -1": "retry_after_s is a number of seconds 0 or more",
                 "retry_after_s = nan": "retry_after_s is a number",
                 "due_base_s = 0": "due_base_s is a number of seconds above 0",
                 "due_base_s = true": "due_base_s is a number",
                 "due_per_audio_s = -0.5": "due_per_audio_s is a number of seconds 0 or more"}
        for line, said in wrong.items():
            with self.subTest(line=line):
                with self.assertRaises(ValueError) as caught:
                    self.settings(f'[models.m]\nengine = "whisper"\n{line}\n', name="bad.toml")
                self.assertIn(said, str(caught.exception))
                self.assertIn("[models.m]", str(caught.exception))
        for line in ("threads = 4", 'vad = "v.bin"', "retry_after_s = 60", "due_base_s = 30"):
            with self.subTest(llama=line):
                with self.assertRaises(ValueError) as caught:
                    self.settings(f"[models.m]\n{line}\n", name="llama.toml")
                self.assertIn("engine llamacpp never reads", str(caught.exception))

    def test_whisper_server_is_named_as_llama_server_is(self):
        self.assertEqual(Config.default().runner["whisper_server"], "whisper-server")
        cfg = self.settings('[runner]\nwhisper_server = ["py", "fake_whisper_server.py"]\n',
                            name="named.toml")
        self.assertEqual(cfg.runner["whisper_server"], ["py", "fake_whisper_server.py"])

    def test_real_runs_each_model_on_its_own_engine_and_llamacpp_runs_llama_cpp_alone(self):
        """`real` runs each model on its own engine; `llamacpp` - the real
        backend's name from before engines were named - runs every model on
        llama.cpp, so a whisper model under it is refused, naming `real`.
        Under the stand-in, what runs a model is the stand-in: nothing of it
        is on the card."""
        real = self.settings(ENGINE_SETTINGS.replace('backend = "mock"', 'backend = "real"'),
                             name="real.toml")
        self.assertEqual((real.backend, real.engine("alpha"), real.engine("ears")),
                         ("real", "llamacpp", "whisper"))
        mock = self.settings()
        self.assertEqual((mock.engine("alpha"), mock.engine("ears")), ("mock", "mock"))
        with self.assertRaises(ValueError) as caught:
            self.settings(ENGINE_SETTINGS.replace('backend = "mock"', 'backend = "llamacpp"'),
                          name="llama.toml")
        said = str(caught.exception)
        self.assertIn("[models.ears]", said)
        self.assertIn('backend = "real"', said)
        llama = self.settings(SETTINGS.replace('backend = "mock"', 'backend = "llamacpp"'),
                              name="llama-only.toml")
        self.assertEqual(llama.engine("alpha"), "llamacpp")

    # ------------------------------------------------------------ the engines

    def test_every_engine_names_its_program_its_processes_and_how_it_answers(self):
        from runner import engines as E
        from runner.config import DEFAULTS
        self.assertEqual({k: (e.program, e.names, e.answers) for k, e in E.ENGINES.items()},
                         {"llamacpp": ("llama_server", ("llama-server.exe", "llama-server"),
                                       "stream"),
                          "whisper": ("whisper_server", ("whisper-server.exe", "whisper-server"),
                                      "once")})
        for e in E.ENGINES.values():
            self.assertIn(e.program, DEFAULTS["runner"], f"{e.name}'s program is no setting")

    def test_cardwatch_sees_every_engines_processes_and_nothing_else(self):
        """The real card's instrument (cardwatch.py) knows every engine's
        process names: a whisper-server on the card is seen as a llama-server
        is (M1: "cardwatch sees whisper-server")."""
        import cardwatch
        seen = cardwatch.model_servers([(11, 1, "llama-server.exe"),
                                        (12, 1, "whisper-server.exe"), (13, 1, "python.exe"),
                                        (14, 2, "WHISPER-SERVER.EXE"), (15, 2, "llama-server"),
                                        (16, 2, "whisper-cli.exe")])
        self.assertEqual(seen, [{"pid": 11, "ppid": 1, "engine": "llamacpp"},
                                {"pid": 12, "ppid": 1, "engine": "whisper"},
                                {"pid": 14, "ppid": 2, "engine": "whisper"},
                                {"pid": 15, "ppid": 2, "engine": "llamacpp"}])

    def test_whispers_proof_is_its_load_on_the_request_path(self):
        """D9: whisper-server's proof is `--request-path /<load id>` - every
        route of that load is under it - beside its program, its model file
        and its port. llama.cpp's stays its own (`is_ours`' rows above)."""
        from runner.engines import is_ours
        for line in (WOURS, WOURS.replace(f"--request-path /{WLOAD}",
                                          f"--request-path=/{WLOAD}"),
                     WOURS.replace(f"/{WLOAD}", f'"/{WLOAD}"')):
            with self.subTest(line=line):
                self.assertTrue(is_ours(line, WHISPER, BIN, WPORT, WLOAD, engine="whisper"))
        not_ours = {
            "no load on the record": (WOURS, None),
            "the load elsewhere, another on the path": (
                WOURS.replace(f"/{WLOAD}", "/load-aaaaaaaa") + f" --tmp-dir {WLOAD}", WLOAD),
            "a longer path": (WOURS.replace(f"/{WLOAD}", f"/{WLOAD}x"), WLOAD),
            "a path below it": (WOURS.replace(f"/{WLOAD}", f"/{WLOAD}/inference"), WLOAD),
            "its load as an alias": (WOURS.replace(f"--request-path /{WLOAD}", f"--alias {WLOAD}"),
                                     WLOAD),
            "another program": (WOURS.replace("whisper-server.exe", "whisper-cli.exe"), WLOAD),
            "another model": (WOURS.replace("ggml-large-v3", "ggml-medium"), WLOAD),
            "another port": (WOURS.replace(f"--port {WPORT}", f"--port {WPORT + 1}"), WLOAD),
            "llama-server carrying all of it": (
                WOURS.replace("whisper\\bin\\whisper-server.exe", "llama\\bin\\llama-server.exe"),
                WLOAD)}
        for why, (line, load) in not_ours.items():
            with self.subTest(why=why):
                self.assertFalse(is_ours(line, WHISPER, BIN, WPORT, load, engine="whisper"))
        self.assertFalse(is_ours(WOURS, WHISPER, BIN, WPORT, WLOAD, engine="vllm"),
                         "an engine this runner does not know proved a process its own")

    def orphan(self, **line) -> None:
        folder = self.tmp / "bound"
        folder.mkdir(exist_ok=True)
        rows = [{"event": "session_start", "pid": 1},
                {"event": "load_ok", "model_key": "ears", "pid": 4343, "port": WPORT,
                 "gguf": BIN, "load": WLOAD, **line}]
        (folder / M.EVENTS_NAME).write_text("".join(json.dumps(r) + "\n" for r in rows),
                                            encoding="utf-8")

    def test_the_startup_check_holds_each_orphan_to_its_own_engines_proof(self):
        """A load's line names its engine, and the startup check reads the
        process against THAT engine's program and proof. A line naming none
        is from before engines were named: llama.cpp's."""
        cases = {"a whisper load, its own command line": ({"engine": "whisper"}, WOURS, True),
                 "a whisper load, llama-server's command line": (
                     {"engine": "whisper"}, OURS.replace(LOAD, WLOAD).replace(str(PORT),
                                                                           str(WPORT)), False),
                 "a line naming no engine is llama.cpp's": ({}, WOURS, False),
                 "an engine these settings do not know": ({"engine": "vllm"}, WOURS, False)}
        for why, (line, command, ours) in cases.items():
            with self.subTest(why=why):
                self.orphan(**line)
                os_ = Processes({4343: {"alive": True, "command_line": command}})
                m = ModelManager(self.real_settings(), self.tmp / "bound", clock=self.clock,
                                 processes=os_.info, kill=os_.kill, card=M.LocalCard())
                self.addCleanup(m.shutdown)
                out = m.startup_check(reclaim=True)
                self.assertEqual([o["ours"] for o in out["orphans"]], [ours])
                self.assertEqual(os_.killed, [4343] if ours else [])

    # ------------------------------------------------------------ the runner

    def test_a_real_loads_line_names_its_engine_and_its_model_file(self):
        """What a startup check reads a process against is on the load's own
        line: its engine, and its model file - `file`, whatever the engine. A
        llama.cpp line says it as `gguf` too, the field's name from before
        engines were named; a whisper model's file is not a GGUF, and its line
        never calls it one (S8 R5). A stand-in load names neither: nothing of
        it is on the card. [S12b, on purpose: `gguf` held every engine's file.]"""
        m = self.manager(self.real_settings(), ports=Ports(41000, 41001), vram=lambda: None,
                         card=M.LocalCard())
        m.acquire("story", W)
        m.acquire("ears", W)
        oks = {e["model_key"]: e for e in self.events(m) if e["event"] == "load_ok"}
        models = m.cfg.models_dir
        story = str(models / "story-12b.Q5_K_M.gguf")
        self.assertEqual((oks["story"]["engine"], oks["story"]["file"], oks["story"]["gguf"]),
                         ("llamacpp", story, story))
        self.assertEqual((oks["ears"]["engine"], oks["ears"]["file"]),
                         ("whisper", str(models / "ggml-large-v3.bin")))
        self.assertNotIn("gguf", oks["ears"], "a whisper model's file was called a GGUF")
        stand_in = self.manager(self.settings(), folder="mock")
        stand_in.acquire("ears", W)
        ok = next(e for e in self.events(stand_in) if e["event"] == "load_ok")
        self.assertEqual((ok.get("engine", "none"), ok.get("file", "none"), ok["gguf"]),
                         ("none", "none", None))

    def test_the_startup_check_reads_a_lines_file_and_a_line_from_before_by_its_gguf(self):
        """S8 R5, and older records still read: a line names its model file as
        `file`; one written before that name says it as `gguf`, and is read by
        it. The file is part of the proof - a process running another model
        is not this load's, whichever name the line gave its file."""
        other = BIN.replace("ggml-large-v3", "ggml-medium")
        cases = {"a line naming its file": ({"file": BIN}, True),
                 "a line naming another file": ({"file": other}, False),
                 "a line from before, its file as gguf": ({"gguf": BIN}, True),
                 "a line from before, another gguf": ({"gguf": other}, False),
                 "a file named both ways: file is the line's": ({"file": other, "gguf": BIN},
                                                                 False)}
        for why, (named, ours) in cases.items():
            with self.subTest(why=why):
                folder = self.tmp / "bound"
                folder.mkdir(exist_ok=True)
                rows = [{"event": "session_start", "pid": 1},
                        {"event": "load_ok", "model_key": "ears", "pid": 4343, "port": WPORT,
                         "load": WLOAD, "engine": "whisper", **named}]
                (folder / M.EVENTS_NAME).write_text("".join(json.dumps(r) + "\n" for r in rows),
                                                    encoding="utf-8")
                os_ = Processes({4343: {"alive": True, "command_line": WOURS}})
                m = ModelManager(self.real_settings(), folder, clock=self.clock,
                                 processes=os_.info, kill=os_.kill, card=M.LocalCard())
                self.addCleanup(m.shutdown)
                out = m.startup_check(reclaim=True)
                self.assertEqual([o["ours"] for o in out["orphans"]], [ours])
                self.assertEqual(os_.killed, [4343] if ours else [])

    def test_what_a_real_load_puts_on_the_card_is_named_with_its_own_engine(self):
        """The card source hears each model's own engine: its fit is asked,
        and its process named, as whisper or llama.cpp - never one engine for
        the whole runner."""
        told, asked = [], []

        class Card:
            turn, lost = {"id": "t-0001-k3x9"}, None

            def take(self_, holder, model_key, for_=None, cancel=None, on_wait=None,
                     terms=None):
                return self_.turn

            def give_back(self_):
                pass

            def fit(self_, model, engine, need_mib, **kw):
                asked.append((model, engine))

            def running(self_, pid, proof, model, engine, port):
                told.append((model, engine))

            def stopped(self_, proof):
                pass

        def make(key, port):
            b = FakeBackend(key, on_start=lambda b_: b_.on_spawn(4200 + len(self.made),
                                                                  b_.load_id))
            self.made.append(b)
            return b
        m = self.manager(self.real_settings(), backend_factory=make,
                         ports=Ports(41000, 41001), vram=lambda: None, card=Card())
        m.acquire("story", W)
        m.acquire("ears", W)
        self.assertEqual((asked, told), ([("story", "llamacpp"), ("ears", "whisper")],
                                         [("story", "llamacpp"), ("ears", "whisper")]))

    def test_the_real_backend_makes_each_model_its_own_engines_backend(self):
        m = self.manager(self.real_settings(), fake=False, ports=Ports(), card=M.LocalCard())
        self.assertEqual((type(m._make("story", 41000)).__name__,
                          type(m._make("ears", 41001)).__name__),
                         ("LlamaCppBackend", "WhisperBackend"))
        stand_in = self.manager(self.settings(), folder="mock", fake=False)
        a, e = stand_in._make("alpha", None), stand_in._make("ears", None)
        self.assertEqual((type(a).__name__, a.answers, type(e).__name__, e.answers),
                         ("MockBackend", "stream", "MockBackend", "once"))

    # ------------------------------------------------ the stand-in's shapes

    def mock_ears(self, **mock) -> Backend:
        text = ENGINE_SETTINGS.replace("[mock]\n", "[mock]\n" + "".join(
            f"{k} = {v}\n" for k, v in mock.items()))
        m = self.manager(self.settings(text, name="ears.toml"), fake=False)
        return m.acquire("ears", W)

    def test_the_stand_in_transcribes_in_one_piece_due_by_the_deadline_it_declared(self):
        """D10's one call, of its second kind: before it is asked, the model
        declares the deadline its answer is due by - the seam a stall watch
        and a hang check read so as never to call a quiet transcription stuck
        (`answer_deadline_s`, S5's) - and the answer comes whole, in one
        `done`. A model whose answer streams declares none."""
        import fake_whisper_server as FW
        ears = self.mock_ears(answer_seconds=0.6, answer_due_s=7)
        wav = _wav(self.tmp / "said.wav", seconds=1.5)
        request = {"audio": str(wav)}
        self.assertEqual(ears.answer_deadline_s(request, GenParams()), 7.0)
        alpha = self.manager(self.settings(), folder="text", fake=False).acquire("alpha", W)
        self.assertIsNone(alpha.answer_deadline_s("Tell me about rain.", GenParams()))
        rest = list(ears.stream(request, GenParams(), None))
        self.assertEqual([e["type"] for e in rest], ["done"])
        done = rest[0]
        heard = FW.Fake({}, set()).answer({"response_format": "verbose_json"}, 1.5, True)
        self.assertEqual((done["text"], done["transcript"], done["audio_s"], done["finish_reason"],
                          done["tokens"]),
                         (heard["text"], heard, 1.5, "stop",
                          sum(len(s["tokens"]) for s in heard["segments"])))

    def test_a_transcript_of_silence_is_an_answer_with_no_words(self):
        ears = self.mock_ears()
        wav = _wav(self.tmp / "quiet.wav", seconds=2.0, sound=False)
        done = list(ears.stream({"audio": str(wav)}, GenParams(), None))[-1]
        self.assertEqual((done["type"], done["text"], done["transcript"]["segments"],
                          done["tokens"], done["finish_reason"]), ("done", "", [], 0, "stop"))

    def test_the_stand_in_answers_in_the_shape_the_whisper_stand_in_server_does(self):
        """The stand-in model's transcript is the stand-in server's verbose
        answer - the shape whisper-server 1.9.1 was measured to answer in -
        the language named as the server names it, and its table left out
        exactly when the server would leave it out."""
        import fake_whisper_server as FW
        ears = self.mock_ears()
        wav = _wav(self.tmp / "said.wav", seconds=2.25)
        for fields in ({}, {"language": "no"}, {"no_language_probabilities": True},
                       {"language": "auto", "no_language_probabilities": "0"},
                       {"no_language_probabilities": "y"}):
            with self.subTest(fields=fields):
                done = list(ears.stream({"audio": str(wav), "fields": fields}, GenParams(),
                                        None))[-1]
                from runner.whisper_fields import as_form
                sent = {k: as_form(v) for k, v in fields.items()}
                want = FW.Fake({}, set()).answer({**sent, "response_format": "verbose_json"},
                                                 2.25, True)
                self.assertEqual(done["transcript"], want)

    def test_the_stand_in_hears_a_cancel_while_it_answers_in_one_piece(self):
        ears = self.mock_ears(answer_seconds=30, answer_due_s=60)
        wav = _wav(self.tmp / "said.wav")
        cancel = threading.Event()
        stream = ears.stream({"audio": str(wav)}, GenParams(), cancel)
        threading.Timer(0.2, cancel.set).start()
        t0 = time.monotonic()
        with self.assertRaises(Cancelled):
            list(stream)
        self.assertLess(time.monotonic() - t0, 5.0)

    # ------------------------------------------------------- the give-up rule

    def test_a_give_up_ends_a_start_within_the_rule_and_the_rule_fits_a_close(self):
        """S7 R3, made a rule every engine is held to: once a load is given
        up (`abandon`), its `start` ends within `base.GIVE_UP_S` - and that is
        within the `CLOSE_JOIN_S` a pipeline's close waits for a load under
        way. Here the stand-in, in each engine's shape; llama.cpp and whisper
        are held to it against their stand-in servers, in their own suites."""
        from runner import base
        self.assertLess(base.GIVE_UP_S, ModelManager.CLOSE_JOIN_S)
        for engine in ("llamacpp", "whisper"):
            with self.subTest(engine=engine):
                b = MockBackend("m", "m", {"load_seconds": 60}, engine=engine)
                took, raised = give_up_timed(b)
                self.assertIsNotNone(took, "the start never ended")
                self.assertLess(took, base.GIVE_UP_S)
                self.assertIsInstance(raised, BackendError)
                self.assertIn("given up", str(raised))


# ================================================ where the runner writes ===

class TheRunnerHasNoShippedDataRoot(Case):
    """Carried over: the runner has no data root of its own - by
    behaviour, as that suite asked: "a row reading assertNotIn('data_dir',
    DEFAULTS) would pass on a tree where the key had been moved". What must
    hold is that nothing bound writes NOTHING, and a bound folder is where
    everything lands."""

    def test_a_manager_with_nothing_bound_writes_nothing_anywhere(self):
        cwd = self.tmp / "cwd"
        cwd.mkdir()
        here = os.getcwd()
        os.chdir(cwd)
        self.addCleanup(os.chdir, here)
        cfg = self.config()
        cfg.root = cwd                             # a relative root would land here
        m = self.manager(cfg, folder=None, fake=False)
        m.acquire("alpha", W)
        m.unload()
        self.assertIsNone(m.events_path)
        self.assertIsNone(m.loads_path)
        self.assertEqual(sorted(p.name for p in cwd.iterdir()), [])
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["cwd", "runner.toml"])
        # CHANGED ON PURPOSE (S6): the queue's own pipeline opens with the load
        # and closes once its work is done - kept in memory, as every line is
        self.assertEqual([e["event"] for e in m.recent_events()],
                         ["session_start", "pipeline_opened", "load_start", "load_ok",
                          "unload_start", "unload_ok", "pipeline_closed"])

    def test_the_manager_writes_through_the_folder_it_was_given(self):
        m = self.manager(fake=False)
        m.acquire("alpha", W)
        m.unload()
        folder = self.tmp / "bound"
        # CHANGED ON PURPOSE (S11b): the timing statistics and their records
        # are kept in the same folder (PLAN-4 D12)
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         ["model_loads.json", "model_stats.json", "model_stats.jsonl",
                          "runner_events.jsonl"])
        self.assertEqual(m.events_path.parent, folder)

    def test_the_models_folder_is_found_beside_the_settings_file(self):
        cfg = self.config("[runner]\nmodels_dir = 'weights'\n")
        self.assertEqual(cfg.models_dir, (self.tmp / "weights").resolve())


class NativeRebindRows(Case):
    """D5: the root follows the folder, the model does not reload, and the
    load's record goes with it - so neither folder's record lies about it."""

    def setUp(self):
        super().setUp()
        self.m = self.manager(folder="a")
        self.a, self.b = self.tmp / "a" / M.EVENTS_NAME, self.tmp / "b" / M.EVENTS_NAME

    def test_a_rebind_follows_the_folder_without_reloading_the_model(self):
        b = self.m.acquire("alpha", W)
        self.m.rebind(self.tmp / "b")
        self.assertIs(self.m.acquire("alpha", W), b)
        self.assertEqual((b.starts, b.stops, self.m.status()["loads_this_session"]), (1, 0, 1))
        self.assertNotIn("load_start", self.kinds(self.b))

    def test_the_old_folder_is_told_the_load_left_and_the_new_one_that_it_arrived(self):
        b = self.m.acquire("alpha", W)
        self.m.rebind(self.tmp / "b")
        left = [e for e in self.events(self.a) if e["event"] == "rebound_away"]
        self.assertEqual([e["load"] for e in left], [b.load_id])
        self.assertEqual(self.kinds(self.b), ["session_start", "load_carried"])
        self.assertEqual(self.events(self.b)[1]["load"], b.load_id)
        self.assertIs(self.events(self.b)[0]["rebound"], True)

    def test_neither_folder_calls_a_carried_load_an_orphan(self):
        self.m.acquire("alpha", W)
        self.m.rebind(self.tmp / "b")
        self.m.unload()
        # a later server in each folder reads what this one left
        for folder in ("a", "b"):
            self.manager(folder=folder)
        self.assertEqual(orphaned_loads(self.events(self.a)), [])
        self.assertEqual(orphaned_loads(self.events(self.b)), [])
        self.assertEqual(max_concurrency(self.events(self.b)), 1)

    def test_a_server_that_dies_after_a_rebind_leaves_its_orphan_where_it_was(self):
        b = self.m.acquire("alpha", W)
        self.m.rebind(self.tmp / "b")                # ...and is killed here, still loaded
        self.manager(folder="a")
        self.manager(folder="b")
        self.assertEqual(orphaned_loads(self.events(self.a)), [])
        self.assertEqual([o["load"] for o in orphaned_loads(self.events(self.b))], [b.load_id])

    def test_rebinding_to_nothing_writes_nothing_more_anywhere(self):
        self.m.acquire("alpha", W)
        self.m.rebind(None)
        before = self.a.read_bytes()
        self.m.unload()
        self.assertEqual(self.a.read_bytes(), before)
        # CHANGED ON PURPOSE (S6): the queue's own pipeline closes after the
        # unload, and says so - in memory, as the unload is
        self.assertEqual([e["event"] for e in self.m.recent_events()[-2:]],
                         ["unload_ok", "pipeline_closed"])

    def test_rebinding_to_the_same_folder_changes_nothing(self):
        self.m.acquire("alpha", W)
        before = self.a.read_bytes()
        self.m.rebind(self.tmp / "a")
        self.assertEqual(self.a.read_bytes(), before)

    def test_the_load_record_follows_the_folder_too(self):
        self.m.rebind(self.tmp / "b")
        self.m.acquire("alpha", W)
        self.assertTrue((self.tmp / "b" / M.LOADS_NAME).exists())
        self.assertFalse((self.tmp / "a" / M.LOADS_NAME).exists())


# ======================================================== load identity ===

class NativeLoadIdentityRows(Case):
    def test_every_load_has_its_own_id_on_every_line_it_writes(self):
        m = self.manager()
        first = m.acquire("alpha", W)
        second = m.acquire("beta", W)
        m.unload()
        for b in (first, second):
            self.assertTrue(ids.matches("actor", b.load_id), b.load_id)
            self.assertTrue(b.load_id.startswith("load-"))
        self.assertNotEqual(first.load_id, second.load_id)
        by_load = {}
        for e in self.events(m):
            if e["event"] in ("load_start", "load_ok", "unload_start", "unload_ok"):
                by_load.setdefault(e["load"], []).append(e["event"])
        self.assertEqual(by_load, {
            first.load_id: ["load_start", "load_ok", "unload_start", "unload_ok"],
            second.load_id: ["load_start", "load_ok", "unload_start", "unload_ok"]})

    def test_the_load_s_line_names_its_process_and_its_port(self):
        """What a startup check finds an orphan by. Every line a load writes
        after it is up carries them."""
        m = self.manager()
        b = m.acquire("alpha", W)
        m.unload()
        for kind in ("load_ok", "unload_start", "unload_ok"):
            with self.subTest(kind=kind):
                e = next(e for e in self.events(m) if e["event"] == kind)
                self.assertEqual((e["load"], e["pid"], e["port"], e["model_key"]),
                                 (b.load_id, b.pid, b.port, "alpha"))

    def test_status_names_the_load(self):
        m = self.manager()
        b = m.acquire("alpha", W)
        self.assertEqual(m.status()["load"], b.load_id)
        m.unload()
        self.assertIsNone(m.status()["load"])

    def test_an_unload_closes_ITS_load_and_not_the_latest(self):
        """With two loads open - the violation the record exists to catch - an
        unload of the FIRST must leave the SECOND open. Pairing by position
        would close the wrong one and blame the orphan on the wrong load."""
        events = [{"event": "session_start"},
                  {"event": "load_ok", "load": "load-aaaaaaaa"},
                  {"event": "load_ok", "load": "load-bbbbbbbb"},
                  {"event": "unload_ok", "load": "load-aaaaaaaa"},
                  {"event": "session_start"}]
        self.assertEqual(max_concurrency(events), 2)
        self.assertEqual([o["load"] for o in orphaned_loads(events)], ["load-bbbbbbbb"])

    def test_a_line_that_names_no_load_closes_the_latest(self):
        events = [{"event": "load_ok"}, {"event": "load_ok"}, {"event": "unload_ok"},
                  {"event": "session_start"}]
        self.assertEqual(len(orphaned_loads(events)), 1)

    def test_the_stand_in_opens_no_port(self):
        m = self.manager(fake=False)
        b = m.acquire("alpha", W)
        self.assertIsNone(b.port)
        start = next(e for e in self.events(m) if e["event"] == "load_start")
        self.assertIsNone(start["port"])


class NativePortRows(Case):
    """D2: a real server gets the configured port, or - by default - a free
    one per load, so two servers can never be pointed at one by accident."""

    def llamacpp(self, port):
        cfg = self.config(f"[runner]\nbackend = 'llamacpp'\nport = {port}\n[models.m]\n"
                          f"file = 'm.gguf'\n")
        return ModelManager(cfg, None, clock=self.clock)

    def test_a_configured_port_is_used_as_it_is(self):
        self.assertEqual(self.llamacpp(8901)._port_for_load("m", "load-k3x9m2pq"), 8901)

    def test_port_zero_takes_a_free_one_from_the_runners_own_range(self):
        """CHANGED ON PURPOSE (S7, deciding H1's register row 2): no longer one
        the operating system picks - it may hand that one to any connection
        between the pick and llama-server's bind - but one of the runner's own
        range, below every system's range for passing connections, checked
        free by binding it."""
        port = self.llamacpp(0)._port_for_load("m", "load-k3x9m2pq")
        self.assertIsInstance(port, int)
        lo, hi = M.QUIET_PORTS
        self.assertTrue(lo <= port <= hi < 32768, (port, M.QUIET_PORTS))


class Ports:
    """A port source that writes down what it was asked, and can refuse."""

    def __init__(self, *free, refuse=None, give_back_fails=False):
        self.free, self.refuse, self.give_back_fails = list(free), refuse, give_back_fails
        self.taken, self.given_back = [], []
        #: the load each port was taken for (S7: a port per load, by its name)
        self.loads = []

    def take(self, model_key, load=None):
        if self.refuse:
            raise RuntimeError(self.refuse)
        port = self.free.pop(0)
        self.taken.append((model_key, port))
        self.loads.append(load)
        return port

    def give_back(self, port):
        if self.give_back_fails:
            raise RuntimeError("the registry is not answering")
        self.given_back.append(port)


LLAMA_SETTINGS = ('[runner]\nbackend = "llamacpp"\n\n[models.alpha]\nlabel = "Alpha"\n\n'
                  '[models.beta]\nlabel = "Beta"\n')


class NativePortSourceRows(Case):
    """S4: a load's port comes from a PORT SOURCE - by default the configured
    one or a free one, and the coordinator's registry once the server holds
    its ports there. One per load, never per reuse; given back when the load
    comes off the card, or fails."""

    def llama(self, ports, **opts):
        made = self.made

        def make(key, port):
            b = FakeBackend(key, **opts)
            b.given_port = port
            made.append(b)
            return b
        return self.manager(self.config(LLAMA_SETTINGS), backend_factory=make, ports=ports,
                            vram=lambda: None)

    def test_a_load_takes_its_port_from_the_source(self):
        ports = Ports(41000, 41001)
        m = self.llama(ports)
        m.acquire("alpha", W)
        start = next(e for e in self.events(m) if e["event"] == "load_start")
        self.assertEqual((self.made[0].given_port, start["port"], ports.taken),
                         (41000, 41000, [("alpha", 41000)]))

    def test_a_reuse_asks_for_no_port(self):
        ports = Ports(41000, 41001)
        m = self.llama(ports)
        m.acquire("alpha", W)
        m.acquire("alpha", W)
        self.assertEqual(ports.taken, [("alpha", 41000)])

    def test_a_port_goes_back_when_its_load_comes_off_the_card(self):
        ports = Ports(41000, 41001)
        m = self.llama(ports)
        m.acquire("alpha", W)
        m.unload()
        self.assertEqual(ports.given_back, [41000])

    def test_models_side_by_side_hold_a_port_each_in_their_own_loads_names(self):
        """CHANGED ON PURPOSE (S7; was "a swap gives the old port back and takes
        a new one"): beta goes up beside alpha, so each holds a port of its
        own at once - each taken in its own load's name - and each goes back
        with its own load."""
        ports = Ports(41000, 41001)
        m = self.llama(ports)
        a = m.acquire("alpha", W)
        b = m.acquire("beta", W)
        self.assertEqual((ports.given_back, ports.taken, ports.loads),
                         ([], [("alpha", 41000), ("beta", 41001)], [a.load_id, b.load_id]))
        m.unload("alpha")
        self.assertEqual(ports.given_back, [41000])
        m.unload("beta")
        self.assertEqual(ports.given_back, [41000, 41001])

    def test_a_load_that_fails_gives_its_port_back(self):
        ports = Ports(41000)
        m = self.llama(ports, fail="llama-server did not come up")
        with self.assertRaises(BackendError):
            m.acquire("alpha", W)
        self.assertEqual(ports.given_back, [41000])
        self.assertIsNone(m.status()["lease"]["holder"])

    def test_a_source_that_refuses_fails_the_load_before_anything_moves(self):
        ports = Ports(refuse="no free port in 31000-31999: 1000 held")
        m = self.llama(ports)
        with self.assertRaises(BackendError) as caught:
            m.acquire("alpha", W)
        self.assertIn("no port for alpha", str(caught.exception))
        self.assertIn("1000 held", str(caught.exception))
        self.assertEqual((self.made, m.status()["lease"]["holder"]), ([], None))

    def test_a_port_that_cannot_be_given_back_is_recorded_and_the_unload_stands(self):
        ports = Ports(41000, give_back_fails=True)
        m = self.llama(ports)
        m.acquire("alpha", W)
        m.unload()
        self.assertIsNone(m.status()["loaded"])
        failed = [e for e in self.events(m) if e["event"] == "port_give_back_failed"]
        self.assertEqual([(e["port"], e["model_key"]) for e in failed], [(41000, "alpha")])

    def test_a_stop_that_fails_keeps_its_port(self):
        """What would not stop may still be listening on it: the port is not
        handed on while that is possible."""
        ports = Ports(41000)
        m = self.llama(ports, stop_fails="the process would not die")
        m.acquire("alpha", W)
        with self.assertRaises(RuntimeError):
            m.unload()
        self.assertEqual(ports.given_back, [])

    def test_a_stop_that_failed_gives_its_port_back_once_it_goes(self):
        """S3: the stop that failed is asked again before the next load; once
        it works, nothing that one started listens on the port any more, and
        the port goes back - before the next load takes its own."""
        ports = Ports(41000, 41001)
        m = self.llama(ports, stop_fails="the process would not die")
        m.acquire("alpha", W)
        with self.assertRaises(RuntimeError):
            m.unload()
        self.made[0].stop_fails = None
        m.acquire("beta", W)
        self.assertEqual((ports.given_back, ports.taken),
                         ([41000], [("alpha", 41000), ("beta", 41001)]))
        ports.free.append(41002)
        self.made[1].stop_fails = None
        m.acquire("alpha", W)                           # a swap: nothing is asked again
        again = [e["event"] for e in self.events(m) if e.get("again")]
        self.assertEqual(again, ["unload_start", "unload_ok"],
                         "a stop that went was asked again at a later load")

    def test_the_card_source_is_told_each_process_from_its_spawn_to_its_stop(self):
        """S3: what the runner tells its card source, which names it on the
        rental: a load's process the moment it exists - with its model, this
        runner's engine and the load's port - and that it has gone once its
        stop returned. Nothing for a stop that failed: that process may still
        be on the card, until its stop, asked again before the next load,
        works."""
        told = []

        class Card:
            turn, lost = {"id": "t-0001-k3x9"}, None

            def take(self_, holder, model_key, for_=None, cancel=None, on_wait=None):
                return self_.turn

            def give_back(self_):
                pass

            def running(self_, pid, proof, model, engine, port):
                told.append(("running", pid, proof, model, engine, port))

            def stopped(self_, proof):
                told.append(("stopped", proof))

        def make(key, port):
            b = FakeBackend(key, on_start=lambda b_: b_.on_spawn(4200 + len(self.made),
                                                                  b_.load_id))
            self.made.append(b)
            return b
        m = self.manager(self.config(LLAMA_SETTINGS), backend_factory=make,
                         ports=Ports(41000, 41001, 41002), vram=lambda: None, card=Card())
        a = m.acquire("alpha", W)
        self.assertEqual(told, [("running", 4201, a.load_id, "alpha", "llamacpp", 41000)],
                         "not named the moment it existed, with its model, engine and port")
        m.unload()
        self.assertEqual(told[-1], ("stopped", a.load_id))
        b = m.acquire("beta", W)
        b.stop_fails = "the process would not die"
        told.clear()
        with self.assertRaises(RuntimeError):
            m.unload()
        self.assertEqual(told, [], "a process whose stop failed was said to be gone")
        b.stop_fails = None
        c = m.acquire("alpha", W)                       # its stop is asked again, and works
        self.assertEqual(told, [("stopped", b.load_id),
                                ("running", 4203, c.load_id, "alpha", "llamacpp", 41002)])

    def test_a_card_source_that_cannot_hear_a_stop_fails_nothing(self):
        """What the card source is told of a stop never fails the unload it
        follows: the record says it could not be told."""
        class Card:
            turn, lost = None, None

            def take(self_, holder, model_key, for_=None, cancel=None, on_wait=None):
                return None

            def give_back(self_):
                pass

            def stopped(self_, proof):
                raise RuntimeError("the coordinator is not answering")
        m = self.manager(card=Card())
        up = m.acquire("alpha", W)
        m.unload()
        self.assertIsNone(m.status()["loaded"])
        told = [e for e in self.events(m) if e["event"] == "card_stopped_failed"]
        self.assertEqual([(e["load"], e["detail"]) for e in told],
                         [(up.load_id, "the coordinator is not answering")])

    def test_the_stand_in_asks_for_no_port(self):
        ports = Ports(41000)
        m = self.manager(ports=ports)
        m.acquire("alpha", W)
        m.unload()
        self.assertEqual((ports.taken, ports.given_back), ([], []))


# ===================================================== the startup check ===

LLAMA = "C:/llama/bin/llama-server.exe"
GGUF = "D:/models/story-12b.Q5_K_M.gguf"
LOAD = "load-k3x9m2pq"
PORT = 50123
OURS = (f'"C:\\llama\\bin\\llama-server.exe" -m "D:\\models\\story-12b.Q5_K_M.gguf" '
        f'--host 127.0.0.1 --port {PORT} --alias {LOAD} -c 8192')


class Processes:
    """The operating system, as the startup check sees it: pid -> info."""

    def __init__(self, table):
        self.table, self.asked, self.killed = table, [], []
        self.kill_works = True

    def info(self, pid):
        self.asked.append(pid)
        return self.table.get(pid, {"alive": False, "command_line": None})

    def kill(self, pid):
        self.killed.append(pid)
        return self.kill_works


class NativeStartupCheckRows(Case):
    """D4: orphans come from the RECORD, a reclaim is opt-in, and a kill needs
    proof of identity. The existing application's reclaim killed whatever
    held its port."""

    def settings(self):
        return self.config(f"[runner]\nbackend = 'llamacpp'\nllama_server = '{LLAMA}'\n"
                           f"[models.story]\nfile = 'story-12b.Q5_K_M.gguf'\n")

    def leave_an_orphan(self, **over):
        folder = self.tmp / "bound"
        folder.mkdir(exist_ok=True)
        rows = [{"event": "session_start", "pid": 1},
                {"event": "load_ok", "model_key": "story", "pid": 4242, "port": PORT,
                 "gguf": GGUF, "load": LOAD, **over}]
        (folder / M.EVENTS_NAME).write_text("".join(json.dumps(r) + "\n" for r in rows),
                                            encoding="utf-8")

    def checked(self, table, reclaim=False, **over):
        self.leave_an_orphan(**over)
        os_ = Processes(table)
        m = ModelManager(self.settings(), self.tmp / "bound", clock=self.clock,
                         processes=os_.info, kill=os_.kill)
        return m, os_, m.startup_check(reclaim=reclaim)

    def test_by_default_it_only_reports(self):
        m, os_, out = self.checked({4242: {"alive": True, "command_line": OURS}})
        self.assertEqual(out["orphans"], [{"pid": 4242, "port": PORT, "model_key": "story",
                                           "load": LOAD, "alive": True, "ours": True}])
        self.assertEqual((out["reclaimed"], os_.killed), ([], []))
        self.assertIn("orphan_found", self.kinds(m))

    def test_asked_to_reclaim_it_kills_a_process_that_is_provably_ours(self):
        m, os_, out = self.checked({4242: {"alive": True, "command_line": OURS}}, reclaim=True)
        self.assertEqual((out["reclaimed"], os_.killed), ([4242], [4242]))
        reclaimed = [e for e in self.events(m) if e["event"] == "orphan_reclaimed"]
        self.assertEqual([(e["pid"], e["load"]) for e in reclaimed], [(4242, LOAD)])

    def test_a_reclaim_grants_nothing(self):
        m, _, _ = self.checked({4242: {"alive": True, "command_line": OURS}}, reclaim=True)
        self.assertIsNone(m.status()["lease"]["holder"])

    def test_it_never_kills_what_it_cannot_prove_is_its_own(self):
        not_ours = {
            "another program on the port": f"python -m http.server --port {PORT}",
            "our server, another model": OURS.replace("story-12b", "other-7b"),
            "our model, another port": OURS.replace(f"--port {PORT}", f"--port {PORT + 1}"),
            "the port inside a longer one": OURS.replace(f"--port {PORT}", f"--port {PORT}9"),
            "another load of ours": OURS.replace(LOAD, "load-aaaaaaaa"),
            "another executable": OURS.replace("llama-server.exe", "llama-cli.exe"),
            "a command line it could not read": None,
        }
        for why, line in not_ours.items():
            with self.subTest(why=why):
                m, os_, out = self.checked({4242: {"alive": True, "command_line": line}},
                                           reclaim=True)
                self.assertEqual([o["ours"] for o in out["orphans"]], [False])
                self.assertEqual(os_.killed, [])

    def test_a_process_it_could_not_ask_about_is_reported_and_left(self):
        m, os_, out = self.checked({4242: {"alive": None, "command_line": OURS}}, reclaim=True)
        self.assertEqual([(o["alive"], o["ours"]) for o in out["orphans"]], [(None, False)])
        self.assertEqual(os_.killed, [])

    def test_one_it_could_not_ask_about_is_asked_again_at_the_next_start_and_taken_back(self):
        """Late, never lost: a process the system could not be asked about -
        its process query failed, as one can under load - is left running;
        the next start finds it on the record again, asks again, and takes it
        back on proof. What makes leaving it the right call: a guess would be
        a kill without proof."""
        self.leave_an_orphan()
        os_ = Processes({4242: {"alive": None, "command_line": OURS}})
        first = ModelManager(self.settings(), self.tmp / "bound", clock=self.clock,
                             processes=os_.info, kill=os_.kill)
        out = first.startup_check(reclaim=True)
        self.assertEqual((out["reclaimed"], os_.killed), ([], []))
        os_.table[4242] = {"alive": True, "command_line": OURS}
        heir = ModelManager(self.settings(), self.tmp / "bound", clock=self.clock,
                            processes=os_.info, kill=os_.kill)
        out = heir.startup_check(reclaim=True)
        self.assertEqual((out["reclaimed"], os_.killed), ([4242], [4242]))

    def test_a_process_that_is_gone_is_nobody_s_problem(self):
        m, os_, out = self.checked({}, reclaim=True)
        self.assertEqual((out, os_.asked, os_.killed),
                         ({"orphans": [], "reclaimed": []}, [4242], []))

    def test_a_load_with_no_process_is_not_even_asked_about(self):
        m, os_, out = self.checked({}, reclaim=True, pid=None)
        self.assertEqual((out["orphans"], os_.asked), ([], []))

    def test_a_kill_the_system_refused_says_so(self):
        self.leave_an_orphan()
        os_ = Processes({4242: {"alive": True, "command_line": OURS}})
        os_.kill_works = False
        m = ModelManager(self.settings(), self.tmp / "bound", clock=self.clock,
                         processes=os_.info, kill=os_.kill)
        out = m.startup_check(reclaim=True)
        self.assertEqual((out["reclaimed"], os_.killed), ([], [4242]))
        self.assertIn("orphan_reclaim_failed", self.kinds(m))

    def test_building_a_manager_asks_about_nothing_and_kills_nothing(self):
        self.leave_an_orphan()
        os_ = Processes({4242: {"alive": True, "command_line": OURS}})
        ModelManager(self.settings(), self.tmp / "bound", clock=self.clock,
                     processes=os_.info, kill=os_.kill)
        self.assertEqual((os_.asked, os_.killed), ([], []))

    def test_an_unbound_manager_finds_nothing(self):
        os_ = Processes({4242: {"alive": True, "command_line": OURS}})
        m = ModelManager(self.settings(), None, clock=self.clock,
                         processes=os_.info, kill=os_.kill)
        self.assertEqual(m.startup_check(reclaim=True), {"orphans": [], "reclaimed": []})
        self.assertEqual(os_.asked, [])

    def test_the_identity_check_reads_the_shapes_a_command_line_comes_in(self):
        for line in (OURS,
                     OURS.replace(f"--port {PORT}", f"--port={PORT}"),
                     f"/opt/llama/llama-server -m /srv/story-12b.Q5_K_M.gguf --alias {LOAD} "
                     f"--port {PORT}"):
            with self.subTest(line=line):
                self.assertTrue(is_ours(line, LLAMA, GGUF, PORT, LOAD))
        self.assertTrue(is_ours(OURS, LLAMA, GGUF, PORT, None), "a record with no load id")
        for missing in ((None, GGUF, PORT), (OURS, None, PORT), (OURS, GGUF, None)):
            with self.subTest(missing=missing):
                self.assertFalse(is_ours(missing[0], LLAMA, missing[1], missing[2], LOAD))

    def test_this_suite_can_kill_nothing(self):
        """The tripwire is in place, and every manager built without a fake
        gets it."""
        self.assertIs(M.kill_pid, _no_kill)
        self.assertIs(self.manager()._kill, _no_kill)


class _NotAnsweringOnce:
    """The subprocess module as the runner's manager sees it, whose first
    query of the process list does not answer within its bound - as one did
    not beside three sweeps (wave 3)."""

    def __init__(self):
        self.refused = 0

    def __getattr__(self, name):
        return getattr(subprocess, name)

    def run(self, args, *a, **k):
        if not self.refused and args and args[0] == "powershell":
            self.refused += 1
            raise subprocess.TimeoutExpired(args, k.get("timeout"))
        return subprocess.run(args, *a, **k)


class NativeProcessInfoRows(unittest.TestCase):
    """The one real question the startup check asks the operating system,
    asked about processes this suite owns. Read-only.

    `alive: None` is the runner's own word for "could not be asked" - its
    query did not answer within its 30 s beside three sweeps, and this row,
    reading that as its answer, went red (wave 3). These rows ask again
    within a bound: the runner's reading of that word is the startup check
    rows' to prove."""

    @staticmethod
    def info(pid) -> dict:
        return asked_again(lambda: M.process_info(pid), lambda i: i["alive"] is not None,
                           f"whether pid {pid} is alive")

    def test_this_process_is_alive_and_its_command_line_is_readable(self):
        info = self.info(os.getpid())
        self.assertIs(info["alive"], True)
        self.assertIn("test_runner", info["command_line"] or "", info)

    def test_a_process_that_has_ended_is_gone(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(60)                                      # it ends of itself: bounded all the same
        self.assertEqual(self.info(child.pid), {"alive": False, "command_line": None})

    @unittest.skipUnless(sys.platform == "win32", "the process list is a query on Windows")
    def test_what_the_process_list_did_not_answer_is_asked_again(self):
        """Provoked: the first query does not answer in its bound. The runner
        says it could not ask; the row asks again, and reads the answer."""
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(60)                                      # it ends of itself: bounded all the same
        busy = _NotAnsweringOnce()
        self.addCleanup(setattr, M, "subprocess", M.subprocess)
        M.subprocess = busy
        self.assertEqual(self.info(child.pid), {"alive": False, "command_line": None})
        self.assertEqual(busy.refused, 1, "the process list was never asked")

    def test_a_pid_that_is_not_a_number_never_reaches_the_question(self):
        """A pid read from a record is never put into a command unread. The
        value is one that WOULD change the question if it got there - it
        widens it to this very process - so the row can tell. (A plain
        "not-a-pid" could not: the operating system answers "gone" to it
        either way, and the sweep kept that row green with the check taken
        out.)"""
        widened = f"999999999 OR ProcessId={os.getpid()}"
        self.assertEqual(M.process_info(widened), {"alive": False, "command_line": None})
        self.assertEqual(M.process_info(None), {"alive": False, "command_line": None})


# ============================================================ the record ===

class NativeRecordRows(Case):
    def test_every_line_carries_when_what_which_backend_and_how_many_live(self):
        m = self.manager(fake=False)
        m.acquire("alpha", W)
        for e in self.events(m):
            self.assertTrue({"ts", "event", "backend", "live_after"} <= set(e), e)
        ok = next(e for e in self.events(m) if e["event"] == "load_ok")
        self.assertEqual((ok["live_after"], ok["backend"], ok["ts"]), (1, "mock", self.clock.t))

    def test_a_torn_last_line_is_skipped_and_the_rest_kept(self):
        m = self.manager(fake=False)
        m.acquire("alpha", W)
        with open(m.events_path, "a", encoding="utf-8") as fh:
            fh.write('{"event": "load_o')
        self.assertEqual([e["event"] for e in read_events(m.events_path)][-1], "load_ok")

    def test_the_instrument_sees_a_violation(self):
        """What the fault looks like, so its absence means something."""
        events = [{"event": "session_start"}, {"event": "load_ok", "load": "load-aaaaaaaa"},
                  {"event": "load_ok", "load": "load-bbbbbbbb"}]
        self.assertEqual(max_concurrency(events), 2)

    def test_a_failed_unload_keeps_the_load_open_on_the_record_and_says_why(self):
        m = self.manager(backend_factory=self.factory(stop_fails="would not die"))
        b = m.acquire("alpha", W)
        with self.assertRaises(RuntimeError):
            m.unload()
        failed = [e for e in self.events(m) if e["event"] == "unload_failed"]
        self.assertEqual([(e["load"], e["detail"]) for e in failed], [(b.load_id, "would not die")])
        self.assertNotIn("unload_ok", self.kinds(m))
        self.manager()                              # the next server finds it
        self.assertEqual([o["load"] for o in orphaned_loads(self.events(m))], [b.load_id])

    def test_every_load_line_names_the_request_it_served(self):
        """A load, a reuse and a failed load each say which request asked for
        them - the record of the card joins the request's record by its id."""
        m = self.manager(backend_factory=self.factory())
        first, again = "c-20260925-090000-k3x9m2p7", "c-20260925-090001-k3x9m2p7"
        m.acquire("alpha", W, for_=first)
        m.acquire("alpha", W, for_=again)
        lines = {e["event"]: e.get("for") for e in self.events(m)
                 if e["event"] in ("load_start", "load_ok", "reuse")}
        self.assertEqual(lines, {"load_start": first, "load_ok": first, "reuse": again})
        broken = self.manager(folder="other", backend_factory=self.factory(fail="no weights"))
        with self.assertRaises(BackendError):
            broken.acquire("alpha", W, for_=first)
        (failed,) = [e for e in self.events(broken) if e["event"] == "load_failed"]
        self.assertEqual(failed.get("for"), first)
        # and a load nobody named a request for says none, never a blank
        m.unload()
        m.acquire("alpha", W)
        self.assertNotIn("for", [e for e in self.events(m) if e["event"] == "load_ok"][-1])


# ============================================================ load timing ===

class LoadTimingCase(Case):
    def setUp(self):
        super().setUp()
        self.path = self.tmp / "model_loads.json"

    def seed(self, key, cold, *seconds):
        return [note_load(self.path, key, cold, s) for s in seconds][-1]


class WhereTheRecordLives(LoadTimingCase):
    """Carried over: where the load record lives. The existing application
    derived the path from the
    events file's; here both come from the folder the manager was given."""

    def test_the_record_lives_beside_the_events_in_the_folder_given(self):
        m = self.manager(fake=False)
        m.acquire("alpha", W)
        self.assertEqual(m.loads_path, self.tmp / "bound" / "model_loads.json")
        self.assertEqual(m.loads_path.parent, m.events_path.parent)
        self.assertIn("alpha::cold", read_loads(m.loads_path))

    def test_a_missing_record_is_created_rather_than_refused(self):
        note_load(self.path, "alpha", True, 14.0)
        self.assertTrue(self.path.exists())

    def test_the_write_leaves_no_temp_file_behind(self):
        note_load(self.path, "alpha", True, 14.0)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["model_loads.json"])


class WhatIsRecorded(LoadTimingCase):
    def test_a_load_is_recorded_with_its_key_its_class_and_its_seconds(self):
        note_load(self.path, "alpha", True, 14.2, at=1_700_000_000.0)
        entry = read_loads(self.path)["alpha::cold"]
        self.assertEqual((entry["model"], entry["cold"], entry["samples"], entry["last"],
                          entry["last_at"]), ("alpha", True, [14.2], 14.2, 1_700_000_000.0))

    def test_cold_and_warm_are_different_buckets_for_the_same_model(self):
        note_load(self.path, "alpha", True, 292.0)
        note_load(self.path, "alpha", False, 14.0)
        self.assertEqual(sorted(read_loads(self.path)), ["alpha::cold", "alpha::warm"])

    def test_two_models_are_different_buckets(self):
        note_load(self.path, "alpha", True, 14.0)
        note_load(self.path, "beta", True, 21.0)
        self.assertEqual(sorted(read_loads(self.path)), ["alpha::cold", "beta::cold"])

    def test_the_bucket_key_says_model_and_class_in_that_order(self):
        self.assertEqual((load_bucket("alpha", True), load_bucket("alpha", False)),
                         ("alpha::cold", "alpha::warm"))

    def test_n_counts_every_load_ever_and_samples_are_capped(self):
        last = self.seed("alpha", True, *([14.0] * (LOAD_SAMPLES_KEPT + 7)))
        entry = read_loads(self.path)["alpha::cold"]
        self.assertEqual((len(entry["samples"]), entry["n"]),
                         (LOAD_SAMPLES_KEPT, LOAD_SAMPLES_KEPT + 7))
        self.assertIsNotNone(last["baseline"])

    def test_a_flood_of_warm_loads_cannot_push_the_cold_history_out(self):
        self.seed("alpha", True, 13.0, 14.0, 15.0)
        self.seed("alpha", False, *([2.0] * (LOAD_SAMPLES_KEPT * 3)))
        self.assertEqual(read_loads(self.path)["alpha::cold"]["samples"], [13.0, 14.0, 15.0])

    def test_the_record_carries_no_paths_and_nothing_else(self):
        note_load(self.path, "alpha", True, 14.0)
        raw = self.path.read_text(encoding="utf-8")
        for forbidden in ("\\", "/"):
            self.assertNotIn(forbidden, raw, raw)
        doc = json.loads(raw)
        self.assertEqual(doc["version"], 1)
        self.assertEqual(sorted(doc["buckets"]["alpha::cold"]),
                         ["cold", "last", "last_at", "model", "n", "samples"])


class NativeLoadTimingRows(LoadTimingCase):
    """Which samples a full bucket keeps."""

    def test_the_cap_keeps_the_NEWEST_samples(self):
        self.seed("alpha", True, *range(1, LOAD_SAMPLES_KEPT + 3))
        samples = read_loads(self.path)["alpha::cold"]["samples"]
        self.assertEqual((samples[0], samples[-1]), (3, LOAD_SAMPLES_KEPT + 2))


class TheBaseline(LoadTimingCase):
    def test_nothing_measured_is_none_and_never_zero(self):
        self.assertIsNone(load_baseline([]))
        self.assertIsNone(load_baseline(None))

    def test_there_is_no_baseline_below_the_minimum_sample_count(self):
        for n in range(1, LOAD_BASELINE_MIN):
            self.assertIsNone(load_baseline([14.0] * n), n)
        self.assertEqual(load_baseline([14.0] * LOAD_BASELINE_MIN), 14.0)

    def test_it_is_the_median_and_not_the_mean(self):
        self.assertEqual(load_baseline([13.0, 14.0, 14.0, 15.0, 291.8]), 14.0)

    def test_an_even_count_averages_the_middle_pair(self):
        self.assertEqual(load_baseline([10.0, 12.0, 14.0, 16.0]), 13.0)

    def test_the_order_samples_arrive_in_does_not_matter(self):
        self.assertEqual(load_baseline([291.8, 13.0, 14.0]), 14.0)

    def test_the_first_load_of_a_bucket_reports_no_baseline(self):
        first = note_load(self.path, "alpha", True, 14.0)
        self.assertEqual((first["baseline"], first["n"], first["slow"]), (None, 0, False))

    def test_the_baseline_is_taken_over_the_history_before_this_load(self):
        self.seed("alpha", True, 14.0, 14.0, 14.0)
        note = note_load(self.path, "alpha", True, 292.0)
        self.assertEqual((note["baseline"], note["n"]), (14.0, 3))


class TheWarning(LoadTimingCase):
    def test_the_sentence_says_what_to_check(self):
        self.assertEqual(SLOW_LOAD_WARNING, "Model loading is slow, check computer")

    def test_no_baseline_means_no_warning_however_slow_the_load(self):
        self.assertFalse(load_is_slow(292.0, None))

    def test_a_normal_load_is_silent(self):
        self.assertFalse(load_is_slow(15.0, 14.0))

    def test_the_291_second_load_against_a_14_second_baseline_warns(self):
        self.assertTrue(load_is_slow(291.8, 14.0))

    def test_the_factor_alone_is_not_enough(self):
        """The FLOOR is the condition that is easy to leave out: without it
        the stand-in's second-long loads would print the warning."""
        self.assertFalse(load_is_slow(2.4, 1.2))
        self.assertFalse(load_is_slow(11.1, 1.2))
        self.assertTrue(load_is_slow(11.2, 1.2))

    def test_the_floor_alone_is_not_enough(self):
        self.assertFalse(load_is_slow(300.0, 200.0))
        self.assertTrue(load_is_slow(400.0, 200.0))

    def test_the_boundary_on_a_real_baseline(self):
        self.assertFalse(load_is_slow(27.0, 14.0))
        self.assertTrue(load_is_slow(28.0, 14.0))
        self.assertEqual(14.0 * LOAD_SLOW_FACTOR, 28.0)
        self.assertLess(LOAD_SLOW_FLOOR_S, 14.0)

    def test_the_warning_travels_with_the_folded_load(self):
        self.seed("alpha", True, 13.0, 14.0, 15.0)
        note = note_load(self.path, "alpha", True, 291.8)
        self.assertEqual((note["slow"], note["baseline"], note["bucket"]),
                         (True, 14.0, "alpha::cold"))

    def test_a_slow_load_still_joins_the_history(self):
        self.seed("alpha", True, 13.0, 14.0, 15.0, 291.8)
        self.assertEqual(read_loads(self.path)["alpha::cold"]["samples"],
                         [13.0, 14.0, 15.0, 291.8])


class ItMayNotBreakALoad(LoadTimingCase):
    def test_a_corrupt_record_reads_as_empty_rather_than_raising(self):
        self.path.write_text("{not json at all", encoding="utf-8")
        self.assertEqual(read_loads(self.path), {})

    def test_a_record_of_the_wrong_shape_reads_as_empty(self):
        self.path.write_text('["a list, not a document"]', encoding="utf-8")
        self.assertEqual(read_loads(self.path), {})

    def test_a_missing_record_reads_as_empty(self):
        self.assertEqual(read_loads(self.path), {})

    def test_a_corrupt_record_does_not_lose_the_next_load(self):
        self.path.write_text("{not json at all", encoding="utf-8")
        note = note_load(self.path, "alpha", True, 14.0)
        self.assertIsNone(note["baseline"])
        self.assertEqual(read_loads(self.path)["alpha::cold"]["samples"], [14.0])

    def test_a_bucket_holding_rubbish_samples_is_survived(self):
        # `True` is an int to Python; a flag is not a load time
        self.path.write_text(json.dumps({"version": 1, "buckets": {
            "alpha::cold": {"samples": ["14.0", None, True, 13.0]}}}), encoding="utf-8")
        note = note_load(self.path, "alpha", True, 14.0)
        self.assertIsNone(note["baseline"])
        self.assertEqual(read_loads(self.path)["alpha::cold"]["samples"], [13.0, 14.0])

    def test_a_record_that_cannot_be_written_is_an_event_and_not_a_failed_load(self):
        """CHANGED SHAPE: the existing application asserted this on the SOURCE, because
        building its manager could kill a model. Here it is done: the record
        is made unwritable, and the load still succeeds."""
        m = self.manager(fake=False)
        m.loads_path.mkdir(parents=True)            # a folder where the file goes
        b = m.acquire("alpha", W)
        self.assertIsNotNone(b)
        self.assertEqual(m.status()["loaded"], "alpha")
        self.assertIn("load_record_failed", self.kinds(m))
        self.assertEqual((m.status()["load_baseline_seconds"], m.status()["load_slow"]),
                         (None, False))


class WhatTheStatusSays(Case):
    """Carried over: what the status says - by behaviour instead of by source."""

    def test_the_class_the_baseline_and_the_flag_are_on_the_wire(self):
        m = self.manager(fake=False)
        note_load(m.loads_path, "alpha", True, 0.0)
        for _ in range(2):
            note_load(m.loads_path, "alpha", True, 0.0)
        m.acquire("alpha", W)
        st = m.status()
        self.assertEqual((st["load_cold"], st["load_baseline_seconds"], st["load_slow"]),
                         (True, 0.0, False))

    def test_an_unmeasured_baseline_goes_out_as_none_and_not_as_zero(self):
        m = self.manager(fake=False)
        m.acquire("alpha", W)
        self.assertIsNone(m.status()["load_baseline_seconds"])

    def test_the_load_class_is_dropped_when_the_model_is(self):
        m = self.manager(fake=False)
        m.acquire("alpha", W)
        m.unload()
        st = m.status()
        self.assertEqual((st["load_cold"], st["load_baseline_seconds"], st["load_slow"],
                          st["load_seconds"]), (None, None, False, 0.0))


class NativeSlowLoadRows(Case):
    """The warning itself, from a real load - and its absence."""

    def test_a_slow_load_says_so_out_loud_and_on_the_record(self):
        """The floor is lowered for this one row - the only way to make a
        stand-in's load "slow" without waiting ten seconds for it - and put
        back however the row ends."""
        m = self.manager(fake=False)
        m.cfg.mock["load_seconds"] = 0.05
        self.addCleanup(setattr, M, "LOAD_SLOW_FLOOR_S", M.LOAD_SLOW_FLOOR_S)
        M.LOAD_SLOW_FLOOR_S = 0.0
        for _ in range(3):
            note_load(m.loads_path, "alpha", True, 0.001)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            m.acquire("alpha", W)
        self.assertIn(SLOW_LOAD_WARNING, out.getvalue())
        slow = [e for e in self.events(m) if e["event"] == "load_slow"]
        self.assertEqual([(e["baseline_seconds"], e["samples"], e["cold"]) for e in slow],
                         [(0.001, 3, True)])
        self.assertIs(m.status()["load_slow"], True)

    def test_an_ordinary_load_prints_nothing(self):
        m = self.manager(fake=False)
        for _ in range(3):
            note_load(m.loads_path, "alpha", True, 0.001)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            m.acquire("alpha", W)
        self.assertEqual(out.getvalue(), "")
        self.assertNotIn("load_slow", self.kinds(m))


class ColdAndWarm(Case):
    """Carried over: cold and warm loads - by behaviour instead of by source."""

    def classes(self, m):
        return [(e["model_key"], e["cold"]) for e in self.events(m) if e["event"] == "load_ok"]

    def test_the_first_load_of_a_key_is_cold_and_a_later_one_warm(self):
        """CHANGED ON PURPOSE (S7): beta goes up beside alpha, so alpha loads
        again only once it came off - taken off here, as a swap once did."""
        m = self.manager()
        m.acquire("alpha", W)
        m.acquire("beta", W)
        m.unload("alpha")
        m.acquire("alpha", W)
        self.assertEqual(self.classes(m), [("alpha", True), ("beta", True), ("alpha", False)])
        self.assertEqual(sorted(read_loads(m.loads_path)),
                         ["alpha::cold", "alpha::warm", "beta::cold"])

    def test_unload_does_not_forget_what_has_been_loaded(self):
        m = self.manager()
        m.acquire("alpha", W)
        m.unload()
        m.acquire("alpha", W)
        self.assertEqual(self.classes(m), [("alpha", True), ("alpha", False)])


class NativeColdWarmRows(Case):
    """A reload is warm; a new process is cold again."""

    def classes(self, m):
        return [(e["model_key"], e["cold"]) for e in self.events(m) if e["event"] == "load_ok"]

    def test_a_reload_under_a_dead_backend_is_warm(self):
        m = self.manager()
        m.acquire("alpha", W).alive = False
        m.acquire("alpha", W)
        self.assertEqual(self.classes(m), [("alpha", True), ("alpha", False)])

    def test_a_new_process_loads_cold_again(self):
        self.manager().acquire("alpha", W)
        m2 = self.manager()
        m2.acquire("alpha", W)
        self.assertEqual(self.classes(m2)[-1], ("alpha", True))


# ============================================ native: giving a load up ===

SLOW_LOAD = SETTINGS.replace("load_seconds = 0", "load_seconds = 8")


class NativeAbandonRows(Case):
    """Found on the real card: a load holds the manager's lock for as long as
    the weights take to go up, and nothing could end it - a stop waited it out
    (10.2 s, for a load of 8.2), and so did a card the coordinator had taken
    back. `abandon` ends it where it stands, without the lock."""

    def slow(self):
        m = self.manager(self.config(SLOW_LOAD), fake=False)
        box = {}

        def load():
            try:
                box["got"] = m.acquire("alpha", W)
            except Exception as exc:                        # noqa: BLE001
                box["got"] = exc
        t = threading.Thread(target=load, daemon=True)
        t.start()
        deadline = time.monotonic() + 5
        while m.status()["loading"] != "alpha":
            self.assertLess(time.monotonic(), deadline, "the load never started")
            time.sleep(0.01)
        return m, t, box

    def test_a_load_given_up_fails_at_once_in_the_words_it_was_given_up_with(self):
        m, t, box = self.slow()
        started = time.monotonic()
        self.assertTrue(m.abandon("the server is stopping"))
        t.join(3)
        self.assertFalse(t.is_alive(), "the load went on after it was given up")
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIsInstance(box["got"], BackendError)
        self.assertIn("the server is stopping", str(box["got"]))
        st = m.status()
        self.assertEqual((st["loaded"], st["loading"], st["lease"]["holder"]), (None, None, None))
        (failed,) = [e for e in self.events(m) if e["event"] == "load_failed"]
        self.assertIn("the server is stopping", failed["detail"])

    def test_giving_up_with_no_load_under_way_touches_nothing(self):
        m = self.manager(fake=False)
        loaded = m.acquire("alpha", W)
        self.assertFalse(m.abandon("the server is stopping"))
        self.assertIs(m.acquire("alpha", W), loaded, "a model already up was given up")

    def test_once_stopping_nothing_more_is_loaded(self):
        m = self.manager(fake=False)
        m.stopping("the server is stopping")
        with self.assertRaises(BackendError) as caught:
            m.acquire("alpha", W)
        self.assertIn("the server is stopping", str(caught.exception))
        self.assertNotIn("load_start", self.kinds(m))

    def test_once_stopping_not_even_a_model_already_up_is_handed_out(self):
        """A generation started now would be cut short by the unload the stop
        is about to make."""
        m = self.manager(fake=False)
        m.acquire("alpha", W)
        m.stopping("the server is stopping")
        with self.assertRaises(BackendError) as caught:
            m.acquire("alpha", W)
        self.assertIn("the server is stopping", str(caught.exception))
        self.assertNotIn("reuse", self.kinds(m))

    def test_a_card_granted_as_the_server_stops_goes_straight_back(self):
        """The stop arrives while the load waits for the card, and the card is
        granted all the same: nothing goes up, and the card goes back."""
        given = []

        class Card:
            turn, lost, on_lost = None, None, "heard elsewhere"

            def take(self_, holder, model_key, for_=None, cancel=None, on_wait=None):
                m.stopping("the server is stopping")        # heard while it waited
                self_.turn = {"id": "t-1-abcdefgh"}
                return self_.turn

            def give_back(self_):
                given.append(self_.turn)
                self_.turn = None

            def status(self_):
                return None
        m = self.manager(fake=False, card=Card())
        with self.assertRaises(BackendError) as caught:
            m.acquire("alpha", W)
        self.assertIn("the server is stopping", str(caught.exception))
        self.assertEqual(given, [{"id": "t-1-abcdefgh"}], "the card was kept")
        self.assertNotIn("load_start", self.kinds(m))

    def test_a_shutdown_during_a_load_does_not_wait_it_out(self):
        m, t, box = self.slow()
        started = time.monotonic()
        m.shutdown()
        self.assertLess(time.monotonic() - started, 2.0, "the shutdown waited out the load")
        t.join(3)
        self.assertIsInstance(box["got"], BackendError)
        self.assertIsNone(m.status()["loaded"])


class NativeMachineLockRows(Case):
    """With no coordinator, a REAL card is taken through the machine's lock -
    and only a real one: a stand-in touches no card, and must never hold up a
    real server on the same machine."""

    def test_a_stand_in_model_takes_no_lock(self):
        m = self.manager(fake=False)
        self.assertIs(type(m._card), M.LocalCard)
        m.acquire("alpha", W)                               # and loads without one

    def test_a_real_card_is_taken_through_the_lock_and_given_back_with_the_model(self):
        from runner.cardlock import MachineCard
        m = self.manager(self.config(LLAMA_SETTINGS), vram=lambda: None)
        self.assertIsInstance(m._card, MachineCard)
        self.assertEqual(m._card.path.parent, _LOCKS, "this suite took the machine's own lock")
        m.acquire("alpha", W)
        self.assertTrue(m._card.held)
        m.unload()
        self.assertFalse(m._card.held)


class NativeCancelDuringALoadRows(Case):
    """Found on the real card: a job cancelled while its model loaded ended
    only once the weights were up (7.0-7.4 s after the cancel, for a load of
    about 8), and the model then sat on the card for whoever came next - or
    for nobody, until the idle sweep. Now: given up at once, unless the job
    that runs next wants the same model."""

    CORR = "c-20260925-100000-k3x9m2p7"

    def loading(self, settings, keep_loading):
        m = self.manager(self.config(settings), fake=False)
        cancel, box = threading.Event(), {}

        def load():
            try:
                box["got"] = m.acquire("alpha", W, for_=self.CORR, cancel=cancel,
                                       keep_loading=keep_loading)
            except Exception as exc:                        # noqa: BLE001
                box["got"] = exc
        t = threading.Thread(target=load, daemon=True)
        t.start()
        deadline = time.monotonic() + 5
        while m.status()["loading"] != "alpha":
            self.assertLess(time.monotonic(), deadline, "the load never started")
            time.sleep(0.01)
        return m, cancel, t, box

    def test_nothing_waiting_for_the_model_gives_the_load_up_at_once(self):
        m, cancel, t, box = self.loading(SLOW_LOAD, None)
        started = time.monotonic()
        cancel.set()
        t.join(3)
        self.assertFalse(t.is_alive(), "the load went on for nobody")
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIsInstance(box["got"], Cancelled, box["got"])
        self.assertIn("the load was given up, and nothing is on the card", str(box["got"]))
        st = m.status()
        self.assertEqual((st["loaded"], st["loading"], st["lease"]["holder"]), (None, None, None))
        (failed,) = [e for e in self.events(m) if e["event"] == "load_failed"]
        self.assertIn("its job was cancelled while alpha loaded", failed["detail"])
        self.assertEqual(failed["for"], self.CORR)

    def test_the_next_job_wanting_the_same_model_keeps_the_load_going_for_it(self):
        m, cancel, t, box = self.loading(SLOW_LOAD.replace("load_seconds = 8",
                                                           "load_seconds = 1.5"),
                                         lambda: True)
        cancel.set()
        t.join(5)
        self.assertIsInstance(box["got"], Cancelled, box["got"])
        self.assertIn("it stays up for the job that runs next", str(box["got"]))
        self.assertEqual(m.status()["loaded"], "alpha")
        self.assertNotIn("load_failed", self.kinds(m))
        (kept,) = [e for e in self.events(m) if e["event"] == "load_kept"]
        self.assertEqual(kept["for"], self.CORR)
        # and the next job gets it without a second load
        again = m.acquire("alpha", W)
        self.assertEqual(again.model_key, "alpha")
        self.assertEqual(self.kinds(m).count("load_start"), 1)
        self.assertEqual(self.kinds(m)[-1], "reuse")

    def test_a_next_job_cancelled_too_gives_the_load_up_then(self):
        wanted = {"yes": True}
        m, cancel, t, box = self.loading(SLOW_LOAD, lambda: wanted["yes"])
        cancel.set()
        time.sleep(0.8)
        self.assertTrue(t.is_alive(), "the load was given up while the next job wanted it")
        started = time.monotonic()
        wanted["yes"] = False
        t.join(3)
        self.assertFalse(t.is_alive())
        self.assertLess(time.monotonic() - started, 1.5, "it was not asked again")
        self.assertIsInstance(box["got"], Cancelled, box["got"])
        self.assertIsNone(m.status()["loaded"])

    def test_kept_for_a_next_job_that_no_longer_wants_it_once_up_comes_off(self):
        """Asked once more when the weights are up: wanted while they went up,
        and not now - the model comes off, and the record says so."""
        m = None
        m, cancel, t, box = self.loading(
            SLOW_LOAD.replace("load_seconds = 8", "load_seconds = 1.0"),
            lambda: m.status()["loading"] == "alpha")
        cancel.set()
        t.join(5)
        self.assertIsInstance(box["got"], Cancelled, box["got"])
        self.assertIn("no job waiting wants it now, so it came off", str(box["got"]))
        self.assertIsNone(m.status()["loaded"])
        # CHANGED ON PURPOSE (S6): the queue's own pipeline, its work done and
        # nothing loaded, closes after the model comes off - its line is last
        self.assertEqual(self.kinds(m)[-3:], ["unload_start", "unload_ok", "pipeline_closed"])
        self.assertNotIn("load_kept", self.kinds(m))

    def test_a_question_that_cannot_be_answered_keeps_nothing(self):
        def broken():
            raise RuntimeError("the queue could not be read")
        m, cancel, t, box = self.loading(SLOW_LOAD, broken)
        started = time.monotonic()
        cancel.set()
        t.join(3)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIsInstance(box["got"], Cancelled, box["got"])

    def test_a_load_nobody_cancels_is_left_alone(self):
        m, cancel, t, box = self.loading(SLOW_LOAD.replace("load_seconds = 8",
                                                           "load_seconds = 0.5"), None)
        t.join(5)
        self.assertEqual(box["got"].model_key, "alpha")
        self.assertEqual(m.status()["loaded"], "alpha")


# ================================================ native: the loads table ===

def _as_read(steps, records, loads) -> list:
    """A run with only what differs between two runs of one runner taken out:
    a load's id is minted fresh, so each becomes L1, L2 ... in the order it
    first appears, and a pipeline's P1, P2 ...; a time measured on the wall
    becomes "S"; this process's id "PID". Every other value stays exactly as
    it was answered."""
    names: dict = {}
    pipes: dict = {}

    def load(v):
        return names.setdefault(v, f"L{len(names) + 1}") if v else v

    def pipe(v):
        return pipes.setdefault(v, f"P{len(pipes) + 1}") if v else v

    looks = []
    for name, st, flags in steps:
        st = dict(st, load=load(st["load"]))
        if st["loaded"] is not None:
            st["load_seconds"] = "S"
        looks.append([name, st, flags])
    lines = {}
    for folder, events in records.items():
        lines[folder] = []
        for e in events:
            e = dict(e)
            if "load" in e:
                e["load"] = load(e["load"])
            if "beside" in e:                           # S7: what else was up, by load
                e["beside"] = [dict(b, load=load(b["load"])) for b in e["beside"]]
            if "pipeline" in e:
                e["pipeline"] = pipe(e["pipeline"])
            if e["event"] == "session_start":
                e["pid"] = "PID"
            if "load_seconds" in e:
                e["load_seconds"] = "S"
            lines[folder].append(e)
    kept = {folder: {bucket: dict(entry, samples=len(entry["samples"]), last="S")
                     for bucket, entry in sorted(buckets.items())}
            for folder, buckets in loads.items()}
    return [looks, lines, kept]


#: what one run answers, read through `_as_read`. First captured from the
#: runner of single slots (f30a5c5), before the loads were one table. CHANGED
#: ON PURPOSE by the pipeline (PLAN-4 S6): the queue's own pipeline opens with
#: the first load and closes once its work is done - its `pipeline_*` lines.
#: CHANGED ON PURPOSE by models side by side (S7), and nothing else moved:
#:   * beta goes up BESIDE alpha, where it once took alpha off: no unload of
#:     alpha at "beta loading"; while beta loads, the model used last is still
#:     alpha - `status()` shows it, `models()` has both up - and the lease is
#:     never released between, so it is held since the first load;
#:   * every `load_ok` names what else was up (`beside`): [] alone, alpha
#:     beside beta;
#:   * the rebind carries both loads: two `rebound_away`, two `load_carried`,
#:     the new folder's first line counting two up;
#:   * the idle sweep takes each model on its own idle clock: alpha after 690 s
#:     (last used at the reuse), then beta after 600 s.
#: The load history kept in each folder is exactly as it was.
#: CHANGED ON PURPOSE by answers at once (S17): `status()` says which models a
#: generation holds (`in_use_models`) - [] at every step here, since this
#: run's one generation names no model (`in_use()`) - and nothing else moved.
TABLE_RUN: list = [
    [
        ["nothing loaded",
         {"backend": "mock", "loaded": None, "loaded_label": None, "load": None, "loading": None,
          "in_use": 0, "in_use_models": [], "loaded_at": None, "last_used": None, "load_seconds": 0.0,
          "loads_this_session": 0, "pid": None, "port": None, "idle_unload_s": 600.0,
          "load_cold": None, "load_baseline_seconds": None, "load_slow": False,
          "lease": {"holder": None, "since": None, "held_seconds": None, "model_key": None}},
         [("alpha", False), ("beta", False)]],
        ["alpha loading",
         {"backend": "mock", "loaded": None, "loaded_label": None, "load": None,
          "loading": "alpha", "in_use": 0, "in_use_models": [], "loaded_at": None, "last_used": None,
          "load_seconds": 0.0, "loads_this_session": 0, "pid": None, "port": None,
          "idle_unload_s": 600.0, "load_cold": None, "load_baseline_seconds": None,
          "load_slow": False,
          "lease": {"holder": "srv-w5r8t2hh", "since": 1000000.0, "held_seconds": 0.0,
                    "model_key": "alpha"}},
         [("alpha", False), ("beta", False)]],
        ["alpha up",
         {"backend": "mock", "loaded": "alpha", "loaded_label": "alpha", "load": "L1",
          "loading": None, "in_use": 0, "in_use_models": [], "loaded_at": 1000000.0, "last_used": 1000000.0,
          "load_seconds": "S", "loads_this_session": 1, "pid": 40001, "port": 50001,
          "idle_unload_s": 600.0, "load_cold": True, "load_baseline_seconds": None,
          "load_slow": False,
          "lease": {"holder": "srv-w5r8t2hh", "since": 1000000.0, "held_seconds": 0.0,
                    "model_key": "alpha"}},
         [("alpha", True), ("beta", False)]],
        ["alpha used",
         {"backend": "mock", "loaded": "alpha", "loaded_label": "alpha", "load": "L1",
          "loading": None, "in_use": 0, "in_use_models": [], "loaded_at": 1000000.0, "last_used": 1000030.0,
          "load_seconds": "S", "loads_this_session": 1, "pid": 40001, "port": 50001,
          "idle_unload_s": 600.0, "load_cold": True, "load_baseline_seconds": None,
          "load_slow": False,
          "lease": {"holder": "srv-w5r8t2hh", "since": 1000000.0, "held_seconds": 30.0,
                    "model_key": "alpha"}},
         [("alpha", True), ("beta", False)]],
        ["alpha reused",
         {"backend": "mock", "loaded": "alpha", "loaded_label": "alpha", "load": "L1",
          "loading": None, "in_use": 0, "in_use_models": [], "loaded_at": 1000000.0, "last_used": 1000060.0,
          "load_seconds": "S", "loads_this_session": 1, "pid": 40001, "port": 50001,
          "idle_unload_s": 600.0, "load_cold": True, "load_baseline_seconds": None,
          "load_slow": False,
          "lease": {"holder": "srv-w5r8t2hh", "since": 1000000.0, "held_seconds": 60.0,
                    "model_key": "alpha"}},
         [("alpha", True), ("beta", False)]],
        ["beta loading",
         {"backend": "mock", "loaded": "alpha", "loaded_label": "alpha", "load": "L1",
          "loading": "beta", "in_use": 1, "in_use_models": [], "loaded_at": 1000000.0, "last_used": 1000060.0,
          "load_seconds": "S", "loads_this_session": 1, "pid": 40001, "port": 50001,
          "idle_unload_s": 600.0, "load_cold": True, "load_baseline_seconds": None,
          "load_slow": False,
          "lease": {"holder": "srv-w5r8t2hh", "since": 1000000.0, "held_seconds": 90.0,
                    "model_key": "beta"}},
         [("alpha", True), ("beta", False)]],
        ["beta up, a generation on it",
         {"backend": "mock", "loaded": "beta", "loaded_label": "beta", "load": "L2",
          "loading": None, "in_use": 1, "in_use_models": [], "loaded_at": 1000090.0, "last_used": 1000090.0,
          "load_seconds": "S", "loads_this_session": 2, "pid": 40001, "port": 50001,
          "idle_unload_s": 600.0, "load_cold": True, "load_baseline_seconds": None,
          "load_slow": False,
          "lease": {"holder": "srv-w5r8t2hh", "since": 1000000.0, "held_seconds": 90.0,
                    "model_key": "beta"}},
         [("alpha", True), ("beta", True)]],
        ["the generation over",
         {"backend": "mock", "loaded": "beta", "loaded_label": "beta", "load": "L2",
          "loading": None, "in_use": 0, "in_use_models": [], "loaded_at": 1000090.0, "last_used": 1000120.0,
          "load_seconds": "S", "loads_this_session": 2, "pid": 40001, "port": 50001,
          "idle_unload_s": 600.0, "load_cold": True, "load_baseline_seconds": None,
          "load_slow": False,
          "lease": {"holder": "srv-w5r8t2hh", "since": 1000000.0, "held_seconds": 120.0,
                    "model_key": "beta"}},
         [("alpha", True), ("beta", True)]],
        ["beta loading",
         {"backend": "mock", "loaded": "alpha", "loaded_label": "alpha", "load": "L1",
          "loading": "beta", "in_use": 0, "in_use_models": [], "loaded_at": 1000000.0, "last_used": 1000120.0,
          "load_seconds": "S", "loads_this_session": 2, "pid": 40001, "port": 50001,
          "idle_unload_s": 600.0, "load_cold": True, "load_baseline_seconds": None,
          "load_slow": False,
          "lease": {"holder": "srv-w5r8t2hh", "since": 1000000.0, "held_seconds": 150.0,
                    "model_key": "beta"}},
         [("alpha", True), ("beta", False)]],
        ["beta dead, loaded again",
         {"backend": "mock", "loaded": "beta", "loaded_label": "beta", "load": "L3",
          "loading": None, "in_use": 0, "in_use_models": [], "loaded_at": 1000150.0, "last_used": 1000150.0,
          "load_seconds": "S", "loads_this_session": 3, "pid": 40001, "port": 50001,
          "idle_unload_s": 600.0, "load_cold": False, "load_baseline_seconds": None,
          "load_slow": False,
          "lease": {"holder": "srv-w5r8t2hh", "since": 1000000.0, "held_seconds": 150.0,
                    "model_key": "beta"}},
         [("alpha", True), ("beta", True)]],
        ["rebound",
         {"backend": "mock", "loaded": "beta", "loaded_label": "beta", "load": "L3",
          "loading": None, "in_use": 0, "in_use_models": [], "loaded_at": 1000150.0, "last_used": 1000150.0,
          "load_seconds": "S", "loads_this_session": 3, "pid": 40001, "port": 50001,
          "idle_unload_s": 600.0, "load_cold": False, "load_baseline_seconds": None,
          "load_slow": False,
          "lease": {"holder": "srv-w5r8t2hh", "since": 1000000.0, "held_seconds": 150.0,
                    "model_key": "beta"}},
         [("alpha", True), ("beta", True)]],
        ["idle, off the card",
         {"backend": "mock", "loaded": None, "loaded_label": None, "load": None, "loading": None,
          "in_use": 0, "in_use_models": [], "loaded_at": None, "last_used": 1000150.0, "load_seconds": 0.0,
          "loads_this_session": 3, "pid": None, "port": None, "idle_unload_s": 600.0,
          "load_cold": None, "load_baseline_seconds": None, "load_slow": False,
          "lease": {"holder": None, "since": None, "held_seconds": None, "model_key": None}},
         [("alpha", False), ("beta", False)]],
        ["alpha loading",
         {"backend": "mock", "loaded": None, "loaded_label": None, "load": None,
          "loading": "alpha", "in_use": 0, "in_use_models": [], "loaded_at": None, "last_used": 1000150.0,
          "load_seconds": 0.0, "loads_this_session": 3, "pid": None, "port": None,
          "idle_unload_s": 600.0, "load_cold": None, "load_baseline_seconds": None,
          "load_slow": False,
          "lease": {"holder": "srv-w5r8t2hh", "since": 1000750.0, "held_seconds": 0.0,
                    "model_key": "alpha"}},
         [("alpha", False), ("beta", False)]],
        ["a load that failed",
         {"backend": "mock", "loaded": None, "loaded_label": None, "load": None, "loading": None,
          "in_use": 0, "in_use_models": [], "loaded_at": None, "last_used": 1000150.0, "load_seconds": 0.0,
          "loads_this_session": 3, "pid": None, "port": None, "idle_unload_s": 600.0,
          "load_cold": None, "load_baseline_seconds": None, "load_slow": False,
          "lease": {"holder": None, "since": None, "held_seconds": None, "model_key": None}},
         [("alpha", False), ("beta", False)]],
        ["alpha loading",
         {"backend": "mock", "loaded": None, "loaded_label": None, "load": None,
          "loading": "alpha", "in_use": 0, "in_use_models": [], "loaded_at": None, "last_used": 1000150.0,
          "load_seconds": 0.0, "loads_this_session": 3, "pid": None, "port": None,
          "idle_unload_s": 600.0, "load_cold": None, "load_baseline_seconds": None,
          "load_slow": False,
          "lease": {"holder": "srv-w5r8t2hh", "since": 1000750.0, "held_seconds": 0.0,
                    "model_key": "alpha"}},
         [("alpha", False), ("beta", False)]],
        ["a stop that threw",
         {"backend": "mock", "loaded": None, "loaded_label": None, "load": None, "loading": None,
          "in_use": 0, "in_use_models": [], "loaded_at": None, "last_used": 1000750.0, "load_seconds": 0.0,
          "loads_this_session": 4, "pid": None, "port": None, "idle_unload_s": 600.0,
          "load_cold": None, "load_baseline_seconds": None, "load_slow": False,
          "lease": {"holder": None, "since": None, "held_seconds": None, "model_key": None}},
         [("alpha", False), ("beta", False)]],
        ["beta refused beside it",
         {"backend": "mock", "loaded": None, "loaded_label": None, "load": None, "loading": None,
          "in_use": 0, "in_use_models": [], "loaded_at": None, "last_used": 1000750.0, "load_seconds": 0.0,
          "loads_this_session": 4, "pid": None, "port": None, "idle_unload_s": 600.0,
          "load_cold": None, "load_baseline_seconds": None, "load_slow": False,
          "lease": {"holder": None, "since": None, "held_seconds": None, "model_key": None}},
         [("alpha", False), ("beta", False)]],
        ["beta loading",
         {"backend": "mock", "loaded": None, "loaded_label": None, "load": None,
          "loading": "beta", "in_use": 0, "in_use_models": [], "loaded_at": None, "last_used": 1000750.0,
          "load_seconds": 0.0, "loads_this_session": 4, "pid": None, "port": None,
          "idle_unload_s": 600.0, "load_cold": None, "load_baseline_seconds": None,
          "load_slow": False,
          "lease": {"holder": "srv-w5r8t2hh", "since": 1000780.0, "held_seconds": 0.0,
                    "model_key": "beta"}},
         [("alpha", False), ("beta", False)]],
        ["beta up after it",
         {"backend": "mock", "loaded": "beta", "loaded_label": "beta", "load": "L4",
          "loading": None, "in_use": 0, "in_use_models": [], "loaded_at": 1000780.0, "last_used": 1000780.0,
          "load_seconds": "S", "loads_this_session": 5, "pid": 40001, "port": 50001,
          "idle_unload_s": 600.0, "load_cold": False, "load_baseline_seconds": None,
          "load_slow": False,
          "lease": {"holder": "srv-w5r8t2hh", "since": 1000780.0, "held_seconds": 0.0,
                    "model_key": "beta"}},
         [("alpha", False), ("beta", True)]],
        ["stopped",
         {"backend": "mock", "loaded": None, "loaded_label": None, "load": None, "loading": None,
          "in_use": 0, "in_use_models": [], "loaded_at": None, "last_used": 1000780.0, "load_seconds": 0.0,
          "loads_this_session": 5, "pid": None, "port": None, "idle_unload_s": 600.0,
          "load_cold": None, "load_baseline_seconds": None, "load_slow": False,
          "lease": {"holder": None, "since": None, "held_seconds": None, "model_key": None}},
         [("alpha", False), ("beta", False)]],
    ],
    {
        "bound": [
            {"ts": 1000000.0, "event": "session_start", "backend": "mock", "live_after": 0,
             "pid": "PID"},
            {"ts": 1000000.0, "event": "pipeline_opened", "backend": "mock", "live_after": 0,
             "pipeline": "P1", "opened_by": "queue", "class": "responsive", "label": None,
             "models": [], "keep_s": None},
            {"ts": 1000000.0, "event": "load_start", "backend": "mock", "live_after": 0,
             "model_key": "alpha", "load": "L1", "port": None,
             "for": "c-20260925-110000-k3x9m2p7"},
            {"ts": 1000000.0, "event": "load_ok", "backend": "mock", "live_after": 1,
             "model_key": "alpha", "load": "L1", "pid": 40001, "port": 50001, "gguf": None,
             "cold": True, "load_seconds": "S", "beside": [],
             "for": "c-20260925-110000-k3x9m2p7"},
            {"ts": 1000060.0, "event": "reuse", "backend": "mock", "live_after": 1,
             "model_key": "alpha", "load": "L1"},
            {"ts": 1000090.0, "event": "load_start", "backend": "mock", "live_after": 1,
             "model_key": "beta", "load": "L2", "port": None},
            {"ts": 1000090.0, "event": "load_ok", "backend": "mock", "live_after": 2,
             "model_key": "beta", "load": "L2", "pid": 40001, "port": 50001, "gguf": None,
             "cold": True, "load_seconds": "S", "beside": [{"model_key": "alpha", "load": "L1"}]},
            {"ts": 1000150.0, "event": "stale_backend", "backend": "mock", "live_after": 2,
             "model_key": "beta", "load": "L2",
             "reason": "its process is gone; loading it again"},
            {"ts": 1000150.0, "event": "unload_start", "backend": "mock", "live_after": 2,
             "model_key": "beta", "load": "L2", "pid": 40001, "port": 50001, "gguf": None},
            {"ts": 1000150.0, "event": "unload_ok", "backend": "mock", "live_after": 1,
             "model_key": "beta", "load": "L2", "pid": 40001, "port": 50001, "gguf": None},
            {"ts": 1000150.0, "event": "load_start", "backend": "mock", "live_after": 1,
             "model_key": "beta", "load": "L3", "port": None},
            {"ts": 1000150.0, "event": "load_ok", "backend": "mock", "live_after": 2,
             "model_key": "beta", "load": "L3", "pid": 40001, "port": 50001, "gguf": None,
             "cold": False, "load_seconds": "S",
             "beside": [{"model_key": "alpha", "load": "L1"}]},
            {"ts": 1000150.0, "event": "rebound_away", "backend": "mock", "live_after": 2,
             "model_key": "alpha", "load": "L1", "pid": 40001, "port": 50001, "gguf": None},
            {"ts": 1000150.0, "event": "rebound_away", "backend": "mock", "live_after": 2,
             "model_key": "beta", "load": "L3", "pid": 40001, "port": 50001, "gguf": None},
        ],
        "other": [
            {"ts": 1000150.0, "event": "session_start", "backend": "mock", "live_after": 2,
             "pid": "PID", "rebound": True},
            {"ts": 1000150.0, "event": "load_carried", "backend": "mock", "live_after": 2,
             "model_key": "alpha", "load": "L1", "pid": 40001, "port": 50001, "gguf": None},
            {"ts": 1000150.0, "event": "load_carried", "backend": "mock", "live_after": 2,
             "model_key": "beta", "load": "L3", "pid": 40001, "port": 50001, "gguf": None},
            {"ts": 1000750.0, "event": "idle_unload", "backend": "mock", "live_after": 2,
             "model_key": "alpha", "load": "L1", "idle_seconds": 690.0},
            {"ts": 1000750.0, "event": "unload_start", "backend": "mock", "live_after": 2,
             "model_key": "alpha", "load": "L1", "pid": 40001, "port": 50001, "gguf": None},
            {"ts": 1000750.0, "event": "unload_ok", "backend": "mock", "live_after": 1,
             "model_key": "alpha", "load": "L1", "pid": 40001, "port": 50001, "gguf": None},
            {"ts": 1000750.0, "event": "idle_unload", "backend": "mock", "live_after": 1,
             "model_key": "beta", "load": "L3", "idle_seconds": 600.0},
            {"ts": 1000750.0, "event": "unload_start", "backend": "mock", "live_after": 1,
             "model_key": "beta", "load": "L3", "pid": 40001, "port": 50001, "gguf": None},
            {"ts": 1000750.0, "event": "unload_ok", "backend": "mock", "live_after": 0,
             "model_key": "beta", "load": "L3", "pid": 40001, "port": 50001, "gguf": None},
            {"ts": 1000750.0, "event": "pipeline_closed", "backend": "mock", "live_after": 0,
             "pipeline": "P1", "why": "its work is done, and nothing is loaded"},
            {"ts": 1000750.0, "event": "pipeline_opened", "backend": "mock", "live_after": 0,
             "pipeline": "P2", "opened_by": "queue", "class": "responsive", "label": None,
             "models": [], "keep_s": None},
            {"ts": 1000750.0, "event": "load_start", "backend": "mock", "live_after": 0,
             "model_key": "alpha", "load": "L5", "port": None},
            {"ts": 1000750.0, "event": "load_failed", "backend": "mock", "live_after": 0,
             "model_key": "alpha", "load": "L5", "detail": "no weights"},
            {"ts": 1000750.0, "event": "pipeline_closed", "backend": "mock", "live_after": 0,
             "pipeline": "P2", "why": "its work is done, and nothing is loaded"},
            {"ts": 1000750.0, "event": "pipeline_opened", "backend": "mock", "live_after": 0,
             "pipeline": "P3", "opened_by": "queue", "class": "responsive", "label": None,
             "models": [], "keep_s": None},
            {"ts": 1000750.0, "event": "load_start", "backend": "mock", "live_after": 0,
             "model_key": "alpha", "load": "L6", "port": None},
            {"ts": 1000750.0, "event": "load_ok", "backend": "mock", "live_after": 1,
             "model_key": "alpha", "load": "L6", "pid": 40001, "port": 50001, "gguf": None,
             "cold": False, "load_seconds": "S", "beside": []},
            {"ts": 1000750.0, "event": "unload_start", "backend": "mock", "live_after": 1,
             "model_key": "alpha", "load": "L6", "pid": 40001, "port": 50001, "gguf": None},
            {"ts": 1000750.0, "event": "unload_failed", "backend": "mock", "live_after": 1,
             "model_key": "alpha", "load": "L6", "pid": 40001, "port": 50001, "gguf": None,
             "detail": "would not die"},
            {"ts": 1000780.0, "event": "unload_start", "backend": "mock", "live_after": 0,
             "model_key": "alpha", "load": "L6", "pid": 40001, "port": 50001, "gguf": None,
             "again": True},
            {"ts": 1000780.0, "event": "unload_failed", "backend": "mock", "live_after": 0,
             "model_key": "alpha", "load": "L6", "pid": 40001, "port": 50001, "gguf": None,
             "again": True, "detail": "would not die"},
            {"ts": 1000780.0, "event": "unload_start", "backend": "mock", "live_after": 0,
             "model_key": "alpha", "load": "L6", "pid": 40001, "port": 50001, "gguf": None,
             "again": True},
            {"ts": 1000780.0, "event": "unload_ok", "backend": "mock", "live_after": 0,
             "model_key": "alpha", "load": "L6", "pid": 40001, "port": 50001, "gguf": None,
             "again": True},
            {"ts": 1000780.0, "event": "load_start", "backend": "mock", "live_after": 0,
             "model_key": "beta", "load": "L4", "port": None},
            {"ts": 1000780.0, "event": "load_ok", "backend": "mock", "live_after": 1,
             "model_key": "beta", "load": "L4", "pid": 40001, "port": 50001, "gguf": None,
             "cold": False, "load_seconds": "S", "beside": []},
            {"ts": 1000780.0, "event": "unload_start", "backend": "mock", "live_after": 1,
             "model_key": "beta", "load": "L4", "pid": 40001, "port": 50001, "gguf": None},
            {"ts": 1000780.0, "event": "unload_ok", "backend": "mock", "live_after": 0,
             "model_key": "beta", "load": "L4", "pid": 40001, "port": 50001, "gguf": None},
            {"ts": 1000780.0, "event": "pipeline_closed", "backend": "mock", "live_after": 0,
             "pipeline": "P3", "why": "the server stopped"},
        ],
    },
    {
        "bound": {
            "alpha::cold": {"model": "alpha", "cold": True, "samples": 1, "n": 1, "last": "S",
                            "last_at": 1000000.0},
            "beta::cold": {"model": "beta", "cold": True, "samples": 1, "n": 1, "last": "S",
                           "last_at": 1000090.0},
            "beta::warm": {"model": "beta", "cold": False, "samples": 1, "n": 1, "last": "S",
                           "last_at": 1000150.0},
        },
        "other": {
            "alpha::warm": {"model": "alpha", "cold": False, "samples": 1, "n": 1, "last": "S",
                            "last_at": 1000750.0},
            "beta::warm": {"model": "beta", "cold": False, "samples": 1, "n": 1, "last": "S",
                           "last_at": 1000780.0},
        },
    },
]


class NativeLoadTableRows(Case):
    """The runner's loads are ONE table (`LoadTable`), keyed by load id: the
    slots it replaced held one model's facts each. Since S7 it holds loads
    side by side - one load of each model, and refuses a second of one.

    The ISOLATION rows come first - one run answers as it did before the
    table, every change since named; and the table never holds two loads of
    one model. Then the table's own bookkeeping."""

    FIRST = "c-20260925-110000-k3x9m2p7"

    def the_run(self) -> list:
        """Every door a load passes, once: a cold load, a use, a reuse, a swap
        while a generation holds the card, a dead backend loaded again, a
        rebind, the idle sweep, a load that fails, a stop that throws, a load
        after it, the stop. What `status()` and `models()` answered at each
        step and in the middle of every load, every line of both folders'
        records, and the load times each folder kept."""
        steps, how = [], {"fail": None, "stop_fails": None}

        def look(name):
            steps.append([name, m.status(), [(x["key"], x["loaded"])
                                              for x in m.models()["models"]]])

        def make(key, port):
            b = FakeBackend(key, fail=how["fail"], stop_fails=how["stop_fails"],
                            on_start=lambda _b: look(f"{key} loading"))
            self.made.append(b)
            return b
        m = self.manager(backend_factory=make)
        m.cfg.runner["idle_unload_s"] = 600
        look("nothing loaded")
        m.acquire("alpha", W, for_=self.FIRST)
        look("alpha up")
        self.clock.tick(30)
        m.touch()
        look("alpha used")
        self.clock.tick(30)
        m.acquire("alpha", W)
        look("alpha reused")
        with m.in_use():
            self.clock.tick(30)
            beta = m.acquire("beta", W)
            look("beta up, a generation on it")
            self.clock.tick(30)
        look("the generation over")
        self.clock.tick(30)
        beta.alive = False
        m.acquire("beta", W)
        look("beta dead, loaded again")
        m.rebind(self.tmp / "other")
        look("rebound")
        self.clock.tick(600)
        m.maybe_idle_unload()
        look("idle, off the card")
        how["fail"] = "no weights"
        with self.assertRaises(BackendError):
            m.acquire("alpha", W)
        look("a load that failed")
        how.update(fail=None, stop_fails="would not die")
        m.acquire("alpha", W)
        stuck = self.made[-1]
        with self.assertRaises(RuntimeError):
            m.unload()
        look("a stop that threw")
        how["stop_fails"] = None
        self.clock.tick(30)
        with self.assertRaises(BackendError):
            m.acquire("beta", W)                    # its stop is asked again, and fails again
        look("beta refused beside it")
        stuck.stop_fails = None
        m.acquire("beta", W)                        # it stops now: beta goes up, and alone
        look("beta up after it")
        m.shutdown()
        look("stopped")
        folders = ("bound", "other")
        return _as_read(steps, {f: self.events(self.tmp / f / M.EVENTS_NAME) for f in folders},
                        {f: read_loads(self.tmp / f / M.LOADS_NAME) for f in folders})

    def test_one_run_answers_exactly_as_it_did_before_the_table(self):
        """ISOLATION. `status()` - health's `runner`, verbatim - `models()` and
        the record, for one run, compared with what the runner of single slots
        answered for the same run, every change since named where the run is
        kept (`TABLE_RUN`). It pins what no other row does: the card's idle
        clock outlives the load it last timed (`last_used` after an unload,
        as the contract's own health example shows it), the in-use count is
        one for every load, and a load's facts leave with it.

        CHANGED ON PURPOSE (S7): the run is the same run; what it answers is
        models side by side - beta beside alpha, `beside` on every load, both
        carried by the rebind, each swept on its own clock (`TABLE_RUN`'s
        comment says each difference).

        ONE PART CHANGED ON PURPOSE (S3): after the stop that threw, the old
        runner loaded beta beside the model that may still have been on the
        card, and its record read two loads at once (S1's register row 3).
        Now that stop is asked again before beta: beta is refused while it
        fails, and goes up once it works - the record closing the old load
        first."""
        looks, lines, kept = self.the_run()
        want_looks, want_lines, want_kept = TABLE_RUN
        self.assertEqual([step[0] for step in looks], [step[0] for step in want_looks])
        for got, want in zip(looks, want_looks):
            with self.subTest(step=got[0]):
                self.assertEqual(got, want)
        for folder, want in want_lines.items():
            with self.subTest(record=folder):
                self.assertEqual(lines[folder], want)
        self.assertEqual(kept, want_kept)

    def test_the_table_never_holds_two_loads_of_one_model(self):
        """CHANGED ON PURPOSE (S7; was "the table never holds two loads", the
        ISOLATION row for one model at a time): models go up side by side,
        and the rule the table keeps is one load of EACH model. Its entries
        are counted at every step of a busy run, in the middle of every load
        and of every stop, and while two callers for one model wait for one
        card - where a load has no id yet, so no entry either. The two callers
        once raced to a refusal ("loaded while it waited"); now the second
        waits for the load lock, and is handed the load the first made."""
        seen, how = [], {"fail": None}

        def note(what):
            seen.append((what, len(m._loads)))

        class Counted(FakeBackend):
            def stop(self_):
                note(f"stop {self_.model_key}")
                super().stop()

        def make(key, port):
            b = Counted(key, fail=how["fail"], on_start=lambda _b: note(f"load {key}"))
            self.made.append(b)
            return b

        class Card:
            """A card that makes its callers wait until it is opened."""
            turn = lost = None

            def __init__(self_):
                self_.open, self_.arrived = threading.Event(), []
                self_.open.set()

            def take(self_, holder, model_key, for_=None, cancel=None, on_wait=None):
                self_.arrived.append(model_key)
                self_.open.wait(5)

            def give_back(self_):
                pass

            def status(self_):
                return None
        card = Card()
        m = self.manager(backend_factory=make, card=card)
        m.acquire("alpha", W)
        note("alpha up")
        m.acquire("alpha", W)
        note("alpha reused")
        beta = m.acquire("beta", W)
        note("beta up beside it")
        beta.alive = False
        m.acquire("beta", W)
        note("beta loaded again")
        m.unload("alpha")
        note("alpha off")
        how["fail"] = "no weights"
        with self.assertRaises(BackendError):
            m.acquire("alpha", W)
        note("a load that failed")
        how["fail"] = None
        m.unload()
        note("nothing up")
        # two callers for one model pass the first look with nothing loaded,
        # and both wait for the card
        card.open.clear()
        card.arrived.clear()
        got = []

        def racer():
            try:
                got.append(m.acquire("alpha", W))
            except Exception as exc:                        # noqa: BLE001 - the row reads it
                got.append(exc)
        racers = [threading.Thread(target=racer, daemon=True) for _ in range(2)]
        for t in racers:
            t.start()
        deadline = time.monotonic() + 5
        while len(card.arrived) < 2:
            self.assertLess(time.monotonic(), deadline, "the two never both waited")
            time.sleep(0.01)
        note("two waiting for the card")
        self.assertEqual((m._loads.card_waiting, m.loading), ("alpha", True))
        card.open.set()
        for t in racers:
            t.join(5)
            self.assertFalse(t.is_alive(), "a caller never came back")
        note("one load, both handed it")
        self.assertEqual(len(got), 2, got)
        self.assertIsInstance(got[0], FakeBackend, got)
        self.assertIs(got[0], got[1], "two loads of one model, or a refusal")
        m.unload()
        note("nothing up again")
        self.assertEqual(seen, [
            ("load alpha", 1), ("alpha up", 1), ("alpha reused", 1),
            ("load beta", 2), ("beta up beside it", 2),
            ("stop beta", 2), ("load beta", 2), ("beta loaded again", 2),
            ("stop alpha", 2), ("alpha off", 1),
            ("load alpha", 2), ("a load that failed", 1),
            ("stop beta", 1), ("nothing up", 0),
            ("two waiting for the card", 0), ("load alpha", 1), ("one load, both handed it", 1),
            ("stop alpha", 1), ("nothing up again", 0)])
        events = self.events(m)
        self.assertEqual((max_concurrency(events), runner.side_by_side(events)), (1, 2))

    def test_the_table_refuses_a_second_load_of_one_model_and_keeps_the_first(self):
        """CHANGED ON PURPOSE (S7; was "...a second load and keeps the first"):
        the table's own rule, alone - one load of each model. `acquire` never
        asks for a second, so only the table itself can show that it refuses
        one too: beside a load of the model under way, one up, and one being
        stopped. A load of another model goes in beside it."""
        t = M.LoadTable()
        first = t.begin("load-aaaaaaaa", "alpha", FakeBackend("alpha"), None)
        with self.assertRaises(M.ConcurrencyViolation) as caught:
            t.begin("load-bbbbbbbb", "alpha", FakeBackend("alpha"), None)
        self.assertIn("load-aaaaaaaa", str(caught.exception))
        self.assertEqual((t.ids(), t.loading()), (("load-aaaaaaaa",), first))
        t.came_up(first, 0.5, self.clock())
        with self.assertRaises(M.ConcurrencyViolation):
            t.begin("load-bbbbbbbb", "alpha", FakeBackend("alpha"), None)
        beside = t.begin("load-cccccccc", "beta", FakeBackend("beta"), None)
        self.assertEqual((t.ids(), t.loaded(), t.loading(), t.live),
                         (("load-aaaaaaaa", "load-cccccccc"), first, beside, 1))
        first.stopping = True
        with self.assertRaises(M.ConcurrencyViolation):
            t.begin("load-dddddddd", "alpha", FakeBackend("alpha"), None)
        self.assertEqual((t.ids(), t.up(), t.on_card()),
                         (("load-aaaaaaaa", "load-cccccccc"), (), (first,)))

    def test_a_load_is_one_entry_from_under_way_to_up_to_gone(self):
        """Keyed by its id from the moment the id is minted; the same entry
        comes up; ending it takes it off and its count with it - never the
        session's count of loads, and never the idle clock, which outlives
        the load it last timed. A load that never came up counts nothing."""
        t = M.LoadTable()
        self.assertEqual((len(t), t.ids(), t.loaded(), t.loading(), t.live,
                          t.loads_this_session, t.last_used, t.in_use, t.card_waiting),
                         (0, (), None, None, 0, 0, None, 0, None))
        b = FakeBackend("alpha")
        e = t.begin("load-aaaaaaaa", "alpha", b, 41000)
        self.assertEqual((e.load_id, e.model_key, e.backend, e.port),
                         ("load-aaaaaaaa", "alpha", b, 41000))
        self.assertEqual((t.ids(), t.loading(), t.loaded(), t.live, t.loads_this_session),
                         (("load-aaaaaaaa",), e, None, 0, 0))
        t.came_up(e, 1.25, 1000.0)
        self.assertEqual((t.ids(), t.loading(), t.loaded(), t.live, t.loads_this_session,
                          t.last_used, e.loaded_at, e.load_seconds),
                         (("load-aaaaaaaa",), None, e, 1, 1, 1000.0, 1000.0, 1.25))
        t.end(e)
        self.assertEqual((t.ids(), t.loaded(), t.live, t.loads_this_session, t.last_used),
                         ((), None, 0, 1, 1000.0))
        never = t.begin("load-bbbbbbbb", "beta", FakeBackend("beta"), None)
        t.end(never)
        self.assertEqual((t.ids(), t.live, t.loads_this_session), ((), 0, 1))

    def test_the_load_under_way_is_the_entry_its_record_names_and_comes_up_in_place(self):
        mid = []
        m = self.manager(backend_factory=self.factory(on_start=lambda b: mid.append(
            (m._loads.ids(), m._loads.loading(), m._loads.loaded()))))
        b = m.acquire("alpha", W)
        (start,) = [e for e in self.events(m) if e["event"] == "load_start"]
        ((ids_then, under_way, up_then),) = mid
        self.assertEqual(ids_then, (start["load"],), "the load under way is not keyed by its id")
        self.assertIs(under_way.backend, b)
        self.assertIsNone(up_then)
        self.assertEqual(m._loads.ids(), (start["load"],))
        self.assertIs(m._loads.loaded(), under_way, "the load came up as another entry")
        m.unload()
        self.assertEqual((m._loads.ids(), m._loads.loaded()), ((), None))


# ============================================ native: the pipeline (S6) ===

def _free(path) -> bool:
    """Could anybody take the card's lock now? The lock itself is asked -
    never through a wait. A second handle in this same process is refused like
    any other process's, so this is also "does the manager hold it"."""
    fh = _CL._try_lock(Path(path))
    if fh is None:
        return False
    _CL._unlock(fh)
    return True


class RecordingCard:
    """A card source that writes down what it was asked - each take, the
    terms it came with, the thread it came on, each give-back - and that a
    row can tell the card is wanted, as a coordinator's heartbeat would."""
    kind = "recording card"

    def __init__(self):
        self.turn = self.lost = None
        self.on_lost = self.on_wanted = None
        self.wanted = False
        self.takes, self.terms, self.threads, self.gave = [], [], [], []

    def take(self, holder, model_key, for_=None, cancel=None, on_wait=None, terms=None):
        self.takes.append(model_key)
        self.terms.append(terms)
        self.threads.append(threading.current_thread().name)
        # a new grant is a card held again, as a coordinator's claim is
        self.turn, self.lost = {"id": f"t-{len(self.takes):04d}-abcd"}, None
        return self.turn

    def give_back(self):
        if self.turn is not None:
            self.gave.append(self.turn["id"])
        self.turn = None

    def status(self):
        return None


class NativePipelineRows(Case):
    """PLAN-4 D6, S6: the card is held from a pipeline's open to its close -
    one ticket's rental with a coordinator, the machine's lock without one -
    and a model switch inside it never gives the card back. Without an open
    pipeline the queue opens one of its own for its work, and closes it once
    the work is done: at once when the card is wanted, otherwise when the idle
    sweep has taken its model off. Still one model at a time (S7 puts several
    side by side). The clock is the row's."""

    def wait_for(self, what, pred, timeout=5.0):
        deadline = time.monotonic() + timeout
        while not pred():
            self.assertLess(time.monotonic(), deadline, f"timed out waiting for {what}")
            time.sleep(0.01)

    def holding(self, m):
        self.wait_for("the pipeline to hold the card",
                      lambda: m.pipeline_status()["state"] == "holding")
        return m.pipeline_status()

    def loaded(self, m, key):
        self.wait_for(f"{key} to be up", lambda: m.status()["loaded"] == key)

    def lines(self, m):
        return [e for e in self.kinds(m) if e.startswith(("pipeline_", "card_"))]

    def test_a_pipeline_holds_the_card_from_its_open_to_its_close(self):
        path = self.tmp / "card.lock"
        m = self.manager(card=MachineCard(path))
        self.assertTrue(_free(path))
        opened = m.open_pipeline(W, keep_s=0)
        held = self.holding(m)
        self.assertFalse(_free(path), "an open pipeline did not take the card")
        self.assertEqual((held["id"], held["opened_by"], held["models"], held["keep_s"]),
                         (opened["id"], "application", [], 0.0))
        self.assertTrue(ids.matches("pipeline", held["id"]), held["id"])
        self.assertIsNone(m.status()["loaded"], "a pipeline named no model, and one went up")
        closed = m.close_pipeline(opened["id"])
        self.assertTrue(_free(path), "a closed pipeline kept the card")
        self.assertEqual((closed["id"], closed["state"], closed["why"]),
                         (opened["id"], "closed", "closed by the application"))
        self.assertEqual(self.lines(m), ["pipeline_opened", "card_granted", "card_given_back",
                                         "pipeline_closed"])
        granted = next(e for e in self.events(m) if e["event"] == "card_granted")
        self.assertEqual((granted["source"], granted["pipeline"]), ("machine lock", opened["id"]))

    def test_a_model_switch_inside_a_pipeline_never_gives_the_card_back(self):
        """REVERSES "a model switch gives it back and asks again" (the server
        README; test_coordination's switch row, rewritten on purpose there):
        the card is held across every switch, and goes back at the close."""
        path = self.tmp / "card.lock"
        mid = []
        m = self.manager(card=MachineCard(path), backend_factory=self.factory(
            on_start=lambda b: mid.append((b.model_key, _free(path)))))
        opened = m.open_pipeline(W, models=["alpha"], keep_s=0)
        self.loaded(m, "alpha")
        m.acquire("beta", W)                                # beta beside alpha
        m.unload("alpha")                                   # the swap is the application's
        m.acquire("alpha", W)                               # and back
        # CHANGED ON PURPOSE (S7): beta goes up beside alpha, so the switch
        # back takes alpha off by hand first - the card held throughout
        self.assertEqual(mid, [("alpha", False), ("beta", False), ("alpha", False)],
                         "the card was free while a model went up")
        self.assertEqual(self.kinds(m).count("card_granted"), 1, "the card was taken twice")
        self.assertNotIn("card_given_back", self.kinds(m), "a switch gave the card back")
        m.close_pipeline(opened["id"])
        self.assertTrue(_free(path))

    def test_a_switch_in_the_queues_own_pipeline_keeps_the_card_too(self):
        path = self.tmp / "card.lock"
        mid = []
        m = self.manager(card=MachineCard(path), backend_factory=self.factory(
            on_start=lambda b: mid.append((b.model_key, _free(path)))))
        with m.in_use():
            m.acquire("alpha", W)
        with m.in_use():
            m.acquire("beta", W)
        self.assertEqual(mid, [("alpha", False), ("beta", False)])
        self.assertEqual(self.lines(m), ["pipeline_opened", "card_granted"])

    def test_without_an_open_pipeline_the_queue_opens_its_own_and_closes_it_when_the_work_is_done(self):
        path = self.tmp / "card.lock"
        m = self.manager(card=MachineCard(path))
        # CHANGED ON PURPOSE (S7): a pipeline says `up` too
        self.assertEqual(m.pipeline_status(), {"id": None, "state": "closed", "why": None,
                                               "since": None, "label": None, "models": [],
                                               "up": [], "class": None, "keep_s": None,
                                               "opened_by": None})
        m.acquire("alpha", W)
        own = m.pipeline_status()
        self.assertEqual((own["state"], own["opened_by"], own["keep_s"], own["models"]),
                         ("holding", "queue", None, []))
        self.assertFalse(_free(path))
        m.unload()
        after = m.pipeline_status()
        self.assertEqual((after["id"], after["state"]), (own["id"], "closed"))
        self.assertIn("nothing is loaded", after["why"])
        self.assertTrue(_free(path), "the queue's own pipeline kept the card with no work")

    def test_the_queues_own_pipeline_keeps_a_model_warm_while_nobody_wants_the_card(self):
        card = RecordingCard()
        m = self.manager(card=card)
        m.cfg.runner["idle_unload_s"] = 600
        m.acquire("alpha", W)
        self.clock.tick(599)
        self.assertFalse(m.settle(), "a warm model nobody else wants was taken off")
        self.assertFalse(m.maybe_idle_unload())
        self.assertEqual((m.status()["loaded"], m.pipeline_status()["state"]), ("alpha", "holding"))
        self.assertEqual(card.gave, [])
        self.clock.tick(2)
        self.assertTrue(m.maybe_idle_unload())
        self.assertEqual(m.pipeline_status()["state"], "closed")
        self.assertEqual(card.gave, ["t-0001-abcd"])

    def test_the_queues_own_pipeline_lets_the_card_go_at_once_when_it_is_wanted(self):
        card = RecordingCard()
        m = self.manager(card=card)
        m.cfg.runner["idle_unload_s"] = 600
        m.acquire("alpha", W)
        card.wanted = True
        card.on_wanted()                                    # as a heartbeat's answer would
        self.wait_for("the queue's pipeline to close", lambda: card.gave)
        self.assertIsNone(m.status()["loaded"], "the card went back under a model")
        st = m.pipeline_status()
        self.assertEqual(st["state"], "closed")
        self.assertIn("the card is wanted", st["why"])
        self.assertEqual(card.gave, ["t-0001-abcd"])

    def test_work_still_owed_keeps_the_queues_pipeline_even_when_the_card_is_wanted(self):
        card = RecordingCard()
        m = self.manager(card=card)
        owed = {"yes": True}
        m.work_pending = lambda: owed["yes"]
        m.acquire("alpha", W)
        card.wanted = True
        self.assertFalse(m.settle(), "the card went back while the queue still owed work")
        owed["yes"] = False
        with m.in_use():
            self.assertFalse(m.settle(), "the card went back under a generation")
            self.assertEqual(card.gave, [])
        # the generation's end is the work's end: the card, wanted, goes at once
        self.assertEqual((card.gave, m.status()["loaded"]), (["t-0001-abcd"], None))
        self.assertIn("the card is wanted", m.pipeline_status()["why"])

    def test_the_queues_own_close_never_takes_a_model_a_generation_took_meanwhile(self):
        """Found by a control of the mutation run: the queue's own pipeline
        found its work done, a job's generation took the warm model in that
        same moment, and the close took the model off under it - the job
        failed. The close looks again, under the manager's lock, before it
        takes anything off."""
        card = RecordingCard()
        m = self.manager(card=card)
        m.acquire("alpha", W)
        card.wanted = True
        asked, answer = threading.Event(), threading.Event()
        self.addCleanup(answer.set)

        def pending():
            asked.set()
            answer.wait(5)
            return False                                    # nothing queued, when it was asked
        m.work_pending = pending
        closing = threading.Thread(target=m.settle, daemon=True)
        closing.start()
        self.assertTrue(asked.wait(5), "the pipeline never asked whether its work was done")
        m.work_pending = None
        with m.in_use():
            backend = m.acquire("alpha", W)                 # a job's generation: the warm model
            answer.set()
            closing.join(5)
            self.assertFalse(closing.is_alive())
            self.assertEqual((backend.stops, m.status()["loaded"]), (0, "alpha"),
                             "the close took the model off under a generation")
            self.assertEqual(card.gave, [], "the card went back under a generation")
        # the generation's end is the work's end: the card, wanted, goes now
        self.assertEqual((card.gave, m.status()["loaded"]), (["t-0001-abcd"], None))

    def test_the_queues_own_close_never_gives_up_a_load_a_job_began_meanwhile(self):
        """The same moment, with a job that switches models: the close its
        pipeline decided must not give up the load the job began."""
        card = RecordingCard()
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        class SlowBeta(FakeBackend):
            given_up = None

            def start(self_):
                if self_.model_key == "beta":
                    started.set()
                    release.wait(5)
                    if self_.given_up:
                        raise BackendError(self_.given_up)
                super().start()

            def abandon(self_, reason):
                self_.given_up = reason
                release.set()
        m = self.manager(card=card, backend_factory=lambda key, port: SlowBeta(key))
        m.acquire("alpha", W)
        card.wanted = True
        asked, answer = threading.Event(), threading.Event()
        self.addCleanup(answer.set)

        def pending():
            asked.set()
            answer.wait(5)
            return False                                    # nothing queued, when it was asked
        m.work_pending = pending
        closing = threading.Thread(target=m.settle, daemon=True)
        closing.start()
        self.assertTrue(asked.wait(5), "the pipeline never asked whether its work was done")
        m.work_pending = None
        box, done = {}, threading.Event()

        def job():
            with m.in_use():
                try:
                    box["got"] = m.acquire("beta", W)
                except Exception as e:                      # noqa: BLE001
                    box["got"] = e
                done.wait(5)
        t = threading.Thread(target=job, daemon=True)
        t.start()
        self.assertTrue(started.wait(5), "the job's model never began to load")
        answer.set()
        closing.join(0.3)                                   # the close, decided, goes on meanwhile
        release.set()
        self.wait_for("the job's model", lambda: "got" in box)
        self.assertNotIsInstance(box["got"], Exception, "the close gave up the job's load")
        self.assertEqual((m.status()["loaded"], card.gave), ("beta", []))
        done.set()
        t.join(5)
        closing.join(5)
        self.assertEqual((card.gave, m.status()["loaded"]), (["t-0001-abcd"], None))

    def test_a_question_about_the_work_that_cannot_be_answered_owes_nothing(self):
        card = RecordingCard()
        m = self.manager(card=card)

        def broken():
            raise RuntimeError("the store could not be read")
        m.work_pending = broken
        m.acquire("alpha", W)
        m.unload()
        self.assertEqual(card.gave, ["t-0001-abcd"])

    def test_the_queues_own_pipeline_asks_with_the_class_of_the_work_that_opens_it(self):
        card = RecordingCard()
        m = self.manager(card=card)
        terms = Terms(klass="scheduled", patience_s=5.0)
        m.acquire("alpha", W, terms=terms)
        self.assertIs(card.terms[0], terms, "the request's terms did not reach the card")
        self.assertEqual(m.pipeline_status()["class"], "scheduled")

    def test_a_pipelines_class_reaches_the_card_from_the_pipelines_own_thread(self):
        """The terms travel by hand: the pipeline takes the card on a thread
        of its own, where no caller's context could reach."""
        card = RecordingCard()
        m = self.manager(card=card)
        m.open_pipeline(W, klass="scheduled", keep_s=0)
        self.holding(m)
        self.assertEqual(card.terms[0].klass, "scheduled")
        self.assertNotEqual(card.threads[0], threading.current_thread().name)

    def test_a_pipeline_nobody_renews_lapses_after_its_keep_s_and_a_lapse_is_a_close(self):
        card = RecordingCard()
        m = self.manager(card=card)
        opened = m.open_pipeline(W, models=["alpha"], keep_s=120)
        self.loaded(m, "alpha")
        self.clock.tick(100)
        self.assertFalse(m.maybe_lapse())
        self.assertEqual(m.renew_pipeline(opened["id"])["state"], "holding")
        self.clock.tick(100)
        self.assertFalse(m.maybe_lapse(), "a renewal was not counted")
        self.assertEqual(card.gave, [])
        self.clock.tick(21)
        self.assertTrue(m.maybe_lapse())
        st = m.pipeline_status()
        self.assertEqual(st["state"], "closed")
        self.assertIn("lapsed: not renewed for 121s", st["why"])
        self.assertIsNone(m.status()["loaded"], "a lapse left its model on the card")
        self.assertEqual(card.gave, ["t-0001-abcd"])
        kinds = self.kinds(m)
        self.assertIn("pipeline_lapsed", kinds, "a lapse has a line of its own")
        self.assertNotIn("pipeline_closed", kinds)
        with self.assertRaises(PipelineRefused) as caught:
            m.renew_pipeline(opened["id"])
        self.assertEqual(caught.exception.reason, "not_open")
        self.assertIn("lapsed", str(caught.exception))

    def test_keep_s_zero_holds_the_pipeline_until_it_is_closed(self):
        card = RecordingCard()
        m = self.manager(card=card)
        m.open_pipeline(W, keep_s=0)
        self.holding(m)
        self.clock.tick(10 * 86400)
        self.assertFalse(m.maybe_lapse())
        self.assertEqual((m.pipeline_status()["state"], card.gave), ("holding", []))

    def test_a_pipeline_that_names_no_keep_s_takes_the_settings(self):
        self.assertEqual(self.manager().open_pipeline(W)["keep_s"], 120.0)
        cfg = self.config(SETTINGS.replace('backend = "mock"',
                                           'backend = "mock"\npipeline_keep_s = 30.0'),
                          name="keep.toml")
        self.assertEqual(self.manager(cfg, folder="two").open_pipeline(W)["keep_s"], 30.0)

    def test_one_pipeline_at_a_time_and_each_refusal_says_why(self):
        # CHANGED ON PURPOSE (S7): two models are a pipeline side by side;
        # more than the runner keeps at once (here, max_loads 1) is refused,
        # and so is a model named twice
        m = self.manager()
        m.cfg.runner["max_loads"] = 1
        for kw, reason in ((dict(models=["nope"]), "unknown_model"),
                           (dict(models=["alpha", "beta"]), "too_many_models"),
                           (dict(models=["alpha", "alpha"]), "repeated_model"),
                           (dict(keep_s=-1), "bad_keep"), (dict(keep_s=float("nan")), "bad_keep")):
            with self.subTest(reason=reason):
                with self.assertRaises(PipelineRefused) as caught:
                    m.open_pipeline(W, **kw)
                self.assertEqual((caught.exception.status, caught.exception.reason),
                                 (400, reason))
        opened = m.open_pipeline(W)
        with self.assertRaises(PipelineRefused) as caught:
            m.open_pipeline(W)
        self.assertEqual((caught.exception.status, caught.exception.reason),
                         (409, "already_open"))
        self.assertIn(opened["id"], str(caught.exception))
        with self.assertRaises(PipelineRefused) as caught:
            m.close_pipeline("pipe-w5r8t2hh")
        self.assertEqual(str(caught.exception), "no pipeline pipe-w5r8t2hh is open here")
        m.close_pipeline(opened["id"])
        with self.assertRaises(PipelineRefused) as caught:
            m.close_pipeline(opened["id"])
        self.assertEqual((caught.exception.reason, str(caught.exception)),
                         ("not_open", f"{opened['id']} is not open: closed by the application"))
        again = m.open_pipeline(W)
        self.assertNotEqual(again["id"], opened["id"], "a pipeline's id was used twice")
        m.close_pipeline(again["id"])
        m.stopping("the server is stopping")
        with self.assertRaises(PipelineRefused) as caught:
            m.open_pipeline(W)
        self.assertEqual((caught.exception.status, caught.exception.reason), (409, "stopping"))

    def test_an_open_takes_over_the_queues_own_pipeline_card_and_all(self):
        path = self.tmp / "card.lock"
        m = self.manager(card=MachineCard(path))
        m.acquire("alpha", W)
        own = m.pipeline_status()
        opened = m.open_pipeline(W, label="plotting session")
        self.assertEqual((opened["state"], opened["opened_by"], opened["label"]),
                         ("holding", "application", "plotting session"))
        self.assertNotEqual(opened["id"], own["id"])
        self.assertFalse(_free(path))
        self.assertEqual(m.status()["loaded"], "alpha", "the take-over took the model off")
        m.unload()                                          # the queue's rule no longer applies
        self.assertFalse(_free(path), "an application's pipeline let go when nothing was loaded")
        self.assertNotIn("card_given_back", self.kinds(m))
        carried = [e for e in self.events(m) if e["event"] in ("pipeline_opened", "pipeline_closed")]
        self.assertEqual([(e["event"], e["pipeline"], e.get("took_over")) for e in carried],
                         [("pipeline_opened", own["id"], None),
                          ("pipeline_closed", own["id"], None),
                          ("pipeline_opened", opened["id"], own["id"])])
        m.close_pipeline(opened["id"])
        self.assertTrue(_free(path))

    def test_a_pipelines_models_go_up_once_it_holds_the_card_and_the_idle_sweep_leaves_them(self):
        m = self.manager(card=RecordingCard())
        m.cfg.runner["idle_unload_s"] = 60
        m.open_pipeline(W, models=["alpha"], keep_s=0)
        self.loaded(m, "alpha")
        self.assertEqual(m.status()["lease"]["holder"], W,
                         "the pipeline's model is not held by the server's actor")
        self.assertLess(self.kinds(m).index("card_granted"), self.kinds(m).index("load_start"),
                        "a model went up before the card was had")
        self.clock.tick(3600)
        self.assertFalse(m.maybe_idle_unload(), "the idle sweep took a model the pipeline keeps")
        self.assertEqual(m.status()["loaded"], "alpha")
        m.acquire("beta", W)                                # on demand: not kept
        self.clock.tick(61)
        self.assertTrue(m.maybe_idle_unload())
        # CHANGED ON PURPOSE (S7): beta went up BESIDE alpha - it once took
        # alpha off, the model the pipeline keeps - and the sweep takes beta
        # alone: alpha is still up
        self.assertEqual((m.status()["loaded"], m.pipeline_status()["state"],
                          m.pipeline_status()["up"]), ("alpha", "holding", ["alpha"]))

    def test_a_pipeline_with_nothing_loaded_still_holds_the_card(self):
        """The Dictaphone's H-2: a host's swap has a moment with nothing on the
        card - zero loads is not a release, and nothing sweeps it away."""
        path = self.tmp / "card.lock"
        m = self.manager(card=MachineCard(path))
        m.open_pipeline(W, keep_s=0)
        self.holding(m)
        m.acquire("alpha", W)
        m.unload()                                          # the swap's empty moment
        self.clock.tick(3600)
        self.assertFalse(m.settle())
        self.assertFalse(m.maybe_idle_unload())
        self.assertFalse(m.maybe_lapse())
        self.assertFalse(_free(path), "a pipeline with nothing loaded let the card go")
        self.assertEqual(m.pipeline_status()["state"], "holding")
        self.assertNotIn("card_given_back", self.kinds(m))

    def test_a_close_under_a_load_gives_the_load_up_and_the_card_back(self):
        path = self.tmp / "card.lock"
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        class Slow(FakeBackend):
            def start(self_):
                started.set()
                release.wait(5)
                if self_.given_up:
                    raise BackendError(self_.given_up)
                super().start()

            given_up = None

            def abandon(self_, reason):
                self_.given_up = reason
                release.set()
        m = self.manager(card=MachineCard(path),
                         backend_factory=lambda key, port: Slow(key))
        opened = m.open_pipeline(W, models=["alpha"], keep_s=0)
        self.assertTrue(started.wait(5), "the pipeline's model never began to load")
        t0 = time.monotonic()
        closed = m.close_pipeline(opened["id"])
        self.assertLess(time.monotonic() - t0, 3.0, "the close waited out the load")
        self.assertEqual(closed["state"], "closed")
        self.assertIsNone(m.status()["loaded"])
        self.assertTrue(_free(path))

    def test_an_open_during_the_queues_own_close_takes_the_card_after_that_close(self):
        """Probed while building: an open while the queue's own pipeline is
        closing must not be answered by the card that close is about to give
        back. The open's first record line waits for the manager's lock, which
        the close holds from its unload to its give-back."""
        path = self.tmp / "card.lock"
        stopping, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        class SlowOff(FakeBackend):
            def stop(self_):
                stopping.set()
                release.wait(5)
                super().stop()
        card = MachineCard(path)
        m = self.manager(card=card, backend_factory=lambda key, port: SlowOff(key))
        m.acquire("alpha", W)
        card.wanted = True                                  # its work is done, and it is wanted
        closing = threading.Thread(target=m.settle, daemon=True)
        closing.start()
        self.assertTrue(stopping.wait(5), "the queue's own pipeline never began to close")
        opened = m.open_pipeline(W, keep_s=0)
        release.set()
        closing.join(5)
        self.holding(m)
        self.assertEqual(m.pipeline_status()["id"], opened["id"])
        self.assertFalse(_free(path), "the new pipeline holds a card its predecessor gave back")

    def test_the_card_goes_back_only_once_every_model_of_it_has_come_off(self):
        path = self.tmp / "card.lock"
        at_stop = []

        class Watched(FakeBackend):
            def stop(self_):
                at_stop.append(_free(path))
                super().stop()
        m = self.manager(card=MachineCard(path), backend_factory=lambda key, port: Watched(key))
        opened = m.open_pipeline(W, models=["alpha"], keep_s=0)
        self.loaded(m, "alpha")
        m.close_pipeline(opened["id"])
        self.assertEqual(at_stop, [False], "the card was free while its model came off")
        self.assertTrue(_free(path))

    def test_a_close_while_the_pipeline_waits_for_the_card_ends_its_wait_at_once(self):
        class Busy:
            turn = lost = None

            def take(self_, holder, model_key, for_=None, cancel=None, on_wait=None,
                     terms=None):
                deadline = time.monotonic() + 20
                while not (cancel is not None and cancel.is_set()):
                    if time.monotonic() > deadline:
                        raise RuntimeError("nobody ended this wait")
                    time.sleep(0.01)
                raise RuntimeError("cancelled while waiting for the card")

            def give_back(self_):
                pass

            def status(self_):
                return None
        m = self.manager(card=Busy())
        opened = m.open_pipeline(W, models=["alpha"], keep_s=0)
        self.wait_for("the pipeline to wait for the card", lambda: m.loading)
        t0 = time.monotonic()
        closed = m.close_pipeline(opened["id"])
        self.assertLess(time.monotonic() - t0, 3.0, "the close waited out the wait for the card")
        self.assertEqual((closed["state"], closed["why"]), ("closed", "closed by the application"))
        self.assertFalse(m.loading)
        self.assertNotIn("load_start", self.kinds(m))

    def test_a_close_gives_up_a_load_its_pipeline_did_not_start(self):
        """A job's load inside an application's pipeline watches the job's
        cancel, not the pipeline's: the close gives it up all the same."""
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        class Slow(FakeBackend):
            given_up = None

            def start(self_):
                started.set()
                release.wait(5)
                if self_.given_up:
                    raise BackendError(self_.given_up)
                super().start()

            def abandon(self_, reason):
                self_.given_up = reason
                release.set()
        m = self.manager(card=RecordingCard(), backend_factory=lambda key, port: Slow(key))
        opened = m.open_pipeline(W, keep_s=0)
        self.holding(m)
        box = {}

        def job():
            try:
                box["got"] = m.acquire("alpha", W)
            except Exception as e:                          # noqa: BLE001
                box["got"] = e
        t = threading.Thread(target=job, daemon=True)
        t.start()
        self.assertTrue(started.wait(5), "the job's model never began to load")
        t0 = time.monotonic()
        m.close_pipeline(opened["id"])
        self.assertLess(time.monotonic() - t0, 3.0, "the close waited out a job's load")
        t.join(5)
        self.assertIsInstance(box.get("got"), BackendError)
        self.assertIsNone(m.status()["loaded"])

    def test_the_queues_own_pipeline_lost_is_over_and_its_next_work_opens_another(self):
        card = RecordingCard()
        m = self.manager(card=card)
        m.acquire("alpha", W)
        first = m.pipeline_status()["id"]
        card.lost = "reclaimed"
        card.on_lost("reclaimed", "no heartbeat for 16s")   # as a heartbeat's answer would
        self.wait_for("the model to come off", lambda: m.status()["loaded"] is None)
        self.assertEqual(m.pipeline_status()["state"], "lost")
        m.acquire("alpha", W)
        again = m.pipeline_status()
        self.assertNotEqual(again["id"], first, "a lost pipeline was held again")
        self.assertEqual((again["state"], again["opened_by"]), ("holding", "queue"))
        self.assertEqual(card.gave, [], "a card taken back was given back")

    def test_hearing_the_card_is_wanted_never_waits_on_the_manager(self):
        """It is heard on a heartbeat's thread, which must never wait for a
        model to come off."""
        card = RecordingCard()
        release = threading.Event()
        self.addCleanup(release.set)

        class SlowOff(FakeBackend):
            def stop(self_):
                release.wait(5)
                super().stop()
        m = self.manager(card=card, backend_factory=lambda key, port: SlowOff(key))
        m.acquire("alpha", W)
        card.wanted = True
        t0 = time.monotonic()
        card.on_wanted()
        self.assertLess(time.monotonic() - t0, 0.5, "hearing it waited for the model to come off")
        release.set()
        self.wait_for("the queue's pipeline to close", lambda: card.gave)
        self.assertIsNone(m.status()["loaded"])

    def test_a_stuck_model_that_comes_off_when_asked_again_keeps_the_card_no_longer(self):
        """Where S3 and S6 meet, found merging them: a model whose stop threw
        keeps the card (S6) and stays named, its stop asked again before any
        load (S3). Once that stop works, nothing that would not stop is on the
        card - so a load that fails next must not leave the pipeline refusing
        every close (`unload_failed`) until the server stops."""
        card = RecordingCard()
        how = {"stop_fails": None, "fail": None}

        def make(key, port):
            b = FakeBackend(key, stop_fails=how["stop_fails"], fail=how["fail"])
            self.made.append(b)
            return b

        m = self.manager(card=card, backend_factory=make)
        opened = m.open_pipeline(W, keep_s=0)
        self.holding(m)
        how["stop_fails"] = "the process would not die"
        m.acquire("alpha", W)
        with self.assertRaises(RuntimeError):
            m.unload()                                      # alpha stuck: the card is kept
        self.made[0].stop_fails = None                      # it would go now
        how.update(stop_fails=None, fail="beta will not load")
        with self.assertRaises(BackendError):
            m.acquire("beta", W)                            # alpha asked again and gone; beta fails
        again = [e["event"] for e in self.events(m) if e.get("again")]
        self.assertEqual(again, ["unload_start", "unload_ok"], "alpha's stop was not asked again")
        self.assertIsNone(m.status()["loaded"])
        closed = m.close_pipeline(opened["id"])
        self.assertEqual(closed["state"], "closed")
        self.assertEqual(card.gave, ["t-0001-abcd"], "the card was not given back at the close")


class NativePipelineRegisterRows(Case):
    """Wave 1's registers, closed with the pipeline."""

    def test_a_patience_of_zero_on_the_machines_lock_is_now_or_not_at_all(self):
        """S2 R1: the settings' `card_patience_s = 0` was read as "none given"
        and became ten minutes on the machine's lock."""
        lock = (self.tmp / "shared.lock").as_posix()
        first = self.manager(self.config(LLAMA_SETTINGS.replace(
            '[runner]\n', f'[runner]\ncard_lock = "{lock}"\n', 1), name="first.toml"),
            folder="first", vram=lambda: None)
        first.acquire("alpha", W)
        second = self.manager(self.config(LLAMA_SETTINGS.replace(
            '[runner]\n', f'[runner]\ncard_lock = "{lock}"\ncard_patience_s = 0\n', 1),
            name="second.toml"), folder="second", vram=lambda: None)
        self.assertEqual(second._card.patience_s, 0.0)
        box = {}

        def go():
            try:
                second.acquire("alpha", X)
            except BackendError as e:
                box["e"] = e
        t = threading.Thread(target=go, daemon=True)
        t0 = time.monotonic()
        t.start()
        t.join(10)
        self.assertFalse(t.is_alive(), "a patience of 0 waited")
        self.assertLess(time.monotonic() - t0, 2.0, "a patience of 0 waited")
        self.assertIsInstance(box.get("e"), BackendError)

    def test_each_wait_for_the_card_counts_until_its_own_has_ended(self):
        """S1 R4: two loads waiting for the card were one slot - the first to
        stop waiting cleared it, and the other's wait read as nothing loading."""
        gates = {"alpha": threading.Event(), "beta": threading.Event()}
        arrived = []

        class Held:
            turn = lost = None

            def take(self_, holder, model_key, for_=None, cancel=None, on_wait=None):
                arrived.append(model_key)
                while not gates[model_key].wait(0.01):
                    if cancel is not None and cancel.is_set():
                        raise RuntimeError("cancelled")
                return None

            def give_back(self_):
                pass

            def status(self_):
                return None
        m = self.manager(card=Held())
        cancel_alpha = threading.Event()
        out = {}
        a = threading.Thread(daemon=True, target=lambda: out.update(
            alpha=self.suppress(m.acquire, "alpha", W, cancel=cancel_alpha)))
        b = threading.Thread(daemon=True, target=lambda: out.update(
            beta=self.suppress(m.acquire, "beta", W)))
        a.start()
        b.start()
        deadline = time.monotonic() + 5
        while len(arrived) < 2:
            self.assertLess(time.monotonic(), deadline, "the two never both waited")
            time.sleep(0.01)
        self.assertTrue(m.loading)
        cancel_alpha.set()
        a.join(5)
        self.assertIsInstance(out.get("alpha"), Cancelled)
        self.assertTrue(m.loading, "one wait ended, and the other read as nothing loading")
        self.assertEqual(m._loads.card_waiting, "beta")
        gates["beta"].set()
        b.join(5)
        self.assertFalse(m.loading)
        self.assertEqual(m.status()["loaded"], "beta")

    @staticmethod
    def suppress(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:                              # noqa: BLE001
            return e

    def test_a_refusal_carries_the_card_sources_own_reason_to_the_caller(self):
        class Busy:
            turn = lost = None
            kind = "recording card"

            def take(self_, holder, model_key, for_=None, cancel=None, on_wait=None,
                     terms=None):
                err = RuntimeError("the card is busy, and this request would not wait")
                err.reason = "card_busy"
                raise err

            def give_back(self_):
                pass

            def status(self_):
                return None
        m = self.manager(card=Busy())
        terms = Terms(klass="responsive", patience_s=0)
        with self.assertRaises(BackendError) as caught:
            m.acquire("alpha", W, terms=terms)
        self.assertEqual((terms.refused, caught.exception.reason), ("card_busy", "card_busy"))
        self.assertIn("would not wait", str(caught.exception))

    def test_the_managers_words_are_the_rentals_never_a_turns(self):
        """S2 R3: the turns were replaced by tickets; the manager still said
        "turn" in its words and in its record's field."""
        self.assertIsNone(re.search(r"\bturns?\b", MANAGER_SRC.replace('"turn"', "")
                                    .replace("self._card.turn", "").replace(".turn", "")),
                          "the manager still speaks of turns")
        card = RecordingCard()
        m = self.manager(card=card)
        m.acquire("alpha", W)
        granted = next(e for e in self.events(m) if e["event"] == "card_granted")
        self.assertEqual(granted.get("ticket"), "t-0001-abcd")
        self.assertNotIn("turn", granted)


# ================================================== model health (C7) ===

# ===================================== the memory: the card source decides ===

class Deciding(RecordingCard):
    """A card source with the memory check: it writes down every question it
    is asked - the model, the engine, the need, how the card is read, the
    margin - and answers what the row set (`verdict`)."""

    def __init__(self, verdict=None):
        super().__init__()
        self.asked, self.verdict = [], verdict

    def fit(self, model, engine, need_mib, *, read=None, margin_mib=None, how="", hint=""):
        self.asked.append({"model": model, "engine": engine, "need_mib": need_mib, "read": read,
                           "margin_mib": margin_mib, "how": how, "hint": hint})
        return None if self.verdict is None else dict(self.verdict)


def _verdict(verdict, need=5000, free=20000, margin=256, total=24576, words="said"):
    return {"verdict": verdict, "need_mib": need, "free_mib": free, "margin_mib": margin,
            "total_mib": total, "words": words}


class NativeMemoryRows(Case):
    """PLAN-4 D2: the runner's own check before a load goes - the CARD SOURCE
    decides, by the contract's one memory check, with the runner's estimate
    of what each engine needs."""

    def test_whisper_needs_its_file_a_quarter_more_and_600_mib(self):
        from runner import needs
        self.assertEqual(needs.whisper_need_mib(3 * 1024 ** 3), 3072 + 768 + 600)
        self.assertEqual(needs.whisper_need_mib(1), 601, "a part of a MiB is a whole one")
        self.assertEqual(needs.whisper_need_mib(0), 600)

    def test_llama_cpp_needs_its_weights_its_cache_and_a_slack(self):
        from runner import needs
        self.assertEqual(needs.llama_need_mib(2 * 1024 ** 3, 1024 ** 3), 2048 + 1024 + 1536)
        self.assertEqual(needs.llama_need_mib(1, 0), 1 + 1536, "a part of a MiB is a whole one")

    def test_a_models_need_is_the_estimate_of_its_engine(self):
        from runner import needs
        models = self.tmp / "models"
        models.mkdir()
        (models / "plain.bin").write_bytes(b"\0" * (3 * 1024 * 1024))
        cfg = self.config('[runner]\nbackend = "llamacpp"\nmodels_dir = "models"\n\n'
                          '[models.alpha]\nfile = "plain.bin"\n\n[models.gone]\nfile = "no.bin"\n')
        need, how, hint = needs.need_mib(cfg, "alpha", "llamacpp")
        self.assertEqual(need, 3 + 1536, how)          # a file with no header: no cache read
        self.assertIn("slack", how)
        self.assertIn("ctx", hint)
        need, how, _ = needs.need_mib(cfg, "alpha", "whisper")
        self.assertEqual(need, 3 + 1 + 600, how)
        self.assertIsNone(needs.need_mib(cfg, "gone", "llamacpp")[0],
                          "a model whose file is not there has no estimate: it fails at its start")
        self.assertIsNone(needs.need_mib(cfg, "alpha", "mock")[0],
                          "the stand-in puts nothing on the card, whatever file it names")

    def test_the_card_source_decides_before_each_load(self):
        card = Deciding(_verdict("fits"))
        m = self.manager(card=card)
        m.acquire("alpha", W)
        m.acquire("beta", W)
        self.assertEqual([(a["model"], a["engine"], a["need_mib"], a["margin_mib"])
                          for a in card.asked], [("alpha", "mock", None, 256), ("beta", "mock", None, 256)])
        self.assertIs(card.asked[0]["read"], m._vram, "the card is read the way this runner reads it")
        checks = [e for e in self.events(m) if e["event"] == "vram_check"]
        self.assertEqual([(e["model_key"], e["verdict"], e["source"], e["need_mib"], e["free_mib"],
                           e["margin_mib"], e["card_mib"]) for e in checks],
                         [(k, "fits", "recording card", 5000, 20000, 256, 24576)
                          for k in ("alpha", "beta")])
        ev = self.kinds(m)
        self.assertLess(ev.index("vram_check"), ev.index("load_start"))

    def test_a_refusal_refuses_the_load_before_it_is_paid_for_in_the_sources_words(self):
        for verdict in ("no_room", "never_fits"):
            with self.subTest(verdict=verdict):
                card = Deciding(_verdict(verdict, words=f"alpha {verdict}: in numbers"))
                m = self.manager(card=card, folder=verdict)
                with self.assertRaises(BackendError) as caught:
                    m.acquire("alpha", W)
                self.assertEqual(str(caught.exception), f"alpha {verdict}: in numbers")
                self.assertEqual([b.starts for b in self.made], [0] * len(self.made),
                                 "a refused load was started")
                self.assertIsNone(m.status()["lease"]["holder"])
                self.assertIn(verdict, [e.get("verdict") for e in self.events(m)
                                        if e["event"] == "vram_check"])

    def test_a_card_that_could_not_be_read_never_gates_and_says_so(self):
        card = Deciding(_verdict("unknown", free=None, total=None))
        m = self.manager(card=card)
        self.assertTrue(m.acquire("alpha", W).is_alive())
        unknown = [e for e in self.events(m) if e["event"] == "vram_unknown"]
        self.assertEqual([(e["model_key"], e["source"]) for e in unknown],
                         [("alpha", "recording card")])

    def test_a_card_source_with_no_memory_check_decides_nothing(self):
        m = self.manager(card=RecordingCard())
        self.assertTrue(m.acquire("alpha", W).is_alive())
        self.assertFalse({"vram_check", "vram_unknown"} & set(self.kinds(m)))
        m = self.manager(card=Deciding(None), folder="nothing")
        self.assertTrue(m.acquire("alpha", W).is_alive())
        self.assertFalse({"vram_check", "vram_unknown"} & set(self.kinds(m)))

    def test_the_margin_is_a_setting_the_contracts_by_default(self):
        import memory as MEM
        self.assertEqual(Config.default().runner["card_margin_mib"], MEM.MARGIN_MIB)
        card = Deciding(_verdict("fits"))
        m = self.manager(self.config(SETTINGS.replace('backend = "mock"\n',
                                                      'backend = "mock"\ncard_margin_mib = 1000\n')),
                         card=card)
        m.acquire("alpha", W)
        self.assertEqual(card.asked[0]["margin_mib"], 1000)
        for bad in ("-1", "1.5", "true"):
            with self.subTest(margin=bad):
                with self.assertRaises(ValueError):
                    self.config(SETTINGS.replace('backend = "mock"\n',
                                                 f'backend = "mock"\ncard_margin_mib = {bad}\n'))


def _hold(path, note=None):
    """Hold *path*'s lock with a handle of its own - as another process would
    - and write the note beside it: -> the handle, to let go with `_CL._unlock`."""
    fh = _CL._try_lock(Path(path))
    assert fh is not None, f"{path} was held already"
    if note is not None:
        Path(str(path) + _CL.NOTE_SUFFIX).write_bytes(json.dumps(note).encode("utf-8"))
    return fh


# CHANGED ON PURPOSE (H6, H5's register 6): a note names the process that
# holds the lock - here this suite's own, which holds it (`_hold`) - and was
# written after it started. A note whose pid is not such a live process is
# stale: it names nobody
APP_NOTE = {"pid": os.getpid(), "since": time.time(), "holder": "srv-k3x9m2p7",
            "model": "demo-7b", "for": None}
COORDINATOR_NOTE = {"pid": os.getpid(), "since": time.time(),
                    "coordinator": "http://127.0.0.1:7071", "actor": "crd-m4n8p2q6"}


def _dead_note(**over) -> dict:
    """The note a holder that died left behind: a coordinator's, naming a
    process of this suite's own that has ended - as a dead test coordinator's
    was found beside the machine's own lock."""
    child = subprocess.Popen([sys.executable, "-c", "pass"],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    since = time.time()
    killed(child)
    return {"pid": child.pid, "since": since, "coordinator": "http://127.0.0.1:30087",
            "actor": "crd-a2b3c4d5", **over}


class Through(RecordingCard):
    """A coordinator's card source, as the lock's note leads to it: made by
    the lock for one address and one holder, and written down."""
    kind = "coordinator rental"
    made: list = []

    def __init__(self, url, holder):
        super().__init__()
        self.url, self.holder, self.closed = url, holder, False
        self.named_ = []
        self.decided = []
        Through.made.append(self)

    def running(self, pid, proof, model, engine=None, port=None, mib=None):
        self.named_.append(pid)

    def stopped(self, proof):
        self.named_.append(f"-{proof}")

    def named(self):
        return list(self.named_)

    def fit(self, model, engine, need_mib, **kw):
        self.decided.append(model)
        return _verdict("fits")

    def status(self):
        return {"coordinator": "reachable", "holding": self.turn is not None,
                "waiting_since": None}

    def close(self):
        self.closed = True


class NativeLockRows(Case):
    """PLAN-4 D5: the machine's lock says who holds the card at the top. A
    server looks, each time it opens a session: free - it takes the lock;
    held by a coordinator - it goes through that coordinator, whose address
    the note gives; held otherwise - an application run on its own has the
    card, the request's patience applies, and 0 is card_busy at once. What it
    says while it waits, and when it gives up, names nobody (D4)."""

    def setUp(self):
        super().setUp()
        Through.made = []

    def lock(self, name="card.lock"):
        return self.tmp / name

    def bounded_take(self, card, **kw):
        """`card.take`, on a thread of its own, within five seconds: -> what it
        answered, or raised. A take that would wait out a longer patience
        than it was given fails the row instead of holding the suite."""
        box = {}

        def go():
            try:
                box["got"] = card.take(W, "alpha", **kw)
            except Exception as e:                          # noqa: BLE001
                box["got"] = e
        t = threading.Thread(target=go, daemon=True)
        t.start()
        t.join(5)
        if t.is_alive():
            card.stop_asking()
            t.join(2)
            self.fail("the take waited past the patience it was given")
        return box["got"]

    def test_the_lock_and_its_note_are_the_contracts(self):
        doc = json.loads((HERE.parent / "llm-bootstrap-contract" / "machine" / "card-lock.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual(_CL.LOCK_NAME, doc["file"])
        self.assertEqual(_CL.LOCK_NAME + _CL.NOTE_SUFFIX, doc["note_file"])
        card = MachineCard(self.lock())
        card.take(W, "alpha", for_="c-20260924-090000-k3x9m2p7")
        self.addCleanup(card.give_back)
        note = json.loads(card.holder_path.read_text(encoding="utf-8"))
        self.assertEqual(V.check(note, doc["note"], V.load(HERE.parent / "llm-bootstrap-contract")[0]),
                         [], note)
        self.assertEqual((note["holder"], note["model"], note["pid"]), (W, "alpha", os.getpid()))

    def test_a_patience_of_0_is_card_busy_at_once_naming_no_one(self):
        """S6 R2: on the machine's lock a patience of 0 refused at once, but
        with no reason of its own - and its words named the other server's
        pid and actor."""
        fh = _hold(self.lock(), APP_NOTE)
        self.addCleanup(_CL._unlock, fh)
        card = MachineCard(self.lock(), patience_s=600)
        t0 = time.monotonic()
        got = self.bounded_take(card, terms=Terms(patience_s=0))
        self.assertLess(time.monotonic() - t0, 1.0, "a patience of 0 waited")
        self.assertIsInstance(got, _CL.CardWaitRefused)
        self.assertEqual(got.reason, "card_busy")
        for other in (str(APP_NOTE["pid"]), "srv-k3x9m2p7", "demo-7b"):
            self.assertNotIn(other, str(got))

    def test_the_requests_patience_applies_and_runs_out_card_patience_naming_no_one(self):
        fh = _hold(self.lock(), APP_NOTE)
        self.addCleanup(_CL._unlock, fh)
        card = MachineCard(self.lock(), patience_s=600, retry_s=0.05)
        heard = []
        t0 = time.monotonic()
        got = self.bounded_take(card, on_wait=heard.append, terms=Terms(patience_s=0.3))
        took = time.monotonic() - t0
        self.assertTrue(0.3 <= took < 3.0, f"{took:.2f}s")
        self.assertIsInstance(got, _CL.CardWaitRefused)
        self.assertEqual(got.reason, "card_patience")
        self.assertTrue(heard and all(h.startswith("waiting for the card, since ") for h in heard),
                        heard)
        for other in (str(APP_NOTE["pid"]), "srv-k3x9m2p7", "demo-7b"):
            self.assertNotIn(other, str(got) + " ".join(heard))

    def test_a_lock_held_by_a_coordinator_is_gone_through(self):
        fh = _hold(self.lock(), COORDINATOR_NOTE)
        # a patience of its own: a take that never goes through fails the row,
        # and never waits the server's ten minutes
        card = MachineCard(self.lock(), through=Through, patience_s=2)
        wired = []
        card.on_lost, card.on_wanted = (lambda *a: wired.append("lost")), (lambda: None)
        rental = card.take(W, "alpha")
        self.assertEqual(len(Through.made), 1, "no way through was made")
        via = Through.made[0]
        self.assertEqual((via.url, via.holder, via.takes), ("http://127.0.0.1:7071", W, ["alpha"]))
        self.assertEqual(rental, via.turn)
        self.assertIs(via.on_lost, card.on_lost, "the manager's ear was not handed on")
        self.assertEqual((card.kind, card.turn, card.status()["coordinator"]),
                         ("coordinator rental", via.turn, "reachable"))
        via.lost = "reclaimed"
        self.assertEqual(card.lost, "reclaimed", "a loss the coordinator's card source heard is "
                                                 "not the runner's to read")
        via.lost = None
        card.running(41, "load-a2b3c4d5", "alpha", "llamacpp", 31001)
        card.stopped("load-a2b3c4d5")
        self.assertEqual((via.named_, card.named()), ([41, "-load-a2b3c4d5"], via.named_))
        self.assertEqual(card.fit("alpha", "llamacpp", 5000)["verdict"], "fits")
        self.assertEqual(via.decided, ["alpha"])
        self.assertEqual((card.take(W, "alpha"), via.takes), (via.turn, ["alpha"]),
                         "a rental held was asked for again")
        card.give_back()
        self.assertEqual((via.gave, via.closed), (["t-0001-abcd"], True))
        _CL._unlock(fh)
        Path(str(self.lock()) + _CL.NOTE_SUFFIX).unlink()
        self.assertIsNone(card.take(W, "beta"), "a free lock was not taken")
        self.addCleanup(card.give_back)
        self.assertEqual((card.kind, card.turn, card.held), ("machine lock", None, True))
        self.assertEqual(len(Through.made), 1, "a free lock went through a coordinator")

    def test_a_session_whose_rental_was_taken_back_is_let_go_and_the_lock_looked_at_again(self):
        """A rental the coordinator took back ends its session with no give-
        back: the next take lets that card source go - closed - and looks at
        the lock afresh, never carrying the old loss into a new session."""
        fh = _hold(self.lock(), COORDINATOR_NOTE)
        # a patience of its own: a take that never goes through fails the row,
        # and never waits the server's ten minutes
        card = MachineCard(self.lock(), through=Through, patience_s=2)
        card.take(W, "alpha")
        via = Through.made[0]
        via.lost = "reclaimed"                      # taken back, as an answer said
        _CL._unlock(fh)                             # and the coordinator has gone
        Path(str(self.lock()) + _CL.NOTE_SUFFIX).unlink()
        self.assertIsNone(card.take(W, "alpha"), "the free lock was not taken")
        self.addCleanup(card.give_back)
        self.assertEqual((card.lost, card.kind, via.closed), (None, "machine lock", True))

    @staticmethod
    def attempt(card, key):
        try:
            return card.take(W, key)
        except Exception as e:                              # noqa: BLE001
            return e

    def wait_until(self, pred, what):
        deadline = time.monotonic() + 5
        while not pred():
            self.assertLess(time.monotonic(), deadline, f"never: {what}")
            time.sleep(0.01)

    @staticmethod
    def one_in_line(gate):
        """A coordinator's card source whose take waits for *gate*, one take at
        a time, as a coordinator's own does (its one place in line): a take
        behind one under way is handed that one's rental."""
        class InLine(Through):
            def __init__(self_, url, holder):
                super().__init__(url, holder)
                self_.one = threading.Lock()

            def take(self_, holder, model_key, for_=None, cancel=None, on_wait=None,
                     terms=None):
                with self_.one:
                    if self_.turn is not None:
                        return dict(self_.turn)
                    self_.takes.append(model_key)
                    gate.wait(5)
                    if self_.closed:
                        raise RuntimeError("its card source was closed under its take")
                    self_.turn = {"id": "t-0001-abcd"}
                    return dict(self_.turn)
        return InLine

    def test_two_takes_at_once_through_a_coordinator_share_its_card_source(self):
        """The manager may take the card twice at once - a pipeline's thread
        and a load (S1 R4). Through the coordinator the lock names, the second
        waits for the first's answer: one card source, one ticket - and the
        first's is never closed under it (found building S4: it was, and the
        second asked a ticket of its own)."""
        fh = _hold(self.lock(), COORDINATOR_NOTE)
        self.addCleanup(_CL._unlock, fh)
        gate = threading.Event()
        card = MachineCard(self.lock(), through=self.one_in_line(gate), patience_s=2)
        self.addCleanup(card.give_back)
        got = {}
        first = threading.Thread(daemon=True,
                                 target=lambda: got.update(a=self.attempt(card, "alpha")))
        first.start()
        self.wait_until(lambda: Through.made and Through.made[0].takes, "the first take")
        second = threading.Thread(daemon=True,
                                  target=lambda: got.update(b=self.attempt(card, "beta")))
        second.start()
        time.sleep(0.2)                             # the second arrives while the first waits
        gate.set()
        first.join(5)
        second.join(5)
        self.assertEqual(len(Through.made), 1, "a second card source: a second ticket")
        self.assertEqual((got.get("a"), got.get("b")), ({"id": "t-0001-abcd"},) * 2, str(got))
        self.assertFalse(Through.made[0].closed, "the first take's card source was closed")

    def test_two_takes_that_find_the_coordinator_at_once_make_one_card_source(self):
        fh = _hold(self.lock(), COORDINATOR_NOTE)
        self.addCleanup(_CL._unlock, fh)
        gate, release, arrived = threading.Event(), threading.Event(), []
        card = MachineCard(self.lock(), through=self.one_in_line(gate), patience_s=2)
        self.addCleanup(card.give_back)
        real = card._coordinator

        def slow():                                 # the first holds between its looks
            arrived.append(1)
            if len(arrived) == 1:
                release.wait(5)
            return real()
        card._coordinator = slow
        got = {}
        first = threading.Thread(daemon=True,
                                 target=lambda: got.update(a=self.attempt(card, "alpha")))
        first.start()
        self.wait_until(lambda: arrived, "the first take's look at the lock")
        second = threading.Thread(daemon=True,
                                  target=lambda: got.update(b=self.attempt(card, "beta")))
        second.start()
        self.wait_until(lambda: Through.made, "the second's card source")
        release.set()
        time.sleep(0.2)
        gate.set()
        first.join(5)
        second.join(5)
        self.assertEqual(len(Through.made), 1, "two card sources: two tickets for one server")
        self.assertEqual((got.get("a"), got.get("b")), ({"id": "t-0001-abcd"},) * 2, str(got))

    def test_a_take_refused_through_a_coordinator_leaves_the_next_to_look_at_the_lock(self):
        fh = _hold(self.lock(), COORDINATOR_NOTE)

        class Busy(Through):
            def take(self_, holder, model_key, for_=None, cancel=None, on_wait=None,
                     terms=None):
                self_.takes.append(model_key)
                raise _CL.CardWaitRefused("the card is busy, and this request would not wait "
                                          "for it (card_busy)", "card_busy")
        card = MachineCard(self.lock(), through=Busy, patience_s=2)
        self.assertEqual(getattr(self.attempt(card, "alpha"), "reason", None), "card_busy")
        _CL._unlock(fh)
        Path(str(self.lock()) + _CL.NOTE_SUFFIX).unlink()
        self.assertIsNone(self.attempt(card, "alpha"), "the free lock was not taken")
        self.addCleanup(card.give_back)
        self.assertEqual((card.kind, Through.made[0].closed, len(Through.made)),
                         ("machine lock", True, 1))

    def test_without_a_way_through_a_coordinators_lock_is_a_card_in_use(self):
        fh = _hold(self.lock(), COORDINATOR_NOTE)
        self.addCleanup(_CL._unlock, fh)
        got = self.bounded_take(MachineCard(self.lock()), terms=Terms(patience_s=0))
        self.assertIsInstance(got, _CL.CardWaitRefused)
        self.assertEqual(got.reason, "card_busy")

    def test_a_lock_just_taken_its_note_not_yet_written_is_looked_at_again(self):
        """S4 R5: a holder writes its note just after it takes the lock. In
        that instant a request that would not wait found the lock held and no
        note, took the card for an application's, and was refused card_busy -
        though a coordinator had just taken it, and the request would have
        gone through it. The note is read here, and written the moment after
        the first look - as a coordinator that has just started writes it."""
        fh = _hold(self.lock())
        self.addCleanup(_CL._unlock, fh)
        card = MachineCard(self.lock(), through=Through, patience_s=600)
        looks = []
        real = card._coordinator

        def looking():
            said = real()
            looks.append(said)
            if len(looks) == 1:                     # its first look: the note comes now
                Path(str(self.lock()) + _CL.NOTE_SUFFIX).write_bytes(
                    json.dumps(COORDINATOR_NOTE).encode("utf-8"))
            return said
        card._coordinator = looking
        got = self.bounded_take(card, terms=Terms(patience_s=0))
        self.assertNotIsInstance(got, _CL.CardWaitRefused, f"refused: {got}")
        self.assertEqual([via.url for via in Through.made], [COORDINATOR_NOTE["coordinator"]],
                         "the coordinator the note names was not gone through")
        self.assertEqual(looks, [None, COORDINATOR_NOTE["coordinator"]],
                         "not looked at again, once, when the note had come")

    def test_a_lock_held_with_no_note_at_all_is_card_busy_once_its_holder_had_its_moment(self):
        """...and a holder that never writes a note is given that moment, and
        no more: then card_busy, naming no one."""
        fh = _hold(self.lock())
        self.addCleanup(_CL._unlock, fh)
        real, _CL.NOTE_WAIT_S = getattr(_CL, "NOTE_WAIT_S", None), 0.3
        self.addCleanup(setattr, _CL, "NOTE_WAIT_S", real)
        t0 = time.monotonic()
        got = self.bounded_take(MachineCard(self.lock(), patience_s=600),
                                terms=Terms(patience_s=0))
        took = time.monotonic() - t0
        self.assertIsInstance(got, _CL.CardWaitRefused)
        self.assertEqual(got.reason, "card_busy")
        self.assertTrue(0.3 <= took < 1.3, f"{took:.2f}s: the moment was not given, or not "
                                           f"bounded")

    def test_a_stop_or_a_cancel_ends_the_moment_a_holder_is_given(self):
        """Rule 11: a stop never waits out work nobody will use - nor does a
        request's cancel - the moment given a holder to write its note
        included."""
        fh = _hold(self.lock())
        self.addCleanup(_CL._unlock, fh)
        real, _CL.NOTE_WAIT_S = getattr(_CL, "NOTE_WAIT_S", None), 3.0
        self.addCleanup(setattr, _CL, "NOTE_WAIT_S", real)
        for how in ("stop", "cancel"):
            with self.subTest(ended_by=how):
                card = MachineCard(self.lock(), patience_s=600)
                cancel = threading.Event()
                box = {}

                def take(card=card, cancel=cancel, box=box):
                    try:
                        box["got"] = card.take(W, "alpha", cancel=cancel,
                                               terms=Terms(patience_s=0))
                    except Exception as e:                      # noqa: BLE001
                        box["got"] = e
                t = threading.Thread(target=take, daemon=True)
                t0 = time.monotonic()
                t.start()
                time.sleep(0.3)                             # inside the moment
                card.stop_asking() if how == "stop" else cancel.set()
                t.join(5)
                self.assertFalse(t.is_alive(), f"a {how} did not end the moment")
                self.assertLess(time.monotonic() - t0, 2.0, f"a {how} waited the moment out")
                self.assertIsInstance(box.get("got"), _CL.CardWaitRefused)

    def test_a_lock_whose_note_says_who_holds_it_is_card_busy_at_once_no_moment_given(self):
        """The moment is for a lock with no note yet: one whose holder has said
        who it is - an application run on its own - is card_busy at once."""
        fh = _hold(self.lock(), APP_NOTE)
        self.addCleanup(_CL._unlock, fh)
        real, _CL.NOTE_WAIT_S = getattr(_CL, "NOTE_WAIT_S", None), 3.0
        self.addCleanup(setattr, _CL, "NOTE_WAIT_S", real)
        t0 = time.monotonic()
        got = self.bounded_take(MachineCard(self.lock(), patience_s=600),
                                terms=Terms(patience_s=0))
        self.assertEqual(getattr(got, "reason", None), "card_busy", str(got))
        self.assertLess(time.monotonic() - t0, 1.0, "a note that says who holds the lock was "
                                                    "given a moment to appear")

    def test_a_stale_note_names_nobody_never_a_coordinator_to_go_through(self):
        """H5's register 6: a holder that dies lets go of the lock and leaves
        its note - a dead test coordinator's was found beside the machine's
        own lock. Beside a lock somebody else holds, it was read as that
        coordinator's: the session went through a coordinator that had gone.
        A note whose writer has gone - or whose pid was handed on to a process
        started after it - names nobody: never gone through, it is no note at
        all (its holder is given the moment to write one), and the refusal
        says the note was stale, never whose it was (D4)."""
        real, _CL.NOTE_WAIT_S = getattr(_CL, "NOTE_WAIT_S", None), 0.3
        self.addCleanup(setattr, _CL, "NOTE_WAIT_S", real)
        for i, (name, note) in enumerate((
                ("its writer gone", _dead_note()),
                ("its pid handed on", {**COORDINATOR_NOTE, "since": 1_000_000_000.0}))):
            with self.subTest(note=name):
                Through.made = []
                fh = _hold(self.lock(f"stale-{i}.lock"), note)
                try:
                    card = MachineCard(self.lock(f"stale-{i}.lock"), through=Through,
                                       patience_s=600)
                    t0 = time.monotonic()
                    got = self.bounded_take(card, terms=Terms(patience_s=0))
                    took = time.monotonic() - t0
                finally:
                    _CL._unlock(fh)
                self.assertEqual(Through.made, [], "a stale note's coordinator was gone through")
                self.assertEqual(getattr(got, "reason", None), "card_busy", str(got))
                self.assertIn("the note beside its lock is stale", str(got))
                for other in (str(note["pid"]), "30087", "7071", "crd-"):
                    self.assertNotIn(other, str(got))
                self.assertGreaterEqual(took, 0.3, "a stale note was taken for its holder's word: "
                                                   "no moment given for the holder's own")

    def test_a_lock_just_taken_beside_a_dead_holders_note_is_gone_through_once_its_note_comes(self):
        """...the instant after the next holder took the lock is when a dead
        holder's note is read: a coordinator that has only just taken it,
        its note written a moment later, is gone through - at its own
        address, never the dead one's."""
        dead = _dead_note()
        fh = _hold(self.lock(), dead)
        self.addCleanup(_CL._unlock, fh)
        card = MachineCard(self.lock(), through=Through, patience_s=600)
        looks = []
        real = card._coordinator

        def looking():
            said = real()
            looks.append(said)
            if len(looks) == 1:                     # its first look: the holder's note comes now
                Path(str(self.lock()) + _CL.NOTE_SUFFIX).write_bytes(
                    json.dumps(COORDINATOR_NOTE).encode("utf-8"))
            return said
        card._coordinator = looking
        got = self.bounded_take(card, terms=Terms(patience_s=0))
        self.assertNotIsInstance(got, _CL.CardWaitRefused, f"refused: {got}")
        self.assertEqual([via.url for via in Through.made], [COORDINATOR_NOTE["coordinator"]],
                         "not gone through the coordinator that holds the lock")
        self.assertEqual(looks, [None, COORDINATOR_NOTE["coordinator"]],
                         "the dead holder's note was read as its holder's")

    def test_a_patient_request_beside_a_stale_note_waits_for_the_lock_and_says_why(self):
        """A request that waits waits for the lock - never through the dead
        coordinator - and a patience that runs out says the note was stale."""
        fh = _hold(self.lock(), _dead_note())
        self.addCleanup(_CL._unlock, fh)
        card = MachineCard(self.lock(), through=Through, patience_s=600, retry_s=0.05)
        got = self.bounded_take(card, terms=Terms(patience_s=0.3))
        self.assertEqual((getattr(got, "reason", None), Through.made), ("card_patience", []),
                         str(got))
        self.assertIn("the note beside its lock is stale", str(got))

    def test_on_the_lock_the_card_is_read_here_and_judged_by_the_one_check(self):
        card = MachineCard(self.lock())
        card.take(W, "alpha")
        self.addCleanup(card.give_back)
        ok = card.fit("alpha", "llamacpp", 9000, read=lambda: {"free": 9256, "total": 24576},
                      margin_mib=256, how="weights and cache")
        self.assertEqual((ok["verdict"], ok["need_mib"], ok["free_mib"], ok["total_mib"]),
                         ("fits", 9000, 9256, 24576))
        no = card.fit("alpha", "llamacpp", 9001, read=lambda: {"free": 9256, "total": 24576},
                      margin_mib=256, how="weights and cache")
        self.assertEqual(no["verdict"], "no_room")
        self.assertIn("not enough free VRAM for alpha", no["words"])
        self.assertIn("9001 MiB", no["words"])
        never = card.fit("alpha", "llamacpp", 30000, read=lambda: 1024, margin_mib=256,
                         hint="Lower ctx.")
        self.assertEqual((never["verdict"], never["total_mib"]), ("no_room", None),
                         "a card whose total is not known can still have no room")
        never = card.fit("alpha", "llamacpp", 30000, read=lambda: {"free": 1000, "total": 24576},
                         margin_mib=256, hint="Lower ctx.")
        self.assertEqual(never["verdict"], "never_fits")
        self.assertIn("cannot fit on this card at all", never["words"])
        self.assertIn("Lower ctx.", never["words"])
        self.assertEqual(card.fit("alpha", "llamacpp", 9000, read=lambda: None,
                                  margin_mib=256)["verdict"], "unknown")

    def test_on_the_lock_no_room_now_is_said_loudly_what_is_outside_and_what_to_do(self):
        """H9, the owner's ruling of 2026-09-28 ("Fail loudly"): a model that
        does not fit on a card that is not over-full is refused at once, in
        the contract's words - the numbers, what programs outside the
        bootstrap hold (the card's use less what this session's own
        processes hold), and what to do: close programs, or reboot."""
        card = MachineCard(self.lock())
        card.take(W, "alpha")
        self.addCleanup(card.give_back)
        card.running(4242, "load-a2b3c4d5", "alpha", "llamacpp", 27001, mib=5000)
        no = card.fit("beta", "llamacpp", 9001, read=lambda: {"free": 9256, "total": 24576},
                      margin_mib=256, how="weights and cache")
        self.assertEqual(no["verdict"], "no_room")
        for said in ("not enough free VRAM for beta", "9001 MiB", "9256 MiB free of 24576",
                     "programs outside the bootstrap hold 10320 MiB",
                     "does not wait for room", "Close programs that use the card, or reboot",
                     "(weights and cache)"):
            self.assertIn(said, no["words"])
        self.assertEqual(no.get("outside_mib"), 10320,
                         "the numbers a record keeps: what programs outside hold")
# =============================================== native: side by side (S7) ===

SIDE = SETTINGS + '\n[models.gamma]\nlabel = "Gamma"\n'


def card_of_two(m):
    """A fit check that says what a card with room for two models would: a
    third does not fit while two are on it (being stopped included) - in the
    words a real refusal uses. It stands in for the card source's decision
    (S4's), asked through the one method the manager asks."""
    def fit(key):
        if len(m._loads.on_card()) >= 2:
            raise BackendError(f"not enough free VRAM for {key}: two models fill the card")
    return fit


class NativeSideBySideRows(Case):
    """PLAN-4 D7, S7: models side by side inside a rental - one load of each,
    loaded one at a time under the load lock and never the manager's lock,
    so nothing that asks the manager waits for a load; stops that run
    together; pinned models and models on demand; room made by taking off
    the on-demand model used longest ago; every load naming what else was up.
    The clock is the row's."""

    def side(self, **kw):
        return self.manager(self.config(SIDE), **kw)

    def up(self, m):
        return [x["key"] for x in m.models()["models"] if x["loaded"]]

    def wait_for(self, what, pred, timeout=5.0):
        deadline = time.monotonic() + timeout
        while not pred():
            self.assertLess(time.monotonic(), deadline, f"timed out waiting for {what}")
            time.sleep(0.01)

    def quick(self, what, fn, bound=1.0):
        """*fn*, answered within *bound* seconds - or the row fails, naming
        it: a call that waited out a load would take the load's whole time."""
        t0 = time.monotonic()
        out = fn()
        self.assertLess(time.monotonic() - t0, bound, f"{what} waited for a load")
        return out

    def stop_held(self, key, fails=None):
        """A backend factory whose *key* stops only once the row lets it -
        and then fails in *fails*' words, if given. -> (the gate the row
        sets, the event its stop began, the factory)."""
        gate, began = threading.Event(), threading.Event()
        self.addCleanup(gate.set)

        class Held(FakeBackend):
            def stop(self_):
                if self_.model_key == key:
                    began.set()
                    gate.wait(5)
                super().stop()
        return gate, began, lambda k, port: Held(k, stop_fails=fails if k == key else None)

    # ------------------------------------------------------------ the table

    def test_two_models_stay_up_side_by_side_and_each_load_names_the_other(self):
        m = self.side()
        a = m.acquire("alpha", W)
        b = m.acquire("beta", W)
        self.assertEqual((a.stops, b.stops), (0, 0), "a second model took the first off")
        self.assertEqual(self.up(m), ["alpha", "beta"])
        oks = [e for e in self.events(m) if e["event"] == "load_ok"]
        self.assertEqual([[(x["model_key"], x["load"]) for x in e["beside"]] for e in oks],
                         [[], [("alpha", a.load_id)]])
        events = self.events(m)
        self.assertEqual((max_concurrency(events), runner.side_by_side(events),
                          runner.beside_faults(events)), (1, 2, []))
        m.unload("alpha")
        self.assertEqual(m.status()["lease"]["holder"], W, "the hold ended with a model up")
        m.unload("beta")
        self.assertIsNone(m.status()["lease"]["holder"], "the hold outlived the last model")

    def test_a_model_loaded_again_while_its_stop_runs_waits_for_the_stop(self):
        """Never two loads of one model: a load of alpha while alpha's stop is
        under way waits the stop out - its process may still hold the card -
        then goes up; it is neither refused nor loaded beside it."""
        gate, stopping = threading.Event(), threading.Event()
        self.addCleanup(gate.set)

        class SlowOff(FakeBackend):
            def stop(self_):
                stopping.set()
                gate.wait(5)
                super().stop()
        m = self.side(backend_factory=lambda key, port: SlowOff(key))
        old = m.acquire("alpha", W)
        off = threading.Thread(target=m.unload, args=("alpha",), daemon=True)
        off.start()
        self.assertTrue(stopping.wait(5), "the stop never began")
        box = {}
        again = threading.Thread(target=lambda: box.update(got=m.acquire("alpha", W)),
                                 daemon=True)
        again.start()
        time.sleep(0.3)
        self.assertTrue(again.is_alive(), "alpha loaded again before its stop ended")
        gate.set()
        off.join(5)
        again.join(5)
        self.assertIsNot(box.get("got"), old)
        self.assertIsInstance(box.get("got"), FakeBackend, box)
        self.assertEqual(max_concurrency(self.events(m)), 1)

    def test_a_close_asks_a_stop_that_fails_in_it_once(self):
        """A model whose stop fails in a close is asked once there - the old
        failed stops are asked again first - and the close is refused: a
        llama.cpp stop can take its whole bound, and the close would wait it
        out twice."""
        m = self.side(card=RecordingCard(),
                      backend_factory=self.factory(stop_fails="the process would not die"))
        opened = m.open_pipeline(W, models=["alpha"], keep_s=0, wait_s=5)
        with self.assertRaises(PipelineRefused) as caught:
            m.close_pipeline(opened["id"])
        self.assertEqual(caught.exception.reason, "unload_failed")
        load = next(e["load"] for e in self.events(m) if e["event"] == "load_ok")
        asked = [e["event"] for e in self.events(m)
                 if e.get("load") == load and e["event"].startswith("unload_")]
        self.assertEqual(asked, ["unload_start", "unload_failed"],
                         "a stop that failed in the close was asked again in it")

    def test_no_load_goes_up_on_a_card_a_close_gave_back(self):
        """A load that looked before a close began and goes on - here, taking
        its port from a slow registry - is seen by the close: never a model
        going up while the card is not held. Read off the record: every load
        between a take of the card and its give-back."""
        path = self.tmp / "card.lock"
        taking, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        class SlowPorts(Ports):
            def take(self_, model_key, load=None):
                taking.set()
                release.wait(5)
                return super().take(model_key, load)
        m = self.manager(self.config(LLAMA_SETTINGS), card=MachineCard(path),
                         ports=SlowPorts(41000, 41001, 41002), vram=lambda: None)
        opened = m.open_pipeline(W, keep_s=0, wait_s=5)
        job = threading.Thread(target=lambda: self.suppressed(m.acquire, "alpha", W),
                               daemon=True)
        job.start()
        self.assertTrue(taking.wait(5), "the load never took its port")
        closing = threading.Thread(target=m.close_pipeline, args=(opened["id"],), daemon=True)
        closing.start()
        time.sleep(0.3)
        release.set()
        closing.join(5)
        job.join(5)
        self.assertFalse(closing.is_alive() or job.is_alive())
        held, wrong = False, []
        for e in self.events(m):
            if e["event"] == "card_granted":
                held = True
            elif e["event"] == "card_given_back":
                held = False
            elif e["event"] in ("load_start", "load_ok") and not held:
                wrong.append(e["event"])
        self.assertEqual(wrong, [], "a model went up on a card that was not held")

    def test_a_slow_port_registry_never_holds_up_an_answer(self):
        """A load takes its port outside the manager's lock: a registry that
        is slow to answer - a question over HTTP - holds up that load alone,
        never a generation on a model already up, nor health."""
        taking, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        class SlowPorts(Ports):
            def take(self_, model_key, load=None):
                if model_key == "beta":
                    taking.set()
                    release.wait(5)
                return super().take(model_key, load)
        m = self.manager(self.config(LLAMA_SETTINGS), ports=SlowPorts(41000, 41001),
                         vram=lambda: None)
        alpha = m.acquire("alpha", W)
        loading = threading.Thread(target=lambda: self.suppressed(m.acquire, "beta", W),
                                   daemon=True)
        loading.start()
        self.assertTrue(taking.wait(5), "beta never asked for its port")
        self.assertIs(self.quick("a generation on alpha", lambda: m.acquire("alpha", W)), alpha)
        self.quick("health", m.status)
        release.set()
        loading.join(5)
        self.assertEqual(self.up(m), ["alpha", "beta"])

    def test_a_close_waits_out_a_load_that_cannot_be_given_up(self):
        """A load under way that the close's give-up cannot reach - a backend
        with no way to give up - is waited out, and taken off once it is up,
        before the card goes back: never a model going up on a card given
        back. Read off the record."""
        path = self.tmp / "card.lock"
        gate, started = threading.Event(), threading.Event()
        self.addCleanup(gate.set)

        def on_start(b):
            started.set()
            gate.wait(5)
        m = self.side(card=MachineCard(path), backend_factory=self.factory(on_start=on_start))
        opened = m.open_pipeline(W, keep_s=0, wait_s=5)
        job = threading.Thread(target=lambda: self.suppressed(m.acquire, "alpha", W),
                               daemon=True)
        job.start()
        self.assertTrue(started.wait(5), "the load never began")
        closing = threading.Thread(target=m.close_pipeline, args=(opened["id"],), daemon=True)
        closing.start()
        time.sleep(0.3)
        self.assertFalse(_free(path), "the card went back under a load still going up")
        gate.set()
        closing.join(5)
        job.join(5)
        self.assertEqual((_free(path), self.up(m)), (True, []))
        held, wrong = False, []
        for e in self.events(m):
            if e["event"] == "card_granted":
                held = True
            elif e["event"] == "card_given_back":
                held = False
            elif e["event"] in ("load_start", "load_ok") and not held:
                wrong.append(e["event"])
        self.assertEqual(wrong, [], "a model went up on a card that was not held")

    def test_a_close_refuses_while_a_stop_another_caller_began_has_failed(self):
        """A stop another caller began is waited out; when it fails, that
        model may still be on the card, and the close is refused - the card
        kept - as for a stop of its own that failed."""
        path = self.tmp / "card.lock"
        gate, stopping = threading.Event(), threading.Event()
        self.addCleanup(gate.set)

        class FailsLate(FakeBackend):
            def stop(self_):
                stopping.set()
                gate.wait(5)
                raise RuntimeError("the process would not die")
        m = self.side(card=MachineCard(path), backend_factory=lambda key, port: FailsLate(key))
        opened = m.open_pipeline(W, models=["alpha"], keep_s=0, wait_s=5)
        off = threading.Thread(target=lambda: self.suppressed(m.unload, "alpha"), daemon=True)
        off.start()
        self.assertTrue(stopping.wait(5), "the stop never began")
        box = {}
        closing = threading.Thread(target=lambda: box.update(
            got=self.suppressed(m.close_pipeline, opened["id"])), daemon=True)
        closing.start()
        time.sleep(0.3)
        gate.set()
        closing.join(5)
        off.join(5)
        self.assertIsInstance(box.get("got"), PipelineRefused, box)
        self.assertEqual(box["got"].reason, "unload_failed")
        self.assertFalse(_free(path), "the card went back while a model may still be on it")

    def suppressed(self, fn, *a):
        try:
            return fn(*a)
        except Exception as exc:                            # noqa: BLE001 - the row reads the record
            return exc

    def test_a_shutdown_waits_out_a_stop_under_way(self):
        """The server stopping does not cut the close's waits short: a stop
        another caller began is waited out before the card goes back."""
        path = self.tmp / "card.lock"
        gate, stopping = threading.Event(), threading.Event()
        self.addCleanup(gate.set)

        class SlowOff(FakeBackend):
            def stop(self_):
                stopping.set()
                gate.wait(5)
                super().stop()
        m = self.side(card=MachineCard(path), backend_factory=lambda key, port: SlowOff(key))
        m.open_pipeline(W, models=["alpha"], keep_s=0, wait_s=5)
        off = threading.Thread(target=m.unload, args=("alpha",), daemon=True)
        off.start()
        self.assertTrue(stopping.wait(5), "the stop never began")
        m.stopping("the server is stopping")
        down = threading.Thread(target=m.shutdown, daemon=True)
        down.start()
        time.sleep(0.3)
        self.assertFalse(_free(path), "the card went back while a model was still coming off")
        gate.set()
        down.join(5)
        off.join(5)
        self.assertTrue(_free(path))

    def test_a_close_waits_for_a_stop_another_caller_began(self):
        """The card goes back only once every model has come off: a stop
        another caller began is waited out before the close gives it back."""
        path = self.tmp / "card.lock"
        gate, stopping = threading.Event(), threading.Event()
        self.addCleanup(gate.set)

        class SlowOff(FakeBackend):
            def stop(self_):
                stopping.set()
                gate.wait(5)
                super().stop()
        m = self.side(card=MachineCard(path), backend_factory=lambda key, port: SlowOff(key))
        opened = m.open_pipeline(W, models=["alpha"], keep_s=0, wait_s=5)
        off = threading.Thread(target=m.unload, args=("alpha",), daemon=True)
        off.start()
        self.assertTrue(stopping.wait(5), "the stop never began")
        closing = threading.Thread(target=m.close_pipeline, args=(opened["id"],), daemon=True)
        closing.start()
        time.sleep(0.3)
        self.assertFalse(_free(path), "the card went back while a model was still coming off")
        gate.set()
        closing.join(5)
        off.join(5)
        self.assertTrue(_free(path))

    def test_the_records_check_finds_two_loads_of_one_model_and_a_wrong_beside(self):
        """The instrument itself: `max_concurrency` counts loads of ONE model,
        `side_by_side` any, and `beside_faults` names a load whose line does
        not say exactly what the record held open."""
        def ok(model, load, *beside):
            return {"event": "load_ok", "model_key": model, "load": load,
                    "beside": [{"model_key": k, "load": x} for k, x in beside]}
        start = {"event": "session_start"}
        two = [start, ok("alpha", "load-a"), ok("beta", "load-b", ("alpha", "load-a"))]
        self.assertEqual((max_concurrency(two), runner.side_by_side(two),
                          runner.beside_faults(two)), (1, 2, []))
        twice = two + [ok("alpha", "load-c", ("alpha", "load-a"), ("beta", "load-b"))]
        self.assertEqual(max_concurrency(twice), 2, "two loads of alpha went unseen")
        wrong = [start, ok("alpha", "load-a"), ok("beta", "load-b")]
        (fault,) = runner.beside_faults(wrong)
        self.assertIn("load-b (beta) names [] beside it", fault)
        silent = [start, {"event": "load_ok", "model_key": "alpha", "load": "load-a"}]
        (fault,) = runner.beside_faults(silent)
        self.assertIn("names nothing beside it", fault)
        gone = [start, ok("alpha", "load-a"), {"event": "unload_ok", "load": "load-a"},
                ok("beta", "load-b"), {"event": "session_start"}, ok("gamma", "load-c")]
        self.assertEqual((runner.beside_faults(gone), runner.side_by_side(gone)), ([], 1))

    def test_loads_run_one_at_a_time_under_the_load_lock(self):
        going, gate, alpha_started = [], threading.Event(), threading.Event()
        self.addCleanup(gate.set)

        def on_start(b):
            going.append(("start", b.model_key))
            if b.model_key == "alpha":
                alpha_started.set()
                gate.wait(5)
            going.append(("end", b.model_key))
        m = self.side(backend_factory=self.factory(on_start=on_start))
        first = threading.Thread(target=m.acquire, args=("alpha", W), daemon=True)
        first.start()
        self.assertTrue(alpha_started.wait(5), "alpha's load never began")
        second = threading.Thread(target=m.acquire, args=("beta", W), daemon=True)
        second.start()
        time.sleep(0.3)
        self.assertEqual(going, [("start", "alpha")], "beta's load began beside alpha's")
        self.assertEqual(m.status()["loading"], "alpha")
        gate.set()
        first.join(5)
        second.join(5)
        self.assertEqual(going, [("start", "alpha"), ("end", "alpha"),
                                 ("start", "beta"), ("end", "beta")])
        self.assertEqual(self.up(m), ["alpha", "beta"])

    def test_nothing_that_asks_the_manager_waits_for_a_load(self):
        """A load holds the load lock, never the manager's: a generation on a
        model already up, a use of it, health, a bind (S6 R7: a bind waited
        on the manager's lock while the start session's model loaded) and a
        stop of another model are each answered while beta's weights go up."""
        gate, beta_started = threading.Event(), threading.Event()
        self.addCleanup(gate.set)

        def on_start(b):
            if b.model_key == "beta":
                beta_started.set()
                gate.wait(5)
        m = self.side(backend_factory=self.factory(on_start=on_start))
        alpha = m.acquire("alpha", W)
        m.acquire("gamma", W)
        loading = threading.Thread(target=m.acquire, args=("beta", W), daemon=True)
        loading.start()
        self.assertTrue(beta_started.wait(5), "beta's load never began")
        self.assertIs(self.quick("a generation on alpha", lambda: m.acquire("alpha", W)), alpha)

        def use():
            with m.in_use("alpha"):
                pass
        self.quick("a use of alpha", use)
        self.assertEqual(self.quick("health", m.status)["loading"], "beta")
        self.quick("a bind", lambda: m.rebind(self.tmp / "other"))
        self.quick("a stop of gamma", lambda: m.unload("gamma"))
        self.assertTrue(loading.is_alive(), "beta's load ended before the row released it")
        gate.set()
        loading.join(5)
        self.assertEqual(self.up(m), ["alpha", "beta"])

    def test_stops_run_together(self):
        began, release = [], threading.Event()
        self.addCleanup(release.set)

        class Slow(FakeBackend):
            def stop(self_):
                began.append(self_.model_key)
                release.wait(5)
                super().stop()
        m = self.side(backend_factory=lambda key, port: Slow(key))
        m.acquire("alpha", W)
        m.acquire("beta", W)
        stops = [threading.Thread(target=m.unload, args=(key,), daemon=True)
                 for key in ("alpha", "beta")]
        for t in stops:
            t.start()
        self.wait_for("both stops under way at once", lambda: len(began) == 2, timeout=3.0)
        release.set()
        for t in stops:
            t.join(5)
        self.assertEqual((sorted(began), self.up(m)), (["alpha", "beta"], []))

    # ------------------------------------------------------ making room

    def test_a_load_that_does_not_fit_takes_the_on_demand_model_used_longest_ago_off(self):
        m = self.side()
        m._check_vram = card_of_two(m)
        a = m.acquire("alpha", W)
        b = m.acquire("beta", W)
        m.acquire("alpha", W)                   # used again: beta is the one used longest ago
        m.acquire("gamma", W)
        self.assertEqual((a.stops, b.stops), (0, 1))
        self.assertEqual(self.up(m), ["alpha", "gamma"])
        made = [e for e in self.events(m) if e["event"] == "room_made"]
        self.assertEqual([(e["model_key"], e["load"], e["for_model"]) for e in made],
                         [("beta", b.load_id, "gamma")])
        self.assertIn("not enough free VRAM for gamma", made[0]["detail"])
        self.assertLess(self.kinds(m).index("room_made"), self.kinds(m).index("unload_ok"))

    def test_a_pinned_model_never_comes_off_for_another(self):
        """The on-demand one comes off though the pinned one was used longest
        ago; with only pinned models up, the load is refused in the fit
        check's own words, and nothing comes off."""
        m = self.side(card=RecordingCard())
        m._check_vram = card_of_two(m)
        m.open_pipeline(W, models=["alpha"], keep_s=0)
        self.wait_for("alpha up", lambda: self.up(m) == ["alpha"])
        b = m.acquire("beta", W)
        m.acquire("gamma", W)
        self.assertEqual((b.stops, self.up(m)), (1, ["alpha", "gamma"]))
        m.unload("gamma")
        m.load_model(W, "beta", wait_s=5)
        self.assertEqual(self.up(m), ["alpha", "beta"])
        with self.assertRaises(BackendError) as caught:
            m.acquire("gamma", W)
        self.assertIn("not enough free VRAM for gamma", str(caught.exception))
        self.assertEqual(self.up(m), ["alpha", "beta"], "a pinned model came off")
        self.assertEqual(self.kinds(m).count("room_made"), 1)

    def test_a_model_that_can_never_fit_takes_no_other_model_off(self):
        """Where S4 and S7 meet, found merging them: making room takes an idle
        model off only for a model some room could hold. One the card source
        says could never fit - more than the whole card - is refused at once,
        in its words, and every model on the card stays."""
        card = Deciding(_verdict("fits"))
        m = self.side(card=card)
        a = m.acquire("alpha", W)
        card.verdict = _verdict("never_fits", words="gamma never fits: in numbers")
        with self.assertRaises(BackendError) as caught:
            m.acquire("gamma", W)
        self.assertIn("gamma never fits", str(caught.exception))
        self.assertEqual((a.stops, self.up(m)), (0, ["alpha"]), "a model came off for nothing")
        self.assertNotIn("room_made", self.kinds(m))

    def test_a_model_answering_a_request_never_comes_off_to_make_room(self):
        m = self.side()
        m._check_vram = card_of_two(m)
        m.acquire("alpha", W)
        b = m.acquire("beta", W)
        with m.in_use("alpha"):                     # alpha, used longest ago, is answering
            m.acquire("gamma", W)
            self.assertEqual((b.stops, self.up(m)), (1, ["alpha", "gamma"]))
            with m.in_use("gamma"):
                with self.assertRaises(BackendError):
                    m.acquire("beta", W)            # both answering: nothing may come off
        self.assertEqual(self.up(m), ["alpha", "gamma"])

    def test_at_most_max_loads_side_by_side_and_one_on_a_configured_port(self):
        m = self.side()
        m.cfg.runner["max_loads"] = 2
        a = m.acquire("alpha", W)
        m.acquire("beta", W)
        m.acquire("gamma", W)
        self.assertEqual((a.stops, self.up(m)), (1, ["beta", "gamma"]))
        (made,) = [e for e in self.events(m) if e["event"] == "room_made"]
        self.assertEqual(made["detail"], "no room for gamma: 2 model(s) on the card, and this "
                                         "runner keeps at most 2 at once")
        one = self.manager(self.config(LLAMA_SETTINGS.replace(
            'backend = "llamacpp"', 'backend = "llamacpp"\nport = 8901'), name="one.toml"),
            vram=lambda: None, folder="one")
        first = one.acquire("alpha", W)
        one.acquire("beta", W)
        self.assertEqual((first.stops, [x["key"] for x in one.models()["models"] if x["loaded"]]),
                         (1, ["beta"]), "two loads were pointed at one configured port")
        (made,) = [e for e in self.events(one) if e["event"] == "room_made"]
        self.assertIn("its settings name one port for every model", made["detail"])

    def test_a_stop_under_way_is_room_made_already_never_a_second_model_off(self):
        """A load that does not fit while another caller's stop is under way
        waits that stop out, and is measured again: the room it makes is
        room made - no second model comes off for it (found by review: beta
        came off beside alpha, and a real card would have loaded it again,
        for minutes, for its next job)."""
        gate, began, factory = self.stop_held("alpha")
        m = self.side(backend_factory=factory)
        m.cfg.runner["max_loads"] = 2
        m.acquire("alpha", W)
        b = m.acquire("beta", W)
        off = threading.Thread(target=m.unload, args=("alpha",), daemon=True)
        off.start()
        self.assertTrue(began.wait(5), "alpha's stop never began")
        box = {}
        job = threading.Thread(
            target=lambda: box.update(got=self.suppressed(m.acquire, "gamma", W)), daemon=True)
        job.start()
        time.sleep(0.3)
        self.assertEqual(b.stops, 0, "beta came off for room alpha's stop was making")
        self.assertTrue(job.is_alive(), "gamma was answered before alpha's stop ended")
        gate.set()
        off.join(5)
        job.join(5)
        self.assertIsInstance(box.get("got"), FakeBackend, box)
        self.assertEqual((b.stops, self.up(m)), (0, ["beta", "gamma"]))
        self.assertNotIn("room_made", self.kinds(m))

    def test_a_load_is_never_refused_for_room_a_stop_under_way_makes(self):
        """The same with beta answering a request, so nothing may come off
        for gamma: alpha's stop under way makes its room, and gamma waits it
        out and goes up - never refused "no room" while the room was being
        made."""
        gate, began, factory = self.stop_held("alpha")
        m = self.side(backend_factory=factory)
        m.cfg.runner["max_loads"] = 2
        m.acquire("alpha", W)
        m.acquire("beta", W)
        box = {}
        with m.in_use("beta"):
            off = threading.Thread(target=m.unload, args=("alpha",), daemon=True)
            off.start()
            self.assertTrue(began.wait(5), "alpha's stop never began")
            job = threading.Thread(
                target=lambda: box.update(got=self.suppressed(m.acquire, "gamma", W)),
                daemon=True)
            job.start()
            time.sleep(0.3)
            self.assertTrue(job.is_alive(), f"gamma was answered before alpha's stop ended: "
                                            f"{box.get('got')!r}")
            gate.set()
            off.join(5)
            job.join(5)
        self.assertIsInstance(box.get("got"), FakeBackend, box)
        self.assertEqual(self.up(m), ["beta", "gamma"])

    def test_a_stop_under_way_that_fails_refuses_the_load_that_waited_for_it(self):
        """A stop the load waited out that FAILED leaves its model perhaps on
        the card: the load is refused in words that name it - nothing loads
        while one may be - and nothing else comes off for it."""
        gate, began, factory = self.stop_held("alpha", fails="the process would not die")
        m = self.side(backend_factory=factory)
        m.cfg.runner["max_loads"] = 2
        a = m.acquire("alpha", W)
        b = m.acquire("beta", W)
        off = threading.Thread(target=lambda: self.suppressed(m.unload, "alpha"), daemon=True)
        off.start()
        self.assertTrue(began.wait(5), "alpha's stop never began")
        box = {}
        job = threading.Thread(
            target=lambda: box.update(got=self.suppressed(m.acquire, "gamma", W)), daemon=True)
        job.start()
        time.sleep(0.3)
        gate.set()
        off.join(5)
        job.join(5)
        got = box.get("got")
        self.assertIsInstance(got, BackendError, box)
        self.assertIn(f"alpha ({a.load_id}) did not come off the card", str(got))
        self.assertEqual((b.stops, self.up(m)), (0, ["beta"]), "beta came off for a load refused")

    def test_a_stop_that_fails_as_a_load_begins_refuses_the_load(self):
        """Stops run beside loads, so a stop can fail after a load last asked
        - here while the load takes its port from a slow registry: the load
        looks again as it begins, and is refused while a model that would not
        stop may still be on the card - never loaded beside it. Its port goes
        back."""
        taking, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        class SlowPorts(Ports):
            def take(self_, model_key, load=None):
                if model_key == "beta":
                    taking.set()
                    release.wait(5)
                return super().take(model_key, load)
        ports = SlowPorts(41000, 41001, 41002)
        m = self.manager(self.config(LLAMA_SETTINGS), ports=ports, vram=lambda: None,
                         backend_factory=self.factory(stop_fails="the process would not die"))
        a = m.acquire("alpha", W)
        box = {}
        job = threading.Thread(
            target=lambda: box.update(got=self.suppressed(m.acquire, "beta", W)), daemon=True)
        job.start()
        self.assertTrue(taking.wait(5), "beta never took its port")
        with self.assertRaises(RuntimeError):
            m.unload("alpha")                       # its stop fails: it may still be on the card
        release.set()
        job.join(5)
        got = box.get("got")
        self.assertIsInstance(got, BackendError, box)
        self.assertIn(f"alpha ({a.load_id}) did not come off the card", str(got))
        self.assertNotIn("beta", [e.get("model_key") for e in self.events(m)
                                  if e["event"] == "load_start"], "beta went up beside it")
        self.assertEqual(ports.given_back, [41001], "the refused load's port was kept")

    def test_a_load_waiting_for_the_room_a_stop_makes_asks_the_fit_check_once_it_ends(self):
        """Through the card source's fit check (S4's decision, asked through
        the one method): a load that does not fit beside a model being
        stopped waits the stop out - asking nothing meanwhile, never in a
        spin (a real check asks the card, or a coordinator) - and asks again
        once, when the stop has ended."""
        gate, began, factory = self.stop_held("alpha")
        m = self.side(backend_factory=factory)
        asked, fit = [], card_of_two(m)

        def check(key):
            asked.append(key)
            fit(key)
        m._check_vram = check
        m.acquire("alpha", W)
        b = m.acquire("beta", W)
        del asked[:]
        off = threading.Thread(target=m.unload, args=("alpha",), daemon=True)
        off.start()
        self.assertTrue(began.wait(5), "alpha's stop never began")
        job = threading.Thread(target=lambda: self.suppressed(m.acquire, "gamma", W),
                               daemon=True)
        job.start()
        time.sleep(0.3)
        self.assertEqual((len(asked), b.stops), (1, 0),
                         "the fit check was asked again while alpha's stop ran, or beta came off")
        gate.set()
        off.join(5)
        job.join(5)
        self.assertEqual((asked, b.stops, self.up(m)), (["gamma", "gamma"], 0, ["beta", "gamma"]))

    def test_a_cancel_ends_a_loads_wait_for_the_room_a_stop_makes(self):
        """A job cancelled while its load waits out another model's stop for
        its room ends at once - the wait is its own, never the stop's - and
        nothing comes off for it."""
        gate, began, factory = self.stop_held("alpha")
        m = self.side(backend_factory=factory)
        m.cfg.runner["max_loads"] = 2
        m.acquire("alpha", W)
        b = m.acquire("beta", W)
        off = threading.Thread(target=m.unload, args=("alpha",), daemon=True)
        off.start()
        self.assertTrue(began.wait(5), "alpha's stop never began")
        cancel, box = threading.Event(), {}
        job = threading.Thread(target=lambda: box.update(
            got=self.suppressed(lambda: m.acquire("gamma", W, cancel=cancel))), daemon=True)
        job.start()
        time.sleep(0.3)
        self.assertTrue(job.is_alive(), f"gamma did not wait for alpha's stop: {box}")
        cancel.set()
        job.join(2)
        self.assertFalse(job.is_alive(), "the cancel did not end the wait for room")
        self.assertIsInstance(box.get("got"), Cancelled, box)
        gate.set()
        off.join(5)
        self.assertEqual((b.stops, self.up(m)), (0, ["beta"]))

    def test_the_server_stopping_ends_a_loads_wait_for_the_room_a_stop_makes(self):
        """The server stopping ends a load's wait for the room another
        model's stop is making at once: nothing more is loaded, and a stop
        never waits out work nobody will use."""
        gate, began, factory = self.stop_held("alpha")
        m = self.side(backend_factory=factory)
        m.cfg.runner["max_loads"] = 2
        m.acquire("alpha", W)
        m.acquire("beta", W)
        off = threading.Thread(target=m.unload, args=("alpha",), daemon=True)
        off.start()
        self.assertTrue(began.wait(5), "alpha's stop never began")
        box = {}
        job = threading.Thread(
            target=lambda: box.update(got=self.suppressed(m.acquire, "gamma", W)), daemon=True)
        job.start()
        time.sleep(0.3)
        self.assertTrue(job.is_alive(), f"gamma did not wait for alpha's stop: {box}")
        m.stopping("the server is stopping")
        job.join(2)
        self.assertFalse(job.is_alive(), "the server stopping did not end the wait for room")
        self.assertIsInstance(box.get("got"), BackendError, box)
        self.assertIn("no model is loaded now: the server is stopping", str(box["got"]))
        gate.set()
        off.join(5)

    # ---------------------------------------------------- on demand, pinned

    def test_the_idle_sweep_takes_each_on_demand_model_on_its_own_clock(self):
        m = self.side()
        m.cfg.runner["idle_unload_s"] = 600
        m.acquire("alpha", W)
        m.acquire("beta", W)
        self.clock.tick(400)
        m.acquire("beta", W)                        # beta used: its clock starts again
        self.clock.tick(250)
        self.assertTrue(m.maybe_idle_unload())
        self.assertEqual(self.up(m), ["beta"], "one model's use kept every model up")
        with m.in_use("beta"):
            self.clock.tick(900)
            self.assertFalse(m.maybe_idle_unload(), "a model answering a request was swept")
        self.assertEqual(self.up(m), ["beta"])
        self.clock.tick(600)
        self.assertTrue(m.maybe_idle_unload())
        self.assertEqual(self.up(m), [])
        swept = [(e["model_key"], e["idle_seconds"]) for e in self.events(m)
                 if e["event"] == "idle_unload"]
        self.assertEqual(swept, [("alpha", 650.0), ("beta", 600.0)])

    def test_a_pinned_load_answers_the_moment_it_is_up_within_its_wait(self):
        """The Dictaphone's H-4: `load` waits until the model answers, or
        fails, within its bound - and not a moment longer."""
        how = {"slow": 0.0, "fail": None}

        def make(key, port):
            b = FakeBackend(key, fail=how["fail"], on_start=lambda _b: time.sleep(how["slow"]))
            self.made.append(b)
            return b
        m = self.side(backend_factory=make, card=RecordingCard())
        with self.assertRaises(PipelineRefused) as caught:
            m.load_model(W, "alpha")
        self.assertEqual((caught.exception.status, caught.exception.reason), (409, "no_pipeline"))
        m.acquire("gamma", W)                       # the queue's own pipeline keeps no model
        with self.assertRaises(PipelineRefused) as caught:
            m.load_model(W, "alpha")
        self.assertEqual(caught.exception.reason, "no_pipeline")
        with self.assertRaises(KeyError):
            m.load_model(W, "delta")
        opened = m.open_pipeline(W, keep_s=0)
        how["slow"] = 0.4
        t0 = time.monotonic()
        got = m.load_model(W, "alpha", wait_s=10)
        took = time.monotonic() - t0
        self.assertEqual((got["model"]["key"], got["model"]["state"], got["pipeline"]["id"]),
                         ("alpha", "up", opened["id"]))
        self.assertEqual(got["model"]["load"], m._loads.of("alpha").load_id)
        self.assertTrue(0.3 < took < 5.0, f"answered after {took:.2f}s, not when it was up")
        self.assertEqual((got["pipeline"]["models"], got["pipeline"]["up"]),
                         (["alpha"], ["gamma", "alpha"]))
        at_once = m.load_model(W, "beta")
        self.assertIn(at_once["model"]["state"], ("loading", "waiting"), at_once)
        self.wait_for("beta up", lambda: "beta" in self.up(m))
        m.cfg.runner["max_loads"] = 2
        with self.assertRaises(PipelineRefused) as caught:
            m.load_model(W, "gamma")                # alpha and beta kept: as many as it holds
        self.assertEqual((caught.exception.status, caught.exception.reason),
                         (409, "too_many_models"))
        m.cfg.runner["max_loads"] = 8
        how.update(slow=0.0, fail="no weights")
        m.unload("gamma")
        failed = m.load_model(W, "gamma", wait_s=10)
        self.assertEqual((failed["model"]["state"], failed["model"]["why"]), ("failed", "no weights"))
        self.assertIn("gamma could not be loaded: no weights", failed["pipeline"]["why"])
        m.stopping("the server is stopping")
        with self.assertRaises(PipelineRefused) as caught:
            m.load_model(W, "alpha")
        self.assertEqual(caught.exception.reason, "stopping")

    def test_an_open_waits_until_every_model_it_names_is_up(self):
        m = self.side(backend_factory=self.factory(on_start=lambda _b: time.sleep(0.3)),
                      card=RecordingCard())
        t0 = time.monotonic()
        opened = m.open_pipeline(W, models=["alpha", "beta"], keep_s=0, wait_s=10)
        took = time.monotonic() - t0
        self.assertEqual((opened["state"], opened["models"], opened["up"]),
                         ("holding", ["alpha", "beta"], ["alpha", "beta"]))
        self.assertTrue(0.5 < took < 5.0, f"answered after {took:.2f}s")
        oks = [e for e in self.events(m) if e["event"] == "load_ok"]
        self.assertEqual([len(e["beside"]) for e in oks], [0, 1])
        m.close_pipeline(opened["id"])

        class SlowCard(RecordingCard):
            def take(self_, *a, **kw):
                time.sleep(0.3)
                return super().take(*a, **kw)
        slow = self.side(card=SlowCard(), folder="slow")
        empty = slow.open_pipeline(W, keep_s=0, wait_s=10)
        self.assertEqual((empty["state"], empty["up"]), ("holding", []),
                         "a pipeline naming no model answered before it held the card")

    def test_unload_takes_one_model_off_and_the_rest_and_the_card_stay(self):
        path = self.tmp / "card.lock"
        how = {"stop_fails": None}

        def make(key, port):
            b = FakeBackend(key, stop_fails=how["stop_fails"])
            self.made.append(b)
            return b
        m = self.side(card=MachineCard(path), backend_factory=make)
        opened = m.open_pipeline(W, models=["alpha", "beta"], keep_s=0, wait_s=10)
        self.assertEqual(opened["up"], ["alpha", "beta"])
        self.assertEqual(m.unload_model("alpha"),
                         {"key": "alpha", "state": "off", "why": None, "load": None})
        st = m.pipeline_status()
        self.assertEqual((st["models"], st["up"], st["state"]), (["beta"], ["beta"], "holding"))
        self.assertFalse(_free(path), "an unload let the card go")
        self.assertEqual(m.unload_model("gamma")["state"], "off")    # not up: off at once
        with m.in_use("beta"):
            with self.assertRaises(PipelineRefused) as caught:
                m.unload_model("beta")
        self.assertEqual((caught.exception.status, caught.exception.reason), (409, "in_use"))
        self.assertEqual(self.up(m), ["beta"])
        self.made[1].stop_fails = "the process would not die"
        with self.assertRaises(PipelineRefused) as caught:
            m.unload_model("beta")
        self.assertEqual(caught.exception.reason, "unload_failed")
        self.assertIn("would not die", str(caught.exception))
        with self.assertRaises(KeyError):
            m.unload_model("delta")

    def test_a_pinned_load_unloaded_before_it_is_up_never_goes_up(self):
        gate, started = threading.Event(), threading.Event()
        self.addCleanup(gate.set)

        class Slow(FakeBackend):
            given_up = None

            def start(self_):
                started.set()
                gate.wait(5)
                if self_.given_up:
                    raise BackendError(self_.given_up)
                super().start()

            def abandon(self_, reason):
                self_.given_up = reason
                gate.set()
        m = self.side(backend_factory=lambda key, port: Slow(key), card=RecordingCard())
        m.open_pipeline(W, keep_s=0, wait_s=5)
        m.load_model(W, "alpha")
        self.assertTrue(started.wait(5), "alpha's load never began")
        self.assertEqual(self.quick("an unload under a load", lambda: m.unload_model("alpha"),
                                    bound=3.0)["state"], "off")
        time.sleep(0.2)
        self.assertEqual((self.up(m), m.pipeline_status()["models"]), ([], []))

    def test_a_pinned_load_waiting_its_turn_ends_when_its_model_is_unloaded(self):
        """A pinned load queued behind another model's load - no entry yet,
        nothing to give up - ends by itself once its model is unloaded: it
        never goes up after the load before it."""
        gate, started = threading.Event(), threading.Event()
        self.addCleanup(gate.set)

        def on_start(b):
            if b.model_key == "beta":
                started.set()
                gate.wait(5)
        m = self.side(backend_factory=self.factory(on_start=on_start), card=RecordingCard())
        m.open_pipeline(W, keep_s=0, wait_s=5)
        m.load_model(W, "beta")
        self.assertTrue(started.wait(5), "beta's load never began")
        m.load_model(W, "alpha")                            # waits for beta's load
        time.sleep(0.2)
        self.assertEqual(m.unload_model("alpha")["state"], "off")
        gate.set()
        self.wait_for("beta up", lambda: "beta" in self.up(m))
        time.sleep(0.3)
        self.assertEqual((self.up(m), m.pipeline_status()["models"]), (["beta"], ["beta"]))

    def test_an_unload_gives_up_a_jobs_load_of_that_model(self):
        """A model a job is loading on demand is given up when the
        application unloads it - pinned or not, a load under way ends."""
        started = threading.Event()

        class Slow(FakeBackend):
            given_up = None

            def start(self_):
                started.set()
                deadline = time.monotonic() + 5
                while self_.given_up is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                if self_.given_up:
                    raise BackendError(self_.given_up)
                super().start()

            def abandon(self_, reason):
                self_.given_up = reason
        m = self.side(backend_factory=lambda key, port: Slow(key))
        box = {}

        def job():
            try:
                box["got"] = m.acquire("alpha", W)
            except Exception as exc:                        # noqa: BLE001 - the row reads it
                box["got"] = exc
        t = threading.Thread(target=job, daemon=True)
        t.start()
        self.assertTrue(started.wait(5))
        t0 = time.monotonic()
        self.assertEqual(m.unload_model("alpha")["state"], "off")
        t.join(3)
        self.assertLess(time.monotonic() - t0, 2.0, "the unload waited out the load")
        self.assertIn("alpha was unloaded", str(box.get("got")))
        self.assertEqual(self.up(m), [])

    # ------------------------------------------ every load, every way off

    def test_a_rebind_carries_every_load(self):
        m = self.side()
        a = m.acquire("alpha", W)
        b = m.acquire("beta", W)
        m.rebind(self.tmp / "other")
        old = self.events(self.tmp / "bound" / M.EVENTS_NAME)
        new = self.events(self.tmp / "other" / M.EVENTS_NAME)
        self.assertEqual([e["load"] for e in old if e["event"] == "rebound_away"],
                         [a.load_id, b.load_id])
        self.assertEqual([e["load"] for e in new if e["event"] == "load_carried"],
                         [a.load_id, b.load_id])
        m.unload()
        # a later server in each folder reads what this one left - a record
        # read before a later session_start has no orphans at all
        self.manager(folder="bound")
        self.manager(folder="other")
        self.assertEqual(orphaned_loads(self.events(self.tmp / "bound" / M.EVENTS_NAME)), [],
                         "a load carried away reads as an orphan where it was")
        self.assertEqual(orphaned_loads(self.events(self.tmp / "other" / M.EVENTS_NAME)), [])

    def test_a_rebind_carries_a_load_whose_stop_failed(self):
        """A load whose stop failed may still be on the card: a rebind carries
        it as it carries every load, so a server that dies with it still
        there leaves its orphan in the folder it was bound to - where the
        next start's check reads the record - never in the one it left."""
        m = self.side(backend_factory=self.factory(stop_fails="the process would not die"))
        a = m.acquire("alpha", W)
        with self.assertRaises(RuntimeError):
            m.unload("alpha")
        m.rebind(self.tmp / "other")                # ...and is killed here, alpha perhaps up
        self.manager(folder="bound")
        self.manager(folder="other")
        old = self.events(self.tmp / "bound" / M.EVENTS_NAME)
        new = self.events(self.tmp / "other" / M.EVENTS_NAME)
        self.assertEqual([e["load"] for e in new if e["event"] == "load_carried"], [a.load_id])
        self.assertEqual(orphaned_loads(old), [], "the folder it left calls it an orphan")
        self.assertEqual([o["load"] for o in orphaned_loads(new)], [a.load_id])

    def test_letting_the_folder_go_leaves_a_load_whose_stop_failed_open_there(self):
        """...but letting the folder go - a rebind to no folder, the last thing
        a server does as it stops, once its models are off - leaves such a
        load open in the folder it was in: that record is the only one the
        next start on the folder can find it by, which is why a server stops
        its models while its folder is still bound. Found by the controls of
        a sweep: a rebind that carried it wrote it away from the only record
        that knew it (test_adapter's row on a failed unload at the stop)."""
        m = self.side(backend_factory=self.factory(stop_fails="the process would not die"))
        a = m.acquire("alpha", W)
        with self.assertRaises(RuntimeError):
            m.unload("alpha")
        m.rebind(None)
        self.manager(folder="bound")                # the next start on the folder
        old = self.events(self.tmp / "bound" / M.EVENTS_NAME)
        self.assertNotIn("rebound_away", [e["event"] for e in old if e.get("load") == a.load_id])
        self.assertEqual([o["load"] for o in orphaned_loads(old)], [a.load_id],
                         "the next start on the folder would not find it")

    def test_a_card_taken_back_takes_every_load_off(self):
        card = RecordingCard()
        m = self.side(card=card)
        a = m.acquire("alpha", W)
        b = m.acquire("beta", W)
        self.assertEqual((self.up(m), a.stops), (["alpha", "beta"], 0))
        card.lost = "reclaimed"
        m._card_lost("reclaimed", "no heartbeat for 20s")
        self.wait_for("every model off", lambda: self.up(m) == [])
        self.assertEqual((a.stops, b.stops), (1, 1))

    def test_shutdown_takes_every_load_off_and_gives_the_card_back(self):
        path = self.tmp / "card.lock"
        m = self.side(card=MachineCard(path))
        a = m.acquire("alpha", W)
        b = m.acquire("beta", W)
        self.assertEqual((self.up(m), a.stops, _free(path)), (["alpha", "beta"], 0, False))
        m.shutdown()
        self.assertEqual((a.stops, b.stops, self.up(m), _free(path)), (1, 1, [], True))

    def test_stopping_ends_a_wait_for_the_load_lock_at_once(self):
        """A load under way that cannot be given up holds the load lock; the
        server stopping ends every wait for it at once, in the stop's words -
        never after the load. (Giving a load up is `NativeAbandonRows`'.)"""
        gate, started = threading.Event(), threading.Event()
        self.addCleanup(gate.set)

        class Stubborn(FakeBackend):
            def start(self_):
                started.set()
                gate.wait(5)
                super().start()
        m = self.side(backend_factory=lambda key, port: Stubborn(key))
        got = {}

        def load(key):
            try:
                got[key] = m.acquire(key, W)
            except Exception as exc:                        # noqa: BLE001 - the row reads it
                got[key] = exc
        first = threading.Thread(target=load, args=("alpha",), daemon=True)
        first.start()
        self.assertTrue(started.wait(5))
        second = threading.Thread(target=load, args=("beta",), daemon=True)
        second.start()
        time.sleep(0.2)                                     # beta waits for the load lock
        t0 = time.monotonic()
        m.stopping("the server is stopping")
        second.join(3)
        self.assertLess(time.monotonic() - t0, 1.0, "the wait outlasted the stop")
        self.assertTrue(first.is_alive(), "alpha's load ended before the row released it")
        self.assertEqual(str(got["beta"]), "no model is loaded now: the server is stopping")
        gate.set()
        first.join(5)

    def test_a_close_asks_a_failed_stop_again(self):
        """S6 R8: once a model's stop failed, only the next load asked it
        again - a close could never succeed until the server stopped. The
        close asks it again too, and closes once it goes."""
        path = self.tmp / "card.lock"
        m = self.side(card=MachineCard(path),
                      backend_factory=self.factory(stop_fails="the process would not die"))
        opened = m.open_pipeline(W, models=["alpha"], keep_s=0, wait_s=5)
        with self.assertRaises(RuntimeError):
            m.unload("alpha")
        with self.assertRaises(PipelineRefused) as caught:
            m.close_pipeline(opened["id"])                  # it still will not stop
        self.assertEqual(caught.exception.reason, "unload_failed")
        self.assertFalse(_free(path))
        self.made[0].stop_fails = None
        closed = m.close_pipeline(opened["id"])
        self.assertEqual((closed["state"], _free(path)), ("closed", True))
        again = [e["event"] for e in self.events(m) if e.get("again")]
        self.assertEqual(again, ["unload_start", "unload_failed", "unload_start", "unload_ok"])

    # ------------------------------------------------ what is kept, and said

    def test_the_load_history_is_written_whole_by_loads_side_by_side(self):
        """Two loads beside each other fold into one record: under one lock,
        through temporary files of their own - a fixed name read, edited and
        written back by two at once lost samples, or failed a move."""
        path = self.tmp / "hist" / M.LOADS_NAME
        errors = []

        def fold(key):
            for _ in range(40):
                try:
                    note_load(path, key, False, 1.5)
                except Exception as exc:                    # noqa: BLE001 - the row reads it
                    errors.append(exc)
        folds = [threading.Thread(target=fold, args=(k,), daemon=True) for k in ("alpha", "beta")]
        for t in folds:
            t.start()
        for t in folds:
            t.join(20)
        kept = read_loads(path)
        self.assertEqual((errors, kept["alpha::warm"]["n"], kept["beta::warm"]["n"]),
                         ([], 40, 40))
        self.assertEqual(sorted(x.name for x in path.parent.iterdir()), [M.LOADS_NAME],
                         "a temporary file was left behind")

    def test_a_leftover_temporary_file_never_blocks_the_record(self):
        """A temporary name of its own for each write: a writer that died
        left `model_loads.json.tmp` behind - here, even a folder of that
        name - and a fixed name would fail every load's record from then on.
        And a write that fails leaves no temporary file of its own behind."""
        path = self.tmp / "left" / M.LOADS_NAME
        (path.parent / (M.LOADS_NAME + ".tmp")).mkdir(parents=True)
        note_load(path, "alpha", True, 2.0)
        self.assertEqual(read_loads(path)["alpha::cold"]["n"], 1)
        blocked = self.tmp / "blocked" / M.LOADS_NAME
        blocked.mkdir(parents=True)                         # the record itself cannot be replaced
        with self.assertRaises(OSError):
            note_load(blocked, "alpha", True, 2.0)
        self.assertEqual([x.name for x in blocked.parent.iterdir()], [M.LOADS_NAME],
                         "a failed write left its temporary file behind")

    def test_healths_holding_comes_from_the_pipeline(self):
        """With no coordinator, `card.holding` is the pipeline's - not "a job
        is running": a model up while the queue is idle still holds the card,
        and a pipeline with nothing loaded does too."""
        m = self.side()
        self.assertEqual(m.card_status(), {"coordinator": "none", "holding": False,
                                           "waiting_since": None})
        m.acquire("alpha", W)                       # the queue's own, its model warm
        self.assertTrue(m.card_status()["holding"])
        m.unload()
        self.assertFalse(m.card_status()["holding"], "a pipeline closed, and still holding")
        opened = m.open_pipeline(W, keep_s=0, wait_s=5)
        self.assertTrue(m.card_status()["holding"])
        m.close_pipeline(opened["id"])
        self.assertFalse(m.card_status()["holding"])

    def test_free_port_skips_a_port_something_listens_on(self):
        """H1 R2, decided: the runner's own range, bind-checked - a port
        something listens on is never handed out."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
            held.bind(("127.0.0.1", M.free_port()))            # the runner's own range
            held.listen(1)
            port = held.getsockname()[1]
            with self.assertRaises(OSError) as caught:
                M.free_port(ports=(port, port))
            self.assertIn(f"no free port in {port}-{port}", str(caught.exception))
        self.assertEqual(M.free_port(ports=(port, port)), port)


# ================================================== the watch (PLAN-4 D3) ===

#: a line of llama-server's log that says the card's memory ran out - b10724's
#: own words (ggml-cuda.cu, ggml_backend_cuda_buffer_type_alloc_buffer)
OOM = ("ggml_backend_cuda_buffer_type_alloc_buffer: allocating 2304.00 MiB on device 0: "
       "cudaMalloc failed: out of memory")


class Answering(FakeBackend):
    """A backend that answers: `said` tokens, then its end - each token held
    until the row lets it go (`gate`, when set), a cancel honoured between
    tokens. `deadline` is what it declares for an answer in one piece; `log`
    the memory errors its log says, handed out once each."""

    def __init__(self, key, said=("a", "b", "c"), reasoning=(), deadline=None, log=(),
                 breaks=None, **kw):
        super().__init__(key, **kw)
        self.said, self.reasoning, self.deadline = list(said), list(reasoning), deadline
        self.log, self.breaks = list(log), breaks
        self.gate: threading.Event | None = None
        self.streaming = threading.Event()

    def answer_deadline_s(self, messages, params):
        return self.deadline

    def memory_errors(self):
        out, self.log = self.log, []
        return out

    def stream(self, messages, params, cancel=None):
        self.streaming.set()
        for kind, text in [("reasoning", r) for r in self.reasoning] + \
                [("token", t) for t in self.said]:
            if self.gate is not None:
                # held - at most ten seconds: a row that never lets it go
                # fails, never hangs the suite
                held_until = time.monotonic() + 10
                while not self.gate.wait(0.02):
                    if cancel is not None and cancel.is_set():
                        raise Cancelled("cancelled during generation")
                    if time.monotonic() > held_until:
                        raise BackendError("the row never let the token go")
            if cancel is not None and cancel.is_set():
                raise Cancelled("cancelled during generation")
            yield {"type": kind, "text": text}
        if self.breaks:
            raise BackendError(self.breaks)
        yield {"type": "done", "finish_reason": "stop", "usage": {}, "timings": {}}


class Watched(RecordingCard):
    """A card source a coordinator's watch stands behind: the runner hands it
    the work to say on each heartbeat (`work`), and it tells the runner when
    the card is degraded (`on_degraded`) - and a heartbeat asked for at once
    is counted (`beat_now`)."""

    def __init__(self):
        super().__init__()
        self.work = self.on_degraded = None
        self.beats = 0

    def beat_now(self):
        self.beats += 1


UNDER = {"reason": "under_floor", "why": "the card has 176 MiB free of 24576, under the floor of "
         "256 MiB a rental keeps free: nothing new goes on it until it has room again",
         "since": 1790172090.0, "free_mib": 176, "floor_mib": 256, "total_mib": 24576,
         "error": None}


class NativeWatchRows(Case):
    """PLAN-4 D3, the runner's side of the coordinator's watch. The MANAGER
    counts the work where it really happens - each token streamed and each
    answer ended, which only real work moves - and says, on each heartbeat
    its card source makes, whether a model is answering, that count, a
    one-piece answer's deadline (the backend's seam), and the memory errors
    its models' logs said. When the card source hears the card is DEGRADED,
    the answer under way ends and the next is refused, with a reason of its
    own, until the card has room again."""

    def answering(self, **kw):
        self.made_b = []

        def make(key, port):
            b = Answering(key, **kw)
            self.made_b.append(b)
            return b
        return make

    def answer(self, m, key="alpha", cancel=None):
        b = m.acquire(key, W)
        return [ev["type"] for ev in m.answer(b, "hello", GenParams(), cancel)]

    def wait_for(self, what, pred, timeout=5.0):
        deadline = time.monotonic() + timeout
        while not pred():
            self.assertLess(time.monotonic(), deadline, f"timed out waiting for {what}")
            time.sleep(0.01)

    def test_a_token_and_an_answers_end_move_the_progress_and_nothing_else_does(self):
        m = self.manager(backend_factory=self.answering(reasoning=("hm",)))
        # CHANGED ON PURPOSE (S11b): the work says its slow answers too (D3, D12)
        self.assertEqual(m.work(), {"working": False, "progress": 0, "deadline_s": None,
                                    "memory_errors": 0, "memory_error": None,
                                    "slow_answers": 0, "slow_answer": None})
        got = self.answer(m)
        self.assertEqual(got, ["reasoning", "token", "token", "token", "done"],
                         "the answer is the backend's stream, as it came")
        self.assertEqual(m.work()["progress"], 5, "a piece of reasoning, three tokens, an end")
        m.acquire("beta", W)
        m.unload("alpha")
        m.touch()
        self.assertEqual(m.work()["progress"], 5, "a load, an unload, a touch moved it")
        self.answer(m, "beta")
        self.assertEqual(m.work()["progress"], 10)

    def test_working_while_an_answer_streams_and_only_then(self):
        m = self.manager(backend_factory=self.answering())
        b = m.acquire("alpha", W)
        b.gate = threading.Event()
        box = {}
        t = threading.Thread(target=lambda: box.update(got=list(m.answer(b, "hi", GenParams()))),
                             daemon=True)
        t.start()
        self.wait_for("the answer to stream", b.streaming.is_set)
        self.assertTrue(m.work()["working"], "an answer streaming is work")
        self.assertEqual(m.work()["progress"], 0, "nothing has moved yet")
        b.gate.set()
        t.join(5)
        self.assertEqual(len(box["got"]), 4)
        self.assertEqual((m.work()["working"], m.work()["progress"]), (False, 4))

    def test_an_answer_in_one_piece_declares_its_deadline_by_its_backends_seam(self):
        self.assertIsNone(Backend("x").answer_deadline_s("hello", GenParams()),
                          "a backend declares none unless it says: its answers stream")
        self.assertEqual(Backend("x").memory_errors(), [],
                         "and its log says nothing unless it reads one")
        m = self.manager(backend_factory=self.answering(deadline=90.0))
        b = m.acquire("alpha", W)
        b.gate = threading.Event()
        t = threading.Thread(target=lambda: list(m.answer(b, "hi", GenParams())), daemon=True)
        t.start()
        self.wait_for("the answer to stream", b.streaming.is_set)
        due = m.work()["deadline_s"]
        self.assertTrue(due is not None and 88.0 < due <= 90.0, due)
        time.sleep(0.3)
        self.assertLess(m.work()["deadline_s"], due, "the deadline is said from now")
        b.gate.set()
        t.join(5)
        self.assertIsNone(m.work()["deadline_s"], "an answer that ended declares nothing")

    def test_the_card_source_is_handed_the_work_to_say(self):
        card = Watched()
        m = self.manager(card=card, backend_factory=self.answering())
        self.assertEqual(card.work(), m.work())
        self.answer(m)
        self.assertEqual(card.work()["progress"], 4, "its card source reads the count")
        m2 = self.manager(folder="other", backend_factory=self.answering())
        self.assertFalse(hasattr(m2._card, "work"), "a source that says nothing is told nothing")

    def test_a_memory_error_a_models_log_said_is_counted_told_and_recorded(self):
        card = Watched()
        m = self.manager(card=card, backend_factory=self.answering(log=[OOM]))
        self.answer(m)
        said = m.work()
        self.assertEqual((said["memory_errors"], said["memory_error"]), (1, OOM))
        self.assertGreaterEqual(card.beats, 1, "said to the coordinator at once, not a beat later")
        lines = [e for e in self.events(m) if e["event"] == "memory_error"]
        self.assertEqual([(e["model_key"], e["line"]) for e in lines], [("alpha", OOM)])
        self.assertEqual(lines[0]["load"], self.made_b[0].load_id)
        self.answer(m)
        self.assertEqual(m.work()["memory_errors"], 1, "a line is counted once")

    def test_a_load_that_failed_has_its_log_read_too(self):
        m = self.manager(backend_factory=self.answering(log=[OOM], fail="the load failed"))
        with self.assertRaises(BackendError):
            m.acquire("alpha", W)
        self.assertEqual((m.work()["memory_errors"], m.work()["memory_error"]), (1, OOM))

    def test_an_answer_that_broke_while_its_log_said_the_memory_ran_out_is_the_card_degraded(self):
        m = self.manager(backend_factory=self.answering(log=[OOM], breaks="the stream broke"))
        with self.assertRaises(M.Degraded) as caught:
            self.answer(m)
        self.assertEqual(caught.exception.reason, "card_degraded")
        self.assertIn("the stream broke", str(caught.exception))
        self.assertIn(OOM, str(caught.exception))
        m2 = self.manager(folder="plain", backend_factory=self.answering(breaks="it broke"))
        with self.assertRaises(BackendError) as caught:
            self.answer(m2)
        self.assertNotIsInstance(caught.exception, M.Degraded,
                                 "an answer that broke with nothing in its log is no memory error")

    def degraded(self, **kw):
        card = Watched()
        m = self.manager(card=card, backend_factory=self.answering(**kw))
        return card, m

    def test_the_card_degraded_ends_the_answer_under_way_and_refuses_the_next(self):
        card, m = self.degraded()
        opened = m.open_pipeline(W, keep_s=0, wait_s=5)
        self.assertEqual(opened["state"], "holding")
        b = m.acquire("alpha", W)
        b.gate = threading.Event()
        box = {}

        def run():
            try:
                list(m.answer(b, "hi", GenParams()))
            except Exception as exc:                        # noqa: BLE001 - read below
                box["exc"] = exc
        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.wait_for("the answer to stream", b.streaming.is_set)
        card.on_degraded(dict(UNDER))
        t.join(5)
        b.gate = None                                       # its next answer is not held
        self.assertIsInstance(box.get("exc"), M.Degraded, box)
        self.assertEqual(box["exc"].reason, "card_degraded")
        self.assertIn(UNDER["why"], str(box["exc"]))
        with self.assertRaises(M.Degraded) as caught:
            m.acquire("alpha", W)
        self.assertIn(UNDER["why"], str(caught.exception))
        with self.assertRaises(M.Degraded):
            m.acquire("beta", W)
        self.assertEqual([x.starts for x in self.made_b], [1], "nothing new was loaded")
        st = m.pipeline_status()
        self.assertEqual(st["state"], "degraded")
        self.assertIn(UNDER["why"], st["why"])
        self.assertEqual(m.card_status()["holding"], True, "and the card is still held")
        card.on_degraded(None)
        self.assertEqual(m.pipeline_status()["state"], "holding")
        self.assertIsNone(m.pipeline_status()["why"])
        self.assertEqual(self.answer(m), ["token", "token", "token", "done"],
                         "work goes on once the card has room again")
        self.wait_for("both on the record", lambda: {"card_degraded", "card_restored"}
                      <= set(self.kinds(m)))
        said = [e for e in self.events(m) if e["event"] == "card_degraded"]
        self.assertEqual(said[0]["why"], UNDER["why"])
        m.close_pipeline(opened["id"])

    def test_a_load_under_way_when_the_card_is_degraded_is_given_up(self):
        cfg = self.config(SETTINGS.replace("load_seconds = 0", "load_seconds = 30"))
        card = Watched()
        m = self.manager(cfg, card=card, fake=False)
        box = {}

        def load():
            try:
                m.acquire("alpha", W)
            except Exception as exc:                        # noqa: BLE001 - read below
                box["exc"] = exc
        t = threading.Thread(target=load, daemon=True)
        t.start()
        self.wait_for("the load to begin", lambda: m.status()["loading"] == "alpha")
        started = time.monotonic()
        card.on_degraded(dict(UNDER))
        t.join(10)
        self.assertLess(time.monotonic() - started, 5, "a load nobody can use was waited out")
        self.assertIsInstance(box.get("exc"), BackendError, box)
        self.assertIn("degraded", str(box["exc"]))
        self.assertEqual(m.status()["loaded"], None)

    def test_a_card_degraded_while_a_load_made_room_refuses_the_load(self):
        """The load's own look as it begins: degraded while its fit check was
        asked, nothing is made for it."""
        class Midway(Watched):
            def fit(self, model, engine, need_mib, **kw):
                self.on_degraded(dict(UNDER))
                return _verdict("fits")
        card = Midway()
        m = self.manager(card=card, backend_factory=self.answering())
        with self.assertRaises(M.Degraded) as caught:
            m.acquire("alpha", W)
        self.assertIn("nothing new is loaded now", str(caught.exception))
        self.assertEqual(self.made_b, [], "a backend was made for a load on a degraded card")

    def test_the_queues_own_pipeline_on_a_degraded_card_still_lets_the_card_go(self):
        """A degraded card is still a card held: the queue's own pipeline,
        its work done and the card wanted, closes and gives it back, as it
        would holding it."""
        card, m = self.degraded()
        m.acquire("alpha", W)                               # the queue's own, its model warm
        card.on_degraded(dict(UNDER))
        self.assertEqual(m.pipeline_status()["state"], "degraded")
        card.wanted = True
        self.assertTrue(m.settle(), "a degraded pipeline kept a card that is wanted")
        self.assertEqual(m.pipeline_status()["state"], "closed")
        self.assertEqual(card.gave, ["t-0001-abcd"])

    def test_a_new_rental_is_not_the_degraded_one(self):
        card, m = self.degraded()
        m.acquire("alpha", W)
        card.on_degraded(dict(UNDER))
        self.assertIsNotNone(m.degraded)
        card.turn = {"id": "t-0099-zzzz"}                   # granted again: a new rental
        self.assertIsNone(m.degraded, "a new rental carried the last one's degraded")
        self.assertEqual(self.answer(m), ["token", "token", "token", "done"])

    def test_a_refusal_for_memory_has_a_reason_of_its_own(self):
        for verdict in ("no_room", "never_fits"):
            with self.subTest(verdict=verdict):
                m = self.manager(card=Deciding(_verdict(verdict)), folder=verdict)
                with self.assertRaises(BackendError) as caught:
                    m.acquire("alpha", W)
                self.assertEqual(getattr(caught.exception, "reason", None), verdict)
        card = Deciding(_verdict("fits"))
        m = self.manager(card=card, folder="degraded-amend")
        m.acquire("alpha", W)
        card.verdict = _verdict("degraded", words="alpha may not go on the card now: degraded")
        with self.assertRaises(M.Degraded) as caught:
            m.acquire("beta", W)
        self.assertEqual(caught.exception.reason, "card_degraded")
        self.assertIn("may not go on the card now", str(caught.exception))
        self.assertEqual(m.status()["loaded"], "alpha",
                         "no model is taken off to make room the card does not have")

    def test_no_room_now_fails_at_once_and_the_log_says_it_on_every_path_a_model_is_taken(self):
        """H9, the owner's ruling of 2026-09-28: "Fail loudly. Memory almost
        never ever cleans itself up." A model the card source finds no room
        for on a card that is not over-full fails AT ONCE - never waits for
        room - and the server's log says it, in the card source's words, on
        every path a model is taken: a job's model on demand, a load of one
        (`/load`), a pipeline's open, and the models a session starts ready
        with. The model's failed state and the pipeline's why say the same."""
        loud = ("not enough free VRAM for beta: it needs 5000 MiB; programs outside the "
                "bootstrap hold 12000 MiB of it. Close programs that use the card, or reboot")
        for path in ("job", "load", "open", "start"):
            with self.subTest(path=path):
                card = Deciding(_verdict("no_room", words=loud))
                m = self.manager(card=card, folder=f"loud-{path}")
                said = []
                m.on_say = said.append
                began = time.monotonic()
                if path == "job":
                    with self.assertRaises(BackendError) as caught:
                        m.acquire("beta", W)
                    self.assertIn(loud, str(caught.exception))
                    self.assertEqual(caught.exception.reason, "no_room")
                elif path == "load":
                    m.open_pipeline(W, keep_s=0, wait_s=5.0)
                    got = m.load_model(W, "beta", wait_s=5.0)
                    self.assertEqual(got["model"]["state"], "failed", str(got))
                    self.assertIn(loud, got["model"]["why"])
                    self.assertIn(loud, got["pipeline"]["why"] or "")
                else:
                    got = m.open_pipeline(W, models=["beta"], keep_s=0, wait_s=5.0,
                                          opened_by="start" if path == "start" else "application")
                    self.assertEqual(m.model_state("beta")["state"], "failed", str(got))
                    self.assertIn(loud, m.model_state("beta")["why"])
                    self.assertIn(loud, got["why"] or "")
                self.assertLess(time.monotonic() - began, 3.0, "it waited for room")
                self.assertEqual(len(card.asked), 1, "the card source was asked again: a wait")
                lines = [line for line in said if loud in line]
                self.assertEqual(len(lines), 1, f"the log: {said}")
                self.assertIn("beta", lines[0])


class Recovering(Watched):
    """A card source that says when the card has room again (S5b), as a
    coordinator's rental does: work is held for the card's memory on it."""
    recovers = True


#: the card full, as a coordinator says it (S5b): the room it waits for, and
#: what programs outside the bootstrap use
FULL = dict(UNDER, need_mib=6554, outside_mib=10035)
FULL_WORDS = ("The card is full: 0.2 GB free, 6.4 GB needed. 9.8 GB is used by programs "
              "outside the bootstrap. Close something, and this runs again by itself.")


class NativeCardFullRows(Case):
    """S5b, the runner's side: DEGRADED MEANS WAITING, NOT FAILING. A job's
    acquire on a full card - on a card source that says when it has room
    again - is held, within the job's patience, saying the card's numbers as
    it waits, and goes on by itself once the card has room: its model loaded
    again first if it came off. Patience 0 is today's rule, at once; a
    patience run out fails, saying how long and what the card read."""

    def held(self, m, terms, key="alpha"):
        """An acquire held for the card's memory, on a thread: -> (thread,
        box, heard) once it has said it waits."""
        box, heard = {}, []

        def acquire():
            try:
                box["got"] = m.acquire(key, W, terms=terms, on_full=heard.append)
            except Exception as exc:                        # noqa: BLE001 - read below
                box["got"] = exc
        t = threading.Thread(target=acquire, daemon=True)
        t.start()
        deadline = time.monotonic() + 5
        while not heard and t.is_alive():
            self.assertLess(time.monotonic(), deadline, "the acquire never said it waits")
            time.sleep(0.01)
        return t, box, heard

    def full_card(self, **kw):
        card = Recovering()
        m = self.manager(card=card, backend_factory=NativeWatchRows.answering(self, **kw))
        m.acquire("alpha", W)
        card.on_degraded(dict(FULL))
        return card, m

    def test_a_jobs_acquire_on_a_full_card_is_held_and_goes_on_once_it_has_room(self):
        card, m = self.full_card()
        self.assertEqual(m.pipeline_status()["why"],
                         f"{FULL_WORDS} (the card is degraded: {UNDER['why']})",
                         "the session does not say what a person does")
        t, box, heard = self.held(m, Terms(patience_s=10.0))
        self.assertEqual(heard[0], {"words": FULL_WORDS, "free_mib": 176, "need_mib": 6554,
                                    "outside_mib": 10035, "waited_s": 0,
                                    "patience_left_s": 10})
        time.sleep(0.3)
        self.assertTrue(t.is_alive(), f"it did not wait for the card: {box}")
        card.on_degraded(None)
        t.join(5)
        self.assertIsInstance(box.get("got"), Answering, box)
        waited = [e for e in self.events(m) if e["event"] == "card_full_waited"]
        self.assertEqual([e["model_key"] for e in waited], ["alpha"])
        self.assertGreaterEqual(waited[0]["waited_s"], 0.3)

    def test_patience_0_fails_at_once_as_today_and_one_that_ran_out_says_how_long_and_what(self):
        card, m = self.full_card()
        started = time.monotonic()
        with self.assertRaises(M.Degraded) as caught:
            m.acquire("alpha", W, terms=Terms(patience_s=0), on_full=lambda said: None)
        self.assertLess(time.monotonic() - started, 0.2, "patience 0 waited")
        self.assertTrue(caught.exception.final)
        self.assertEqual(caught.exception.reason, "card_degraded")
        started = time.monotonic()
        with self.assertRaises(M.Degraded) as caught:
            m.acquire("alpha", W, terms=Terms(patience_s=0.4), on_full=lambda said: None)
        self.assertGreaterEqual(time.monotonic() - started, 0.4)
        self.assertTrue(caught.exception.final)
        self.assertIn("the card stayed full for the whole of this request's patience of 0.4s: "
                      "it read 176 MiB free at the end, and 6554 MiB was needed",
                      str(caught.exception))
        self.assertEqual((caught.exception.card["free_mib"], caught.exception.card["need_mib"]),
                         (176, 6554))
        started = time.monotonic()
        with self.assertRaises(M.Degraded) as caught:
            m.acquire("alpha", W, terms=Terms(patience_s=30, waited_s=30),
                      on_full=lambda said: None)
        self.assertLess(time.monotonic() - started, 0.2,
                        "its patience counts from the first time the card broke it")

    def test_without_on_full_or_on_a_source_that_never_says_room_again_it_is_refused_at_once(self):
        card, m = self.full_card()
        with self.assertRaises(M.Degraded) as caught:
            m.acquire("alpha", W, terms=Terms(patience_s=10))
        self.assertFalse(caught.exception.final, "refused as today: not held, so not final")
        card = Watched()
        m = self.manager(card=card, folder="never", backend_factory=self.factory())
        m.acquire("alpha", W)
        card.on_degraded(dict(FULL))
        heard = []
        started = time.monotonic()
        with self.assertRaises(M.Degraded):
            m.acquire("alpha", W, terms=Terms(patience_s=10), on_full=heard.append)
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(heard, [], "held on a source that never says the card has room")
        self.assertFalse(m.holds_while_full(Terms(patience_s=10)))

    def test_the_same_word_with_newer_numbers_refreshes_what_it_says_and_ends_nothing(self):
        card, m = self.full_card()
        t, box, heard = self.held(m, Terms(patience_s=10.0))
        ends = m._degraded_now
        card.on_degraded(dict(FULL, free_mib=2048, outside_mib=9000))
        deadline = time.monotonic() + 7
        while not any(h["free_mib"] == 2048 for h in heard):
            self.assertLess(time.monotonic(), deadline, "the newer numbers were never said")
            time.sleep(0.02)
        said = next(h for h in heard if h["free_mib"] == 2048)
        self.assertIn("2.0 GB free", said["words"])
        self.assertEqual(m.pipeline_status()["why"], f"{said['words']} (the card is degraded: "
                                                     f"{UNDER['why']})",
                         "the session does not say what a person does, in the newest numbers")
        self.assertIs(m._degraded_now, ends, "the same word again ended the answers again")
        self.assertEqual(self.kinds(m).count("card_degraded"), 1,
                         "a line of the record for a newer reading")
        card.on_degraded(None)
        t.join(5)
        self.assertIsInstance(box.get("got"), Answering, box)

    def test_room_again_loads_again_first_the_pinned_model_that_came_off(self):
        card = Recovering()
        m = self.manager(card=card, backend_factory=NativeWatchRows.answering(self))
        opened = m.open_pipeline(W, models=["alpha", "beta"], keep_s=0, wait_s=5)
        self.assertEqual(sorted(opened["up"]), ["alpha", "beta"])
        alpha = next(b for b in self.made_b if b.model_key == "alpha")
        card.on_degraded(dict(FULL))
        alpha.alive = False                                 # its process died of it
        card.on_degraded(None)
        deadline = time.monotonic() + 5
        while sum(1 for b in self.made_b if b.model_key == "alpha") < 2 \
                or m.model_state("alpha")["state"] != "up":
            self.assertLess(time.monotonic(), deadline, "the pinned model was not loaded again")
            time.sleep(0.02)
        self.assertEqual(sum(1 for b in self.made_b if b.model_key == "beta"), 1,
                         "a model still up was loaded again")
        back = [e for e in self.events(m) if e["event"] == "card_restored"]
        self.assertEqual(back[-1].get("again"), ["alpha"])
        m.close_pipeline(opened["id"])

    def test_a_memory_error_its_source_has_not_yet_said_holds_the_job_until_it_does(self):
        card = Recovering()
        m = self.manager(card=card, backend_factory=NativeWatchRows.answering(
            self, log=[OOM], breaks="the stream broke"))
        b = m.acquire("alpha", W)
        with self.assertRaises(M.Degraded):
            list(m.answer(b, "hi", GenParams()))
        t, box, heard = self.held(m, Terms(patience_s=10.0))
        self.assertEqual(heard[0]["words"], "The card is full. Close something, and this runs "
                                            "again by itself.")
        card.on_degraded(dict(FULL))
        card.on_degraded(None)
        t.join(5)
        self.assertIsInstance(box.get("got"), Answering, box)

    def test_a_memory_error_no_word_follows_holds_the_job_a_moment_at_most(self):
        card = Recovering()
        m = self.manager(card=card, backend_factory=NativeWatchRows.answering(
            self, log=[OOM], breaks="the stream broke"))
        self.addCleanup(setattr, M, "UNHEARD_S", M.UNHEARD_S)
        M.UNHEARD_S = 0.4
        b = m.acquire("alpha", W)
        with self.assertRaises(M.Degraded):
            list(m.answer(b, "hi", GenParams()))
        started = time.monotonic()
        got = m.acquire("alpha", W, terms=Terms(patience_s=10), on_full=lambda said: None)
        self.assertIsInstance(got, Answering)
        self.assertLess(time.monotonic() - started, 3.0, "held past the moment")


class NativeStandInCardRows(Case):
    """S16a R2: the stand-in could not fill the card, so no application on the
    stand-ins could try its card-full path. `[mock] card_mib` gives it a card
    of its own - read by the stand-in alone, never the real card - and
    `card_full_s` makes it read full for a while, then free again. It is
    watched as a real card is on the machine's lock (S5b), on a lock of its
    own: the same degraded and recovered path, the same words."""

    def settings(self, full, lock="stand-in.lock", **runner):
        own = "".join(f"{k} = {v}\n" for k, v in runner.items())
        return self.config(
            f'[runner]\nbackend = "mock"\ncard_lock = "{(self.tmp / lock).as_posix()}"\n'
            f'card_watch_s = 0.05\ncard_recover_watch_s = 0.05\ncard_patience_s = 30\n{own}'
            f'[mock]\ntokens_per_second = 0\nload_seconds = 0\nunload_seconds = 0\n'
            f'card_mib = 24576\ncard_full_s = {full}\n'
            f'[models.alpha]\nlabel = "Alpha"\n', name=f"stand-in-{lock}.toml")

    def test_it_reads_full_for_its_while_then_free_again_and_never_the_real_card(self):
        from runner.mock import StandInCard
        clock = Clock(1000.0)
        card = StandInCard({"card_mib": 24576, "card_full_s": [5, 10]}, clock=clock)
        seen = []
        for at in (0, 4.9, 5, 14.9, 15, 100):
            clock.t = 1000.0 + at
            seen.append(card.read())
        self.assertEqual([r["free"] for r in seen], [24576, 24576, 0, 0, 24576, 24576])
        self.assertEqual({r["total"] for r in seen}, {24576})
        never = StandInCard({"card_mib": 8192, "card_full_s": []}, clock=clock)
        self.assertEqual(never.read(), {"free": 8192, "total": 8192})

    def test_a_job_on_the_full_stand_in_card_is_held_and_runs_again_by_itself(self):
        """The whole S5b path on the stand-ins: its session's card read full,
        the pipeline DEGRADED with the card-full words; a job's acquire HELD
        for the card's memory, saying so; and once the card reads free again
        the pipeline recovers and the job goes on by itself."""
        m = ModelManager(self.settings("[0.4, 1.2]"), self.tmp / "stand-in-folder",
                         clock=self.clock)
        self.addCleanup(m.shutdown)
        opened = m.open_pipeline(W, models=["alpha"], keep_s=0, wait_s=5.0)
        self.assertEqual(opened["up"], ["alpha"], opened)
        deadline = time.monotonic() + 5
        while m.pipeline_status()["state"] != "degraded":
            self.assertLess(time.monotonic(), deadline, "the stand-in card never read full")
            time.sleep(0.02)
        self.assertIn("The card is full", m.pipeline_status()["why"])
        heard = []
        box = {}

        def acquire():
            try:
                box["got"] = m.acquire("alpha", W, terms=Terms(patience_s=20.0),
                                       on_full=heard.append)
            except Exception as exc:                        # noqa: BLE001 - read below
                box["got"] = exc
        t = threading.Thread(target=acquire, daemon=True)
        t.start()
        deadline = time.monotonic() + 5
        while not heard:
            self.assertLess(time.monotonic(), deadline, f"never held for the card: {box}")
            time.sleep(0.01)
        self.assertIn("The card is full: 0.0 GB free", heard[0]["words"])
        t.join(10)
        self.assertFalse(t.is_alive(), "the job never went on once the card read free again")
        self.assertNotIsInstance(box.get("got"), Exception, box)
        self.assertEqual(m.pipeline_status()["state"], "holding")
        kinds = [e["event"] for e in self.events(m)]
        self.assertIn("card_full_waited", kinds)
        self.assertTrue((self.tmp / "stand-in.lock").exists(), "not on the lock of its own")


class NativeScheduledRows(Case):
    """S18, the owner's second ruling of 2026-09-28, in the runner: scheduled
    work whose model does not fit either waits for room - by the server's
    setting (`scheduled_if_no_room`, `wait` unless it says `fail`) or the
    request's own `if_no_room` - or fails at once, loudly; live work always
    fails at once. A JOB that waits (its terms `parks`) is refused with the
    room it waits on, so its queue keeps it - never while the application's
    own models hold the card: that fails at once, as ever. The same on a
    server run on its own (the owner's ruling of 2026-09-27): the lock's look
    reads the card and fits the model's need, and a scheduled pipeline waits
    for room WITHOUT the machine's lock, so it holds back nobody."""

    NO_ROOM = dict(_verdict("no_room", need=8000, free=4000, words="not enough free VRAM for "
                            "alpha: loud"), outside_mib=20320)

    def stand_in(self, full, patience=30, name="sched", **runner):
        own = "".join(f"{k} = {v}\n" for k, v in runner.items())
        return self.config(
            f'[runner]\nbackend = "mock"\ncard_lock = "{(self.tmp / (name + ".lock")).as_posix()}"'
            f'\ncard_watch_s = 30\ncard_recover_watch_s = 0.05\ncard_patience_s = {patience}\n'
            f'{own}[mock]\ntokens_per_second = 0\nload_seconds = 0\nunload_seconds = 0\n'
            f'card_mib = 24576\ncard_full_s = {full}\nneed_mib = {{ alpha = 8000 }}\n'
            f'[models.alpha]\nlabel = "Alpha"\n[models.beta]\nlabel = "Beta"\n',
            name=f"{name}.toml")

    def test_the_setting_is_wait_unless_it_says_fail_and_nothing_else(self):
        self.assertEqual(self.config().runner.get("scheduled_if_no_room"), "wait")
        cfg = self.config('[runner]\nbackend = "mock"\nscheduled_if_no_room = "fail"\n',
                          name="fail.toml")
        self.assertEqual(cfg.runner["scheduled_if_no_room"], "fail")
        with self.assertRaises(ValueError) as caught:
            self.config('[runner]\nbackend = "mock"\nscheduled_if_no_room = "later"\n',
                        name="later.toml")
        self.assertIn("scheduled_if_no_room", str(caught.exception))

    def test_the_stand_ins_models_need_what_its_settings_say_and_only_on_its_own_card(self):
        m = self.manager(self.stand_in("[]"), fake=False)
        self.assertEqual((m.need("alpha")[0], m.need("beta")[0]), (8000, None))
        for text, said in (('need_mib = { gamma = 10 }\ncard_mib = 24576\n', "gamma"),
                           ('need_mib = { alpha = -1 }\ncard_mib = 24576\n', "alpha"),
                           ('need_mib = { alpha = 10 }\n', "card_mib")):
            with self.subTest(text=text):
                with self.assertRaises(ValueError) as caught:
                    self.config(f'[runner]\nbackend = "mock"\ncard_lock = '
                                f'"{(self.tmp / "x.lock").as_posix()}"\n[mock]\n{text}'
                                f'[models.alpha]\nlabel = "A"\n', name="bad-need.toml")
                self.assertIn(said, str(caught.exception))

    def test_the_stand_ins_card_reads_less_free_by_what_its_models_on_it_need(self):
        m = self.manager(self.stand_in("[]", name="held"), fake=False)
        self.assertEqual(m._vram()["free"], 24576)
        m.open_pipeline(W, models=["alpha", "beta"], keep_s=0, wait_s=5.0)
        self.assertEqual(sorted(m.pipeline_status()["up"]), ["alpha", "beta"])
        self.assertEqual(m._vram()["free"], 24576 - 8000,
                         "a stand-in model on its card holds nothing of it")

    def test_the_runner_says_the_servers_patience_to_its_queue(self):
        from runner.adapter import RunnerBackend
        cfg = self.config('[runner]\nbackend = "mock"\ncard_patience_s = 90\n', name="p90.toml")
        backend = RunnerBackend(self.manager(cfg))
        self.addCleanup(backend.close)
        self.assertEqual(backend.card_patience_s, 90.0)

    def test_a_job_that_waits_is_refused_with_the_room_it_waits_on_saying_nothing_loud(self):
        card = Deciding(dict(self.NO_ROOM))
        m = self.manager(card=card)
        said = []
        m.on_say = said.append
        terms = Terms(klass="scheduled", parks=True)
        with self.assertRaises(BackendError) as caught:
            m.acquire("alpha", W, terms=terms)
        room = getattr(caught.exception, "room", None)
        self.assertIsInstance(room, dict, "the refusal does not say the room it waits on")
        self.assertEqual((room["memory"], room["outside_mib"]),
                         ({"need_mib": 8000, "free_mib": 4000, "margin_mib": 256,
                           "total_mib": 24576}, 20320))
        self.assertIn("starts by itself once it fits", room["words"])
        self.assertEqual(caught.exception.reason, "no_room")
        self.assertEqual(said, [], "waiting for room is not a failure to shout about")
        self.assertEqual(len(card.asked), 1)

    def test_scheduled_work_that_fails_or_live_work_is_refused_at_once_with_no_room_to_wait_on(self):
        for terms in (Terms(klass="scheduled", parks=True, if_no_room="fail"),
                      Terms(klass="responsive", parks=True), None):
            with self.subTest(terms=terms):
                card = Deciding(dict(self.NO_ROOM))
                m = self.manager(card=card, folder=f"f-{id(terms)}")
                with self.assertRaises(BackendError) as caught:
                    m.acquire("alpha", W, **({"terms": terms} if terms else {}))
                self.assertIsNone(getattr(caught.exception, "room", None))
                self.assertEqual(caught.exception.reason, "no_room")

    def test_the_servers_setting_fail_makes_scheduled_work_fail_at_once(self):
        cfg = self.config('[runner]\nbackend = "mock"\nscheduled_if_no_room = "fail"\n'
                          '[models.alpha]\nlabel = "A"\n', name="set-fail.toml")
        m = self.manager(cfg, card=Deciding(dict(self.NO_ROOM)))
        with self.assertRaises(BackendError) as caught:
            m.acquire("alpha", W, terms=Terms(klass="scheduled", parks=True))
        self.assertIsNone(getattr(caught.exception, "room", None))
        self.assertEqual(m.if_no_room(Terms(klass="scheduled")), "fail")
        self.assertEqual(m.if_no_room(Terms(klass="scheduled", if_no_room="wait")), "wait")
        self.assertEqual(m.if_no_room(Terms(klass="responsive", if_no_room="wait")), "fail")

    def test_when_the_applications_own_models_hold_the_card_it_fails_at_once(self):
        card = Deciding(_verdict("fits"))
        m = self.manager(card=card)
        m.open_pipeline(W, models=["beta"], keep_s=0, wait_s=5.0)
        self.assertEqual(m.model_state("beta")["state"], "up")
        card.verdict = dict(self.NO_ROOM)
        with self.assertRaises(BackendError) as caught:
            m.acquire("alpha", W, terms=Terms(klass="scheduled", parks=True))
        self.assertIsNone(getattr(caught.exception, "room", None),
                          "it waits on room its own pinned model holds")
        self.assertEqual(caught.exception.reason, "no_room")

    def test_on_the_lock_the_look_reads_the_card_and_fits_the_models_need(self):
        m = self.manager(self.stand_in("[0, 0.8]", name="look"), fake=False)
        want = {"job": "j-0001", "model": "alpha", "not_before": None, "for": None,
                "if_no_room": None, "waited": True}
        look = m.room_look(want, W)
        self.assertEqual(look["due"], False, look)
        self.assertEqual((look["room"]["memory"]["need_mib"], look["room"]["memory"]["free_mib"]),
                         (8000, 0))
        self.assertIn("starts by itself once it fits", look["room"]["words"])
        deadline = time.monotonic() + 5
        while not m.room_look(want, W)["due"]:
            self.assertLess(time.monotonic(), deadline, "the card never had room again")
            time.sleep(0.05)
        self.assertTrue(m.room_look(dict(want, waited=False), W)["due"])
        self.assertIsNone(m.room_look(None, W))

    def test_a_scheduled_pipeline_on_the_lock_waits_for_room_without_holding_the_lock(self):
        m = self.manager(self.stand_in("[0, 1.5]", name="pipe"), fake=False)
        opened = m.open_pipeline(W, models=["alpha"], klass="scheduled", keep_s=0, wait_s=0.5)
        self.assertEqual(opened["state"], "waiting", opened)
        deadline = time.monotonic() + 5
        while "starts by itself once it fits" not in (m.pipeline_status()["why"] or ""):
            self.assertLess(time.monotonic(), deadline,
                            f"it never said the room it waits on: {m.pipeline_status()}")
            time.sleep(0.02)
        self.assertIn("programs outside the bootstrap hold 24576 MiB",
                      m.pipeline_status()["why"], "what programs outside hold was not said")
        other = MachineCard(self.tmp / "pipe.lock", patience_s=0)
        self.assertIsNone(other.take("srv-a2b3c4d5", "x"), "the lock was held while it waited")
        other.give_back()
        deadline = time.monotonic() + 8
        while m.model_state("alpha")["state"] != "up":
            self.assertLess(time.monotonic(), deadline, "it never went on once the card had room")
            time.sleep(0.05)
        self.assertEqual(m.pipeline_status()["state"], "holding")

    def test_a_scheduled_pipeline_on_the_lock_that_asked_to_fail_fails_at_once_loudly(self):
        m = self.manager(self.stand_in("[0, 30]", name="pipe-fail"), fake=False)
        m.open_pipeline(W, models=["alpha"], klass="scheduled", keep_s=0, wait_s=3.0,
                        if_no_room="fail")
        state = m.model_state("alpha")
        self.assertEqual(state["state"], "failed", state)
        self.assertIn("does not wait for room", state["why"])

    def test_a_pipelines_patience_on_the_lock_that_runs_out_fails_loudly(self):
        m = self.manager(self.stand_in("[0, 30]", patience=1, name="pipe-out"), fake=False)
        m.open_pipeline(W, models=["alpha"], klass="scheduled", keep_s=0)
        deadline = time.monotonic() + 8
        while m.pipeline_status()["state"] != "closed":
            self.assertLess(time.monotonic(), deadline, "its patience never ran out")
            time.sleep(0.05)
        why = m.pipeline_status()["why"]
        for words in ("did not fit on the card within its patience of 1 s",
                      "It waited 1 s for room", "none came"):
            self.assertIn(words, why)


class NativeLockWatchRows(Case):
    """S5b, point 7 (the owner, 2026-09-27: a server run on its own degrades
    "same behaviour as coordinator"): the machine's lock watches the card
    itself while it holds it - read every watch_s, every recover_watch_s
    while degraded; free memory under its floor, or a memory error its
    models' logs said, marks it degraded; the first read with room for what
    its session needs clears it. Here with a reader of the row's own."""

    def setUp(self):
        super().setUp()
        self.free = [20000]
        self.reads = 0
        self.heard = []
        self.counts = {"memory_errors": 0, "memory_error": None}

    def read(self):
        self.reads += 1
        return {"free": self.free[0], "total": 24576}

    def card(self, hooks=True, **kw):
        """The lock, read by the row's reader - and, with `hooks`, heard by
        the row, its work the row's counts; without, a manager's to hook."""
        kw.setdefault("read", self.read)
        card = MachineCard(self.tmp / "card.lock", **{"watch_s": 0.2, "recover_watch_s": 0.05,
                                                      **kw})
        if hooks:
            card.on_degraded = self.heard.append
            card.work = lambda: dict(self.counts)
        self.addCleanup(card.give_back)
        return card

    def wait_for(self, what, pred, timeout=5.0):
        deadline = time.monotonic() + timeout
        while not pred():
            self.assertLess(time.monotonic(), deadline, f"timed out waiting for {what}")
            time.sleep(0.01)

    def test_under_its_floor_it_is_degraded_and_read_faster_until_it_has_room_for_its_need(self):
        card = self.card()
        self.assertIsNone(card.take(W, "alpha"))
        self.assertEqual((card.session, card.recovers), ("lock-1", True))
        self.assertEqual(card.fit("alpha", "llamacpp", 6000, read=self.read)["verdict"], "fits")
        card.running(40001, "load-k3x9m2p7", "alpha")
        self.free[0] = 176
        self.wait_for("the mark", lambda: self.heard)
        d = self.heard[0]
        self.assertEqual((d["reason"], d["free_mib"], d["floor_mib"], d["total_mib"],
                          d["need_mib"], d["outside_mib"]),
                         ("under_floor", 176, 256, 24576, 256, 24576 - 176 - 6000))
        self.assertIn("176 MiB free of 24576, under the floor of 256 MiB", d["why"])
        before = self.reads
        time.sleep(0.5)
        self.assertGreaterEqual(self.reads - before, 5, "not read every recover_watch_s")
        card.stopped("load-k3x9m2p7")                       # its model came off
        self.free[0] = 300
        self.wait_for("the numbers of a newer read", lambda: self.heard[-1] is not None
                      and self.heard[-1]["free_mib"] == 300)
        self.assertEqual(self.heard[-1]["need_mib"], 6256, "300 MiB free is not room for 6000")
        self.free[0] = 6256
        self.wait_for("room again", lambda: self.heard[-1] is None)
        self.assertEqual(sum(1 for h in self.heard if h is not None and h["free_mib"] == 176), 1)
        before = self.reads
        time.sleep(0.35)
        self.assertLessEqual(self.reads - before, 2, "still read every recover_watch_s")

    def test_a_memory_error_its_models_said_marks_it_at_once_and_a_read_with_room_clears_it(self):
        card = self.card(watch_s=30.0)
        card.take(W, "alpha")
        self.counts.update(memory_errors=1, memory_error="CUDA error: out of memory")
        card.beat_now()
        self.wait_for("the mark", lambda: self.heard)
        d = self.heard[0]
        self.assertEqual((d["reason"], d["error"]), ("memory_error", "CUDA error: out of memory"))
        self.wait_for("room again, read within recover_watch_s", lambda: self.heard[-1] is None,
                      timeout=2.0)

    def test_degraded_nothing_new_goes_on_the_card_and_the_card_is_not_read_for_it(self):
        card = self.card()
        card.take(W, "alpha")
        self.free[0] = 100
        self.wait_for("the mark", lambda: self.heard)
        card._watch_stop.set()                              # the watch reads no more
        time.sleep(0.1)
        before = self.reads
        got = card.fit("beta", "llamacpp", 1000, read=self.read)
        self.assertEqual(got["verdict"], "degraded")
        self.assertIn("beta may not go on the card now", got["words"])
        self.assertEqual(self.reads, before)

    def test_its_state_ends_with_its_session_and_a_counts_from_before_it_is_not_news(self):
        self.counts.update(memory_errors=3)
        card = self.card()
        card.take(W, "alpha")
        self.free[0] = 100
        self.wait_for("the mark", lambda: self.heard)
        card.give_back()
        self.assertIsNone(card.session)
        self.assertIsNone(card.degraded)
        self.free[0] = 20000
        self.heard.clear()
        card.take(W, "alpha")
        self.assertEqual(card.session, "lock-2")
        time.sleep(0.5)
        self.assertEqual(self.heard, [], "a new session was degraded by the last one's errors")

    def test_without_a_reader_it_starts_no_watch_and_never_holds_work(self):
        card = MachineCard(self.tmp / "bare.lock")
        self.addCleanup(card.give_back)
        card.take(W, "alpha")
        self.assertFalse(card.recovers)
        self.assertIsNone(card._watcher)

    def test_its_watch_is_a_setting_a_bad_one_is_refused_and_a_real_servers_lock_has_it(self):
        own = LLAMA_SETTINGS.replace("[runner]\n", "[runner]\ncard_watch_s = 5.0\n"
                                     "card_recover_watch_s = 0.5\ncard_floor_mib = 1000\n", 1)
        cfg = self.config(own, name="watch.toml")
        self.assertEqual(M.lock_watch(cfg), {"watch_s": 5.0, "recover_watch_s": 0.5,
                                             "floor_mib": 1000})
        self.assertEqual(M.lock_watch(self.config()), {"watch_s": 30.0, "recover_watch_s": 2.0,
                                                       "floor_mib": 256}, "the defaults (D0)")
        m = self.manager(cfg, vram=self.read)
        self.assertEqual((m._card.watch_s, m._card.recover_watch_s, m._card.floor_mib),
                         (5.0, 0.5, 1000), "a real server's lock does not watch as it is set")
        for key, bad in (("card_watch_s", 0), ("card_recover_watch_s", -1),
                         ("card_recover_watch_s", "2"), ("card_floor_mib", -1),
                         ("card_floor_mib", 1.5)):
            with self.subTest(key=key, bad=bad):
                text = SETTINGS.replace("[runner]\n", f"[runner]\n{key} = {json.dumps(bad)}\n", 1)
                with self.assertRaises(ValueError) as caught:
                    self.config(text, name="bad.toml")
                self.assertIn(key, str(caught.exception))

    def test_a_manager_hands_it_its_reader_only_when_it_reads_the_card(self):
        card = MachineCard(self.tmp / "a.lock")
        m = self.manager(card=card, vram=self.read)
        self.assertIsNotNone(card.read)
        self.assertTrue(m._recovers())
        plain = MachineCard(self.tmp / "b.lock")
        self.manager(card=plain, folder="plain")
        self.assertIsNone(plain.read, "a manager that reads no card handed a reader")

    def test_a_job_held_on_the_lock_runs_once_its_own_watch_finds_room(self):
        """The same as under a coordinator: the answer the card broke, the job
        held, the watch's read with room, the job on."""
        card = self.card(hooks=False, watch_s=30.0)
        m = self.manager(card=card, vram=self.read, backend_factory=NativeWatchRows.answering(
            self, log=[OOM], breaks="the stream broke"))
        b = m.acquire("alpha", W)
        self.free[0] = 100
        with self.assertRaises(M.Degraded):
            list(m.answer(b, "hi", GenParams()))
        heard = []
        box = {}
        t = threading.Thread(target=lambda: box.update(got=m.acquire(
            "alpha", W, terms=Terms(patience_s=10), on_full=heard.append)), daemon=True)
        t.start()
        self.wait_for("the lock's own mark", lambda: m.degraded)
        self.assertIn("the card's memory ran out", m.degraded)
        time.sleep(0.3)
        self.assertTrue(t.is_alive(), "it did not wait for the card")
        self.free[0] = 20000
        t.join(5)
        self.assertIsInstance(box.get("got"), Answering, box)
        self.assertIsNone(m.degraded)


class ReadSpeedRows(unittest.TestCase):
    """The read-speed measure (runner/diskread.py): one bounded stretch, said
    honestly. Real reads of a small file in a folder of the row's own; the
    clock is faked where a row needs time to pass."""

    def setUp(self):
        from runner import diskread
        self.D = diskread
        self.tmp = Path(tempfile.mkdtemp(prefix="llmb-diskread-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.file = self.tmp / "model.gguf"
        self.file.write_bytes(os.urandom(12 * diskread.MIB + 777))
        self.readers = (False, True) if sys.platform == "win32" else (False,)

    def test_one_stretch_a_third_of_the_way_in_and_never_past_the_end(self):
        MIB = self.D.MIB
        for past in self.readers:
            m = self.D.measure(self.file, stretch=3 * MIB + 5, past_cache=past)
            # (12 MiB + 777) // 3 is 4 MiB + 259: on the MiB boundary, 4 MiB
            self.assertEqual(m["offset"], 4 * MIB, past)
            self.assertEqual(m["bytes"], 3 * MIB + 5, f"the stretch asked for, and no more ({past})")
            m = self.D.measure(self.file, past_cache=past)
            self.assertEqual(m["bytes"], 8 * MIB + 777, f"to the end of the file at most ({past})")

    def test_a_plain_read_cannot_tell_and_a_repeat_is_most_likely_the_cache(self):
        seen = set()
        first = self.D.measure(self.file, past_cache=False, seen=seen)
        again = self.D.measure(self.file, past_cache=False, seen=seen)
        self.assertIsNone(first["past_cache"])
        self.assertIn("cannot tell the disk from its cache", first["said"])
        self.assertIs(again["past_cache"], False)
        self.assertIn("most likely the operating system's cache", again["said"])

    @unittest.skipUnless(sys.platform == "win32", "only Windows reads past the cache here")
    def test_a_read_past_the_cache_says_so_and_leaves_nothing_in_it(self):
        seen = set()
        past = self.D.measure(self.file, past_cache=True, seen=seen)
        self.assertIs(past["past_cache"], True)
        self.assertIn("past the operating system's cache", past["said"])
        after = self.D.measure(self.file, past_cache=False, seen=seen)
        self.assertIsNone(after["past_cache"], "a read past the cache never put the stretch there")

    def test_the_time_limit_stops_the_read_and_says_so(self):
        for past in self.readers:
            ticks = iter(range(100))
            m = self.D.measure(self.file, past_cache=past, limit_s=2.5,
                               clock=lambda: float(next(ticks)))
            # a second a tick: two chunks read, the third refused at 3 s
            self.assertEqual(m["bytes"], 2 * self.D.MIB, past)
            self.assertIn("stopped at the 2.5 s limit", m["said"], past)
            self.assertEqual(m["mb_s"], round(m["bytes"] / 1e6 / m["seconds"], 1), past)

    def test_a_short_stretch_says_it_is_too_little(self):
        m = self.D.measure(self.file, stretch=self.D.MIB, past_cache=False)
        self.assertIn("only 1.0 MB - too little to say much about the disk", m["said"])
        m = self.D.measure(self.file, past_cache=False)
        self.assertNotIn("too little", m["said"], "8.4 MB is enough to say something")

    def test_the_disk_is_named(self):
        disk = self.D.disk_of(self.file)
        if sys.platform == "win32":
            self.assertEqual(disk, os.path.splitdrive(str(self.file.resolve()))[0])
        else:
            self.assertTrue(disk and os.path.ismount(disk), disk)


class ModelHealthRows(unittest.TestCase):
    """What `GET /api/models` says of one model (runner/modelhealth.py) where
    the server's rows cannot reach: a llama.cpp model with no file, a record
    somebody broke, and the arithmetic of the history."""

    def setUp(self):
        from runner.config import ModelSpec
        from runner.modelhealth import history, model_health
        self.Spec, self.history, self.health = ModelSpec, history, model_health
        self.tmp = Path(tempfile.mkdtemp(prefix="llmb-health-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_llamacpp_model_with_no_file_says_so_and_the_stand_in_needs_none(self):
        spec = self.Spec("draft", file="")
        got = self.health(spec, self.tmp, "llamacpp", None, loaded=False, measure=None)
        self.assertEqual(got["problems"], ["no file named: llama.cpp needs [models.draft] file "
                                           "to point at a GGUF"])
        got = self.health(spec, self.tmp, "mock", None, loaded=False, measure=None)
        self.assertEqual(got["problems"], [])
        self.assertIsNone(got["file"])

    def test_each_model_says_its_engine_and_a_whisper_models_file_is_never_read_as_a_gguf(self):
        """PLAN-4 D10, D11: the list says each model's engine. A GGUF header is
        llama.cpp's: a whisper model's file (the ggml magic) is there and
        said, never called "not a usable GGUF" - its header is its own, the
        eleven numbers and its shape in words, and it has no context to set
        (S8 R4: it was the 8192 no whisper model reads). With no file named,
        each engine's words. [S12b, on purpose: the header was null here,
        waiting for the engine.]"""
        import struct
        (self.tmp / "ears.bin").write_bytes(struct.pack("<I11i", *GGML_HEADER))
        ears = self.Spec("ears", engine="whisper", file="ears.bin")
        got = self.health(ears, self.tmp, "real", {}, loaded=False, measure=None)
        names = ("n_vocab", "n_audio_ctx", "n_audio_state", "n_audio_head", "n_audio_layer",
                 "n_text_ctx", "n_text_state", "n_text_head", "n_text_layer", "n_mels", "ftype")
        self.assertEqual((got["engine"], got["file"]["present"], got["header"], got["problems"],
                          got["context"]),
                         ("whisper", True, {"shape": "large v3", **dict(zip(names, GGML_HEADER[1:]))},
                          [], None))
        shapes = V.load(HERE.parent / "llm-bootstrap-contract")[0]
        # merging S12b with S14b: where a model stands - its state, and whether
        # a pipeline keeps it - is the manager's to add (S14b), so the entry is
        # held to the shape with those two added as the manager adds them
        placed = {**got, "state": {"key": "ears", "state": "off", "why": None, "load": None},
                  "pinned": False}
        self.assertEqual(V.check(placed, {"ref": "model"}, shapes), [])
        story = self.health(self.Spec("story", ctx=4096), self.tmp, "mock", None, loaded=False,
                            measure=None)
        self.assertEqual(story["context"], 4096, "a text model's context went with it")
        bare = self.health(self.Spec("ears", engine="whisper"), self.tmp, "real", None,
                           loaded=False, measure=None)
        self.assertEqual(bare["problems"], ["no file named: whisper-server needs [models.ears] "
                                            "file to point at a whisper model"])
        self.assertEqual(self.health(self.Spec("draft"), self.tmp, "mock", None, loaded=False,
                                     measure=None)["engine"], "llamacpp")

    def test_a_whisper_models_header_is_read_as_whisper_reads_it_once_and_a_wrong_file_said(self):
        """PLAN-4 D11: a whisper model's header - the ggml magic and eleven
        numbers - read once while its file is the same file, as a GGUF's is;
        a file that is not a whisper model said in the whisper reader's own
        words, and no header. One file named by two models of two engines is
        read by each engine's own reader, never handed the other's answer."""
        import struct
        from unittest import mock
        import runner.modelhealth as MHm
        (self.tmp / "ears.bin").write_bytes(struct.pack("<I11i", *GGML_HEADER))
        (self.tmp / "text.gguf").write_bytes(b"GGUF" + bytes(60))
        (self.tmp / "short.bin").write_bytes(struct.pack("<I3i", GGML_HEADER[0], 1, 2, 3))
        headers = {}
        ears = self.Spec("ears", engine="whisper", file="ears.bin")
        with mock.patch.object(MHm, "read_ggml", side_effect=MHm.read_ggml) as read:
            first = self.health(ears, self.tmp, "real", None, loaded=False, measure=None,
                                headers=headers)
            again = self.health(ears, self.tmp, "real", None, loaded=False, measure=None,
                                headers=headers)
            self.assertEqual(read.call_count, 1, "an unchanged file's header is read once")
        self.assertEqual((first["header"], first["problems"]), (LARGE_V3_HEADER, []))
        self.assertEqual(first, again)
        wrong = self.health(self.Spec("wrong", engine="whisper", file="text.gguf"), self.tmp,
                            "real", None, loaded=False, measure=None, headers=headers)
        self.assertEqual((wrong["header"], wrong["problems"]),
                         (None, ["text.gguf is not a whisper model: it does not start with the "
                                 "ggml magic 0x67676d6c (its first bytes are b'GGUF')"]))
        short = self.health(self.Spec("short", engine="whisper", file="short.bin"), self.tmp,
                            "real", None, loaded=False, measure=None, headers=headers)
        self.assertEqual((short["header"], short["problems"]),
                         (None, ["short.bin is not a whisper model: its header ends after 16 "
                                 "of 48 bytes"]))
        as_text = self.health(self.Spec("both", file="ears.bin"), self.tmp, "real", None,
                              loaded=False, measure=None, headers=headers)
        self.assertIsNone(as_text["header"])
        self.assertEqual(len(as_text["problems"]), 1)
        self.assertIn("ears.bin is not a usable GGUF", as_text["problems"][0])
    def test_a_whisper_models_file_that_is_not_a_whisper_model_is_said_and_has_no_header(self):
        """The words a launch would refuse it in (runner/ggml.py), and no
        header: a file without the ggml magic is not a whisper model."""
        (self.tmp / "junk.bin").write_bytes(b"GGUF, not ggml " * 8)
        got = self.health(self.Spec("ears", engine="whisper", file="junk.bin"), self.tmp, "real",
                          {}, loaded=False, measure=None)
        self.assertIs(got["file"]["present"], True)
        self.assertIsNone(got["header"])
        self.assertEqual(len(got["problems"]), 1, got["problems"])
        self.assertIn("junk.bin is not a whisper model", got["problems"][0])
        self.assertIn("0x67676d6c", got["problems"][0])

    def test_a_file_that_is_not_a_gguf_is_there_and_said(self):
        (self.tmp / "junk.gguf").write_bytes(b"not a gguf at all " * 64)
        got = self.health(self.Spec("junk", file="junk.gguf"), self.tmp, "llamacpp", {},
                          loaded=False, measure=None)
        self.assertIs(got["file"]["present"], True)
        self.assertIsNone(got["header"])
        self.assertEqual(len(got["problems"]), 1)
        self.assertIn("junk.gguf is not a usable GGUF", got["problems"][0])

    def test_a_header_is_read_once_until_its_file_changes(self):
        import struct
        from unittest import mock
        import runner.modelhealth as MHm

        def gguf(arch: str) -> bytes:
            s = lambda text: struct.pack("<Q", len(text.encode())) + text.encode()   # noqa: E731
            return (b"GGUF" + struct.pack("<IQQ", 3, 0, 1)
                    + s("general.architecture") + struct.pack("<I", 8) + s(arch))
        path = self.tmp / "m.gguf"
        path.write_bytes(gguf("llama"))
        spec, headers = self.Spec("m", file="m.gguf"), {}
        with mock.patch.object(MHm, "read_header", side_effect=MHm.read_header) as read:
            first = self.health(spec, self.tmp, "llamacpp", None, loaded=False, measure=None,
                                headers=headers)
            again = self.health(spec, self.tmp, "llamacpp", None, loaded=False, measure=None,
                                headers=headers)
            self.assertEqual(read.call_count, 1, "an unchanged file's header is read once")
            self.assertEqual(first, again)
            path.write_bytes(gguf("qwen2moe"))          # another file, another size
            changed = self.health(spec, self.tmp, "llamacpp", None, loaded=False, measure=None,
                                  headers=headers)
            self.assertEqual(read.call_count, 2, "a file that changed is read again")
        self.assertEqual(changed["header"]["architecture"], "qwen2moe")

    def test_the_last_load_is_judged_against_those_before_it(self):
        # three loads, the last far above: against the two before it there is
        # no normal yet, so it is not slow - judged against itself it would be
        buckets = {"m::cold": {"samples": [5.0, 5.2, 30.0], "n": 3, "last": 30.0, "last_at": 1.0}}
        self.assertEqual(self.history(buckets, "m", True),
                         {"runs": 3, "normal": 5.2, "last": 30.0, "last_at": 1.0, "slow": False})

    def test_runs_counts_every_load_not_the_samples_kept(self):
        buckets = {"m::warm": {"samples": [1.0, 1.1, 1.2], "n": 57, "last": 1.2, "last_at": 2.0}}
        self.assertEqual(self.history(buckets, "m", False)["runs"], 57)

    def test_a_record_somebody_broke_reads_as_no_loads(self):
        # a bool is not a number, though Python counts True as one
        buckets = {"m::cold": {"samples": ["x", None, True], "n": True, "last": "slow",
                               "last_at": []},
                   "m::warm": "not a bucket"}
        none = {"runs": 0, "normal": None, "last": None, "last_at": None, "slow": False}
        self.assertEqual(self.history(buckets, "m", True), none)
        self.assertEqual(self.history(buckets, "m", False), none)


# ========================================== the statistics (PLAN-4 D12, S11b) ===

from runner import stats as ST                                  # noqa: E402

CONTRACT = HERE.parent / "llm-bootstrap-contract"


def _pause(seconds: float) -> None:
    """At least *seconds*, by the monotonic clock: a timed wait can end early."""
    until = time.monotonic() + seconds
    while True:
        left = until - time.monotonic()
        if left <= 0:
            return
        time.sleep(left)


class Paced(FakeBackend):
    """What the statistics time, made to measure: its start marks each step
    an engine's start marks (`steps`), `step_s` apart; its answer streams
    `tokens` tokens `delay` apart - or, `once`, comes in one piece after
    `delay`, over `audio_s` of audio - and its stop takes `stop_s`."""

    def __init__(self, key, tokens=5, delay=0.0, step_s=0.0, stop_s=0.0, once=False,
                 audio_s=10.0, prompt=12, steps=("spawn", "port", "healthy", "identity"), **kw):
        super().__init__(key, **kw)
        self.tokens, self.delay, self.step_s, self.stop_s = tokens, delay, step_s, stop_s
        self.audio_s, self.prompt, self.marked = audio_s, prompt, steps
        if once:
            self.answers = "once"

    def start(self):
        super().start()
        for step in self.marked:
            _pause(self.step_s)
            self.mark(step)

    def stop(self):
        _pause(self.stop_s)
        super().stop()

    def stream(self, messages, params, cancel=None):
        if self.answers == "once":
            _pause(self.delay)
            yield {"type": "done", "finish_reason": "stop", "text": "heard",
                   "tokens": self.tokens, "audio_s": self.audio_s}
            return
        for i in range(self.tokens):
            if i:
                _pause(self.delay)
            yield {"type": "token", "text": "t "}
        yield {"type": "done", "finish_reason": "stop",
               "usage": {"prompt_tokens": self.prompt, "completion_tokens": self.tokens},
               "timings": {}}


class Sized(RecordingCard):
    """A card source that reads the card for its fit check - `free` MiB free,
    as the machine's lock or a coordinator's amend answers - and writes down
    every process it is told of, with the memory it is said to use."""

    def __init__(self, free=20000):
        super().__init__()
        self.free, self.told = free, []

    def fit(self, model, engine, need_mib, *, read=None, margin_mib=None, how="", hint=""):
        return {"verdict": "fits", "need_mib": need_mib or 0, "free_mib": self.free,
                "margin_mib": 256, "total_mib": 24576, "words": "fits"}

    def running(self, pid, proof, model, engine=None, port=None, mib=None):
        self.told.append((proof, model, mib))


class Claimed(RecordingCard):
    """A card source whose take waits `wait_s` - a ticket waiting, then
    offered - and says how long its claim took (`claim_s`), as a
    coordinator's does."""

    def __init__(self, wait_s=0.2, claim_s=0.03):
        super().__init__()
        self.wait_s, self.claim_of = wait_s, claim_s
        self.claim_s = None

    def take(self, holder, model_key, for_=None, cancel=None, on_wait=None, terms=None):
        self.claim_s = None
        _pause(self.wait_s)
        got = super().take(holder, model_key, for_, cancel, on_wait, terms)
        self.claim_s = self.claim_of
        return got


class StatsCase(Case):
    def paced(self, **kw):
        self.made_p = []

        def make(key, port):
            b = Paced(key, **kw)
            self.made_p.append(b)
            return b
        return make

    def timed(self, m, what=None):
        """The statistics' records: the `timed` lines, in the order written."""
        return [e for e in m.stat_lines() if what in (None, e["what"])]

    def kept(self, m, model="alpha"):
        return ST.ModelStats(m.stats_path).read()["models"].get(model, {})

    def wait_for(self, what, pred, timeout=5.0):
        deadline = time.monotonic() + timeout
        while not pred():
            self.assertLess(time.monotonic(), deadline, f"timed out waiting for {what}")
            time.sleep(0.01)

    def answer(self, m, b, text="hello"):
        return list(m.answer(b, text, GenParams()))


class NativeStatisticsRows(StatsCase):
    """PLAN-4 D12, wired in (S11b): what the runner measures - a load, an
    answer, an unload, a job's share - is one `timed` line of its record,
    folded as it is written into the model's statistics in the bound folder
    (`runner/model_stats.json`). The records stay: the summary rebuilt from
    them equals the one kept."""

    def test_an_answer_is_timed_and_kept_under_its_model(self):
        m = self.manager(backend_factory=self.paced(tokens=5, delay=0.02, prompt=12))
        b = m.acquire("alpha", W)
        self.answer(m, b)
        line = self.timed(m, "answer")[0]
        self.assertEqual((line["model_key"], line["load"]), ("alpha", b.load_id))
        got = line["measures"]
        self.assertEqual(list(got), ["first_token_s", "answer_s", "tokens", "tokens_per_s",
                                     "prompt_tokens_per_s"])
        self.assertEqual(got["tokens"], 5)
        self.assertGreaterEqual(got["answer_s"] - got["first_token_s"], 0.08,
                                "four tokens after the first, 0.02 s apart")
        self.assertAlmostEqual(got["tokens_per_s"], 4 / (got["answer_s"] - got["first_token_s"]),
                               delta=0.001)
        self.assertAlmostEqual(got["prompt_tokens_per_s"] * got["first_token_s"], 12, places=6)
        kept = self.kept(m)
        self.assertEqual((kept["answer_s"]["count"], kept["answer_s"]["last"]),
                         (1, got["answer_s"]))
        self.assertEqual(kept["answer_s"]["last_at"], line["ts"], "at the line's own time")

    def test_the_summary_rebuilt_from_its_records_equals_the_one_kept(self):
        m = self.manager(backend_factory=self.paced(tokens=4, step_s=0.01, stop_s=0.01))
        for key in ("alpha", "beta", "alpha"):
            b = m.acquire(key, W)
            self.answer(m, b)
            self.answer(m, b)
            m.unload(key)
        records = M.stat_records(m.stat_lines())
        self.assertGreater(len(records), 20)
        kept = ST.ModelStats(m.stats_path).read()
        self.assertEqual(ST.rebuild(records), kept)
        self.assertNotEqual(ST.rebuild(records[:-1]), kept, "a record left out is seen")
        self.assertEqual(set(kept["models"]), {"alpha", "beta"})

    def test_answers_at_once_each_say_what_ran_beside_it_and_a_lone_one_nothing(self):
        """S17: two answers on two models at once. Each answer's timed line
        names every model that answered beside it at any moment of it
        (`beside`), and its statistics keep how many beside each sample; an
        answer alone names none. The summary is still the rebuild of its
        records."""
        m = self.manager(backend_factory=self.paced(tokens=6, delay=0.1))
        a, b = m.acquire("alpha", W), m.acquire("beta", W)
        t = threading.Thread(target=self.answer, args=(m, b), daemon=True)
        t.start()
        self.wait_for("beta to answer", lambda: m.work()["working"])
        self.answer(m, a)
        t.join(5)
        self.answer(m, a)
        lines = [(x["model_key"], x["beside"]) for x in self.timed(m, "answer")]
        self.assertEqual(sorted(lines[:2]), [("alpha", ["beta"]), ("beta", ["alpha"])])
        self.assertEqual(lines[2], ("alpha", []))
        self.assertEqual(self.kept(m)["answer_s"]["beside"], [1, 0])
        self.assertEqual(self.kept(m, "beta")["tokens"]["beside"], [1])
        kept = ST.ModelStats(m.stats_path).read()
        self.assertEqual(ST.rebuild(M.stat_records(m.stat_lines())), kept)

    def test_the_heartbeat_speaks_of_every_answer_under_way(self):
        """S17, what the rental's heartbeat says (`work`), with a transcript
        made in one piece while a text model streams beside it: working while
        either answers, the progress counting both, and the deadline the
        one-piece answer's - still said once the stream beside it has ended."""
        class Once(Paced):
            def answer_deadline_s(self_, messages, params):
                return 30.0
        m = self.manager(backend_factory=lambda key, port: (
            Once(key, tokens=3, delay=1.0, once=True) if key == "beta"
            else Paced(key, tokens=4, delay=0.1)))
        a, b = m.acquire("alpha", W), m.acquire("beta", W)
        t = threading.Thread(target=self.answer, args=(m, b), daemon=True)
        t.start()
        self.wait_for("the transcript to begin", lambda: m.work()["working"])
        self.answer(m, a)
        said = m.work()
        self.assertTrue(said["working"], "the transcript beside it is still answering")
        self.assertEqual(said["progress"], 5, "the stream's 4 tokens and its end")
        self.assertGreater(said["deadline_s"], 25.0)
        t.join(5)
        self.assertEqual((m.work()["working"], m.work()["progress"], m.work()["deadline_s"]),
                         (False, 6, None))

    def test_health_says_every_model_a_generation_holds(self):
        m = self.manager(backend_factory=self.paced())
        self.assertEqual(m.status()["in_use_models"], [])
        with m.in_use("beta"), m.in_use("alpha"):
            st = m.status()
            self.assertEqual((st["in_use"], st["in_use_models"]), (2, ["alpha", "beta"]))
        self.assertEqual(m.status()["in_use_models"], [])

    def test_it_says_which_models_are_up_and_whether_one_is_loading(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        class Slow(FakeBackend):
            def start(self_):
                if self_.model_key == "beta":
                    started.set()
                    release.wait(5)
                super().start()
        m = self.manager(backend_factory=lambda key, port: Slow(key))
        self.assertEqual(m.up_keys(), [])
        m.acquire("alpha", W)
        t = threading.Thread(target=lambda: m.acquire("beta", W), daemon=True)
        t.start()
        self.assertTrue(started.wait(5))
        self.assertEqual(m.up_keys(), ["alpha"], "a model loading is not up")
        self.assertEqual((m.loading_for("beta"), m.loading_for("alpha")), (True, False))
        release.set()
        t.join(5)
        self.assertEqual(m.up_keys(), ["alpha", "beta"])
        self.assertEqual(m.loading_for("beta"), False)

    def test_a_take_waiting_for_the_card_is_its_models_load(self):
        opened, arrived = threading.Event(), threading.Event()
        self.addCleanup(opened.set)

        class Card:
            turn = lost = None

            def take(self_, holder, model_key, for_=None, cancel=None, on_wait=None,
                     terms=None):
                arrived.set()
                opened.wait(5)

            def give_back(self_):
                pass

            def status(self_):
                return None
        m = self.manager(backend_factory=self.paced(), card=Card())
        t = threading.Thread(target=lambda: m.acquire("beta", W), daemon=True)
        t.start()
        self.assertTrue(arrived.wait(5))
        self.assertEqual((m.loading_for("beta"), m.loading_for("alpha")), (True, False))
        opened.set()
        t.join(5)
        self.assertEqual(m.loading_for("beta"), False)

    def test_nothing_bound_keeps_nothing_and_says_null(self):
        m = self.manager(folder=None, backend_factory=self.paced())
        b = m.acquire("alpha", W)
        self.answer(m, b)
        self.assertIsNone(m.stats_path)
        self.assertEqual([x["stats"] for x in m.models()["models"]], [None, None])
        self.assertEqual([e["what"] for e in self.timed(m)], ["load", "answer"],
                         "measured, and said in memory, where the record is kept then")
        self.assertEqual(list(self.tmp.rglob(ST.FILE)), [])

    def test_a_loads_steps_and_its_total(self):
        m = self.manager(backend_factory=self.paced(step_s=0.03))
        m.acquire("alpha", W)
        got = self.timed(m, "load")[0]["measures"]
        steps = ["load_spawn_s", "load_port_s", "load_healthy_s", "load_identity_s"]
        self.assertEqual(list(got), steps + ["load_s"], "the steps in order, then the total")
        for step in steps:
            self.assertGreaterEqual(got[step], 0.03, step)
        self.assertGreaterEqual(got["load_s"], sum(got[s] for s in steps))

    def test_an_engine_that_has_fewer_steps_is_timed_by_the_ones_it_has(self):
        m = self.manager(backend_factory=self.paced(step_s=0.03, steps=("spawn", "healthy")))
        m.acquire("alpha", W)
        got = self.timed(m, "load")[0]["measures"]
        self.assertEqual(list(got), ["load_spawn_s", "load_healthy_s", "load_s"])
        self.assertGreaterEqual(got["load_healthy_s"], 0.03, "from the step before it")
        self.assertLess(got["load_healthy_s"], 0.5)

    def test_the_wait_for_the_card_and_its_claim_are_the_load_that_took_it(self):
        card = Claimed(wait_s=0.2, claim_s=0.03)
        m = self.manager(card=card, backend_factory=self.paced())
        m.acquire("alpha", W)
        m.acquire("beta", W)
        first, second = self.timed(m, "load")
        self.assertEqual((first["model_key"], second["model_key"]), ("alpha", "beta"))
        self.assertEqual(first["measures"]["load_claim_s"], 0.03)
        self.assertGreaterEqual(first["measures"]["load_wait_s"], 0.2 - 0.03)
        self.assertLess(first["measures"]["load_wait_s"], 0.2)
        self.assertGreaterEqual(first["measures"]["load_s"], 0.2, "the total counts the wait")
        self.assertFalse({"load_wait_s", "load_claim_s"} & set(second["measures"]),
                         "the card was held already: nothing waited for")
        self.assertLess(second["measures"]["load_s"], 0.2)

    def test_the_take_a_pipeline_made_is_its_first_models_load(self):
        card = Claimed(wait_s=0.2, claim_s=0.03)
        m = self.manager(card=card, backend_factory=self.paced())
        m.open_pipeline(W, models=["alpha"], keep_s=0)
        self.wait_for("alpha to be up", lambda: self.timed(m, "load"))
        got = self.timed(m, "load")[0]
        self.assertEqual(got["model_key"], "alpha")
        self.assertEqual(got["measures"]["load_claim_s"], 0.03,
                         "the pipeline took the card for it: its wait and claim are this load's")
        self.assertGreaterEqual(got["measures"]["load_wait_s"], 0.2 - 0.03)
        self.assertGreaterEqual(got["measures"]["load_s"], 0.2, "timed from the take's start")

    def test_a_stand_in_card_is_no_wait(self):
        m = self.manager(backend_factory=self.paced())
        m.acquire("alpha", W)
        self.assertFalse({"load_wait_s", "load_claim_s"} & set(self.timed(m, "load")[0]["measures"]))

    def test_an_unload_is_timed_from_asking_to_its_process_gone(self):
        m = self.manager(backend_factory=self.paced(stop_s=0.1))
        m.acquire("alpha", W)
        m.unload("alpha")
        line = self.timed(m, "unload")[0]
        self.assertEqual((line["model_key"], list(line["measures"])), ("alpha", ["unload_s"]))
        self.assertGreaterEqual(line["measures"]["unload_s"], 0.1)

    def test_a_stop_that_failed_is_no_unload(self):
        m = self.manager(backend_factory=self.paced(stop_fails="it would not go"))
        m.acquire("alpha", W)
        with self.assertRaises(RuntimeError):
            m.unload("alpha")
        self.assertEqual(self.timed(m, "unload"), [])

    def test_a_failed_answer_is_no_answer(self):
        m = self.manager(backend_factory=self.answering_breaks())
        b = m.acquire("alpha", W)
        with self.assertRaises(BackendError):
            self.answer(m, b)
        self.assertEqual(self.timed(m, "answer"), [])

    def answering_breaks(self):
        def make(key, port):
            return Answering(key, breaks="the model went away")
        return make

    def test_the_measures_are_the_contracts(self):
        shape = json.loads((CONTRACT / "shapes" / "model_stats.json").read_text(encoding="utf-8"))
        self.assertEqual(list(M.MEASURES), list(shape["object"]))

    def test_the_models_answer_carries_each_models_statistics_as_the_contract_says(self):
        m = self.manager(backend_factory=self.paced(tokens=3))
        b = m.acquire("alpha", W)
        self.answer(m, b)
        answer = m.models()
        shapes, routes = V.load(CONTRACT)
        self.assertEqual(V.check(answer, routes["server/models-list"]["responses"]["200"],
                                 shapes), [])
        got = {x["key"]: x["stats"] for x in answer["models"]}
        self.assertEqual(got["beta"], {}, "nothing measured yet")
        kept = self.kept(m)
        self.assertEqual(got["alpha"], {k: ST.describe(e) for k, e in kept.items()})
        self.assertIn("answer_s", got["alpha"])

    def test_a_summary_that_cannot_be_read_keeps_its_records_and_says_so_once(self):
        m = self.manager(backend_factory=self.paced())
        m.stats_path.parent.mkdir(parents=True, exist_ok=True)
        m.stats_path.write_bytes(b"[1, 2]")
        b = m.acquire("alpha", W)
        self.answer(m, b)
        self.answer(m, b)
        self.assertEqual(m.stats_path.read_bytes(), b"[1, 2]", "never written over")
        self.assertEqual(len(self.timed(m, "answer")), 2, "the records stay")
        self.assertEqual(self.kinds(m).count("stats_unreadable"), 1, "said once, not per note")
        self.assertEqual([x["stats"] for x in m.models()["models"]], [None, None])

    def test_a_jobs_share_is_kept_under_its_model(self):
        m = self.manager(backend_factory=self.paced())
        m.note_job("alpha", 0.012)
        line = self.timed(m, "job")[0]
        self.assertEqual((line["model_key"], line["measures"]), ("alpha", {"queue_s": 0.012}))
        self.assertEqual(self.kept(m)["queue_s"]["last"], 0.012)


class NativeSlowAnswerRows(StatsCase):
    """PLAN-4 D3 with D12: an answer FAR SLOWER than its model's own normal -
    under a third of its normal speed (`slow_answer_factor`) AND a quarter of
    a second lost over it at least (`slow_answer_floor_s`), its normal the
    median of the model's kept samples, three at least, BEFORE it - is
    flagged: on the record (`answer_slow`), in the statistics (`slow_answer`:
    how many times its normal it took), and in the work each heartbeat says,
    counted, with the newest in words, said at once."""

    def test_far_slower_is_a_factor_and_a_floor_both(self):
        slow = M.answer_slowness
        at = {"tokens": 101, "first_token_s": 0.5}
        self.assertIsNone(slow({**at, "answer_s": 2.5, "tokens_per_s": 50.0}, 48.0, 3.0, 0.25))
        self.assertAlmostEqual(slow({**at, "answer_s": 8.5, "tokens_per_s": 12.5}, 50.0, 3.0,
                                    0.25), 4.0)
        self.assertAlmostEqual(slow({**at, "answer_s": 10.5, "tokens_per_s": 10.0}, 30.0, 3.0,
                                    0.25), 3.0, msg="exactly a third of its normal is far slower")
        self.assertIsNone(slow({**at, "answer_s": 10.49, "tokens_per_s": 10.01}, 30.0, 3.0, 0.25),
                          "just faster than a third is not")
        self.assertIsNone(slow({"tokens": 3, "first_token_s": 0.1, "answer_s": 0.3,
                                "tokens_per_s": 10.0}, 50.0, 3.0, 0.25),
                          "five times slower, and 0.16 s lost: under the floor")
        self.assertIsNotNone(slow({"tokens": 3, "first_token_s": 0.1, "answer_s": 0.4,
                                   "tokens_per_s": 6.67}, 50.0, 3.0, 0.25),
                             "0.26 s lost: over it")
        self.assertIsNone(slow({**at, "answer_s": 8.5, "tokens_per_s": 12.5}, None, 3.0, 0.25),
                          "no normal, no judgement")
        self.assertIsNone(slow({"tokens": 1, "first_token_s": 0.5, "answer_s": 9.0}, 50.0, 3.0,
                               0.25), "one token has no speed")

    def test_an_answer_in_one_piece_is_judged_by_its_real_time_factor(self):
        slow = M.answer_slowness
        self.assertAlmostEqual(slow({"answer_s": 4.0, "audio_s": 10.0, "real_time_factor": 0.4},
                                    0.08, 3.0, 0.25), 5.0)
        self.assertIsNone(slow({"answer_s": 2.0, "audio_s": 10.0, "real_time_factor": 0.2},
                               0.08, 3.0, 0.25), "two and a half times: not far")
        self.assertIsNone(slow({"answer_s": 0.2, "audio_s": 0.5, "real_time_factor": 0.4},
                               0.08, 3.0, 0.25), "0.16 s lost on half a second of audio")

    def test_the_factor_and_the_floor_are_settings(self):
        runner = Config.default().runner
        self.assertEqual((runner["slow_answer_factor"], runner["slow_answer_floor_s"]),
                         (3.0, 0.25))
        cfg = self.config(SETTINGS.replace('backend = "mock"\n', 'backend = "mock"\n'
                                           'slow_answer_factor = 5\nslow_answer_floor_s = 1.5\n'))
        self.assertEqual((cfg.runner["slow_answer_factor"], cfg.runner["slow_answer_floor_s"]),
                         (5, 1.5))
        for line in ("slow_answer_factor = 1", "slow_answer_factor = 0.5",
                     "slow_answer_factor = true", 'slow_answer_factor = "3"',
                     "slow_answer_floor_s = -0.1", "slow_answer_floor_s = true",
                     "slow_answer_floor_s = inf", "slow_answer_factor = nan"):
            with self.subTest(line=line):
                with self.assertRaises(ValueError) as caught:
                    self.config(SETTINGS.replace('backend = "mock"\n',
                                                 f'backend = "mock"\n{line}\n'), name="bad.toml")
                self.assertIn(line.split(" =")[0], str(caught.exception))

    def test_the_models_answer_says_the_thresholds_a_slow_answer_is_judged_by(self):
        """H8 (S14c R3): what the server judges by, from its settings - so a
        page shows the server's thresholds, never ones typed into it."""
        cfg = self.config(SETTINGS.replace('backend = "mock"\n', 'backend = "mock"\n'
                                           'slow_answer_factor = 5\nslow_answer_floor_s = 1.5\n'))
        answer = self.manager(cfg).models()
        self.assertEqual(answer.get("slow_answer"), {"factor": 5.0, "floor_s": 1.5})
        shapes, routes = V.load(CONTRACT)
        self.assertEqual(V.check(answer, routes["server/models-list"]["responses"]["200"],
                                 shapes), [])
        self.assertEqual(self.manager(folder="other").models()["slow_answer"],
                         {"factor": 3.0, "floor_s": 0.25})

    def test_a_slow_answer_is_flagged_kept_counted_and_said_at_once(self):
        card = Watched()
        m = self.manager(card=card, backend_factory=self.paced(tokens=20))
        b = m.acquire("alpha", W)
        for _ in range(3):
            self.answer(m, b)
        self.assertEqual((m.work()["slow_answers"], m.work()["slow_answer"]), (0, None))
        b.delay = 0.03
        beats = card.beats
        self.answer(m, b)
        said = m.work()
        self.assertEqual(said["slow_answers"], 1)
        self.assertIn("alpha", said["slow_answer"])
        self.assertIn("times slower than its normal", said["slow_answer"])
        self.assertGreater(card.beats, beats, "said to the coordinator at once")
        flagged = [e for e in self.events(m) if e["event"] == "answer_slow"]
        self.assertEqual([(e["model_key"], e["load"]) for e in flagged], [("alpha", b.load_id)])
        self.assertGreater(flagged[0]["times"], 3.0)
        last = self.timed(m, "answer")[-1]["measures"]
        self.assertEqual(last["slow_answer"], flagged[0]["times"])
        self.assertEqual(self.kept(m)["slow_answer"]["count"], 1)
        b.delay = 0.0
        self.answer(m, b)
        self.assertEqual(m.work()["slow_answers"], 1, "an answer at its normal adds nothing")

    def test_it_is_judged_against_the_answers_before_it_three_at_least(self):
        m = self.manager(backend_factory=self.paced(tokens=20))
        b = m.acquire("alpha", W)
        for _ in range(2):
            self.answer(m, b)
        b.delay = 0.03
        self.answer(m, b)
        self.assertEqual(m.work()["slow_answers"], 0,
                         "two before it are no normal - with itself they would be three")
        self.assertNotIn("slow_answer", self.timed(m, "answer")[-1]["measures"])

    def test_each_model_has_its_own_normal(self):
        m = self.manager(backend_factory=self.paced(tokens=20))
        a = m.acquire("alpha", W)
        for _ in range(3):
            self.answer(m, a)
        b = m.acquire("beta", W)
        b.delay = 0.03
        for _ in range(3):
            self.answer(m, b)
        self.assertEqual(m.work()["slow_answers"], 0, "beta's own pace is its normal")

    def test_an_answer_in_one_piece_is_flagged_by_its_real_time_factor(self):
        m = self.manager(backend_factory=self.paced(once=True, tokens=7, audio_s=2.0))
        b = m.acquire("alpha", W)
        for _ in range(3):
            self.answer(m, b)
        got = self.timed(m, "answer")[0]["measures"]
        self.assertEqual(list(got), ["first_token_s", "answer_s", "tokens", "audio_s",
                                     "real_time_factor"])
        self.assertEqual(got["first_token_s"], got["answer_s"], "in one piece: the whole answer")
        self.assertEqual((got["tokens"], got["audio_s"]), (7, 2.0))
        self.assertAlmostEqual(got["real_time_factor"], got["answer_s"] / 2.0, delta=1e-12)
        b.delay = 0.6
        self.answer(m, b)
        self.assertEqual(m.work()["slow_answers"], 1)
        self.assertIn("of audio", m.work()["slow_answer"])


class NativeMemoryMeasureRows(StatsCase):
    """PLAN-4 D12: the card's memory a load took, at ready and after its
    first answer - read where the card can be read, against the free memory
    its own fit check read before it - and an unload's memory back. Each is
    a measure only when nothing else of this server came or went between
    the reads; the heartbeat's `mib` for its process is filled from them."""

    def reads(self, *free):
        """A way to read the card that answers *free*, in turn, and counts."""
        self.asked = 0
        seq = list(free)

        def read():
            self.asked += 1
            if not seq:
                raise AssertionError("the card was read more often than the row allows")
            return {"free": seq.pop(0), "total": 24576}
        return read

    def test_the_cards_memory_at_ready_and_after_the_first_answer(self):
        card = Sized(free=20000)
        m = self.manager(card=card, vram=self.reads(15000, 14500),
                         backend_factory=self.paced())
        b = m.acquire("alpha", W)
        self.assertEqual([e["measures"] for e in self.timed(m, "memory")],
                         [{"ready_mib": 5000}], "read at ready, before it is handed out")
        self.answer(m, b)
        self.wait_for("the look after its first answer", lambda: len(self.timed(m, "memory")) == 2)
        self.assertEqual(self.timed(m, "memory")[1]["measures"], {"first_answer_mib": 5500})
        self.answer(m, b)
        time.sleep(0.2)
        self.assertEqual(self.asked, 2, "its first answer only")
        self.assertEqual([t for t in card.told if t[2] is not None],
                         [(b.load_id, "alpha", 5000), (b.load_id, "alpha", 5500)],
                         "the heartbeat's mib for its process: at ready, then after its first answer")

    def test_the_card_is_never_read_without_a_way_to_read_it(self):
        def tripwire():
            raise AssertionError("the card was read by a row that gave no way to read it")
        was = M.ModelManager.__dict__["card_memory_mib"]         # the staticmethod itself
        M.ModelManager.card_memory_mib = staticmethod(tripwire)
        self.addCleanup(setattr, M.ModelManager, "card_memory_mib", was)
        m = self.manager(card=Sized(), backend_factory=self.paced())
        b = m.acquire("alpha", W)
        self.answer(m, b)
        m.unload("alpha")
        time.sleep(0.2)
        self.assertEqual(self.timed(m, "memory"), [])
        self.assertNotIn("card_read_failed", self.kinds(m))

    def test_a_card_that_cannot_be_read_measures_nothing(self):
        m = self.manager(card=Sized(), vram=lambda: None, backend_factory=self.paced())
        b = m.acquire("alpha", W)
        self.answer(m, b)
        time.sleep(0.2)
        self.assertEqual(self.timed(m, "memory"), [])

    def test_a_model_that_came_or_went_beside_it_makes_its_memory_no_measure(self):
        card = Sized(free=20000)
        was = M.MEMORY_BACK_S
        M.MEMORY_BACK_S = 0.3
        self.addCleanup(setattr, M, "MEMORY_BACK_S", was)
        m = self.manager(card=card, vram=lambda: {"free": 15000, "total": 24576},
                         backend_factory=self.paced())
        m.acquire("alpha", W)

        def unload_alpha(backend):
            if backend.model_key == "beta":
                m.unload("alpha")
        self.made_p.clear()
        m._factory = lambda key, port: Paced(key, on_start=unload_alpha)
        m.acquire("beta", W)
        got = [(e["model_key"], e["measures"]) for e in self.timed(m, "memory")]
        self.assertEqual(got, [("alpha", {"ready_mib": 5000})],
                         "alpha came off while beta loaded: beta's reads measure both")

    def test_an_unloads_memory_back(self):
        card = Sized(free=20000)
        # at ready; at the stop's asking; then after the process has gone
        m = self.manager(card=card, vram=self.reads(15000, 15000, 15000, 17000, 19900),
                         backend_factory=self.paced())
        m.acquire("alpha", W)
        m.unload("alpha")
        self.wait_for("the memory back", lambda: len(self.timed(m, "memory")) == 2)
        line = self.timed(m, "memory")[1]
        self.assertEqual(list(line["measures"]), ["unload_memory_s"])
        unload = self.timed(m, "unload")[0]["measures"]["unload_s"]
        self.assertGreaterEqual(line["measures"]["unload_memory_s"], unload,
                                "back after its process had gone: two looks later")
        self.assertEqual(self.asked, 5)

    def test_memory_that_never_comes_back_is_no_measure(self):
        card = Sized(free=20000)
        looks = []

        def read():
            looks.append(time.monotonic())
            return {"free": 15000, "total": 24576}
        m = self.manager(card=card, vram=read, backend_factory=self.paced())
        was = M.MEMORY_BACK_S
        M.MEMORY_BACK_S = 0.3
        self.addCleanup(setattr, M, "MEMORY_BACK_S", was)
        m.acquire("alpha", W)
        m.unload("alpha")
        time.sleep(0.8)
        self.assertEqual([list(e["measures"]) for e in self.timed(m, "memory")],
                         [["ready_mib"]])
        seen = len(looks)
        time.sleep(0.4)
        self.assertEqual(len(looks), seen, "the look for it is bounded: it ended")

    def test_a_load_that_took_no_memory_is_no_measure(self):
        m = self.manager(card=Sized(free=20000), vram=self.reads(21000),
                         backend_factory=self.paced())
        m.acquire("alpha", W)
        self.assertEqual(self.asked, 1, "read at ready")
        self.assertEqual(self.timed(m, "memory"), [],
                         "more free after it than before: something else let go meanwhile")

    def test_a_model_that_came_while_its_memory_came_back_makes_it_no_measure(self):
        free = {"now": 15000}
        m = self.manager(card=Sized(free=20000),
                         vram=lambda: {"free": free["now"], "total": 24576},
                         backend_factory=self.paced())
        m.acquire("alpha", W)
        m.unload("alpha")
        m.acquire("beta", W)
        free["now"] = 19900                                 # back - with beta beside it
        time.sleep(0.5)
        self.assertEqual([list(e["measures"]) for e in self.timed(m, "memory")
                          if e["model_key"] == "alpha"], [["ready_mib"]],
                         "beta came while alpha's memory was looked for: no measure")


class NativeMeasuredNeedRows(StatsCase):
    """PLAN-4 D2 with D12: once a model has three measured loads - its
    memory after its first answer - what it needs on the card is the largest
    of them + 256 MiB, in place of the runner's estimate."""

    def test_three_measured_loads_make_the_need_the_largest_plus_256(self):
        from runner import needs as N
        cfg = self.config()
        entry = {"count": 3, "most": 5500.0}
        self.assertEqual(N.need_mib(cfg, "alpha", "llamacpp", measured=entry)[0], 5756)
        self.assertIn("measured", N.need_mib(cfg, "alpha", "llamacpp", measured=entry)[1])
        self.assertEqual(N.need_mib(cfg, "alpha", "llamacpp",
                                    measured={"count": 2, "most": 5500.0})[0], None,
                         "two are not enough: the estimate - the stand-in's none")
        self.assertEqual(N.need_mib(cfg, "alpha", "llamacpp", measured=None)[0], None)
        self.assertEqual(N.MEASURED_SLACK_MIB, 256)

    def test_the_fit_check_is_asked_with_the_measured_need(self):
        card = Deciding(_verdict("fits"))
        m = self.manager(card=card, backend_factory=self.paced())
        store = ST.ModelStats(m.stats_path)
        for i, mib in enumerate((5200, 5500, 5300)):
            store.note("alpha", "first_answer_mib", mib, at=float(i))
        store.note("beta", "first_answer_mib", 9000, at=4.0)
        m.acquire("alpha", W)
        m.acquire("beta", W)
        asked = {a["model"]: a for a in card.asked}
        self.assertEqual(asked["alpha"]["need_mib"], 5756)
        self.assertIn("measured", asked["alpha"]["how"])
        self.assertIsNone(asked["beta"]["need_mib"], "one measured load: the estimate")
        self.assertEqual(m.need("alpha")[0], 5756)


# ================================== the line every suite of this part ends with ===

class NativeTallyRows(unittest.TestCase):
    """`_suites.tally`: what every unittest suite of this part says last -
    "N passed, M failed, T total" - counted in ROWS, because that is what a
    sweep compares with its controls. unittest's own count leaves out a row
    that never ran: a class whose `setUpClass` failed (a stand-in that could
    not start) read as ONE failure, its rows counted nowhere, and a total
    short by every one of them."""

    def tally(self, *classes) -> tuple:
        suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(c)
                                   for c in classes)
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
        return tally(types.SimpleNamespace(test=suite, result=result))

    def test_a_class_that_could_not_be_set_up_counts_every_row_it_has_as_failed(self):
        class CouldNotStart(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise RuntimeError("the stand-in never answered")

            def test_one(self):
                pass

            def test_two(self):
                pass

            def test_three(self):
                pass

        class Fine(unittest.TestCase):
            def test_four(self):
                pass

            def test_five(self):
                pass
        self.assertEqual(self.tally(CouldNotStart, Fine), (2, 3, 5))

    def test_a_row_is_counted_once_however_many_of_its_parts_fail(self):
        class ThreeWrong(unittest.TestCase):
            def test_parts(self):
                for i in range(3):
                    with self.subTest(i=i):
                        self.fail(f"part {i}")

        class Fine(unittest.TestCase):
            def test_whole(self):
                pass
        self.assertEqual(self.tally(ThreeWrong, Fine), (1, 1, 2))

    def test_a_class_that_could_not_tear_down_fails_the_tally(self):
        """Its rows passed; what they left behind did not go. Still a
        failure - the suite must not pass - charged as one row."""
        class LeftSomething(unittest.TestCase):
            @classmethod
            def tearDownClass(cls):
                raise RuntimeError("a stand-in outlived its class")

            def test_one(self):
                pass

            def test_two(self):
                pass
        self.assertEqual(self.tally(LeftSomething), (1, 1, 2))

    def test_a_skipped_row_is_a_row_that_did_not_fail(self):
        class Skipping(unittest.TestCase):
            @unittest.skip("not on this machine")
            def test_skipped(self):
                pass

            def test_run(self):
                pass
        self.assertEqual(self.tally(Skipping), (2, 0, 2))


def _the_gate():
    """The repository's gate (`gate.py`, beside the parts), loaded by its
    path: nothing of the root is put on the import path."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("the_gate", HERE.parent / "gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NativeGateTallyRows(unittest.TestCase):
    """What the gate reads of a suite's end: the suite's own tally, the last
    "P passed, F failed, T total" it prints - never unittest's own "Ran N
    tests", which unittest prints after it (to stderr, which the gate reads
    after stdout), and which leaves out every row of a class that could not
    be set up. A suite whose tally says a row failed fails the gate, whatever
    its exit code; one that prints no tally fails it too. Each is a real
    suite, run by the gate's own `run_suite`."""

    def run_suite(self, script: str) -> dict:
        tmp = Path(tempfile.mkdtemp(prefix="llmb-gate-rows-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "suite.py").write_bytes(script.encode("utf-8"))
        return _the_gate().run_suite("fake", str(tmp), "suite.py", tmp)

    def test_it_reads_a_unittest_suite_s_own_tally_not_unittest_s_count(self):
        r = self.run_suite(
            "import sys, unittest\n"
            f"sys.path.insert(0, {str(HERE)!r})\n"
            "from _suites import report\n"
            "class CouldNotStart(unittest.TestCase):\n"
            "    @classmethod\n"
            "    def setUpClass(cls):\n"
            "        raise RuntimeError('the stand-in never answered')\n"
            "    def test_one(self):\n"
            "        pass\n"
            "    def test_two(self):\n"
            "        pass\n"
            "class Fine(unittest.TestCase):\n"
            "    def test_three(self):\n"
            "        pass\n"
            "    def test_four(self):\n"
            "        pass\n"
            "    def test_five(self):\n"
            "        pass\n"
            "report(unittest.main(exit=False))\n")
        # unittest says "Ran 3 tests"; the suite says 3 passed, 2 failed, 5 total
        self.assertEqual(r["tally"], "3/5", r)
        self.assertIn("exit 1", r["problems"])

    def test_a_tally_that_says_a_row_failed_fails_the_gate_whatever_the_exit_code(self):
        r = self.run_suite("print('4 passed, 1 failed, 5 total')\n")
        self.assertEqual(r["tally"], "4/5")
        self.assertIn("its tally says 1 failed", r["problems"])

    def test_a_suite_with_unittest_s_count_and_no_tally_of_its_own_fails_the_gate(self):
        r = self.run_suite("import sys\n"
                           "print('Ran 3 tests in 0.010s', file=sys.stderr)\n"
                           "print('', file=sys.stderr)\n"
                           "print('OK', file=sys.stderr)\n")
        self.assertIsNone(r["tally"])
        self.assertIn("no tally", r["problems"])

    def test_the_last_tally_is_the_suite_s(self):
        r = self.run_suite("print('a row said: 9 passed, 9 failed, 18 total')\n"
                           "print('2 passed, 0 failed, 2 total')\n")
        self.assertEqual((r["tally"], r["problems"]), ("2/2", []))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from _suites import report                             # the tally, counted in rows
    report(unittest.main(exit=False, verbosity=1))
