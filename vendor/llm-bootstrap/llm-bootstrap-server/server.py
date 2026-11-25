"""The server: the job queue on HTTP, bound to one folder at a time.

    python server.py [--port 7072] [--host 127.0.0.1] [--workspace <folder>]
                     [--backend none|echo|mock|real|llamacpp] [--runner <settings.toml>]
                     [--reclaim] [--coordinator <url>] [--name <what it calls itself>]
                     [--client <folder>] [--allow-origin <page address> ...]
                     [--max-body <bytes>] [--read-timeout <seconds>]

Every route it answers is one the contract declares
(`llm-bootstrap-contract/routes/server`), and every answer is one of that
route's declared shapes. The route table is READ FROM the contract when the
server is made, and a route the contract declares that this file does not
serve - or the other way round - stops it from starting.

A request body is checked against its route's request shape with the
contract's own validator, so `bad_request` names the first problem in exactly
the words the contract's examples use. A missing field the route has its own
refusal for (`kind`, `gen.model`, `path`) is left to that refusal.

THE BACKEND is the model. `--backend none` (the default) has none, and every
generation fails saying so. `--backend echo` answers each request back - a
stand-in that lets the server run end to end with no model and no GPU, and
never anything more. `--backend mock`, `--backend real` and `--backend
llamacpp` put the RUNNER behind the queue (`runner/adapter.py`), with the
models of the runner's settings file (`--runner`): the stand-in model,
answering in each model's engine's shape; each model on its own engine,
llama.cpp or whisper (PLAN-4 D10); or every model on llama.cpp, the name
`real` had before engines were named. The runner holds the
card under this server's actor id, keeps its record in the bound folder's
`runner/`, and takes the model off the card when the server stops. What a
stopped server of a folder left running is reported at bind, and reclaimed
only with `--reclaim` - and only on proof that it is this runner's own.

WITH A COORDINATOR (`--coordinator`), the server is one of its holders. The
runner rents the card before a model's weights go up - a ticket asked for
once, waited on, and claimed the moment it is offered - and gives it back when
the model comes off; a job's priority is its ticket's class (one that names
none is interactive: responsive, S18b), and its own patience
(`card_patience_s`) how long it waits. A load's port and - when no `--port`
is given - the server's own port come from the coordinator's registry. The
server's actor id is minted before anything is asked for, and is the one name
it holds the folder, the rental and the ports by. Health's `card` says where
it stands: `reachable` or `unreachable` (the last contact), `holding`,
`waiting_since`. A coordinator that cannot be reached never takes
anything away - and one that cannot be reached for the server's own port
stops the start, saying so.

WITHOUT `--coordinator`, a real card's server finds the coordinator by itself
(PLAN-4 D5): each time a session opens it looks at the machine's card lock -
free, it takes it; held by a coordinator, the session goes through the
coordinator at the address the lock's note gives, as `--name`; held by an
application run on its own, it waits. `--coordinator` stays, as the explicit
way: for the stand-in model with a coordinator, for a coordinator whose lock
this user cannot see, and for the server's own port from the registry.

A PAGE FROM ANOTHER ADDRESS - an application's own page showing the client's
widgets - is answered only when the server was started to allow it
(`--allow-origin`, repeatable): a request whose Origin is neither the server's
own nor named is refused before any route, whatever it asks, so a web page
open in the same browser can never act on the server (`Handler`). Before
that, the NAME a request was sent to: a Host that is not one of the server's
own names - a website whose name was made to point at this machine (DNS
rebinding) - is refused, with or without an Origin. And after it, the BODY: one
sent in chunks (a Transfer-Encoding), or over the cap (`--max-body <bytes>`, 16
MiB unless given), is refused and never read. Every request goes through these
checks whatever its method; a method no route takes is then answered 501. A
request must arrive whole within `--read-timeout <seconds>` (10 unless given)
of the server starting to wait for it, or its connection is closed. A request
with no Origin, sent to one of the server's own names, and one from the
server's own page, are answered as before. The server's port is its alone: a
port another program holds stops the start, never shared (`Listener`).

What is outside the contract, because no route can declare it: a path no
route has (404 `no_route`), a method a route does not take (405
`method_not_allowed`), and a failure of the server itself (500 `internal`,
logged with its traceback - a bug, never an answer).
"""
from __future__ import annotations

import argparse
import io
import json
import math
import re
import socket
import sys
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

HERE = Path(__file__).resolve().parent
CONTRACT = HERE.parent / "llm-bootstrap-contract"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(CONTRACT))
import configs                                                  # noqa: E402
import coordination as K                                        # noqa: E402
import ids                                                      # noqa: E402
import jobqueue as Q                                            # noqa: E402
import validate as V                                            # noqa: E402

#: The longest an answer may be asked to wait - a job's result for the job to
#: end, a model's load or a pipeline's open for its models to be up - in
#: seconds: the contract's word (`server.jobs.result`, `server.models.load`,
#: `server.pipeline.open`), not a setting: an application written against the
#: contract relies on it.
RESULT_WAIT_MAX_S = 60
#: `wait_s` is a plain number: digits, and a fraction if any. Never what
#: float() also takes - "nan", "1e1", " 5", or digits of another script.
WAIT_S = re.compile(r"[0-9]+(?:\.[0-9]+)?")

#: A missing field that a route answers with a refusal of its own, rather than
#: with `bad_request` - the problem strings exactly as the validator words them.
OWN_REFUSALS = {
    "server.jobs.submit": {'$: missing required field "kind"',
                           '$: missing required field "gen"',
                           '$.gen: missing required field "model"'},
    "server.workspace.bind": {'$: missing required field "path"'},
}

#: Routes a PAGE may never ask - a request that carries an Origin, the
#: server's own page and a page it was started to allow included - refused
#: `from_a_page` before their handler runs: stopping the server is a
#: program's on this machine, or a person's at its console (H8, S13b R2).
NOT_FROM_A_PAGE = {"server.stop"}


class EchoBackend:
    """A stand-in model: it answers the request back, reporting progress as it
    goes. It is what lets the server run with no model and no GPU."""

    def __init__(self, delay_s: float = 0.02):
        self.delay_s = delay_s

    def __call__(self, request, params, run):
        if isinstance(request, dict) and isinstance(request.get("prompt"), str):
            text = request["prompt"]
        elif request is None:
            text = ""
        else:
            text = json.dumps(request, ensure_ascii=False)
        words = text.split()
        for i in range(1, min(len(words), 20) + 1):
            if run.stopped():
                raise Q.Cancelled("the echo saw the cancel")
            run.report(tokens=i)
            time.sleep(self.delay_s)
        return {"text": text, "tokens": len(words), "finish_reason": "stop",
                "model": params.get("model") or "echo"}

    def cancel(self, handle) -> None:
        """Nothing to stop: the echo watches `run.cancelled` itself."""


