"""The thin application (PLAN-4 D13): the reference client, and three whole
sessions driven through it over HTTP.

    python test_sessions.py

No GPU: every model is the stand-in, answering in its engine's shape. The
coordinator is REAL - the coordinator part's own, served over HTTP in this
process on a port of this copy's own - so every ticket, rental and release
below was asked for on the wire. The coordinator part is imported by this
suite alone; the server reaches it through the contract.

The sessions:
  * DICTAPHONE-SHAPED - its server started as a child by the reference
    client, and its folder bound; a session opened with its two dictation
    models pinned and ready; dictations, each taken; a compare - one take on
    both models; the close. One ticket, one rental, one load of each model.
  * ANALYSIS-SHAPED - a pipeline per replay (thinking, then writing), closed
    after it; another application's live work, waiting as the first replay
    ends, goes ahead of the second.
  * A HOST running a component's models in its own session (PLAN-4 D6, the
    Dictaphone's H-1 to H-7): whisper, then a deep-thinking text model, then
    whisper again - the card never given back, and never two loads of one
    model.

THIS SUITE NEVER TAKES THE MACHINE'S CARD LOCK, AND NEVER ASKS THE CARD.
Every runner's settings name a lock file of its own (`card_lock`), and the
lock's default is pointed at a folder of this copy's own besides; every
manager gets a card reader of its own (`vram`), and the real reader is a
tripwire (`_suites.forbid_the_card`) - in this process, and in the child
server it starts (`--serve`, below).
"""
from __future__ import annotations

import atexit
import http.server
import itertools
import json
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "llm-bootstrap-coordinator"))

import _suites                                                  # noqa: E402
_suites.forbid_the_card()        # a suite never asks the real card (nvidia-smi)
import runner.cardlock as _CL                                   # noqa: E402

_LOCKS = Path(tempfile.mkdtemp(prefix="llmb-sessions-locks-"))
_LOCK_N = itertools.count()
_CL.machine_lock = lambda: _LOCKS / f"card-{next(_LOCK_N)}.lock"
atexit.register(shutil.rmtree, _LOCKS, True)

if __name__ == "__main__" and sys.argv[1:2] == ["--serve"]:
    # THE CHILD SERVER a session starts: server.py's own start, in a process
    # whose real card reader is a tripwire and whose lock is of its own - the
    # command line names this file, so a sweep finds it by its folder
    import server as _S
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(_S.main(sys.argv[2:]))

import coordinator as C                                         # noqa: E402  (the suite's only)
import coordination as K                                        # noqa: E402
import ids                                                      # noqa: E402
import jobqueue as Q                                            # noqa: E402
import reference_client as R                                   # noqa: E402
import server as S                                              # noqa: E402
import validate as V                                            # noqa: E402
from runner import manager as M                                # noqa: E402
from _suites import START_S, quiet_ports                        # noqa: E402

CONTRACT = HERE.parent / "llm-bootstrap-contract"
#: the ports this copy's servers and coordinators listen on, and the ranges
#: its coordinators hand out: blocks of the coordination suite's own ranges,
#: claimed through the same file, so no two copies of either meet
QUIET_PORTS, REGISTRY_PORTS, REGISTRY_BLOCK = (30500, 30999), (32000, 32767), 64
FAST = dict(tick_s=0.02, supervise_s=0.02, cancel_wait_s=1.0, give_up_grace_s=0.3,
            stall_s=60.0, load_s=120.0, stale_head_s=600.0, recover_timeout_s=0.3)
#: a whisper model file's header: large-v3's eleven numbers (the ggml magic first)
WHISPER_HEADER = struct.pack("<I11i", 0x67676d6c, 51866, 1500, 1280, 20, 32, 448, 1280, 20,
                             32, 128, 1)
#: how long a session's step is given - a bound for what did not work, never
#: for a busy machine
STEP_S = 60.0
_REGISTRIES = itertools.count()


def port() -> int:
    return quiet_ports(*QUIET_PORTS).take()


