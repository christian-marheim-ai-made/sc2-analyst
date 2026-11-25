"""The job queue's rows.

    py -3.12 test_queue.py

No model, no GPU, no network, no subprocess. The backend is an in-process
function that answers from its request; the application is a set of hooks.

Two kinds of rows live here:

  * ROWS CARRIED OVER BY MEANING - behaviours an existing application's queue
    already has and pins, restated against this queue's own interface. They
    are the measurement: how much of a working queue these hooks can carry.
  * NATIVE ROWS - what this queue adds or decides for itself: dependencies,
    the contract's record, the refusals' answers. Marked `Native` in the class
    name so the two are never counted together.

Every test that leaves a store behind has every row in it checked against the
contract's `job` shape on the way out (`check_records`), and the public records
the queue hands out are checked the same way: a record that drifts from the
contract fails whichever test wrote it.
"""
from __future__ import annotations

import builtins
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONTRACT = HERE.parent / "llm-bootstrap-contract"


# The queue module is `jobqueue`, not `queue`: a file named queue.py in a
# script's folder takes the standard library's name, and every later
# `import queue` in the process - by this suite or by a standard module -
# would find it instead.
import jobqueue as Q                                            # noqa: E402
sys.path.insert(0, str(CONTRACT))
import validate as V                                            # noqa: E402

SHAPES, ROUTES = V.load()

FAST = dict(tick_s=0.02, supervise_s=0.02, cancel_wait_s=1.0,
            give_up_grace_s=0.3, stall_s=60.0, load_s=120.0,
            stale_head_s=600.0, recover_timeout_s=0.3)
MODEL = "demo-7b"


# ------------------------------------------------------------------ doubles

class Backend:
    """A model that answers from its request - by default the job's input.

        {"text": s}                  answers s
        {"fail": why}                raises GenerationFailed(why)
        {"raise": "TimeoutError"}    raises that built-in exception
        {"gate": name}               waits for the test to open the gate,
                                     reporting progress and watching the cancel
        ... "stubborn": true         ...and ignoring the cancel
        ... "quiet": true            ...and reporting nothing at all
        {"sleep": s}                 takes s seconds, then answers

    `steps`, when given, answer the calls in order instead (a retry sequence).
    `healthy` ignores every gate: the backend is fine now."""

    def __init__(self, *steps, loading=None, recover=None):
        self.steps = list(steps)
        self.calls, self.cancelled = [], []
        self.gates: dict = {}
        self.lock = threading.Lock()
        self.healthy = False
        if loading is not None:
            self.loading = loading
        if recover is not None:
            self.recover = recover

    def gate(self, name) -> threading.Event:
        with self.lock:
            return self.gates.setdefault(name, threading.Event())

    def open_all(self) -> None:
        with self.lock:
            gates = list(self.gates.values())
        for g in gates:
            g.set()

    def __call__(self, request, params, run):
        with self.lock:
            self.calls.append({"request": request, "params": dict(params),
                               "handle": run.handle, "job": run.job_id})
            step = self.steps.pop(0) if self.steps else None
        r = request if step is None else step
        if callable(r) and not isinstance(r, type):
            return r(request, params, run)
        if isinstance(r, BaseException):
            raise r
        if isinstance(r, str):
            r = {"text": r}
        if not isinstance(r, dict):
            r = {"text": str(r)}
        if "gate" in r and not self.healthy:
            ev = self.gate(r["gate"])
            if not r.get("quiet"):
                run.report(stage="writing", tokens=1)
            while not ev.wait(0.01):
                if not r.get("stubborn") and run.stopped():
                    raise Q.Cancelled("the backend saw the cancel")
        if r.get("sleep"):
            time.sleep(float(r["sleep"]))
        if "fail" in r:
            raise Q.GenerationFailed(r["fail"])
        if "raise" in r:
            raise getattr(builtins, r["raise"])(r.get("why", r["raise"]))
        text = r.get("text", "Rain on the tin roof.")
        out = {k: v for k, v in r.items() if k not in self.CONTROL}
        out.setdefault("tokens", len(str(text).split()))
        out.setdefault("finish_reason", "stop")
        out.setdefault("model", params.get("model"))
        out["text"] = text
        return out

    #: what steers the double; everything else in a step is what it answers
    CONTROL = ("gate", "stubborn", "quiet", "sleep", "fail", "raise", "why",
               "no", "brief")

    def cancel(self, handle) -> None:
        self.cancelled.append(handle)


class App(Q.Hooks):
    """Hooks that record what they were asked and do what a test tells them."""

    def __init__(self, build=None, land=None, sampling=None, merge=None, judge=None):
        self._build, self._land, self._sampling = build, land, sampling
        self._merge, self._judge = merge, judge
        self.built, self.landed, self.merges, self.judged = [], [], [], []

    def build_request(self, job, after_results):
        self.built.append((job["id"], after_results))
        return self._build(job, after_results) if self._build else job.get("input")

    def land_result(self, job, result):
        self.landed.append((job["id"], result))
        return self._land(job, result) if self._land else None

    def sampling_for(self, kind, input):
        return self._sampling(kind, input) if self._sampling else {}

    def merge(self, group, members):
        self.merges.append({"group": group,
                            "members": [(m["job"]["id"], m["job"]["status"])
                                        for m in members],
                            "texts": [((m.get("result") or {}).get("candidates")
                                       or [{}])[0].get("text") for m in members],
                            "errors": [m["job"]["error"] for m in members]})
        if self._merge:
            self._merge(group, members)

    def judge(self, job, candidate):
        self.judged.append((job["id"], candidate.get("text")))
        return self._judge(job, candidate) if self._judge else None


def corr_for(jid: str) -> str:
    """A well-formed correlation id that is always the same for one job id -
    any job id, including the hand-made ones that are not numbers."""
    n = zlib.crc32(jid.encode("utf-8"))
    return "c-20260101-000000-" + "".join(Q.ids.ALPHABET[(n >> (5 * k)) % 32]
                                          for k in range(8))


def fixture_row(jid, status, finished=None, *, error=None, created=100.0,
                started=50.0, correlation_id=None, name=None, kind="generate",
                input=None):
    return {"job": {"id": jid, "kind": kind,
                    "name": kind if name is None else name, "status": status,
                    "priority": "batch", "queue_pos": 0, "after": [],
                    "group": None, "scope": None,
                    "correlation_id": correlation_id or corr_for(jid),
                    "correlation_source": "server", "client": None, "claimed_by": None,
                    "retry_of": None, "model": MODEL, "n": 1, "n_done": 0,
                    "progress": {}, "created": created, "finished": finished,
                    "stalled_for": None, "error": error},
            "input": input, "gen": Q.Queue.gen_for({"model": MODEL}),
            "result": None,
            "run": {"phase": "ended" if status in Q.FINISHED else status,
                    "started": started}}


def write_store(folder: Path, *rows, seq=None) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / Q.STORE_NAME).write_text(
        json.dumps({"seq": len(rows) if seq is None else seq, "jobs": list(rows)}),
        encoding="utf-8")


# ---------------------------------------------------------------- the frame