class Server:
    """What the routes do. Every handler returns (status, body)."""

    def __init__(self, hooks=None, backend=None, url=None, log=None, actor=None,
                 coordinator=None, **queue_config):
        """`actor` is this server's own id, minted when it starts: the name it
        holds its folder by, and the start of every generation's handle.
        `coordinator` is its client of the coordinator, when there is one: it
        speaks for the same actor."""
        self.hooks, self.backend, self.url = hooks, backend, url
        if coordinator is not None and actor is not None and actor != coordinator.actor:
            raise ValueError(f"one server, one actor: {actor} cannot hold through "
                             f"{coordinator.actor}'s client")
        self.coordinator = coordinator
        self.actor = actor or (coordinator.actor if coordinator else ids.mint_actor("srv"))
        # A backend with a runner behind it holds the card under THIS server's
        # actor, and hears when folders come and go.
        attach = getattr(backend, "attach", None)
        if attach is not None:
            attach(self.actor)
        # ...and, when a cancel lands during a load, asks whether a job that
        # can run next wants that model - of whichever folder is bound by then;
        # and, before the queue's own pipeline lets the card go, whether a job
        # is still queued
        if hasattr(backend, "wants"):
            backend.wants = self._wants
        if hasattr(backend, "work_pending"):
            backend.work_pending = self._work_pending
        self.queue_config = queue_config
        self._log = log or (lambda s: print(s, flush=True))
        self._bind_lock = threading.RLock()
        self.queue = Q.Queue(None, start=False, actor=self.actor)
        _, routes = V.load(CONTRACT)
        self.routes = [r for key, r in routes.items() if key.startswith("server/")]
        # the version of the contract this server serves - the folder its route
        # table is read from, hashed once - so health can say which it speaks
        self.contract_version = V.contract_version(CONTRACT)
        self._shapes = V.load(CONTRACT)[0]
        self.handlers = {
            "server.health": lambda p, q, b: self.health(),
            "server.workspace.get": lambda p, q, b: self.workspace(),
            "server.workspace.bind": lambda p, q, b: self.bind(b),
            "server.workspace.unload": lambda p, q, b: self.unload(),
            "server.jobs.submit": lambda p, q, b: self.submit(b),
            "server.jobs.list": lambda p, q, b: self.jobs(q),
            "server.jobs.result": lambda p, q, b: self.result(p["id"], q),
            "server.jobs.claim": lambda p, q, b: self._job(
                lambda s: s.claim(p["id"], client=b.get("client"))),
            "server.jobs.cancel": lambda p, q, b: self._job(lambda s: s.cancel(p["id"])),
            "server.jobs.retry": lambda p, q, b: self._job(lambda s: s.retry(p["id"]),
                                                           creates=True),
            "server.jobs.give_up": lambda p, q, b: self._job(lambda s: s.give_up(p["id"])),
            "server.jobs.forget": lambda p, q, b: self._job(lambda s: s.forget(p["id"])),
            "server.jobs.held": lambda p, q, b: self._job(lambda s: {"held": s.held(p["id"])}),
            "server.jobs.send": lambda p, q, b: self._job(lambda s: s.send(
                p["id"], request=b["request"] if "request" in b else Q.AS_BUILT,
                gen=b.get("gen"), unset=b.get("unset"))),
            "server.jobs.prune": lambda p, q, b: self.prune(b),
            "server.config.get": lambda p, q, b: self.config_get(),
            "server.config.keep": lambda p, q, b: self.config_keep(b),
            "server.models.list": lambda p, q, b: self.models(),
            "server.models.read_speed": lambda p, q, b: self.read_speed(p["key"]),
            "server.models.load": lambda p, q, b: self.load_model(p["key"], q),
            "server.models.unload": lambda p, q, b: self.unload_model(p["key"]),
            "server.pipeline.open": lambda p, q, b: self.open_pipeline(b, q),
            "server.pipeline.renew": lambda p, q, b: self._pipeline(
                "renew_pipeline", "it has no pipeline", b["pipeline"]),
            "server.pipeline.close": lambda p, q, b: self._pipeline(
                "close_pipeline", "it has no pipeline", b["pipeline"]),
            "server.stop": lambda p, q, b: self.stop_asked(),
        }
        #: a stop asked over HTTP, owed once its answer is out (`stop_owed`)
        self._stop = {"owed": False, "asked": False}
        self._stop_lock = threading.Lock()
        declared = {r["id"] for r in self.routes}
        if declared != set(self.handlers):
            raise RuntimeError(
                "the contract and this server disagree about the routes: "
                f"declared, not served {sorted(declared - set(self.handlers))}; "
                f"served, not declared {sorted(set(self.handlers) - declared)}")
        self.table = [(r["method"], _pattern(r["path"]), r) for r in self.routes]
        # AN APPLICATION STARTS READY (PLAN-4 D6): the session its runner's
        # settings name opens now, and its models load at once - held until it
        # is closed or the server stops
        self._runner("start_session")

    def _say(self, line: str) -> None:
        try:
            self._log(line)
        except Exception:                                   # noqa: BLE001
            pass

    def close(self) -> None:
        """The server's last act, in the order that loses nothing:

          * ask the coordinator for nothing more - a wait for the card ends -
            and give up a load under way: the queue's worker is inside it, the
            queue waits for its worker, and a stop must never wait out a load
            nobody will use (found on the real card: 10.2 s, for a load of
            8.2);
          * stop the queue;
          * take the model off the card WHILE THE FOLDER IS STILL BOUND - which
            gives the card and its port back - so that its unload is on the
            folder's record, where the next start looks for what a stopped
            server left running (found on the real card: the folder went
            first, its record said the load had left, and an unload that
            failed would have been written nowhere);
          * put the folder down;
          * give back whatever else is held of the coordinator's."""
        if self.coordinator is not None:
            self.coordinator.stop_asking()
        self._runner("stopping", "the server is stopping")
        with self._bind_lock:
            self.queue.close()
            self.queue = Q.Queue(None, start=False, actor=self.actor)
            self._runner("close")
            self._runner("on_unbind")
        if self.coordinator is not None:
            self.coordinator.close()

    def _wants(self, model) -> bool:
        """Does a job the queue can run next name *model*? (S17: jobs on
        different models run at once, so any such job, not one.)"""
        return self.queue.wants(model)

    def _work_pending(self) -> bool:
        """Is a job of the bound folder unfinished - queued, or taken by the
        worker and not yet landed - work the queue's own pipeline keeps the
        card for? A job whose request is still being built has no generation
        yet and is work all the same; a job waiting on another's result is the
        same work. A HELD job (PLAN-4 D16) is not: it waits on a person, who
        may read it for as long as they like, and the card is not kept from
        everyone else meanwhile - sent, it asks for the card as any job does.
        Raises when nothing is bound or the store cannot be read, and the
        runner hears no work owed."""
        return bool(self.queue.unfinished(held=False))

    def _pipeline(self, what: str, none: str, *args, **kw):
        """A pipeline route, answered by the runner - or refused `no_runner`
        when there is none behind the backend (none, echo)."""
        fn = getattr(self.backend, what, None)
        if fn is None:
            return 409, {"error": f"no model runner behind this server: {none}",
                         "reason": "no_runner"}
        return fn(*args, **kw)

    def _runner(self, what: str, *args):
        """Tell the runner behind the backend, if there is one. It must never
        stop the server doing what it was asked."""
        fn = getattr(self.backend, what, None)
        if fn is None:
            return None
        try:
            return fn(*args)
        except Exception as e:                              # noqa: BLE001
            self._say(f"the runner could not {what}: {e}")
            return None

    # ------------------------------------------------------------ the folder

    def workspace(self):
        f = self.queue.folder
        return 200, {"loaded": f is not None, "workspace": str(f) if f else None}

    def _owed(self, q):
        """The first job this folder still owes an answer for, or None. A store
        that cannot be read cannot prove it owes anything, and a server stuck
        in a folder whose store is broken could never leave it."""
        try:
            owed = q.unfinished()
        except Q.StoreUnreadable as e:
            self._say(f"the job store could not be read ({e}); the folder is let go "
                      "without checking it for unfinished work")
            return None
        except Q.Refused:
            return None
        return owed[0] if owed else None

    @staticmethod
    def _busy(job):
        return 409, {"error": f"a job is unfinished: {job['name']}", "reason": "busy",
                     "job": job["id"]}

    def bind(self, body):
        """Point the server at a folder. On any refusal it stays where it was."""
        raw = body.get("path")
        if raw is None or not str(raw).strip():
            return 400, {"error": "no folder given", "reason": "no_path"}
        try:
            folder = Path(str(raw).strip()).expanduser().resolve()
        except (OSError, RuntimeError) as e:
            return 400, {"error": f"cannot use that folder: {e}", "reason": "unusable_folder"}
        if not folder.exists():
            return 400, {"error": "cannot use that folder: it does not exist",
                         "reason": "unusable_folder"}
        if not folder.is_dir():
            return 400, {"error": "cannot use that folder: it is not a folder",
                         "reason": "unusable_folder"}
        with self._bind_lock:
            cur = self.queue
            if cur.folder is not None and Path(cur.folder) == folder:
                return 200, {"loaded": True, "workspace": str(folder)}
            owed = self._owed(cur)
            if owed:
                return self._busy(owed)
            try:
                new = Q.Queue(folder, self.hooks, self.backend, url=self.url,
                              log=self._log, actor=self.actor, **self.queue_config)
            except Q.FolderInUse as e:
                return e.code, e.answer()
            except OSError as e:
                return 400, {"error": f"cannot use that folder: {e}",
                             "reason": "unusable_folder"}
            cur.close()
            self.queue = new
            self._runner("on_bind", folder)
        self._say(f"bound to {folder}")
        return 200, {"loaded": True, "workspace": str(folder)}

    def unload(self):
        with self._bind_lock:
            cur = self.queue
            if cur.folder is None:
                return 200, {"loaded": False, "was": None}
            owed = self._owed(cur)
            if owed:
                return self._busy(owed)
            was = str(cur.folder)
            cur.close()
            self.queue = Q.Queue(None, start=False, actor=self.actor)
            self._runner("on_unbind")
        self._say(f"released {was}")
        return 200, {"loaded": False, "was": was}

    # ------------------------------------------------------------ the health

    def health(self):
        h = self.queue.health()
        h["contract_version"] = self.contract_version
        card = self._runner("card_status")
        if card is None and self.coordinator is not None:
            # a coordinator, and no runner holding the card through it
            card = {"coordinator": self.coordinator.word(), "holding": False,
                    "waiting_since": None}
        h["card"] = card or {"coordinator": "none", "holding": h["queue"]["running"] > 0,
                             "waiting_since": None}
        # the runner's own answer, verbatim - null when no runner is behind
        # the backend (none, echo); and the pipeline holding the card, the same
        h["runner"] = self._runner("status")
        h["pipeline"] = self._runner("pipeline_status")
        return 200, h

    # ------------------------------------------------ the kept configuration

    NO_WORKSPACE = {"error": "no folder is bound: choose one first", "reason": "no_workspace"}

    def config_get(self):
        folder = self.queue.folder
        if folder is None:
            return 409, dict(self.NO_WORKSPACE)
        try:
            return 200, configs.current(folder)
        except configs.ConfigUnreadable as e:
            return 503, {"error": str(e), "reason": "config_unreadable"}

    def config_keep(self, body):
        """Kept under the binding: the folder cannot be let go between the
        check and the write."""
        with self._bind_lock:
            folder = self.queue.folder
            if folder is None:
                return 409, dict(self.NO_WORKSPACE)
            try:
                return 200, configs.keep(folder, body.get("config"))
            except configs.ConfigTooLarge as e:
                return 400, {"error": str(e), "reason": "too_large"}
            except OSError as e:
                return 503, {"error": f"the configuration could not be kept: {e}",
                             "reason": "config_unwritable"}

    # ------------------------------------------------------------ the models

    def models(self):
        """Every model the runner's settings name. No runner, no models. Asked
        of the runner directly, never through `_runner`: a runner that fails
        here is a bug, never 'no runner'."""
        ask = getattr(self.backend, "models", None)
        if ask is None:
            return 200, {"runner": None, "models_dir": None, "loads_from": None,
                         "slow_answer": None, "models": []}
        return 200, ask()

    def read_speed(self, key: str):
        ask = getattr(self.backend, "read_speed", None)
        if ask is None:
            return 409, {"error": "no model runner behind this server: it has no models to "
                                  "measure", "reason": "no_runner"}
        return ask(key)

    def load_model(self, key: str, query: dict):
        """A model loaded and kept in the application's pipeline, answered
        once it is up - or failed - within `?wait_s`. A malformed wait is
        refused before anything is asked of the runner."""
        wait, refused = self._wait_s(query)
        if refused is not None:
            return refused
        ask = getattr(self.backend, "load_model", None)
        if ask is None:
            return 409, {"error": "no model runner behind this server: it has no models to "
                                  "load", "reason": "no_runner"}
        return ask(key, wait)

    def unload_model(self, key: str):
        ask = getattr(self.backend, "unload_model", None)
        if ask is None:
            return 409, {"error": "no model runner behind this server: it has no models to "
                                  "unload", "reason": "no_runner"}
        return ask(key)

    def open_pipeline(self, b, query: dict):
        """The pipeline opened - answered once its models are up, or failed,
        within `?wait_s`. A malformed wait is refused before anything is asked
        of the runner."""
        wait, refused = self._wait_s(query)
        if refused is not None:
            return refused
        # S18: `if_no_room` passed on only when named - a runner that says
        # nothing of scheduled work is asked as it always was
        own = {"if_no_room": b["if_no_room"]} if b.get("if_no_room") is not None else {}
        return self._pipeline("open_pipeline", "it has no card to hold",
                              models=b.get("models") or [], klass=b.get("class"),
                              label=b.get("label"), keep_s=b.get("keep_s"), wait_s=wait, **own)

    @staticmethod
    def _wait_s(query: dict):
        """`?wait_s=N`, as every route that waits reads it: -> (seconds, None),
        0 when not given - or (None, the `bad_wait` answer)."""
        said = (query.get("wait_s") or [None])[0]
        if said is None:
            return 0.0, None
        if not WAIT_S.fullmatch(said) or float(said) > RESULT_WAIT_MAX_S:
            return None, (400, {"error": f"wait_s is a number of seconds from 0 to "
                                         f"{RESULT_WAIT_MAX_S}, not "
                                         f"{json.dumps(said, ensure_ascii=False)}",
                                "reason": "bad_wait"})
        return float(said), None

    # -------------------------------------------------------------- the jobs

    def _job(self, fn, creates: bool = False):
        """A queue call, its refusals turned into answers. A call that creates
        jobs holds the binding, so a folder cannot be let go between the busy
        check and a new job arriving in it."""
        try:
            if creates:
                with self._bind_lock:
                    return 200, fn(self.queue)
            return 200, fn(self.queue)
        except Q.Refused as e:
            return e.code, e.answer()
        except Q.StoreUnreadable as e:
            return 503, {"error": str(e), "reason": "store_unreadable"}

    def submit(self, b):
        return self._job(lambda s: s.submit(
            b.get("kind"), b.get("input"), b.get("gen"), name=b.get("name"),
            priority=b.get("priority", "interactive"), after=b.get("after") or (),
            group=b.get("group"), group_size=b.get("group_size"), scope=b.get("scope"),
            client=b.get("client"), correlation_id=b.get("correlation_id"),
            card_patience_s=b.get("card_patience_s"), check=bool(b.get("check")),
            not_before=b.get("not_before"), if_no_room=b.get("if_no_room")),
            creates=True)

    def result(self, job_id: str, query: dict):
        """A job's result. `?wait_s=N` holds the answer until the job ends or
        N seconds pass, then answers as without it. N is refused before
        anything is asked of the queue: a question that is malformed is
        malformed whether or not a folder is bound."""
        wait, refused = self._wait_s(query)
        if refused is not None:
            return refused
        return self._job(lambda s: s.result(job_id, wait_s=wait))

    def jobs(self, query):
        """The folder's jobs, narrowed by `?scope=` and `?client=`. Each is
        given once or not at all - a list narrowed by one of two values would
        be a guess - and a client is an actor id: a malformed one once listed
        nothing, silently. Refused before anything is asked of the queue, as
        a malformed `wait_s` is. A value left blank counts as not given (the
        one `parse_qs` every route shares drops it)."""
        for key, reason in (("client", "bad_client"), ("scope", "bad_scope")):
            given = query.get(key) or []
            if len(given) > 1:
                return 400, {"error": f"{key} is given once, not {len(given)} times",
                             "reason": reason}
        client = (query.get("client") or [None])[0]
        if client is not None and not ids.matches("actor", client):
            return 400, {"error": f"client is an actor id, not "
                                  f"{json.dumps(client, ensure_ascii=False)}",
                         "reason": "bad_client"}
        scope = (query.get("scope") or [None])[0]
        status, out = self._job(lambda s: s.jobs(scope=scope, client=client))
        return (status, {"jobs": out}) if status == 200 else (status, out)

    def prune(self, b):
        return self._job(lambda s: s.prune(statuses=b.get("statuses"), keep=b.get("keep", 0),
                                           scope=b.get("scope")))

    # ----------------------------------------------------------- the routing

    def route(self, method: str, path: str):
        """-> (route, path params) | (None, 404 or 405)."""
        allowed = False
        for m, pattern, route in self.table:
            hit = pattern.fullmatch(path)
            if hit:
                if m == method:
                    return route, {k: unquote(v) for k, v in hit.groupdict().items()}
                allowed = True
        return None, (405 if allowed else 404)

    def refuse_body(self, route, body):
        """The first way *body* does not match its route's request shape, or
        None - leaving a missing field to the route's own refusal for it."""
        return V.refuse_body(route, body, self._shapes, OWN_REFUSALS.get(route["id"], ()))

    def stop_asked(self):
        """A program asked this server to stop (`server.stop`): answered at
        once, and the stop owed until the answer is out - the handler that
        wrote it begins it (`Handler._serve`, `stop_owed`). Asked again while
        it stops, it answers the same, and nothing is begun twice."""
        with self._stop_lock:
            if not self._stop["asked"]:
                self._stop.update(owed=True, asked=True)
        self._say("a stop was asked over HTTP: the server stops once its answer is out")
        return 202, {"stopping": True, "actor": self.actor}

    def stop_owed(self) -> bool:
        """Is a stop owed now? True once, to the one that begins it."""
        with self._stop_lock:
            owed, self._stop["owed"] = self._stop["owed"], False
        return owed

    def answer(self, method: str, raw_path: str, body: dict, page: bool = False):
        """One request, start to end: -> (status, body). Never raises.
        `page`: the request carries an Origin - a page sent it."""
        path, _, query = raw_path.partition("?")
        try:
            route, params = self.route(method, path)
            if route is None:
                if params == 405:
                    return 405, {"error": f"{path} does not take {method}",
                                 "reason": "method_not_allowed"}
                return 404, {"error": f"no route {path}", "reason": "no_route"}
            if page and route["id"] in NOT_FROM_A_PAGE:
                return 403, {"error": "a page may not stop this server: it is stopped by a "
                                      "program on this machine, which sends no Origin, or at "
                                      "its console", "reason": "from_a_page"}
            if route["request"] is not None:
                wrong = self.refuse_body(route, body)
                if wrong:
                    return 400, {"error": wrong, "reason": V.BAD_REQUEST}
            return self.handlers[route["id"]](params, parse_qs(query), body)
        except Exception as e:                              # noqa: BLE001
            self._say(f"THE SERVER FAILED on {method} {raw_path}: {type(e).__name__}: {e}\n"
                      + traceback.format_exc())
            return 500, {"error": f"the server failed: {type(e).__name__}: {e}",
                         "reason": "internal"}