def registry() -> tuple:
    first, _ = quiet_ports(*REGISTRY_PORTS, size=REGISTRY_BLOCK).block
    start = first + 16 * (next(_REGISTRIES) % (REGISTRY_BLOCK // 16))
    return start, start + 15


def write_wav(path: Path, seconds: float = 1.0) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((struct.pack("<h", 1200) + struct.pack("<h", -1200))
                      * int(8000 * seconds))
    return path


class Coord:
    """A real coordinator, in this process, on a port of this copy's own. It
    asks nothing of the card (no `hardware`) and holds no lock (built in
    code)."""

    def __init__(self, data: Path, ttl=2.0):
        self.c = C.Coordinator(data, ttl_s=ttl, ports=registry(), hold_s=5.0)
        self.srv = C.serve(self.c, port(), log=lambda s: None)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def stop(self):
        if self.srv is not None:
            C.stop(self.srv)
            self.srv = None

    def of(self, kind, actor=None):
        return [e for e in self.c.events if e["kind"] == kind
                and (actor is None or e.get("actor") == actor)]

    def rental(self):
        return self.c.state()[1]["rental"]

    def tickets(self):
        return self.c.state()[1]["tickets"]


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="llmb-sessions-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.said: list = []

    def coord(self, **kw) -> Coord:
        co = Coord(self.tmp / f"coordinator-{len(list(self.tmp.glob('coordinator-*')))}", **kw)
        self.addCleanup(co.stop)
        return co

    def settings(self, name: str, whisper=(), text=(), load_s=0.05, **mock) -> Path:
        """A runner's settings: the stand-in, these whisper models (each with
        a file carrying a whisper header) and text models - and a card lock of
        its own."""
        models = self.tmp / f"{name}-models"
        models.mkdir(exist_ok=True)
        m = {"tokens_per_second": 0, "load_seconds": load_s, "unload_seconds": 0.0,
             "answer_seconds": 0.0, **mock}
        text_ = [f'[runner]\nbackend = "mock"\nmodels_dir = "{models.as_posix()}"\n'
                 f'card_lock = "{(self.tmp / f"{name}-card.lock").as_posix()}"\n'
                 f'idle_unload_s = 600.0\n\n[mock]\n'
                 + "".join(f"{k} = {v}\n" for k, v in m.items())]
        for key in whisper:
            (models / f"{key}.bin").write_bytes(WHISPER_HEADER)
            text_.append(f'\n[models.{key}]\nengine = "whisper"\nfile = "{key}.bin"\n')
        for key in text:
            text_.append(f'\n[models.{key}]\nlabel = "{key}"\n')
        path = self.tmp / f"{name}.toml"
        path.write_bytes("".join(text_).encode("utf-8"))
        return path

    def served(self, settings: Path, co: Coord | None = None, name="writer-app"):
        """A server in this process, over real HTTP - through the coordinator
        when one is given: -> (url, its folder's record, its backend)."""
        client = None
        if co is not None:
            client = K.CoordinatorClient(co.url, ids.mint_actor("srv"), name,
                                         log=self.said.append)
            self.addCleanup(client.close)
        backend = S.build_backend("mock", settings, log=self.said.append, coordinator=client,
                                  vram=lambda: None)
        httpd = S.serve(port(), backend=backend, log=self.said.append,
                        **({"actor": client.actor, "coordinator": client} if client else {}),
                        **FAST)
        self.addCleanup(S.stop, httpd)
        return httpd.app.url, backend

    def folder(self, name: str) -> Path:
        f = self.tmp / name
        f.mkdir(exist_ok=True)
        return f

    @staticmethod
    def record(folder: Path) -> list:
        """The runner's record in the application's folder: every load and
        unload, every card taken and given back."""
        return M.read_events(folder / "runner" / M.EVENTS_NAME)

    def assert_one_load_of_each(self, events, models):
        self.assertEqual(M.max_concurrency(events), 1, "two loads of one model at once")
        self.assertEqual(M.beside_faults(events), [], "a load did not name what was beside it")
        loaded = [e["model_key"] for e in events if e.get("event") == "load_ok"]
        self.assertEqual(sorted(loaded), sorted(models), f"the loads were {loaded}")

    def started(self, args) -> R.StartedServer:
        """A server started as a child through the reference client - this
        file's `--serve`, so its real card reader is a tripwire and its lock
        its own - and stopped after the row. Should its stop fail, it is
        killed by the handle that started it: a row never leaves a server
        running."""
        server = R.start_server(args, command=[sys.executable, str(HERE / "test_sessions.py"),
                                               "--serve"], start_s=START_S)
        self.addCleanup(lambda: server.proc.poll() is None and _suites.killed(server.proc))
        self.addCleanup(server.stop)
        return server

    @staticmethod
    def suppress(fn, *a, **kw):
        """-> None once *fn* has run, or what it raised: a thread's end, told."""
        try:
            fn(*a, **kw)
            return None
        except BaseException as e:                          # noqa: BLE001
            return e

    def wait_for(self, pred, what, timeout=STEP_S):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            got = pred()
            if got:
                return got
            time.sleep(0.02)
        self.fail(f"timed out after {timeout}s waiting for {what}")

    def dictate(self, app: R.Client, take: Path, model: str, **job) -> dict:
        """One recording, sent and read: -> its result, taken."""
        jid = app.submit("transcribe", {"audio": str(take), "fields": {"language": "no"}},
                         {"model": model}, **job)
        got = app.wait(jid, within_s=STEP_S)
        self.assertEqual((got["job"]["status"], got["job"]["client"]), ("done", app.actor),
                         got["job"])
        taken = app.take(jid)
        self.assertEqual((taken["status"], taken["claimed_by"]), ("claimed", app.actor))
        with self.assertRaises(R.Refused) as gone:
            app.result(jid)
        self.assertEqual(gone.exception.reason, "no_such_job", "the job was not forgotten")
        return got


# ======================================================== the reference client

class Stub:
    """A server that answers every request with one status and body, counting
    them - what a server outside the contract would say."""

    def __init__(self, status: int, body: bytes, delay_s: float = 0.0):
        self.hits = []
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def answer(self):
                stub.hits.append((self.command, self.path))
                time.sleep(delay_s)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            do_GET = do_POST = answer
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", port()), Handler)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


class ReferenceClientRows(Case):
    """Every answer checked against the contract, as the client's JavaScript
    checks it; the contract's version first; a wait held by the server, never
    asked again and again; a component opens nothing; a start says where it
    listens, or why it did not."""

    def stub(self, status, body, **kw) -> Stub:
        s = Stub(status, json.dumps(body).encode("utf-8") if not isinstance(body, bytes)
                 else body, **kw)
        self.addCleanup(s.stop)
        return s

    def test_an_answer_the_contract_does_not_declare_is_no_answer(self):
        for why, status, body in (
                ("a field missing", 200, {"loaded": False}),
                ("a status the route does not declare", 418, {"error": "a teapot",
                                                              "reason": "teapot"}),
                ("not JSON", 200, b"<html>hello</html>")):
            with self.subTest(why=why):
                with self.assertRaises(R.NoAnswer) as caught:
                    R.Client(self.stub(status, body).url)
                self.assertIn("server.health", str(caught.exception))

    def test_silence_is_no_answer(self):
        with self.assertRaises(R.NoAnswer):
            R.Client(f"http://127.0.0.1:{port()}", timeout_s=2.0)

    def test_a_declared_refusal_is_raised_with_its_reason_its_sentence_and_its_status(self):
        url, _ = self.served(self.settings("plain", text=["alpha"]))
        app = R.Client(url)
        with self.assertRaises(R.Refused) as caught:
            app.result("j-0001")
        got = caught.exception
        self.assertEqual((got.status, got.reason, got.route_id),
                         (409, "no_workspace", "server.jobs.result"))
        self.assertTrue(got.error and got.error == got.answer["error"], got.answer)
        with self.assertRaises(R.Refused) as caught:
            app.bind(self.tmp / "not-there" / "at-all")
        self.assertEqual((caught.exception.status, caught.exception.reason),
                         (400, "unusable_folder"))

    def test_a_pipeline_is_opened_saying_what_it_does_when_its_model_does_not_fit(self):
        url, _ = self.served(self.settings("plain", text=["alpha"]))
        app = R.Client(url)
        with self.assertRaises(R.Refused) as caught:
            app.open(["alpha"], if_no_room="wait")
        self.assertEqual((caught.exception.status, caught.exception.reason), (400, "live_is_now"),
                         "the open did not say it would wait for room")

    def test_a_body_the_contract_refuses_is_never_sent(self):
        url, _ = self.served(self.settings("plain", text=["alpha"]))
        app = R.Client(url)
        asked = list(app.asked)
        for body in ({"kind": 5, "gen": {"model": "alpha"}, "input": "hi"},
                     {"kind": "generate", "gen": {"model": "alpha", "temprature": 0.5},
                      "input": "hi"}):
            with self.assertRaises(ValueError):
                app.call("server.jobs.submit", body=body)
        with self.assertRaises(ValueError):
            app.call("server.jobs.forget", params={"id": "j-0001"}, body={})
        self.assertEqual(app.asked, asked, "a body the contract refuses was sent")

    def test_the_contracts_version_is_checked_before_anything_else(self):
        url, _ = self.served(self.settings("plain", text=["alpha"]))
        app = R.Client(url)
        self.assertEqual((app.asked, app.version), (["server.health"],
                                                    V.contract_version(CONTRACT)))
        other = self.tmp / "other-contract"
        shutil.copytree(CONTRACT, other, ignore=shutil.ignore_patterns("__pycache__"))
        route = other / "routes" / "server" / "health.json"
        route.write_bytes(route.read_bytes().replace(b"Never refuses", b"Never  refuses", 1))
        theirs, ours = V.contract_version(CONTRACT), V.contract_version(other)
        self.assertNotEqual(theirs, ours)
        with self.assertRaises(R.OtherContract) as caught:
            R.Client(url, contract=other)
        self.assertIn(theirs, str(caught.exception))
        self.assertIn(ours, str(caught.exception))

    def test_a_wait_is_held_by_the_server_never_asked_again_and_again(self):
        """3. The result waited for: one ask, held until the job ends - and an
        answer held longer than the client's own timeout is still waited for,
        since it asked the server to hold it."""
        url, _ = self.served(self.settings("slow", text=["alpha"], load_s=1.5))
        app = R.Client(url, timeout_s=0.5)
        app.bind(self.folder("slow-folder"))
        jid = app.submit("generate", {"prompt": "hi"}, {"model": "alpha", "max_tokens": 5})
        t0 = time.monotonic()
        first = app.result(jid, wait_s=1.0)
        self.assertGreaterEqual(time.monotonic() - t0, 0.9, "the server did not hold the answer")
        self.assertNotIn(first["job"]["status"], R.ENDED, "the row proved nothing: it had ended")
        before = app.asked.count("server.jobs.result")
        got = app.wait(jid, within_s=STEP_S)
        self.assertEqual(got["job"]["status"], "done")
        self.assertEqual(app.asked.count("server.jobs.result") - before, 1,
                         "the result was asked again and again")

    def test_a_wait_returns_when_its_job_is_held_for_its_check(self):
        """R-B3 with S16a R5: a job marked `check` is held once its request is
        built, and nothing more comes of it until a person sends it. The
        client's wait returns it then - held - never waiting out its bound
        to raise TimeoutError; its one ask is answered at held."""
        url, _ = self.served(self.settings("checked", text=["alpha"]))
        app = R.Client(url)
        app.bind(self.folder("checked-folder"))
        jid = app.submit("generate", {"prompt": "hi"}, {"model": "alpha", "max_tokens": 5},
                         check=True)
        t0 = time.monotonic()
        got = app.wait(jid, within_s=20.0, wait_s=30)
        self.assertEqual(got["job"]["status"], "held")
        self.assertLess(time.monotonic() - t0, 5.0, "the wait outlived the hold")

    def test_a_wait_that_runs_out_says_so_naming_the_job(self):
        url, _ = self.served(self.settings("slow", text=["alpha"], load_s=3.0))
        app = R.Client(url)
        app.bind(self.folder("slow-folder"))
        jid = app.submit("generate", {"prompt": "hi"}, {"model": "alpha", "max_tokens": 5})
        t0 = time.monotonic()
        with self.assertRaises(TimeoutError) as caught:
            app.wait(jid, within_s=0.6, wait_s=30)
        self.assertLess(time.monotonic() - t0, 2.5, "the wait outlived its bound")
        self.assertIn(jid, str(caught.exception))

    def test_a_component_opens_nothing_and_is_told_when_its_host_has_no_session(self):
        url, _ = self.served(self.settings("host", text=["alpha"]))
        host = R.Client(url)
        opened = host.open([], keep_s=0, wait_s=STEP_S)
        self.assertEqual(R.Client(url, pipeline=opened["id"]).session()["id"], opened["id"])
        pipeline = ids.mint_pipeline()                  # not the one the host has open
        ears = R.Client(url, pipeline=pipeline)
        asked = list(ears.asked)
        for call in (lambda: ears.open(["alpha"]), lambda: ears.close(pipeline),
                     lambda: ears.load("alpha"), lambda: ears.unload("alpha")):
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(ears.asked, asked, "a component asked for the host's session")
        with self.assertRaises(R.NotInSession) as caught:
            ears.session()
        self.assertIn(pipeline, str(caught.exception))

    def test_a_start_says_where_the_server_listens_or_why_it_did_not(self):
        with self.assertRaises(R.CannotStart) as caught:
            R.start_server(["--backend", "none", "--runner", "nothing.toml"],
                           command=[sys.executable, str(HERE / "test_sessions.py"), "--serve"],
                           start_s=START_S)
        self.assertIn("exit 2", str(caught.exception))
        self.assertIn("--runner", str(caught.exception))
        server = self.started(["--backend", "echo", "--port", str(port())])
        self.assertTrue(ids.matches("actor", server.actor), server.lines)
        self.assertEqual(server.client().health()["actor"], server.actor)
        self.wait_for(lambda: any("GET /api/health -> 200" in line for line in server.lines),
                      "the server's lines after it said it was listening")
        self.assertEqual(server.stop(within_s=60), server.proc.returncode)
        self.assertIsNotNone(server.proc.returncode, "the server was not ended")

    def test_a_server_stopped_hands_its_card_back_never_lets_it_lapse(self):
        """H8 (S13b R2): the stop is asked over HTTP (`server.stop`) - its last
        act done: the folder put down, its models off, and the rental RELEASED
        to the coordinator - and the process ends by itself, exit code 0."""
        co = self.coord()
        settings = self.settings("stopped", whisper=["large-v3"])
        server = self.started(["--backend", "mock", "--runner", settings, "--coordinator",
                               co.url, "--name", "stopped"])
        app = server.client()
        app.bind(self.folder("stopped"))
        pipe = app.open(["large-v3"], keep_s=0, label="a session left open", wait_s=STEP_S)
        self.assertEqual((pipe["state"], pipe["up"]), ("holding", ["large-v3"]), pipe)
        self.assertEqual(co.rental()["actor"], server.actor)
        self.assertEqual(server.stop(within_s=60), 0, server.lines[-8:])
        self.assertIsNone(co.rental(), "the rental is still held")
        self.assertEqual((len(co.of("rental_released")), co.of("rental_reclaimed")), (1, []))
        self.assertTrue(any("POST /api/stop -> 202" in line for line in server.lines),
                        server.lines[-8:])
        # a component asks no stop: the server is its host's
        with self.assertRaises(ValueError):
            R.Client(server.url, pipeline=pipe["id"], check_version=False).stop()


# ================================================================ the sessions

class SessionRows(Case):
    """Three whole sessions, over HTTP, through the reference client."""

    def test_a_dictaphone_session_two_pinned_models_dictations_a_compare_a_close(self):
        co = self.coord()
        settings = self.settings("dictaphone", whisper=["large-v3", "nb-large", "nb-medium"])
        # 1. its server started as its own child, finding the coordinator it is named
        server = self.started(["--backend", "mock", "--runner", settings, "--coordinator",
                               co.url, "--name", "dictaphone"])
        app = server.client()
        self.assertEqual(app.check_version(), co.c.contract_version,
                         "the server, the client and the coordinator run different contracts")
        folder = self.folder("dictations")
        self.assertEqual(app.bind(folder)["workspace"], str(folder))
        # 2. the session opens with its two dictation models, ready when answered
        pipe = app.open(["large-v3", "nb-large"], keep_s=0, label="dictation session",
                        wait_s=STEP_S)
        self.assertEqual((pipe["state"], sorted(pipe["up"]), pipe["opened_by"]),
                         ("holding", ["large-v3", "nb-large"], "application"), pipe)
        rental = co.rental()
        self.assertEqual((rental["actor"], rental["holder"]), (server.actor, "dictaphone"))
        # 3, 4. dictations - live: the card now or not at all, and it is theirs
        takes = [write_wav(folder / f"take-{i}.wav", 1.0 + i) for i in range(3)]
        for i, take in enumerate(takes):
            got = self.dictate(app, take, "large-v3", card_patience_s=0, priority="interactive")
            said = got["candidates"][0]
            self.assertEqual((said["audio_s"], bool(said["text"])), (1.0 + i, True), said)
        # a compare: one take on both models, side by side
        both = {model: self.dictate(app, takes[0], model)["candidates"][0]
                for model in ("large-v3", "nb-large")}
        self.assertEqual({m: c["audio_s"] for m, c in both.items()},
                         {"large-v3": 1.0, "nb-large": 1.0})
        self.assertTrue(all(c["transcript"]["text"] == c["text"] for c in both.values()))
        # the whole session held one rental: one ticket, never given back
        self.assertEqual((len(co.of("ticket_asked")), len(co.of("rental_granted")),
                          co.of("rental_released")), (1, 1, []))
        self.assertEqual(co.rental()["id"], rental["id"])
        # 5. the close: every model off, and the card back
        closed = app.close(pipe["id"])
        self.assertEqual((closed["state"], closed["up"]), ("closed", []), closed)
        self.assertEqual({m["key"]: m["state"]["state"] for m in app.models()},
                         {"large-v3": "off", "nb-large": "off", "nb-medium": "off"})
        self.assertIsNone(co.rental())
        self.assertEqual(len(co.of("rental_released")), 1)
        self.assert_one_load_of_each(self.record(folder), ["large-v3", "nb-large"])
        self.assertEqual(M.orphaned_loads(self.record(folder)), [])

    def test_talking_and_translating_the_two_pipelines_answer_at_once(self):
        """The owner's ruling of 2026-09-28, his own case: "If i am talking and
        translating, i want the pipelines to go in parallell. One that does
        voice to text. Then the other one translates the text to text again."
        A session keeps a whisper model and a text model. Each recording is
        transcribed as it comes; each transcript is then translated, while
        the next recording is transcribed - read off the request record's own
        times, each answer taking over a second, so the overlap is no
        accident. One model never answers two at once."""
        co = self.coord()
        settings = self.settings("talking", whisper=["large-v3"], text=["translator"],
                                 answer_seconds=1.2, tokens_per_second=10)
        url, _ = self.served(settings, co, name="dictaphone")
        app = R.Client(url)
        folder = self.folder("talking")
        app.bind(folder)
        pipe = app.open(["large-v3", "translator"], keep_s=0, label="talking and translating",
                        wait_s=STEP_S)
        self.assertEqual(sorted(pipe["up"]), ["large-v3", "translator"], pipe)

        def hear(take):
            return app.submit("transcribe", {"audio": str(take), "fields": {}},
                              {"model": "large-v3"}, priority="interactive")
        takes = [write_wav(folder / f"chunk-{i}.wav", 1.0) for i in range(3)]
        heard, said = [hear(takes[0])], []
        for i in range(len(takes)):
            got = app.wait(heard[i], within_s=STEP_S)
            self.assertEqual(got["job"]["status"], "done", got["job"])
            text = got["candidates"][0]["text"]
            said.append(app.submit("translate", {"prompt": f"Translate to English: {text}"},
                                   {"model": "translator", "max_tokens": 12},
                                   priority="interactive"))
            if i + 1 < len(takes):
                heard.append(hear(takes[i + 1]))
        for jid in said:
            got = app.wait(jid, within_s=STEP_S)
            self.assertEqual(got["job"]["status"], "done", got["job"])

        def span(jid):
            corr = app.result(jid)["job"]["correlation_id"]
            deadline = time.monotonic() + STEP_S
            while True:
                ev = Q.request_record(folder, corr)["events"]
                if any(e["event"] == "ended" for e in ev):
                    break
                self.assertLess(time.monotonic(), deadline, f"{jid}'s record never ended")
                time.sleep(0.02)
            return ([e["ts"] for e in ev if e["event"] == "attempt_started"][0],
                    [e["ts"] for e in ev if e["event"] == "attempt_ended"][-1])
        heard_s, said_s = [span(j) for j in heard], [span(j) for j in said]
        for i in range(len(takes) - 1):
            (t0, t1), (h0, h1) = said_s[i], heard_s[i + 1]
            self.assertGreater(min(t1, h1) - max(t0, h0), 0.8,
                               f"chunk {i + 1} was not transcribed while chunk {i} was "
                               f"translated: {said_s[i]} and {heard_s[i + 1]}")
        for spans in (heard_s, said_s):
            for (_, end), (begin, _) in zip(sorted(spans), sorted(spans)[1:]):
                self.assertLessEqual(end, begin, f"one model answered two at once: {spans}")
        beside = {(e["model_key"], tuple(e["beside"]))
                  for e in M.read_events(folder / "runner" / M.STATS_RECORDS_NAME)
                  if e.get("what") == "answer"}
        self.assertIn(("large-v3", ("translator",)), beside)
        self.assertIn(("translator", ("large-v3",)), beside)
        self.assertEqual((len(co.of("ticket_asked")), len(co.of("rental_granted"))), (1, 1))
        app.close(pipe["id"])
        self.assert_one_load_of_each(self.record(folder), ["large-v3", "translator"])

    def test_an_analysis_session_a_pipeline_per_replay_and_live_work_going_ahead_between(self):
        co = self.coord()
        url, _ = self.served(self.settings("analyst", text=["thinker", "writer"]), co,
                             name="replay-analyst")
        desk_url, _ = self.served(self.settings("desk", whisper=["ears"]), co, name="desk-app")
        analyst, desk = R.Client(url), R.Client(desk_url)
        analyst.bind(self.folder("replays"))
        desk.bind(self.folder("desk"))
        folder = self.folder("desk")
        me, other = analyst.health()["actor"], desk.health()["actor"]
        opened = {}

        def replay(n: int) -> None:
            pipe = opened[n] = analyst.open(["thinker", "writer"], class_="scheduled",
                                            label=f"replay {n}", keep_s=30, wait_s=STEP_S)
            self.assertEqual((pipe["state"], sorted(pipe["up"])),
                             ("holding", ["thinker", "writer"]), pipe)
            think = analyst.submit("generate", {"prompt": f"replay {n}: what happened?"},
                                   {"model": "thinker", "max_tokens": 20}, priority="batch")
            write = analyst.submit("generate", {"prompt": f"replay {n}: write it up"},
                                   {"model": "writer", "max_tokens": 20}, priority="batch",
                                   after=[think])
            if n == 1:
                # live work of another application's asks while the first replay runs
                live.start()
                self.wait_for(lambda: [t for t in co.tickets() if t["actor"] == other],
                              "the other application's live ticket")
            analyst.renew(pipe["id"])
            for jid in (think, write):
                self.assertEqual(analyst.wait(jid, within_s=STEP_S)["job"]["status"], "done")
            for jid in (write, think):              # what waits on a job is forgotten first
                analyst.take(jid)
            closed = analyst.close(pipe["id"])
            self.assertEqual(closed["state"], "closed", closed)

        desk_said = {}

        def live_work():
            pipe = desk.open(["ears"], keep_s=0, label="one dictation", wait_s=STEP_S)
            desk_said["pipe"] = pipe
            desk_said["got"] = self.dictate(desk, write_wav(folder / "live.wav"), "ears",
                                            priority="interactive")
            desk_said["closed"] = desk.close(pipe["id"])
        live = threading.Thread(target=lambda: desk_said.update(
            error=Case.suppress(live_work)), daemon=True)
        replay(1)
        # the second replay asks as soon as the first is done, and waits
        second = threading.Thread(target=lambda: opened.update(
            error=Case.suppress(replay, 2)), daemon=True)
        second.start()
        live.join(STEP_S)
        second.join(STEP_S)
        self.assertFalse(live.is_alive() or second.is_alive(), "a session did not end")
        self.assertIsNone(desk_said.get("error"), desk_said.get("error"))
        self.assertIsNone(opened.get("error"), opened.get("error"))
        self.assertEqual(desk_said["pipe"]["state"], "holding", desk_said)
        # the card went: replay 1, the live work, replay 2 - each its own rental
        self.assertEqual([e["actor"] for e in co.of("rental_granted")], [me, other, me])
        self.assertEqual([e["actor"] for e in co.of("rental_released")], [me, other, me])
        self.assertEqual(len(co.of("ticket_asked", me)), 2, "not one ticket per replay")
        self.assertEqual([t["class"] for t in co.of("ticket_asked") if t["actor"] == me],
                         ["scheduled", "scheduled"])
        self.assertEqual([t["class"] for t in co.of("ticket_asked") if t["actor"] == other],
                         ["responsive"])
        self.assertIsNone(co.rental())

    def test_a_host_runs_a_components_models_in_its_own_session_and_swaps_with_no_gap(self):
        """The Dictaphone's H-1 to H-7, as a trading app would run them: it
        brings up the Dictaphone's models in its own session (H-1); unloads
        them, loads the deep thinker, takes its answer, unloads it and loads
        the dictation models again - ready the moment the load answers (H-4),
        the swap timed whole (H-5); the card held with nothing on it between
        (H-2) while another application's live ask is refused at once, naming
        no one (H-6); and the models stop with the session (H-7). H-3 - an
        amend right after the rental's own unload waits for the memory to
        come back - needs a process named on the rental, which the stand-in
        never names: test_coordination proves it end to end."""
        co = self.coord()
        url, _ = self.served(self.settings("trading", whisper=["large-v3", "nb-large"],
                                           text=["deep"]), co, name="trading-app")
        out_url, _ = self.served(self.settings("outsider", text=["other"]), co,
                                 name="outsider-app")
        host = R.Client(url)
        folder = self.folder("trading")
        host.bind(folder)
        me = host.health()["actor"]
        # the host's session, with the component's models, ready when answered
        pipe = host.open(["large-v3", "nb-large"], keep_s=0, label="trading session",
                         wait_s=STEP_S)
        self.assertEqual(sorted(pipe["up"]), ["large-v3", "nb-large"], pipe)
        held = co.rental()["id"]
        # H-1: the component is handed the host's address and its pipeline:
        # it runs no server, opens no pipeline, and a second beside the
        # host's would be refused
        ears = R.Client(url, pipeline=pipe["id"])
        self.assertEqual(ears.session()["state"], "holding")
        with self.assertRaises(R.Refused) as caught:
            R.Client(url).open(["nb-large"])
        self.assertEqual(caught.exception.reason, "already_open")
        take = write_wav(folder / "analysis.wav", 2.0)
        self.dictate(ears, take, "large-v3", card_patience_s=0, priority="interactive")
        # the swap: the dictation models off...
        for key in ("large-v3", "nb-large"):
            self.assertEqual(host.unload(key)["state"], "off")
        # H-2: nothing on the card, and the card still the host's...
        gap = host.health()["pipeline"]
        self.assertEqual((gap["state"], gap["up"], gap["id"]), ("holding", [], pipe["id"]), gap)
        self.assertEqual((co.rental()["id"], co.rental()["processes"]), (held, []))
        # H-6: ...while another application's live ask - the card now or not
        # at all - is refused at once, naming no one
        outsider = R.Client(out_url)
        outsider.bind(self.folder("outsider"))
        t0 = time.monotonic()
        jid = outsider.submit("generate", {"prompt": "now"}, {"model": "other", "max_tokens": 5},
                              card_patience_s=0, priority="interactive")
        refused = outsider.wait(jid, within_s=STEP_S)["job"]
        self.assertEqual((refused["status"], refused.get("reason")), ("failed", "card_busy"),
                         refused)
        self.assertLess(time.monotonic() - t0, 20.0, "a live ask waited")
        for name in (me, "trading-app", pipe["id"], held):
            self.assertNotIn(name, json.dumps(refused), "the refusal named who has the card")
        self.assertEqual(co.rental()["id"], held, "the empty session lost the card")
        # ...the deep thinker on, ready the moment its load answers (H-4)
        asked = len(host.asked)
        loaded = host.load("deep", wait_s=STEP_S)
        self.assertEqual((loaded["model"]["state"], loaded["pipeline"]["up"]), ("up", ["deep"]))
        think = host.submit("generate", {"prompt": "my analysis, and yours?"},
                            {"model": "deep", "max_tokens": 20})
        self.assertEqual(host.wait(think, within_s=STEP_S)["job"]["status"], "done")
        answered = time.monotonic()
        host.take(think)
        # ...off at once, and the dictation models on again, ready for the answer
        self.assertEqual(host.unload("deep")["state"], "off")
        for key in ("large-v3", "nb-large"):
            self.assertEqual(host.load(key, wait_s=STEP_S)["model"]["state"], "up")
        ready = time.monotonic()
        self.assertEqual([a for a in host.asked[asked:] if a in ("server.health",
                                                                 "server.models.list")], [],
                         "the host asked whether a model was ready: a load's wait says it")
        # H-5, timed whole: the answer done -> whisper ready
        print(f"\n[H-5] answer done -> whisper ready: {ready - answered:.3f}s (stand-ins)",
              flush=True)
        self.dictate(ears, take, "nb-large", card_patience_s=0, priority="interactive")
        # one ticket, one rental, never given back through the swap; never two
        # loads of one model, and the thinker never beside whisper
        self.assertEqual((len(co.of("ticket_asked", me)), len(co.of("rental_granted", me)),
                          co.of("rental_released", me)), (1, 1, []))
        events = self.record(folder)
        self.assertEqual([e for e in events if e.get("event") == "card_given_back"], [],
                         "the card was given back inside the session")
        self.assertEqual(M.max_concurrency(events), 1, "two loads of one model at once")
        self.assertEqual(M.beside_faults(events), [])
        deep = [e for e in events if e.get("event") == "load_ok" and e["model_key"] == "deep"]
        self.assertEqual([e["beside"] for e in deep], [[]], "the thinker went up beside whisper")
        # H-7: stopped, the models stop with the session
        host.close(pipe["id"])
        self.assertEqual({m["key"]: m["state"]["state"] for m in host.models()},
                         {"large-v3": "off", "nb-large": "off", "deep": "off"})
        self.assertIsNone(co.rental())
        with self.assertRaises(R.NotInSession):
            ears.session()


# ======================================================= S18: scheduled work

class Wall:
    """The wall clock the server's queue and the coordinator both read, moved
    by the row - by nine hours in one step, as a machine that slept through
    them finds it. Never slept through for real."""

    def __init__(self, t: float = 1_790_172_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class Card:
    """The card as the coordinator reads it, set by the row: `used` of 24576
    MiB. It asks nothing of the real card, and ends nothing."""

    def __init__(self, used: int):
        self.used = used

    def probe(self, fresh=False):
        return {"known": True, "used_mib": self.used, "total_mib": 24576}


class ScheduledSessionRows(Case):
    """THE OWNER'S CASE, end to end, over HTTP, through the reference client
    (S18; the owner's second ruling of 2026-09-28: "I can plan it earlier in
    the day and just let the program run, then when i go to sleep it runs the
    task" - and "Lets support both combined"). A batch planned in the
    morning, not before a time tonight; live work asked meanwhile goes
    first; at its time the card has no room, and the batch waits for room,
    holding back nobody - live work asked then goes first too; room appears,
    and the batch runs by itself; a patience that runs out while it waits
    fails loudly. Under a coordinator, and on a server of its own - the
    owner's ruling of 2026-09-27: "Yes, same behaviour as coordinator"."""

    LOOK = dict(room_look_s=0.05)

    def batch_settings(self, name: str, **mock) -> Path:
        # the batch's models need 9800 MiB each on the card; the live one
        # nothing - and the lock's own watch (S5b) looks at the card too
        # seldom to call it degraded while the row runs
        path = self.settings(name, text=["painter", "painter2", "quick"],
                             need_mib="{ painter = 9800, painter2 = 9800 }", card_mib=24576,
                             **mock)
        path.write_bytes(path.read_bytes().replace(b"[runner]\n",
                                                   b"[runner]\ncard_watch_s = 3600\n", 1))
        return path

    def owners_case(self, app: R.Client, clk: Wall, fill, free, outside: int, co=None):
        """The owner's case against *app*, its card filled by `fill()` and
        emptied by `free()`; `outside` is what programs outside the bootstrap
        hold while it is full, as the room words say it."""
        tonight = clk.t + 9 * 3600
        batch = app.plan("generate", {"prompt": "page 1 of tonight's batch"},
                         {"model": "painter", "max_tokens": 8}, not_before=tonight,
                         card_patience_s=4 * 3600)
        job = app.result(batch)["job"]
        self.assertEqual((job["status"], job.get("not_before")), ("queued", tonight), job)
        planned = self.wait_for(lambda: app.scheduled(), "health to say the batch is planned")
        self.assertEqual((planned["planned"], planned["next_at"], planned["for_room"]),
                         (1, tonight, 0), planned)
        if co is not None:
            placed = self.wait_for(lambda: [t for t in co.tickets()
                                            if t.get("not_before") == tonight],
                                   "its place in the coordinator's line, from the morning")
            self.assertEqual((placed[0]["state"], placed[0]["class"]), ("waiting", "scheduled"))
        # live work asked in the morning goes first: the batch holds back nobody
        live = app.submit("generate", {"prompt": "the command, as heard"},
                          {"model": "quick", "max_tokens": 4}, priority="interactive")
        self.assertEqual(app.wait(live, within_s=STEP_S)["job"]["status"], "done")
        self.assertEqual(app.result(batch)["job"]["status"], "queued", "it ran before its time")
        # tonight: the machine slept through the time, and the card is full
        fill()
        clk.t = tonight + 60
        waiting = self.wait_for(
            lambda: (lambda j: j if (j["progress"] or {}).get("stage")
                     == "waiting for room on the card" else None)(app.result(batch)["job"]),
            "the batch at its time, waiting for room")
        said = waiting["progress"]["waiting_for"]
        for words in ("painter: it needs 9800 MiB", f"programs outside the bootstrap hold "
                      f"{outside} MiB", "It waits for room, and starts by itself once it fits"):
            self.assertIn(words, said)
        self.assertEqual(waiting["status"], "queued", "waiting for room, it holds a thread")
        scheduled = app.scheduled()
        self.assertEqual((scheduled["for_room"], scheduled["room"]), (1, said), scheduled)
        # live work asked while it waits for room goes first
        live = app.submit("generate", {"prompt": "another command"},
                          {"model": "quick", "max_tokens": 4}, priority="interactive")
        self.assertEqual(app.wait(live, within_s=STEP_S)["job"]["status"], "done")
        # still waiting: tried again at each look its card source says it
        # may - here, inside this server's own rental while it held one - and
        # never done while its model does not fit
        self.assertIn(app.result(batch)["job"]["status"], ("queued", "running"))
        if co is not None:
            # its place in the coordinator's line, passed over for room: the
            # numbers it waits on, as the operator's /state says them
            placed = self.wait_for(lambda: [t for t in co.tickets() if t.get("room")],
                                   "its ticket in line, passed over for room")
            self.assertEqual((placed[0]["class"], placed[0]["room"]["outside_mib"]),
                             ("scheduled", outside), placed)
        # room appears: the batch runs by itself
        free()
        got = app.wait(batch, within_s=STEP_S)
        self.assertEqual(got["job"]["status"], "done", got["job"])
        self.assertTrue(got["candidates"] and got["candidates"][0]["text"], got)
        # while it waited it was tried again only when its card source said
        # it might go - never at every look (every 50 ms here)
        self.assertLessEqual(got["candidates"][0]["sample"], 8, got["candidates"][0])
        # A PATIENCE THAT RUNS OUT while it waits for room fails loudly
        fill()
        # its second model: the first, warm on the card, comes off to make room
        short = app.plan("generate", {"prompt": "page 2"}, {"model": "painter2", "max_tokens": 8},
                         not_before=clk.t, card_patience_s=1800)
        self.wait_for(lambda: (app.result(short)["job"]["progress"] or {}).get("stage")
                      == "waiting for room on the card", "the second page, waiting for room")
        clk.t += 1800
        ended = app.wait(short, within_s=STEP_S)["job"]
        self.assertEqual((ended["status"], ended.get("reason")), ("failed", "no_room"), ended)
        for words in ("painter2 did not fit on the card within its patience of 30 min",
                      f"programs outside the bootstrap hold {outside} MiB",
                      "It waited 30 min for room, within its patience, and none came",
                      "Close programs that use the card, or reboot"):
            self.assertIn(words, ended["error"])
        free()
        return batch

    def test_the_owners_case_under_a_coordinator(self):
        clk, card = Wall(), Card(used=1000)
        c = C.Coordinator(self.tmp / "coordinator-night", ttl_s=10 ** 6, drop_s=10 ** 6,
                          ports=registry(), hold_s=5.0, hardware=card, clock=clk,
                          recheck_s=0.05)
        srv = C.serve(c, port(), log=lambda s: None)
        self.addCleanup(C.stop, srv)
        co = in_process(c, srv)
        client = K.CoordinatorClient(co.url, ids.mint_actor("srv"), "image-batch",
                                     log=self.said.append)
        self.addCleanup(client.close)
        backend = S.build_backend("mock", self.batch_settings("night"), log=self.said.append,
                                  coordinator=client, vram=lambda: None)
        httpd = S.serve(port(), backend=backend, log=self.said.append, actor=client.actor,
                        coordinator=client, clock=clk, **FAST, **self.LOOK)
        self.addCleanup(S.stop, httpd)
        app = R.Client(httpd.app.url)
        app.bind(self.folder("night"))

        def fill():
            card.used = 20000

        def free():
            card.used = 1000
        self.owners_case(app, clk, fill, free, outside=20000, co=co)
        me = client.actor
        classes = [(t["class"], t.get("not_before") is not None)
                   for t in co.of("ticket_asked", me)]
        self.assertIn(("scheduled", True), classes, "the batch never kept its place in line")
        self.assertTrue(co.of("ticket_passed_over", me), "it was never passed over for room")
        over = co.of("ticket_passed_over", me)[0]
        self.assertEqual(over.get("outside_mib"), 20000)

    def test_the_owners_case_on_a_server_of_its_own(self):
        clk = Wall()
        settings = self.batch_settings("own")
        backend = S.build_backend("mock", settings, log=self.said.append, vram=lambda: None)
        httpd = S.serve(port(), backend=backend, log=self.said.append, clock=clk, **FAST,
                        **self.LOOK)
        self.addCleanup(S.stop, httpd)
        app = R.Client(httpd.app.url)
        app.bind(self.folder("own"))
        # THE STAND-IN'S CARD (H9's `[mock] card_mib`), filled and emptied by
        # the row: read full, it has nothing free
        stand_in = backend.manager._vram.__self__

        def fill():
            stand_in.full_from, stand_in.full_for = 0.0, 10.0 ** 9

        def free():
            stand_in.full_from = None
        self.owners_case(app, clk, fill, free, outside=24576)
        lock = self.tmp / "own-card.lock"
        self.assertTrue(lock.exists(), "not on the lock of its own")


def in_process(c, srv):
    """The in-process coordinator as the rows read it."""
    co = Coord.__new__(Coord)
    co.c, co.srv = c, srv
    co.url = f"http://127.0.0.1:{srv.server_address[1]}"
    return co


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    _suites.report(unittest.main(exit=False, verbosity=1))