class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.folder = self.root / "work"
        self.folder.mkdir()
        self.said: list = []
        self.queues: list = []
        self.backends: list = []
        self.free_records = False       # a test that writes hand-made rows

    def tearDown(self):
        for b in self.backends:
            if hasattr(b, "open_all"):
                b.open_all()
        for q in self.queues:
            q.close(3.0)
        try:
            if not self.free_records:
                self.check_records()
        finally:
            self._tmp.cleanup()

    def check_records(self):
        for path in self.root.rglob(Q.STORE_NAME):
            doc = json.loads(path.read_text(encoding="utf-8"))
            for row in doc.get("jobs") or []:
                problems = V.check(row["job"], SHAPES["job"], SHAPES)
                self.assertEqual(problems, [], f"{row['job'].get('id')} in "
                                 f"{path.parent.name} is not a contract job record")
        # The public records, read by a fresh queue on each folder: the test's
        # own queues are closed by now, and a closed queue is bound to nothing.
        for path in self.root.rglob(Q.STORE_NAME):
            try:
                reader = Q.Queue(path.parent, start=False, log=lambda s: None)
            except Q.FolderInUse:
                continue                    # a queue the test left bound on purpose
            try:
                jobs = reader.jobs()
            except (Q.Refused, Q.StoreUnreadable):
                continue
            finally:
                reader.close()
            for j in jobs:
                self.assertEqual(V.check(j, SHAPES["job"], SHAPES), [],
                                 f"the public record of {j['id']}")

    # ------------------------------------------------------------ helpers
    def make(self, hooks=None, backend=None, *, folder=None, start=True, **cfg):
        b = backend if backend is not None else Backend()
        self.backends.append(b)
        q = Q.Queue(folder or self.folder, hooks if hooks is not None else App(),
                    b, start=start, log=self.said.append, **{**FAST, **cfg})
        self.queues.append(q)
        return q

    @staticmethod
    def submit(q, input=None, gen=None, **kw):
        g = {"model": MODEL}
        g.update(gen or {})
        kind = kw.pop("kind", "generate")
        return q.submit(kind, {"text": "Rain on the tin roof."} if input is None
                        else input, g, **kw)["job_id"]

    def wait_for(self, pred, what, timeout=6.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            got = pred()
            if got:
                return got
            time.sleep(0.01)
        self.fail(f"timed out after {timeout}s waiting for {what}")

    def status(self, q, jid):
        return q.get(jid)["status"]

    def wait_status(self, q, jid, *want, timeout=6.0):
        self.wait_for(lambda: self.status(q, jid) in want,
                      f"{jid} to be {'/'.join(want)}", timeout)
        return q.get(jid)

    def settle(self, q, *jids, timeout=6.0):
        for jid in jids:
            self.wait_status(q, jid, *Q.FINISHED, timeout=timeout)

    def idle(self, q, timeout=6.0):
        self.wait_for(lambda: q._worker_job is None, "the worker to be idle", timeout)

    @staticmethod
    def raw(q, jid):
        doc = json.loads(q.store.path.read_text(encoding="utf-8"))
        return next(r for r in doc["jobs"] if r["job"]["id"] == jid)

    @staticmethod
    def queued_order(q):
        return [j["id"] for j in sorted(q.jobs(), key=lambda j: j["queue_pos"])
                if j["status"] == "queued"]

    @staticmethod
    def crash(q):
        """The server stops where it stands: nothing it does afterwards may
        reach the store, exactly as a killed process writes nothing. A thread
        cannot be killed, so the supervisor's pass in flight is waited out -
        a real process that died would not finish its last swap either."""
        q._stop.set()
        q._wake.set()
        q.store = None
        if q._supervisor is not None:
            q._supervisor.join(3.0)
        # and the operating system lets go of a dead process's folder lock
        # (proven against a real killed process in the server's suite)
        if q._folder_lock is not None:
            q._folder_lock.release()
            q._folder_lock = None

    def refused(self, fn, *a, **kw) -> Q.Refused:
        with self.assertRaises(Q.Refused) as cm:
            fn(*a, **kw)
        return cm.exception


# =================================================== carried over: cancel

class CancelRows(Case):
    def test_cancelling_a_queued_job_ends_it(self):
        q = self.make(start=False)
        jid = self.submit(q)
        self.assertEqual(q.cancel(jid)["was"], "queued")
        self.assertEqual(self.status(q, jid), "cancelled")

    def test_cancelling_a_running_job_with_nothing_downstream_ends_it(self):
        # it has not reached the backend: the build hook is still working, so
        # nothing downstream exists to wait for - the cancel ends it now
        gate = threading.Event()
        backend = Backend()
        q = self.make(App(build=lambda job, after: gate.wait(5) and job["input"]),
                      backend)
        jid = self.submit(q)
        self.wait_status(q, jid, "running")
        out = q.cancel(jid)
        gate.set()
        self.assertEqual(out["was"], "running")
        self.assertEqual(out["job"]["status"], "cancelled",
                         "a running job that never reached the backend must not "
                         "stay running")
        self.idle(q)
        self.assertEqual(self.status(q, jid), "cancelled")

    def test_a_running_job_that_has_reached_the_backend_is_left_to_the_worker(self):
        backend = Backend()
        q = self.make(backend=backend, cancel_wait_s=0.2)
        jid = self.submit(q, {"gate": "g", "stubborn": True})
        self.wait_for(lambda: backend.calls, "the backend to be asked")
        out = q.cancel(jid)
        self.assertEqual(out["was"], "running")
        self.assertEqual(self.status(q, jid), "running",
                         "it is mid-generation; the worker unwinds it")
        self.assertTrue(self.raw(q, jid)["run"]["cancel"])
        self.assertEqual(backend.cancelled, [backend.calls[0]["handle"]],
                         "and the backend is told to stop its side")
        backend.gate("g").set()
        self.wait_status(q, jid, "cancelled")
        self.assertEqual(self.raw(q, jid)["run"]["ended_by"], "worker")

    def test_the_worker_looks_for_cancel_before_it_asks_the_backend(self):
        gate = threading.Event()
        backend = Backend()
        q = self.make(App(build=lambda job, after: gate.wait(5) and job["input"]),
                      backend)
        jid = self.submit(q)
        self.wait_status(q, jid, "running")
        q.cancel(jid)
        gate.set()
        self.idle(q)
        self.assertEqual(backend.calls, [], "it must not reach the model at all")
        self.assertEqual(self.status(q, jid), "cancelled")

    def test_cancelling_something_already_finished_is_a_no_op(self):
        q = self.make()
        jid = self.submit(q)
        self.wait_status(q, jid, "done")
        e = self.refused(q.cancel, jid)
        self.assertEqual((e.reason, e.extra["status"]), ("finished", "done"))
        self.assertEqual(self.status(q, jid), "done")

    def test_cancelling_a_job_that_does_not_exist_says_so(self):
        q = self.make(start=False)
        e = self.refused(q.cancel, "j-9999")
        self.assertEqual((e.reason, e.code), ("no_such_job", 404))


# ======================================== carried over: order and claiming

class QueueRows(Case):
    def test_one_running_job_holds_the_queue_which_is_why_wedges_matter(self):
        backend = Backend()
        q = self.make(backend=backend, start=False)
        a = self.submit(q, {"gate": "a"})
        b = self.submit(q)
        self.assertEqual(self.queued_order(q), [a, b])
        q.start()
        self.wait_for(lambda: backend.calls, "the first generation")
        self.assertEqual(self.queued_order(q), [b],
                         "a running job leaves the queue but still blocks the worker")
        time.sleep(0.15)
        self.assertEqual(self.status(q, b), "queued", "nothing starts beside it")
        backend.gate("a").set()
        self.settle(q, a, b)
        self.assertEqual([c["job"] for c in backend.calls], [a, b])

    def test_interactive_work_goes_before_batch(self):
        backend = Backend()
        q = self.make(backend=backend, start=False)
        batch = self.submit(q, priority="batch")
        inter = self.submit(q, priority="interactive")
        self.assertEqual(self.queued_order(q), [inter, batch])
        q.start()
        self.settle(q, batch, inter)
        self.assertEqual([c["job"] for c in backend.calls], [inter, batch])

    def test_a_claimed_result_cannot_be_claimed_twice(self):
        q = self.make(backend=Backend("t"))
        jid = self.submit(q)
        self.wait_status(q, jid, "done")
        self.assertEqual(q.result(jid)["candidates"][0]["text"], "t")
        self.assertEqual(q.claim(jid)["job"]["status"], "claimed")
        e = self.refused(q.claim, jid)
        self.assertEqual((e.reason, e.extra["status"]), ("claimed_elsewhere", "claimed"))

    def test_a_job_that_never_ran_cannot_be_claimed(self):
        q = self.make(start=False)
        jid = self.submit(q)
        e = self.refused(q.claim, jid)
        self.assertEqual((e.reason, e.extra["status"]), ("not_done", "queued"))
        self.assertIn("queued", e.error)


# ====================================== carried over: forgetting and pruning

class ForgetRows(Case):
    def finished(self, q, **inputs):
        """One job per requested end state, driven there for real."""
        ids = {}
        for status, inp in inputs.items():
            ids[status] = self.submit(q, inp)
        for jid in ids.values():
            self.settle(q, jid)
        return ids

    def test_a_finished_job_can_be_forgotten(self):
        q = self.make()
        jid = self.submit(q, {"fail": "could not build it"})
        self.wait_status(q, jid, "failed")
        self.assertEqual(q.forget(jid), {"forgot": jid})
        self.assertIsNone(q.get(jid))

    def test_a_queued_or_running_job_is_refused(self):
        q = self.make(start=False)
        jid = self.submit(q)
        e = self.refused(q.forget, jid)
        self.assertEqual(e.reason, "unfinished")
        self.assertIn("queued", e.error)
        q._pick(q._beat["gen"])                  # the worker takes it: running
        self.assertEqual(self.status(q, jid), "running")
        self.refused(q.forget, jid)
        self.assertIsNotNone(q.get(jid),
                             "forgetting a running job leaves it with nobody watching")

    def test_forgetting_something_that_is_gone_says_so(self):
        q = self.make(start=False)
        self.assertEqual(self.refused(q.forget, "j-9999").reason, "no_such_job")

    def test_prune_clears_only_what_is_finished(self):
        q = self.make()
        ids = self.finished(q, failed={"fail": "x"}, done={"text": "a"},
                            claimed={"text": "b"})
        q.claim(ids["claimed"])
        q.close()
        q2 = self.make(start=False)
        keepme = self.submit(q2)
        q2.cancel(self.submit(q2))
        self.assertEqual(q2.prune()["removed"], 4)
        self.assertIsNotNone(q2.get(keepme), "a queued job is never pruned")

    def test_prune_can_take_just_the_failures(self):
        q = self.make()
        ids = self.finished(q, failed={"fail": "x"}, done={"text": "a"})
        q.prune(statuses=["failed"])
        self.assertIsNone(q.get(ids["failed"]))
        self.assertIsNotNone(q.get(ids["done"]), "a good result must survive")

    def test_prune_keeps_the_most_recent_when_asked(self):
        q = self.make()
        ids = [self.submit(q, {"fail": f"x{i}"}) for i in range(4)]
        self.settle(q, *ids)
        q.prune(statuses=["failed"], keep=2)
        self.assertIsNone(q.get(ids[0]))
        self.assertIsNotNone(q.get(ids[-1]), "keep holds back what you were about to look at")


# ========================================== carried over: recovery at boot

class BootRows(Case):
    def test_a_running_job_that_never_reached_the_backend_is_requeued_on_boot(self):
        q1 = self.make(start=False)
        jid = self.submit(q1)
        q1._pick(q1._beat["gen"])                # running, and then the server stops
        self.assertEqual(self.raw(q1, jid)["job"]["status"], "running")
        self.crash(q1)
        q2 = self.make(start=False)
        q2.recover()
        self.assertEqual(self.status(q2, jid), "queued",
                         "it must not survive a restart still running")


# ================================= carried over: an unreadable job store

class Squeeze:
    def __init__(self, q):
        self.q, self.real = q, q.store.read_bytes

    def __enter__(self):
        def boom():
            raise MemoryError("cannot allocate 7.2 MB")
        self.q.store.read_bytes = boom
        return self

    def __exit__(self, *exc):
        self.q.store.read_bytes = self.real


class UnreadableStoreRows(Case):
    def test_an_unreadable_store_raises_rather_than_answering_empty(self):
        q = self.make(start=False)
        self.submit(q)
        with Squeeze(q), self.assertRaises(Q.StoreUnreadable):
            q.jobs()

    def test_a_submit_during_the_squeeze_leaves_every_job_on_disk(self):
        q = self.make(start=False)
        kept = [self.submit(q) for _ in range(3)]
        with Squeeze(q), self.assertRaises(Q.StoreUnreadable):
            self.submit(q)
        on_disk = json.loads(q.store.path.read_text(encoding="utf-8"))
        self.assertEqual([r["job"]["id"] for r in on_disk["jobs"]], kept,
                         "a store that could not be READ must not be REWRITTEN")

    def test_a_cancel_during_the_squeeze_leaves_every_job_on_disk(self):
        q = self.make(start=False)
        jid = self.submit(q)
        before = q.store.path.read_bytes()
        with Squeeze(q), self.assertRaises(Q.StoreUnreadable):
            q.cancel(jid)
        self.assertEqual(q.store.path.read_bytes(), before)

    def test_a_folder_that_has_never_run_a_job_still_reads_as_empty(self):
        q = self.make(start=False)
        self.assertFalse(q.store.path.exists())
        self.assertEqual(q.jobs(), [])
        self.assertEqual(q.store_fault()["count"], 0)

    def test_a_corrupt_store_still_reads_as_empty(self):
        write_store(self.folder)
        q = self.make(start=False)
        q.store.path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(q.jobs(), [])
        # DECIDED HERE, beyond the carried-over row: the bytes are kept beside
        # the new store rather than written over by the next save
        aside = list(self.folder.glob(Q.STORE_NAME + ".unreadable-*"))
        self.assertEqual([p.read_text(encoding="utf-8") for p in aside], ["{ not json"])

    def test_the_fault_is_recorded_and_counted(self):
        q = self.make(start=False)
        self.submit(q)
        with Squeeze(q):
            for _ in range(2):
                with self.assertRaises(Q.StoreUnreadable):
                    q.jobs()
        f = q.store_fault()
        self.assertEqual((f["kind"], f["count"]), ("MemoryError", 2),
                         "a repeat has to read as a repeat, not a fresh incident")
        self.assertGreater(f["at"], 0.0)
        self.assertIn("7.2 MB", f["detail"])

    def test_nothing_is_recorded_when_nothing_went_wrong(self):
        q = self.make(start=False)
        self.submit(q)
        q.jobs()
        self.assertEqual((q.store_fault()["kind"], q.store_fault()["count"]), ("", 0))

    def test_health_carries_the_fault_because_health_still_answers(self):
        q = self.make(start=False)
        self.submit(q)
        with Squeeze(q):
            with self.assertRaises(Q.StoreUnreadable):
                q.jobs()
            h = q.health()
        self.assertEqual(h["queue"]["store_fault"]["kind"], "MemoryError")
        json.dumps(h)

    def test_reading_the_record_cannot_reset_it(self):
        q = self.make(start=False)
        self.submit(q)
        with Squeeze(q), self.assertRaises(Q.StoreUnreadable):
            q.jobs()
        handed = q.store_fault()
        handed["kind"], handed["count"] = "", 0
        self.assertEqual((q.store_fault()["kind"], q.store_fault()["count"]),
                         ("MemoryError", 1))

    def test_a_memory_squeeze_does_not_kill_the_worker_loop(self):
        q = self.make(start=False)
        rounds = []

        def once(gen):
            rounds.append(1)
            q._stop.set()
            raise MemoryError("cannot allocate 7.2 MB")
        q._worker_cycle = once
        try:
            q._worker_loop(q._beat["gen"])           # must RETURN, not propagate
        finally:
            q._stop.clear()
        self.assertEqual(len(rounds), 1)
        self.assertGreater(q._beat["started_at"], 0.0)


# ============================================ carried over: supervision

class SupervisionRows(Case):
    def setUp(self):
        super().setUp()
        self.q = self.make(start=False)
        self.q._reap_given_up = lambda: None
        self.q._watch_stalls = lambda: None
        self.threads = []

    def tearDown(self):
        self.q._stop.set()
        for t in self.threads:
            t.join(5)
        super().tearDown()

    @staticmethod
    def dead_thread():
        t = threading.Thread(target=lambda: None, name="already-dead")
        t.start()
        t.join(5)
        return t

    def worker_end(self, boom):
        q = self.q

        def once(gen):
            q._stop.set()
            raise boom
        q._worker_cycle = once
        try:
            q._worker_loop(q._beat["gen"])
            return None
        except BaseException as e:                           # noqa: BLE001
            return type(e)
        finally:
            q._stop.clear()

    def run_supervisor(self, passes=2):
        q, n = self.q, [0]

        def pause(_s):
            n[0] += 1
            if n[0] >= passes:
                q._stop.set()
        q._pause = pause
        try:
            q._supervise_loop()
            return None
        except BaseException as e:                           # noqa: BLE001
            return type(e)
        finally:
            q._stop.clear()

    def test_only_the_non_exception_shapes_actually_end_the_worker(self):
        self.assertIsNone(self.worker_end(MemoryError("cannot allocate")))
        self.assertIsNone(self.worker_end(ValueError("x")))
        self.assertIs(self.worker_end(SystemExit(1)), SystemExit)
        self.assertIs(self.worker_end(KeyboardInterrupt()), KeyboardInterrupt)

    def test_a_worker_killed_the_way_it_really_dies_comes_back(self):
        q, gate, calls = self.q, threading.Event(), []

        def cycle(gen):
            calls.append(1)
            if len(calls) == 1:
                raise SystemExit("something in the cycle called sys.exit")
            gate.wait(5)
        q._worker_cycle = cycle
        q._start_worker()
        first = q._worker
        self.threads.append(first)
        first.join(5)
        self.assertFalse(first.is_alive(), "the kill has to be real")
        self.assertEqual(q._beat["gen"], 1)
        q._supervise_pass()
        second = q._worker
        self.threads.append(second)
        self.assertIsNot(second, first, "a restart is a NEW thread")
        self.assertTrue(second.is_alive())
        self.assertEqual((q._beat["restarts"], q._beat["gen"]), (1, 2))
        self.assertTrue(q.health()["worker"]["alive"])
        self.assertEqual(q.restart_fault()["kind"], "", "a restart that worked is not a fault")
        q._stop.set()
        gate.set()

    def test_a_live_worker_is_left_alone(self):
        q, gate = self.q, threading.Event()
        q._worker_cycle = lambda gen: gate.wait(5)
        q._start_worker()
        alive = q._worker
        self.threads.append(alive)
        q._supervise_pass()
        self.assertIs(q._worker, alive)
        self.assertEqual(q._beat["restarts"], 0)
        q._stop.set()
        gate.set()

    def test_a_restart_that_cannot_start_a_thread_does_not_end_supervision(self):
        self.q._worker = self.dead_thread()

        def cannot():
            raise RuntimeError("can't start new thread")
        self.q._start_worker = cannot
        self.assertIsNone(self.run_supervisor(passes=3),
                          "nothing may escape the loop that revives the queue")

    def test_a_restart_that_failed_is_not_counted_as_a_restart(self):
        self.q._worker = self.dead_thread()
        self.q._start_worker = lambda: (_ for _ in ()).throw(RuntimeError("no thread"))
        self.q._supervise_pass()
        self.assertEqual(self.q._beat["restarts"], 0)

    def test_a_failed_restart_is_recorded_where_health_can_show_it(self):
        self.q._worker = self.dead_thread()
        self.q._start_worker = lambda: (_ for _ in ()).throw(
            RuntimeError("can't start new thread"))
        self.q._supervise_pass()
        self.q._supervise_pass()
        w = self.q.health()["worker"]
        self.assertFalse(w["alive"])
        self.assertEqual((w["restart_fault"]["kind"], w["restart_fault"]["count"]),
                         ("RuntimeError", 2))
        self.assertIn("can't start new thread", w["restart_fault"]["detail"])
        self.assertGreater(w["restart_fault"]["at"], 0.0)
        json.dumps(w)

    def test_reading_the_restart_record_cannot_reset_it(self):
        self.q._worker = self.dead_thread()
        self.q._start_worker = lambda: (_ for _ in ()).throw(RuntimeError("no"))
        self.q._supervise_pass()
        self.q.restart_fault()["kind"] = ""
        self.assertEqual(self.q.restart_fault()["kind"], "RuntimeError")

    def test_a_failed_restart_says_so_out_loud(self):
        self.q._worker = self.dead_thread()
        self.q._start_worker = lambda: (_ for _ in ()).throw(RuntimeError("nope"))
        self.q._supervise_pass()
        lines = [s for s in self.said if "COULD NOT BE RESTARTED" in s]
        self.assertTrue(lines, self.said)
        self.assertEqual(lines[0], lines[0].encode("ascii", "replace").decode(),
                         "the line has to survive a console that is not UTF-8")

    def test_the_supervisor_tries_again_on_the_next_pass(self):
        q = self.q
        q._worker = self.dead_thread()
        tries, real = [], Q.Queue._start_worker.__get__(q)

        def flaky():
            tries.append(1)
            if len(tries) == 1:
                raise RuntimeError("can't start new thread")
            q._worker_cycle = lambda gen: q._stop.wait(5)
            real()
            self.threads.append(q._worker)
        q._start_worker = flaky
        self.assertIsNone(self.run_supervisor(passes=3))
        self.assertEqual(len(tries), 2, "it must try again, not give up")
        self.assertEqual(q._beat["restarts"], 1)

    def test_a_pass_that_breaks_anywhere_else_still_does_not_end_supervision(self):
        boom = []

        def bad_pass():
            boom.append(1)
            raise SystemExit("something at the top of the pass called sys.exit")
        self.q._supervise_pass = bad_pass
        self.assertIsNone(self.run_supervisor(passes=3))
        self.assertEqual(len(boom), 3, "it has to keep going round")
        f = self.q.restart_fault()
        self.assertEqual(f["kind"], "SystemExit")
        self.assertIn("supervisor pass", f["detail"])

    def test_a_reaper_that_dies_does_not_cost_the_worker_its_restart(self):
        self.q._worker = self.dead_thread()

        def reaper_exits():
            raise SystemExit("a library in the reaper called sys.exit")
        self.q._reap_given_up = reaper_exits
        started = []
        self.q._start_worker = lambda: started.append(1)
        self.q._supervise_pass()
        self.assertEqual(started, [1], "the restart has to happen in the SAME pass")

    def test_a_start_that_raises_leaves_the_generation_where_it_was(self):
        q = self.q
        q._worker_cycle = lambda gen: q._stop.wait(5)
        q._start_worker()
        self.threads.append(q._worker)
        live, gen = q._worker, q._beat["gen"]
        real = Q.threading.Thread

        class Refuses(real):
            def start(self):
                raise RuntimeError("can't start new thread")
        Q.threading.Thread = Refuses
        try:
            with self.assertRaises(RuntimeError):
                q._start_worker()
        finally:
            Q.threading.Thread = real
        self.assertEqual(q._beat["gen"], gen, "a generation that never ran is not reported")
        self.assertIs(q._worker, live, "and the record still names the one that did")

    def test_nothing_is_recorded_when_supervision_is_uneventful(self):
        q, gate = self.q, threading.Event()
        q._worker_cycle = lambda gen: gate.wait(5)
        q._start_worker()
        self.threads.append(q._worker)
        self.assertIsNone(self.run_supervisor(passes=3))
        self.assertEqual((q.restart_fault()["kind"], q.restart_fault()["count"]), ("", 0))
        self.assertEqual(q._beat["restarts"], 0)
        gate.set()

    def test_the_supervisor_does_not_restart_into_a_shutdown(self):
        self.q._worker = self.dead_thread()
        started = []
        self.q._start_worker = lambda: started.append(1)
        self.q._stop.set()
        try:
            self.q._supervise_pass()
        finally:
            self.q._stop.clear()
        self.assertEqual(started, [])


# =============================================== carried over: fault ages

class FaultAgeRows(Case):
    def setUp(self):
        super().setUp()
        self.q = self.make(start=False)

    def test_a_record_that_never_happened_has_no_age_at_all(self):
        h = self.q.health()
        self.assertIsNone(h["queue"]["store_fault"]["age_s"])
        self.assertIsNone(h["worker"]["restart_fault"]["age_s"])

    def test_a_fault_that_just_happened_is_seconds_old(self):
        self.q._note_store_fault("MemoryError", "7.2 MB")
        age = self.q.health()["queue"]["store_fault"]["age_s"]
        self.assertTrue(0.0 <= age < 5.0, age)

    def test_an_old_record_reads_as_old(self):
        self.q._note_store_fault("MemoryError", "7.2 MB")
        self.q._store_fault["at"] = time.time() - 240.0
        age = self.q.health()["queue"]["store_fault"]["age_s"]
        self.assertTrue(239.0 < age < 245.0, age)

    def test_both_records_are_aged_the_same_way(self):
        self.q._note_store_fault("MemoryError", "x")
        self.q._note_restart_fault("RuntimeError", "can't start new thread")
        h = self.q.health()
        self.assertEqual(sorted(h["queue"]["store_fault"]),
                         sorted(h["worker"]["restart_fault"]))
        self.assertIn("age_s", h["queue"]["store_fault"])

    def test_the_age_does_not_leak_into_the_sticky_record(self):
        self.q._note_store_fault("MemoryError", "x")
        self.q._note_restart_fault("RuntimeError", "y")
        self.assertEqual(sorted(self.q.store_fault()), ["at", "count", "detail", "kind"])
        self.assertEqual(sorted(self.q.restart_fault()), ["at", "count", "detail", "kind"])

    def test_aging_a_record_cannot_reset_it(self):
        self.q._note_store_fault("MemoryError", "x")
        self.q.health()["queue"]["store_fault"]["kind"] = ""
        self.assertEqual(self.q.store_fault()["kind"], "MemoryError")

    def test_aging_a_record_does_not_touch_the_record(self):
        self.q._note_store_fault("MemoryError", "x")
        aged = Q.Queue._aged(self.q._store_fault, time.time())
        self.assertIn("age_s", aged)
        self.assertNotIn("age_s", self.q._store_fault)
        aged["kind"] = "WIPED"
        self.assertEqual(self.q._store_fault["kind"], "MemoryError")

    def test_health_is_still_json_safe(self):
        self.q._note_store_fault("MemoryError", "x")
        self.q._note_restart_fault("RuntimeError", "y")
        json.dumps(self.q.health())

    def test_idle_s_cannot_tell_a_wedge_from_a_long_generation(self):
        # measured, not wished: idle_s is "since the worker last woke", so a
        # perfectly normal long generation climbs it exactly as a wedge would
        backend = Backend()
        q = self.make(backend=backend, folder=self.root / "long")
        self.submit(q, {"gate": "long"})
        self.wait_for(lambda: backend.calls, "the generation to start")
        q._beat["cycle_at"] = time.time() - 292.0
        h = q.health()
        self.assertTrue(h["worker"]["alive"])
        self.assertGreater(h["worker"]["idle_s"], 290.0)
        self.assertEqual(h["queue"]["store_fault"]["kind"], "",
                         "nothing is wrong here, and idle_s says 292 anyway")


# ================================ carried over: speaking on the worker path

class LoudRows(Case):
    def test_a_failing_reaper_says_so_through_the_real_supervisor_pass(self):
        q = self.make(start=False)
        q._reap_given_up = lambda: (_ for _ in ()).throw(OSError("disk gone"))
        q._worker, q._start_worker = None, (lambda: None)
        q._supervise_pass()
        self.assertTrue(any("OSError" in s and "disk gone" in s for s in self.said),
                        self.said)

    def test_a_failing_stall_watch_says_so_through_the_real_supervisor_pass(self):
        q = self.make(start=False)
        q._watch_stalls = lambda: (_ for _ in ()).throw(OSError("disk gone"))
        q._worker, q._start_worker = None, (lambda: None)
        q._supervise_pass()
        self.assertTrue(any("_watch_stalls" in s or "OSError" in s for s in self.said),
                        self.said)

    def test_a_second_worker_beside_a_live_one_says_so(self):
        q, gate = self.make(start=False), threading.Event()
        q._worker_cycle = lambda gen: gate.wait(5)
        q._start_worker()
        first = q._worker
        q._start_worker()
        gate.set()
        self.assertTrue(any("two_engines" in s and first.name in s for s in self.said),
                        self.said)

    def test_it_warns_rather_than_raises_so_a_restart_still_happens(self):
        q, gate = self.make(start=False), threading.Event()
        q._worker_cycle = lambda gen: gate.wait(5)
        q._start_worker()
        q._start_worker()                        # must not raise
        gate.set()
        self.assertEqual(q._beat["gen"], 2)

    def test_a_normal_restart_is_not_a_false_alarm(self):
        q = self.make(start=False)
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join(5)
        q._worker = dead
        q._worker_cycle = lambda gen: q._stop.wait(5)
        q._supervise_pass()
        self.assertFalse([s for s in self.said if "two_engines" in s])

    def test_a_document_that_is_not_a_store_is_named_rather_than_indexed(self):
        for i, doc in enumerate(([], [1, 2, 3], 7, None, "jobs", {"jobs": 5})):
            with self.subTest(doc=doc):
                folder = self.root / f"odd{i}"
                folder.mkdir()
                (folder / Q.STORE_NAME).write_text(json.dumps(doc), encoding="utf-8")
                q = self.make(start=False, folder=folder)
                self.assertEqual(q.jobs(), [])
                self.assertEqual(q.store_fault()["kind"], "unreadable")
                aside = list(folder.glob(Q.STORE_NAME + ".unreadable-*"))
                self.assertEqual(len(aside), 1, "the document is kept, never overwritten")
                self.assertEqual(json.loads(aside[0].read_text(encoding="utf-8")), doc)

    def test_the_read_rule_itself_is_untouched_by_the_shape_check(self):
        # a store that has lost its rows still answers - and keeps counting
        # from where it was rather than reusing an id
        write_store(self.folder, seq=3)
        (self.folder / Q.STORE_NAME).write_text(json.dumps({"seq": 3}), encoding="utf-8")
        q = self.make(start=False)
        self.assertEqual(q.jobs(), [])
        self.assertEqual(self.submit(q), "j-0004")
        self.assertTrue(q.health()["loaded"])

    def test_the_worker_names_a_store_that_is_not_a_store(self):
        q = self.make(start=False)
        q.store.path.write_text(json.dumps([{"id": "j1"}]), encoding="utf-8")
        q._worker_cycle(q._beat["gen"])           # must return, not raise
        self.assertTrue(any("list" in s and "not a job store" in s for s in self.said),
                        self.said)

    def test_a_real_store_is_picked_from_as_before(self):
        q = self.make(start=False)
        q.store.path.write_text(json.dumps({"seq": 0, "jobs": []}), encoding="utf-8")
        q._worker_cycle(q._beat["gen"])
        self.assertFalse([s for s in self.said if "not a job store" in s])


# =================================================== carried over: retries

COPY, CLEAN, SHORT = "COPY copy copy", "a clean part", "SHORT"


def copies(job, cand):
    """The application's verdict. Which text is refused, and why, is the
    application's business; the queue only counts, re-seeds and records."""
    if job["kind"] == "summary":
        return None
    t = cand["text"]
    return {"why": "copied" if t.startswith("COPY") else "short" if t == SHORT else None,
            "runs": t.count("copy")}


class RetryRows(Case):
    def run_job(self, *steps, gen=None, kind="prose"):
        backend = Backend(*steps)
        q = self.make(App(judge=copies), backend)
        jid = self.submit(q, {"text": "unused"}, gen=gen, kind=kind)
        self.settle(q, jid)
        return q, jid, backend

    def cand(self, q, jid, i=0):
        return q.result(jid)["candidates"][i]

    def test_dirty_then_clean_stops_at_two_and_the_clean_one_stands(self):
        q, jid, b = self.run_job(COPY, CLEAN)
        c = self.cand(q, jid)
        self.assertEqual(c["text"], CLEAN)
        self.assertEqual(len(b.calls), 2)
        self.assertEqual([a["why"] for a in c["attempts"]], ["copied", None])
        self.assertIsNone(c["verdict"]["why"], "the FINAL witness is the one kept")

    def test_three_dirty_stop_at_three_and_the_job_fails(self):
        # CHANGED ON PURPOSE (the judge's rule): the third used to stand,
        # red, and the job ended done with an answer its judge refused
        q, jid, b = self.run_job(COPY, COPY, COPY, CLEAN)
        self.assertEqual(len(b.calls), 3, "never a fourth")
        job = q.get(jid)
        self.assertEqual(job["status"], "failed", "an invalid last attempt never stands")
        self.assertEqual(job["error"], "the judge refused the last attempt (3 of 3): copied")
        self.assertEqual(q.result(jid)["candidates"], [], "the refused answer is not offered")

    def test_a_clean_first_try_never_retries(self):
        q, jid, b = self.run_job(CLEAN, CLEAN)
        self.assertEqual(len(b.calls), 1)
        self.assertEqual(len(self.cand(q, jid)["attempts"]), 1)

    def test_each_refused_attempt_records_why_it_was_refused(self):
        q, jid, _ = self.run_job(COPY, SHORT, CLEAN)
        self.assertEqual([a["why"] for a in self.cand(q, jid)["attempts"]],
                         ["copied", "short", None])

    def test_a_kind_the_application_does_not_judge_never_retries(self):
        q, jid, b = self.run_job(COPY, CLEAN, kind="summary")
        self.assertEqual(len(b.calls), 1)
        self.assertNotIn("attempts", self.cand(q, jid))

    def test_retries_zero_on_the_request_turns_it_off(self):
        # CHANGED ON PURPOSE (the judge's rule): the one attempt is the last,
        # so its refusal fails the job - it used to stand, as the candidate
        q, jid, b = self.run_job(COPY, CLEAN, gen={"retries": 0})
        self.assertEqual(len(b.calls), 1)
        self.assertEqual((q.get(jid)["status"], q.get(jid)["error"]),
                         ("failed", "the judge refused the last attempt (1 of 1): copied"))

    def test_the_seed_moves_on_every_attempt_so_a_retry_is_not_a_replay(self):
        _, _, b = self.run_job(COPY, COPY, CLEAN, gen={"seed": 7})
        self.assertEqual([c["params"]["seed"] for c in b.calls], [7, 107, 207])

    def test_no_seed_stays_no_seed(self):
        _, _, b = self.run_job(COPY, CLEAN)
        self.assertEqual([c["params"]["seed"] for c in b.calls], [None, None])

    def test_every_attempt_is_its_own_sample(self):
        q, jid, b = self.run_job(COPY, COPY, CLEAN)
        self.assertEqual([c["handle"] for c in b.calls],
                         [Q.ids.handle(q.actor, jid, n) for n in (1, 2, 3)])
        self.assertEqual([a["sample"] for a in self.cand(q, jid)["attempts"]], [1, 2, 3])

    def test_a_second_variant_numbers_its_attempts_after_the_first(self):
        q, jid, b = self.run_job(COPY, CLEAN, CLEAN, gen={"n": 2, "seed": 7})
        self.assertEqual([c["handle"][-2:] for c in b.calls], ["01", "02", "03"])
        self.assertEqual([c["params"]["seed"] for c in b.calls], [7, 107, 8])
        self.assertEqual([self.cand(q, jid, i)["sample"] for i in (0, 1)], [2, 3])

    def test_a_cancel_mid_retry_is_a_cancel(self):
        q, jid, b = self.run_job(COPY, Q.Cancelled("cancelled"))
        self.assertEqual(self.status(q, jid), "cancelled")
        self.assertEqual(len(b.calls), 2)

    def test_the_final_candidate_is_the_one_the_worker_hands_on(self):
        q, jid, _ = self.run_job(COPY, CLEAN)
        c = self.cand(q, jid)
        self.assertTrue({"text", "attempts", "verdict"} <= set(c))
        self.assertIsNone(c["attempts"][-1]["why"])


class NativeJudgeRows(Case):
    """THE JUDGE'S RULE: an attempt the application's judge refuses is asked
    again with a new seed, and a refused LAST attempt fails its job - it
    never stands. What was refused stays in the request's record; the job's
    error says why, in the judge's own words."""

    def run_job(self, *steps, gen=None, app=None):
        backend = Backend(*steps)
        q = self.make(app or App(judge=copies), backend)
        jid = self.submit(q, {"text": "unused"}, gen=gen, kind="prose")
        self.settle(q, jid)
        self.idle(q)
        return q, jid, backend

    def test_a_clean_last_attempt_stands(self):
        q, jid, b = self.run_job(COPY, COPY, CLEAN)
        self.assertEqual((len(b.calls), q.get(jid)["status"]), (3, "done"))
        (c,) = q.result(jid)["candidates"]
        self.assertEqual((c["text"], [a["why"] for a in c["attempts"]]),
                         (CLEAN, ["copied", "copied", None]))

    def test_a_job_its_judge_failed_says_so_in_its_reason(self):
        """A program branches on `reason`, never on the error's words: a job
        its judge refused to the last attempt ends with a reason of its own,
        in the store and in every answer."""
        q, jid, _ = self.run_job(COPY, COPY, COPY)
        self.assertEqual((q.get(jid)["status"], q.get(jid).get("reason")),
                         ("failed", "judge_refused"))
        self.assertEqual(self.raw(q, jid)["job"].get("reason"), "judge_refused",
                         "the store does not keep it")

    def test_a_refused_last_attempt_is_never_landed_and_is_told_failed(self):
        app = EndedApp(judge=copies)
        q, jid, _ = self.run_job(COPY, COPY, COPY, app=app)
        self.assertEqual(app.landed, [], "the application never takes a refused answer")
        self.assertEqual(app.told, [(jid, "failed",
                                     "the judge refused the last attempt (3 of 3): copied")])

    def test_what_waits_on_it_is_never_built_from_the_refused_answer(self):
        app, backend = App(judge=copies), Backend(COPY, COPY, COPY)
        q = self.make(app, backend)
        first = self.submit(q, {"text": "unused"}, kind="prose")
        then = self.submit(q, {"text": "the next part"}, kind="prose", after=[first])
        self.settle(q, first, then)
        self.idle(q)
        self.assertEqual(q.get(then)["status"], "cancelled")
        self.assertIn(first, q.get(then)["error"])
        self.assertNotIn(then, [jid for jid, _ in app.built], "it was never built")
        self.assertEqual(len(backend.calls), 3)

    def test_a_later_variant_refused_to_the_last_fails_the_job_and_no_variant_follows(self):
        q, jid, b = self.run_job(CLEAN, COPY, COPY, COPY, CLEAN, gen={"n": 3})
        self.assertEqual(len(b.calls), 4, "variant 3 is never asked for")
        job = q.get(jid)
        self.assertEqual((job["status"], job["error"], job["n_done"]),
                         ("failed", "the judge refused the last attempt (3 of 3) of variant 2 "
                                    "of 3: copied", 1))
        self.assertEqual([c["text"] for c in q.result(jid)["candidates"]], [CLEAN],
                         "what was accepted before it is kept, as with any failure")

    def test_the_record_keeps_every_refused_attempt_and_says_why_the_job_failed(self):
        q, jid, _ = self.run_job(COPY, SHORT, COPY)
        rec = Q.request_record(self.folder, q.get(jid)["correlation_id"])
        ends = [e for e in rec["events"] if e["event"] == "attempt_ended"]
        self.assertEqual([(e["sample"], e["outcome"], e["verdict"]["why"]) for e in ends],
                         [(1, "refused", "copied"), (2, "refused", "short"),
                          (3, "refused", "copied")])
        self.assertEqual([rec["samples"][s]["candidate.json"]["text"]
                          for s in ("01", "02", "03")],
                         [COPY, SHORT, COPY], "what each refused attempt answered is kept")
        last = rec["events"][-1]
        self.assertEqual((last["event"], last["status"], last["error"]),
                         ("ended", "failed",
                          "the judge refused the last attempt (3 of 3): copied"))


# ======================================= carried over: one generation

class GenerationRows(Case):
    def test_a_plain_success_comes_back_with_the_text(self):
        backend = Backend("THE GENERATED PROSE.")
        q = self.make(backend=backend)
        jid = self.submit(q)
        self.wait_status(q, jid, "done")
        c = q.result(jid)["candidates"][0]
        self.assertEqual(c["text"], "THE GENERATED PROSE.")
        self.assertEqual(c["handle"], backend.calls[0]["handle"],
                         "the candidate names the generation that made it")

    def test_a_failed_generation_is_a_failure_never_a_cancel(self):
        q = self.make(backend=Backend(Q.GenerationFailed("CUDA OOM")))
        jid = self.submit(q)
        j = self.wait_status(q, jid, "failed", "cancelled")
        self.assertEqual(j["status"], "failed")
        self.assertIn("CUDA OOM", j["error"])

    def test_done_but_empty_is_a_failure_not_an_empty_success(self):
        q = self.make(backend=Backend("   "))
        jid = self.submit(q)
        j = self.wait_status(q, jid, "failed", "done")
        self.assertEqual(j["status"], "failed")
        self.assertIn("nothing", j["error"].lower())

    def test_a_failure_is_never_the_cancel_signal(self):
        # the two are different classes, so a fault can never reach the owner
        # as "cancelled" - his own doing - with the backend's reason thrown away
        self.assertFalse(issubclass(Q.GenerationFailed, Q.Cancelled))
        self.assertFalse(issubclass(Q.Cancelled, Q.GenerationFailed))
        q = self.make(backend=Backend(Q.GenerationFailed("the model server exited")))
        jid = self.submit(q)
        j = self.wait_status(q, jid, "failed", "cancelled")
        self.assertEqual((j["status"], j["error"]), ("failed", "the model server exited"))

    def test_a_backend_timeout_mid_generation_fails_the_job_rather_than_hanging(self):
        q = self.make(backend=Backend(TimeoutError("read timed out")))
        jid = self.submit(q)
        j = self.wait_status(q, jid, "failed", "cancelled")
        self.assertEqual(j["status"], "failed")
        self.assertIn("TimeoutError", j["error"])

    def test_a_cancel_mid_generation_also_cancels_the_backend(self):
        backend = Backend()
        q = self.make(backend=backend)
        jid = self.submit(q, {"gate": "g"})
        self.wait_for(lambda: backend.calls, "the generation to start")
        q.cancel(jid)
        self.wait_status(q, jid, "cancelled")
        self.assertEqual(backend.cancelled, [backend.calls[0]["handle"]],
                         "the queue never told the backend to stop")


class MultiVariantRows(Case):
    def test_a_partial_multi_variant_job_fails_and_keeps_what_it_made(self):
        # the job fails and names the variant that fell over; DECIDED HERE:
        # the variant that already succeeded is kept and readable, not claimable
        q = self.make(backend=Backend("VARIANT 1.", Q.GenerationFailed("variant 2 fell over")))
        jid = self.submit(q, gen={"n": 3})
        j = self.wait_status(q, jid, "failed", "done")
        self.assertEqual(j["status"], "failed")
        self.assertIn("variant 2", j["error"])
        self.assertEqual([c["text"] for c in q.result(jid)["candidates"]], ["VARIANT 1."])
        self.assertEqual(j["n_done"], 1)
        # a failed job ENDED: `not_done` would tell the claimant to wait
        self.assertEqual(self.refused(q.claim, jid).reason, "ended")

    def test_a_healthy_multi_variant_job_keeps_every_variant(self):
        q = self.make(backend=Backend("V1.", "V2.", "V3."))
        jid = self.submit(q, gen={"n": 3})
        self.wait_status(q, jid, "done")
        self.assertEqual([c["text"] for c in q.result(jid)["candidates"]],
                         ["V1.", "V2.", "V3."])

    def test_spent_accumulates_so_the_progress_bar_does_not_restart(self):
        q = self.make(backend=Backend({"text": "x", "tokens": 40},
                                      {"text": "y", "tokens": 40}))
        jid = self.submit(q, gen={"n": 2})
        j = self.wait_status(q, jid, "done")
        self.assertEqual(j["progress"]["spent"], 80)


class NativeCardTermsRows(Case):
    """What a job says about waiting for the card goes to the backend with
    each of its generations: its class (the job's priority) and its own
    patience. A job that ends for a reason of its own says which - for a
    program, beside the error a person reads."""

    @staticmethod
    def terms(seen):
        return lambda request, params, run: seen.append(
            (run.job_id, run.priority, run.card_patience_s)) or "answered"

    def test_each_generation_is_handed_its_jobs_priority_and_own_patience(self):
        seen = []
        q = self.make(backend=Backend(*[self.terms(seen)] * 3))
        a = self.submit(q, priority="batch", card_patience_s=12.5)
        b = self.submit(q, priority="interactive", card_patience_s=0)
        c = self.submit(q)
        self.settle(q, a, b, c)
        # CHANGED ON PURPOSE (S18b, PLAN-4 D0 "class default RESPONSIVE"): a
        # job that names no priority is live - interactive - not batch
        self.assertEqual(sorted(seen), sorted([(a, "batch", 12.5), (b, "interactive", 0),
                                               (c, "interactive", None)]),
                         "a job's own patience - 0 included - or its priority was lost")

    def test_a_patience_below_0_or_not_a_number_is_refused(self):
        q = self.make(start=False)
        e = self.refused(self.submit, q, card_patience_s=-1)
        self.assertEqual((e.reason, e.code), ("bad_patience", 400))
        for wrong, kind in (("soon", "string"), (True, "boolean")):
            e = self.refused(self.submit, q, card_patience_s=wrong)
            self.assertEqual((e.reason, e.code, e.error),
                             ("bad_request", 400,
                              f"$.card_patience_s: expected number, got {kind}"))
        self.assertEqual(q.jobs(), [], "a refused job was queued")

    def test_a_job_that_ends_for_a_reason_of_its_own_says_which(self):
        q = self.make(backend=Backend(Q.GenerationFailed(
            "no card within its patience of 5s (card_patience)", reason="card_patience")))
        jid = self.submit(q)
        j = self.wait_status(q, jid, "failed", "done")
        self.assertEqual((j["status"], j.get("reason")), ("failed", "card_patience"))
        self.assertIn("card_patience", j["error"])
        self.assertEqual(self.raw(q, jid)["job"].get("reason"), "card_patience",
                         "the reason is not on the record")

    def test_no_reason_of_its_own_is_none_and_one_the_contract_does_not_declare_is_not_kept(self):
        q = self.make(backend=Backend(Q.GenerationFailed("CUDA OOM"),
                                      Q.GenerationFailed("x", reason="made_up"), "fine"))
        a, b, c = self.submit(q), self.submit(q), self.submit(q)
        self.settle(q, a, b, c)
        self.assertEqual([self.status(q, x) for x in (a, b, c)], ["failed", "failed", "done"])
        for x in (a, b, c):
            self.assertNotIn("reason", q.get(x))

    def test_a_retry_keeps_the_jobs_own_patience_and_starts_without_a_reason(self):
        seen = []
        q = self.make(backend=Backend(Q.GenerationFailed("busy (card_busy)", reason="card_busy"),
                                      self.terms(seen)))
        jid = self.submit(q, priority="interactive", card_patience_s=0)
        self.wait_status(q, jid, "failed")
        again = q.retry(jid)["job"]
        self.assertNotIn("reason", again)
        self.settle(q, again["id"])
        self.assertEqual(seen, [(again["id"], "interactive", 0)])
        self.assertEqual(self.status(q, again["id"]), "done")


def card_full(why="the card is degraded: a model's log said the card's memory ran out",
              **numbers) -> Q.GenerationFailed:
    """An attempt the card's memory broke, as a backend that may hold the job
    for the card says it (S5b): `card_degraded`, with the card's numbers."""
    failed = Q.GenerationFailed(why, reason="card_degraded")
    failed.card_full = {"free_mib": 1229, "need_mib": 6554, "outside_mib": 10035, **numbers}
    return failed


class NativeCardFullRows(Case):
    """S5b, THE CARD FULL RECOVERED BY ITSELF (the owner, 2026-09-27: he
    closes programs, and the request goes through by itself): an attempt the
    card's memory broke is thrown away and run again from the start - never
    counted as the model's failure, against its retries or its judge - the
    backend holding the run again until the card has room, within the job's
    patience counted from the first break; the record says so; and a card
    that fills again each time fails the job after `recover_attempts`."""

    def test_an_attempt_the_card_broke_runs_again_by_itself_and_the_job_is_done(self):
        seen = []

        def answered(request, params, run):
            seen.append((run.sample, params.get("seed"), run.card_waited_s))
            return "the story, whole"
        backend = Backend(card_full(), answered)
        q = self.make(App(judge=copies), backend)
        jid = self.submit(q, {"text": "unused"}, gen={"retries": 0, "seed": 42}, kind="prose")
        j = self.wait_status(q, jid, "done", "failed")
        self.assertEqual((j["status"], j.get("reason")), ("done", None), j.get("error"))
        (c,) = q.result(jid)["candidates"]
        self.assertEqual((c["text"], c["sample"]), ("the story, whole", 2))
        self.assertEqual([a["attempt"] for a in c["attempts"]], [1],
                         "the attempt the card broke was counted against its judge")
        self.assertEqual(seen[0][:2], (2, 42), "it was not run again from the start, the "
                                               "same seed")
        self.assertIsInstance(seen[0][2], float, "the run again did not carry how long the "
                                                 "job has been held for the card")
        self.assertGreaterEqual(seen[0][2], 0.0)

    def test_the_record_says_which_attempt_the_card_broke_when_and_what_it_read(self):
        q = self.make(backend=Backend(card_full(), "the story, whole"))
        jid = self.submit(q)
        self.wait_status(q, jid, "done", "failed")
        self.idle(q)
        rec = Q.request_record(self.folder, q.get(jid)["correlation_id"])
        full = [e for e in rec["events"] if e["event"] == "attempt_card_full"]
        self.assertEqual(len(full), 1, rec["events"])
        (f,) = full
        self.assertEqual((f["job"], f["sample"], f["attempt"], f["free_mib"], f["need_mib"],
                          f["outside_mib"]), (jid, 1, 1, 1229, 6554, 10035))
        self.assertIn("the card's memory ran out", f["error"])
        started = [e["sample"] for e in rec["events"] if e["event"] == "attempt_started"]
        self.assertEqual(started, [1, 2], "the run again is not an attempt of its own")
        order = [e["event"] for e in rec["events"]
                 if e["event"] in ("attempt_ended", "attempt_card_full", "attempt_started")]
        self.assertEqual(order, ["attempt_started", "attempt_ended", "attempt_card_full",
                                 "attempt_started", "attempt_ended"])

    def test_a_card_that_fills_again_each_time_fails_after_recover_attempts_naming_each(self):
        for most in (3, 1):
            with self.subTest(recover_attempts=most):
                backend = Backend(*[card_full()] * (most + 1), "never reached")
                (self.root / f"fills-{most}").mkdir(exist_ok=True)
                q = self.make(backend=backend, folder=self.root / f"fills-{most}",
                              **({"recover_attempts": most} if most != 3 else {}))
                jid = self.submit(q)
                j = self.wait_status(q, jid, "failed", "done")
                self.assertEqual((j["status"], j.get("reason")), ("failed", "card_degraded"))
                self.assertEqual(len(backend.calls), most + 1,
                                 "held again more, or fewer, times than recover_attempts")
                self.assertIn(f"{most + 1} times, and it is not held again after {most}",
                              j["error"])
                for i in range(1, most + 2):
                    self.assertIn(f"attempt {i} ended at ", j["error"])
                self.idle(q)
                rec = Q.request_record(self.root / f"fills-{most}", j["correlation_id"])
                self.assertEqual(len([e for e in rec["events"]
                                      if e["event"] == "attempt_card_full"]), most,
                                 "a line for the attempt it was not held after")

    def test_a_card_degraded_failure_the_backend_will_not_hold_fails_at_once(self):
        backend = Backend(Q.GenerationFailed("the card is degraded", reason="card_degraded"),
                          "never reached")
        q = self.make(backend=backend)
        jid = self.submit(q)
        j = self.wait_status(q, jid, "failed", "done")
        self.assertEqual((j["status"], j.get("reason"), len(backend.calls)),
                         ("failed", "card_degraded", 1))

    def test_a_cancel_while_the_job_is_held_for_the_card_ends_it(self):
        def held(request, params, run):
            while not run.stopped():
                time.sleep(0.01)
            raise Q.Cancelled("cancelled while it waited for the card's memory")
        backend = Backend(card_full(), held)
        q = self.make(backend=backend)
        jid = self.submit(q)
        self.wait_for(lambda: len(backend.calls) == 2, "the run again")
        q.cancel(jid)
        j = self.wait_status(q, jid, "cancelled", "failed", "done")
        self.assertEqual(j["status"], "cancelled")


class WorkerCannotDieRows(Case):
    def test_an_unexpected_exception_does_not_kill_the_worker(self):
        q = self.make(start=False)
        calls = []

        def boom(gen):
            calls.append(1)
            if len(calls) == 1:
                raise PermissionError(5, "Access is denied")
            q._stop.set()
        q._worker_cycle = boom
        t = threading.Thread(target=q._worker_loop, args=(q._beat["gen"],), daemon=True)
        t.start()
        t.join(8.0)
        q._stop.clear()
        self.assertFalse(t.is_alive(), "the worker never came back round")
        self.assertGreaterEqual(len(calls), 2, "it exited on the first exception")

    def test_a_transient_replace_failure_is_retried_not_fatal(self):
        q = self.make(start=False)
        tries, real = [], q.store.replace

        def flaky(src, dst):
            tries.append(1)
            if len(tries) < 3:
                raise PermissionError(5, "Access is denied")
            return real(src, dst)
        q.store.replace = flaky
        q.store.save({"seq": 7, "jobs": []})
        self.assertEqual(len(tries), 3)
        self.assertEqual(json.loads(q.store.path.read_text(encoding="utf-8"))["seq"], 7)

    def test_a_replace_that_never_succeeds_still_raises(self):
        q = self.make(start=False)
        q.store.replace = lambda *_: (_ for _ in ()).throw(PermissionError(5, "denied"))
        with self.assertRaises(PermissionError):
            q.store.save({"seq": 1, "jobs": []})
        self.assertEqual(list(self.folder.glob("*.tmp")), [], "no temporary file is left")


# ================================ carried over: recovery against the backend

class RecoverRows(Case):
    def zombie(self, n=1, **gen):
        """A generation in flight when the server stopped."""
        backend = Backend()
        q1 = self.make(backend=backend)
        jid = self.submit(q1, {"gate": "hold", "stubborn": True, "quiet": True},
                          gen={"n": n, **gen})
        self.wait_for(lambda: backend.calls, "the generation to reach the backend")
        self.crash(q1)
        return jid

    def test_adopt_a_running_job_whose_backend_work_finished(self):
        jid = self.zombie()
        app = App()
        q2 = self.make(app, Backend(recover=lambda h: {"text": f"finished as {h}"}),
                       start=False)
        self.assertEqual(q2.recover()["adopted"], 1)
        j = q2.get(jid)
        self.assertEqual(j["status"], "done")
        self.assertRegex(q2.result(jid)["candidates"][0]["text"],
                         rf"^finished as srv-[a-z2-9]{{8}}:{jid}#01$",
                         "the backend is asked about the handle the stopped server used")
        self.assertEqual([x[0] for x in app.landed], [jid],
                         "an adopted result is landed like any other")

    def test_a_job_that_asked_for_no_answer_is_adopted_with_the_none_it_finished_with(self):
        """S10b RG5: `max_tokens` 0 asks for no answer, so an empty one is what
        the job asked for (`test_a_job_that_asks_for_no_answer_is_done_with_
        none_never_failed_as_empty`) - but a recovered empty answer was read as
        nothing recovered, and the job ran again."""
        jid = self.zombie(max_tokens=0)
        app = App()
        q2 = self.make(app, Backend(recover=lambda h: {"text": ""}), start=False)
        self.assertEqual(q2.recover()["adopted"], 1)
        self.assertEqual(self.status(q2, jid), "done")
        self.assertEqual([c["text"] for c in q2.result(jid)["candidates"]], [""])
        self.assertEqual([x[0] for x in app.landed], [jid])

    def test_an_empty_answer_recovered_for_a_job_that_asked_for_one_is_nothing_recovered(self):
        """...while for any other length an empty answer is still a failure,
        so a recovered one is nothing recovered: the job goes back to the
        queue."""
        jid = self.zombie(max_tokens=50)
        q2 = self.make(backend=Backend(recover=lambda h: {"text": "  "}), start=False)
        self.assertEqual(q2.recover()["adopted"], 0)
        self.assertEqual(self.status(q2, jid), "queued")

    def test_a_running_job_whose_backend_work_did_not_finish_is_not_left_running(self):
        jid = self.zombie()
        q2 = self.make(backend=Backend(recover=lambda h: None), start=False)
        q2.recover()
        self.assertEqual(self.status(q2, jid), "queued")

    def test_an_unreachable_backend_still_leaves_no_job_running(self):
        jid = self.zombie()

        def down(h):
            raise ConnectionError("the backend is down")
        q2 = self.make(backend=Backend(recover=down), start=False)
        q2.recover()
        self.assertNotEqual(self.status(q2, jid), "running")

    def test_a_queued_job_is_left_alone_by_recovery(self):
        q1 = self.make(start=False)
        jid = self.submit(q1)
        self.crash(q1)
        q2 = self.make(backend=Backend(recover=lambda h: {"text": "no"}), start=False)
        q2.recover()
        self.assertEqual(self.status(q2, jid), "queued")

    def test_one_wedged_job_cannot_block_every_future_generation(self):
        jid = self.zombie()
        backend = Backend(recover=lambda h: (_ for _ in ()).throw(ConnectionError("down")))
        backend.healthy = True
        q2 = self.make(backend=backend)
        self.assertFalse([j for j in q2.jobs() if j["status"] == "running"
                          and j["id"] == jid and q2._worker_job is None
                          and not backend.calls])
        after = self.submit(q2)
        self.settle(q2, jid, after)
        self.assertEqual(self.status(q2, after), "done", "the queue is still wedged")


class CancelRaceRows(Case):
    def test_a_cancel_that_lands_while_the_generation_is_starting(self):
        # the narrow window: the cancel arrives from another thread while the
        # backend is being asked. Exactly one generation starts and exactly one
        # cancel reaches the backend - never a started generation left running
        box = {}

        def slow_start(request, params, run):
            t = threading.Thread(target=lambda: box.setdefault("out", q.cancel(run.job_id)))
            t.start()
            t.join(3.0)
            if run.stopped():
                raise Q.Cancelled("the backend saw the cancel")
            return {"text": "too late"}
        backend = Backend(slow_start)
        q = self.make(backend=backend, cancel_wait_s=0.2)
        jid = self.submit(q)
        self.wait_status(q, jid, "cancelled", "done")
        self.assertEqual(self.status(q, jid), "cancelled")
        self.assertEqual(len(backend.calls), 1, "the job never reached the backend")
        self.assertEqual(backend.cancelled, [backend.calls[0]["handle"]],
                         "a generation was STARTED and never cancelled")

    def test_a_cancel_that_arrives_after_the_job_is_done_changes_nothing(self):
        backend = Backend("THE GENERATED PROSE.")
        q = self.make(backend=backend)
        jid = self.submit(q)
        self.wait_status(q, jid, "done")
        self.refused(q.cancel, jid)
        self.assertEqual(q.result(jid)["candidates"][0]["text"], "THE GENERATED PROSE.")
        self.assertEqual(backend.cancelled, [])


# ============================================ carried over: queue health

class HealthRows(Case):
    def health(self, *rows):
        if rows:
            write_store(self.folder, *rows)
        q = self.make(start=False)
        h = q.health()["queue"]
        for k in ("last", "consecutive_failures", "queued", "running"):
            self.assertIn(k, h)
        return h

    def last(self, *rows):
        h = self.health(*rows)
        self.assertIsNotNone(h["last"], "expected a terminal job, got none")
        return h["last"]

    # nothing has happened yet
    def test_a_server_that_has_never_generated_anything_says_so(self):
        h = self.health(fixture_row("j-0001", "queued", started=None))
        self.assertIsNone(h["last"], "no outcome is not the same as a good one")
        self.assertEqual((h["consecutive_failures"], h["running"]), (0, 0))

    def test_a_store_that_was_never_written_answers_the_same_way(self):
        self.assertFalse((self.folder / Q.STORE_NAME).exists())
        h = self.health()
        self.assertIsNone(h["last"])
        self.assertEqual(h["consecutive_failures"], 0)

    def test_a_job_held_for_its_check_is_counted_apart_from_the_queued_and_running(self):
        """H8 (S15 RS2): a held job waits on a person - health says so, and
        never counts it as queued or running work."""
        h = self.health(fixture_row("j-0001", "held", started=None),
                        fixture_row("j-0002", "held", started=None),
                        fixture_row("j-0003", "queued", started=None),
                        fixture_row("j-0004", "running", started=60.0))
        self.assertEqual((h.get("held"), h["queued"], h["running"]), (2, 1, 1))

    def test_nothing_held_is_zero_held(self):
        self.assertEqual(self.health(fixture_row("j-0001", "queued", started=None))["held"], 0)

    # the last outcome
    def test_the_last_good_generation_is_reported_with_its_job_and_thread(self):
        last = self.last(fixture_row("j-0001", "done", 1000.0, correlation_id="c-20260923-140012-a3k9m2p7"))
        self.assertEqual((last["status"], last["error"], last["at"], last["job"],
                          last["correlation_id"]),
                         ("done", None, 1000.0, "j-0001", "c-20260923-140012-a3k9m2p7"))

    def test_a_failure_carries_the_error_text_the_owner_needs_to_see(self):
        last = self.last(fixture_row("j-0007", "failed", 1000.0,
                                     error="port 8901 is already in use"))
        self.assertEqual((last["status"], last["error"], last["job"]),
                         ("failed", "port 8901 is already in use", "j-0007"))

    def test_the_newest_outcome_wins_even_when_that_job_started_first(self):
        h = self.health(fixture_row("j-0001", "failed", 3000.0, error="boom", created=1.0),
                        fixture_row("j-0002", "done", 2000.0, created=2.0))
        self.assertEqual(h["last"]["job"], "j-0001", "ordered by when it ENDED")
        self.assertEqual(h["consecutive_failures"], 1)

    def test_two_jobs_that_finished_in_the_same_tick_resolve_to_the_later_one(self):
        t = 1755000000.123456
        h = self.health(fixture_row("j-0001", "failed", t, error="boom"),
                        fixture_row("j-0002", "done", t))
        self.assertEqual(h["last"]["job"], "j-0002")
        self.assertEqual(h["consecutive_failures"], 0)

    def test_the_same_tie_the_other_way_round_reports_the_failure(self):
        t = 1755000000.123456
        h = self.health(fixture_row("j-0002", "done", t),
                        fixture_row("j-0001", "failed", t, error="boom"))
        self.assertEqual(h["last"]["job"], "j-0001")
        self.assertEqual(h["consecutive_failures"], 1)

    def test_a_terminal_job_with_no_finished_stamp_cannot_be_the_last_one(self):
        h = self.health(fixture_row("j-0001", "failed", 1000.0, error="boom"),
                        fixture_row("j-0002", "done", None))
        self.assertEqual(h["last"]["job"], "j-0001",
                         "an unstamped success must not hide a real failure")
        self.assertEqual(h["consecutive_failures"], 1)

    def test_a_stamp_that_is_not_a_number_is_ignored_rather_than_crashing(self):
        self.free_records = True               # a hand-edited row, on purpose
        bad = fixture_row("j-0002", "failed", error="boom")
        bad["job"]["finished"] = "2026-08-25T13:54:05"
        self.assertEqual(self.last(fixture_row("j-0001", "done", 1000.0), bad)["job"],
                         "j-0001")

    # the failure streak
    def test_one_failure_counts_as_one(self):
        self.assertEqual(self.health(fixture_row("j-0001", "failed", 1000.0, error="b"))
                         ["consecutive_failures"], 1)

    def test_three_failures_in_a_row_count_as_three(self):
        self.assertEqual(self.health(*[fixture_row(f"j-000{i}", "failed", 1000.0 + i,
                                                   error="x") for i in (1, 2, 3)])
                         ["consecutive_failures"], 3)

    def test_one_success_afterwards_clears_the_streak(self):
        h = self.health(fixture_row("j-0001", "failed", 1000.0, error="a"),
                        fixture_row("j-0002", "failed", 1100.0, error="b"),
                        fixture_row("j-0003", "done", 1200.0))
        self.assertEqual((h["consecutive_failures"], h["last"]["status"]), (0, "done"))

    def test_failures_from_before_a_success_are_not_counted_again(self):
        h = self.health(fixture_row("j-0001", "failed", 1000.0, error="a"),
                        fixture_row("j-0002", "done", 1100.0),
                        fixture_row("j-0003", "failed", 1200.0, error="c"))
        self.assertEqual(h["consecutive_failures"], 1)

    def test_a_cancellation_stops_the_walk_back(self):
        h = self.health(fixture_row("j-0001", "failed", 1000.0, error="a"),
                        fixture_row("j-0002", "cancelled", 1100.0),
                        fixture_row("j-0003", "failed", 1200.0, error="c"))
        self.assertEqual((h["consecutive_failures"], h["last"]["status"]), (1, "failed"))

    def test_a_failure_that_never_finished_is_not_counted(self):
        h = self.health(fixture_row("j-0001", "done", 1000.0),
                        fixture_row("j-0002", "failed", None, error="boom"))
        self.assertEqual((h["consecutive_failures"], h["last"]["job"]), (0, "j-0001"))

    def test_work_still_in_flight_does_not_break_a_streak(self):
        h = self.health(fixture_row("j-0001", "failed", 1000.0, error="a"),
                        fixture_row("j-0002", "failed", 1100.0, error="b"),
                        fixture_row("j-0003", "running"),
                        fixture_row("j-0004", "queued", started=None))
        self.assertEqual(h["consecutive_failures"], 2)

    # work in flight
    def test_queued_and_running_work_is_counted(self):
        h = self.health(fixture_row("j-0001", "done", 1000.0),
                        fixture_row("j-0002", "running"),
                        fixture_row("j-0003", "queued", started=None),
                        fixture_row("j-0004", "queued", started=None))
        self.assertEqual((h["running"], h["queued"]), (1, 2))

    def test_in_flight_work_is_never_mistaken_for_an_outcome(self):
        h = self.health(fixture_row("j-0002", "running"),
                        fixture_row("j-0003", "queued", started=None))
        self.assertIsNone(h["last"])
        self.assertEqual((h["consecutive_failures"], h["running"], h["queued"]), (0, 1, 1))

    # it never goes dark
    def test_a_corrupt_job_store_still_answers(self):
        (self.folder / Q.STORE_NAME).write_text("{not json at all", encoding="utf-8")
        self.assertIsNone(self.health()["last"])

    def test_a_job_store_with_no_jobs_key_still_answers(self):
        (self.folder / Q.STORE_NAME).write_text(json.dumps({"seq": 3}), encoding="utf-8")
        self.assertIsNone(self.health()["last"])

    def test_a_job_row_missing_every_field_still_answers(self):
        self.free_records = True                 # rubbish rows, left on purpose
        (self.folder / Q.STORE_NAME).write_text(
            json.dumps({"seq": 2, "jobs": [{}, "rubbish", None]}), encoding="utf-8")
        h = self.health()
        self.assertIsNone(h["last"])
        self.assertEqual(h["queued"], 0)
        kept = json.loads((self.folder / Q.STORE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(kept["jobs"], [{}, "rubbish", None], "health wrote nothing")

    # the real fields, through the real submit and the real worker
    def test_a_real_failed_job_is_seen_through_the_fields_the_worker_writes(self):
        q = self.make(backend=Backend(Q.GenerationFailed("the model server exited with code 1")))
        jid = self.submit(q)
        self.wait_status(q, jid, "failed")
        h = q.health()["queue"]
        self.assertEqual((h["last"]["job"], h["last"]["status"], h["last"]["error"]),
                         (jid, "failed", "the model server exited with code 1"))
        self.assertEqual(h["consecutive_failures"], 1)

    def test_a_real_jobs_thread_id_travels_with_the_outcome(self):
        q = self.make()
        jid = self.submit(q)
        corr = q.get(jid)["correlation_id"]
        self.assertTrue(corr)
        self.wait_status(q, jid, "done")
        self.assertEqual(q.health()["queue"]["last"]["correlation_id"], corr)

    def test_a_queued_job_shows_up_as_queued_work(self):
        q = self.make(start=False)
        self.submit(q)
        h = q.health()["queue"]
        self.assertEqual(h["queued"], 1)
        self.assertIsNone(h["last"], "queuing something is not an outcome")

    def test_claiming_a_finished_job_leaves_it_the_last_good_outcome(self):
        q = self.make()
        jid = self.submit(q)
        self.wait_status(q, jid, "done")
        q.claim(jid)
        h = q.health()["queue"]
        self.assertEqual((h["last"]["job"], h["last"]["status"], h["consecutive_failures"]),
                         (jid, "claimed", 0))

    # the answer health gives
    def test_health_carries_the_queue_block_read_from_the_real_store(self):
        write_store(self.folder,
                    fixture_row("j-0042", "failed", 1000.0, error="port 8901 is in use"),
                    fixture_row("j-0043", "queued", started=None))
        h = self.make(start=False).health()["queue"]
        self.assertEqual((h["last"]["job"], h["last"]["error"], h["consecutive_failures"],
                          h["queued"], h["running"]),
                         ("j-0042", "port 8901 is in use", 1, 1, 0))

    def test_health_is_json_safe_so_a_page_can_read_it(self):
        write_store(self.folder, fixture_row("j-0001", "done", 1000.0))
        h = json.loads(json.dumps(self.make(start=False).health()))
        self.assertEqual(h["queue"]["last"]["job"], "j-0001")


# ===================================== carried over: reading never claims

RICH = {"text": "The bolt gave.", "model": MODEL, "tokens": 689,
        "finish_reason": "stop", "verdict": {"why": None, "runs": 0},
        "sample": 1, "seed": 4242, "handle": "set by the queue",
        "temperature": 0.8, "tokens_per_second": 41.2, "error": None,
        "payload_sha256": "a" * 64}


class PeekRows(Case):
    def finished(self):
        q = self.make(backend=Backend(dict(RICH)))
        jid = self.submit(q)
        self.wait_status(q, jid, "done")
        return q, jid

    def test_a_finished_job_is_read_with_one_candidate(self):
        q, jid = self.finished()
        self.assertEqual(len(q.result(jid)["candidates"]), 1)
        self.assertEqual(self.status(q, jid), "done", "reading is not claiming")

    def test_the_read_loses_nothing_the_candidate_holds(self):
        q, jid = self.finished()
        got = q.result(jid)["candidates"][0]
        self.assertEqual(sorted(set(RICH) - set(got)), [])

    def test_specifically_the_fields_that_have_gone_missing_before(self):
        q, jid = self.finished()
        got = q.result(jid)["candidates"][0]
        for k in ("verdict", "sample", "seed"):
            with self.subTest(field=k):
                self.assertEqual(got.get(k), RICH[k])

    def test_values_are_carried_through_unchanged_not_merely_present(self):
        q, jid = self.finished()
        got = q.result(jid)["candidates"][0]
        self.assertEqual({k: got.get(k) for k in RICH if k != "handle"},
                         {k: v for k, v in RICH.items() if k != "handle"})

    def test_the_queue_drops_nothing_and_adds_only_its_own_bookkeeping(self):
        q, jid = self.finished()
        self.assertEqual(set(q.result(jid)["candidates"][0]), set(RICH))

    def test_sabotage_a_narrowed_read_is_caught(self):
        q, jid = self.finished()
        real = q.result

        def narrowed(job_id):
            out = real(job_id)
            out["candidates"] = [{k: c[k] for k in ("text", "model", "tokens",
                                                    "finish_reason")}
                                 for c in out["candidates"]]
            return out
        lost = sorted(set(RICH) - set(narrowed(jid)["candidates"][0]))
        self.assertIn("sample", lost, "if nothing is lost the check is decoration")
        self.assertIn("seed", lost)

    def test_no_candidates_no_crash(self):
        q = self.make(start=False)
        self.assertEqual(q.result(self.submit(q))["candidates"], [])


class NativeResultWaitRows(Case):
    """A result asked with `wait_s` waits - at most that long - for its job to
    end, however it ends, and then answers as without it: an application
    waits for its answer without asking again and again."""

    @staticmethod
    def ask_aside(q, jid, wait_s):
        """The result asked on a thread of its own. -> (thread, box): once it
        is answered, the box holds the answer (or the refusal's reason) and
        when it came."""
        box: dict = {}

        def ask():
            try:
                box["answer"] = q.result(jid, wait_s=wait_s)
            except Q.Refused as e:
                box["answer"] = e.reason
            box["at"] = time.monotonic()
        t = threading.Thread(target=ask, daemon=True)
        t.start()
        return t, box

    def test_a_wait_answers_the_moment_its_job_ends(self):
        backend = Backend()
        q = self.make(backend=backend)
        jid = self.submit(q, {"gate": "hold", "text": "held"})
        self.wait_for(lambda: backend.calls, "the generation to start")
        t, box = self.ask_aside(q, jid, 10.0)
        time.sleep(0.3)                          # its ask is in, and waiting
        opened = time.monotonic()
        backend.gate("hold").set()
        t.join(8.0)
        self.assertLess(box.get("at", float("inf")) - opened, 3.0, "it waited out its wait")
        self.assertEqual((box["answer"]["job"]["status"],
                          [c["text"] for c in box["answer"]["candidates"]]), ("done", ["held"]))

    def test_a_wait_is_answered_the_moment_its_job_is_held_for_its_check(self):
        """R-B3: a job held for a person's check - nothing more comes of it
        until it is sent - answered a wait only when the wait ran out (a
        batch paid all of it: 5.03 s of 5). A wait is answered at held."""
        q = self.make(backend=Describing())
        jid = self.submit(q, check=True)
        t0 = time.monotonic()
        t, box = self.ask_aside(q, jid, 10.0)
        t.join(8.0)
        self.assertLess(box.get("at", float("inf")) - t0, 3.0, "it waited out its wait")
        self.assertEqual(box["answer"]["job"]["status"], "held")

    def test_a_cancel_ends_a_wait_too(self):
        q = self.make(start=False)
        jid = self.submit(q)
        t, box = self.ask_aside(q, jid, 10.0)
        time.sleep(0.3)
        cancelled = time.monotonic()
        q.cancel(jid)
        t.join(8.0)
        self.assertLess(box.get("at", float("inf")) - cancelled, 3.0, "it waited out its wait")
        self.assertEqual(box["answer"]["job"]["status"], "cancelled")

    def test_a_wait_that_runs_out_answers_as_without_it(self):
        q = self.make(start=False)
        jid = self.submit(q)
        t0 = time.monotonic()
        t, box = self.ask_aside(q, jid, 0.4)
        t.join(5.0)                              # bounded: a wait without one must not hang
        took = box.get("at", float("inf")) - t0
        self.assertTrue(0.35 <= took < 3.0, f"{took:.2f}s for a wait of 0.4")
        self.assertEqual(box["answer"], q.result(jid))

    def test_a_job_that_has_ended_or_is_not_there_is_answered_at_once(self):
        q = self.make()
        jid = self.submit(q)
        self.settle(q, jid)
        t0 = time.monotonic()
        self.assertEqual(q.result(jid, wait_s=30.0)["job"]["status"], "done")
        with self.assertRaises(Q.Refused) as e:
            q.result("j-9999", wait_s=30.0)
        self.assertEqual(e.exception.reason, "no_such_job")
        self.assertLess(time.monotonic() - t0, 3.0)

    def test_a_queue_that_closes_ends_a_wait(self):
        q = self.make(start=False)
        jid = self.submit(q)
        t, box = self.ask_aside(q, jid, 30.0)
        time.sleep(0.3)
        closed = time.monotonic()
        q.close(3.0)
        t.join(8.0)
        self.assertLess(box.get("at", float("inf")) - closed, 3.0, "the close left it waiting")


# ========================================== carried over: what gen keeps

PROBE = {"model": "probe-model", "n": 3, "max_tokens": 777, "temperature": 0.11,
         "seed": 4242, "retries": 0}


def _sample(shape, i: int):
    """A value *shape* takes - a value of its own for each `i`, so no two
    parameters share one, and False as well as True among the booleans."""
    shape = V.deref(shape, SHAPES)
    if isinstance(shape, list):
        return _sample(shape[-1], i)
    if shape == "integer":
        return 1000 + i
    if shape == "number":
        return i + 0.25
    if shape == "boolean":
        return i % 2 == 0
    if shape in ("string", "any"):
        return f"value-{i}"
    if "enum" in shape:
        return shape["enum"][-1]
    if "array" in shape:
        return [_sample(shape["array"], i)]
    optional = set(shape.get("optional", ()))
    out = {n: _sample(s, i) for n, s in shape["object"].items() if n not in optional}
    return out if out or not shape.get("open") else {f"key-{i}": f"value-{i}"}


def every_parameter() -> dict:
    """A value for every request parameter a job's `gen` may name, read off
    the contract (jobs-submit's `gen`) - the queue's own three aside, which
    are the queue's and never the model's. Each is of its own kind and its
    own value, so a row can tell one lost, renamed or changed on the way."""
    gen = ROUTES["server/jobs-submit"]["request"]["object"]["gen"]["object"]
    return {name: _sample(gen[name], i) for i, name in enumerate(sorted(gen))
            if name not in ("model", "n", "retries")}


def same(a, b) -> bool:
    """One JSON value, written the same way: Python's `True == 1` would let a
    boolean sent on as a number pass for the one the application named."""
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


class GenFieldRows(Case):
    def stored_gen(self, gen):
        q = self.make(start=False)
        try:
            jid = q.submit("generate", None, gen)["job_id"]
            return self.raw(q, jid)["gen"]
        finally:
            q.close()          # one live queue per folder: the next call binds it again

    def test_a_bare_gen_gets_exactly_the_always_fields(self):
        self.assertEqual(set(Q.Queue.gen_for({})), set(Q.GEN_DEFAULTS))

    def test_an_if_set_field_is_absent_when_not_asked_for(self):
        for name in Q.GEN_IF_SET:
            self.assertNotIn(name, Q.Queue.gen_for({}))

    def test_retries_zero_is_kept_because_zero_is_a_real_answer(self):
        self.assertEqual(Q.Queue.gen_for({"retries": 0}).get("retries"), 0)

    def test_retries_is_clamped_to_the_queue_maximum(self):
        self.assertEqual(Q.Queue.gen_for({"retries": 99}).get("retries"), Q.RETRY_MAX)

    def test_every_declared_field_crosses_the_front_door(self):
        gen = self.stored_gen(PROBE)
        for name in list(Q.GEN_DEFAULTS) + list(Q.GEN_IF_SET):
            with self.subTest(field=name):
                self.assertEqual(gen.get(name), PROBE[name])

    def test_each_field_crosses_alone_as_well(self):
        for name in list(Q.GEN_DEFAULTS) + list(Q.GEN_IF_SET):
            if name == "model":
                continue
            with self.subTest(field=name):
                gen = self.stored_gen({"model": "m", name: PROBE[name]})
                self.assertEqual(gen.get(name), PROBE[name])

    def test_a_field_not_in_the_table_does_not_reach_the_job(self):
        # CHANGED ON PURPOSE (every text parameter): a field the
        # contract's gen does not name was dropped without a word, and the
        # job made - a misspelt parameter looked as if it took effect. It is
        # refused at the door now, and no job is made of it.
        q = self.make(start=False)
        with self.assertRaises(Q.Refused) as caught:
            q.submit("generate", None, {"model": "m", "nonsense": "MARK-JUNK"})
        self.assertEqual((caught.exception.reason, caught.exception.code),
                         ("bad_request", 400))
        self.assertEqual(q.jobs(), [], "a job was made of a refused request")

    def test_an_untouched_request_gets_the_defaults_and_no_temperature(self):
        # an existing application's queue stamped temperature 0.7 here; by the
        # owner's ruling of 2026-09-23 an untouched request names none, and
        # the model's own temperature applies
        self.assertEqual(self.stored_gen({"model": "m"}),
                         {"model": "m", "n": 1, "max_tokens": 600, "seed": None})


class NativeTemperatureRows(Case):
    """The queue stamps no temperature: the model's own applies."""

    def test_a_job_that_names_no_temperature_sends_none_to_the_backend(self):
        backend = Backend()
        q = self.make(backend=backend)
        jid = self.submit(q)
        self.settle(q, jid)
        self.assertNotIn("temperature", backend.calls[0]["params"])

    def test_a_job_that_names_one_sends_it_zero_included(self):
        backend = Backend()
        q = self.make(backend=backend)
        warm, cold = self.submit(q, gen={"temperature": 1.3}), self.submit(q, gen={"temperature": 0})
        self.settle(q, warm, cold)
        self.assertEqual([c["params"]["temperature"] for c in backend.calls], [1.3, 0.0])


class NativeRequestParameterRows(Case):
    """EVERY TEXT PARAMETER: what a job's `gen` may name is the
    contract's list - every parameter llama-server's chat route reads that is
    the application's to set - each optional, each kept and handed to the
    backend exactly as it was named. One the list does not name is refused
    at the door, never dropped without a word and never sent; so is one of
    the wrong kind, in the contract's own words."""

    def test_every_parameter_the_contract_names_is_kept_and_handed_on_as_it_was_named(self):
        backend = Backend()
        q = self.make(backend=backend)
        named = every_parameter()
        jid = self.submit(q, gen=named)
        self.settle(q, jid)
        self.assertEqual(self.status(q, jid), "done")
        stored, sent = self.raw(q, jid)["gen"], backend.calls[0]["params"]
        for name, value in named.items():
            with self.subTest(parameter=name):
                self.assertTrue(same(stored.get(name), value), f"stored {stored.get(name)!r}")
                self.assertTrue(same(sent.get(name), value), f"handed on {sent.get(name)!r}")

    def test_a_parameter_given_as_none_is_unset(self):
        backend = Backend()
        q = self.make(backend=backend)
        jid = self.submit(q, gen={"top_k": None, "json_schema": None})
        self.settle(q, jid)
        self.assertEqual(self.status(q, jid), "done")
        self.assertFalse({"top_k", "json_schema"} & set(backend.calls[0]["params"]))

    def test_a_parameter_the_contract_does_not_name_is_refused_in_its_words(self):
        q = self.make(start=False)
        with self.assertRaises(Q.Refused) as caught:
            q.submit("generate", None, {"model": MODEL, "temprature": 0.7})
        self.assertEqual((caught.exception.reason, caught.exception.error),
                         ("bad_request", '$.gen: unexpected field "temprature"'))
        self.assertEqual(q.jobs(), [])

    def test_a_parameter_of_the_wrong_kind_is_refused_in_its_words(self):
        q = self.make(start=False)
        with self.assertRaises(Q.Refused) as caught:
            q.submit("generate", None, {"model": MODEL, "top_k": "forty"})
        self.assertEqual((caught.exception.reason, caught.exception.error),
                         ("bad_request", "$.gen.top_k: expected integer, got string"))

    def test_a_gen_that_names_no_model_is_no_model_never_bad_request(self):
        """The list's check leaves a missing model to its own refusal, as the
        server's door does: a program branching on `no_model` still can."""
        q = self.make(start=False)
        for gen in (None, {}, {"top_k": 5}):
            with self.subTest(gen=gen):
                with self.assertRaises(Q.Refused) as caught:
                    q.submit("generate", None, gen)
                self.assertEqual(caught.exception.reason, "no_model")

    def test_what_the_applications_sampling_names_is_handed_on_over_the_jobs_own(self):
        backend = Backend()
        q = self.make(App(sampling=lambda kind, input: {"top_k": 20, "cache_prompt": False}),
                      backend)
        jid = self.submit(q, gen={"top_k": 40})
        self.settle(q, jid)
        params = backend.calls[0]["params"]
        self.assertEqual((params["top_k"], params["cache_prompt"]), (20, False))

    def test_a_parameter_the_applications_sampling_names_that_the_contract_does_not_is_never_sent(
            self):
        backend = Backend()
        q = self.make(App(sampling=lambda kind, input: {"topk": 20}), backend)
        jid = self.submit(q)
        job = self.wait_status(q, jid, *Q.FINISHED)
        self.assertEqual((job["status"], job["error"]),
                         ("failed", 'could not choose the sampling: $.gen: unexpected field "topk"'))
        self.assertEqual(backend.calls, [], "the backend was asked with a parameter no model reads")

    def test_a_sampling_that_names_the_queues_own_fails_the_job_before_the_backend_is_asked(self):
        """The job's record names the model it runs, how many answers and how
        often a refused one is asked again - as it was submitted. A sampling
        that named another model would run the job on a model its record
        does not name, so it fails the job, saying which, and nothing runs."""
        for name, value in (("model", "other-7b"), ("n", 3), ("retries", 0)):
            with self.subTest(named=name):
                backend = Backend()
                folder = self.root / f"sampling-{name}"
                folder.mkdir()
                q = self.make(App(sampling=lambda kind, input, n=name, v=value: {n: v}), backend,
                              folder=folder)
                jid = self.submit(q)
                job = self.wait_status(q, jid, *Q.FINISHED)
                self.assertEqual((job["status"], job["error"]),
                                 ("failed", f'could not choose the sampling: it names "{name}", '
                                            f"which is the job's own, as it was submitted - a "
                                            f"sampling names the model's parameters, never the "
                                            f"queue's"))
                self.assertEqual(backend.calls, [], "the backend was asked")


class NativeCountRows(Case):
    """What the queue does with a text request's length: the answer a job
    asked for none of, the budget a job with no limit has, and the reason a
    request too long for its model's context ends with."""

    def test_a_job_that_asks_for_no_answer_is_done_with_none_never_failed_as_empty(self):
        """`max_tokens` 0 reads the prompt into the cache and asks for no
        answer (llama-server's README): an empty answer is what was asked
        for. With any other length an empty answer is still a failure
        (`test_done_but_empty_is_a_failure_not_an_empty_success`)."""
        q = self.make(backend=Backend(""))
        jid = self.submit(q, gen={"max_tokens": 0})
        job = self.wait_status(q, jid, *Q.FINISHED)
        self.assertEqual((job["status"], job["error"]), ("done", None))
        self.assertEqual([c["text"] for c in q.result(jid)["candidates"]], [""])

    def test_a_job_that_names_no_limit_has_no_budget_never_a_negative_one(self):
        """The budget is the most a job's answers can take - `max_tokens` for
        each of its `n` - which a page fills against. -1 names no limit, so
        there is no most: no budget, never -n."""
        q = self.make(backend=Backend())
        for gen, want in (({"max_tokens": -1, "n": 2}, None), ({"max_tokens": 50, "n": 2}, 100),
                          ({"max_tokens": 0}, 0), ({}, 600)):
            with self.subTest(gen=gen):
                jid = self.submit(q, gen=gen)
                self.settle(q, jid)
                job = q.get(jid)
                self.assertEqual(job["status"], "done")
                self.assertEqual(job["progress"].get("budget"), want)
                self.assertEqual(V.check(job, SHAPES["job"], SHAPES), [])

    def test_a_length_llama_server_refuses_is_refused_at_the_submit(self):
        """S10b RG3: llama-server takes a `max_tokens` of -1 up to 2147483647
        (b10724: n_predict's hard limits) and refuses any other. Accepted at
        the submit, such a job waited for the card and its model before the
        model refused it - and had it been too long for its context, the
        count's words would have said it named no limit. It is refused where
        it is asked, `bad_max_tokens`, and never sent."""
        q = self.make(start=False)
        for most in (-2, -600, 2 ** 31, -2.0):
            with self.subTest(max_tokens=most):
                e = self.refused(self.submit, q, gen={"max_tokens": most})
                self.assertEqual((e.reason, e.code), ("bad_max_tokens", 400))
                self.assertIn("$.gen.max_tokens", e.error)
                self.assertIn(str(int(most)), e.error)
        for most in (-1, 0, 1, 2 ** 31 - 1, 5.0):
            with self.subTest(max_tokens=most):
                self.assertTrue(self.submit(q, gen={"max_tokens": most}))

    def test_a_sampling_that_names_a_length_llama_server_refuses_fails_the_job_unsent(self):
        """...and a sampling the application's hook chooses is held to the
        same range: the job fails before anything is asked of the model."""
        backend = Backend()
        q = self.make(App(sampling=lambda kind, input: {"max_tokens": -2}), backend)
        jid = self.submit(q, gen={"max_tokens": 50})
        job = self.wait_status(q, jid, *Q.FINISHED)
        self.assertEqual(job["status"], "failed")
        self.assertIn("max_tokens", job["error"])
        self.assertEqual(backend.calls, [], "a length llama-server refuses was sent")

    #: H5's register 4: every other parameter a text request names that
    #: llama-server holds to hard limits (b10724's server-schema.cpp,
    #: `set_hard_limits`) -> (a value just outside, a value just inside, the
    #: other end). Ints are read as 32 bits, so none takes more than 2147483647
    #: (t_max_predict_ms is read as 64); adaptive_decay is compared as a float
    OUTSIDE = {"n_indent": (-1, 0, 2 ** 31 - 1), "n_keep": (-2, -1, 2 ** 31 - 1),
               "n_discard": (-1, 0, 2 ** 31 - 1), "n_cache_reuse": (-1, 0, 2 ** 31 - 1),
               "t_max_predict_ms": (-2, -1, 2 ** 63 - 1), "repeat_last_n": (-1, 0, 2 ** 31 - 1),
               "dry_allowed_length": (-1, 0, 2 ** 31 - 1),
               "dry_penalty_last_n": (-1, 0, 2 ** 31 - 1), "min_keep": (-1, 0, 2 ** 31 - 1),
               "reasoning_budget_tokens": (-2, -1, 2 ** 31 - 1),
               "adaptive_decay": (-0.01, 0.0, 0.99)}
    #: ...and past the top end, where the other end is not the most a number holds
    ABOVE = {"n_indent": 2 ** 31, "n_keep": 2 ** 31, "n_discard": 2 ** 31,
             "n_cache_reuse": 2 ** 31, "t_max_predict_ms": 2 ** 63, "repeat_last_n": 2 ** 31,
             "dry_allowed_length": 2 ** 31, "dry_penalty_last_n": 2 ** 31, "min_keep": 2 ** 31,
             "reasoning_budget_tokens": 2 ** 31, "adaptive_decay": 0.995}

    def test_every_other_hard_limit_llama_server_holds_is_refused_at_the_submit(self):
        """H5's register 4: n_keep below -1, n_indent below 0 and the rest -
        llama-server refuses each (400) - were accepted at the submit, so the
        job waited for the card and its model before the model refused it.
        Each is refused where it is asked, `out_of_range`, its words naming
        the parameter, its range and the value, and never sent; each end of
        each range is taken. `max_tokens` keeps its own reason."""
        q = self.make(start=False)
        self.assertEqual(set(self.OUTSIDE), set(self.ABOVE))
        for key, (out, low, high) in self.OUTSIDE.items():
            self.assertIn(key, Q.GEN_FIELDS, "a parameter the contract does not name")
            for bad in (out, self.ABOVE[key]):
                with self.subTest(key=key, value=bad):
                    e = self.refused(self.submit, q, gen={key: bad})
                    self.assertEqual((e.reason, e.code), ("out_of_range", 400), e.error)
                    self.assertIn(f"$.gen.{key}", e.error)
                    self.assertIn(f"not {bad}", e.error)
            for good in (low, high):
                with self.subTest(key=key, value=good):
                    self.assertTrue(self.submit(q, gen={key: good}))
        # adaptive_decay is a float llama-server holds in single precision: a
        # value that is 0.99 there is taken, though it is above 0.99 here
        self.assertTrue(self.submit(q, gen={"adaptive_decay": 0.9900000001}))

    def test_a_sampling_that_names_a_value_llama_server_refuses_fails_the_job_unsent(self):
        """...and a sampling the application's hook chooses is held to the
        same ranges: the job fails before anything is asked of the model."""
        backend = Backend()
        q = self.make(App(sampling=lambda kind, input: {"n_keep": -2}), backend)
        jid = self.submit(q, gen={"n_keep": 4})
        job = self.wait_status(q, jid, *Q.FINISHED)
        self.assertEqual(job["status"], "failed")
        self.assertIn("$.gen.n_keep", job["error"])
        self.assertEqual(backend.calls, [], "a value llama-server refuses was sent")

    def test_a_length_the_sampling_unsets_is_budgeted_as_the_one_the_runner_asks_for(self):
        """A sampling that names `max_tokens` None unsets it: the runner asks
        for the bootstrap's own, 600, so that is the budget - never 0."""
        backend = Backend()
        q = self.make(App(sampling=lambda kind, input: {"max_tokens": None}), backend)
        jid = self.submit(q, gen={"max_tokens": 50})
        self.settle(q, jid)
        self.assertIsNone(backend.calls[0]["params"]["max_tokens"])
        self.assertEqual(q.get(jid)["progress"].get("budget"), 600)

    def test_a_request_too_long_for_its_models_context_ends_with_its_own_reason(self):
        """A program branches on `reason`: a request that does not fit its
        model's context ends `prompt_too_long`, the contract's, in every
        answer and in the store."""
        said = ("the request does not fit demo-7b's context of 64 tokens: the prompt is 70 "
                "tokens as the model reads it, and its answer needs room for 600 more "
                "(max_tokens 600) - 670 in all")
        q = self.make(backend=Backend(Q.GenerationFailed(said, reason="prompt_too_long")))
        jid = self.submit(q)
        job = self.wait_status(q, jid, *Q.FINISHED)
        self.assertEqual((job["status"], job["error"], job.get("reason")),
                         ("failed", said, "prompt_too_long"))
        self.assertEqual(self.raw(q, jid)["job"].get("reason"), "prompt_too_long",
                         "the store does not keep it")
        self.assertEqual(V.check(job, SHAPES["job"], SHAPES), [])


class RetriesAtTheFrontDoorRows(Case):
    def refusing(self, gen):
        backend = Backend()
        q = self.make(App(judge=lambda job, c: {"why": "refused"}), backend)
        jid = self.submit(q, gen=gen)
        self.settle(q, jid)
        return q, jid, backend

    def test_retries_zero_survives_the_submit(self):
        q, jid, _ = self.refusing({"retries": 0})
        self.assertEqual(self.raw(q, jid)["gen"]["retries"], 0)

    def test_the_stored_zero_makes_the_generator_take_one_attempt(self):
        _, _, b = self.refusing({"retries": 0})
        self.assertEqual(len(b.calls), 1)

    def test_omitting_retries_still_means_the_full_ladder(self):
        q, jid, b = self.refusing({})
        self.assertNotIn("retries", self.raw(q, jid)["gen"])
        self.assertEqual(len(b.calls), Q.RETRY_MAX)

    def test_a_silly_value_is_clamped_rather_than_trusted(self):
        q, jid, _ = self.refusing({"retries": 99})
        self.assertEqual(self.raw(q, jid)["gen"]["retries"], Q.RETRY_MAX)


CLIENT_ID, OTHER_CLIENT = "cli-w5r8t2h6", "cli-b2x7q4n8"
MINE = "c-20260923-140012-a3k9m2p7"


class CorrelationRows(Case):
    def test_a_client_correlation_id_is_kept_and_echoed(self):
        q = self.make(start=False)
        out = q.submit("generate", None, {"model": MODEL}, correlation_id=MINE)
        self.assertEqual((out["correlation_id"], out["correlation_source"]), (MINE, "client"))
        j = q.get(out["job_id"])
        self.assertEqual((j["correlation_id"], j["correlation_source"]), (MINE, "client"))

    def test_a_missing_correlation_id_is_minted_and_the_answer_echoes_the_record(self):
        q = self.make(start=False)
        out = q.submit("generate", None, {"model": MODEL})
        self.assertTrue(Q.ids.matches("correlation", out["correlation_id"]))
        self.assertEqual(out["correlation_source"], "server",
                         "the record says the SERVER minted it, never the client")
        self.assertEqual(out["correlation_id"], q.get(out["job_id"])["correlation_id"])

    def test_a_correlation_id_that_is_not_one_is_refused_not_replaced(self):
        # DECIDED HERE, beyond the carried-over row: the application this was
        # derived from minted a substitute for a malformed id. A substitute is
        # how a client ends up joining on an id the record does not wear, so a
        # malformed one is refused, in the validator's own words.
        q = self.make(start=False)
        for bad in ("   ", "req-1", "c-20260923-140012-a3k9m2p1"):
            e = self.refused(q.submit, "generate", None, {"model": MODEL},
                             correlation_id=bad)
            self.assertEqual((e.code, e.reason), (400, "bad_request"))
            self.assertEqual(e.error, V.check(bad, {"id": "correlation"},
                                              at="$.correlation_id")[0])
        self.assertEqual(q.jobs(), [], "a refused submit queues nothing")

    def test_a_client_correlation_id_the_folder_already_has_is_refused(self):
        # never re-minted: the client would join on its own id, and the record
        # would wear another
        q = self.make(start=False)
        first = q.submit("generate", None, {"model": MODEL}, correlation_id=MINE)["job_id"]
        e = self.refused(q.submit, "generate", None, {"model": MODEL}, correlation_id=MINE)
        self.assertEqual((e.code, e.reason), (409, "correlation_taken"))
        self.assertIn(first, e.error)
        self.assertEqual(len(q.jobs()), 1)

    def test_a_server_minted_id_is_never_one_the_folder_already_has(self):
        q = self.make(start=False, clock=lambda: 1790172012.0)   # one second, every time
        drawn = iter([0] * 8 + [0] * 8 + [1] * 8)                  # the second draw repeats
        real = Q.ids._draw
        Q.ids._draw = lambda: next(drawn)
        try:
            a = q.submit("generate", None, {"model": MODEL})["correlation_id"]
            b = q.submit("generate", None, {"model": MODEL})["correlation_id"]
        finally:
            Q.ids._draw = real
        self.assertNotEqual(a, b, "the repeat was minted again")
        self.assertTrue(b.endswith("bbbbbbbb"))

    def test_a_retry_is_a_new_request_with_a_new_id(self):
        q = self.make()
        jid = self.submit(q, {"fail": "x"}, correlation_id=MINE)
        self.wait_status(q, jid, "failed")
        self.idle(q)
        again = q.retry(jid)["job"]
        self.assertNotEqual(again["correlation_id"], MINE)
        self.assertEqual((again["correlation_source"], again["retry_of"]), ("server", jid))


class NativeClientRows(Case):
    """Which client asked, and which client took the result - recorded so a
    client can find its own work, never enforced."""

    def test_a_job_records_the_client_that_asked(self):
        q = self.make(start=False)
        jid = self.submit(q, client=CLIENT_ID)
        self.assertEqual(q.get(jid)["client"], CLIENT_ID)
        self.assertIsNone(q.get(self.submit(q))["client"])

    def test_a_client_finds_its_own_work(self):
        q = self.make(start=False)
        mine = [self.submit(q, client=CLIENT_ID) for _ in range(2)]
        self.submit(q, client=OTHER_CLIENT)
        self.submit(q)
        self.assertEqual([j["id"] for j in q.jobs(client=CLIENT_ID)], mine)

    def test_a_claim_records_who_claimed(self):
        q = self.make()
        jid = self.submit(q, client=CLIENT_ID)
        self.wait_status(q, jid, "done")
        self.assertEqual(q.claim(jid, client=OTHER_CLIENT)["job"]["claimed_by"], OTHER_CLIENT,
                         "another client may take it - recorded, not refused")
        self.assertEqual(q.get(jid)["claimed_by"], OTHER_CLIENT)

    def test_a_client_that_is_not_an_actor_id_is_refused(self):
        q = self.make(start=False)
        e = self.refused(self.submit, q, client="tab-2")
        self.assertEqual((e.reason, e.error), ("bad_request",
                                               '$.client: expected an actor id, got "tab-2"'))
        jid = self.submit(q)
        self.assertEqual(self.refused(q.claim, jid, client="tab-2").reason, "bad_request")

    def test_a_retry_is_still_the_same_clients_work(self):
        q = self.make()
        jid = self.submit(q, {"fail": "x"}, client=CLIENT_ID)
        self.wait_status(q, jid, "failed")
        self.idle(q)
        self.assertEqual(q.retry(jid)["job"]["client"], CLIENT_ID)


class NativeActorRows(Case):
    """A server's own id names every generation it runs."""

    def test_every_handle_names_the_server_that_ran_it(self):
        backend = Backend()
        q = self.make(backend=backend, actor="srv-k3x9m2p7")
        jid = self.submit(q)
        self.wait_status(q, jid, "done")
        self.assertEqual(backend.calls[0]["handle"], f"srv-k3x9m2p7:{jid}#01")
        self.assertEqual(q.result(jid)["candidates"][0]["handle"], f"srv-k3x9m2p7:{jid}#01")

    def test_two_servers_never_name_a_generation_alike(self):
        # the same job number in two folders, one model server between them
        backend = Backend()
        qa = self.make(backend=backend)
        qb = self.make(backend=backend, folder=self.root / "other-book")
        a, b = self.submit(qa), self.submit(qb)
        self.assertEqual(a, b, "both folders' first job is j-0001")
        self.settle(qa, a)
        self.settle(qb, b)
        handles = [c["handle"] for c in backend.calls]
        self.assertEqual(len(set(handles)), 2, f"two generations, two names: {handles}")

    def test_a_queue_is_known_by_its_actor_and_says_so(self):
        q = self.make(start=False, actor="srv-k3x9m2p7")
        self.assertEqual(q.health()["actor"], "srv-k3x9m2p7")
        self.assertEqual(q._folder_lock.read_note()["actor"], "srv-k3x9m2p7")
        with self.assertRaises(Q.FolderInUse) as cm:
            Q.Queue(self.folder, start=False)
        self.assertEqual(cm.exception.extra["holder"]["actor"], "srv-k3x9m2p7")

    def test_a_queue_is_a_servers_never_a_clients(self):
        with self.assertRaises(ValueError):
            Q.Queue(None, start=False, actor=CLIENT_ID)


# ================================================ carried over: no folder

class NoWorkspaceRows(Case):
    def test_the_job_list_refuses_instead_of_answering(self):
        q = Q.Queue(None, start=False)
        e = self.refused(q.jobs)
        self.assertEqual((e.reason, e.code), ("no_workspace", 409))

    def test_the_sentence_names_the_job_queue_and_not_the_folder(self):
        q = Q.Queue(None, start=False)
        self.assertIn("job queue", self.refused(q.jobs).error)

    def test_no_folder_is_never_answered_as_an_empty_store(self):
        q = Q.Queue(None, start=False)
        for call in (q.jobs, q.unfinished, lambda: q.submit("generate", None, {"model": "m"}),
                     lambda: q.claim("j-0001"), lambda: q.cancel("j-0001")):
            with self.subTest(call=call):
                self.assertEqual(self.refused(call).reason, "no_workspace")
        self.assertFalse(q.health()["loaded"], "and health still answers")

    def test_a_real_empty_store_still_answers_empty(self):
        q = self.make(start=False)
        self.assertFalse(q.store.path.exists())
        self.assertEqual(q.jobs(), [])


# ======================================= carried over: what is still owed

class BusyRows(Case):
    def owed(self, *rows):
        self.free_records = any(r["job"]["status"] not in Q.STATUSES for r in rows)
        write_store(self.folder, *rows)
        return self.make(start=False).unfinished()

    def test_a_running_job_makes_the_queue_busy(self):
        owed = self.owed(fixture_row("j-0001", "running"))
        self.assertEqual([j["id"] for j in owed], ["j-0001"])

    def test_the_busy_answer_names_the_running_job(self):
        owed = self.owed(fixture_row("j-0001", "running", name="New part 1.2"))
        self.assertEqual(owed[0]["name"], "New part 1.2")

    def test_the_running_one_comes_first_even_when_queued_jobs_were_first(self):
        owed = self.owed(fixture_row("j-0001", "queued", started=None, created=1.0),
                         fixture_row("j-0002", "queued", started=None, created=2.0),
                         fixture_row("j-0003", "running", created=3.0))
        self.assertEqual([j["id"] for j in owed], ["j-0003", "j-0001", "j-0002"])

    def test_a_queue_with_nothing_running_names_the_first_waiting(self):
        owed = self.owed(fixture_row("j-0001", "queued", started=None, created=1.0),
                         fixture_row("j-0002", "queued", started=None, created=2.0))
        self.assertEqual(owed[0]["id"], "j-0001")

    def test_every_finished_status_lets_the_folder_go(self):
        self.assertEqual(self.owed(*[fixture_row(f"j-{i + 1:04d}", s, 1000.0)
                                     for i, s in enumerate(Q.FINISHED)]), [])

    def test_an_empty_or_missing_store_is_not_busy(self):
        self.assertEqual(self.make(start=False).unfinished(), [])

    def test_the_busy_answer_carries_the_records_and_their_names(self):
        owed = self.owed(fixture_row("j-0001", "running", name="New part 1.2"),
                         fixture_row("j-0002", "queued", started=None, name="Verdict on 1.2.3"))
        self.assertEqual([(j["id"], j["name"]) for j in owed],
                         [("j-0001", "New part 1.2"), ("j-0002", "Verdict on 1.2.3")])

    def test_a_job_with_no_name_still_names_something(self):
        q = self.make(start=False)
        q.submit("mystery_kind", None, {"model": MODEL})
        self.assertEqual(q.unfinished()[0]["name"], "mystery_kind")

    def test_it_is_running_and_queued_and_nothing_else(self):
        owed = self.owed(fixture_row("j-0001", "running"), fixture_row("j-0002", "queued", started=None),
                         fixture_row("j-0003", "done", 1.0), fixture_row("j-0004", "failed", 1.0),
                         fixture_row("j-0005", "cancelled", 1.0), fixture_row("j-0006", "claimed", 1.0))
        self.assertEqual([j["id"] for j in owed], ["j-0001", "j-0002"])

    def test_it_answers_for_its_own_folder(self):
        write_store(self.folder, fixture_row("j-0001", "running"))
        other = self.root / "elsewhere"
        self.assertEqual(len(self.make(start=False).unfinished()), 1)
        self.assertEqual(self.make(start=False, folder=other).unfinished(), [])

    def test_a_status_nobody_has_seen_before_counts_as_unfinished(self):
        owed = self.owed(fixture_row("j-0001", "paused_by_something_later"))
        self.assertEqual(len(owed), 1)

    def test_a_folder_being_left_is_refused_while_a_job_is_owed(self):
        owed = self.owed(fixture_row("j-0001", "running", name="part 2"),
                         fixture_row("j-0002", "queued", started=None))
        self.assertEqual((owed[0]["id"], len(owed) - 1), ("j-0001", 1))

    def test_a_finished_queue_does_not_refuse(self):
        self.assertEqual(self.owed(fixture_row("j-0001", "done", 1.0)), [])

    def test_no_folder_is_never_answered_as_not_busy(self):
        self.assertEqual(self.refused(Q.Queue(None, start=False).unfinished).reason,
                         "no_workspace")


# =================================================== carried over: give up

class GiveUpRows(Case):
    def test_give_up_hands_the_queue_back_from_a_wedged_generation(self):
        backend = Backend()
        q = self.make(backend=backend, give_up_grace_s=5.0)
        stuck = self.submit(q, {"gate": "never"}, gen={"n": 2})
        behind = self.submit(q)
        self.wait_for(lambda: backend.calls, "the generation to wedge")
        self.assertEqual(self.status(q, stuck), "running", "a generation is genuinely wedged")
        self.assertEqual(self.status(q, behind), "queued", "a second job is stuck behind it")
        out = q.give_up(stuck)
        self.assertEqual(out["was"], "running", "the give-up verb exists and answers")
        ended = self.wait_status(q, stuck, *Q.FINISHED)
        self.assertTrue(ended["status"] == "cancelled"
                        or "given up" in (ended["error"] or ""), ended)
        self.assertEqual(self.wait_status(q, behind, "done", "failed")["status"], "done",
                         "THE QUEUE MOVES AGAIN")
        self.assertTrue(q.health()["worker"]["alive"], "and the app never stopped answering")

    def test_give_up_ends_a_generation_that_ignores_every_cancel(self):
        # the case the reaper exists for: the backend never looks at the cancel
        # and never returns, so no flag the worker could read will ever be read
        backend = Backend()
        q = self.make(backend=backend)
        stuck = self.submit(q, {"gate": "never", "stubborn": True, "quiet": True})
        behind = self.submit(q)
        self.wait_for(lambda: backend.calls, "the generation to wedge")
        t0 = time.monotonic()
        q.give_up(stuck)
        ended = self.wait_status(q, stuck, *Q.FINISHED)
        self.assertEqual(ended["status"], "failed")
        self.assertIn("given up", ended["error"])
        self.assertEqual(self.wait_status(q, behind, "done", "failed")["status"], "done",
                         "the job behind it runs though the wedged call never returned")
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertFalse(backend.gate("never").is_set(), "the wedged call is still wedged")
        self.assertEqual(self.raw(q, stuck)["run"]["ended_by"], "give_up")


# ======================================================= carried over: stalls

class StallRows(Case):
    def test_a_generation_that_stops_moving_fails_and_says_so(self):
        backend = Backend()
        q = self.make(backend=backend, stall_s=0.3)
        jid = self.submit(q, {"gate": "frozen", "quiet": True}, gen={"n": 2})
        self.assertTrue(jid, "the queue accepted a two-variant job")
        j = self.wait_status(q, jid, *Q.FINISHED)
        self.assertEqual(j["status"], "failed", "a job that cannot finish must say so")
        self.assertRegex(j["error"], "no progress|stall")
        self.assertIn(Q.ids.handle(q.actor, jid, 1), j["error"],
                      "it names the generation, so it can be found")

    def test_a_model_load_is_not_a_stall(self):
        backend = Backend(loading=lambda: True)
        q = self.make(backend=backend, stall_s=0.3, load_s=30.0)
        jid = self.submit(q, {"gate": "loading", "quiet": True})
        self.wait_for(lambda: backend.calls, "the generation to start")
        time.sleep(1.0)
        j = q.get(jid)
        self.assertEqual(j["status"], "running", "a LOAD is not killed by the stall deadline")
        self.assertIs(j["progress"].get("loading"), True)
        self.assertIsInstance(j["progress"].get("waited"), int)
        self.assertEqual(j["progress"].get("stage"), "loading the model")

    def test_a_backend_that_cannot_say_is_the_patient_case_and_still_ends(self):
        def dark():
            raise ConnectionError("the status probe is dark")
        backend = Backend(loading=dark)
        q = self.make(backend=backend, stall_s=0.3, load_s=1.2)
        jid = self.submit(q, {"gate": "dark", "quiet": True})
        self.wait_for(lambda: backend.calls, "the generation to start")
        time.sleep(0.7)
        self.assertEqual(self.status(q, jid), "running",
                         "an unanswerable probe buys the patient deadline")
        j = self.wait_status(q, jid, *Q.FINISHED, timeout=6)
        self.assertEqual(j["status"], "failed")
        self.assertRegex(j["error"], "giving up|no progress")
        self.assertIn("cancel sent", j["error"])
        self.assertEqual(backend.cancelled, [Q.ids.handle(q.actor, jid, 1)],
                         "no generation stays behind holding the model")
        self.idle(q)


# ========================================== one call, two kinds (PLAN-4 D10)

class NativeOnePieceRows(Case):
    """A model that answers in one piece - a transcription - says nothing
    between its start and its answer, and declares the deadline it is due by
    as it begins: `due_s`, on the job's progress. Before it, a quiet
    generation is working, never stalled: neither the stall watch nor a new
    submission takes it back. After it, it is judged as any quiet one is. And
    a transcript of silence is an answer: "the model returned nothing" is the
    text engine's rule."""

    @staticmethod
    def once(due_s: float, answer_s: float, **answer):
        def run_once(request, params, run):
            run.report(stage="answering", due_s=due_s)
            deadline = time.monotonic() + answer_s
            while time.monotonic() < deadline:
                if run.stopped():
                    raise Q.Cancelled("the backend saw the cancel")
                time.sleep(0.01)
            return {"text": "Rain on the tin roof.", **answer}
        return run_once

    def test_an_answer_in_one_piece_is_not_stalled_before_its_deadline(self):
        q = self.make(backend=Backend(self.once(due_s=4.0, answer_s=1.2)), stall_s=0.3)
        jid = self.submit(q)
        j = self.wait_status(q, jid, *Q.FINISHED, timeout=10)
        self.assertEqual(j["status"], "done", j.get("error"))

    def test_past_its_deadline_a_quiet_answer_in_one_piece_is_a_stall(self):
        backend = Backend(self.once(due_s=0.5, answer_s=30.0))
        q = self.make(backend=backend, stall_s=0.3)
        jid = self.submit(q)
        self.wait_for(lambda: backend.calls, "the answer to begin")
        time.sleep(0.2)
        self.assertEqual(self.status(q, jid), "running", "taken back before its own deadline")
        j = self.wait_status(q, jid, *Q.FINISHED, timeout=10)
        self.assertEqual(j["status"], "failed")
        self.assertRegex(j["error"], "no progress")

    def test_asking_again_leaves_an_answer_in_one_piece_before_its_deadline(self):
        backend = Backend(self.once(due_s=4.0, answer_s=1.2), "the second answer")
        q = self.make(backend=backend, stale_head_s=0.3)
        first = self.submit(q)
        self.wait_for(lambda: backend.calls, "the first answer to begin")
        time.sleep(0.6)
        second = self.submit(q)
        self.assertEqual(self.wait_status(q, first, *Q.FINISHED, timeout=10)["status"], "done",
                         "a new submission took back an answer before its deadline")
        self.assertEqual(self.wait_status(q, second, *Q.FINISHED, timeout=10)["status"], "done")

    def test_the_deadline_is_on_the_jobs_progress_while_it_answers(self):
        backend = Backend(self.once(due_s=7.5, answer_s=30.0))
        q = self.make(backend=backend)
        jid = self.submit(q)
        self.wait_for(lambda: q.get(jid)["progress"].get("due_s"), "the deadline to be said")
        prog = q.get(jid)["progress"]
        self.assertEqual((prog["due_s"], prog["stage"]), (7.5, "answering"))
        self.assertEqual(V.check(q.get(jid), SHAPES["job"], SHAPES), [])
        q.cancel(jid)

    def test_its_stalled_for_is_null_until_its_deadline_then_counted_from_its_last_move(self):
        """S8 R9: before its deadline an answer in one piece is answering, not
        stalled - its `stalled_for` says so, null - and after it, it is judged
        as any quiet job is: `stalled_for` counted from its last movement, as
        the stall watch counts it (never from the deadline)."""
        backend = Backend(self.once(due_s=2.0, answer_s=30.0))
        q = self.make(backend=backend, stall_s=60.0)
        jid = self.submit(q)
        self.wait_for(lambda: q.get(jid)["progress"].get("due_s"), "the deadline to be said")
        seen = time.monotonic()
        early = q.get(jid)
        self.assertEqual(early["status"], "running")
        self.assertIsNone(early["stalled_for"], "called stalled before its own deadline")
        time.sleep(max(0.0, seen + 2.3 - time.monotonic()))
        late = q.get(jid)
        self.assertEqual(late["status"], "running", late.get("error"))
        self.assertIsNotNone(late["stalled_for"], "still not stalled past its deadline")
        self.assertGreaterEqual(late["stalled_for"], 2.2,
                                "counted from the deadline, not from the last movement")
        self.assertEqual(V.check(late, SHAPES["job"], SHAPES), [])
        q.cancel(jid)

    def test_a_transcript_of_silence_is_an_answer_and_an_empty_text_is_not(self):
        heard = {"task": "transcribe", "language": "english", "duration": 2.0, "text": "",
                 "segments": []}
        backend = Backend({"text": "", "transcript": heard, "audio_s": 2.0}, {"text": ""})
        q = self.make(backend=backend)
        quiet = self.submit(q)
        j = self.wait_status(q, quiet, *Q.FINISHED)
        self.assertEqual(j["status"], "done", j.get("error"))
        (cand,) = q.result(quiet)["candidates"]
        self.assertEqual((cand["text"], cand["transcript"], cand["audio_s"]), ("", heard, 2.0))
        self.assertEqual(V.check(cand, SHAPES["candidate"], SHAPES), [])
        nothing = self.submit(q)
        j = self.wait_status(q, nothing, *Q.FINISHED)
        self.assertEqual((j["status"], j["error"]), ("failed", "the model returned nothing (stop)"))


class NativeSubmitCheckRows(Case):
    """PLAN-4 D11 (S8 R8): a request its model can never answer is refused AT
    SUBMIT - when the request is the job's input, as the default build hook
    makes it - by the backend's own check (`backend.check_request`), in the
    backend's own reason and words: never stored, never after the job waited
    for the card and its model. With an application's own build hook the
    request is built at the job's turn, from whatever it is built from, so
    nothing is checked at submit."""

    class Checking(Backend):
        def __init__(self, *steps):
            super().__init__(*steps)
            self.checked: list = []

        def check_request(self, request, params):
            self.checked.append((json.loads(json.dumps(request)), dict(params)))
            if isinstance(request, dict) and request.get("never"):
                raise Q.Refused("bad_audio_request", "this model can never answer it", 400)

    def test_with_the_default_hooks_the_backend_checks_the_request_at_submit(self):
        b = self.Checking()
        q = self.make(hooks=Q.Hooks(), backend=b)
        with self.assertRaises(Q.Refused) as caught:
            q.submit("transcribe", {"never": True}, {"model": MODEL, "max_tokens": 50})
        said = caught.exception
        self.assertEqual((said.reason, said.code, said.error),
                         ("bad_audio_request", 400, "this model can never answer it"))
        self.assertEqual(q.jobs(), [], "a submit refused for its request stored a job")
        self.assertEqual(len(b.checked), 1)
        request, params = b.checked[0]
        self.assertEqual((request, params["model"], params["max_tokens"]),
                         ({"never": True}, MODEL, 50),
                         "the check was not handed the request and the job's own gen")
        fine = self.submit(q, {"text": "Rain on the tin roof."})
        self.assertEqual(self.wait_status(q, fine, *Q.FINISHED)["status"], "done")
        self.assertEqual(b.checked[-1][0], {"text": "Rain on the tin roof."})

    def test_a_gen_the_contract_refuses_is_refused_before_the_backend_is_asked(self):
        """The queue's own refusals come first: a misspelt parameter is the
        contract's `bad_request`, and the backend is asked nothing of it."""
        b = self.Checking()
        q = self.make(hooks=Q.Hooks(), backend=b)
        with self.assertRaises(Q.Refused) as caught:
            q.submit("transcribe", {"never": True}, {"model": MODEL, "temprature": 0.2})
        self.assertEqual(caught.exception.reason, "bad_request")
        self.assertEqual(b.checked, [])

    def test_with_its_own_build_hook_nothing_is_checked_at_submit(self):
        b = self.Checking()
        q = self.make(backend=b)                            # App: a build hook of its own
        jid = self.submit(q, {"never": True, "text": "built at its turn"})
        self.assertEqual(self.wait_status(q, jid, *Q.FINISHED)["status"], "done")
        self.assertEqual(b.checked, [], "a request built at the job's turn was checked at submit")


# =============================================== carried over: restarting

class RestartRows(Case):
    def test_a_server_stopped_mid_generation_is_reconciled_by_the_restart(self):
        backend = Backend()
        q1 = self.make(backend=backend)
        wedged = self.submit(q1, {"gate": "hold", "stubborn": True}, gen={"n": 2})
        self.assertTrue(wedged, "a generation is genuinely in flight")
        self.wait_for(lambda: backend.calls, "the generation to start")
        self.assertEqual(self.status(q1, wedged), "running")
        self.crash(q1)
        zombie = json.loads((self.folder / Q.STORE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(zombie["jobs"][0]["job"]["status"], "running",
                         "the stopped server left a `running` row on disk")
        healthy = Backend()
        healthy.healthy = True
        t0 = time.monotonic()
        q2 = self.make(backend=healthy)
        self.assertTrue(q2.health()["loaded"], "it comes back up on the same folder")
        self.wait_for(lambda: not [j for j in q2.jobs()
                                   if j["status"] == "running" and j["id"] == wedged
                                   and q2._worker_job is None],
                      "the zombie to be cleared")
        self.assertLess(time.monotonic() - t0, 2.0, "and quickly")
        again = self.submit(q2)
        self.assertEqual(self.wait_status(q2, again, "done", "failed")["status"], "done",
                         "ASKING AGAIN RECOVERS: the new job actually runs")

    def test_asking_again_takes_the_queue_from_a_stalled_head(self):
        backend = Backend()
        q = self.make(backend=backend, stale_head_s=0.4)
        first = self.submit(q, {"gate": "stuck", "stubborn": True, "quiet": True},
                            gen={"n": 2})
        self.wait_for(lambda: backend.calls, "the head to wedge")
        self.assertEqual(self.status(q, first), "running")
        time.sleep(0.6)
        second = self.submit(q)
        self.assertEqual(self.wait_status(q, second, "done", "failed")["status"], "done",
                         "asking again takes the queue, with no other action")
        j = q.get(first)
        self.assertIn(j["status"], Q.FINISHED)
        self.assertRegex(j["error"], "no progress|new generation")


class LivenessRows(Case):
    def test_health_carries_the_workers_own_pulse(self):
        q = self.make()
        w = q.health()["worker"]
        self.assertIsInstance(w["alive"], bool)
        self.assertTrue(isinstance(w["idle_s"], (int, float)) and w["idle_s"] >= 0)
        self.assertTrue(w["alive"])
        jid = self.submit(q)
        self.assertEqual(self.wait_status(q, jid, "done", "failed")["status"], "done",
                         "a job still runs normally with the pulse in place")

    def test_a_backend_that_hangs_does_not_hold_the_queue_shut_at_boot(self):
        orphan = fixture_row("j-0001", "running", input={"text": "old work"})
        orphan["job"]["n"] = 3
        orphan["run"].update(phase="generating", handles=["ghost-1", "ghost-2", "ghost-3"])
        write_store(self.folder, orphan)

        def hangs(h):
            time.sleep(20)
        t0 = time.monotonic()
        q = self.make(backend=Backend(recover=hangs))
        jid = self.submit(q)
        self.wait_for(lambda: self.status(q, jid) != "queued", "the new job to leave the queue")
        self.assertLess(time.monotonic() - t0, 4.0,
                        "boot recovery is bounded: a backend too slow to answer is "
                        "answering no")


# ====================================================== carried over: chaos

FAULTS = {
    # fault: (the steps the backend takes, what the error must name, success allowed)
    "stall": ([{"gate": "frozen", "quiet": True}], "no progress|stall", False),
    "refuse": ([Q.GenerationFailed("the backend refused: HTTP 500")], "refuse|500", False),
    "vanish": (["v1", LookupError("no such job: the backend lost it")], "no such job", False),
    "garbage": (["v1", ValueError("the backend answered with something that is not JSON")],
                "JSON", False),
    "disconnect": (["v1", ConnectionResetError("connection reset by the backend")],
                   "connection|reset", False),
    "amnesia": (["v1", KeyError("no such job")], "no such job", False),
    "flap": (["v1", ConnectionError("the backend flapped"), "v2"], ".", True),
    "slow": (["v1", {"text": "v2", "sleep": 0.3}], ".", True),
}


class ChaosRows(Case):
    def test_every_backend_fault_keeps_the_four_things_that_must_stay_true(self):
        for fault, (steps, names, success_ok) in FAULTS.items():
            with self.subTest(fault=fault):
                backend = Backend(*[s for s in steps])
                q = self.make(backend=backend, folder=self.root / fault, stall_s=0.3)
                faulted = self.submit(q, {"text": "the owner's case"}, gen={"n": 2})
                j = self.wait_status(q, faulted, *Q.FINISHED)
                if j["status"] == "failed":
                    self.assertRegex(j["error"], names, "its error names the fault")
                else:
                    self.assertTrue(success_ok and j["status"] == "done",
                                    f"finished as {j['status']} despite the fault")
                backend.steps.clear()
                backend.healthy = True
                h = q.health()
                self.assertTrue(h["loaded"] and h["worker"]["alive"],
                                "the queue still answers health")
                after = self.submit(q)
                self.assertEqual(self.wait_status(q, after, "done", "failed")["status"],
                                 "done", "THE NEXT JOB STILL RUNS")


# ================================= carried over: a fan of readings, and one
# that stops. The application here runs readings of one collection as a fan
# and merges them; what it writes is its own - the rows ask what the QUEUE did.

class FanApp(App):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.settlings = []

    def merge(self, group, members):
        super().merge(group, members)
        landed = [m for m in members if m["job"]["status"] in Q.LANDED]
        self.settlings.append({
            "group": group, "partial": len(landed) != len(members),
            "read": [m["job"]["input"]["no"] for m in landed],
            "unread": [m["job"]["input"]["no"] for m in members if m not in landed],
            "why": [m["job"]["error"] for m in members if m not in landed]})


class FanRows(Case):
    def fan(self, q, texts, group="run-1"):
        return [self.submit(q, {"no": i, "text": t}, group=group, group_size=len(texts),
                            name=f"reading {i} of {len(texts)}")
                for i, t in enumerate(texts, start=1)]

    def test_a_three_member_fan_is_merged_once_when_all_have_landed(self):
        app = FanApp()
        q = self.make(app)
        ids = self.fan(q, ["A", "B", "C"])
        self.settle(q, *ids)
        self.idle(q)
        self.assertEqual(len(app.settlings), 1)
        self.assertEqual((app.settlings[0]["partial"], app.settlings[0]["read"]),
                         (False, [1, 2, 3]))

    def test_nothing_is_merged_until_the_last_member_lands(self):
        app, backend = FanApp(), Backend()
        q = self.make(app, backend)
        ids = [self.submit(q, {"no": 1, "text": "A"}, group="run-1", group_size=3),
               self.submit(q, {"no": 2, "text": "B"}, group="run-1", group_size=3),
               self.submit(q, {"no": 3, "gate": "last"}, group="run-1", group_size=3)]
        self.wait_status(q, ids[1], "done")
        self.wait_for(lambda: len(backend.calls) == 3, "the last member to start")
        self.assertEqual(app.settlings, [], "a member merged the fan early")
        backend.gate("last").set()
        self.settle(q, ids[2])
        self.idle(q)
        self.assertEqual(len(app.settlings), 1)

    def test_no_member_is_shown_what_another_member_produced(self):
        app = FanApp()
        q = self.make(app)
        ids = self.fan(q, ["A", "B", "C"])
        self.settle(q, *ids)
        self.assertEqual([after for _, after in app.built], [{}, {}, {}],
                         "a member of a fan is blind unless it asks with `after`")

    def test_a_job_outside_the_fan_never_advances_it(self):
        app, backend = FanApp(), Backend()
        q = self.make(app, backend)
        ids = [self.submit(q, {"no": 1, "text": "A"}, group="run-1", group_size=2),
               self.submit(q, {"no": 2, "gate": "held"}, group="run-1", group_size=2)]
        ghost = self.submit(q, {"no": 9, "text": "ghost"}, group="another-run", group_size=1)
        stray = self.submit(q, {"no": 8, "text": "stray"})
        self.wait_for(lambda: len(backend.calls) == 2, "the held member to start")
        self.assertEqual([s["group"] for s in app.settlings], [])
        backend.gate("held").set()
        self.settle(q, ids[1], ghost, stray)
        self.idle(q)
        run1 = [s for s in app.settlings if s["group"] == "run-1"]
        self.assertEqual([s["read"] for s in run1], [[1, 2]],
                         "an answer no member asked for advanced the fan")

    def test_a_retried_member_counts_once_in_the_merge(self):
        # the application makes the second attempt succeed through its own
        # build hook - the queue never reads the input it re-sends
        app = FanApp(build=lambda job, after: {"text": "B again"} if job.get("retry_of")
                     else job["input"])
        q = self.make(app)
        ids = [self.submit(q, {"no": 1, "text": "A"}, group="run-1", group_size=2),
               self.submit(q, {"no": 2, "fail": "the model went away"}, group="run-1",
                           group_size=2)]
        self.settle(q, *ids)
        self.idle(q)
        self.assertEqual(app.settlings[-1]["partial"], True)
        retry = q.retry(ids[1])["job"]["id"]
        self.settle(q, retry)
        self.idle(q)
        self.assertEqual(len(app.settlings), 2)
        self.assertEqual([m[0] for m in app.merges[-1]["members"]], [ids[0], retry],
                         "one reading landed twice is still one reading")
        self.assertEqual((app.settlings[-1]["partial"], app.settlings[-1]["read"]),
                         (False, [1, 2]))


class StoppedFanRows(Case):
    def start_fan(self, q, n=3, fail_at=None, cancel_gate=None):
        ids = []
        for i in range(1, n + 1):
            inp = {"no": i, "text": f"reading {i}"}
            if fail_at == i:
                inp = {"no": i, "fail": "the model went away"}
            ids.append(self.submit(q, inp, group="run-1", group_size=n,
                                   name=f"reading {i} of {n}"))
        return ids

    def test_a_failed_member_stops_the_fan_and_the_merge_keeps_what_landed(self):
        app = FanApp()
        q = self.make(app)
        ids = self.start_fan(q, fail_at=2)
        self.settle(q, *ids)
        self.idle(q)
        self.assertEqual([self.status(q, i) for i in ids], ["done", "failed", "cancelled"])
        self.assertIn("the group stopped when", q.get(ids[2])["error"])
        self.assertEqual(len(app.settlings), 1)
        s = app.settlings[0]
        self.assertEqual((s["partial"], s["read"], s["unread"]), (True, [1], [2, 3]),
                         "the work already done is what a partial merge exists to keep")

    def revive(self, q, app, ids):
        """Make the failed member's retry succeed: the application fixes its
        own input (the queue never reads it) through the build hook."""
        app._build = lambda job, after: ({"text": f"reading {job['input']['no']} again"}
                                         if job.get("retry_of") else job["input"])
        return q.retry(ids[1])

    def test_a_retry_that_succeeds_ends_with_a_merge_that_is_not_partial(self):
        app = FanApp()
        q = self.make(app)
        ids = self.start_fan(q, fail_at=2)
        self.settle(q, *ids)
        self.idle(q)
        out = self.revive(q, app, ids)
        self.assertEqual(len(out["revived"]), 1, "the member the failure stopped comes back")
        self.settle(q, out["job"]["id"], *out["revived"])
        self.idle(q)
        self.assertEqual(len(app.settlings), 2)
        self.assertEqual((app.settlings[-1]["partial"], app.settlings[-1]["read"]),
                         (False, [1, 2, 3]))

    def test_the_retried_member_reads_exactly_the_input_the_failed_one_had(self):
        app = FanApp()
        q = self.make(app, start=False)
        ids = self.start_fan(q, fail_at=2)
        q.start()
        self.settle(q, *ids)
        retry = q.retry(ids[1])["job"]["id"]
        self.assertEqual(self.raw(q, retry)["input"], self.raw(q, ids[1])["input"],
                         "a retry reads the bytes its original was given, never a fresh cut")
        self.settle(q, retry)
        self.assertEqual(self.raw(q, retry)["gen"], self.raw(q, ids[1])["gen"])

    def test_a_retry_is_a_new_job_and_the_failed_one_keeps_saying_so(self):
        app = FanApp()
        q = self.make(app)
        ids = self.start_fan(q, fail_at=2)
        self.settle(q, *ids)
        new = self.revive(q, app, ids)["job"]
        self.assertNotEqual(new["id"], ids[1])
        self.assertEqual(new["retry_of"], ids[1])
        self.assertNotEqual(new["correlation_id"], q.get(ids[1])["correlation_id"])
        self.assertEqual(self.status(q, ids[1]), "failed", "the dead row keeps saying so")

    def test_a_retry_of_the_last_member_finishes_the_fan(self):
        app = FanApp()
        q = self.make(app)
        ids = self.start_fan(q, fail_at=3)
        self.settle(q, *ids)
        self.idle(q)
        out = self.revive(q, app, [None, ids[2]])
        self.settle(q, out["job"]["id"])
        self.idle(q)
        self.assertEqual((app.settlings[-1]["partial"], app.settlings[-1]["read"]),
                         (False, [1, 2, 3]))

    def test_a_revived_fan_is_not_stopped_again_by_its_old_failure(self):
        app = FanApp()
        q = self.make(app)
        ids = self.start_fan(q, fail_at=2)
        self.settle(q, *ids)
        out = self.revive(q, app, ids)
        time.sleep(0.2)                              # several supervisor passes
        for jid in [out["job"]["id"]] + out["revived"]:
            self.assertEqual(self.wait_status(q, jid, *Q.FINISHED)["status"], "done")

    def test_a_stopped_fan_is_merged_once_not_on_every_pass(self):
        app = FanApp()
        q = self.make(app)
        ids = self.start_fan(q, fail_at=2)
        self.settle(q, *ids)
        self.idle(q)
        time.sleep(0.25)
        q._drain_merges()
        self.assertEqual(len(app.settlings), 1)

    def test_a_member_that_already_landed_cannot_be_failed_afterwards(self):
        app = FanApp()
        q = self.make(app)
        (only,) = self.start_fan(q, n=1)
        self.settle(q, only)
        self.idle(q)
        with q._lock:
            doc = q._load()
            row = q._find(doc, only)
            self.assertFalse(q._end(doc, row, "failed", "cleared as stale"),
                             "a stall guard arriving late cannot reopen a finished fan")
        self.assertEqual(self.status(q, only), "done")
        self.assertEqual(len(app.settlings), 1)

    def test_a_failure_in_one_fan_never_stops_another(self):
        app = FanApp()
        q = self.make(app)
        bad = [self.submit(q, {"no": 1, "fail": "gone"}, group="bad", group_size=2),
               self.submit(q, {"no": 2, "text": "x"}, group="bad", group_size=2)]
        good = [self.submit(q, {"no": 1, "text": "g1"}, group="good", group_size=2),
                self.submit(q, {"no": 2, "text": "g2"}, group="good", group_size=2)]
        self.settle(q, *bad, *good)
        self.idle(q)
        by = {s["group"]: s for s in app.settlings}
        self.assertTrue(by["bad"]["partial"])
        self.assertEqual((by["good"]["partial"], by["good"]["read"]), (False, [1, 2]))

    def test_a_failed_job_outside_any_fan_merges_nothing(self):
        app = FanApp()
        q = self.make(app)
        jid = self.submit(q, {"no": 1, "fail": "no"})
        self.settle(q, jid)
        self.idle(q)
        self.assertEqual(app.settlings, [])

    def test_a_cancelled_member_stops_the_fan_the_same_way(self):
        app, backend = FanApp(), Backend()
        q = self.make(app, backend)
        first = self.submit(q, {"no": 1, "gate": "first"}, group="run-1", group_size=3)
        second = self.submit(q, {"no": 2, "text": "b"}, group="run-1", group_size=3)
        third = self.submit(q, {"no": 3, "text": "c"}, group="run-1", group_size=3)
        self.wait_for(lambda: backend.calls, "the first member to start")
        q.cancel(second)
        backend.gate("first").set()
        self.settle(q, first, second, third)
        self.idle(q)
        s = app.settlings[-1]
        self.assertEqual((s["partial"], s["read"], s["unread"]), (True, [1], [2, 3]))
        self.assertIn("cancelled", " ".join(e or "" for e in s["why"]),
                      "a cancel with no error message still has to say why")

    def test_retrying_a_member_that_is_still_going_is_refused(self):
        q = self.make(FanApp(), start=False)
        ids = self.start_fan(q, n=2)
        e = self.refused(q.retry, ids[0])
        self.assertEqual((e.reason, e.extra["status"]), ("not_retryable", "queued"))
        self.assertEqual(len(q.jobs()), 2, "a second copy of a live member was queued")

    def test_retrying_a_member_that_landed_is_refused(self):
        q = self.make(FanApp())
        (only,) = self.start_fan(q, n=1)
        self.settle(q, only)
        self.assertEqual(self.refused(q.retry, only).reason, "not_retryable")

    def test_retrying_a_job_that_does_not_exist_is_refused(self):
        q = self.make(FanApp(), start=False)
        self.assertEqual(self.refused(q.retry, "j-9999").reason, "no_such_job")

    def test_a_member_taken_back_by_the_supervisor_still_stops_the_fan(self):
        # the worker is the broken thing here, so nothing on the worker's path
        # can report the failure - the supervisor's taking-back has to
        app, backend = FanApp(), Backend()
        q = self.make(app, backend)
        first = self.submit(q, {"no": 1, "gate": "wedge", "stubborn": True, "quiet": True},
                            group="run-1", group_size=2)
        second = self.submit(q, {"no": 2, "text": "b"}, group="run-1", group_size=2)
        self.wait_for(lambda: backend.calls, "the member to wedge")
        q.give_up(first)
        self.settle(q, first, second)
        self.wait_for(lambda: app.settlings, "the fan to be merged")
        s = app.settlings[0]
        self.assertEqual((s["partial"], s["unread"]), (True, [1, 2]))
        self.assertIn("given up", s["why"][0])


# ========================= carried over: a study, fanned out and folded back

class StudyApp(App):
    """Studies a subject in chunks. Blind chunks run as a fan; sequential
    chunks each carry a brief built from the findings SO FAR, which exist only
    at run time; merges are planned from the ACTUAL findings' sizes, so the
    shape of the fold cannot be known when the study starts."""

    def __init__(self, merge_budget=10 ** 6):
        super().__init__()
        self.q = None
        self.budget = merge_budget
        self.docs: dict = {}

    def start(self, sid, k, sequential):
        ids = []
        for no in range(1, k + 1):
            ids.append(self.q.submit(
                "study_chunk", {"study": sid, "no": no, "of": k, "sequential": sequential},
                {"model": MODEL}, group=f"{sid}/chunks", group_size=k,
                after=list(ids) if sequential else [],
                name=f"{sid}: chunk {no} of {k}")["job_id"])
        self.docs[sid] = {"chunks": ids, "merges": [], "final": None}
        return ids

    def build_request(self, job, after_results):
        super().build_request(job, after_results)
        inp = job["input"]
        if job["kind"] == "study_chunk":
            brief = "\n".join(after_results[a]["candidates"][0]["text"]
                              for a in job["after"])
            return {"text": f"FINDING-P{inp['no']}of{inp['of']}"
                            + (" brief seen" if brief else ""), "brief": brief}
        return {"text": " | ".join(after_results[a]["candidates"][0]["text"]
                                   for a in job["after"])}

    def merge(self, group, members):
        super().merge(group, members)
        sid, level = group.split("/")
        doc = self.docs[sid]
        texts = [m["result"]["candidates"][0]["text"] for m in members]
        ids = [m["job"]["id"] for m in members]
        if len(members) == 1:
            doc["final"] = {"text": texts[0], "merged": level != "chunks"}
            return
        nxt = 1 if level == "chunks" else int(level[1:]) + 1
        # pack by the findings' real sizes; a pack always takes two when it
        # can, so every level is smaller than the one before and the fold ends
        packs, cur, size = [], [], 0
        for jid, t in zip(ids, texts):
            if len(cur) >= 2 and size + len(t) > self.budget:
                packs.append(cur)
                cur, size = [], 0
            cur.append(jid)
            size += len(t)
        packs.append(cur)
        for p in packs:
            mid = self.q.submit("study_merge", {"study": sid, "level": nxt},
                                {"model": MODEL}, after=p,
                                group=f"{sid}/L{nxt}", group_size=len(packs))["job_id"]
            doc["merges"].append({"level": nxt, "job": mid})


class StudyRows(Case):
    def study(self, k, sequential, budget=10 ** 6):
        app = StudyApp(budget)
        backend = Backend()
        q = self.make(app, backend, start=False)
        app.q = q
        ids = app.start("s1", k, sequential)
        return app, backend, q, ids

    def finish(self, app, q):
        self.wait_for(lambda: app.docs["s1"]["final"], "the study to finish", timeout=10)
        self.idle(q)
        return app.docs["s1"]

    def test_a_small_subject_is_one_job_and_its_finding_is_the_final(self):
        app, _, q, ids = self.study(1, False)
        self.assertTrue(ids, "accepted")
        q.start()
        doc = self.finish(app, q)
        self.assertEqual((len(doc["chunks"]), doc["final"]["merged"]), (1, False))
        self.assertIn("FINDING-P1of1", doc["final"]["text"])

    def test_a_blind_fan_queues_every_member_at_once_and_merges_them_all(self):
        app, _, q, ids = self.study(3, False)
        self.assertEqual([self.status(q, i) for i in ids], ["queued"] * 3,
                         "every chunk queued AT ONCE")
        self.assertTrue(all(q._runnable(q._load(), q._find(q._load(), i)) for i in ids),
                        "and every one of them can run")
        q.start()
        doc = self.finish(app, q)
        self.assertTrue(doc["final"]["merged"])
        self.assertTrue(all("brief seen" not in t
                            for t in (q.result(i)["candidates"][0]["text"] for i in ids)),
                        "blind means blind")
        for no in (1, 2, 3):
            self.assertIn(f"FINDING-P{no}of3", doc["final"]["text"],
                          "the merge carried EVERY part's findings")
        self.assertEqual([m["level"] for m in doc["merges"]], [1])

    def test_a_sequential_fan_runs_one_at_a_time_each_built_from_the_last(self):
        app, backend, q, ids = self.study(6, True, budget=40)
        self.assertEqual([bool(q._runnable(q._load(), q._find(q._load(), i))) for i in ids],
                         [True] + [False] * 5, "SEQUENTIAL starts with chunk 1 alone")
        q.start()
        doc = self.finish(app, q)
        self.assertEqual([self.status(q, i) for i in ids], ["done"] * 6, "every chunk ran")
        texts = [q.result(i)["candidates"][0]["text"] for i in ids]
        self.assertNotIn("brief seen", texts[0])
        self.assertTrue(all("brief seen" in t for t in texts[1:]),
                        "chunk 1 ran BLIND, every later chunk carried the brief")
        briefs = [c["request"]["brief"] for c in backend.calls[:6]]
        self.assertEqual(briefs[3].splitlines(), texts[:3],
                         "the brief is the ACTUAL output of the chunks before it")
        levels = [m["level"] for m in doc["merges"]]
        self.assertTrue(1 in levels and 2 in levels and doc["final"]["merged"],
                        f"the merges went TWO levels deep: {levels}")
        self.assertIn("FINDING-P1of6", doc["final"]["text"])
        self.assertIn("FINDING-P6of6", doc["final"]["text"],
                      "the final still carries the first and the last parts")


# ============================== carried over: two arms of one ask, and smoke

class ArmsRows(Case):
    def test_two_arms_of_one_ask_each_keep_their_own_correlation_id(self):
        q = self.make()
        arm = {1: "c-20260923-140101-a3k9m2p7", 2: "c-20260923-140101-b2x7q4n8"}
        outs = [q.submit("generate", {"text": f"arm {k}"}, {"model": MODEL},
                         correlation_id=arm[k]) for k in (1, 2)]
        for k, out in zip((1, 2), outs):
            self.assertTrue(out["job_id"], f"arm {k}: accepted")
            self.assertEqual(out["correlation_id"], arm[k], f"arm {k}: kept its own id")
        self.settle(q, *[o["job_id"] for o in outs])
        self.assertEqual([self.status(q, o["job_id"]) for o in outs], ["done", "done"])
        self.assertEqual(q.health()["queue"]["consecutive_failures"], 0)

    def test_a_refused_submit_queues_nothing(self):
        q = self.make(start=False)
        before = len(q.jobs())
        self.refused(q.submit, "generate", None, {"model": MODEL}, after=["j-9999"])
        self.assertEqual(len(q.jobs()), before)


class SmokeRows(Case):
    def test_a_job_is_accepted_and_runs_to_done_with_no_failure_streak(self):
        q = self.make()
        self.assertTrue(q.health()["loaded"])
        jid = self.submit(q)
        self.assertEqual(self.wait_status(q, jid, *Q.FINISHED)["status"], "done")
        self.assertIsNone(q.get(jid)["error"])
        self.assertEqual(q.health()["queue"]["consecutive_failures"], 0)

    def test_a_job_asking_for_three_samples_takes_three_at_its_temperature(self):
        backend = Backend()
        q = self.make(backend=backend)
        jid = self.submit(q, gen={"n": 3, "temperature": 0.0})
        self.wait_status(q, jid, "done")
        self.assertEqual(len(q.result(jid)["candidates"]), 3)
        self.assertEqual([c["params"]["temperature"] for c in backend.calls], [0.0] * 3)


# ================================================================== NATIVE

class NativeDependencyRows(Case):
    def test_a_job_waits_for_its_dependency_and_is_built_from_its_actual_output(self):
        app = App(build=lambda job, after: {"text": "after: " + " + ".join(
            r["candidates"][0]["text"] for r in after.values())} if after else job["input"])
        backend = Backend()
        q = self.make(app, backend)
        part1 = self.submit(q, {"gate": "part1", "text": "Part one, as written."})
        part2 = self.submit(q, {"text": "unused"}, after=[part1], priority="interactive")
        self.wait_for(lambda: backend.calls, "part 1 to start")
        time.sleep(0.1)
        self.assertEqual(self.status(q, part2), "queued",
                         "interactive or not, it waits for what it names")
        backend.gate("part1").set()
        self.settle(q, part1, part2)
        self.assertEqual(q.result(part2)["candidates"][0]["text"],
                         "after: Part one, as written.")

    def test_a_dependent_of_a_failed_job_is_stopped_and_names_it(self):
        q = self.make()
        a = self.submit(q, {"fail": "the model went away"})
        b = self.submit(q, after=[a])
        c = self.submit(q, after=[b])
        self.settle(q, a, b, c)
        self.assertEqual([self.status(q, x) for x in (a, b, c)],
                         ["failed", "cancelled", "cancelled"])
        self.assertIn(a, q.get(b)["error"])
        self.assertIn(b, q.get(c)["error"])

    def test_a_dependent_of_a_cancelled_job_is_stopped_too(self):
        q = self.make(start=False)
        a = self.submit(q)
        b = self.submit(q, after=[a])
        q.cancel(a)
        self.assertEqual(self.status(q, b), "cancelled")
        self.assertIn("cancelled", q.get(b)["error"])

    def test_retrying_a_failed_dependency_revives_the_chain(self):
        app = App(build=lambda job, after: {"text": "fixed"} if job.get("retry_of")
                  else job["input"])
        q = self.make(app)
        a = self.submit(q, {"fail": "once"})
        b = self.submit(q, {"text": "b"}, after=[a])
        self.settle(q, a, b)
        out = q.retry(a)
        self.assertEqual(len(out["revived"]), 1)
        b2 = out["revived"][0]
        self.assertEqual(q.get(b2)["after"], [out["job"]["id"]])
        self.settle(q, out["job"]["id"], b2)
        self.assertEqual(self.status(q, b2), "done")

    def test_a_job_never_counts_as_an_outcome_if_it_never_ran(self):
        q = self.make()
        a = self.submit(q, {"fail": "boom"})
        b = self.submit(q, after=[a])
        self.settle(q, a, b)
        h = q.health()["queue"]
        self.assertEqual((h["last"]["job"], h["consecutive_failures"]), (a, 1),
                         "a stopped dependent must not hide the failure that stopped it")


class NativeContractRows(Case):
    def route(self, name):
        return ROUTES[f"server/{name}"]

    def answers(self, route, status, value):
        shape = self.route(route)["responses"][str(status)]
        self.assertEqual(V.check(value, shape, SHAPES), [], f"{route} {status}")

    def refusal(self, route, fn, *a, code, reason):
        """A refusal checked against the response for the status the QUEUE
        chose. A row that names the status itself checks the body against the
        right shape and cannot see a wrong status: a 404 answered as a 409
        passed that way (found by a mutation sweep)."""
        e = self.refused(fn, *a)
        self.assertEqual((e.code, e.reason), (code, reason))
        self.assertIn(str(e.code), self.route(route)["responses"], f"{route} {e.code}")
        self.answers(route, e.code, e.answer())
        return e

    def test_a_submit_answer_matches_the_contract(self):
        q = self.make(start=False)
        self.answers("jobs-submit", 200, q.submit("generate", None, {"model": MODEL}))

    def test_submit_refusals_match_the_contract(self):
        q = self.make(start=False)
        for call, reason in ((lambda: q.submit("", None, {"model": MODEL}), "no_kind"),
                             (lambda: q.submit("generate", None, {"model": ""}), "no_model"),
                             (lambda: q.submit("generate", None, {"model": MODEL},
                                               after=["j-9999"]), "unknown_after")):
            e = self.refused(call)
            self.assertEqual((e.reason, e.code), (reason, 400))
            self.answers("jobs-submit", 400, e.answer())
        self.answers("jobs-submit", 409, self.refused(
            Q.Queue(None, start=False).submit, "generate", None, {"model": MODEL}).answer())

    def test_claim_answers_match_the_contract(self):
        q = self.make()
        done, queued_q = self.submit(q), None
        self.wait_status(q, done, "done")
        self.answers("job-claim", 200, q.claim(done))
        self.refusal("job-claim", q.claim, done, code=409, reason="claimed_elsewhere")
        self.refusal("job-claim", q.claim, "j-9999", code=404, reason="no_such_job")
        q2 = self.make(start=False, folder=self.root / "b")
        self.refusal("job-claim", q2.claim, self.submit(q2), code=409, reason="not_done")

    def test_cancel_answers_match_the_contract(self):
        q = self.make(start=False)
        jid = self.submit(q)
        self.answers("job-cancel", 200, q.cancel(jid))
        self.refusal("job-cancel", q.cancel, jid, code=409, reason="finished")
        self.refusal("job-cancel", q.cancel, "j-9999", code=404, reason="no_such_job")

    def test_a_claim_on_a_job_that_ended_badly_says_it_ended(self):
        q = self.make()
        jid = self.submit(q, {"fail": "x"})
        self.wait_status(q, jid, "failed")
        self.assertEqual(self.refusal("job-claim", q.claim, jid, code=409,
                                      reason="ended").extra["status"], "failed")
        q2 = self.make(start=False, folder=self.root / "c")
        gone = self.submit(q2)
        q2.cancel(gone)
        self.refusal("job-claim", q2.claim, gone, code=409, reason="ended")

    def test_fan_size_refusals_match_the_contract(self):
        q = self.make(start=False)
        self.submit(q, group="g", group_size=1)
        self.refusal("jobs-submit", lambda: self.submit(q, group="g"),
                     code=409, reason="group_full")
        self.refusal("jobs-submit", lambda: self.submit(q, group="g", group_size=2),
                     code=400, reason="bad_group_size")
        self.refusal("jobs-submit", lambda: self.submit(q, group_size=2),
                     code=400, reason="bad_group_size")

    def test_every_job_route_answers_no_workspace_when_nothing_is_bound(self):
        q = Q.Queue(None, start=False)
        for route, call in (("jobs-list", lambda: q.jobs()),
                            ("jobs-submit", lambda: q.submit("generate", None, {"model": MODEL})),
                            ("job-result", lambda: q.result("j-0001")),
                            ("job-claim", lambda: q.claim("j-0001")),
                            ("job-cancel", lambda: q.cancel("j-0001")),
                            ("job-retry", lambda: q.retry("j-0001")),
                            ("job-give-up", lambda: q.give_up("j-0001")),
                            ("job-forget", lambda: q.forget("j-0001")),
                            ("jobs-prune", lambda: q.prune())):
            with self.subTest(route=route):
                self.refusal(route, call, code=409, reason="no_workspace")

    def test_retry_answers_match_the_contract(self):
        q = self.make()
        bad = self.submit(q, {"fail": "x"})
        ok = self.submit(q)
        self.settle(q, bad, ok)
        self.idle(q)
        self.answers("job-retry", 200, q.retry(bad))
        self.refusal("job-retry", q.retry, bad, code=409, reason="already_retried")
        self.refusal("job-retry", q.retry, ok, code=409, reason="not_retryable")
        self.refusal("job-retry", q.retry, "j-9999", code=404, reason="no_such_job")

    def test_give_up_answers_match_the_contract(self):
        q = self.make(start=False)
        jid = self.submit(q)
        self.answers("job-give-up", 200, q.give_up(jid))          # queued: ends at once
        self.refusal("job-give-up", q.give_up, jid, code=409, reason="finished")
        self.refusal("job-give-up", q.give_up, "j-9999", code=404, reason="no_such_job")
        running = self.submit(q)
        q._pick(q._beat["gen"])
        self.answers("job-give-up", 200, q.give_up(running))      # running: the clock starts

    def test_forget_and_prune_answers_match_the_contract(self):
        q = self.make(start=False)
        first = self.submit(q)
        gen = q._beat["gen"]
        q._pick(gen)
        q._finish(first, gen, "done")
        needs = self.submit(q, after=[first])
        self.refusal("job-forget", q.forget, first, code=409, reason="needed")
        self.refusal("job-forget", q.forget, needs, code=409, reason="unfinished")
        self.refusal("job-forget", q.forget, "j-9999", code=404, reason="no_such_job")
        self.answers("jobs-prune", 200, q.prune())
        q.cancel(needs)
        self.answers("job-forget", 200, q.forget(needs))

    def test_health_answers_match_the_contract_bound_and_unbound(self):
        """The queue's half of the health answer, with the four fields the
        server adds - the card, the runner and the pipeline (null: this queue
        has no runner behind it), and the version of the contract it serves."""
        # CHANGED ON PURPOSE: health grew `contract_version`, which the server adds;
        # and (PLAN-4 S6) `pipeline`, null with no runner behind the queue
        server_adds = {"card": {"coordinator": "none", "holding": False, "waiting_since": None},
                       "runner": None, "pipeline": None,
                       "contract_version": V.contract_version(CONTRACT)}
        q = self.make()
        jid = self.submit(q)
        self.settle(q, jid)
        self.answers("health", 200, {**q.health(), **server_adds})
        self.answers("health", 200, {**Q.Queue(None, start=False).health(), **server_adds})

    def test_list_and_result_answers_match_the_contract(self):
        q = self.make()
        jid = self.submit(q, {"text": "plain"})
        self.wait_status(q, jid, "done")
        self.answers("jobs-list", 200, {"jobs": q.jobs()})
        plain = q.result(jid)
        plain["candidates"] = [{k: c[k] for k in ("text", "model", "tokens", "finish_reason")}
                               for c in plain["candidates"]]
        self.answers("job-result", 200, plain)
        self.refusal("job-result", q.result, "j-9999", code=404, reason="no_such_job")

    def test_a_read_that_must_lose_nothing_crosses_the_wire(self):
        # the candidate is open: everything the queue and the application put
        # on it is answered, and the answer is a contract answer
        q = self.make(backend=Backend(dict(RICH)))
        jid = self.submit(q)
        self.wait_status(q, jid, "done")
        got = q.result(jid)
        self.answers("job-result", 200, got)
        self.assertLessEqual(set(RICH), set(got["candidates"][0]))

    def test_a_judged_candidate_is_a_contract_candidate(self):
        q = self.make(App(judge=copies), Backend(COPY, CLEAN))
        jid = self.submit(q, {"text": "unused"}, kind="prose", gen={"seed": 7})
        self.settle(q, jid)
        got = q.result(jid)
        self.answers("job-result", 200, got)
        self.assertEqual([a["seed"] for a in got["candidates"][0]["attempts"]], [7, 107])

    def test_a_job_its_judge_failed_is_a_contract_result(self):
        q = self.make(App(judge=copies), Backend(COPY, COPY, COPY))
        jid = self.submit(q, {"text": "unused"}, kind="prose", gen={"seed": 7})
        self.settle(q, jid)
        got = q.result(jid)
        self.answers("job-result", 200, got)
        self.assertEqual((got["job"]["status"], got["candidates"]), ("failed", []))

    def test_a_candidate_whose_queue_field_has_the_wrong_kind_is_refused(self):
        # open is not unchecked: the fields the queue writes keep their kinds
        q = self.make(backend=Backend(dict(RICH)))
        jid = self.submit(q)
        self.wait_status(q, jid, "done")
        got = q.result(jid)
        got["candidates"][0]["sample"] = "one"
        shape = self.route("job-result")["responses"]["200"]
        self.assertEqual(V.check(got, shape, SHAPES),
                         ['$.candidates[0].sample: expected integer, got string'])


class NativeLandingRows(Case):
    def test_a_land_hook_that_raises_does_not_take_the_result_away(self):
        def land(job, result):
            raise ValueError("the parser tripped")
        q = self.make(App(land=land))
        jid = self.submit(q, {"text": "kept"})
        j = self.wait_status(q, jid, "done", "failed")
        self.assertEqual(j["status"], "done")
        self.assertIn("parser tripped", j["progress"]["land_error"])
        self.assertEqual(q.result(jid)["candidates"][0]["text"], "kept")

    def test_a_land_hook_can_replace_what_is_offered(self):
        def land(job, result):
            return {"candidates": [dict(c, parsed=c["text"].upper())
                                   for c in result["candidates"]]}
        q = self.make(App(land=land))
        jid = self.submit(q, {"text": "shout"})
        self.wait_status(q, jid, "done")
        self.assertEqual(q.result(jid)["candidates"][0]["parsed"], "SHOUT")

    def test_claiming_a_member_after_the_merge_does_not_merge_again(self):
        app = FanApp()
        q = self.make(app)
        ids = [self.submit(q, {"no": i, "text": "x"}, group="g", group_size=2) for i in (1, 2)]
        self.settle(q, *ids)
        self.idle(q)
        q.claim(ids[0])
        # A claim alone changes nothing a merge can see until the queue next
        # works out its fans, so the row makes it: another job runs after the
        # claim, as one would. Draining straight after the claim could not
        # fail (found by a mutation sweep).
        later = self.submit(q, {"no": 3, "text": "later"})
        self.settle(q, later)
        self.idle(q)
        self.assertEqual(len(app.settlings), 1)

    def test_a_settling_is_reported_with_the_members_that_settled_after_a_retry(self):
        # found by a flaky run of StoppedFanRows: the fan settles (stopped), a
        # retry revives it BEFORE its merge has run, and the merge used to be
        # handed the fan as it is NOW - two queued members - and then be run
        # again when the revived fan settled. Made deterministic here: nothing
        # drains the merge between the settling and the retry.
        app = FanApp()
        q = self.make(app, start=False)
        m1 = self.submit(q, {"no": 1, "text": "a"}, group="g", group_size=2)
        m2 = self.submit(q, {"no": 2, "text": "b"}, group="g", group_size=2)
        gen = q._beat["gen"]
        q._pick(gen)
        q._finish(m1, gen, "failed", "boom")         # settled; the merge has not run
        self.assertEqual(app.merges, [])
        out = q.retry(m1)                             # revived before it did
        self.assertEqual(app.merges[0]["members"], [(m1, "failed"), (m2, "cancelled")],
                         "the first settling, with the members that settled")
        q.start()
        self.settle(q, out["job"]["id"], *out["revived"])
        self.idle(q)
        self.assertEqual(len(app.merges), 2, "each settling once, and only once")
        self.assertEqual(app.merges[1]["members"],
                         [(out["job"]["id"], "done"), (out["revived"][0], "done")])

    def test_a_late_answer_from_a_worker_that_was_taken_over_is_not_written(self):
        backend = Backend()
        q = self.make(backend=backend)
        jid = self.submit(q, {"gate": "late", "stubborn": True, "quiet": True})
        self.wait_for(lambda: backend.calls, "the generation to start")
        q.give_up(jid)
        self.wait_status(q, jid, "failed")
        backend.gate("late").set()                   # the old call finally returns
        time.sleep(0.2)
        j = q.get(jid)
        self.assertEqual((j["status"], q.result(jid)["candidates"]), ("failed", []),
                         "a job that ended stays ended")


class NativeFanSizeRows(Case):
    def test_a_fan_without_a_size_can_settle_before_its_members_exist(self):
        # THE RACE THE SIZE EXISTS FOR, pinned as behaviour: members arrive one
        # submit at a time while the worker runs, so the first can land alone
        # and a fan of one has "all landed". Found by flaky fan rows.
        app = FanApp()
        q = self.make(app)
        first = self.submit(q, {"no": 1, "text": "a"}, group="g")
        self.settle(q, first)
        self.wait_for(lambda: app.settlings, "the lone member's merge")
        second = self.submit(q, {"no": 2, "text": "b"}, group="g")
        self.settle(q, second)
        self.idle(q)
        self.assertEqual([s["read"] for s in app.settlings], [[1], [1, 2]],
                         "without a size, the queue cannot know the fan was not whole")

    def test_a_sized_fan_settles_only_when_every_member_exists(self):
        app = FanApp()
        q = self.make(app)
        first = self.submit(q, {"no": 1, "text": "a"}, group="g", group_size=2)
        self.settle(q, first)
        self.idle(q)
        self.assertEqual(app.settlings, [], "a fan of two is not whole with one")
        second = self.submit(q, {"no": 2, "text": "b"}, group="g", group_size=2)
        self.settle(q, second)
        self.idle(q)
        self.assertEqual([s["read"] for s in app.settlings], [[1, 2]])

    def test_a_member_arriving_after_its_fan_stopped_is_stopped_too(self):
        app = FanApp()
        q = self.make(app)
        first = self.submit(q, {"no": 1, "fail": "gone"}, group="g", group_size=2)
        self.settle(q, first)
        second = self.submit(q, {"no": 2, "text": "b"}, group="g", group_size=2)
        self.assertEqual(self.status(q, second), "cancelled")
        self.idle(q)
        self.assertEqual([(s["partial"], s["unread"]) for s in app.settlings],
                         [(True, [1, 2])])

    def test_a_bad_or_contradicting_size_and_an_overfull_fan_are_refused(self):
        q = self.make(start=False)
        for size in (0, -1, True, "3", 1.5):
            with self.subTest(size=size):
                self.assertEqual(self.refused(self.submit, q, group="g",
                                              group_size=size).reason, "bad_group_size")
        self.assertEqual(self.refused(self.submit, q, group_size=2).reason,
                         "bad_group_size", "a size needs the group it sizes")
        self.submit(q, group="g", group_size=2)
        self.assertEqual(self.refused(self.submit, q, group="g", group_size=3).reason,
                         "bad_group_size")
        self.submit(q, group="g")
        self.assertEqual(self.refused(self.submit, q, group="g").reason, "group_full")


class NativeScopeRows(Case):
    """A folder is the unit a queue belongs to. These pin that two folders'
    queues never touch each other - which is NOT scoping by an application's
    own key inside one folder: the queue can read no such key."""

    def test_prune_touches_only_its_own_folder(self):
        # a folder is the unit a queue belongs to: another folder's queue is
        # not this one's to clear
        mine = self.make()
        other = self.make(folder=self.root / "other")
        a = self.submit(mine, {"fail": "x"})
        b = self.submit(other, {"fail": "y"})
        self.settle(mine, a)
        self.settle(other, b)
        mine.prune()
        self.assertIsNone(mine.get(a))
        self.assertIsNotNone(other.get(b), "another folder is not yours to clear")

    def test_a_fan_in_one_folder_is_never_touched_by_another_folder(self):
        app_a, app_b = FanApp(), FanApp()
        qa = self.make(app_a)
        qb = self.make(app_b, folder=self.root / "somebody-else")
        a = StoppedFanRows.start_fan(self, qa, n=2)
        b = StoppedFanRows.start_fan(self, qb, n=2, fail_at=1)
        self.settle(qa, *a)
        self.settle(qb, *b)
        self.idle(qa)
        self.idle(qb)
        self.assertEqual(app_a.settlings[0]["partial"], False)
        self.assertEqual(app_b.settlings[0]["partial"], True)



class NativeStoreRows(Case):
    def test_a_transient_read_denial_is_retried_and_never_answered_as_empty(self):
        # found by a flaky run: a reader refused for a moment while another
        # writer swaps the file in (Windows). Retried like the swap is.
        q = self.make(start=False)
        jid = self.submit(q)
        real, n = q.store.read_bytes, []

        def denied_twice():
            n.append(1)
            if len(n) <= 2:
                raise PermissionError(13, "Permission denied")
            return real()
        q.store.read_bytes = denied_twice
        self.assertEqual([j["id"] for j in q.jobs()], [jid])
        self.assertEqual(q.store_fault()["count"], 0, "a transient refusal is not a fault")

    def test_a_read_denial_that_lasts_is_a_fault_and_writes_nothing(self):
        q = self.make(start=False)
        jid = self.submit(q)
        before = q.store.path.read_bytes()
        q.store.read_bytes = lambda: (_ for _ in ()).throw(PermissionError(13, "denied"))
        with self.assertRaises(Q.StoreUnreadable):
            q.cancel(jid)
        self.assertEqual(q.store_fault()["kind"], "PermissionError")
        self.assertEqual(q.store.path.read_bytes(), before)

    def test_any_other_error_reading_the_store_is_a_fault_not_an_empty_store(self):
        # Only a MISSING file is an empty store. Any other error reading it -
        # a failing disk, a share that dropped - is a fault, because every
        # writer reads, edits and writes back: answered as empty, the next
        # submit would lay a one-job store over every job on disk. The denial
        # rows above cover PermissionError only (found by a mutation sweep).
        q = self.make(start=False)
        for _ in range(3):
            self.submit(q)
        before = q.store.path.read_bytes()
        q.store.read_bytes = lambda: (_ for _ in ()).throw(OSError(5, "Input/output error"))
        with self.assertRaises(Q.StoreUnreadable):
            self.submit(q)
        self.assertEqual(q.store_fault()["kind"], "OSError")
        self.assertEqual(q.store.path.read_bytes(), before,
                         "a store that could not be READ must not be REWRITTEN")

    def test_the_store_is_written_with_lf_line_endings(self):
        """`Path.write_text` writes CRLF on Windows (GUIDE, traps): measured,
        45 of 45 newlines of a jobs.json were CRLF. The row reads the bytes -
        a reader that parses the JSON cannot tell."""
        q = self.make(start=False)
        for _ in range(3):
            self.submit(q)
        raw = q.store.path.read_bytes()
        newlines, crlf = raw.count(b"\n"), raw.count(b"\r\n")
        self.assertGreater(newlines, 10, "a store of one line: the row could not fail")
        self.assertEqual(raw.count(b"\r"), 0,
                         f"the job store was written with CRLF: {crlf} of {newlines} newlines")

    def test_every_record_a_run_writes_is_a_contract_job_record(self):
        # the tearDown check runs for every row in this file; this row makes
        # sure it is not vacuous - a record that drifts is caught
        q = self.make()
        jid = self.submit(q)
        self.wait_status(q, jid, "done")
        row = self.raw(q, jid)
        self.assertEqual(V.check(row["job"], SHAPES["job"], SHAPES), [])
        drifted = dict(row["job"], status="consumed")
        self.assertNotEqual(V.check(drifted, SHAPES["job"], SHAPES), [])


# ============================ native: rules a mutation sweep found unpinned
#
# Every rule below was already in the queue. Each row here exists because
# breaking its rule on purpose left all the other rows green.

class NativeRecoveryRows(Case):
    def running_when_stopped(self, backend):
        """A generation that ignores cancels, in flight when the server stops."""
        q = self.make(backend=backend)
        jid = self.submit(q, {"gate": "hold", "stubborn": True, "quiet": True})
        self.wait_for(lambda: backend.calls, "the generation to reach the backend")
        return q, jid

    def test_a_cancel_asked_before_the_server_stopped_stands(self):
        # A cancel is an answer already given. A job still `running` at the
        # stop, because its generation had not let go yet, must not come back
        # as work: re-queued, the next pick would clear the flag and run it in
        # full - the one thing the person asked not to happen.
        q1, jid = self.running_when_stopped(Backend())
        self.assertEqual(q1.cancel(jid)["job"]["status"], "running",
                         "the generation ignores the cancel, so the job is still running")
        self.crash(q1)
        again = Backend()
        again.healthy = True
        q2 = self.make(backend=again)
        j = self.wait_status(q2, jid, *Q.FINISHED)
        self.idle(q2)
        self.assertEqual((j["status"], again.calls), ("cancelled", []))
        self.assertIn("before the server stopped", j["error"])

    def test_a_job_requeued_by_a_restart_keeps_its_place(self):
        # the order is by arrival, and a restart is not an arrival
        q1, first = self.running_when_stopped(Backend())
        later = [self.submit(q1) for _ in range(2)]
        self.crash(q1)
        q2 = self.make(backend=Backend(recover=lambda h: None), start=False)
        self.assertEqual(q2.recover()["requeued"], 1)
        self.assertEqual(self.queued_order(q2), [first] + later)


class NativeKeepRows(Case):
    """What the store must not drop, whatever it is asked to clear."""

    @staticmethod
    def finish(q):
        """The worker's two steps, taken by hand: pick the head, end it done."""
        gen = q._beat["gen"]
        picked = q._pick(gen)
        q._finish(picked["id"], gen, "done")
        return picked["id"]

    def test_the_cap_on_finished_rows_never_takes_an_unfinished_one(self):
        # The cap counts FINISHED rows and trims only those. A cap on the
        # whole list, trimmed oldest first, deletes the job that has waited
        # longest - never run, never reported - once the store is full.
        q = self.make(start=False, keep_finished=2)
        waiting = self.submit(q, priority="batch")
        for _ in range(4):
            self.submit(q, priority="interactive")
            self.finish(q)
        self.assertIsNotNone(q.get(waiting), "the cap deleted a job that never ran")
        self.assertEqual(self.status(q, waiting), "queued")
        self.assertEqual(len([j for j in q.jobs() if j["status"] in Q.FINISHED]), 2,
                         "the cap did trim - this row is not passing because it never ran")

    def test_the_cap_never_takes_a_finished_row_something_still_waits_on(self):
        q = self.make(start=False, keep_finished=1)
        first = self.submit(q)
        self.finish(q)
        # CHANGED ON PURPOSE (S18b): a job that names no priority is live, and
        # would go first; the job that waits here is a batch job, and says so
        needs = self.submit(q, after=[first], priority="batch")
        for _ in range(2):
            self.submit(q, priority="interactive")
            self.finish(q)
        self.assertIsNotNone(q.get(first), "the cap took a result a queued job waits on")
        picked = q._pick(q._beat["gen"])
        self.assertEqual(picked["id"], needs)
        self.assertIn(first, picked["after"])

    def test_forgetting_a_result_something_still_waits_on_is_refused(self):
        q = self.make(start=False)
        first = self.submit(q)
        self.finish(q)
        needs = self.submit(q, after=[first])
        e = self.refused(q.forget, first)
        self.assertEqual(e.reason, "needed")
        self.assertIn(needs, e.error)
        self.assertIsNotNone(q.get(first))


class NativeRetryRows(Case):
    def test_a_retried_job_is_not_retried_again_only_its_latest_attempt(self):
        q = self.make()
        jid = self.submit(q, {"fail": "the model went away"})
        self.wait_status(q, jid, "failed")
        again = q.retry(jid)["job"]["id"]
        self.wait_status(q, again, "failed")
        e = self.refused(q.retry, jid)
        self.assertEqual((e.reason, e.code), ("already_retried", 409))
        self.assertEqual([j["id"] for j in q.jobs() if j["retry_of"] == jid], [again])
        self.assertEqual(q.retry(again)["job"]["retry_of"], again)


class NativeStallRows(Case):
    def test_a_report_that_says_nothing_new_is_not_movement(self):
        # A generation that keeps reporting the same numbers is not moving.
        # The deadline is on CHANGE, never on a write.
        def same_again(request, params, run):
            while not run.stopped():
                run.report(stage="writing", tokens=1)
                time.sleep(0.02)
            raise Q.Cancelled("the backend saw the cancel")
        q = self.make(backend=Backend(same_again), stall_s=0.3)
        jid = self.submit(q)
        j = self.wait_status(q, jid, *Q.FINISHED)
        self.assertEqual(j["status"], "failed")
        self.assertRegex(j["error"], "no progress")

    def test_asking_again_does_not_take_a_result_that_is_being_landed(self):
        # Asking again takes the queue from a generation that stopped moving.
        # A result the application is still taking has finished generating;
        # taking it back would throw the result away.
        landing, held = threading.Event(), threading.Event()
        self.addCleanup(held.set)

        def land(job, result):
            landing.set()
            held.wait(5)
        q = self.make(App(land=land), stale_head_s=0.2)
        first = self.submit(q, {"text": "worth keeping"})
        self.wait_for(landing.is_set, "the result to reach the application")
        time.sleep(0.4)
        second = self.submit(q)
        self.assertEqual(self.status(q, first), "running", "a landing is not a stalled head")
        held.set()
        self.settle(q, first, second)
        self.assertEqual(self.status(q, first), "done")
        self.assertEqual(q.result(first)["candidates"][0]["text"], "worth keeping")


# ================================= native: a scope, an ending, one folder

class NativeScopeKeyRows(Case):
    """`scope` is the application's own key inside one folder - a book, a
    project. The queue reads it for nothing but narrowing a list or a prune."""

    def test_a_list_can_be_narrowed_to_one_scope(self):
        q = self.make(start=False)
        one = [self.submit(q, scope="book-1") for _ in range(2)]
        two = self.submit(q, scope="book-2")
        none = self.submit(q)
        self.assertEqual([j["id"] for j in q.jobs(scope="book-1")], one)
        self.assertEqual([j["id"] for j in q.jobs(scope="book-2")], [two])
        self.assertEqual([j["id"] for j in q.jobs()], one + [two, none])
        self.assertIsNone(q.get(none)["scope"])

    def test_prune_can_be_scoped_to_one_scope(self):
        # the one row of the working application's that this queue could not
        # carry until a job had a key it could read: two books in one folder,
        # one book's failures cleared, the other book's kept
        q = self.make()
        mine = [self.submit(q, {"fail": "x"}, scope="book-1") for _ in range(2)]
        theirs = self.submit(q, {"fail": "y"}, scope="book-2")
        self.settle(q, *mine, theirs)
        self.idle(q)
        self.assertEqual(q.prune(statuses=["failed"], scope="book-1")["removed"], 2)
        self.assertEqual([j["id"] for j in q.jobs()], [theirs])

    def test_a_negative_keep_holds_nothing_back(self):
        q = self.make()
        ids = [self.submit(q, {"fail": f"x{i}"}) for i in range(3)]
        self.settle(q, *ids)
        self.idle(q)
        self.assertEqual(q.prune(statuses=["failed"], keep=-1)["removed"], 3)

    def test_a_retry_keeps_its_scope(self):
        q = self.make()
        jid = self.submit(q, {"fail": "x"}, scope="book-1")
        self.wait_status(q, jid, "failed")
        self.idle(q)
        self.assertEqual(q.retry(jid)["job"]["scope"], "book-1")


class EndedApp(App):
    """Records every ending it is told, in one log with landings and merges."""

    def __init__(self, on_ended=None, **kw):
        super().__init__(**kw)
        self.told, self.log, self._on_ended = [], [], on_ended

    def land_result(self, job, result):
        self.log.append(("land", job["id"]))
        return super().land_result(job, result)

    def merge(self, group, members):
        self.log.append(("merge", group))
        return super().merge(group, members)

    def ended(self, job, result):
        self.told.append((job["id"], job["status"], job["error"]))
        self.log.append(("ended", job["id"]))
        if self._on_ended:
            self._on_ended(job, result)


class NativeEndedRows(Case):
    @staticmethod
    def end_by_hand(q, status, error=None):
        """The worker's two steps without its third: nothing tells anyone."""
        jid = Case.submit(q)
        gen = q._beat["gen"]
        q._pick(gen)
        q._finish(jid, gen, status, error)
        return jid

    def test_every_way_a_job_ends_is_told_once(self):
        app = EndedApp()
        q = self.make(app)
        ok = self.submit(q, {"text": "fine"})
        bad = self.submit(q, {"fail": "the model went away"})
        self.settle(q, ok, bad)
        waits = self.submit(q, after=[bad])      # stopped at once: what it waits on failed
        self.settle(q, waits)
        self.idle(q)
        q._drain_hooks()                         # and a second pass tells nothing again
        self.assertEqual(sorted((j, st) for j, st, _ in app.told),
                         sorted([(ok, "done"), (bad, "failed"), (waits, "cancelled")]))
        self.assertIn(bad, {j: e for j, _, e in app.told}[waits])

    def test_a_success_is_landed_before_it_is_told_it_ended(self):
        app = EndedApp()
        q = self.make(app)
        jid = self.submit(q)
        self.settle(q, jid)
        self.idle(q)
        self.assertEqual([e for e in app.log if e[1] == jid], [("land", jid), ("ended", jid)])

    def test_a_claim_is_not_an_ending(self):
        app = EndedApp()
        q = self.make(app)
        jid = self.submit(q)
        self.settle(q, jid)
        self.idle(q)
        q.claim(jid)
        q._drain_hooks()
        self.assertEqual([t[0] for t in app.told], [jid])

    def test_an_ending_owed_when_the_server_stopped_is_told_when_it_starts(self):
        q1 = self.make(EndedApp(), start=False)
        jid = self.end_by_hand(q1, "failed", "the card was taken")
        self.assertEqual(q1.hooks.told, [], "nothing has told it yet")
        self.crash(q1)
        app = EndedApp()
        self.make(app)                           # the start pays what is owed
        self.wait_for(lambda: app.told, "the owed ending to be told")
        self.assertEqual(app.told, [(jid, "failed", "the card was taken")])

    def test_an_ending_being_told_when_the_server_stopped_is_told_again(self):
        # AT LEAST ONCE: the server stops while the hook is running, so the
        # debt was never marked paid, and the next start pays it again
        q1 = None

        def stop_mid_telling(job, result):
            self.crash(q1)
        q1 = self.make(EndedApp(on_ended=stop_mid_telling), start=False)
        jid = self.end_by_hand(q1, "done")
        q1._drain_hooks()
        self.assertEqual([t[0] for t in q1.hooks.told], [jid], "it was being told")
        app = EndedApp()
        self.make(app)
        self.wait_for(lambda: app.told, "the ending to be told again")
        self.assertEqual([t[0] for t in app.told], [jid])

    def test_a_hook_that_raises_is_told_once_not_for_ever(self):
        def boom(job, result):
            raise RuntimeError("the seal could not be written")
        app = EndedApp(on_ended=boom)
        q = self.make(app)
        jid = self.submit(q)
        self.settle(q, jid)
        self.idle(q)
        q._drain_hooks()
        self.assertEqual(len(app.told), 1)
        self.assertIn("seal could not be written", self.raw(q, jid)["run"]["ending_error"])
        self.assertTrue(any("ended hook failed" in s for s in self.said))

    def interrupted_while_the_store_is_unreadable(self, app_for):
        """Tell an ending, or a merge, whose hook squeezes the store and then
        raises an interrupt - not an application error. -> what `_drain_hooks`
        did with it."""
        squeeze = None

        def interrupt(*args):
            squeeze.__enter__()                     # the mark after it cannot be read
            raise KeyboardInterrupt
        q = self.make(app_for(interrupt), start=False)
        squeeze = Squeeze(q)
        return q, squeeze

    def test_an_interrupt_in_the_ended_hook_goes_on_up_when_nothing_can_be_marked(self):
        """An interrupt raised in a hook is not an application error to note and
        move past: it goes on up - even when the store cannot be read to mark
        the ending told. A `return` inside the `finally` that marks it had
        swallowed it there."""
        q, squeeze = self.interrupted_while_the_store_is_unreadable(
            lambda interrupt: EndedApp(on_ended=interrupt))
        self.end_by_hand(q, "done")
        try:
            with self.assertRaises(KeyboardInterrupt):
                q._drain_hooks()
        finally:
            squeeze.__exit__(None, None, None)

    def test_an_interrupt_in_the_merge_goes_on_up_when_nothing_can_be_marked(self):
        class Merging(EndedApp):
            def __init__(self, interrupt):
                super().__init__()
                self.interrupt = interrupt

            def merge(self, group, members):
                self.interrupt()
        q, squeeze = self.interrupted_while_the_store_is_unreadable(Merging)
        jid = self.submit(q, group="g", group_size=1)
        gen = q._beat["gen"]
        q._pick(gen)
        q._finish(jid, gen, "done", None)
        q._drain_endings()                          # the member's ending first, as always
        try:
            with self.assertRaises(KeyboardInterrupt):
                q._drain_merges()
        finally:
            squeeze.__exit__(None, None, None)

    def test_a_fan_members_ending_is_told_before_its_fan_is_merged(self):
        app = EndedApp()
        q = self.make(app)
        ids = [self.submit(q, {"text": f"r{i}"}, group="g", group_size=2) for i in (1, 2)]
        self.settle(q, *ids)
        self.idle(q)
        order = [e for e in app.log if e[0] in ("ended", "merge")]
        self.assertEqual(order[-1], ("merge", "g"))
        self.assertEqual(sorted(e[1] for e in order[:-1]), sorted(ids))

    def test_the_hook_may_call_back_into_the_queue(self):
        seen = []
        app = EndedApp(on_ended=lambda job, result: seen.append(len(q.jobs())))
        q = self.make(app)
        self.settle(q, self.submit(q))
        self.wait_for(lambda: seen, "the hook to run")
        self.assertEqual(seen, [1])

    def test_the_cap_never_trims_an_ending_not_yet_told(self):
        q = self.make(start=False, keep_finished=0)
        jid = self.end_by_hand(q, "failed", "x")
        self.assertIsNotNone(q.get(jid), "the cap took a row the application is owed")
        q._drain_hooks()
        self.assertIsNone(q.get(jid), "once told, it is an ordinary finished row")

    def test_forgetting_an_ending_not_yet_told_tells_it_first(self):
        app = EndedApp()
        q = self.make(app, start=False)
        jid = self.end_by_hand(q, "failed", "x")
        self.assertEqual(q.forget(jid), {"forgot": jid})
        self.assertEqual([t[0] for t in app.told], [jid])

    def telling(self, told_wait_s=4.0):
        """A queue whose worker is INSIDE the ended hook for a failed job,
        held there until the row opens the gate."""
        inside, gate = threading.Event(), threading.Event()
        app = EndedApp(on_ended=lambda job, result: (inside.set(), gate.wait(10)))
        q = self.make(app, told_wait_s=told_wait_s)
        self.addCleanup(gate.set)
        jid = self.submit(q, {"fail": "could not build it"})
        self.assertTrue(inside.wait(6), "the worker never reached the hook")
        return q, app, jid, gate

    def test_forgetting_while_the_worker_is_telling_it_waits_for_the_telling(self):
        """FOUND BY A SWEEP'S CONTROL, not by a row: the carried row
        `test_a_finished_job_can_be_forgotten` failed once in 268 when its
        forget landed while the worker was inside the hook. `_drain_endings`
        leaves a job another thread is telling to that thread, so the forget
        found the debt unpaid and refused it."""
        q, app, jid, gate = self.telling()
        threading.Timer(0.2, gate.set).start()
        t0 = time.monotonic()
        self.assertEqual(q.forget(jid), {"forgot": jid})
        self.assertLess(time.monotonic() - t0, 2.0, "it waited out the whole bound: "
                        "nothing woke it when the telling ended")
        self.assertEqual([t[0] for t in app.told], [jid], "told once, not twice")

    def test_a_telling_that_does_not_return_is_waited_for_a_bounded_time(self):
        """The bound is the half that is easy to leave out: an application
        whose hook hangs must not hang every forget with it. Past the bound
        the answer is the true one - it has not been told yet."""
        q, app, jid, gate = self.telling(told_wait_s=0.3)
        t0 = time.monotonic()
        e = self.refused(q.forget, jid)
        waited = time.monotonic() - t0
        self.assertEqual(e.reason, "needed")
        self.assertGreaterEqual(waited, 0.3)
        self.assertLess(waited, 3.0)

    def test_prune_waits_for_an_ending_being_told_too(self):
        q, app, jid, gate = self.telling()
        threading.Timer(0.2, gate.set).start()
        self.assertEqual(q.prune(), {"removed": 1, "left": 0})

    def test_a_hook_that_forgets_its_own_job_is_refused_at_once(self):
        """The wait is for ANOTHER thread's telling. A hook that forgets the
        job it is being told about is inside that telling: the true answer is
        `needed` - the debt is paid only when the hook returns - and it must
        not first wait out the whole bound for itself."""
        seen = []

        def forget_it(job, result):
            t0 = time.monotonic()
            try:
                q.forget(job["id"])
                seen.append(("forgot", time.monotonic() - t0))
            except Q.Refused as e:
                seen.append((e.reason, time.monotonic() - t0))

        q = self.make(EndedApp(on_ended=forget_it), told_wait_s=4.0)
        self.submit(q, {"fail": "could not build it"})
        self.wait_for(lambda: seen, "the hook to run")
        reason, took = seen[0]
        self.assertEqual(reason, "needed")
        self.assertLess(took, 1.0, "it waited for its own telling")


class NativeFolderRows(Case):
    """One live queue per folder. The cross-process half - a real process
    killed, and the folder free the moment it is gone - is proven in the
    server's suite, where there are processes."""

    def test_a_second_queue_on_a_bound_folder_is_refused_naming_the_first(self):
        self.make(start=False, url="http://127.0.0.1:7072")
        with self.assertRaises(Q.FolderInUse) as cm:
            Q.Queue(self.folder, start=False)
        e = cm.exception
        self.assertEqual((e.reason, e.code), ("folder_in_use", 409))
        self.assertEqual((e.extra["holder"]["pid"], e.extra["holder"]["url"]),
                         (os.getpid(), "http://127.0.0.1:7072"))

    def test_a_refused_queue_leaves_the_bound_one_working(self):
        q = self.make(start=False)
        first = self.submit(q)
        with self.assertRaises(Q.FolderInUse):
            Q.Queue(self.folder, start=False)
        second = self.submit(q)
        self.assertEqual([j["id"] for j in q.jobs()], [first, second])

    def test_closing_lets_the_folder_go(self):
        q1 = self.make(start=False)
        jid = self.submit(q1)
        q1.close()
        q2 = self.make(start=False)
        self.assertEqual([j["id"] for j in q2.jobs()], [jid])

    def test_a_closed_queue_is_bound_to_nothing_and_writes_nothing(self):
        q = self.make(start=False)
        self.submit(q)
        before = (self.folder / Q.STORE_NAME).read_bytes()
        q.close()
        self.assertEqual(self.refused(self.submit, q).reason, "no_workspace")
        self.assertFalse(q.health()["loaded"])
        self.assertEqual((self.folder / Q.STORE_NAME).read_bytes(), before)

    def test_a_generation_that_comes_back_after_the_close_writes_nothing(self):
        backend = Backend()
        q = self.make(backend=backend)
        self.submit(q, {"gate": "late"})
        self.wait_for(lambda: backend.calls, "the generation to start")
        q.close(timeout=0.2)                     # it does not come back in time
        before = (self.folder / Q.STORE_NAME).read_bytes()
        backend.gate("late").set()               # and now it does
        time.sleep(0.4)
        self.assertEqual((self.folder / Q.STORE_NAME).read_bytes(), before,
                         "a queue that let the folder go wrote to it afterwards")
        self.make(start=False)                   # and the folder is free

    def test_a_close_gives_a_worker_that_does_not_come_back_one_timeout(self):
        """Found on the real card: a stop mid-answer spent ten seconds here for
        a timeout of five - the worker was waited for twice, once as the worker
        and once more in the list of every thread the queue had started."""
        backend = Backend()
        q = self.make(backend=backend)
        self.submit(q, {"gate": "never"})
        self.wait_for(lambda: backend.calls, "the generation to start")
        started = time.monotonic()
        q.close(timeout=1.0)
        took = time.monotonic() - started
        self.assertLess(took, 1.5, f"the close waited {took:.2f}s for a timeout of 1.0")
        self.assertGreater(took, 0.9, "the close gave the worker no time at all")


# ============================================== native: the request record

class ManifestApp(App):
    """Names the inputs a request was built from, and remembers being asked."""

    def __init__(self, inputs=None, fail=None, **kw):
        super().__init__(**kw)
        self.inputs, self.fail, self.asked = inputs, fail, []

    def manifest(self, job, after_results):
        self.asked.append((job["id"], after_results))
        if self.fail:
            raise self.fail
        return self.inputs


class BareHooks:
    """An application's hooks written before the seventh existed."""

    def build_request(self, job, after_results):
        return job.get("input")

    def land_result(self, job, result):
        return None

    def sampling_for(self, kind, input):
        return {}


class NativeRequestRecordRows(Case):
    """Every job leaves a record in the bound folder, named by its
    correlation id: the manifest of what it was built from, the request as
    built, its own story, and a folder per attempt."""

    CORR = "c-20260923-120000-abcdefgh"

    def record(self, corr, folder=None):
        return Q.request_record(folder or self.folder, corr)

    @staticmethod
    def story(rec):
        return [e["event"] for e in rec["events"]]

    def test_every_job_leaves_a_record_named_by_its_correlation_id(self):
        q = self.make()
        jid = q.submit("generate", {"text": "Rain on the tin roof."}, {"model": MODEL},
                       correlation_id=self.CORR)["job_id"]
        self.settle(q, jid)
        self.idle(q)
        rec = self.record(self.CORR)
        self.assertEqual(rec["dir"], self.folder / "requests" / self.CORR)
        self.assertEqual(self.story(rec), ["submitted", "started", "attempt_started",
                                           "attempt_ended", "ended"])
        self.assertTrue(all(e["job"] == jid for e in rec["events"]))
        self.assertEqual(V.check(rec["events"][0]["record"], SHAPES["job"], SHAPES), [],
                         "the first line carries the contract's job record")
        self.assertEqual(rec["manifest"]["job"]["id"], jid)
        self.assertEqual(rec["request"], {"text": "Rain on the tin roof."})
        self.assertEqual(list(rec["samples"]), ["01"])
        cand = q.result(jid)["candidates"][0]
        self.assertEqual(rec["samples"]["01"]["candidate.json"], cand)
        self.assertEqual(rec["samples"]["01"]["params.json"]["handle"], cand["handle"])
        ended = rec["events"][-1]
        self.assertEqual((ended["status"], ended["error"], ended["by"]), ("done", None, "worker"))

    def test_the_manifest_names_what_the_application_says_it_was_built_from(self):
        app = ManifestApp(inputs={"template": "scene-v3", "sha256": "9f2c"},
                          build=lambda job, after: {"text": job["input"]["text"],
                                                    "built": job["id"]})
        q = self.make(app)
        first = self.submit(q, {"text": "The first part."})
        self.settle(q, first)
        second = self.submit(q, {"text": "The second part."}, after=[first])
        self.settle(q, second)
        self.idle(q)
        rec = self.record(q.get(second)["correlation_id"])
        self.assertEqual(rec["request"], {"text": "The second part.", "built": second},
                         "the record keeps the request as built, not the job's input")
        self.assertEqual(rec["manifest"]["inputs"], {"template": "scene-v3", "sha256": "9f2c"})
        self.assertEqual(rec["manifest"]["waited_on"],
                         [{"job": first, "correlation_id": q.get(first)["correlation_id"]}])
        (asked_first, _), (asked_second, results) = app.asked
        self.assertEqual((asked_first, asked_second), (first, second))
        self.assertEqual(results[first]["candidates"][0]["text"], "The first part.",
                         "the manifest is asked with what the job waited on")

    def test_an_application_that_names_nothing_leaves_an_empty_manifest(self):
        for hooks in (App(), BareHooks(), ManifestApp(inputs=None)):
            with self.subTest(hooks=type(hooks).__name__):
                folder = self.root / type(hooks).__name__
                folder.mkdir()
                q = self.make(hooks, folder=folder)
                jid = self.submit(q)
                self.assertEqual(self.wait_status(q, jid, *Q.FINISHED)["status"], "done")
                self.idle(q)
                rec = self.record(q.get(jid)["correlation_id"], folder)
                self.assertEqual(rec["manifest"]["inputs"], {})
                self.assertNotIn("inputs_error", rec["manifest"])

    def test_a_manifest_that_fails_is_written_down_and_the_job_goes_on(self):
        q = self.make(ManifestApp(fail=OSError("the template file is gone")))
        jid = self.submit(q)
        self.assertEqual(self.wait_status(q, jid, *Q.FINISHED)["status"], "done")
        self.idle(q)
        rec = self.record(q.get(jid)["correlation_id"])
        self.assertEqual(rec["manifest"]["inputs"], {})
        self.assertIn("the template file is gone", rec["manifest"]["inputs_error"])
        self.assertTrue(any("manifest hook failed" in s and jid in s for s in self.said),
                        self.said)

    def test_each_attempt_has_its_own_folder_and_its_own_seed(self):
        judge = {"n": 0}

        def refuse_the_first(job, cand):
            judge["n"] += 1
            return {"why": "too short"} if judge["n"] == 1 else {"why": None}
        q = self.make(App(judge=refuse_the_first))
        jid = self.submit(q, gen={"n": 2, "seed": 7})
        self.settle(q, jid)
        self.idle(q)
        rec = self.record(q.get(jid)["correlation_id"])
        self.assertEqual(list(rec["samples"]), ["01", "02", "03"])
        seeds = [rec["samples"][s]["params.json"]["params"]["seed"] for s in ("01", "02", "03")]
        self.assertEqual(seeds, [7, 107, 8], "variant 1 twice (a judged retry), then variant 2")
        self.assertEqual([rec["samples"][s]["params.json"]["variant"] for s in ("01", "02", "03")],
                         [1, 1, 2])
        ends = [e for e in rec["events"] if e["event"] == "attempt_ended"]
        self.assertEqual([(e["sample"], e["outcome"]) for e in ends],
                         [(1, "refused"), (2, "answered"), (3, "answered")])
        self.assertEqual(ends[0]["verdict"], {"why": "too short"})
        self.assertIn("candidate.json", rec["samples"]["01"],
                      "what a refused attempt answered is kept too")

    def test_a_failed_attempt_says_how_and_keeps_no_candidate(self):
        q = self.make()
        jid = self.submit(q, {"fail": "the model went away"})
        self.assertEqual(self.wait_status(q, jid, *Q.FINISHED)["status"], "failed")
        self.idle(q)
        rec = self.record(q.get(jid)["correlation_id"])
        (end,) = [e for e in rec["events"] if e["event"] == "attempt_ended"]
        self.assertEqual((end["outcome"], end["error"]), ("failed", "the model went away"))
        self.assertEqual(set(rec["samples"]["01"]), {"params.json"})
        self.assertEqual((rec["events"][-1]["event"], rec["events"][-1]["status"]),
                         ("ended", "failed"))

    def test_a_judge_that_fails_ends_its_attempt_in_the_record(self):
        def broken(job, cand):
            raise KeyError("verdict")
        q = self.make(App(judge=broken))
        jid = self.submit(q)
        self.assertEqual(self.wait_status(q, jid, *Q.FINISHED)["status"], "failed")
        self.idle(q)
        rec = self.record(q.get(jid)["correlation_id"])
        (end,) = [e for e in rec["events"] if e["event"] == "attempt_ended"]
        self.assertEqual(end["outcome"], "failed")
        self.assertIn("the judge failed: KeyError", end["error"])

    def test_a_cancelled_generation_says_so_in_the_record(self):
        backend = Backend()
        q = self.make(backend=backend)
        jid = self.submit(q, {"gate": "hold"})
        self.wait_for(lambda: backend.calls, "the generation to start")
        q.cancel(jid)
        self.assertEqual(self.wait_status(q, jid, *Q.FINISHED)["status"], "cancelled")
        self.idle(q)
        rec = self.record(q.get(jid)["correlation_id"])
        (end,) = [e for e in rec["events"] if e["event"] == "attempt_ended"]
        self.assertEqual(end["outcome"], "cancelled")
        self.assertEqual(rec["events"][-1]["status"], "cancelled")

    def test_a_job_that_never_ran_still_leaves_its_story(self):
        q = self.make(start=False)
        jid = self.submit(q)
        q.cancel(jid)
        rec = self.record(q.get(jid)["correlation_id"])
        self.assertEqual(self.story(rec), ["submitted", "ended"])
        self.assertEqual((rec["events"][-1]["status"], rec["events"][-1]["by"]),
                         ("cancelled", "cancel"))
        self.assertIsNone(rec["manifest"])
        self.assertEqual(rec["samples"], {})

    def test_an_ending_is_in_the_story_once_however_often_it_is_told(self):
        q = self.make(start=False)
        jid = self.submit(q)
        q.cancel(jid)
        # a server that stopped after telling, before it wrote down that it had
        doc = json.loads(q.store.path.read_text(encoding="utf-8"))
        doc["jobs"][0]["run"]["ending_told"] = False
        q.store.path.write_text(json.dumps(doc), encoding="utf-8")
        q._drain_hooks()
        self.assertIs(self.raw(q, jid)["run"]["ending_told"], True, "it was told again")
        self.assertEqual(self.story(self.record(q.get(jid)["correlation_id"])),
                         ["submitted", "ended"])

    def test_the_ending_is_dated_when_it_ended_not_when_it_was_told(self):
        now = [1000.0]
        q = self.make(start=False, clock=lambda: now[0])
        jid = NativeEndedRows.end_by_hand(q, "done")
        now[0] = 2000.0
        q._drain_hooks()
        ended = self.record(q.get(jid)["correlation_id"])["events"][-1]
        self.assertEqual((ended["event"], ended["ts"]), ("ended", 1000.0))

    def test_a_job_claimed_before_its_ending_was_told_ended_done(self):
        q = self.make(start=False)
        jid = NativeEndedRows.end_by_hand(q, "done")
        q.claim(jid, client="cli-k3x9m2p7")
        q._drain_hooks()
        rec = self.record(q.get(jid)["correlation_id"])
        claimed = [e for e in rec["events"] if e["event"] == "claimed"]
        self.assertEqual([e["by"] for e in claimed], ["cli-k3x9m2p7"])
        self.assertEqual(rec["events"][-1]["status"], "done")

    def test_what_a_backend_keeps_lands_in_its_attempts_folder(self):
        seen = {}

        def keeps(request, params, run):
            seen.update(corr=run.correlation_id, sample=run.sample,
                        call=run.record("call.json", {"exact": "call", "seed": params["seed"]}),
                        text=run.record("prompt.txt", "the words, as sent"))
            return {"text": "An answer."}
        q = self.make(backend=Backend(keeps))
        jid = self.submit(q, gen={"seed": 5})
        self.settle(q, jid)
        self.idle(q)
        corr = q.get(jid)["correlation_id"]
        self.assertEqual((seen["corr"], seen["sample"], seen["call"], seen["text"]),
                         (corr, 1, True, True))
        kept = self.record(corr)["samples"]["01"]
        self.assertEqual(kept["call.json"], {"exact": "call", "seed": 5})
        self.assertEqual(kept["prompt.txt"], "the words, as sent")

    def test_a_backend_keeps_a_plain_name_and_never_one_of_the_queues(self):
        answers = {}
        bad = ["../escape.json", "a/b.json", "a\\b.json", "", ".hidden", "x" * 65, 7,
               "params.json", "Candidate.JSON"]

        def tries(request, params, run):
            for name in bad:
                answers[repr(name)] = run.record(name, {"mine": True})
            return {"text": "An answer."}
        q = self.make(backend=Backend(tries))
        jid = self.submit(q)
        self.settle(q, jid)
        self.idle(q)
        self.assertEqual(set(answers.values()), {False}, answers)
        rec = self.record(q.get(jid)["correlation_id"])
        self.assertEqual(set(rec["samples"]["01"]), {"params.json", "candidate.json"})
        self.assertEqual(rec["samples"]["01"]["params.json"]["sample"], 1,
                         "the queue's own file was written over")
        self.assertEqual(rec["samples"]["01"]["candidate.json"]["text"], "An answer.")
        self.assertFalse((rec["dir"] / "samples" / "escape.json").exists())
        self.assertEqual(len([s for s in self.said if "refused a file named" in s]), len(bad))

    def test_a_record_that_cannot_be_written_never_fails_the_job(self):
        (self.folder / "requests").write_text("a file where the record's folder goes",
                                              encoding="utf-8")
        kept = []

        def keeps(request, params, run):
            kept.append(run.record("call.json", {"exact": "call"}))
            return {"text": "An answer."}
        q = self.make(backend=Backend(keeps))
        jid = self.submit(q)
        self.assertEqual(self.wait_status(q, jid, *Q.FINISHED)["status"], "done")
        self.idle(q)
        self.assertEqual(q.result(jid)["candidates"][0]["text"], "An answer.")
        self.assertEqual(kept, [False])
        self.assertTrue(any("could not keep" in s for s in self.said), self.said)

    def test_an_attempt_the_queue_did_not_number_keeps_nothing_and_says_so(self):
        """What a backend keeps is filed under its attempt's number. A run
        given anything else - nothing, a word, True (which Python would number
        1), a fraction - keeps nothing anywhere, says why, and never raises.
        The queue itself always numbers its attempts, so a row builds the run."""
        q = self.make(start=False)
        jid = q.submit("generate", {"text": "Rain."}, {"model": MODEL},
                       correlation_id=self.CORR)["job_id"]
        for sample in (None, "01", True, 1.0):
            run = Q.Run(q, jid, None, threading.Event(), correlation_id=self.CORR,
                        sample=sample)
            self.assertIs(run.record("call.json", {"exact": "call"}), False, repr(sample))
        self.assertFalse((self.folder / "requests" / self.CORR / "samples").exists())
        self.assertEqual(len([s for s in self.said if "the attempt is not numbered" in s]), 4,
                         self.said)

    def test_a_record_that_fails_in_a_way_nobody_foresaw_never_raises(self):
        q = self.make(start=False)
        jid = q.submit("generate", {"text": "Rain."}, {"model": MODEL},
                       correlation_id=self.CORR)["job_id"]

        def breaks(*args):
            raise RuntimeError("the disk said something new")
        q._record_write = breaks
        run = Q.Run(q, jid, None, threading.Event(), correlation_id=self.CORR, sample=1)
        self.assertIs(run.record("call.json", {"exact": "call"}), False)
        self.assertTrue(any("the disk said something new" in s for s in self.said), self.said)

    def test_a_record_is_never_written_outside_the_requests_folder(self):
        """A store written by hand, or by an older queue, can carry any string
        as a correlation id. The job still runs; its record does not."""
        self.free_records = True                 # the row's id is not a contract id
        write_store(self.folder, fixture_row("j-0001", "queued",
                                             correlation_id="../outside",
                                             input={"text": "Rain."}))
        q = self.make()
        self.assertEqual(self.wait_status(q, "j-0001", *Q.FINISHED)["status"], "done")
        self.idle(q)
        self.assertFalse((self.folder / "outside").exists(), "the record escaped its folder")
        self.assertFalse((self.folder / "requests").exists())

    def test_a_string_request_is_json_in_the_record_like_any_other(self):
        q = self.make()
        jid = q.submit("generate", "Rain on the tin roof.", {"model": MODEL},
                       correlation_id=self.CORR)["job_id"]
        self.settle(q, jid)
        self.idle(q)
        raw = (self.folder / "requests" / self.CORR / "request.json").read_text(encoding="utf-8")
        self.assertEqual(json.loads(raw), "Rain on the tin roof.")

    def test_a_write_that_fails_leaves_nothing_half_written(self):
        q = self.make(start=False)
        q.submit("generate", {"text": "Rain."}, {"model": MODEL}, correlation_id=self.CORR)
        blocked = self.folder / "requests" / self.CORR / "samples" / "01" / "candidate.json"
        blocked.mkdir(parents=True)              # a folder where the file goes
        q.start()
        self.assertEqual(self.wait_status(q, "j-0001", *Q.FINISHED)["status"], "done")
        self.idle(q)
        left = [p.name for p in (self.folder / "requests").rglob("*") if p.name.endswith(".tmp")]
        self.assertEqual(left, [], "a failed write left its temporary file behind")
        self.assertTrue(any("could not keep samples/01/candidate.json" in s for s in self.said),
                        self.said)

    def test_the_record_outlives_the_job(self):
        q = self.make()
        jid = self.submit(q)
        self.settle(q, jid)
        self.idle(q)
        corr = q.get(jid)["correlation_id"]
        before = self.story(self.record(corr))
        q.forget(jid)
        self.assertIsNone(q.get(jid))
        self.assertEqual(self.story(self.record(corr)), before)
        self.assertIn("01", self.record(corr)["samples"])

    def test_a_retry_is_a_new_request_that_names_the_one_it_runs_again(self):
        backend = Backend({"fail": "the model went away"})
        q = self.make(backend=backend)
        old = self.submit(q)
        self.settle(q, old)
        self.idle(q)
        old_corr = q.get(old)["correlation_id"]
        before = self.record(old_corr)["events"]
        new = q.retry(old)["job"]["id"]
        self.settle(q, new)
        self.idle(q)
        new_corr = q.get(new)["correlation_id"]
        self.assertNotEqual(new_corr, old_corr)
        first = self.record(new_corr)["events"][0]
        self.assertEqual((first["event"], first["record"]["retry_of"],
                          first["retry_of_correlation"]), ("submitted", old, old_corr))
        self.assertEqual(self.record(new_corr)["events"][-1]["status"], "done")
        self.assertEqual(self.record(old_corr)["events"], before,
                         "the retry wrote into the record of the request it ran again")

    def test_a_run_a_stopped_server_left_says_what_became_of_it(self):
        backend = Backend()
        q1 = self.make(backend=backend)
        jid = self.submit(q1, {"gate": "hold", "stubborn": True, "quiet": True})
        self.wait_for(lambda: backend.calls, "the generation to reach the backend")
        self.crash(q1)
        healthy = Backend(recover=lambda h: None)
        healthy.healthy = True
        q2 = self.make(backend=healthy, start=False)
        self.assertEqual(q2.recover()["requeued"], 1)
        # the stopped server's generation comes back late - and writes nothing,
        # as a killed process writes nothing
        backend.gate("hold").set()
        self.wait_for(lambda: q1._worker_job is None, "the stopped server's worker to let go")
        q2.start()
        self.settle(q2, jid)
        self.idle(q2)
        rec = self.record(q2.get(jid)["correlation_id"])
        self.assertEqual(self.story(rec), ["submitted", "started", "attempt_started",
                                           "recovered", "started", "attempt_started",
                                           "attempt_ended", "ended"])
        self.assertEqual(rec["events"][3]["settled_as"], "requeued")
        self.assertEqual(set(rec["samples"]["01"]), {"params.json"},
                         "the stopped server wrote its late answer into the record")
        self.assertEqual(set(rec["samples"]["02"]), {"params.json", "candidate.json"})

    def test_every_way_a_start_settles_a_stopped_run_is_on_the_record(self):
        held = []                                # landings the stopped server never finished

        def adopted(folder):
            backend = Backend()
            q1 = self.make(backend=backend, folder=folder)
            jid = self.submit(q1, {"gate": "hold", "stubborn": True, "quiet": True})
            self.wait_for(lambda: backend.calls, "the generation to reach the backend")
            self.crash(q1)
            return jid, Backend(recover=lambda h: {"text": f"finished as {h}"})

        def cancelled(folder):
            backend = Backend()
            q1 = self.make(backend=backend, folder=folder)
            jid = self.submit(q1, {"gate": "hold", "stubborn": True, "quiet": True})
            self.wait_for(lambda: backend.calls, "the generation to reach the backend")
            q1.cancel(jid)                       # asked, and never let go
            self.crash(q1)
            return jid, Backend(recover=lambda h: None)

        def landing(folder):
            gate = threading.Event()
            held.append(gate)
            app = App(land=lambda job, result: gate.wait(5) and None)
            q1 = self.make(app, folder=folder)
            jid = self.submit(q1)
            self.wait_for(lambda: app.landed, "the result to be landing")
            self.crash(q1)
            return jid, Backend(recover=lambda h: None)

        for how, stop in (("adopted", adopted), ("cancelled", cancelled), ("landing", landing)):
            with self.subTest(how=how):
                folder = self.root / how
                folder.mkdir()
                jid, backend = stop(folder)
                q2 = self.make(backend=backend, folder=folder, start=False)
                q2.recover()
                for gate in held:                # the stopped server's landing may end now
                    gate.set()
                rec = self.record(q2.get(jid)["correlation_id"], folder)
                (line,) = [e for e in rec["events"] if e["event"] == "recovered"]
                self.assertEqual(line["settled_as"], how)


class NativeNextUpRows(Case):
    """`next_up` names the job the worker would take next, as things stand -
    what a runner asks when a cancel lands during a load: does the job that
    runs next want the model going up?"""

    def running(self, q):
        """The worker's first step alone: the first job in order is running."""
        return q._pick(q._beat["gen"])["id"]

    def test_the_next_job_is_the_first_in_the_queues_order(self):
        q = self.make(start=False)
        first = self.submit(q, gen={"model": "alpha"})
        self.submit(q, gen={"model": "beta"})
        self.assertEqual(q.next_up()["id"], first)
        self.running(q)
        self.assertEqual(q.next_up()["model"], "beta")

    def test_an_interactive_job_goes_first_as_the_worker_would_take_it(self):
        q = self.make(start=False)
        # CHANGED ON PURPOSE (S18b): a job that names no priority is live; the
        # one an interactive job goes before is batch, and says so
        self.submit(q, gen={"model": "alpha"}, priority="batch")
        urgent = self.submit(q, gen={"model": "beta"}, priority="interactive")
        self.assertEqual(q.next_up()["id"], urgent)

    def test_a_job_waiting_on_the_running_one_is_not_next_nor_is_what_waits_on_it(self):
        q = self.make(start=False)
        first = self.submit(q, gen={"model": "alpha"})
        second = self.submit(q, gen={"model": "alpha"}, after=[first])
        self.submit(q, gen={"model": "alpha"}, after=[second])
        free = self.submit(q, gen={"model": "beta"})
        self.running(q)
        self.assertEqual(q.next_up()["id"], free)

    def test_nothing_runnable_is_no_next_job(self):
        q = self.make(start=False)
        first = self.submit(q)
        self.submit(q, after=[first])
        self.running(q)
        self.assertIsNone(q.next_up())
        self.assertIsNone(Q.Queue(None, start=False).next_up(), "an unbound queue names one")

    def test_asking_takes_nothing(self):
        q = self.make(start=False)
        jid = self.submit(q)
        before = q.store.path.read_bytes()
        self.assertEqual(q.next_up()["id"], jid)
        self.assertEqual(q.store.path.read_bytes(), before)
        self.assertEqual(q.get(jid)["status"], "queued")

    def test_a_store_that_cannot_be_read_has_no_next_job(self):
        q = self.make(start=False)
        self.submit(q)
        with Squeeze(q):
            self.assertIsNone(q.next_up())


class Shares(Backend):
    """A backend that keeps what the queue tells it of each job's own share
    (`job_share`): the model the job named, and the seconds."""

    def __init__(self, *steps):
        super().__init__(*steps)
        self.shares = []

    def job_share(self, model, seconds):
        self.shares.append((model, seconds))


class NativeQueueShareRows(Case):
    """PLAN-4 D12 (S11b): the queue's OWN share of a job - from the pick to
    its end written, less its time in the backend - is told to a backend
    that keeps it (`job_share`), under the model the job named. Measured on
    an empty store and on one of 1 000 jobs."""

    def ran(self, q, backend, sleep):
        jid = self.submit(q, input={"text": "Rain on the tin roof.", "sleep": sleep})
        self.wait_status(q, jid, "done", timeout=20)
        self.idle(q)
        self.wait_for(lambda: backend.shares, "the job's share")
        return backend.shares[-1]

    def test_a_jobs_share_is_its_time_outside_the_backend(self):
        b = Shares()
        q = self.make(backend=b)
        model, share = self.ran(q, b, 1.0)
        self.assertEqual(model, MODEL)
        self.assertGreater(share, 0, "the queue's own work takes some time")
        self.assertLess(share, 1.0, "a second in the backend is not the queue's")
        self.assertEqual(len(b.shares), 1, "one share for one job")

    def test_a_job_that_never_reached_the_backend_has_no_share(self):
        def refuse(job, after):
            raise Q.GenerationFailed("no request could be built for it")
        b = Shares()
        q = self.make(App(build=refuse), backend=b)
        jid = self.submit(q)
        self.wait_status(q, jid, "failed", timeout=20)
        self.idle(q)
        self.assertEqual((b.calls, b.shares), ([], []), "no model ran: no model's share")

    def test_measured_with_an_empty_store_and_with_1000_jobs(self):
        b = Shares()
        q = self.make(backend=b)
        empty = self.ran(q, b, 0.05)[1]
        q.close(3.0)
        full_folder = self.root / "full"
        self.free_records = True
        write_store(full_folder, *[fixture_row(f"j-{i:04d}", "done", finished=200.0 + i,
                                               created=100.0 + i) for i in range(1, 1001)],
                    seq=1000)
        b2 = Shares()
        q2 = self.make(backend=b2, folder=full_folder, keep_finished=2000)
        full = self.ran(q2, b2, 0.05)[1]
        self.assertEqual(len(q2.jobs()), 1001, "the store held its 1 000 jobs")
        self.assertGreater(full, 0)
        print(f"\n  measured: the queue's own share of a job: {empty * 1000:.1f} ms on an "
              f"empty store, {full * 1000:.1f} ms on a store of 1 000 jobs", flush=True)


class Describing(Backend):
    """A backend that describes a request it is asked to hold (`hold`) - its
    engine, its messages, its count - and records what it was handed. `hold`
    may be steered: an exception to raise, a gate to wait on."""

    def __init__(self, *steps, hold=None):
        super().__init__(*steps)
        self.holds, self.hold_step = [], hold

    def hold(self, request, params, run, named):
        self.holds.append({"request": request, "params": dict(params), "named": list(named),
                           "job": run.job_id, "handle": run.handle})
        step = self.hold_step
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, str):
            deaf = step.startswith("deaf:")
            ev = self.gate(step)
            while not ev.wait(0.01):
                if run.stopped() and not deaf:
                    raise Q.Cancelled("cancelled while the request was checked")
        run.report(count={"prompt_tokens": 14, "room": 50, "context": 64})
        return {"engine": "llamacpp", "messages": [{"role": "user", "content": "counted"}],
                "audio": None, "fields": None,
                "params": [{"name": "max_tokens", "value": params.get("max_tokens"),
                            "from": "application" if "max_tokens" in named else "bootstrap"}],
                "count": {"prompt_tokens": 14, "room": 50, "context": 64}, "fits": True,
                "expect": None, "said": ["no expected time yet"]}


class NativeRequestCheckRows(Case):
    """THE REQUEST CHECK (PLAN-4 D16): a job submitted with `check` is held
    once its request is built - at its turn, by the application's build hook
    - with the exact request as it would be sent, until it is sent (as built,
    or changed for this one request) or cancelled. A job not marked never
    waits for anyone; a held job waits for no one; the record keeps both
    what was built and what was sent."""

    CORR = "c-20260927-120000-abcdefgh"

    def held(self, q, jid, timeout=6.0):
        self.wait_status(q, jid, "held", timeout=timeout)
        return q.held(jid)

    def story(self, corr=None):
        return [e["event"] for e in Q.request_record(self.folder, corr or self.CORR)["events"]]

    def test_a_job_not_marked_runs_as_ever_and_says_nothing_of_a_check(self):
        b = Describing()
        q = self.make(backend=b)
        jid = self.submit(q)
        job = self.wait_status(q, jid, "done")
        self.assertNotIn("check", job)
        self.assertEqual((len(b.calls), b.holds), (1, []), "an unmarked job was held")

    def test_a_marked_job_is_held_once_its_request_is_built_from_what_it_waited_on(self):
        app = App(build=lambda job, after: {"prompt": f"built on: {sorted(after)}"})
        b = Describing()
        q = self.make(hooks=app, backend=b)
        first = self.submit(q, input={"text": "the thinking pass"})
        jid = self.submit(q, after=[first], check=True, correlation_id=self.CORR,
                          gen={"temperature": 0.3})
        self.assertEqual(q.get(jid)["check"], "marked")
        h = self.held(q, jid)
        self.assertEqual(h["request"], {"prompt": f"built on: ['{first}']"})
        self.assertEqual((h["job"]["status"], h["job"]["check"]), ("held", "held"))
        self.assertEqual(h["model"], MODEL)
        self.assertEqual(h["gen"], {"max_tokens": 600, "seed": None, "temperature": 0.3})
        self.assertEqual(V.check(h, SHAPES["held_request"], SHAPES), [])
        self.assertEqual([c["job"] for c in b.calls], [first], "the held job was asked")
        self.assertEqual(sorted(b.holds[0]["named"]), ["model", "temperature"],
                         "what the application named is told to the backend")
        self.assertEqual((h["count"], h["fits"], h["said"]),
                         ({"prompt_tokens": 14, "room": 50, "context": 64}, True,
                          ["no expected time yet"]))
        self.assertEqual(h["job"]["progress"].get("count"), h["count"])

    def test_a_held_request_shows_only_the_parameters_its_models_engine_reads(self):
        """R-B1: a transcription's held request showed a text model's numbers
        (gen {max_tokens: 600, seed: null}) whisper never reads. A backend
        that says what its model's engine reads of them (`reads`) is held to
        it: `gen` shows those alone - none, for whisper. Sent as built, the
        job runs as built: nothing counts as changed."""

        class Deaf(Describing):
            def hold(self, request, params, run, named):
                return {**super().hold(request, params, run, named), "reads": []}

        b = Deaf()
        q = self.make(backend=b)
        jid = self.submit(q, check=True, gen={"temperature": 0.3})
        h = self.held(q, jid)
        self.assertEqual(h["gen"], {}, "a parameter its engine never reads is shown")
        self.assertNotIn("reads", h, "the backend's word is not the contract's field")
        self.assertEqual(V.check(h, SHAPES["held_request"], SHAPES), [])
        out = q.send(jid)
        self.assertEqual((out["tuned"], out["changed"]), (False, []), out)
        self.assertEqual(self.wait_status(q, jid, "done")["status"], "done")
        told = Describing()
        (self.root / "told").mkdir()
        q2 = self.make(backend=told, folder=self.root / "told")
        h2 = self.held(q2, self.submit(q2, check=True, gen={"temperature": 0.3}))
        self.assertEqual(h2["gen"], {"max_tokens": 600, "seed": None, "temperature": 0.3},
                         "a backend that says nothing of what it reads shows every one")

    def test_the_sampling_hook_is_the_applications_and_the_queues_length_the_bootstraps(self):
        app = App(sampling=lambda kind, input: {"top_k": 20})
        q = self.make(hooks=app, backend=Backend())
        jid = self.submit(q, check=True, gen={"temperature": 0.3})
        h = self.held(q, jid)
        self.assertEqual((h["engine"], h["messages"], h["count"], h["fits"], h["expect"]),
                         (None, None, None, None, None))
        self.assertEqual(h["params"], [
            {"name": "max_tokens", "value": 600, "from": "bootstrap"},
            {"name": "temperature", "value": 0.3, "from": "application"},
            {"name": "top_k", "value": 20, "from": "application"}])
        self.assertEqual(h["said"], ["this server's backend says nothing more of a request than "
                                     "the queue knows: no engine, no count, no expected time"])
        q2 = self.submit(q, check=True, gen={"max_tokens": 600})
        self.assertEqual(self.held(q, q2)["params"][0],
                         {"name": "max_tokens", "value": 600, "from": "application"},
                         "a length the application named is the application's, whatever it is")

    def test_a_held_job_waits_for_no_one_and_nothing_waits_on_the_worker_for_it(self):
        b = Describing()
        q = self.make(backend=b)
        jid = self.submit(q, check=True)
        self.held(q, jid)
        later = self.submit(q)
        self.wait_status(q, later, "done")
        self.idle(q)
        self.assertEqual(q.get(jid)["status"], "held")
        self.assertEqual([j["id"] for j in q.unfinished()], [jid],
                         "a held job is owed: a folder is not let go under it")
        self.assertEqual(q.unfinished(held=False), [],
                         "a held job is work nothing keeps the card for")

    def test_health_counts_a_held_job_until_it_is_sent_or_cancelled(self):
        """H8 (S15 RS2): an application that shows only health learns that a
        request waits on a person - and when it no longer does."""
        q = self.make(backend=Describing())
        sent, dropped = self.submit(q, check=True), self.submit(q, check=True)
        self.held(q, sent)
        self.held(q, dropped)
        self.idle(q)
        h = q.health()["queue"]
        self.assertEqual((h.get("held"), h["queued"], h["running"]), (2, 0, 0))
        q.cancel(dropped)
        q.send(sent)
        self.wait_status(q, sent, "done")
        self.assertEqual(q.health()["queue"]["held"], 0)

    def test_a_held_job_is_never_stalled_however_long_the_operator_reads(self):
        q = self.make(backend=Describing(), stall_s=0.05)
        jid = self.submit(q, check=True)
        self.held(q, jid)
        time.sleep(0.3)
        job = q.get(jid)
        self.assertEqual((job["status"], job["stalled_for"]), ("held", None))

    def test_sent_as_built_it_runs_what_was_built_never_building_it_again(self):
        app = App(build=lambda job, after: {"prompt": "as built"})
        b = Describing()
        q = self.make(hooks=app, backend=b)
        jid = self.submit(q, check=True, correlation_id=self.CORR)
        self.held(q, jid)
        out = q.send(jid)
        self.assertEqual((out["tuned"], out["changed"], out["job"]["check"]), (False, [], "sent"))
        job = self.wait_status(q, jid, "done")
        self.assertEqual(job["check"], "sent")
        self.assertEqual(len(app.built), 1, "the request was built again")
        self.assertEqual(b.calls[-1]["request"], {"prompt": "as built"})
        rec = Q.request_record(self.folder, self.CORR)
        self.assertEqual(rec["request"], {"prompt": "as built"})
        self.assertEqual(rec["held"]["request"], {"prompt": "as built"})
        self.assertEqual((rec["sent"]["tuned"], rec["sent"]["request"]), (False, {"prompt": "as built"}))
        self.assertEqual(self.story(), ["submitted", "held", "sent", "started", "attempt_started",
                                        "attempt_ended", "ended"])

    def test_tuned_it_runs_what_was_changed_and_the_record_keeps_both(self):
        app = App(build=lambda job, after: {"prompt": "as built"})
        b = Describing()
        q = self.make(hooks=app, backend=b)
        jid = self.submit(q, check=True, correlation_id=self.CORR,
                          gen={"stop": ["##"], "temperature": 0.3, "seed": 7})
        self.held(q, jid)
        out = q.send(jid, request={"prompt": "changed"}, gen={"temperature": 0.9,
                                                               "max_tokens": 80},
                     unset=["stop"])
        self.assertEqual((out["tuned"], out["changed"], out["job"]["check"]),
                         (True, ["request", "max_tokens", "stop", "temperature"], "tuned"))
        self.assertEqual(self.wait_status(q, jid, "done")["check"], "tuned")
        call = b.calls[-1]
        self.assertEqual(call["request"], {"prompt": "changed"})
        self.assertEqual((call["params"]["model"], call["params"]["temperature"],
                          call["params"]["max_tokens"], call["params"]["seed"],
                          "stop" in call["params"]),
                         (MODEL, 0.9, 80, 7, False), "the job's own model was lost, or a change")
        rec = Q.request_record(self.folder, self.CORR)
        self.assertEqual(rec["request"], {"prompt": "as built"}, "what was built was lost")
        self.assertEqual(rec["held"]["gen"]["stop"], ["##"])
        self.assertEqual((rec["sent"]["tuned"], rec["sent"]["request"], rec["sent"]["changed"]),
                         (True, {"prompt": "changed"}, ["request", "max_tokens", "stop",
                                                        "temperature"]))
        self.assertEqual(rec["sent"]["gen"]["temperature"], 0.9)
        sent = [e for e in rec["events"] if e["event"] == "sent"]
        self.assertEqual((sent[0]["tuned"], sent[0]["job"]), (True, jid))

    def test_a_change_to_what_it_already_was_is_not_a_tuned_request(self):
        q = self.make(backend=Describing())
        jid = self.submit(q, check=True, input={"prompt": "same"}, gen={"temperature": 0.3})
        self.held(q, jid)
        out = q.send(jid, request={"prompt": "same"}, gen={"temperature": 0.3})
        self.assertEqual((out["tuned"], out["changed"], out["job"]["check"]), (False, [], "sent"))

    def test_a_send_is_held_to_what_a_submit_is_and_refused_the_same_way(self):
        q = self.make(backend=Describing())
        jid = self.submit(q, check=True)
        self.held(q, jid)
        for kw, reason, words in (
                ({"gen": {"model": "other"}}, "bad_request", '$.gen: unexpected field "model"'),
                ({"gen": {"n": 2}}, "bad_request", '$.gen: unexpected field "n"'),
                ({"gen": {"retries": 0}}, "bad_request", '$.gen: unexpected field "retries"'),
                ({"gen": {"temprature": 0.3}}, "bad_request",
                 '$.gen: unexpected field "temprature"'),
                ({"gen": {"top_k": "many"}}, "bad_request", "$.gen.top_k"),
                ({"unset": ["model"]}, "bad_request", "unset"),
                ({"unset": ["temprature"]}, "bad_request", "unset"),
                ({"gen": {"max_tokens": -2}}, "bad_max_tokens",
                 "$.gen.max_tokens: llama-server takes an answer's length of -1"),
                ({"gen": {"n_keep": -5}}, "out_of_range",
                 "$.gen.n_keep: llama-server takes n_keep of -1 to 2147483647, not -5")):
            with self.subTest(kw=kw):
                e = self.refused(q.send, jid, **kw)
                self.assertEqual((e.code, e.reason), (400, reason), e.error)
                self.assertIn(words, e.error)
        self.assertEqual(q.get(jid)["status"], "held", "a refused send moved the job")

    def test_the_backend_refuses_a_request_it_could_never_send(self):
        class Checking(Describing):
            def check_sent(self, request, params):
                if not isinstance(request, dict) or "prompt" not in request:
                    raise Q.Refused("bad_request", "the request is not a prompt", 400)
        q = self.make(backend=Checking())
        jid = self.submit(q, check=True, input={"prompt": "fine"})
        self.held(q, jid)
        e = self.refused(q.send, jid, request={"nonsense": 1})
        self.assertEqual((e.code, e.reason, e.error), (400, "bad_request",
                                                         "the request is not a prompt"))
        self.assertEqual(q.send(jid, request={"prompt": "better"})["tuned"], True)

    def test_only_a_held_job_is_read_or_sent(self):
        q = self.make(start=False)
        jid = self.submit(q, check=True)
        plain = self.submit(q)
        for fn in (q.held, q.send):
            for j, st in ((jid, "queued"), (plain, "queued")):
                with self.subTest(fn=fn.__name__, job=j):
                    e = self.refused(fn, j)
                    self.assertEqual((e.code, e.reason, e.extra.get("status")),
                                     (409, "not_held", st))
            e = self.refused(fn, "j-9999")
            self.assertEqual((e.code, e.reason), (404, "no_such_job"))

    def test_a_held_job_cancelled_ends_at_once_and_what_waits_on_it_with_it(self):
        q = self.make(backend=Describing())
        jid = self.submit(q, check=True)
        self.held(q, jid)
        after = self.submit(q, after=[jid])
        out = q.cancel(jid)
        self.assertEqual((out["was"], out["job"]["status"]), ("held", "cancelled"))
        self.assertEqual(self.wait_status(q, after, "cancelled")["error"],
                         f"not run: it waits on {jid}, which cancelled")
        e = self.refused(q.send, jid)
        self.assertEqual((e.reason, e.extra.get("status")), ("not_held", "cancelled"))

    def test_a_held_job_given_up_ends_at_once(self):
        q = self.make(backend=Describing())
        jid = self.submit(q, check=True)
        self.held(q, jid)
        out = q.give_up(jid)
        self.assertEqual((out["was"], out["job"]["status"]), ("held", "cancelled"))

    def test_a_held_member_of_a_fan_is_stopped_with_its_fan(self):
        q = self.make(backend=Describing())
        held = self.submit(q, check=True, group="fan", group_size=2)
        self.held(q, held)
        bad = self.submit(q, input={"fail": "no"}, group="fan")
        self.wait_status(q, bad, "failed")
        self.assertEqual(self.wait_status(q, held, "cancelled")["error"],
                         f"not run: the group stopped when {bad} failed")

    def test_a_request_the_check_fails_fails_its_job_as_its_turn_would(self):
        for step, status, reason in (
                (Q.GenerationFailed("the request is not a conversation", reason=None),
                 "failed", None),
                (Q.GenerationFailed("demo-7b: no room", reason="no_room"), "failed", "no_room"),
                (Q.Cancelled("the server is stopping"), "cancelled", None),
                (RuntimeError("boom"), "failed", None)):
            with self.subTest(step=step):
                q = self.make(backend=Describing(hold=step), folder=self.root / str(id(step)))
                jid = self.submit(q, check=True)
                job = self.wait_status(q, jid, status)
                self.assertEqual(job.get("reason"), reason)
                self.assertNotIn("held", json.dumps(self.raw(q, jid).get("check") or {}))

    def test_a_cancel_while_the_request_is_checked_is_a_cancel(self):
        b = Describing(hold="checking")
        q = self.make(backend=b)
        jid = self.submit(q, check=True)
        self.wait_for(lambda: b.holds, "the check to begin")
        self.assertEqual(q.get(jid)["status"], "running")
        q.cancel(jid)
        self.assertEqual(self.wait_status(q, jid, "cancelled")["check"], "marked")

    def test_a_cancel_the_check_did_not_hear_ends_the_job_and_nothing_is_held(self):
        b = Describing(hold="deaf:checking")
        q = self.make(backend=b, folder=self.folder)
        jid = self.submit(q, check=True, correlation_id=self.CORR)
        self.wait_for(lambda: b.holds, "the check to begin")
        self.assertEqual(q.cancel(jid)["job"]["status"], "cancelled")
        b.open_all()
        self.idle(q)
        e = self.refused(q.held, jid)
        self.assertEqual((e.reason, e.extra.get("status")), ("not_held", "cancelled"))
        self.assertNotIn("held", self.story())

    def test_a_held_job_stays_held_across_a_restart_and_a_sent_one_runs_what_was_sent(self):
        app = App(build=lambda job, after: {"prompt": "as built"})
        b = Describing()
        q = self.make(hooks=app, backend=b)
        jid = self.submit(q, check=True)
        h = self.held(q, jid)
        q.close(3.0)
        q2 = self.make(hooks=app, backend=b)
        self.assertEqual(q2.held(jid)["request"], h["request"])
        q2.send(jid, request={"prompt": "changed"})
        self.wait_status(q2, jid, "done")
        self.assertEqual(b.calls[-1]["request"], {"prompt": "changed"})
        self.assertEqual(len(app.built), 1)

    def test_a_sent_job_the_server_stopped_under_runs_what_was_sent_again(self):
        app = App(build=lambda job, after: {"prompt": "as built"})
        b = Describing()
        q = self.make(hooks=app, backend=b)
        jid = self.submit(q, check=True, input={"gate": "g"})
        self.held(q, jid)
        q.send(jid, request={"gate": "g", "text": "changed"})
        self.wait_status(q, jid, "running")
        self.wait_for(lambda: len(b.calls) == 1, "the sent request to be asked")
        self.crash(q)
        b.open_all()
        b2 = Describing()
        b2.healthy = True                        # the gate is the first server's
        q2 = self.make(hooks=app, backend=b2)
        self.assertEqual(self.wait_status(q2, jid, "done")["check"], "tuned")
        self.assertEqual(b2.calls[-1]["request"], {"gate": "g", "text": "changed"})
        self.assertEqual(len(app.built), 1, "the request was built again after the restart")

    def test_a_retry_of_a_marked_job_is_marked_and_held_again(self):
        b = Describing()
        q = self.make(backend=b)
        jid = self.submit(q, check=True, input={"fail": "no"})
        self.held(q, jid)
        q.send(jid)
        self.wait_status(q, jid, "failed")
        again = q.retry(jid)["job"]
        self.assertEqual(again["check"], "marked")
        self.assertEqual(self.held(q, again["id"])["request"], {"fail": "no"})

    def test_what_was_held_leaves_the_store_once_the_job_ends(self):
        q = self.make(backend=Describing())
        jid = self.submit(q, check=True, input={"text": "x" * 5000})
        self.held(q, jid)
        q.send(jid)
        self.wait_status(q, jid, "done")
        self.idle(q)
        self.assertLess(len(json.dumps(self.raw(q, jid).get("check"))), 200)


# ============================================ native: answers at once (S17)

class AtOnce(Backend):
    """The stand-in, counting how many answers each model gives at once - the
    most is kept per model - and, when `up` is given, saying which models are
    up on the card (`models_up`), as the runner does."""

    def __init__(self, *steps, up=None, **kw):
        super().__init__(*steps, **kw)
        self.now: dict = {}
        self.most: dict = {}
        if up is not None:
            self.models_up = up

    def __call__(self, request, params, run):
        m = params.get("model")
        with self.lock:
            self.now[m] = self.now.get(m, 0) + 1
            self.most[m] = max(self.most.get(m, 0), self.now[m])
        try:
            return super().__call__(request, params, run)
        finally:
            with self.lock:
                self.now[m] -= 1


class NativeAnswersAtOnceRows(Case):
    """The owner's ruling of 2026-09-28: "Yes, true parallellisation. If i am
    talking and translating, i want the pipelines to go in parallell." Each
    model answers one request at a time; different models answer at the same
    time, and a job for a busy model never holds back a job for another."""

    def rec(self, q, jid):
        return Q.request_record(self.folder, q.get(jid)["correlation_id"])

    def span(self, q, jid):
        """(its answer began, its answer ended) - read off the job's own record."""
        ev = self.rec(q, jid)["events"]
        began = [e["ts"] for e in ev if e["event"] == "attempt_started"]
        ended = [e["ts"] for e in ev if e["event"] == "attempt_ended"]
        self.assertTrue(began and ended, ev)
        return began[0], ended[-1]

    def ended_line(self, q, jid):
        (line,) = [e for e in self.rec(q, jid)["events"] if e["event"] == "ended"]
        return line

    def started_line(self, q, jid):
        """The queue's own `started` line for *jid*: when it started, and what
        ran beside it then."""
        (line,) = [e for e in self.rec(q, jid)["events"] if e["event"] == "started"]
        return line

    @staticmethod
    def job(q, model, input, **kw):
        return q.submit("generate", input, {"model": model}, **kw)["job_id"]

    def test_two_jobs_on_two_models_answer_at_once_their_record_overlapping(self):
        q = self.make(backend=AtOnce())
        ears = self.job(q, "ears", {"sleep": 1.5, "text": "heard"})
        tongue = self.job(q, "tongue", {"sleep": 1.5, "text": "said"})
        self.settle(q, ears, tongue, timeout=10)
        self.idle(q)
        (a0, a1), (b0, b1) = self.span(q, ears), self.span(q, tongue)
        together = min(a1, b1) - max(a0, b0)
        # each answer takes 1.5 s: one after the other, they would not overlap
        # at all - at once, they overlap for nearly all of it
        self.assertGreater(together, 1.0, f"the answers overlapped {together:.2f}s: "
                                          f"{(a0, a1)} and {(b0, b1)}")

    def test_one_model_never_answers_two_at_once(self):
        backend = AtOnce()
        q = self.make(backend=backend)
        ears = [self.job(q, "ears", {"sleep": 0.4, "text": f"take {i}"}) for i in range(3)]
        tongue = self.job(q, "tongue", {"sleep": 0.8, "text": "said"})
        self.settle(q, *ears, tongue, timeout=10)
        self.idle(q)
        self.assertEqual(backend.most, {"ears": 1, "tongue": 1})
        spans = sorted(self.span(q, j) for j in ears)
        for (_, end), (begin, _) in zip(spans, spans[1:]):
            self.assertLessEqual(end, begin, f"two answers of one model overlapped: {spans}")
        t0, t1 = self.span(q, tongue)
        self.assertLess(t0, spans[0][1], "the other model's answer waited for this one's")

    def test_an_interactive_job_for_a_free_model_is_not_held_back_by_a_batch_job_on_a_busy_one(
            self):
        backend = AtOnce()
        q = self.make(backend=backend)
        long = self.job(q, "ears", {"gate": "long"})
        self.wait_status(q, long, "running")
        behind = self.job(q, "ears", {"text": "next take"})
        urgent = self.job(q, "tongue", {"text": "translated"}, priority="interactive")
        self.wait_status(q, urgent, "done")
        self.assertEqual((self.status(q, long), self.status(q, behind)), ("running", "queued"))
        backend.gate("long").set()
        self.settle(q, long, behind)

    def test_a_job_for_a_busy_model_holds_back_no_job_for_another(self):
        backend = AtOnce()
        q = self.make(backend=backend)
        first = self.job(q, "ears", {"gate": "g"}, priority="interactive")
        self.wait_status(q, first, "running")
        # interactive, and first in the queue's order - but its model answers
        waits = self.job(q, "ears", {"text": "second take"}, priority="interactive")
        free = self.job(q, "tongue", {"text": "a batch job for a free model"})
        self.wait_status(q, free, "done")
        self.assertEqual(self.status(q, waits), "queued")
        backend.gate("g").set()
        self.settle(q, first, waits)

    def test_priorities_and_after_hold_within_what_can_run(self):
        backend = AtOnce()
        q = self.make(backend=backend, start=False)
        busy = self.job(q, "ears", {"gate": "ears"})
        # CHANGED ON PURPOSE (S18b): a job that names no priority is live, so
        # the batch job here says it is one
        later = self.job(q, "tongue", {"sleep": 0.3, "text": "batch"}, priority="batch")
        sooner = self.job(q, "tongue", {"sleep": 0.3, "text": "interactive"},
                          priority="interactive")
        waiting = self.job(q, "tongue", {"text": "after the ears"}, after=[busy])
        q.start()
        self.settle(q, sooner, later)
        self.assertEqual((self.status(q, busy), self.status(q, waiting)), ("running", "queued"),
                         "a job ran before the job it waits on had landed")
        backend.gate("ears").set()
        self.settle(q, busy, waiting)
        order = [c["job"] for c in backend.calls if c["params"]["model"] == "tongue"]
        self.assertEqual(order, [sooner, later, waiting])
        self.assertGreaterEqual(self.span(q, waiting)[0], self.span(q, busy)[1])

    def test_a_cancel_of_one_leaves_the_other_answering(self):
        backend = AtOnce()
        q = self.make(backend=backend)
        ears = self.job(q, "ears", {"gate": "ears"})
        tongue = self.job(q, "tongue", {"gate": "tongue"})
        self.wait_for(lambda: len(backend.calls) == 2, "both answers to begin")
        self.assertEqual(q.cancel(ears)["job"]["status"], "cancelled")
        self.assertEqual(self.status(q, tongue), "running")
        self.assertEqual(backend.cancelled, [q.get(ears)["progress"]["handle"]])
        backend.gate("tongue").set()
        self.assertEqual(self.wait_status(q, tongue, "done", "cancelled", "failed")["status"],
                         "done")

    def test_health_counts_every_answer_under_way(self):
        backend = AtOnce()
        q = self.make(backend=backend)
        self.job(q, "ears", {"gate": "g"})
        self.job(q, "tongue", {"gate": "g"})
        self.wait_for(lambda: len(backend.calls) == 2, "both answers to begin")
        self.assertEqual(q.health()["queue"]["running"], 2)
        self.assertEqual([j["status"] for j in q.unfinished()], ["running", "running"])

    def test_how_many_answers_at_once_is_a_setting_and_one_is_one_at_a_time(self):
        self.assertEqual(Q.DEFAULTS["answers_at_once"], 8)
        backend = AtOnce()
        q = self.make(backend=backend, answers_at_once=1)
        ears = self.job(q, "ears", {"gate": "g"})
        self.wait_status(q, ears, "running")
        tongue = self.job(q, "tongue", {"text": "said"})
        time.sleep(0.3)
        self.assertEqual(self.status(q, tongue), "queued")
        backend.gate("g").set()
        self.settle(q, ears, tongue)

    def test_a_model_that_must_be_loaded_waits_for_the_answers_under_way_and_nothing_jumps_it(
            self):
        """A load beside a running answer could need the room that answer's
        model holds, and nothing in use comes off to make room: so a job whose
        model is not up waits until the answers under way have ended - and a
        job behind it in the queue's order waits too, though its model is up,
        so the load is never starved."""
        loaded = threading.Event()            # zeta's load is done: it is up
        backend = AtOnce(up=lambda: ["ears", "tongue", "wren"] + (["zeta"] if loaded.is_set()
                                                                  else []))
        q = self.make(backend=backend)
        ears = self.job(q, "ears", {"gate": "ears"})
        tongue = self.job(q, "tongue", {"gate": "tongue"})
        self.wait_for(lambda: len(backend.calls) == 2, "both answers to begin")
        cold = self.job(q, "zeta", {"gate": "zeta", "text": "loaded, then said"})
        behind = self.job(q, "wren", {"text": "up, and behind the load"})
        time.sleep(0.3)
        self.assertEqual((self.status(q, cold), self.status(q, behind)), ("queued", "queued"))
        backend.gate("ears").set()
        time.sleep(0.3)
        self.assertEqual(self.status(q, cold), "queued", "a load began beside an answer")
        backend.gate("tongue").set()
        # CHANGED ON PURPOSE (S17b): the job behind the load starts only once
        # the load is done - its model up - and then beside it, still answering
        self.wait_status(q, cold, "running")
        time.sleep(0.3)
        self.assertEqual(self.status(q, behind), "queued", "a job started behind a load")
        loaded.set()
        self.wait_status(q, behind, "done")
        self.assertEqual(self.status(q, cold), "running")
        backend.gate("zeta").set()
        self.settle(q, ears, tongue, cold, behind)
        self.idle(q)
        # the order, read off the queue's own `started` lines - never the order
        # the backend's threads happened to arrive in
        self.assertEqual(self.started_line(q, cold)["beside"], [])
        self.assertEqual(self.started_line(q, behind)["beside"], [{"job": cold, "model": "zeta"}])

    def test_a_backend_that_cannot_say_what_is_up_runs_nothing_beside_a_load(self):
        def cannot():
            raise RuntimeError("the card's table could not be read")
        backend = AtOnce(up=cannot)
        q = self.make(backend=backend)
        ears = self.job(q, "ears", {"gate": "g"})
        self.wait_status(q, ears, "running")
        tongue = self.job(q, "tongue", {"text": "said"})
        time.sleep(0.3)
        self.assertEqual(self.status(q, tongue), "queued")
        backend.gate("g").set()
        self.settle(q, ears, tongue)

    def test_the_record_says_what_ran_beside_each_answer(self):
        q = self.make(backend=AtOnce())
        ears = self.job(q, "ears", {"sleep": 0.8, "text": "heard"})
        tongue = self.job(q, "tongue", {"sleep": 0.8, "text": "said"})
        self.settle(q, ears, tongue, timeout=10)
        self.idle(q)
        alone = self.job(q, "ears", {"text": "heard alone"})
        self.settle(q, alone)
        self.idle(q)
        self.assertEqual(self.ended_line(q, ears)["beside"], [{"job": tongue, "model": "tongue"}])
        self.assertEqual(self.ended_line(q, tongue)["beside"], [{"job": ears, "model": "ears"}])
        self.assertEqual(self.ended_line(q, alone)["beside"], [])
        (began,) = [e for e in self.rec(q, tongue)["events"] if e["event"] == "started"]
        self.assertEqual(began["beside"], [{"job": ears, "model": "ears"}])

    def test_wants_says_whether_a_job_that_can_run_names_a_model(self):
        q = self.make(start=False)
        first = self.job(q, "ears", {"text": "x"})
        self.job(q, "tongue", {"text": "y"}, after=[first])
        self.assertTrue(q.wants("ears"))
        self.assertFalse(q.wants("tongue"), "a job waiting on another cannot run next")
        self.assertFalse(q.wants("wren"))
        q._pick(q._beat["gen"])
        self.assertFalse(q.wants("ears"), "the running job is not one that waits for it")
        self.assertFalse(Q.Queue(None, start=False).wants("ears"))

    def test_a_submit_takes_back_a_quiet_job_of_its_own_model_only(self):
        backend = AtOnce()
        q = self.make(backend=backend, stale_head_s=0.2)
        ears = self.job(q, "ears", {"gate": "e", "quiet": True})
        self.wait_status(q, ears, "running")
        self.wait_for(lambda: (q.get(ears)["progress"] or {}).get("handle"),
                      "the answer to begin")
        time.sleep(0.4)
        other = self.job(q, "tongue", {"text": "said"})
        self.wait_status(q, other, "done")
        self.assertEqual(self.status(q, ears), "running",
                         "a job of another model was taken back for it")
        again = self.job(q, "ears", {"text": "asked again"})
        self.assertEqual(self.wait_status(q, ears, "failed")["status"], "failed")
        self.wait_status(q, again, "done")

    def test_the_stall_watch_asks_whether_this_jobs_own_model_is_loading(self):
        backend = AtOnce(loading=lambda: True)
        backend.loading_for = lambda model: model == "ears"
        q = self.make(backend=backend, stall_s=0.3, load_s=60.0)
        loading = self.job(q, "ears", {"gate": "e", "quiet": True})
        stuck = self.job(q, "tongue", {"gate": "t", "quiet": True})
        self.wait_for(lambda: len(backend.calls) == 2, "both answers to begin")
        self.assertEqual(self.wait_status(q, stuck, "failed")["status"], "failed")
        self.assertIn("stalled", q.get(stuck)["error"])
        self.assertEqual(self.status(q, loading), "running")
        self.assertTrue(q.get(loading)["progress"].get("loading"))

    def test_a_stop_leaves_every_answer_under_way_to_the_next_start(self):
        backend = AtOnce()
        q1 = self.make(backend=backend)
        ears = self.job(q1, "ears", {"gate": "e", "stubborn": True, "quiet": True})
        tongue = self.job(q1, "tongue", {"gate": "t", "stubborn": True, "quiet": True})
        self.wait_for(lambda: len(backend.calls) == 2, "both answers to begin")
        began = time.monotonic()
        q1.close(0.5)
        self.assertLess(time.monotonic() - began, 2.0, "the stop waited for each answer in turn")
        q2 = self.make(backend=Backend(), start=False)
        self.assertEqual(q2.recover()["requeued"], 2)
        self.assertEqual((self.status(q2, ears), self.status(q2, tongue)), ("queued", "queued"))

    def test_a_stop_waits_for_every_answer_under_way_within_its_one_timeout(self):
        """A stop is not a cancel: each answer about to end is waited for -
        within the one timeout - and lands, never left to the next start."""
        backend = AtOnce()
        q1 = self.make(backend=backend)
        ears = self.job(q1, "ears", {"sleep": 0.5, "text": "heard"})
        tongue = self.job(q1, "tongue", {"sleep": 0.5, "text": "said"})
        self.wait_for(lambda: len(backend.calls) == 2, "both answers to begin")
        q1.close(5.0)
        q2 = self.make(backend=Backend(), start=False)
        self.assertEqual((self.status(q2, ears), self.status(q2, tongue)), ("done", "done"))

    def test_a_run_that_cannot_be_started_fails_its_job_saying_so(self):
        q = self.make(backend=AtOnce(), start=False)
        jid = self.job(q, "ears", {"text": "heard"})
        picked = q._pick(q._beat["gen"])
        real = Q.threading.Thread

        class Refuses(real):
            def start(self_):
                raise RuntimeError("can't start new thread")
        Q.threading.Thread = Refuses
        try:
            with self.assertRaises(RuntimeError):
                q._launch(picked, q._beat["gen"])
        finally:
            Q.threading.Thread = real
        j = q.get(jid)
        self.assertEqual(j["status"], "failed")
        self.assertIn("could not start this job's run: RuntimeError", j["error"])
        self.assertEqual(q._answering, {})

    def test_a_give_up_on_one_wedged_answer_leaves_the_other_and_frees_its_model(self):
        backend = AtOnce()
        q = self.make(backend=backend)
        wedged = self.job(q, "ears", {"gate": "never", "stubborn": True})
        other = self.job(q, "tongue", {"gate": "t"})
        self.wait_for(lambda: len(backend.calls) == 2, "both answers to begin")
        q.give_up(wedged)
        self.assertEqual(self.wait_status(q, wedged, "failed")["status"], "failed")
        self.assertEqual(self.status(q, other), "running")
        next_take = self.job(q, "ears", {"text": "the model is free again"})
        self.wait_status(q, next_take, "done")
        self.assertEqual(q._beat["restarts"], 0, "the worker was never inside the answer")
        backend.gate("t").set()
        self.wait_status(q, other, "done")

    def test_the_card_full_breaking_both_answers_runs_each_again_by_itself(self):
        """S5b with two answers at once: the card's memory breaks both - each
        attempt is thrown away and run again, on its own job's thread, and
        both are done."""
        broke, both = {}, threading.Barrier(2, timeout=5)

        def answer(request, params, run):
            m = params["model"]
            if m not in broke:
                broke[m] = run.sample
                both.wait()              # both answers under way when the card fills
                raise card_full()
            return f"{m}, whole"
        backend = AtOnce(*[answer] * 4)
        q = self.make(backend=backend)
        ears = self.job(q, "ears", {"text": "unused"})
        tongue = self.job(q, "tongue", {"text": "unused"})
        for jid, model in ((ears, "ears"), (tongue, "tongue")):
            j = self.wait_status(q, jid, "done", "failed")
            self.assertEqual(j["status"], "done", j.get("error"))
            self.assertEqual(q.result(jid)["candidates"][0]["text"], f"{model}, whole")
        self.idle(q)
        for jid in (ears, tongue):
            full = [e for e in self.rec(q, jid)["events"] if e["event"] == "attempt_card_full"]
            self.assertEqual(len(full), 1)

    def test_the_applications_hooks_run_on_two_threads_at_once(self):
        both = threading.Barrier(2, timeout=5)

        def build(job, after):
            both.wait()                  # returns only once the other is here too
            return job["input"]
        q = self.make(App(build=build), AtOnce())
        ears = self.job(q, "ears", {"text": "heard"})
        tongue = self.job(q, "tongue", {"text": "said"})
        self.settle(q, ears, tongue)
        self.assertEqual((self.status(q, ears), self.status(q, tongue)), ("done", "done"))


# ============================================ S17b: nothing starts behind a load

class OneRoom(Backend):
    """A card with room for ONE model, as the runner is at `max_loads` 1: a
    job whose model is not on the card loads it, and its load makes room by
    taking the other model off - which it may do only while no answer uses
    that model. With the other model in use the load is refused at once, in
    the runner's own words (as found on the real card, M1, 2026-09-28). A
    model is in use from the moment an answer on it begins until it ends;
    `models_up()` names the model on the card.

        ... "hold_room": true   the job's load waits for the test to open the
                                gate "room" before it makes room (bounded)"""

    CONTROL = Backend.CONTROL + ("hold_room",)

    def __init__(self, *steps, **kw):
        super().__init__(*steps, **kw)
        self.on: list = []            # the model on the card, if any
        self.using: dict = {}         # model -> answers using it now
        self.began: list = []         # the jobs whose answer began, in order
        self.loads: list = []         # the models loaded, in order

    def models_up(self):
        with self.lock:
            return list(self.on)

    def _take(self, model) -> None:
        """Under the lock: *model* on the card and in use - loaded first when
        it is not, the other model taken off for it; refused while that one
        answers."""
        if model not in self.on:
            if any(self.using.get(m) for m in self.on):
                raise Q.GenerationFailed(
                    f"the model could not be loaded: no room for {model}: 1 model(s) on the "
                    f"card, and this runner keeps at most 1 at once")
            self.on = [model]
            self.loads.append(model)
        self.using[model] = self.using.get(model, 0) + 1

    def __call__(self, request, params, run):
        m = params.get("model")
        hold = isinstance(request, dict) and bool(request.get("hold_room"))
        with self.lock:
            self.began.append(run.job_id)
            if not hold:
                self._take(m)
        if hold:
            if not self.gate("room").wait(10):
                raise Q.GenerationFailed("the test never opened the room")
            with self.lock:
                self._take(m)
        try:
            return super().__call__(request, params, run)
        finally:
            with self.lock:
                self.using[m] -= 1


class NothingStartsBehindALoadRows(Case):
    """S17b, a defect of S17 found on the real card (M1, 2026-09-28): room for
    one model, jobs a, b, a, b submitted at once. When the first b ended, the
    queue started the next a - not up: it must load, and its load must take b
    off - AND, in the same pass, the next b, up. That b held b in use, so a's
    load could not take it off, and a failed at once. The rule: once a job
    whose model is not up has started - its load under way - nothing behind
    it starts until its model is up or its job has ended, on every card, not
    only a full one. A job already running goes on."""

    @staticmethod
    def job(q, model, input, **kw):
        return q.submit("generate", input, {"model": model}, **kw)["job_id"]

    def one_pass(self, q) -> list:
        """One wake of the worker, driven by hand on a queue never started: it
        starts every job that can run now, and returns - so a row sees exactly
        what one pass started, never what timing let through. -> the jobs
        running after it."""
        q._worker_cycle(q._beat["gen"])
        return [j["id"] for j in q.unfinished() if j["status"] == "running"]

    def started_line(self, q, jid):
        rec = Q.request_record(self.folder, q.get(jid)["correlation_id"])
        (line,) = [e for e in rec["events"] if e["event"] == "started"]
        return line

    def test_a_job_behind_a_load_never_holds_the_model_the_load_must_take_off(self):
        """The real card's failure, deterministic: the pass that starts a's
        load and every answer that pass started have begun before a's load
        makes room."""
        backend = OneRoom()
        q = self.make(backend=backend, start=False)
        first = self.job(q, "b", {"text": "b, first"})
        self.one_pass(q)
        self.wait_status(q, first, "done")      # b on the card, and no answer uses it
        cold = self.job(q, "a", {"hold_room": True, "text": "a, loaded"})
        behind = self.job(q, "b", {"gate": "b", "text": "b, again"})
        started = self.one_pass(q)
        self.wait_for(lambda: set(started) <= set(backend.began),
                      "every answer the pass started to begin")
        backend.gate("room").set()
        j = self.wait_status(q, cold, "done", "failed")
        self.assertEqual(j["status"], "done", j.get("error"))
        self.assertEqual(started, [cold], "a job started behind a load, in the same pass")
        self.assertEqual(self.status(q, behind), "queued")
        backend.gate("b").set()
        self.one_pass(q)
        self.assertEqual(self.wait_status(q, behind, "done", "failed")["status"], "done")
        self.assertEqual(backend.loads, ["b", "a", "b"])
        self.assertEqual(self.started_line(q, behind)["beside"], [])

    def test_on_a_card_with_room_for_both_a_job_behind_a_load_still_waits_for_it(self):
        """Every card, not only a full one - and "behind" is every job not yet
        started, whatever its priority: an interactive job that comes while a
        batch job's model loads waits for that load, as a load is never given
        up for a job that came later."""
        backend = AtOnce(up=lambda: ["ears"])  # zeta: its load not done yet
        q = self.make(backend=backend, start=False)
        cold = self.job(q, "zeta", {"gate": "zeta", "text": "loaded, then said"},
                        priority="batch")
        self.assertEqual(self.one_pass(q), [cold])
        urgent = self.job(q, "ears", {"gate": "ears", "text": "up, and after the load began"},
                          priority="interactive")
        self.assertEqual(self.one_pass(q), [cold], "a job started beside a load under way")
        self.assertEqual(self.status(q, urgent), "queued")
        backend.gate("zeta").set()
        self.wait_status(q, cold, "done")
        self.assertEqual(self.one_pass(q), [urgent])
        backend.gate("ears").set()
        self.assertEqual(self.wait_status(q, urgent, "done", "failed")["status"], "done")
        self.assertEqual(self.started_line(q, urgent)["beside"], [])

    def test_a_load_whose_job_ended_holds_back_nobody(self):
        """Its job has ended - here by a cancel during its load: the jobs
        behind it start."""
        backend = AtOnce(up=lambda: ["ears"])
        q = self.make(backend=backend, start=False)
        cold = self.job(q, "zeta", {"gate": "zeta"})
        behind = self.job(q, "ears", {"gate": "ears", "text": "up, and behind the load"})
        self.assertEqual(self.one_pass(q), [cold])
        self.wait_for(lambda: len(backend.calls) == 1, "the load's answer to begin")
        self.assertEqual(q.cancel(cold)["job"]["status"], "cancelled")
        self.assertEqual(self.one_pass(q), [behind])
        backend.gate("ears").set()
        self.assertEqual(self.wait_status(q, behind, "done", "failed")["status"], "done")


# ============================================ S18: scheduled work

class Wall:
    """A wall clock a row moves by hand - forward by hours in one step, as a
    machine that slept through them finds it."""

    def __init__(self, t: float = 1_790_172_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


#: what a runner says a job waits on while its model does not fit (S18)
ROOM = {"memory": {"need_mib": 9800, "free_mib": 6100, "margin_mib": 256, "total_mib": 24576},
        "outside_mib": 18476,
        "words": "it needs 9800 MiB and 256 MiB kept free beside it; the card has 6100 MiB free "
                 "of 24576; programs outside the bootstrap hold 18476 MiB of it. It waits for "
                 "room, and starts by itself once it fits: close programs that use the card to "
                 "make room"}


def waits_for_room(room=None) -> Q.GenerationFailed:
    """A job whose model does not fit, as a runner that lets scheduled work
    wait for room says it (S18): the room it waits on."""
    failed = Q.GenerationFailed(f"{MODEL} waits for room on the card: {ROOM['words']}",
                                reason="no_room")
    failed.waits_for_room = json.loads(json.dumps(room or ROOM))
    return failed


class Looking(Backend):
    """A backend that says, when the queue looks, whether the job it waits
    to run may go now (`room_look`): each answer of `looks` in turn, the last
    one staying - {"due": bool, "room": dict or None}. What it was asked is
    kept (`wants`)."""

    def __init__(self, *steps, looks=None, patience_s=3600.0):
        super().__init__(*steps)
        self.looks = list(looks or [{"due": True, "room": None}])
        self.wants: list = []
        self.card_patience_s = patience_s
        self.settled = 0

    def settle(self):
        """The queue says its work changed: a job went back to wait."""
        with self.lock:
            self.settled += 1

    def room_look(self, want):
        with self.lock:
            self.wants.append(None if want is None else dict(want))
            return dict(self.looks.pop(0) if len(self.looks) > 1 else self.looks[0])


class NativeScheduledRows(Case):
    """S18, the owner's second ruling of 2026-09-28: SCHEDULED WORK WAITS FOR
    ROOM OR FAILS AT ONCE, BY A SETTING, AND NOT BEFORE A TIME. A batch job
    may name the time it starts no earlier than (`not_before`): until then it
    is queued and holds back nobody. One whose model does not fit goes back
    to the queue - queued, saying the room it waits on - and runs by itself
    the first moment the backend says it may; its patience counts from its
    time, on the wall clock, and one that runs out fails `no_room` in the
    loud words, saying how long it waited. Live work is now."""

    LOOK = dict(room_look_s=0.02)

    def test_live_work_is_now_and_a_time_is_a_finite_number(self):
        clk = Wall()
        q = self.make(start=False, clock=clk)
        e = self.refused(self.submit, q, priority="interactive", not_before=clk.t + 60)
        self.assertEqual((e.reason, e.code), ("live_is_now", 400))
        self.assertIn("live work is now", e.error)
        self.assertIn("60 s away", e.error)
        e = self.refused(self.submit, q, priority="interactive", if_no_room="wait")
        self.assertEqual((e.reason, e.code), ("live_is_now", 400))
        self.assertIn("never waits for room", e.error)
        for wrong, said in (("tonight", "$.not_before: expected number, got string"),
                            (float("inf"), "$.not_before: a time is seconds since the epoch"),
                            (float("nan"), "$.not_before: a time is seconds since the epoch")):
            e = self.refused(self.submit, q, not_before=wrong)
            self.assertEqual((e.reason, e.code), ("bad_request", 400))
            self.assertIn(said, e.error)
        e = self.refused(self.submit, q, if_no_room="later")
        self.assertEqual((e.reason, e.code), ("bad_request", 400))
        self.assertEqual(q.jobs(), [], "a refused job was queued")
        live = self.submit(q, priority="interactive", not_before=clk.t - 60, if_no_room="fail")
        planned = self.submit(q, priority="batch", not_before=clk.t + 9 * 3600, if_no_room="wait")
        plain = self.submit(q)
        self.assertEqual(q.get(live).get("not_before"), clk.t - 60, "a time already past is now")
        self.assertEqual((q.get(planned).get("not_before"), q.get(planned).get("if_no_room")),
                         (clk.t + 9 * 3600, "wait"))
        self.assertNotIn("not_before", q.get(plain))
        self.assertNotIn("if_no_room", q.get(plain), "a job that named none says none: the "
                                                     "server's own applies")

    def test_a_job_that_does_not_say_batch_is_live_in_every_way(self):
        """S18b, the ruled record (PLAN-4 D0, "class default RESPONSIVE"; the
        owner: "none of my agents run scheduled batches, it is always ran
        right now and response asap"): A JOB THAT DOES NOT SAY IT IS BATCH IS
        LIVE. It is `interactive` on its record, it goes to the backend as
        interactive (so it asks the card as responsive, and never waits for
        room), it goes before batch work in the queue's order, and it can
        take neither a time still to come nor a wait for room. Only
        `priority: "batch"`, said by the application, makes a job scheduled."""
        clk = Wall()
        q = self.make(start=False, clock=clk)
        for kw, said, advice in (({"not_before": clk.t + 60}, "60 s away",
                                  'say priority "batch" to plan it for later'),
                                 ({"if_no_room": "wait"}, "never waits for room",
                                  'say priority "batch" to let it wait')):
            e = self.refused(self.submit, q, **kw)
            self.assertEqual((e.reason, e.code), ("live_is_now", 400), kw)
            self.assertIn(said, e.error)
            self.assertIn('a job that does not say priority "batch" is live', e.error)
            self.assertTrue(e.error.endswith(advice), "the refusal does not say what makes a "
                                                      f"job scheduled: {e.error}")
            # a caller that hands the queue a priority of None has said nothing
            e = self.refused(self.submit, q, priority=None, **kw)
            self.assertEqual((e.reason, e.code), ("live_is_now", 400), ("None", kw))
        self.assertEqual(q.jobs(), [], "a refused job was queued")
        self.assertEqual(q.get(self.submit(q, priority=None))["priority"], "interactive",
                         "a priority of None was read as batch")
        q.cancel(q.jobs()[0]["id"])
        batch = self.submit(q, priority="batch")
        plain = self.submit(q)
        planned = self.submit(q, priority="batch", not_before=clk.t + 60, if_no_room="wait")
        self.assertEqual([q.get(j)["priority"] for j in (batch, plain, planned)],
                         ["batch", "interactive", "batch"])
        self.assertEqual(q.next_up()["id"], plain, "a job that did not say batch waited behind "
                                                   "batch work asked before it")

    def test_a_stored_job_that_says_no_priority_is_read_as_live(self):
        """S18b: a row of the folder's that says no priority - written by hand,
        or by another program - is read as a job that did not say batch: live."""
        self.free_records = True                 # the row says no priority, on purpose
        row = fixture_row("j-0001", "queued")
        del row["job"]["priority"]
        write_store(self.folder, row)
        q = self.make(start=False)
        self.assertEqual(q.get("j-0001")["priority"], "interactive")

    def test_a_planned_job_holds_back_nobody_and_starts_at_its_time(self):
        clk = Wall()
        backend = Looking()
        q = self.make(backend=backend, clock=clk, **self.LOOK)
        planned = self.submit(q, {"text": "the night batch"}, priority="batch",
                              not_before=clk.t + 9 * 3600)
        later = self.submit(q, {"text": "batch, no time"}, priority="batch")
        live = self.submit(q, {"text": "live"}, priority="interactive")
        self.settle(q, later, live)
        time.sleep(0.2)
        self.assertEqual(self.status(q, planned), "queued", "it ran before its time")
        self.assertEqual(sorted(c["job"] for c in backend.calls), sorted([live, later]),
                         "work asked after it did not go first")
        self.assertEqual(q.unfinished(held=False), [], "a job planned for later is work the "
                                                      "card is kept for")
        self.assertEqual([j["id"] for j in q.unfinished()], [planned],
                         "...though it is still owed")
        clk.t += 9 * 3600
        j = self.wait_status(q, planned, "done", "failed")
        self.assertEqual(j["status"], "done", j.get("error"))

    def test_a_machine_that_slept_through_its_time_starts_it_when_it_wakes(self):
        clk = Wall()
        backend = Looking()
        q = self.make(backend=backend, clock=clk, **self.LOOK)
        jid = self.submit(q, priority="batch", card_patience_s=600, not_before=clk.t + 3600)
        time.sleep(0.1)
        clk.t += 9 * 3600                        # asleep from before its time till morning
        j = self.wait_status(q, jid, "done", "failed")
        self.assertEqual(j["status"], "done", "its patience was counted from its time, while "
                                              f"the machine slept: {j.get('error')}")

    def test_its_patience_counts_from_its_time_not_from_its_ask(self):
        clk = Wall()
        backend = Looking(looks=[{"due": False, "room": None}])
        q = self.make(backend=backend, clock=clk, **self.LOOK)
        jid = self.submit(q, priority="batch", card_patience_s=3600, not_before=clk.t + 9 * 3600)
        clk.t += 9 * 3600 + 1800                 # half its patience past its time
        self.wait_for(lambda: backend.wants and backend.wants[-1] is not None,
                      "the queue to look for it")
        time.sleep(0.1)
        self.assertEqual(self.status(q, jid), "queued", "it failed for a patience counted "
                                                        "from its ask")
        clk.t += 3600                            # its whole patience after its first look
        j = self.wait_status(q, jid, "failed", "done")
        self.assertEqual((j["status"], j.get("reason")), ("failed", "card_patience"), j)
        self.assertIn("patience of 60 min", j["error"])

    def test_a_job_whose_model_does_not_fit_waits_for_room_queued_and_runs_by_itself(self):
        clk = Wall()
        backend = Looking(waits_for_room(), looks=[{"due": False, "room": ROOM}])
        q = self.make(backend=backend, clock=clk, **self.LOOK)
        jid = self.submit(q, {"text": "the page, whole"}, priority="batch")
        self.wait_for(lambda: self.raw(q, jid)["job"]["status"] == "queued"
                      and len(backend.calls) == 1, "the job back in the queue")
        j = q.get(jid)
        self.assertEqual(j["status"], "queued")
        self.assertEqual(j["progress"].get("stage"), "waiting for room on the card", j)
        self.assertIn("starts by itself once it fits", j["progress"].get("waiting_for", ""))
        self.assertEqual((j["progress"].get("free_mib"), j["progress"].get("need_mib"),
                          j["progress"].get("outside_mib")), (6100, 9800, 18476))
        self.assertEqual(q.unfinished(held=False), [], "a job waiting for room is work the "
                                                      "card is kept for")
        self.assertEqual(backend.settled, 1, "its backend was not told at once that the work "
                                             "it holds the card for went back to wait")
        live = self.submit(q, {"text": "live"}, priority="interactive")
        self.settle(q, live)
        self.assertEqual(self.status(q, live), "done", "live work did not go first")
        time.sleep(0.1)
        self.assertEqual(len(backend.calls), 2, "the job was asked again before the backend "
                                                "said it may go")
        backend.looks = [{"due": True, "room": None}]
        j = self.wait_status(q, jid, "done", "failed")
        self.assertEqual(j["status"], "done", j.get("error"))
        self.assertEqual(q.result(jid)["candidates"][0]["text"], "the page, whole")
        self.idle(q)
        rec = Q.request_record(self.folder, j["correlation_id"])
        waited = [e for e in rec["events"] if e["event"] == "waits_for_room"]
        self.assertEqual(len(waited), 1, rec["events"])
        self.assertEqual(waited[0].get("outside_mib"), 18476)

    def test_a_patience_that_runs_out_while_it_waits_for_room_fails_loudly(self):
        clk = Wall()
        backend = Looking(waits_for_room(), looks=[{"due": False, "room": ROOM}])
        q = self.make(backend=backend, clock=clk, **self.LOOK)
        jid = self.submit(q, priority="batch", card_patience_s=5400)
        self.wait_for(lambda: self.raw(q, jid)["job"]["status"] == "queued"
                      and backend.calls, "the job back in the queue")
        clk.t += 5400
        j = self.wait_status(q, jid, "failed", "done")
        self.assertEqual((j["status"], j.get("reason")), ("failed", "no_room"))
        for said in ("did not fit on the card within its patience of 90 min",
                     "it needs 9800 MiB", "6100 MiB free of 24576",
                     "programs outside the bootstrap hold 18476 MiB",
                     "It waited 90 min for room, within its patience, and none came",
                     "Close programs that use the card, or reboot"):
            self.assertIn(said, j["error"])
        self.assertNotIn("does not wait", j["error"])

    def test_a_job_that_names_no_patience_waits_for_room_as_long_as_the_servers(self):
        clk = Wall()
        backend = Looking(waits_for_room(), looks=[{"due": False, "room": ROOM}], patience_s=600)
        q = self.make(backend=backend, clock=clk, **self.LOOK)
        jid = self.submit(q, priority="batch")
        self.wait_for(lambda: self.raw(q, jid)["job"]["status"] == "queued"
                      and backend.calls, "the job back in the queue")
        clk.t += 600
        j = self.wait_status(q, jid, "failed", "done")
        self.assertEqual(j.get("reason"), "no_room")
        self.assertIn("within its patience of 10 min", j["error"])

    def test_the_room_it_waits_on_is_said_again_at_each_look(self):
        clk = Wall()
        other = dict(ROOM, memory=dict(ROOM["memory"], free_mib=4000), outside_mib=20576)
        backend = Looking(waits_for_room(), looks=[{"due": False, "room": other}])
        q = self.make(backend=backend, clock=clk, **self.LOOK)
        jid = self.submit(q, priority="batch")
        self.wait_for(lambda: q.get(jid)["progress"].get("free_mib") == 4000,
                      "the look's numbers on the job")
        self.assertEqual(q.get(jid)["progress"].get("outside_mib"), 20576)

    def test_the_queue_looks_for_the_soonest_waiting_job_and_nothing_once_none_waits(self):
        clk = Wall()
        backend = Looking(looks=[{"due": False, "room": None}])
        q = self.make(backend=backend, clock=clk, **self.LOOK)
        late = self.submit(q, priority="batch", not_before=clk.t + 7200, if_no_room="fail")
        soon = self.submit(q, priority="batch", not_before=clk.t + 3600, card_patience_s=60)
        self.wait_for(lambda: backend.wants and backend.wants[-1] is not None, "a look")
        want = backend.wants[-1]
        self.assertEqual((want.get("job"), want.get("model"), want.get("not_before")),
                         (soon, MODEL, clk.t + 3600), want)
        self.assertEqual(want.get("for"), q.get(soon)["correlation_id"])
        self.assertIsNone(want.get("if_no_room"), "a job that named none says none")
        q.cancel(soon)
        self.wait_for(lambda: backend.wants[-1] is not None
                      and backend.wants[-1].get("job") == late, "the next soonest")
        self.assertEqual(backend.wants[-1].get("if_no_room"), "fail")
        q.cancel(late)
        self.wait_for(lambda: backend.wants[-1] is None, "nothing, once nothing waits")

    def test_a_planned_and_a_waiting_job_are_kept_across_a_restart(self):
        clk = Wall()
        backend = Looking(waits_for_room(), looks=[{"due": False, "room": ROOM}])
        q = self.make(backend=backend, clock=clk, **self.LOOK)
        room = self.submit(q, priority="batch", card_patience_s=3600)
        planned = self.submit(q, priority="batch", not_before=clk.t + 7200)
        self.wait_for(lambda: self.raw(q, room)["job"]["status"] == "queued"
                      and backend.calls, "the job back in the queue")
        clk.t += 1800
        q.close(3.0)
        again = Looking(looks=[{"due": False, "room": ROOM}])
        q2 = self.make(backend=again, clock=clk, **self.LOOK)
        self.assertEqual(q2.get(planned).get("not_before"), clk.t - 1800 + 7200)
        self.assertEqual(q2.get(room)["progress"].get("stage"), "waiting for room on the card")
        time.sleep(0.1)
        self.assertEqual((self.status(q2, room), self.status(q2, planned)), ("queued", "queued"))
        clk.t += 1800                            # an hour since it first waited: its patience
        j = self.wait_status(q2, room, "failed", "done")
        self.assertEqual(j.get("reason"), "no_room", "its patience was counted afresh after "
                                                     "the restart")
        self.assertIn("It waited 60 min", j["error"])

    def test_health_says_what_scheduled_work_waits_for(self):
        clk = Wall()
        backend = Looking(waits_for_room(), looks=[{"due": False, "room": ROOM}])
        q = self.make(backend=backend, clock=clk, **self.LOOK)
        self.assertNotIn("scheduled", q.health()["queue"], "nothing waits: nothing is said")
        room = self.submit(q, priority="batch")
        self.submit(q, priority="batch", not_before=clk.t + 7200)
        self.submit(q, priority="batch", not_before=clk.t + 3600)
        self.wait_for(lambda: self.raw(q, room)["job"]["status"] == "queued"
                      and backend.calls, "the job back in the queue")
        s = q.health()["queue"].get("scheduled")
        self.assertEqual((s["planned"], s["next_at"], s["for_room"]),
                         (2, clk.t + 3600, 1), s)
        self.assertIn("starts by itself once it fits", s["room"])
        self.assertEqual(V.check(q.health()["queue"], ROUTES["server/health"]["responses"]["200"]
                                 ["object"]["queue"], SHAPES), [])

    def test_its_patience_counts_from_the_first_time_it_waited_whatever_it_tries_between(self):
        clk = Wall()
        seen = []
        state = {"due": False}

        def tried(request, params, run):
            state["due"] = False                 # it may go once: then it waits again
            seen.append((run.card_patience_s, run.if_no_room))
            raise waits_for_room()
        backend = Looking(tried, tried, tried)
        backend.room_look = lambda want: {"due": state["due"], "room": ROOM}
        q = self.make(backend=backend, clock=clk, **self.LOOK)
        jid = self.submit(q, priority="batch", card_patience_s=3600, if_no_room="wait")
        self.wait_for(lambda: len(seen) == 1 and self.status(q, jid) == "queued",
                      "the first wait for room")
        clk.t += 1800
        state["due"] = True
        self.wait_for(lambda: len(seen) == 2 and self.status(q, jid) == "queued",
                      "a second try, and a second wait")
        self.assertEqual(seen[0], (3600, "wait"), "its own patience and wait were not handed on")
        self.assertAlmostEqual(seen[1][0], 1800, delta=1,
                               msg="the second try was not handed what is left of its patience")
        clk.t += 1800                            # an hour since it FIRST waited
        j = self.wait_status(q, jid, "failed", "done")
        self.assertEqual(j.get("reason"), "no_room", "its patience started again at its "
                                                     "second wait")
        self.assertIn("It waited 60 min", j["error"])

    def test_a_retry_keeps_its_time_and_its_wait_for_room(self):
        clk = Wall()
        q = self.make(backend=Looking(Q.GenerationFailed("no")), clock=clk, **self.LOOK)
        jid = self.submit(q, priority="batch", not_before=clk.t - 10, if_no_room="fail")
        self.wait_status(q, jid, "failed")
        again = q.retry(jid)["job"]
        self.assertEqual((again.get("not_before"), again.get("if_no_room")),
                         (clk.t - 10, "fail"))

    def test_a_check_marked_job_whose_model_does_not_fit_waits_for_room_too(self):
        clk = Wall()

        class Holding(Looking):
            def hold(self, request, params, run, named):
                raise waits_for_room()
        backend = Holding(looks=[{"due": False, "room": ROOM}])
        q = self.make(backend=backend, clock=clk, **self.LOOK)
        jid = self.submit(q, priority="batch", check=True)
        self.wait_for(lambda: q.get(jid)["progress"].get("stage")
                      == "waiting for room on the card", "the job back in the queue")
        self.assertEqual(self.status(q, jid), "queued")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from _suites import report                             # the tally, counted in rows
    report(unittest.main(exit=False, verbosity=1))