def _pattern(template: str):
    """`/api/jobs/{id}/claim` -> a regex with a named group per parameter."""
    parts = re.split(r"\{([a-z_]+)\}", template)
    out = "".join(re.escape(p) if i % 2 == 0 else f"(?P<{p}>[^/]+)"
                  for i, p in enumerate(parts))
    return re.compile(out)


def origin_of(address) -> str:
    """*address* as a browser writes a page's address in the Origin header it
    puts on a request: `scheme://host[:port]`, lower case, no default port, no
    path - so a page named at the start is the header its browser sends.
    ValueError, saying why, for anything that is not a page's address: `null`
    and `*` included - either would let in pages of anybody's."""
    text = str(address)
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as e:
        raise ValueError(f"{text!r} is not a page's address: {e}") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{text!r} is not a page's address: one is written "
                         "http://<host>:<port>, as the browser shows it")
    if (parts.path not in ("", "/") or parts.query or parts.fragment
            or parts.username is not None or parts.password is not None):
        raise ValueError(f"{text!r} is not a page's address: that is its scheme, host "
                         "and port alone - no path, query or name")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    default = {"http": 80, "https": 443}[parts.scheme]
    return f"{parts.scheme}://{host}" + ("" if port in (None, default) else f":{port}")


#: what an allowed page's browser is told the page may send: the methods the
#: routes take, and the one header the client sets - a JSON body's type
ALLOW_METHODS = "GET, POST"
ALLOW_HEADERS = "Content-Type"

