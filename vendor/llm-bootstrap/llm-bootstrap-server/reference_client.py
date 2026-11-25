"""The reference client: an application's whole use of its bootstrap server,
in the standard library alone - what a client in any language does (GUIDE.md,
"Use the server from any language").

    from reference_client import Client, start_server

    server = start_server(["--backend", "real", "--runner", "runner.toml",
                           "--name", "dictaphone"])          # 1. start its server...
    app = Client(server.url)            # ...its contract's version checked first
    app.bind("C:/work/dictations")                            # ...and bind its folder
    pipe = app.open(models=["large-v3", "nb-large"], keep_s=0, wait_s=60)   # 2. open
    job = app.submit("transcribe", {"audio": "C:/work/take-1.wav"},
                     {"model": "large-v3"})                   # 3. submit...
    result = app.wait(job, within_s=600)                      # ...and wait for its end
    app.take(job)                                             # 4. claim it, forget it
    app.close(pipe["id"])                                     # 5. close
    server.stop()

THE FIVE KINDS OF CALL (PLAN-4 D13), and the two a host adds:
  1. `start_server` starts `server.py` as a child and reads the line that says
     where it listens; `bind` points it at the application's folder;
  2. `open` the pipeline - the card held from now until it is closed, its
     models loaded and kept - and `renew` it, when it was opened with a
     `keep_s` above 0 (0: held until it is closed, nothing to renew);
  3. `submit` a job, then `wait` for its end: `result(job, wait_s=30)` asked
     again until the job has ended, each answer held by the server until the
     job ends or the 30 s pass - nothing asks again and again;
  4. `take` the result: claim it, so it stops being offered, then forget the
     job, so the record drops it;
  5. `close` the pipeline: every model comes off, and the card goes back.
  And the server a client started, stopped: `StartedServer.stop` asks it
  over HTTP (`Client.stop`, the route `server.stop`) - its rental handed
  back, never left to lapse - and ends it by its handle only when it does
  not answer.
  A host swapping models inside its session (the Dictaphone's H-1 to H-7):
  `load` a model into the open pipeline and `unload` one - `load(key,
  wait_s=N)` answers the moment the model is ready.

EVERY ANSWER IS CHECKED against the contract, as the client's JavaScript does
(`llm-bootstrap-client/parts/api.js`):
  * a body the route refuses - or any body, for a route that takes none - is a
    bug on THIS side: ValueError, before anything is sent;
  * an answer the contract declares for its status is an answer: 200 is
    returned; a declared refusal is raised as `Refused`, carrying its status,
    its `reason` - what a program branches on - and its `error`, the sentence
    for a person;
  * anything else - silence, a status the route does not declare, a field
    missing, a reason not in its list - is NO ANSWER: `NoAnswer`.

THE CONTRACT'S VERSION. A client runs against a copy of the contract (this
repository's, unless it is given another); its version is
`validate.contract_version()` of that copy, and the server's health says the
version it serves. `Client` asks health first and refuses to go on -
`OtherContract`, naming both - when the two differ: an answer checked against
a contract the server does not serve proves nothing.

A COMPONENT HANDED ITS HOST'S SERVER (PLAN-4 D6, the Dictaphone's H-1). A
host - a trading app that brings up the Dictaphone - holds one pipeline, and
the component's models are in the host's settings. The host hands the
component two things: its server's address and its pipeline's id. The
component runs no server and opens no pipeline:

    ears = Client(host_url, pipeline=host_pipeline_id)
    ears.session()      # the host's pipeline, as the host's server says it stands
    job = ears.submit("transcribe", {...}, {"model": "large-v3"})

A component's `open`, `close`, `load` and `unload` are refused before anything
is sent: the session is the host's.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONTRACT = HERE.parent / "llm-bootstrap-contract"
if str(CONTRACT) not in sys.path:
    sys.path.insert(0, str(CONTRACT))
import ids                                                      # noqa: E402
import validate as V                                            # noqa: E402

#: how long an answer is waited for beyond what the call asked the server to
#: hold it (`wait_s`): the server answers at the end of the wait, and a busy
#: machine answers a little late
ANSWER_MARGIN_S = 10.0
#: the longest a server holds an answer (the contract's `wait_s`, 0 to 60)
WAIT_MAX_S = 60.0
#: how long a close is waited for: its answer comes once every model's
#: process has gone and the card is back
CLOSE_S = 120.0
#: a job that has ended - its status never changes again but by a claim
ENDED = ("done", "failed", "cancelled", "claimed")
#: the line a server prints once it listens (`server.main`)
LISTENING = re.compile(r"^listening on (http://\S+) as (srv-[a-z0-9]{8})\s*$")


class NoAnswer(Exception):
    """No answer: silence, or an answer the contract does not declare for its
    route and status - which is no answer either."""


class Refused(Exception):
    """A refusal the contract declares for its route: an answer, not a
    failure to answer. A program branches on `reason`, never on `error`."""

    def __init__(self, route_id: str, status: int, answer: dict):
        self.route_id, self.status, self.answer = route_id, status, answer
        self.reason, self.error = answer["reason"], answer["error"]
        super().__init__(f"{route_id} refused ({status} {self.reason}): {self.error}")


class OtherContract(Exception):
    """The server serves another version of the contract than this client runs."""


class NotInSession(Exception):
    """A component's host has no pipeline open under the id it handed over."""