#: the names every server on this machine answers to - as a Host header
#: writes them - besides the address it listens on and the host of each page
#: it was started to allow
OWN_NAMES = ("127.0.0.1", "localhost", "[::1]")
#: a Host header: a name - an IPv6 address in its brackets - and a port
HOST = re.compile(r"(\[[0-9A-Fa-f:.]+\]|[^\s:@/?#\[\]]+)(?::[0-9]+)?")
#: the most of a request's body the server reads, unless `--max-body` says
#: otherwise. Its largest real body is a text request - a model's whole
#: context, 128 k tokens, is about half a MiB of text; a JSON schema or a
#: grammar some KiB more - and 16 MiB is thirty times that, while a body of
#: gigabytes, sent by mistake or on purpose, is never read. No audio comes in
#: a body: a whisper request names a WAV file on this machine.
MAX_BODY = 16 * 1024 * 1024
#: how long a connection whose body was never read is kept once its answer is
#: out, before it is closed - so an asker still sending hears the answer, not
#: a reset. Measured on this machine (urllib, bodies of 1 KiB to 64 MiB over a
#: cap of 1 KiB, 20 of each): closed at once, not one body of 8 MiB or more was
#: answered, and half of those of 1 MiB; kept 0.02 s, every one was. A quarter
#: of a second is that twelve times over.
UNREAD_GRACE_S = 0.25
#: how long a request has to arrive whole - its line, its headers and its body
#: - from the moment the server starts waiting for it, unless `--read-timeout`
#: says otherwise; one that has not is never answered, and its connection is
#: closed. Every asker here sends a request whole, at once, over this
#: machine's loopback: measured, the largest body the server reads (16 MiB)
#: arrived and was answered in 24 to 49 ms (urllib, 20 times) - ten seconds is
#: two hundred times that. And a page keeps its connection open between its
#: asks, the slowest of which comes every 5 s (the Models feed): ten seconds
#: closes no connection a page that is shown still uses.
READ_TIMEOUT_S = 10.0


class _Reads(io.RawIOBase):
    """A connection's reads, each allowed only what is left of the time its
    request has to arrive whole: `until`, on the monotonic clock, set when the
    server starts waiting for the request (`start`). None left: TimeoutError,
    and http.server closes the connection. The reads alone: the socket is
    blocking again after each, so an answer is written as it always was.
    `came` counts the bytes since the wait began - a request cut off part way
    has some; a connection that never sent a byte has none."""

    def __init__(self, sock) -> None:
        super().__init__()
        self._sock = sock
        self.until = None
        self.came = 0
        self.expired = False

    def readable(self) -> bool:
        return True

    def start(self, seconds: float) -> None:
        self.until, self.came, self.expired = time.monotonic() + seconds, 0, False

    def readinto(self, buf) -> int:
        if self.until is not None:
            left = self.until - time.monotonic()
            if left <= 0:
                self.expired = True
                raise TimeoutError("the request did not arrive whole within the read timeout")
            self._sock.settimeout(left)
        try:
            n = self._sock.recv_into(buf)
        except TimeoutError:
            self.expired = True
            raise
        finally:
            try:
                self._sock.settimeout(None)
            except OSError:
                pass                                    # closed: nothing more is read of it
        self.came += n
        return n


def host_name(host: str):
    """The name a Host header names, in lower case and without its port - an
    IPv6 address in its brackets - or None: a Host that is not a name and a
    port is not guessed around."""
    hit = HOST.fullmatch(str(host).strip())
    return hit.group(1).lower() if hit else None


def as_named(host: str) -> str:
    """A host - an address listened on, a page's host - as a Host header names
    it: lower case, an IPv6 address in brackets."""
    host = str(host).lower().strip("[]")
    return f"[{host}]" if ":" in host else host


def own_names(host: str, allowed) -> frozenset:
    """Every name a server answers to: every server's own (OWN_NAMES), the
    address it listens on, and the host of each page it was started to
    allow."""
    return frozenset(OWN_NAMES) | {as_named(host)} | {as_named(urlsplit(o).hostname)
                                                     for o in allowed}


def _bytes(text: str) -> int:
    """`--max-body`: a whole number of bytes, 1 or more - or refused at the
    start, saying why."""
    try:
        n = int(text)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number of bytes, 1 or more")
    return n


def _seconds(text: str) -> float:
    """`--read-timeout`: a number of seconds above 0 - or refused at the
    start, saying why. Never a bound that is none: not infinity, not nan."""
    try:
        s = float(text)
    except ValueError:
        s = 0.0
    if not math.isfinite(s) or s <= 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of seconds above 0")
    return s