def _body_or_none(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return None


def _seconds(value: float) -> str:
    """A number of seconds as the contract writes `wait_s`: digits, a point,
    digits - never an exponent, a sign or a space."""
    value = float(value)
    if not (math.isfinite(value) and 0 <= value <= WAIT_MAX_S):
        raise ValueError(f"wait_s is a number of seconds from 0 to {WAIT_MAX_S:g}, "
                         f"not {value!r}")
    return f"{value:.3f}"


class Client:
    """An application's calls to its bootstrap server at `base`
    (`http://<host>:<port>`), each checked against `contract` - a copy of
    the contract's folder. `pipeline` makes it a component's: the id of its
    host's pipeline, handed to it with the host's address. `actor` is the
    client's own id (a `cli-` actor), recorded on every job it submits and
    claims; one is minted when none is given."""

    def __init__(self, base: str, *, contract=CONTRACT, pipeline: str | None = None,
                 actor: str | None = None, timeout_s: float = 10.0, check_version: bool = True):
        at = re.fullmatch(r"(https?://[^/?#@\s]+)/?", str(base))
        if not at:
            raise ValueError(f"{base!r} is not a server's address: one is written "
                             f"http://<host>:<port>")
        if pipeline is not None and not ids.matches("pipeline", pipeline):
            raise ValueError(f"{pipeline!r} is not a pipeline's id")
        self.base = at.group(1)
        self.contract = Path(contract)
        self.shapes, routes = V.load(self.contract)
        self.routes = {r["id"]: r for key, r in routes.items() if key.startswith("server/")}
        self.version = V.contract_version(self.contract)
        self.pipeline = pipeline
        self.actor = actor or ids.mint_actor("cli")
        if not ids.matches("actor", self.actor):
            raise ValueError(f"{self.actor!r} is not an actor's id")
        self.timeout_s = float(timeout_s)
        #: every route asked, in order - what a row counts
        self.asked: list = []
        if check_version:
            self.check_version()

    # ---------------------------------------------------------------- the door

    def call(self, route_id: str, *, params=None, query=None, body=None,
             timeout: float | None = None):
        """-> (status, answer), the answer checked against the shape the
        contract declares for its status. Raises ValueError for a body the
        route refuses (nothing is sent), NoAnswer for silence or an answer
        the contract does not declare."""
        route = self.routes.get(route_id)
        if route is None:
            raise ValueError(f"the contract has no server route called {route_id}")
        if route["request"] is None and body is not None:
            raise ValueError(f"{route_id} takes no body")
        if route["request"] is not None and body is not None:
            wrong = V.check(body, route["request"], self.shapes)
            if wrong:
                raise ValueError(f"{route_id}: not a body the contract allows: {wrong[0]}")
        path = re.sub(r"\{(\w+)\}",
                      lambda m: urllib.parse.quote(str((params or {})[m.group(1)]), safe=""),
                      route["path"])
        q = {k: v for k, v in (query or {}).items() if v is not None}
        if q:
            path += "?" + urllib.parse.urlencode(q)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=route["method"],
                                     headers={"Content-Type": "application/json"}
                                     if data is not None else {})
        self.asked.append(route_id)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout_s) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            with e:                                         # an answer is a response: closed
                status, raw = e.code, e.read()
        except OSError as e:                                # refused, reset, timed out
            raise NoAnswer(f"{self.base} did not answer {route_id}: {e}") from None
        answer = _body_or_none(raw)
        declared = route["responses"].get(str(status))
        wrong = (["no shape is declared for it"] if declared is None
                 else V.check(answer, declared, self.shapes))
        if wrong:
            raise NoAnswer(f"{route_id} answered {status}, which the contract does not "
                           f"declare: {wrong[0]}")
        return status, answer

    def ask(self, route_id: str, **kw) -> dict:
        """-> the 200 answer. Raises Refused for a refusal the contract
        declares, NoAnswer for anything else."""
        status, answer = self.call(route_id, **kw)
        if status != 200:
            raise Refused(route_id, status, answer)
        return answer

    def _held(self, wait_s) -> float:
        """How long to wait for an answer the server holds `wait_s`."""
        return self.timeout_s + (float(wait_s) if wait_s is not None else 0.0) + ANSWER_MARGIN_S

    def _own(self, what: str) -> None:
        if self.pipeline is not None:
            raise ValueError(f"a component works in its host's pipeline ({self.pipeline}): "
                             f"the {what} is its host's to ask")

    # --------------------------------------------------------------- the calls

    def health(self) -> dict:
        return self.ask("server.health")

    def check_version(self) -> str:
        """-> the version both speak. Raises OtherContract when the server
        serves another."""
        theirs = self.health()["contract_version"]
        if theirs != self.version:
            raise OtherContract(f"{self.base} serves contract {theirs}, and this client runs "
                                f"contract {self.version} (from {self.contract}): run both "
                                f"from one contract")
        return theirs

    def stop(self) -> dict:
        """Stop the server (`server.stop`): -> its answer, once it has begun
        its last act - the folder put down, its models off, its rental handed
        back. A component asks none: the server is its host's."""
        self._own("stop")
        status, answer = self.call("server.stop")
        if status != 202:
            raise Refused("server.stop", status, answer)
        return answer

    def bind(self, folder) -> dict:
        """1. Point the server at the application's folder."""
        return self.ask("server.workspace.bind", body={"path": str(folder)})

    def unbind(self) -> dict:
        return self.ask("server.workspace.unload")

    def open(self, models=(), *, class_: str | None = None, label: str | None = None,
             keep_s: float | None = None, wait_s: float | None = None,
             if_no_room: str | None = None) -> dict:
        """2. Open the application's pipeline: -> the pipeline. With `wait_s`,
        answered once every model it names is up (or its load failed), or it
        holds the card when it names none, or `wait_s` has passed. A scheduled
        pipeline's `if_no_room` - `wait` or `fail` - is what it does when its
        first model does not fit (S18); None, the server's own."""
        self._own("pipeline")
        body = {"models": list(models)}
        for k, v in (("class", class_), ("label", label), ("keep_s", keep_s),
                     ("if_no_room", if_no_room)):
            if v is not None:
                body[k] = v
        query = {"wait_s": _seconds(wait_s)} if wait_s is not None else None
        return self.ask("server.pipeline.open", body=body, query=query,
                        timeout=self._held(wait_s))["pipeline"]

    def renew(self, pipeline_id: str) -> dict:
        """2. Keep the pipeline open: it is still wanted."""
        return self.ask("server.pipeline.renew", body={"pipeline": pipeline_id})["pipeline"]

    def close(self, pipeline_id: str, within_s: float = CLOSE_S) -> dict:
        """5. Close the pipeline: -> it, closed. Answered once every model has
        come off and the card has gone back."""
        self._own("pipeline")
        return self.ask("server.pipeline.close", body={"pipeline": pipeline_id},
                        timeout=float(within_s))["pipeline"]

    def load(self, key: str, wait_s: float | None = None) -> dict:
        """Load a model into the open pipeline and keep it: -> {model,
        pipeline}. With `wait_s`, answered the moment the model is ready - or
        its load failed, or `wait_s` passed: `model.state` says which."""
        self._own("models")
        query = {"wait_s": _seconds(wait_s)} if wait_s is not None else None
        return self.ask("server.models.load", params={"key": key}, query=query,
                        timeout=self._held(wait_s))

    def unload(self, key: str, within_s: float = CLOSE_S) -> dict:
        """Take a model off the card: -> its state, `off`. The pipeline keeps
        the card with nothing loaded too."""
        self._own("models")
        return self.ask("server.models.unload", params={"key": key},
                        timeout=float(within_s))["model"]

    def models(self) -> list:
        return self.ask("server.models.list")["models"]

    def submit(self, kind: str, input, gen: dict, **job) -> str:
        """3. Queue a job: -> its id. `job` is the rest of the request -
        `priority`, `card_patience_s`, `after`, `scope`... - as the contract
        names it; the client's own id is recorded on it. A job that names no
        `priority` is live (`interactive`, S18b); `plan` is the one that says
        `batch`."""
        body = {"kind": kind, "input": input, "gen": dict(gen), "client": self.actor, **job}
        return self.ask("server.jobs.submit", body=body)["job_id"]

    def plan(self, kind: str, input, gen: dict, not_before: float, *,
             if_no_room: str | None = None, **job) -> str:
        """3. PLAN A BATCH for later (S18, the owner's second ruling of
        2026-09-28: "I can plan it earlier in the day and just let the program
        run, then when i go to sleep it runs the task"): a batch job that
        starts no earlier than `not_before` - seconds since the epoch - and
        the first moment after it that its model fits. Until then it is queued
        and holds back nobody; its patience (`card_patience_s`, the server's
        own unless named) counts from its time. `if_no_room` - `wait` or
        `fail` - is what it does when its model does not fit then; None, the
        server's own. -> its id. `wait` on it as on any job; `scheduled()`
        says what the planned work waits for meanwhile."""
        return self.submit(kind, input, gen, priority="batch", not_before=float(not_before),
                           **({"if_no_room": if_no_room} if if_no_room is not None else {}),
                           **job)

    def scheduled(self):
        """3. What the server's planned and waiting batch work waits for, as
        health says it (S18): {planned, next_at, for_room, room} - or None,
        when nothing waits."""
        return self.health()["queue"].get("scheduled")

    def result(self, job_id: str, wait_s: float | None = None) -> dict:
        """3. The job and what it has produced so far: {job, candidates}. With
        `wait_s`, held by the server until the job ends or `wait_s` passes."""
        query = {"wait_s": _seconds(wait_s)} if wait_s is not None else None
        return self.ask("server.jobs.result", params={"id": job_id}, query=query,
                        timeout=self._held(wait_s))

    def wait(self, job_id: str, within_s: float, wait_s: float = 30.0,
             clock=time.monotonic) -> dict:
        """3. The job's result once it has ended - or once it is HELD for a
        person's check (a job submitted with `check`): nothing more comes of
        it until it is sent, so it is returned then, `held`, as the server
        answers the wait (R-B3). `result(wait_s=...)` asked again until
        either - each answer held by the server, so nothing asks again and
        again. Raises TimeoutError, naming the job and where it stands, when
        it has neither ended nor been held within `within_s`."""
        deadline = clock() + float(within_s)
        while True:
            left = max(0.0, deadline - clock())
            got = self.result(job_id, wait_s=min(float(wait_s), left))
            if got["job"]["status"] in ENDED or got["job"]["status"] == "held":
                return got
            if clock() >= deadline:
                raise TimeoutError(f"{job_id} had not ended {within_s:g}s after it was waited "
                                   f"for: it is {got['job']['status']}")

    def claim(self, job_id: str) -> dict:
        """4. Take the result: it stops being offered. -> the job."""
        return self.ask("server.jobs.claim", params={"id": job_id},
                        body={"client": self.actor})["job"]

    def forget(self, job_id: str) -> str:
        """4. Drop the finished job from the record."""
        return self.ask("server.jobs.forget", params={"id": job_id})["forgot"]

    def take(self, job_id: str) -> dict:
        """4. Claim the job's result, then forget the job: -> the job, claimed."""
        job = self.claim(job_id)
        self.forget(job_id)
        return job

    def session(self) -> dict:
        """A component's: its host's pipeline, as the host's server says it
        stands. Raises NotInSession when the host has no pipeline open under
        the id it handed over - it closed it, lost it, or never opened it."""
        if self.pipeline is None:
            raise ValueError("session() is a component's: it was handed no host pipeline")
        pipe = self.health()["pipeline"]
        if pipe is None or pipe["id"] != self.pipeline or pipe["state"] in ("closed", "lost"):
            said = "no runner" if pipe is None else f"{pipe['id']}, {pipe['state']}"
            raise NotInSession(f"the host's server at {self.base} has no pipeline "
                               f"{self.pipeline} open (it has {said})")
        return pipe


# ------------------------------------------------------------ call 1: start

class CannotStart(Exception):
    """The server did not say it was listening: it ended first, or took too long."""


class StartedServer:
    """A server this client started: its address, its actor id, its process
    (by the handle that started it - never by pid) and every line it printed."""

    def __init__(self, proc, url: str, actor: str, lines: list, reader: threading.Thread):
        self.proc, self.url, self.actor, self.lines = proc, url, actor, lines
        self._reader = reader

    def client(self, **kw) -> Client:
        return Client(self.url, **kw)

    def stop(self, within_s: float = 60.0):
        """Stop it, and wait until it has ended: -> its exit code. Asked over
        HTTP first (`server.stop`), so it does its last act - its folder put
        down, its models off, its rental HANDED BACK to the coordinator - and
        ends by itself, exit code 0. One that does not answer, or has not
        ended within `within_s`, is ended by the handle that started it: an
        ended server's card is taken back only when its heartbeats stop."""
        if self.proc.poll() is None:
            try:
                self.client(check_version=False).stop()
            except (NoAnswer, Refused):
                self.proc.terminate()
        try:
            code = self.proc.wait(within_s)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                code = self.proc.wait(within_s)
            except subprocess.TimeoutExpired:
                raise CannotStart(f"the server {self.actor} at {self.url} was ended and had "
                                  f"not gone {within_s:g}s later") from None
        _drained(self.proc, self._reader, within_s)
        return code