class Listener(ThreadingHTTPServer):
    """The server's listening socket - its port its alone. http.server binds
    SO_REUSEADDR, and on Windows a second program's socket that sets it too
    is let bind a port the first one holds. Measured against this server,
    before: a helper's bind beside it succeeded; and a server started on a
    port a helper already listened on started, said it was listening, and of
    20 requests heard none - the helper took them all. On Windows it binds
    SO_EXCLUSIVEADDRUSE instead, as the llama-server stand-in does: a port
    somebody holds refuses the bind, and nobody's bind shares it after.
    Elsewhere SO_REUSEADDR stays, as http.server sets it."""
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self) -> None:
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address) -> None:
        """A connection its asker reset or dropped mid-request - its process
        ended while it asked - is said in one line of the server's log, never
        a traceback (R-B5). Anything else is said as http.server says it."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            app = getattr(self, "app", None)
            line = (f"a connection from {client_address[0]}:{client_address[1]} was reset by "
                    f"its asker mid-request ({type(exc).__name__}): nothing was answered")
            if app is not None:
                app._say(line)
            else:
                print(line, flush=True)
            return
        super().handle_error(request, client_address)


class CannotListen(OSError):
    """The server's port could not be had: its address, and the system's
    words. Every other error at the start says what it was, never this."""


def listen(port: int, host: str = "127.0.0.1") -> Listener:
    """The server's port, had - its alone (`Listener`) - and not yet served:
    CannotListen when somebody holds it, or it cannot be had."""
    try:
        return Listener((host, port), Handler)
    except OSError as e:
        raise CannotListen(f"cannot listen on {host}:{port}: {e}") from None


class Handler(BaseHTTPRequestHandler):
    """HTTP for the app. Before any route, on every request whatever its
    method (the contract's `origins/server.json`), these are checked in this
    order, and the first that refuses answers (`_refused`):

      * THE NAME it was sent to: a Host that is not one of the server's own
        names is refused 403, with or without an Origin - a website whose
        name was made to point at this machine (DNS rebinding) sends that
        name as its Host and as its Origin, and would otherwise pass for the
        server's own page;
      * A PAGE FROM ANOTHER ADDRESS: a request whose Origin is neither the
        server's own nor a page it was started to allow is refused 403,
        whatever it asks - so a web page open in the same browser can never
        act on the server, not even with a request its browser sends without
        asking first (the server reads any body as JSON);
      * ITS BODY: one sent in chunks (a Transfer-Encoding) is refused 411, and
        one over the cap 413 - neither read at all.

    Then an asker that said `Expect: 100-continue` is told to send its body -
    only now, never before the checks (http.server told it first) - a body
    no route reads - a GET's - is read and set aside, and a method no route
    takes is answered 501, as http.server answers one. All of it in one place
    (`parse_request`), before any method of the handler runs: a method added
    here cannot skip it. A request must arrive whole within the read timeout
    (`handle_one_request`). A request with no Origin, sent to one of the
    server's own names, and one from the server's own page, are answered
    exactly as before the checks."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):          # each answer is logged once, below
        pass

    def setup(self) -> None:
        """The connection, its reads bounded in time (`_Reads`) in place of
        http.server's own reader of the same socket."""
        super().setup()
        self.rfile.close()
        self._reads = _Reads(self.connection)
        self.rfile = io.BufferedReader(self._reads)

    def handle_one_request(self) -> None:
        """One request, which must arrive whole - its line, its headers and
        its body - within the read timeout of the server starting to wait for
        it: of the connection being made, or of the answer before it on a
        connection kept open. One that has not is never answered: its
        connection is closed (http.server closes one whose read timed out).
        A request cut off part way is said in the log; a connection that
        never sent a byte is closed without a word - a browser's, kept open
        and no longer used."""
        self.command = self.path = None
        self._reads.start(self.server.read_timeout_s)
        super().handle_one_request()
        if self._reads.expired and self._reads.came:
            what = f"{self.command} {self.path}" if self.command else "a request"
            self.server.app._say(f"{what} -> its connection closed: not read whole within "
                                 f"{self.server.read_timeout_s:g}s ({self._reads.came} bytes "
                                 f"came)")

    def parse_request(self) -> bool:
        """http.server's own reading of the request line and headers - then,
        on every request whatever its method, the checks before any route
        (`_refused`); a body no route reads, read and set aside; and a method
        no route takes, answered 501 (`_not_taken`). -> True only for a
        request a method of this handler answers; False when it is answered
        already - http.server's own 400 for a request it cannot read, too."""
        self._expects = False                   # set while http.server reads the headers
        if not super().parse_request() or self._refused():
            return False
        if self._expects:
            self._continue()
        if self.command != "POST":
            self._set_aside()
        if not hasattr(self, f"do_{self.command}"):
            self._not_taken()
            return False
        return True

    def handle_expect_100(self) -> bool:
        """`Expect: 100-continue`: its asker sends the body only once it is told
        to - and http.server told it at once, before any check, so a request
        refused before any route was invited to send the body its refusal
        would never read. Only noted here: it is told once the checks let the
        request through (`_continue`); refused, it is answered without it,
        its body never read (`_unread`)."""
        self._expects = True
        return True

    def _continue(self) -> None:
        """The checks let the request through: an asker waiting to be told to
        send its body is told now, as http.server would have told it."""
        self.send_response_only(HTTPStatus.CONTINUE)
        self.end_headers()

    def _set_aside(self) -> None:
        """A body within the cap that no route reads, read and never used -
        so the next request on a connection kept open is read as itself,
        never the body taken for one."""
        n = self._length()
        if n:
            self.rfile.read(n)

    def _not_taken(self) -> None:
        """A method no route takes: 501, as http.server answers one - after
        the checks every request goes through, its body within the cap read
        and set aside, so an asker still sending hears it, not a reset."""
        self.send_error(HTTPStatus.NOT_IMPLEMENTED, f"Unsupported method ({self.command!r})")

    def _host_refused(self) -> bool:
        """Was the request sent to a name that is not the server's? One that
        names no Host at all was not: every browser names one."""
        host = self.headers.get("Host")
        return host is not None and host_name(host) not in self.server.own_names

    def _length(self) -> int:
        """The body's length, as its Content-Length says: a whole number of
        bytes, in digits. Anything else reads as no body - never as a length
        below 0, which `read()` would take for "until the connection closes"."""
        said = (self.headers.get("Content-Length") or "").strip()
        return int(said) if said.isascii() and said.isdigit() else 0

    def _from(self):
        """Which page sent the request, by the Origin header its browser put on
        it: None - no Origin: not a page, or the server's own asking a GET;
        "own" - the server's own page: the Origin is the address the request
        was sent to, `http://` and its Host, however that is written;
        "allowed" - a page the server was started to allow; "refused" - any
        other."""
        origin = self.headers.get("Origin")
        if origin is None:
            return None
        host = self.headers.get("Host")
        if host is not None and origin == f"http://{host}":
            return "own"
        return "allowed" if origin in self.server.allowed_origins else "refused"

    def _allow(self) -> None:
        """What an allowed page's browser must be told to hand the page the
        answer. Nothing for any other request: with no Origin, or from the
        server's own page, an answer is what it was before."""
        if self._from() == "allowed":
            self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
            self.send_header("Vary", "Origin")

    def _refused(self) -> bool:
        """Refused before any route - its Host, its Origin, its body, in that
        order: True when it was, and there is nothing more to do. A refused
        body within the cap is read and never used, so the connection stays
        in step; one in chunks or over the cap is never read (`_before`). A
        page's browser is told nothing but the refusal - no Access-Control
        header, unless the page is one the server was started to allow."""
        if self._host_refused():
            host = self.headers["Host"]
            return self._before(403, {
                "error": f"this server does not answer to {host}: it answers to its own names "
                         "- 127.0.0.1, localhost, [::1] and the address it listens on - and to "
                         "the host of each page it was started to allow (--allow-origin)",
                "reason": "host_not_allowed"}, f" for {host}")
        if self._from() == "refused":
            origin = self.headers["Origin"]
            return self._before(403, {
                "error": f"a page at {origin} may not use this server: it answers only its own "
                         "page and the pages it was started to allow (--allow-origin)",
                "reason": "origin_not_allowed"}, f" from {origin}")
        chunks = self.headers.get("Transfer-Encoding")
        if chunks is not None:
            # read by its Content-Length alone, a body in chunks would be read
            # as the next request on a connection kept open - so it is never
            # read, whatever the method, and whatever length it also names
            return self._before(411, {
                "error": f"a body sent with Transfer-Encoding: {chunks} is never read by this "
                         "server: send it whole, its length in Content-Length",
                "reason": "length_required"}, f" - Transfer-Encoding: {chunks}")
        n = self._length()
        if n > self.server.max_body:
            return self._before(413, {
                "error": f"a body of {n} bytes is more than this server reads: "
                         f"{self.server.max_body} at most (--max-body)",
                "reason": "too_large"}, f" - a body of {n} bytes")
        return False

    def _unread(self) -> bool:
        """Is its body one the server never reads: in chunks, over the cap - or
        one its asker holds back until it is told to continue, which a refusal
        never tells it?"""
        return (self.headers.get("Transfer-Encoding") is not None
                or self._length() > self.server.max_body
                or (self._expects and self._length() > 0))

    def _before(self, status: int, out: dict, said: str) -> bool:
        """An answer before any route. A body within the cap is read and set
        aside, so a connection the browser keeps open stays in step; one in
        chunks, over the cap, or held back by an asker waiting to be told to
        continue, is never read: the answer says `Connection:
        close` - so http.server closes the connection once this returns, and
        nothing the asker sent is ever read as a request - and it returns a
        moment after the answer is out (UNREAD_GRACE_S), reading nothing.
        Closed at once, with the body still coming, the asker is sent a reset
        in place of the answer."""
        t0 = time.perf_counter()
        unread = self._unread()
        if not unread:
            self._set_aside()
        self._json(status, out, t0, said, closing=unread)
        if unread:
            time.sleep(UNREAD_GRACE_S)
        return True

    def _json(self, status: int, out, t0: float, said: str = "", closing: bool = False) -> None:
        """One answer, and its line in the log. `closing`: the connection is
        closed after it, and the answer says so."""
        raw = json.dumps(out, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        if closing:
            self.send_header("Connection", "close")
        self._allow()
        self.end_headers()
        self.wfile.write(raw)
        reason = out.get("reason") if isinstance(out, dict) and status >= 400 else None
        corr = out.get("correlation_id") if isinstance(out, dict) else None
        self.server.app._say(f"{self.command} {self.path} -> {status}"
                             + (f" {reason}" if reason else "") + (f" {corr}" if corr else "")
                             + said + f" ({(time.perf_counter() - t0) * 1000:.0f} ms)")

    def _body(self) -> dict:
        """A body that is not a JSON object reads as an empty one, so the
        route's own refusal for the missing field answers it. It is never in
        chunks or over the cap: those were refused before any route."""
        n = self._length()
        try:
            b = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except (ValueError, UnicodeDecodeError):
            return {}
        return b if isinstance(b, dict) else {}

    def _serve(self, method: str) -> None:
        t0 = time.perf_counter()
        body = self._body() if method == "POST" else {}
        status, out = self.server.app.answer(method, self.path, body,
                                             page=self.headers.get("Origin") is not None)
        self._json(status, out, t0)
        if self.server.app.stop_owed():
            # asked to stop (`server.stop`), and its answer is out: the stop,
            # on a thread of its own - it waits for this server's loop, which
            # this request is not
            threading.Thread(target=_stop_asked, args=(self.server,), name="server-stop").start()

    def do_GET(self):
        if self.path in ("/", "/index.html") and self.server.client is not None:
            return self._page()
        self._serve("GET")

    def _page(self) -> None:
        """The client's page - read at every request, so a page rebuilt while
        the server runs is the one served. Only this one file: the folder is
        fixed at start, and nothing else in it is ever served."""
        try:
            raw = (self.server.client / "index.html").read_bytes()
        except OSError as e:
            raw = json.dumps({"error": f"the client's page cannot be read: {e}",
                              "reason": "internal"}).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self._allow()
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        self._serve("POST")

    def do_OPTIONS(self):
        """The preflight: what a browser asks before it sends a request a page
        could not send from a form - one with a JSON body, here. Answered for
        a page the server was started to allow; refused before any route, as
        every request is, for any other page; and with no Origin, or from the
        server's own page, what it always was - a method the server does not
        take, answered as http.server answers one."""
        if self._from() != "allowed":
            self._not_taken()
            return
        t0 = time.perf_counter()
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", ALLOW_METHODS)
        self.send_header("Access-Control-Allow-Headers", ALLOW_HEADERS)
        self.send_header("Content-Length", "0")
        self._allow()
        self.end_headers()
        self.server.app._say(f"OPTIONS {self.path} -> 204 for {self.headers['Origin']} "
                             f"({(time.perf_counter() - t0) * 1000:.0f} ms)")


def serve(port: int = 7072, host: str = "127.0.0.1", hooks=None, backend=None,
          log=None, actor=None, coordinator=None, client=None, allow_origins=(),
          max_body: int = MAX_BODY, read_timeout_s: float = READ_TIMEOUT_S, **queue_config):
    """Start serving; -> the HTTP server, with the app as `.app`. `client` is
    the folder of the client's built page, served at `/` - outside the
    contract, as the coordinator serves its dashboard. `allow_origins` names
    the pages, besides the server's own, that may use it: an application's
    page showing the client's widgets (`Handler`); the host of each is one of
    the server's own names, beside the address it listens on (`own_names`).
    `max_body` is the most of a request's body it reads, in bytes;
    `read_timeout_s` how long a request has to arrive whole. Each is read
    before a port is taken, so one that is not a page's address, a cap below
    1, or a timeout that is not a number of seconds above 0, stops the start.
    The port is its alone (`Listener`): one somebody holds raises CannotListen
    (an OSError, naming it). Anything else that stops the start raises as
    itself, and the port it had is let go."""
    allowed = frozenset(origin_of(o) for o in allow_origins)
    if isinstance(max_body, bool) or not isinstance(max_body, int) or max_body < 1:
        raise ValueError(f"max_body is a whole number of bytes, 1 or more, not {max_body!r}")
    if isinstance(read_timeout_s, bool) or not isinstance(read_timeout_s, (int, float)) \
            or not math.isfinite(read_timeout_s) or read_timeout_s <= 0:
        raise ValueError(f"read_timeout_s is a number of seconds above 0, not {read_timeout_s!r}")
    httpd = listen(port, host)
    try:
        httpd.daemon_threads = True
        httpd.client = Path(client) if client else None
        httpd.allowed_origins = allowed
        httpd.own_names = own_names(host, allowed)
        httpd.max_body = max_body
        httpd.read_timeout_s = float(read_timeout_s)
        url = f"http://{host}:{httpd.server_address[1]}"
        httpd.app = Server(hooks=hooks, backend=backend, url=url, log=log, actor=actor,
                           coordinator=coordinator, **queue_config)
        #: set once its stop is done - by `stop`, whoever asked for it
        httpd.stopped = threading.Event()
        httpd.stop_lock = threading.Lock()
    except BaseException:
        httpd.server_close()
        raise
    threading.Thread(target=httpd.serve_forever, daemon=True, name="server-http").start()
    return httpd


def stop(httpd) -> None:
    """Put the folder down, then stop answering. Once: a stop asked over HTTP
    and one at the console (or a row's) are one stop, whichever comes first,
    the other waiting for it to be done."""
    with httpd.stop_lock:
        if httpd.stopped.is_set():
            return
        httpd.app.close()
        httpd.shutdown()
        httpd.server_close()
        httpd.stopped.set()


def _stop_asked(httpd) -> None:
    """The stop a program asked for over HTTP (`server.stop`), once its answer
    is out; said when it is done."""
    stop(httpd)
    httpd.app._say("stopped: asked over HTTP")


def build_backend(kind: str, runner_settings=None, reclaim: bool = False, log=None,
                  coordinator=None, vram=None, name=None):
    """The model behind the queue. `mock`, `real` and `llamacpp` are the
    runner, with the models its settings file names: the stand-in model,
    answering in each model's engine's shape; each model on its own engine
    (PLAN-4 D10); or every model on llama.cpp - `real`'s name from before
    engines were named. With
    a `coordinator` client - named on the command line - the card is its
    rental, had by ticket with each job's own terms, and a load's port its
    grant. Without one, a real card's sessions look at the machine's lock
    (PLAN-4 D5): free, the server takes it; held by a coordinator, each
    session goes through that coordinator, found at the address its note
    gives, as `name`; held by an application run on its own, it waits. Every
    ticket declares what the runner estimates its model needs. `vram` answers
    "how much of the card is free" in place of asking the card - a suite's,
    so that no row ever touches it; never set by a start."""
    if kind == "none":
        return None
    if kind == "echo":
        return EchoBackend()
    from runner import needs as N
    from runner.adapter import RunnerBackend
    from runner.cardlock import MachineCard
    from runner.config import Config
    from runner.manager import ModelManager
    cfg = Config.load(runner_settings) if runner_settings else Config.default()
    if cfg.backend != kind:
        (log or print)(f"the runner's settings say backend {cfg.backend!r}; "
                       f"--backend {kind} is what runs")
        cfg.data["runner"]["backend"] = kind
    if kind == "llamacpp":
        # every model on llama.cpp: the settings' own check was made under
        # the backend they name, and the command line's is what runs
        other = next((k for k, s in sorted(cfg.models.items()) if s.engine != "llamacpp"), None)
        if other is not None:
            raise ValueError(f"--backend llamacpp runs every model on llama.cpp, and "
                             f"[models.{other}] runs on {cfg.models[other].engine}: say "
                             f"--backend real, to run each model on its own engine")
    card = ports = None
    patience = float(cfg.runner["card_patience_s"])
    made = []

    def needs(key):
        # the manager's: measured, once the model has three measured loads
        # (PLAN-4 D2, D12) - else the runner's estimate for its engine
        return (made[0].need(key) if made else N.need_mib(cfg, key, cfg.engine(key)))[0]
    # each model is declared on its ticket with its own engine (PLAN-4 D10) -
    # the stand-in's, `mock`, under the stand-in
    if coordinator is not None:
        card = K.CoordinatorCard(coordinator, patience_s=patience, engine=cfg.backend,
                                 needs=needs, engines=cfg.engine)
        ports = K.CoordinatorPorts(coordinator)
    elif cfg.backend != "mock":
        # the real card, whatever each model's engine: the machine's lock
        def through(url, holder):
            client = K.CoordinatorClient(url, holder, name or "llm-bootstrap-server", log=log)
            return K.CoordinatorCard(client, patience_s=patience, engine=cfg.backend, needs=needs,
                                     engines=cfg.engine)
        from runner.manager import lock_watch
        card = MachineCard(cfg.runner.get("card_lock") or None, patience_s=patience,
                           through=through, **lock_watch(cfg))
    # a real card's memory is read for the statistics (D12): what a load
    # took, at ready and after its first answer, and an unload's memory back
    made.append(ModelManager(cfg, card=card, ports=ports, read_card=cfg.backend != "mock",
                             **({"vram": vram} if vram is not None else {})))
    return RunnerBackend(made[0], reclaim=reclaim, log=log)


def _page_address(text: str) -> str:
    """`--allow-origin`, read as the browser will send it - or refused at the
    start, saying why, never met at the first request."""
    try:
        return origin_of(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def parse_args(argv=None):
    """The command line, checked. Refuses what nothing would read."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=None,
                    help=f"default {DEFAULT_PORT}; with --coordinator and no --port, the "
                         "coordinator hands the server its port")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--workspace", default=None, help="bind this folder at start")
    ap.add_argument("--backend", choices=("none", "echo", "mock", "real", "llamacpp"),
                    default="none",
                    help="none: every generation fails saying so; echo: a stand-in that "
                         "answers each request back; mock / real / llamacpp: the runner, "
                         "with the stand-in model, each model on its own engine, or every "
                         "model on llama.cpp")
    ap.add_argument("--runner", default=None,
                    help="the runner's settings file (mock, real, llamacpp): its models")
    ap.add_argument("--reclaim", action="store_true",
                    help="take back what a stopped server of a bound folder left "
                         "running - only a process provably this runner's own")
    ap.add_argument("--coordinator", default=None,
                    help="the coordinator's URL, named: the card's rentals, the loads' ports "
                         "and this server's own come from it. Without it, a real card's "
                         "sessions find the coordinator through the machine's lock")
    ap.add_argument("--name", default=None,
                    help="what this server calls itself to the coordinator")
    ap.add_argument("--client", default=None,
                    help="the client's folder: its built page is served at /")
    ap.add_argument("--allow-origin", action="append", default=None, type=_page_address,
                    metavar="<page address>",
                    help="a page, besides the server's own, that may use it - an "
                         "application's page showing the client's widgets - written as the "
                         "browser shows its address: http://127.0.0.1:7867. Repeatable. A "
                         "page from any other address is refused before any route")
    ap.add_argument("--max-body", type=_bytes, default=MAX_BODY, metavar="<bytes>",
                    help=f"the most of a request's body the server reads, in bytes - "
                         f"{MAX_BODY} (16 MiB) unless given; a larger body is refused "
                         f"(413 too_large) and never read")
    ap.add_argument("--read-timeout", type=_seconds, default=READ_TIMEOUT_S, metavar="<seconds>",
                    help=f"how long a request has to arrive whole - its line, its headers and "
                         f"its body - {READ_TIMEOUT_S:g}s unless given; one that has not is "
                         f"never answered, and its connection is closed")
    a = ap.parse_args(argv)
    if a.backend in ("none", "echo") and (a.runner or a.reclaim):
        # a setting nothing reads must not look like one that took effect
        ap.error("--runner and --reclaim are read only by --backend mock|real|llamacpp")
    if a.name is not None and not a.coordinator and a.backend not in ("real", "llamacpp"):
        ap.error("--name is read only with --coordinator, or with --backend real|llamacpp - "
                 "whose sessions find a coordinator through the machine's lock")
    if a.client is not None and not (Path(a.client) / "index.html").is_file():
        ap.error(f"--client {a.client}: there is no built page (index.html) in it - "
                 "run build.py there")
    return a


#: the port a server listens on when nothing else decides it
DEFAULT_PORT = 7072
#: how long a start asks the coordinator for the server's own port
OWN_PORT_PATIENCE_S = 10.0


class CannotStart(Exception):
    """The server cannot start, for a reason a person must read."""


def own_port(client, patience_s=None, retry_s: float = 0.5) -> int:
    """The server's own port, from the coordinator's registry (purpose
    `server`), kept alive by the client's heartbeat and given back when it
    closes. Asked for until the patience runs out - then the start stops,
    saying so. NEVER A GUESS: a port nobody granted could be one the registry
    is about to hand to somebody else."""
    ports = K.CoordinatorPorts(client, purpose="server")
    patience = OWN_PORT_PATIENCE_S if patience_s is None else float(patience_s)
    deadline = time.monotonic() + patience
    while True:
        try:
            return ports.take("server")
        except K.PortRefused as e:
            if time.monotonic() >= deadline:
                raise CannotStart(f"no port from the coordinator at {client.base} "
                                  f"after {patience:g}s: {e}") from None
            time.sleep(retry_s)


def start_server(a, log=None):
    """Everything a start does, in the order it must: the actor first; then the
    coordinator's client, and the server's own port from it; the backend; the
    HTTP server; the folder. -> the HTTP server. Raises CannotStart."""
    actor = ids.mint_actor("srv")                   # before anything is asked for
    client = None
    if a.coordinator:
        client = K.CoordinatorClient(a.coordinator, actor, a.name or "llm-bootstrap-server",
                                     log=log)
        if not client.probe():
            (log or print)(f"the coordinator at {a.coordinator} does not answer yet; "
                           "the server will keep asking")
    try:
        port = a.port if a.port is not None else (own_port(client) if client else DEFAULT_PORT)
        backend = build_backend(a.backend, a.runner, a.reclaim, log=log, coordinator=client,
                                name=a.name)
    except (OSError, ValueError, CannotStart) as e:
        if client is not None:
            client.close()
        raise CannotStart(str(e)) from None
    try:
        httpd = serve(port, a.host, backend=backend, log=log, actor=actor, coordinator=client,
                      client=a.client, allow_origins=a.allow_origin or (), max_body=a.max_body,
                      read_timeout_s=a.read_timeout)
    except OSError as e:
        # said as what it was: a port somebody holds - refused, never shared
        # (`Listener`) - is CannotListen, naming it; anything else, itself
        if client is not None:
            client.close()
        raise CannotStart(str(e)) from None
    if a.workspace:
        status, out = httpd.app.bind({"path": a.workspace})
        if status != 200:
            stop(httpd)
            raise CannotStart(f"cannot bind {a.workspace}: {out['error']}")
    return httpd


def main(argv=None) -> int:
    a = parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        httpd = start_server(a)
    except CannotStart as e:
        print(f"cannot start: {e}", flush=True)
        return 2
    app = httpd.app
    print(f"listening on {app.url} as {app.actor}", flush=True)
    if httpd.allowed_origins:
        print(f"pages at {', '.join(sorted(httpd.allowed_origins))} may use it", flush=True)
    try:
        # until it is stopped: at the console, or asked over HTTP (`server.stop`)
        while not httpd.stopped.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        stop(httpd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