def _drained(proc, reader: threading.Thread, within_s: float) -> None:
    """Once *proc* has ended: its last lines read, and its output closed."""
    reader.join(within_s)
    if proc.stdout is not None and not reader.is_alive():
        proc.stdout.close()


def start_server(args, *, command=None, start_s: float = 60.0, cwd=None) -> StartedServer:
    """1. Start a bootstrap server as a child: `command` (by default this
    Python running `server.py`) with `args` - its command line, as
    `server.py --help` says it. -> the server once it prints where it
    listens. One that ends first, or says nothing within `start_s`, raises
    CannotStart with what it printed - a start that is slow on a busy machine
    is given the whole of `start_s`."""
    cmd = [*(command or [sys.executable, str(HERE / "server.py")]), *map(str, args)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, cwd=cwd, encoding="utf-8",
                            errors="replace")
    lines: list = []
    heard = threading.Event()
    found: dict = {}

    def read():
        # read every line for as long as it runs: a pipe nobody reads fills,
        # and the server writing to it would wait for ever
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))
            m = LISTENING.match(line.rstrip("\n"))
            if m and not found:
                found.update(url=m.group(1), actor=m.group(2))
                heard.set()
        heard.set()
    reader = threading.Thread(target=read, daemon=True, name="reference-client-server-lines")
    reader.start()
    heard.wait(float(start_s))
    if found:
        return StartedServer(proc, found["url"], found["actor"], lines, reader)
    # its output closed without the line: it ended first
    ended = heard.is_set()
    if proc.poll() is None:
        proc.kill()                                         # by its own handle
    try:
        code = proc.wait(60.0)
    except subprocess.TimeoutExpired:
        code = None
    else:
        _drained(proc, reader, 60.0)
    said = " | ".join(lines[-8:]) or "nothing"
    if ended:
        raise CannotStart(f"the server ended (exit {code}) without saying it was listening: "
                          f"{said}")
    raise CannotStart(f"the server said nothing of listening in {start_s:g}s, and was ended: "
                      f"{said}")
