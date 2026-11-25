"""The runner on the REAL card: a real llama-server, a real model, real VRAM.

    set LLM_BOOTSTRAP_REAL_CARD=<a runner settings file: llama_server, models_dir, a model>
    py -3.12 test_real_card.py                                # every row, one at a time
    py -3.12 test_real_card.py FirstLoadRows                  # one class, or one row
    py -3.12 test_real_card.py --repeat 5 CancelRows          # a race is a chance: again

WITHOUT THAT VARIABLE EVERY ROW SKIPS. This suite takes the card, and nothing
takes the card unasked: it is not one of the part's gates, and no sweep runs
it. `LLM_BOOTSTRAP_REAL_MODEL` picks one of the file's models (default: the
first by name). Each row's evidence - its settings, the server's log, the
runner's record, llama-server's log and the row's timeline - is kept under
`LLM_BOOTSTRAP_REAL_OUT` (default: a new folder in the system's temp).

EVERY ROW IS WATCHED FROM OUTSIDE (`cardwatch.py`): the card's memory and every
llama-server on the machine, sampled all the time, each with its parent. And
every row, whatever it provokes, ends with the same four checks:

  * never two of its llama-servers alive at once - the one claim a card with
    room for one model cares about - read off the MACHINE, not the record.
    Since S7 the runner keeps models side by side; this suite runs it at
    `max_loads = 1`, so the claim is still the runner's own, and a switch is
    the runner making room. Models side by side on the real card are M1's,
    with the owner's go. Answers at once (S17) change none of these checks:
    what runs at once is answers of DIFFERENT models, each model one at a
    time, and at `max_loads = 1` there is one model - one answer at a time
    here. Two answers at once on the real card, against the same two one
    after the other, are M2's to measure;
  * none of them outlives the row (one that does is killed, and the row fails);
  * the card's memory comes back to what it was before the row;
  * the runner's own record never has two models loaded at once.

A LLAMA-SERVER THIS SUITE DID NOT START STOPS THE SUITE - before every row and
while one runs. Its parent is not this suite's, so it is somebody else's model
on the card: the suite takes its own off and touches nothing else.

The rows provoke what the stand-in suites can only imitate: loads that take
real seconds, a card that takes real time to let go, real processes killed
where they stand, and cancels, stops and kills thrown into each of those
windows.
"""
from __future__ import annotations

import json
import os
import random
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "llm-bootstrap-coordinator"))

import cardwatch as W                                           # noqa: E402
import server as S                                              # noqa: E402
from runner import Config, orphaned_loads, read_events, side_by_side  # noqa: E402
from runner.gguf import kv_cache_bytes, read_header             # noqa: E402
from runner.manager import ModelManager, kill_pid               # noqa: E402

REAL = os.environ.get("LLM_BOOTSTRAP_REAL_CARD")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
FINISHED = ("done", "failed", "cancelled", "claimed")

#: the parents whose llama-servers are this suite's: this process, and every
#: server process it starts - held by reference by every watch
OURS: set = {os.getpid()}
#: set when the suite must not go on (somebody else's model on the card)
STOPPED: list = []
#: how many times each row has run in this process - a repeated row keeps
#: each run's evidence in a folder of its own
RUNS: dict = {}

#: the card is "back" within this much of what it held before the row - the
#: desktop's own use drifts by a few hundred MiB
CARD_BACK_TOL_MIB = 700
#: how long the card is given to come back, and a llama-server to go
CARD_BACK_S = 30.0
LINGER_S = 20.0

SHORT = "Reply with exactly one word: ready"
COUNT = ("Count upward from one to five hundred, writing every number as a word, "
         "separated by commas. Do not stop early and do not add anything else.")


def _facts(lines: int) -> str:
    """A long prompt of synthetic facts - about 15 tokens a line."""
    body = "\n".join(f"Fact {i}: the value of item {i} is {i * 7 % 1000}."
                     for i in range(1, lines + 1))
    return f"{body}\nWhat is the value of item {lines // 2}? Answer with the number only."


# ------------------------------------------------------------------ settings

def _base():
    """(config, model spec, the card it asks for in MiB) - or None when this
    suite is not to run."""
    if not REAL:
        return None
    cfg = Config.load(REAL)
    if cfg.backend != "llamacpp":
        raise SystemExit(f"{REAL}: [runner] backend must be llamacpp for the real card")
    key = os.environ.get("LLM_BOOTSTRAP_REAL_MODEL") or sorted(cfg.models)[0]
    spec = cfg.model(key)
    gguf = spec.gguf_path(cfg.models_dir)
    if not gguf.is_file():
        raise SystemExit(f"{REAL}: the model file {gguf} is not there")
    per = 1.0 if str(spec.cache_type_k).startswith("q8") else 2.0
    kv = kv_cache_bytes(read_header(gguf), int(spec.ctx), per) or 0
    need = gguf.stat().st_size + kv + ModelManager.VRAM_SLACK_GB * 1024 ** 3
    return cfg, spec, int(need / 1024 ** 2)


BASE = _base()
OUT = Path(os.environ.get("LLM_BOOTSTRAP_REAL_OUT")
           or Path(tempfile.gettempdir()) / f"llmb-real-{time.strftime('%Y%m%d-%H%M%S')}")


def _toml(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return json.dumps(v)                                    # strings and lists


def write_settings(path: Path, models=None, **runner) -> Path:
    """The row's own settings: the base file's llama-server and model, with the
    row's overrides. `models` maps a key to overrides of the base model."""
    cfg, spec, _ = BASE
    r = {"backend": "llamacpp", "llama_server": cfg.runner["llama_server"],
         "models_dir": str(cfg.models_dir).replace("\\", "/"), "port": 0,
         "startup_timeout_s": 300.0, "health_poll_s": 0.5, "shutdown_timeout_s": 60.0,
         "idle_unload_s": 600.0, "card_patience_s": 600.0,
         # S7: one model at a time on this suite's card - what its claims are about
         "max_loads": 1, **runner}
    lines = ["[runner]"] + [f"{k} = {_toml(v)}" for k, v in r.items()]
    for key, over in (models or {"m": {}}).items():
        m = {"label": key, "file": spec.file, "ctx": spec.ctx,
             "n_gpu_layers": spec.n_gpu_layers, "cache_type_k": spec.cache_type_k,
             "cache_type_v": spec.cache_type_v, "temperature": spec.temperature,
             "disable_thinking": spec.disable_thinking, **over}
        lines += ["", f"[models.{key}]"] + [f"{k} = {_toml(v)}" for k, v in m.items()]
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return path


def quiet_port() -> int:
    """A port for a server this suite starts, below the range the operating
    system hands to passing connections."""
    rng = random.Random()
    for _ in range(400):
        p = rng.randrange(33000, 33500)
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
    raise RuntimeError("no free port in 33000-33499")


def http(url: str, method: str = "GET", body=None, timeout: float = 30.0):
    data = json.dumps(body).encode("utf-8") if body is not None else (
        b"" if method == "POST" else None)
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        with e:
            raw = e.read()
        try:
            return e.code, json.loads(raw or b"null")
        except ValueError:
            return e.code, {"raw": raw[:500].decode("utf-8", "replace")}


class ForeignOnCard(AssertionError):
    """A llama-server this suite did not start is on the machine."""


# ------------------------------------------------------------------- servers

class Client:
    """What a row asks a server, over HTTP - as an application would."""

    url = ""
    ws: Path

    def __init__(self, case):
        self.case = case

    def get(self, path, timeout=30.0):
        return http(self.url + path, timeout=timeout)

    def post(self, path, body=None, timeout=30.0):
        return http(self.url + path, "POST", body, timeout=timeout)

    def health(self) -> dict:
        return self.get("/api/health")[1]

    def submit(self, prompt: str, model: str = "m", **gen) -> str:
        status, out = self.post("/api/jobs", {"kind": "generate", "input": {"prompt": prompt},
                                              "gen": {"model": model, **gen}})
        if status != 200:
            raise AssertionError(f"submit answered {status}: {out}")
        self.case.watch.mark(f"{out['job_id']} submitted ({model})")
        return out["job_id"]

    def cancel(self, jid: str, timeout: float = 30.0):
        self.case.watch.mark(f"{jid} cancel sent")
        t = time.monotonic()
        status, out = self.post(f"/api/jobs/{jid}/cancel", timeout=timeout)
        self.case.watch.mark(f"{jid} cancel answered {status} after "
                             f"{time.monotonic() - t:.2f}s: "
                             f"{(out.get('job') or {}).get('status') if isinstance(out, dict) else out}")
        return status, out

    def job(self, jid: str):
        status, out = self.get(f"/api/jobs/{jid}/result")
        if status != 200:
            raise AssertionError(f"result {jid} answered {status}: {out}")
        return out["job"], out.get("candidates") or []

    def wait(self, jid: str, until=FINISHED, timeout: float = 300.0, stage=None):
        """Poll until the job's status is one of *until* (or, with *stage*,
        until its progress says that stage), marking every stage it passes."""
        deadline = time.monotonic() + timeout
        last = None
        while True:
            self.case.check_foreign()
            job, cands = self.job(jid)
            prog = job.get("progress") or {}
            now = (job["status"], prog.get("stage"))
            if now != last:
                self.case.watch.mark(f"{jid} {job['status']}"
                                     + (f" - {prog['stage']}" if prog.get("stage") else ""))
                last = now
            if job["status"] in until and stage is None:
                return job, cands
            if stage is not None and (prog.get("stage") == stage or job["status"] in FINISHED):
                return job, cands
            if time.monotonic() > deadline:
                raise AssertionError(f"{jid} still {job['status']} after {timeout:g}s: {job}")
            time.sleep(0.15)

    def events(self) -> list:
        return read_events(self.ws / "runner" / "runner_events.jsonl")

    def runner(self) -> dict:
        return self.health().get("runner") or {}

    def wait_runner(self, pred, timeout: float = 300.0, what: str = "") -> dict:
        deadline = time.monotonic() + timeout
        while True:
            self.case.check_foreign()
            r = self.runner()
            if pred(r):
                return r
            if time.monotonic() > deadline:
                raise AssertionError(f"the runner never {what} in {timeout:g}s: {r}")
            time.sleep(0.1)


class Srv(Client):
    """A server in THIS process - so a row can stop it the way a person does,
    and time the stop. Its llama-servers are this process's children."""

    def __init__(self, case, settings: Path, ws: Path, coordinator=None, name=None,
                 backend="llamacpp"):
        super().__init__(case)
        argv = ["--backend", backend, "--runner", str(settings), "--workspace", str(ws)]
        if coordinator:
            argv += ["--coordinator", coordinator] + (["--name", name] if name else [])
        else:
            argv += ["--port", str(quiet_port())]
        self.ws, self.said = ws, []
        self.httpd = S.start_server(S.parse_args(argv), log=self.said.append)
        self.url = self.httpd.app.url
        self.actor = self.httpd.app.actor
        self.stopped = False

    def stop(self) -> float:
        """Stop it; -> the seconds the stop took."""
        if self.stopped:
            return 0.0
        self.stopped = True
        self.case.watch.mark(f"server {self.actor} stopping")
        t = time.monotonic()
        try:
            S.stop(self.httpd)
        finally:
            took = time.monotonic() - t
            self.case.watch.mark(f"server {self.actor} stopped after {took:.2f}s")
            (self.case.dir / f"server-{self.actor}.log").write_bytes(
                ("\n".join(self.said) + "\n").encode("utf-8"))
        return took


class SrvProc(Client):
    """A server in a PROCESS OF ITS OWN - what a row kills where it stands."""

    def __init__(self, case, settings: Path, ws: Path, coordinator=None, name=None,
                 tag="server", extra=(), backend="llamacpp"):
        super().__init__(case)
        argv = ["--backend", backend, "--runner", str(settings), "--workspace", str(ws),
                *extra]
        if coordinator:
            argv += ["--coordinator", coordinator] + (["--name", name] if name else [])
        else:
            argv += ["--port", str(quiet_port())]
        self.ws = ws
        self.log_path = case.dir / f"{tag}.log"
        self._fh = open(self.log_path, "ab")
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        self.proc = subprocess.Popen([sys.executable, "-u", str(HERE / "server.py"), *argv],
                                     stdout=self._fh, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, cwd=str(HERE), env=env,
                                     creationflags=NO_WINDOW | NEW_GROUP)
        OURS.add(self.proc.pid)
        deadline = time.monotonic() + 90
        while True:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"listening on (http://\S+) as (srv-\w+)", text)
            if m:
                self.url, self.actor = m.group(1), m.group(2)
                break
            if self.proc.poll() is not None:
                raise AssertionError(f"the server exited with {self.proc.returncode}:\n"
                                     f"{text[-3000:]}")
            if time.monotonic() > deadline:
                raise AssertionError(f"the server did not start in 90s:\n{text[-3000:]}")
            time.sleep(0.1)
        case.watch.mark(f"server process {self.proc.pid} ({self.actor}) listening")

    def kill(self) -> None:
        """TerminateProcess: no goodbye, no `finally`, nothing unwinds."""
        if self.proc.poll() is None:
            self.case.watch.mark(f"server process {self.proc.pid} KILLED")
            self.proc.kill()
        try:
            self.proc.wait(timeout=15)
        finally:
            self._fh.close()


# ---------------------------------------------------------------------- rows

@unittest.skipUnless(BASE, "the real card is taken only when asked: set LLM_BOOTSTRAP_REAL_CARD "
                           "to a runner settings file (llama_server, models_dir, a model)")
class CardCase(unittest.TestCase):
    def setUp(self):
        if STOPPED:
            self.skipTest(f"the suite stopped: {STOPPED[0]}")
        self.cfg, self.spec, self.need_mib = BASE
        name = self.id().split(".", 1)[-1]
        RUNS[name] = RUNS.get(name, 0) + 1
        self.dir = OUT / (name if RUNS[name] == 1 else f"{name}-{RUNS[name]}")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.notes: list = []
        self._check_card_free()
        self.watch = W.CardWatch(OURS, on_foreign=self._foreign_seen).start()
        self.t0 = time.time()
        self.baseline = self.watch.used()
        self.addCleanup(self._verify_card)                  # runs LAST: after the stops

    # the card ----------------------------------------------------------------

    def _check_card_free(self) -> None:
        alive = W.llama_servers()
        foreign = [s for s in alive if s["ppid"] not in OURS]
        if foreign:
            why = "; ".join(f"pid {s['pid']} (parent {s['ppid']}): "
                            f"{W.command_line(s['pid']) or '?'}" for s in foreign)
            STOPPED.append(f"a llama-server this suite did not start is running - {why}")
            raise ForeignOnCard(STOPPED[0])
        left = [s["pid"] for s in alive]
        if left:
            raise AssertionError(f"a llama-server of this suite is still running from an "
                                 f"earlier row: {left}")
        card = W.card_now()
        if card is None:
            STOPPED.append("the card cannot be read - unknown is never free")
            self.skipTest(STOPPED[0])
        if card["free"] < self.need_mib:
            STOPPED.append(f"the card is not free: {card['used']} MiB used of {card['total']}; "
                           f"the model needs about {self.need_mib} MiB")
            raise ForeignOnCard(STOPPED[0])

    def _foreign_seen(self, pid, ppid) -> None:
        STOPPED.append(f"a llama-server this suite did not start appeared: pid {pid} "
                       f"(parent {ppid})")

    def check_foreign(self) -> None:
        if self.watch.foreign:
            raise ForeignOnCard("; ".join(self.watch.describe(p) for p in self.watch.foreign))

    def note(self, text: str) -> None:
        self.notes.append(text)

    def _verify_card(self) -> None:
        problems = []
        deadline = time.monotonic() + LINGER_S
        while self.watch.ours_alive() and time.monotonic() < deadline:
            time.sleep(0.2)
        for pid in self.watch.ours_alive():
            problems.append(f"a llama-server of this suite outlived the row - killed: "
                            f"{self.watch.describe(pid)}")
            cmd = W.command_line(pid) or ""
            if "--alias load-" in cmd:                      # ours by its parent AND its line
                kill_pid(pid)
        # M1's rows put models side by side ON PURPOSE - as many as the row says
        allowed = getattr(self, "together_allowed", 1)
        if self.watch.peak_together > allowed:
            problems.append(f"{self.watch.peak_together} OF THIS SUITE'S MODEL SERVERS WERE ALIVE "
                            f"AT ONCE (the row allows {allowed}), first at "
                            f"+{self.watch.together_at - self.t0:.2f}s")
        limit = self.baseline + CARD_BACK_TOL_MIB
        back = self.watch.wait_used(limit, CARD_BACK_S, since=time.time() - 0.5)
        if back is None:
            problems.append(f"the card did not come back: {self.watch.used()} MiB used after "
                            f"{CARD_BACK_S:g}s; {self.baseline} before the row")
        for f in self.dir.rglob("runner_events.jsonl"):
            peak = side_by_side(read_events(f))           # max_loads is 1, but in M1's rows
            if peak > allowed:
                problems.append(f"the record {f} has {peak} models loaded at once")
        self.watch.stop()
        extra = []
        for f in self.dir.rglob("runner_events.jsonl"):
            extra += [(e.get("ts", 0), f"runner[{f.parent.parent.name}]: {_event_line(e)}")
                      for e in read_events(f)]
        for f in self.dir.rglob("coordinator-*/events.jsonl"):
            extra += [(e.get("at", 0), f"coordinator[{f.parent.name}]: {e.get('kind')} "
                                       + " ".join(f"{k}={e[k]}" for k in
                                                  ("ticket_id", "holder", "port", "detail")
                                                  if e.get(k))[:300])
                      for e in read_events(f)]
        timeline = self.watch.timeline(self.t0, extra)
        peak = self.watch.peak_used(self.t0)
        head = [f"row: {self.id()}", f"card before: {self.baseline} MiB; peak {peak} MiB; "
                f"together at most {self.watch.peak_together}"] + self.notes
        (self.dir / "timeline.txt").write_bytes(
            ("\n".join(head + [""] + timeline + [""] + problems) + "\n").encode("utf-8"))
        print(f"\n--- {self.id().split('.')[-1]}: card {self.baseline} -> peak {peak} MiB, "
              f"together {self.watch.peak_together}", flush=True)
        for n in self.notes:
            print(f"    {n}", flush=True)
        if problems:
            self.fail("\n".join(problems) + f"\n(timeline: {self.dir / 'timeline.txt'})")

    # servers -----------------------------------------------------------------

    def settings(self, name="settings.toml", **kw) -> Path:
        return write_settings(self.dir / name, **kw)

    def server(self, settings=None, ws="ws", **kw) -> Srv:
        folder = self.dir / ws
        folder.mkdir(exist_ok=True)
        srv = Srv(self, settings or self.settings(), folder, **kw)
        self.addCleanup(srv.stop)
        return srv

    def server_process(self, settings=None, ws="ws", **kw) -> SrvProc:
        folder = self.dir / ws
        folder.mkdir(exist_ok=True)
        srv = SrvProc(self, settings or self.settings(), folder, **kw)
        self.addCleanup(srv.kill)
        return srv

    def coordinator(self, ttl: float = 3.0, **settings) -> Coord:
        co = Coord(self.dir, ttl, **settings)
        self.addCleanup(co.stop)
        return co

    def llama(self, timeout: float = 60.0, besides=()) -> int:
        """The pid of this suite's llama-server that is up now (other than
        *besides*), waiting for one to appear."""
        pid = until(lambda: next((p for p in self.watch.ours_alive() if p not in besides),
                                 None), timeout)
        if pid is None:
            raise AssertionError(f"no llama-server of this suite appeared in {timeout:g}s")
        return pid

    def gone(self, pid: int, timeout: float = 20.0):
        """Seconds until *pid* is gone from the machine, from now; None if not."""
        t = time.time()
        at = until(lambda: self.watch.gone_at(pid), timeout)
        return None if at is None else max(0.0, at - t)

    def loads(self, srv) -> int:
        return sum(1 for e in srv.events() if e["event"] == "load_ok")

    def assert_run_again(self, srv, jid):
        """What the queue promises for a job a stopped server left running: the
        next start puts it back in the queue and runs it again, to a whole
        answer - and the request's record says so."""
        job, cands = srv.wait(jid, timeout=400)
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertIn(cands[-1].get("finish_reason"), ("stop", "length"))
        record = [e["event"] if e["event"] != "recovered" else f"recovered:{e['settled_as']}"
                  for e in read_events(srv.ws / "requests" / job["correlation_id"]
                                       / "events.jsonl")]
        self.assertIn("recovered:requeued", record)
        self.note(f"the next start ran it again: {cands[-1].get('tokens')} tokens, "
                  f"{cands[-1].get('finish_reason')}")


class Coord:
    """A real coordinator, served in this process on a port of its own - which a
    row can restart, with its record or WITHOUT it (a coordinator that lost
    its record knows no ticket, no rental and no port)."""

    def __init__(self, folder: Path, ttl: float, **settings):
        import coordinator as C
        self.C, self.folder, self.ttl = C, folder, float(ttl)
        #: the coordinator's other settings - and `hardware`, the machine it
        #: asks (none, unless a row names it)
        self.settings = settings
        self.n, self.port, self.srv = 0, quiet_port(), None
        self.start()

    def start(self, fresh: bool = False) -> None:
        if fresh:
            self.n += 1
        self.c = self.C.Coordinator(self.folder / f"coordinator-{self.n}", ttl_s=self.ttl,
                                    **self.settings)
        self.srv = self.C.serve(self.c, self.port)
        self.url = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        if self.srv is not None:
            self.srv.stop_reaper.set()
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None

    def restart(self, fresh: bool) -> None:
        self.stop()
        self.start(fresh)

    def events(self) -> list:
        out = []
        for f in sorted(self.folder.glob("coordinator-*/events.jsonl")):
            out += read_events(f)
        return out


def until(pred, timeout: float, every: float = 0.05):
    """The first truthy answer of *pred* within *timeout*, else None."""
    deadline = time.monotonic() + timeout
    while True:
        v = pred()
        if v:
            return v
        if time.monotonic() > deadline:
            return None
        time.sleep(every)


_KEYS: dict = {}


def slots(port: int, pid: int) -> list:
    """llama-server's own account of its slots - asked with the key on its
    command line."""
    if pid not in _KEYS:
        _KEYS[pid] = W.api_key(pid)
    req = urllib.request.Request(f"http://127.0.0.1:{port}/slots",
                                 headers={"Authorization": f"Bearer {_KEYS[pid]}"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def busy_slots(port: int, pid: int) -> int:
    got = slots(port, pid)
    if not got or not all("is_processing" in s for s in got):
        raise AssertionError(f"/slots does not say which slot is busy: {str(got)[:300]}")
    return sum(1 for s in got if s["is_processing"])


def _event_line(e: dict) -> str:
    keep = ("model_key", "load", "pid", "port", "load_seconds", "need_gb", "free_gb",
            "cold", "reason", "detail", "turn")
    parts = [f"{k}={e[k]}" for k in keep if e.get(k) not in (None, "")]
    return f"{e.get('event')} " + " ".join(parts)[:400]


def _first(events, kind):
    return next((e for e in events if e.get("event") == kind), None)


class FirstLoadRows(CardCase):
    def test_a_first_load_answers_and_comes_off_the_card(self):
        """The whole path once, as an application runs it: a server bound to a
        folder, one job, an answer, a stop - and the card as it was."""
        srv = self.server()
        jid = srv.submit(SHORT, max_tokens=24)
        job, cands = srv.wait(jid, timeout=400)
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertTrue(cands and cands[0]["text"].strip(), cands)
        loaded_peak = self.watch.peak_used(self.t0)
        runner = srv.runner()
        self.assertEqual(runner["loaded"], "m")
        self.assertIsNotNone(runner["pid"])
        took = srv.stop()
        events = srv.events()
        kinds = [e["event"] for e in events]
        for kind in ("vram_check", "load_start", "load_ok", "unload_start", "unload_ok"):
            self.assertIn(kind, kinds)
        self.assertEqual(orphaned_loads(events), [])
        vram, ok = _first(events, "vram_check"), _first(events, "load_ok")
        cand = cands[0]
        self.note(f"answer {cand['text'].strip()[:60]!r} ({cand.get('finish_reason')}, "
                  f"{cand.get('tokens')} tokens)")
        self.note(f"load {ok['load_seconds']:.1f}s (cold={ok['cold']}); the runner asked for "
                  f"{vram['need_gb']} GB with {vram['free_gb']} GB free; the card rose by "
                  f"{(loaded_peak - self.baseline) / 1024:.2f} GB")
        timings = cand.get("timings") or {}
        if timings:
            self.note(f"prompt {timings.get('prompt_per_second', 0):.0f} tok/s, generation "
                      f"{timings.get('predicted_per_second', 0):.1f} tok/s")
        self.note(f"stop {took:.2f}s")
        self.assertLess(loaded_peak - self.baseline, vram["need_gb"] * 1024,
                        "the card rose by more than the runner asked for: the estimate is low")


class ReuseRows(CardCase):
    def test_jobs_submitted_at_once_share_one_load(self):
        """Six applications' worth of jobs in the same instant: one load, five
        reuses, one llama-server for the whole row."""
        srv = self.server()
        jids, errs = [], []

        def go(i):
            try:
                jids.append(srv.submit(f"{SHORT} (request {i})", max_tokens=16))
            except Exception as exc:                        # noqa: BLE001
                errs.append(exc)

        threads = [threading.Thread(target=go, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errs, [])
        jobs = [srv.wait(j, timeout=400)[0] for j in jids]
        self.assertEqual([j["status"] for j in jobs], ["done"] * 6,
                         [j.get("error") for j in jobs])
        events = srv.events()
        self.assertEqual(self.loads(srv), 1)
        self.assertEqual(sum(1 for e in events if e["event"] == "reuse"), 5)
        self.assertEqual(len(self.watch.seen), 1, "more than one llama-server for one model")


class CancelRows(CardCase):
    def test_a_cancel_mid_answer_frees_the_slot_at_once(self):
        """The model is writing; the cancel ends the job, and llama-server
        itself lets go of the slot - it does not keep writing to nobody."""
        srv = self.server()
        jid = srv.submit(COUNT, max_tokens=1500)
        srv.wait(jid, stage="writing", timeout=400)
        time.sleep(1.0)
        r = srv.runner()
        self.assertGreater(busy_slots(r["port"], r["pid"]), 0,
                           "the model is writing, so a slot is busy - or the instrument is blind")
        t = time.monotonic()
        srv.cancel(jid)
        job, _ = srv.wait(jid, timeout=30)
        ended = time.monotonic() - t
        self.assertEqual(job["status"], "cancelled", job.get("error"))
        free = until(lambda: busy_slots(r["port"], r["pid"]) == 0, 10)
        freed = time.monotonic() - t
        self.note(f"cancelled after {ended:.2f}s; llama-server's slot free after {freed:.2f}s")
        self.assertLess(ended, 3.0)
        self.assertTrue(free, "llama-server kept writing an answer nobody waits for")
        t = time.monotonic()
        j2 = srv.submit(SHORT, max_tokens=8)
        self.assertEqual(srv.wait(j2, timeout=60)[0]["status"], "done")
        self.note(f"the next job answered in {time.monotonic() - t:.2f}s")
        self.assertEqual(self.loads(srv), 1)

    def test_a_cancel_while_the_prompt_is_read_frees_the_slot(self):
        """Before the first token: llama-server is still reading a long
        prompt. The cancel must end the job AND the reading."""
        srv = self.server()
        j0 = srv.submit(SHORT, max_tokens=4)
        srv.wait(j0, timeout=400)
        r = srv.runner()
        jid = srv.submit(_facts(170), max_tokens=32)
        self.assertTrue(until(lambda: busy_slots(r["port"], r["pid"]) > 0, 30, every=0.02),
                        "llama-server never started reading the prompt")
        self.watch.mark("llama-server is reading the prompt")
        time.sleep(0.4)
        job, _ = srv.job(jid)
        self.assertNotEqual((job.get("progress") or {}).get("stage"), "writing",
                            "the prompt was read before the cancel: make it longer")
        t = time.monotonic()
        srv.cancel(jid)
        job, _ = srv.wait(jid, timeout=30)
        ended = time.monotonic() - t
        free = until(lambda: busy_slots(r["port"], r["pid"]) == 0, 20, every=0.02)
        freed = time.monotonic() - t
        self.note(f"cancelled after {ended:.2f}s; llama-server stopped reading after {freed:.2f}s")
        self.assertEqual(job["status"], "cancelled", job.get("error"))
        self.assertLess(ended, 3.0)
        self.assertTrue(free, "llama-server went on reading a prompt nobody waits for")
        self.assertLess(freed, 2.0, "llama-server went on reading a prompt nobody waits for")

    def test_a_cancel_while_the_model_loads_gives_the_load_up(self):
        """The weights are going up and nothing else waits for them. Health
        keeps answering; the cancel ends the job at once, and llama-server
        with it - the card comes back. (Found on the first runs: the job ended
        only once the weights were up, 7.0-7.4 s after the cancel, and the
        model then sat on the card for nobody.)"""
        srv = self.server()
        jid = srv.submit(SHORT, max_tokens=8)
        srv.wait_runner(lambda r: r.get("loading") == "m", 60, "started loading")
        pid = self.llama()
        time.sleep(1.0)
        worst = 0.0
        for _ in range(5):
            t = time.monotonic()
            srv.health()
            worst = max(worst, time.monotonic() - t)
        t = time.monotonic()
        status, out = srv.cancel(jid, timeout=60)
        answered = time.monotonic() - t
        job, _ = srv.wait(jid, timeout=60)
        ended = time.monotonic() - t
        off = self.gone(pid)
        self.note(f"health answered within {worst:.2f}s during the load; the cancel answered "
                  f"{status} ({(out.get('job') or {}).get('status')}) after {answered:.2f}s; "
                  f"the job ended {job['status']} after {ended:.2f}s; llama-server gone "
                  f"{off if off is None else round(off, 2)}s after that")
        self.assertLess(worst, 1.0, "health waited on the load")
        self.assertEqual(job["status"], "cancelled", job.get("error"))
        self.assertLess(ended, 3.0, "the cancel waited out the load")
        self.assertIsNotNone(off, "llama-server went on loading for nobody")
        self.assertEqual(self.loads(srv), 0, "a load nobody wanted was finished")
        j2 = srv.submit(SHORT, max_tokens=8)
        self.assertEqual(srv.wait(j2, timeout=400)[0]["status"], "done")

    def test_a_cancel_while_the_model_loads_leaves_the_load_to_the_next_job(self):
        """The weights are going up and the job queued behind wants the same
        model: the cancelled job ends once they are up, and they serve it."""
        srv = self.server()
        jid = srv.submit(SHORT, max_tokens=8)
        j2 = srv.submit(SHORT, max_tokens=8)
        srv.wait_runner(lambda r: r.get("loading") == "m", 60, "started loading")
        time.sleep(1.0)
        srv.cancel(jid, timeout=60)
        self.assertEqual(srv.wait(jid, timeout=400)[0]["status"], "cancelled")
        self.assertEqual(srv.wait(j2, timeout=400)[0]["status"], "done")
        self.assertEqual(self.loads(srv), 1, "the load a cancel left was not the next job's")
        self.assertIn("load_kept", [e["event"] for e in srv.events()])

    def test_a_storm_of_cancels_leaves_a_working_model(self):
        """Eight jobs, cancelled at random moments - queued, loading, reading,
        writing. A cancel is never a failure, and the model works after."""
        srv = self.server()
        rng = random.Random(20260924)
        jids = [srv.submit(f"{COUNT} ({i})", max_tokens=200) for i in range(8)]
        for jid in rng.sample(jids, len(jids)):
            time.sleep(rng.uniform(0.2, 2.5))
            srv.cancel(jid)
        ends = [srv.wait(j, timeout=400)[0] for j in jids]
        self.note("ended: " + ", ".join(j["status"] for j in ends))
        self.assertNotIn("failed", [j["status"] for j in ends],
                         [j["error"] for j in ends if j["status"] == "failed"])
        j = srv.submit(SHORT, max_tokens=8)
        self.assertEqual(srv.wait(j, timeout=400)[0]["status"], "done")
        r = srv.runner()
        self.assertEqual(busy_slots(r["port"], r["pid"]), 0)
        # a load whose job was cancelled while nothing waiting wanted it was
        # given up - and said so; no load failed any other way
        for e in srv.events():
            if e["event"] == "load_failed":
                self.assertIn("its job was cancelled", e.get("detail", ""), e)


class StopRows(CardCase):
    def test_a_stop_while_writing_takes_the_model_off(self):
        """A person stops the server mid-answer. The model comes off, and the
        next start settles the job the stop left."""
        srv = self.server()
        jid = srv.submit(COUNT, max_tokens=1500)
        srv.wait(jid, stage="writing", timeout=400)
        time.sleep(0.5)
        took = srv.stop()
        self.note(f"the stop took {took:.2f}s")
        self.assertLess(took, 10.0)
        again = self.server()                               # the same folder
        self.assert_run_again(again, jid)

    def test_a_stop_while_the_model_loads_does_not_wait_out_the_load(self):
        """A person stops the server while the weights go up. The stop must not
        wait for a load nobody will use."""
        srv = self.server()
        srv.submit(SHORT, max_tokens=8)
        srv.wait_runner(lambda r: r.get("loading") == "m", 60, "started loading")
        time.sleep(1.0)
        took = srv.stop()
        ok = _first(srv.events(), "load_ok")
        self.note(f"the stop took {took:.2f}s; the load "
                  + (f"finished after {ok['load_seconds']}s" if ok else "was abandoned"))
        self.assertLess(took, 5.0, f"the stop waited out the load ({took:.1f}s)")


class KillRows(CardCase):
    def test_a_server_killed_mid_answer_takes_its_llama_server_with_it(self):
        """TerminateProcess on the server: nothing of it runs again. Its
        llama-server must go with it (the kill-on-close job), the card must
        come back, and the next start on the folder must work."""
        srv = self.server_process()
        jid = srv.submit(COUNT, max_tokens=1500)
        srv.wait(jid, stage="writing", timeout=400)
        llama = srv.runner()["pid"]
        t = time.time()
        srv.kill()
        gone = self.gone(llama, 20)
        back = self.watch.wait_used(self.baseline + CARD_BACK_TOL_MIB, 30, since=t)
        self.note(f"llama-server gone {gone if gone is None else round(gone, 2)}s after the "
                  f"kill; the card back after {back if back is None else round(back, 2)}s")
        self.assertIsNotNone(gone, "llama-server outlived its killed server")
        again = self.server()                               # the same folder, in this process
        self.assert_run_again(again, jid)
        j2 = again.submit(SHORT, max_tokens=8)
        self.assertEqual(again.wait(j2, timeout=400)[0]["status"], "done")

    def test_a_server_killed_while_the_model_loads_takes_it_along(self):
        srv = self.server_process()
        srv.submit(SHORT, max_tokens=8)
        llama = self.llama()
        time.sleep(3.0)                                     # the weights are going up
        t = time.time()
        srv.kill()
        gone = self.gone(llama, 20)
        back = self.watch.wait_used(self.baseline + CARD_BACK_TOL_MIB, 30, since=t)
        self.note(f"llama-server gone {gone if gone is None else round(gone, 2)}s after the "
                  f"kill; the card back after {back if back is None else round(back, 2)}s")
        self.assertIsNotNone(gone, "a half-loaded llama-server outlived its killed server")

    def test_llama_server_killed_mid_answer_fails_the_job_and_the_next_one_reloads(self):
        srv = self.server()
        jid = srv.submit(COUNT, max_tokens=1500)
        srv.wait(jid, stage="writing", timeout=400)
        llama = srv.runner()["pid"]
        self.assertIn("--alias load-", W.command_line(llama) or "")
        self.watch.mark(f"llama-server {llama} KILLED")
        kill_pid(llama)
        job, _ = srv.wait(jid, timeout=60)
        self.note(f"the job: {job['status']} - {job.get('error')}")
        self.assertEqual(job["status"], "failed")
        self.assertIn("mid-answer", job["error"] or "")
        j2 = srv.submit(SHORT, max_tokens=8)
        job2, _ = srv.wait(j2, timeout=400)
        self.assertEqual(job2["status"], "done", job2.get("error"))
        kinds = [e["event"] for e in srv.events()]
        self.assertIn("stale_backend", kinds)
        self.assertEqual(self.loads(srv), 2)

    def test_llama_server_killed_while_loading_fails_the_load_in_words(self):
        srv = self.server()
        jid = srv.submit(SHORT, max_tokens=8)
        llama = self.llama()
        time.sleep(3.0)
        self.watch.mark(f"llama-server {llama} KILLED")
        kill_pid(llama)
        t = time.monotonic()
        job, _ = srv.wait(jid, timeout=120)
        self.note(f"the job ended {job['status']} {time.monotonic() - t:.2f}s after the kill: "
                  f"{(job.get('error') or '')[:200]}")
        self.assertEqual(job["status"], "failed")
        self.assertIn("exited", job["error"] or "")
        self.assertLess(time.monotonic() - t, 15.0)
        j2 = srv.submit(SHORT, max_tokens=8)
        self.assertEqual(srv.wait(j2, timeout=400)[0]["status"], "done")


class IdleRows(CardCase):
    def test_a_job_arriving_as_the_idle_sweep_takes_the_model_off(self):
        """The sweep has begun taking the model off; a job arrives in that very
        moment - three times. Every job answers; never two on the card."""
        srv = self.server(self.settings(idle_unload_s=2.0))
        j = srv.submit(SHORT, max_tokens=8)
        self.assertEqual(srv.wait(j, timeout=400)[0]["status"], "done")
        for n in range(1, 4):
            hit = until(lambda: sum(1 for e in srv.events() if e["event"] == "idle_unload") >= n,
                        30, every=0.02)
            self.assertTrue(hit, "the idle sweep never took the model off")
            self.watch.mark("the idle sweep is taking the model off")
            j = srv.submit(SHORT, max_tokens=8)
            job, _ = srv.wait(j, timeout=400)
            self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(self.loads(srv), 4)


class SwitchRows(CardCase):
    def test_switching_models_back_to_back_never_puts_two_on_the_card(self):
        """Two models (one file, two contexts) asked for alternately, all
        queued at once, so every unload is followed at once by a load. The
        card must let go of one before the check for the next reads it."""
        srv = self.server(self.settings(models={"a": {"ctx": 4096}, "b": {"ctx": 2048}}))
        order = ["a", "b", "a", "b"]
        jids = [srv.submit(SHORT, model=k, max_tokens=8) for k in order]
        jobs = [srv.wait(j, timeout=600)[0] for j in jids]
        self.assertEqual([j["status"] for j in jobs], ["done"] * 4,
                         [j.get("error") for j in jobs])
        checks = [e for e in srv.events() if e["event"] == "vram_check"]
        self.note("free at each check: " + ", ".join(f"{c['free_gb']} GB (need {c['need_gb']})"
                                                     for c in checks))
        self.assertEqual(self.loads(srv), 4)
        self.assertLessEqual(self.watch.peak_used(self.t0) - self.baseline, self.need_mib)


class MachineLockRows(CardCase):
    """Two servers on one machine and NO coordinator: the machine's lock is
    all that keeps a second model off a card the first is on - the case the
    lock exists for, which the stand-ins can only imitate. The record says
    it: each server's take and give-back of the lock, named."""

    def test_two_servers_with_no_coordinator_take_turns_through_the_machine_lock(self):
        sa = self.server(self.settings("a.toml", idle_unload_s=6.0), ws="ws-a")
        sb = self.server(self.settings("b.toml"), ws="ws-b")
        ja = sa.submit(SHORT, max_tokens=8)
        self.assertEqual(sa.wait(ja, timeout=400)[0]["status"], "done")
        a_llama = sa.runner()["pid"]
        jb = sb.submit(SHORT, max_tokens=8)
        job, _ = sb.wait(jb, stage="waiting for the card", timeout=60)
        self.assertEqual((job.get("progress") or {}).get("stage"), "waiting for the card")
        job_b, _ = sb.wait(jb, timeout=400)
        self.assertEqual(job_b["status"], "done", job_b.get("error"))
        a_took, a_gave = _first(sa.events(), "card_granted"), _first(sa.events(), "card_given_back")
        b_waited, b_took = _first(sb.events(), "card_waited"), _first(sb.events(), "card_granted")
        for name, e in (("the first's take", a_took), ("the first's give-back", a_gave),
                        ("the second's wait", b_waited), ("the second's take", b_took)):
            self.assertIsNotNone(e, f"{name} is not on the record")
        self.assertEqual({a_took.get("source"), a_gave.get("source"), b_took.get("source")},
                         {"machine lock"}, "the record does not name the machine's lock")
        self.assertGreaterEqual(b_took["ts"], a_gave["ts"],
                                "the second took the lock before the first gave it back")
        b_llama = _first(sb.events(), "load_ok")["pid"]
        a_gone, b_seen = self.watch.gone_at(a_llama), self.watch.first_seen(b_llama)
        self.note(f"the first's llama-server gone at +{a_gone - self.t0:.2f}s, its lock given back "
                  f"at +{a_gave['ts'] - self.t0:.2f}s; the second took it at "
                  f"+{b_took['ts'] - self.t0:.2f}s after waiting {b_waited['waited_s']}s, and its "
                  f"llama-server appeared at +{b_seen - self.t0:.2f}s")
        self.assertLess(a_gone, b_seen, "the second model started before the first was gone")


class CoordinatorRows(CardCase):
    def test_two_servers_take_turns_on_the_real_card(self):
        co = self.coordinator(ttl=3.0)
        sa = self.server(self.settings("a.toml", idle_unload_s=6.0), ws="ws-a",
                         coordinator=co.url, name="first-app")
        sb = self.server(self.settings("b.toml"), ws="ws-b", coordinator=co.url,
                         name="second-app")
        ja = sa.submit(SHORT, max_tokens=8)
        self.assertEqual(sa.wait(ja, timeout=400)[0]["status"], "done")
        a_llama = sa.runner()["pid"]
        jb = sb.submit(SHORT, max_tokens=8)
        job, _ = sb.wait(jb, stage="waiting for the card", timeout=60)
        self.assertEqual((job.get("progress") or {}).get("stage"), "waiting for the card")
        self.assertTrue(sa.health()["card"]["holding"])
        job_b, _ = sb.wait(jb, timeout=400)
        self.assertEqual(job_b["status"], "done", job_b.get("error"))
        b_llama = _first(sb.events(), "load_ok")["pid"]
        a_gone, b_seen = self.watch.gone_at(a_llama), self.watch.first_seen(b_llama)
        self.note(f"the first's llama-server gone at +{a_gone - self.t0:.2f}s; the second's "
                  f"appeared at +{b_seen - self.t0:.2f}s")
        self.assertLess(a_gone, b_seen, "the second model started before the first was gone")

    def test_a_holder_killed_with_its_model_up_lets_the_other_load(self):
        co = self.coordinator(ttl=3.0)
        sa = self.server_process(self.settings("a.toml"), ws="ws-a", coordinator=co.url,
                                 name="first-app", tag="server-a")
        sb = self.server(self.settings("b.toml"), ws="ws-b", coordinator=co.url,
                         name="second-app")
        ja = sa.submit(SHORT, max_tokens=8)
        self.assertEqual(sa.wait(ja, timeout=400)[0]["status"], "done")
        a_llama = sa.runner()["pid"]
        jb = sb.submit(SHORT, max_tokens=8)
        sb.wait(jb, stage="waiting for the card", timeout=60)
        t = time.time()
        sa.kill()
        job_b, _ = sb.wait(jb, timeout=400)
        self.assertEqual(job_b["status"], "done", job_b.get("error"))
        ok = _first(sb.events(), "load_ok")
        # under a coordinator the memory is the coordinator's to check (PLAN-4
        # D2): the runner records its approval, never a check of its own
        check = _first(sb.events(), "vram_check") or _first(sb.events(), "vram_unknown") or {}
        a_gone, b_seen = self.watch.gone_at(a_llama), self.watch.first_seen(ok["pid"])
        self.note(f"after the kill: the first's llama-server gone at +{a_gone - t:.2f}s; the "
                  f"second's memory: {check.get('detail') or check.get('free_gb')}; "
                  f"its llama-server appeared at +{b_seen - t:.2f}s")
        self.assertIsNotNone(a_gone, "the killed holder's llama-server outlived it")
        self.assertLess(a_gone, b_seen, "the second model started before the first was gone")

    def test_a_server_stopped_while_it_waits_for_the_card_stops_at_once(self):
        co = self.coordinator(ttl=3.0)
        sa = self.server(self.settings("a.toml"), ws="ws-a", coordinator=co.url,
                         name="first-app")
        sb = self.server(self.settings("b.toml"), ws="ws-b", coordinator=co.url,
                         name="second-app")
        ja = sa.submit(SHORT, max_tokens=8)
        self.assertEqual(sa.wait(ja, timeout=400)[0]["status"], "done")
        jb = sb.submit(SHORT, max_tokens=8)
        sb.wait(jb, stage="waiting for the card", timeout=60)
        took = sb.stop()
        self.note(f"the waiting server stopped in {took:.2f}s")
        self.assertLess(took, 3.0, "the stop sat out the wait for the card")
        self.assertEqual(len(self.watch.seen), 1, "the stopped server loaded a model")

    def test_the_card_taken_back_during_a_load_takes_the_model_off_at_once(self):
        """The coordinator comes back without its record while a model loads:
        the next heartbeat's answer says the rental is unknown, so another holder
        may be loading onto the card now. The model must come off at once -
        not when its own load is done."""
        co = self.coordinator(ttl=3.0)
        srv = self.server(self.settings(), coordinator=co.url, name="first-app")
        jid = srv.submit(SHORT, max_tokens=8)
        srv.wait_runner(lambda r: r.get("loading") == "m", 60, "started loading")
        time.sleep(1.0)
        t = time.time()
        self.watch.mark("the coordinator restarts WITHOUT its record")
        co.restart(fresh=True)
        job, _ = srv.wait(jid, timeout=400)
        # off the card by either road: the load given up, or the model unloaded
        off = until(lambda: next((e for e in srv.events() if e["ts"] > t
                                  and e["event"] in ("load_failed", "unload_ok")), None), 60)
        back = self.watch.wait_used(self.baseline + CARD_BACK_TOL_MIB, 30, since=t)
        ok = _first(srv.events(), "load_ok")
        self.note(f"the model was off the card {off['ts'] - t:.2f}s after the coordinator lost "
                  f"the rental ({off['event']}); the card back after "
                  f"{back if back is None else round(back, 2)}s" if off
                  else "the model never came off")
        self.note(f"its load finished {ok['ts'] - t:.2f}s after" if ok else
                  "its load was given up")
        self.note(f"the job: {job['status']} - {(job.get('error') or '')[:160]}")
        self.assertIsNotNone(off, "a model whose card was taken back stayed on it")
        self.assertLess(off["ts"] - t, 5.0, "the model stayed on the card until its load ended")
        self.assertIsNotNone(back, "the card did not come back")


class LimitRows(CardCase):
    def test_a_context_the_card_cannot_hold_is_refused_before_anything_starts(self):
        srv = self.server(self.settings(models={"m": {"ctx": 131072}}))
        jid = srv.submit(SHORT, max_tokens=8)
        job, _ = srv.wait(jid, timeout=120)
        self.note(f"{job['status']}: {(job.get('error') or '')[:240]}")
        self.assertEqual(job["status"], "failed")
        self.assertIn("cannot fit on this card", job["error"] or "")
        self.assertNotIn("Something else", job["error"] or "", "an empty card was blamed on "
                                                               "somebody else")
        self.assertEqual(self.watch.seen, {}, "a llama-server started for a load the card "
                                              "cannot hold")

    def test_a_prompt_longer_than_the_context_fails_the_job_and_keeps_the_model(self):
        """CHANGED (S10b): the model counts the request before it is asked,
        so the job fails with its own reason, `prompt_too_long` - and the
        count is the model's own: a job it answers read its request as the
        count said (owed to the real card by S10b, read from b10724's
        source without it)."""
        srv = self.server()
        j0 = srv.submit(SHORT, max_tokens=4)
        srv.wait(j0, timeout=400)
        t = time.monotonic()
        jid = srv.submit(_facts(400), max_tokens=16)
        job, _ = srv.wait(jid, timeout=120)
        self.note(f"{job['status']} after {time.monotonic() - t:.2f}s: "
                  f"{(job.get('error') or '')[:240]}; count {job['progress'].get('count')}")
        self.assertEqual((job["status"], job.get("reason")), ("failed", "prompt_too_long"))
        j2 = srv.submit(SHORT, max_tokens=8)
        done, cands = srv.wait(j2, timeout=120)
        self.assertEqual(done["status"], "done")
        count, usage = done["progress"].get("count") or {}, cands[0].get("usage") or {}
        self.note(f"the count {count}; the answer's usage {usage}")
        self.assertEqual(count.get("prompt_tokens"), usage.get("prompt_tokens"),
                         "the model read the request as another number than its count")
        self.assertEqual(self.loads(srv), 1)

    def test_an_answer_that_fills_the_context_ends_cleanly(self):
        """An answer with no limit (`max_tokens` -1), in a context of a
        thousand: it must end - done at the edge of the context, or failed in
        words - and leave the model working.

        CHANGED (S10b): it asked for room for twenty thousand tokens, which
        the count now refuses before the model is asked (`prompt_too_long`) -
        so that is asked first, and the answer that fills the context is one
        that names no limit: its room is one token, and llama-server stops it
        at the context's edge."""
        srv = self.server(self.settings(models={"m": {"ctx": 1024}}))
        # the counting prompt: measured to run on to 1,500 tokens and more
        over = srv.submit(COUNT, max_tokens=20000)
        job, _ = srv.wait(over, timeout=400)
        self.note(f"room for 20,000: {job['status']} ({job.get('reason')}): "
                  f"{(job.get('error') or '')[:200]}")
        self.assertEqual((job["status"], job.get("reason")), ("failed", "prompt_too_long"))
        jid = srv.submit(COUNT, max_tokens=-1)
        job, cands = srv.wait(jid, timeout=400)
        c = cands[0] if cands else {}
        usage = c.get("usage") or {}
        self.note(f"{job['status']}: finish {c.get('finish_reason')}, {c.get('tokens')} tokens, "
                  f"prompt {usage.get('prompt_tokens')}; {(job.get('error') or '')[:200]}")
        self.assertIn(job["status"], ("done", "failed"))
        if job["status"] == "done":
            self.assertEqual(c.get("finish_reason"), "length",
                             "the answer ended on its own: the row did not reach the context's "
                             "edge - make it ask for more")
        j2 = srv.submit(SHORT, max_tokens=8)
        self.assertEqual(srv.wait(j2, timeout=120)[0]["status"], "done")


class SoakRows(CardCase):
    def test_five_starts_and_stops_leave_the_card_as_it_was(self):
        folder = self.dir / "ws"
        folder.mkdir(exist_ok=True)
        settings = self.settings()
        seconds = []
        for i in range(5):
            before = self.watch.used()
            srv = Srv(self, settings, folder)
            self.addCleanup(srv.stop)
            j = srv.submit(SHORT, max_tokens=8)
            self.assertEqual(srv.wait(j, timeout=400)[0]["status"], "done")
            srv.stop()
            back = self.watch.wait_used(before + CARD_BACK_TOL_MIB, 30, since=time.time() - 0.5)
            self.assertIsNotNone(back, f"cycle {i + 1}: the card did not come back")
            ok = [e for e in srv.events() if e["event"] == "load_ok"][-1]
            seconds.append(ok["load_seconds"])
        events = read_events(folder / "runner" / "runner_events.jsonl")
        self.note("loads: " + ", ".join(f"{s:.1f}s" for s in seconds))
        self.assertFalse([e for e in events if e["event"] == "load_slow"])
        self.assertEqual(orphaned_loads(events), [])


class PortRows(CardCase):
    def test_the_port_is_held_from_the_first_second_of_the_load(self):
        """Measured, not assumed - this row was wrong twice before it measured:
        llama-server holds its port, and answers `/health` 503, from the first
        second of a load. So nothing can take the port while the model loads,
        and the port a load was given is the one it answers on."""
        import ids
        from runner.llamacpp import port_in_use
        folder = self.dir / "ws" / "runner"
        port = quiet_port()

        class OnePort:
            def take(self, key, load=None):             # the load's id since the ports
                return port                             # table's purpose carries it

            def give_back(self, p):
                pass

        m = ModelManager(Config.load(self.settings()), folder, ports=OnePort())
        self.addCleanup(m.shutdown)
        box = {}

        def load():
            try:
                box["got"] = m.acquire("m", ids.mint_actor("srv"))
            except Exception as exc:                        # noqa: BLE001
                box["got"] = exc

        t = threading.Thread(target=load, daemon=True)
        t.start()
        self.llama()
        time.sleep(1.0)
        self.assertEqual(m.status()["loading"], "m", "the weights were up already")
        thief = socket.socket()
        self.addCleanup(thief.close)
        with self.assertRaises(OSError):
            thief.bind(("127.0.0.1", port))
        self.assertTrue(port_in_use("127.0.0.1", port), "nothing listens on the port mid-load")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5)
            said = 200
        except urllib.error.HTTPError as e:
            said = e.code
            e.close()
        self.assertEqual(said, 503, "llama-server did not say it was loading")
        self.watch.mark(f"port {port} held, and answering 503, while the weights go up")
        t.join(180)
        self.assertFalse(t.is_alive(), "the load never ended")
        self.assertNotIsInstance(box.get("got"), Exception, str(box.get("got")))
        self.assertEqual(box["got"].port, port)
        self.note(f"port {port}: held from the first second, answering once the weights "
                  f"were up")


#: S5b, M1's: a program OUTSIDE the bootstrap that fills the card and holds it
#: until it is ended - the owner's to name, as a command line (JSON list, or a
#: plain string). Without it the card-full rows skip, whatever else is set
FILLER = os.environ.get("LLM_BOOTSTRAP_REAL_FILLER")


def _filler_argv() -> list:
    try:
        said = json.loads(FILLER)
    except ValueError:
        said = FILLER
    return said if isinstance(said, list) else str(said).split()


@unittest.skipUnless(FILLER, "the card is filled from outside only when asked: set "
                             "LLM_BOOTSTRAP_REAL_FILLER to a program that fills the card and "
                             "holds it until it is ended (M1, with the owner's go)")
class CardFullRows(CardCase):
    """S5b on the real card (M1, with the owner's go): the owner's example -
    VRAM full, a few programs closed, and the request went through - by
    itself. The model is up; a program outside the bootstrap fills the card;
    a request is sent, and waits, saying so; the program is closed; the
    request runs by itself - within the card's recover watch and a load of
    the model closing it. Once on the machine's lock, once through a
    coordinator: the same behaviour (the owner, 2026-09-27)."""

    RECOVER_WATCH_S = 2.0
    #: MEASURED on this machine (M1, 2026-09-28, an RTX 3090 under Windows'
    #: WDDM driver): a program outside the bootstrap cannot fill the card past
    #: the driver's own reserve - alone it stopped at 634 MiB free, and beside
    #: the model at 1.8 GB free; the driver refused it more while nvidia-smi
    #: still showed that much free. The 256 MiB default floor is never reached
    #: that way here, so these rows name a floor the filler can cross
    FLOOR_MIB = 2560

    def fill(self):
        """The program that fills the card, started by this row and ended by
        it through its own handle - never by name."""
        proc = subprocess.Popen(_filler_argv(), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    def full_then_freed(self, srv):
        ja = srv.submit(SHORT, max_tokens=8)
        self.assertEqual(srv.wait(ja, timeout=400)[0]["status"], "done")
        filler = self.fill()
        self.watch.mark("the card filled from outside the bootstrap")
        deadline = time.monotonic() + 120
        while (srv.health().get("pipeline") or {}).get("state") != "degraded":
            self.assertLess(time.monotonic(), deadline, "the card was never seen full")
            time.sleep(0.5)
        jb = srv.submit(COUNT, max_tokens=64)
        job, _ = srv.wait(jb, stage="waiting for the card's memory", timeout=120)
        prog = job.get("progress") or {}
        self.assertIn("Close something, and this runs again by itself.", prog.get("waiting_for"))
        self.note(f"while full: {prog.get('waiting_for')}")
        time.sleep(5)
        self.assertEqual(srv.job(jb)[0]["status"], "running", "it did not wait")
        filler.kill()
        filler.wait(30)
        closed = time.monotonic()
        self.watch.mark("the program that filled the card closed")
        job, _ = srv.wait(jb, until=("running",), stage="writing", timeout=400)
        ran = time.monotonic() - closed
        done, cands = srv.wait(jb, timeout=400)
        self.assertEqual(done["status"], "done", done.get("error"))
        self.note(f"from closing the program to writing again: {ran:.1f}s")
        return ran, srv.events()

    def test_on_the_machines_lock_the_request_runs_by_itself_once_the_program_is_closed(self):
        srv = self.server(self.settings(card_watch_s=1.0,
                                        card_recover_watch_s=self.RECOVER_WATCH_S,
                                        card_floor_mib=self.FLOOR_MIB))
        ran, events = self.full_then_freed(srv)
        load = next((e["load_seconds"] for e in events if e["event"] == "load_ok"), 0)
        self.assertLess(ran, self.RECOVER_WATCH_S + load + 30,
                        "closing to running again took longer than a read and a load")

    def test_through_a_coordinator_the_same(self):
        co = self.coordinator(ttl=3.0, hardware=self_hardware(), watch_s=1.0,
                              recover_watch_s=self.RECOVER_WATCH_S, floor_mib=self.FLOOR_MIB)
        srv = self.server(self.settings(), coordinator=co.url, name="full-app")
        ran, events = self.full_then_freed(srv)
        self.assertTrue([e for e in co.c.events if e["kind"] == "rental_restored"],
                        "the coordinator never said the card had room again")
        load = next((e["load_seconds"] for e in events if e["event"] == "load_ok"), 0)
        self.assertLess(ran, self.RECOVER_WATCH_S + load + 30)


# ------------------------------------------------------------------ M1's rows
#
# M1, with the owner's go (2026-09-28): what the stand-ins could only imitate,
# on the real card - two models of different engines side by side, their
# answers at once (S17), every process named to the coordinator within one
# beat, a lost rental ended whole on proof, and never two rentals' processes
# on the card at once.

#: M1's whisper, as JSON: {"server": whisper-server's program, "model": a
#: model file (a path of its own - it need not be in models_dir), "audio": a
#: WAV file to transcribe}. Without it M1's rows skip, whatever else is set
EARS = json.loads(os.environ.get("LLM_BOOTSTRAP_REAL_WHISPER") or "null")
#: what a whisper model is given room for beside the text model, before a row
#: that puts both on the card begins: large-v3 measured 4233 MiB up, and
#: 350-550 MiB more once it has answered
EARS_NEED_MIB = 4800


def write_pair(path: Path, models=("text", "ears"), **runner) -> Path:
    """Settings with the base file's text model (`text`, on llama.cpp) and
    M1's whisper model (`ears`) - either or both - each model on its own
    engine (`--backend real`), up to two on the card at once."""
    cfg, spec, _ = BASE
    r = {"backend": "real", "llama_server": cfg.runner["llama_server"],
         "whisper_server": EARS["server"],
         "models_dir": str(cfg.models_dir).replace("\\", "/"), "port": 0,
         "startup_timeout_s": 300.0, "health_poll_s": 0.5, "shutdown_timeout_s": 60.0,
         "idle_unload_s": 600.0, "card_patience_s": 600.0, "max_loads": 2, **runner}
    lines = ["[runner]"] + [f"{k} = {_toml(v)}" for k, v in r.items()]
    every = {
        "text": {"engine": "llamacpp", "label": "the text model", "file": spec.file,
                 "ctx": spec.ctx, "n_gpu_layers": spec.n_gpu_layers,
                 "cache_type_k": spec.cache_type_k, "cache_type_v": spec.cache_type_v,
                 "temperature": spec.temperature, "disable_thinking": spec.disable_thinking},
        "ears": {"engine": "whisper", "label": "the ears", "file": EARS["model"]},
    }
    for key in models:
        lines += ["", f"[models.{key}]"] + [f"{k} = {_toml(v)}" for k, v in every[key].items()]
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return path


def transcribe(srv, model: str = "ears") -> str:
    status, out = srv.post("/api/jobs", {
        "kind": "transcribe", "gen": {"model": model},
        "input": {"audio": EARS["audio"], "fields": {"language": "en", "temperature": 0.0}}})
    if status != 200:
        raise AssertionError(f"submit answered {status}: {out}")
    srv.case.watch.mark(f"{out['job_id']} submitted ({model}, a transcription)")
    return out["job_id"]


def open_session(srv, models, timeout: float = 400.0) -> str:
    """Open a pipeline that keeps *models* until it is closed, and wait until
    every one of them is up - failing at once, in its words, on a load that
    failed. -> the pipeline's id."""
    status, out = srv.post("/api/pipeline/open?wait_s=60",
                           {"models": list(models), "keep_s": 0, "label": "M1"}, timeout=90)
    if status != 200:
        raise AssertionError(f"open answered {status}: {out}")
    pid = out["pipeline"]["id"]
    srv.case.watch.mark(f"pipeline {pid} opened for {', '.join(models)}")
    deadline = time.monotonic() + timeout
    while True:
        srv.case.check_foreign()
        pl = srv.health().get("pipeline") or {}
        if set(models) <= set(pl.get("up") or []):
            srv.case.watch.mark(f"pipeline {pid}: {', '.join(models)} up")
            return pid
        if pl.get("why") and "could not be loaded" in str(pl.get("why")):
            raise AssertionError(f"a load failed: {pl['why']}")
        if time.monotonic() > deadline:
            raise AssertionError(f"the models were not up in {timeout:g}s: {pl}")
        time.sleep(0.25)


def close_session(srv, pid: str) -> None:
    status, out = srv.post("/api/pipeline/close", {"pipeline": pid}, timeout=90)
    srv.case.watch.mark(f"pipeline {pid} closed: {status}")
    if status != 200:
        raise AssertionError(f"close answered {status}: {out}")


def rental_processes(co) -> list:
    """The processes the coordinator's rental names now."""
    return ((co.c.state()[1].get("rental") or {}).get("processes")) or []


def span(srv, job: dict):
    """(started, ended, beside) of a job, from its request's own record - its
    `ended` line is written once the result is saved, a moment after the job
    says it ended: waited for, up to 10 s."""
    path = srv.ws / "requests" / job["correlation_id"] / "events.jsonl"
    events = until(lambda: (lambda ev: ev if any(e["event"] == "ended" for e in ev) else None)(
        read_events(path)), 10.0)
    if events is None:
        raise AssertionError(f"{job['id']}'s record never said it ended: {read_events(path)}")
    started = next(e for e in events if e["event"] == "started")
    ended = next(e for e in events if e["event"] == "ended")
    return started["ts"], ended["ts"], ended.get("beside") or []


@unittest.skipUnless(BASE and EARS, "M1 is run only when asked: set LLM_BOOTSTRAP_REAL_CARD and "
                                    "LLM_BOOTSTRAP_REAL_WHISPER (the owner's go for the card)")
class M1Case(CardCase):
    #: both models on the card at once is what these rows are about
    together_allowed = 2

    def setUp(self):
        super().setUp()
        # the text model's measured need, without the slack its own check keeps
        # (the card is checked again by the runner before each load)
        need = self.need_mib - int(ModelManager.VRAM_SLACK_GB * 1024) + EARS_NEED_MIB
        card = W.card_now()
        if card is None or card["free"] < need:
            self.skipTest(f"the card has no room for both models: {card}; they need about "
                          f"{need} MiB")

    def engines_up(self) -> dict:
        """{engine: [pid]} of this suite's model servers alive now."""
        out: dict = {}
        for s in W.model_servers():
            if s["ppid"] in OURS:
                out.setdefault(s["engine"], []).append(s["pid"])
        return out

    def all_gone(self, timeout: float = 30.0) -> float:
        t = time.monotonic()
        if until(lambda: not self.watch.ours_alive(), timeout) is None:
            raise AssertionError(f"still alive after {timeout:g}s: {self.watch.ours_alive()}")
        return time.monotonic() - t


class SideBySideRows(M1Case):
    def test_whisper_beside_the_text_model_on_the_card_answering_at_once(self):
        """The owner's case on the real card (S17): speech to text on one
        model while the other answers - two engines side by side in one
        session, each seen on the card from outside, their answers at once,
        and the card given back whole when the session closes."""
        srv = self.server(write_pair(self.dir / "settings.toml"), backend="real")
        pid = open_session(srv, ["text", "ears"])
        engines = self.engines_up()
        self.assertEqual(sorted(engines), ["llamacpp", "whisper"],
                         f"the watch sees {engines}: one of each engine expected")
        self.note(f"on the card together: llama-server {engines['llamacpp']}, "
                  f"whisper-server {engines['whisper']}; the card rose by "
                  f"{self.watch.used() - self.baseline} MiB")
        jt = srv.submit(COUNT, model="text", max_tokens=400)
        srv.wait(jt, until=("running",), stage="writing", timeout=120)
        je = transcribe(srv)
        ears, ears_c = srv.wait(je, timeout=120)
        text, _ = srv.wait(jt, timeout=300)
        self.assertEqual(ears["status"], "done", ears.get("error"))
        self.assertEqual(text["status"], "done", text.get("error"))
        t0, t1, t_beside = span(srv, text)
        e0, e1, e_beside = span(srv, ears)
        overlap = min(t1, e1) - max(t0, e0)
        self.assertGreater(overlap, 0.0, "the two answers did not run at once")
        self.assertIn(jt, [b["job"] for b in e_beside], e_beside)
        self.note(f"the transcription ran {e1 - e0:.2f}s, all of it beside the text answer "
                  f"({t1 - t0:.2f}s): overlap {overlap:.2f}s; heard: "
                  f"{(ears_c[0].get('text') or '').strip()[:80]!r}")
        close_session(srv, pid)
        self.note(f"closed: both off the card in {self.all_gone():.2f}s")


class RentalRows(M1Case):
    def test_every_process_is_named_to_the_coordinator_within_one_beat(self):
        """Every process of a rental - both engines' - is named on the
        coordinator's rental, with its proof on its command line, within one
        heartbeat of being up."""
        co = self.coordinator(ttl=3.0, hardware=self_hardware())
        srv = self.server(write_pair(self.dir / "settings.toml"), backend="real",
                          coordinator=co.url, name="m1-named")
        pid = open_session(srv, ["text", "ears"])
        up = time.monotonic()

        def named():
            procs = rental_processes(co)
            return procs if len(procs) >= 2 else None
        procs = until(named, co.ttl)
        self.assertIsNotNone(procs, f"not both named within {co.ttl:g}s: {rental_processes(co)}")
        took = time.monotonic() - up
        for p in procs:
            self.assertIn(p["proof"], W.command_line(p["pid"]) or "", p)
        self.assertEqual(sorted(p["engine"] for p in procs), ["llamacpp", "whisper"], procs)
        self.note(f"both named {took:.2f}s after both were up: "
                  + "; ".join(f"{p['engine']} pid {p['pid']} proof {p['proof']}" for p in procs))
        close_session(srv, pid)
        self.all_gone()

    def test_a_lost_rental_is_ended_whole(self):
        """The server holding a rental is killed where it stands: nothing of it
        says goodbye. The coordinator takes the rental back when its beats
        stop, and ends EVERY process it named - both engines' - on proof."""
        co = self.coordinator(ttl=3.0, hardware=self_hardware())
        srv = self.server_process(write_pair(self.dir / "settings.toml"), backend="real",
                                  coordinator=co.url, name="m1-lost")
        open_session(srv, ["text", "ears"])
        procs = until(lambda: len(rental_processes(co)) >= 2 and rental_processes(co),
                      co.ttl + 5)
        self.assertIsNotNone(procs, "the rental never named both processes")
        pids = [p["pid"] for p in procs]
        srv.kill()
        killed = time.monotonic()
        for p in pids:
            self.assertIsNotNone(self.gone(p, timeout=co.ttl + 60),
                                 f"pid {p} outlived its lost rental")
        took = time.monotonic() - killed
        kinds = [e.get("kind") for e in co.events()]
        self.note(f"server killed; both model servers ended {took:.2f}s later; the "
                  f"coordinator's record: {', '.join(k for k in kinds if 'reclaim' in k or 'lost' in k)}")

    def test_a_hung_holders_rental_is_ended_whole_by_the_coordinator(self):
        """The case the coordinator is for: an application HANGS - alive, its
        models still on the card, its heartbeats stopped. Its job object never
        fires (nothing died), so only the coordinator can free the card: the
        rental lapses, and every process it named - both engines' - is ended
        on the proof on its command line."""
        import ctypes
        co = self.coordinator(ttl=3.0, hardware=self_hardware())
        srv = self.server_process(write_pair(self.dir / "settings.toml"), backend="real",
                                  coordinator=co.url, name="m1-hung")
        open_session(srv, ["text", "ears"])
        procs = until(lambda: len(rental_processes(co)) >= 2 and rental_processes(co),
                      co.ttl + 5)
        self.assertIsNotNone(procs, "the rental never named both processes")
        k32, nt = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("ntdll")
        k32.OpenProcess.restype = ctypes.c_void_p
        handle = k32.OpenProcess(0x0800, False, srv.proc.pid)     # PROCESS_SUSPEND_RESUME
        self.assertTrue(handle, f"the server could not be opened: {ctypes.get_last_error()}")

        def thaw():
            nt.NtResumeProcess(ctypes.c_void_p(handle))
            k32.CloseHandle(ctypes.c_void_p(handle))
        self.assertEqual(nt.NtSuspendProcess(ctypes.c_void_p(handle)), 0)
        self.addCleanup(thaw)                                     # before its kill (LIFO)
        hung = time.monotonic()
        self.watch.mark(f"server process {srv.proc.pid} SUSPENDED: alive, and silent")
        for p in procs:
            self.assertIsNotNone(self.gone(p["pid"], timeout=co.ttl + 60),
                                 f"{p['engine']} pid {p['pid']} outlived the hung rental")
        took = time.monotonic() - hung
        self.assertIsNone(srv.proc.poll(), "the hung server itself died: not the case here")
        said = [e for e in co.events() if e.get("kind") == "rental_reclaimed"]
        self.assertTrue(said, "the coordinator never took the rental back")
        self.note(f"hung; both model servers ended {took:.2f}s later, by the coordinator: "
                  f"{said[-1].get('detail')}")

    def test_never_two_rentals_processes_on_the_card_at_once(self):
        """Two applications under one coordinator: the first holds a session
        with the text model; the second asks for its whisper model and waits;
        it gets the card only once the first's process is gone. The watch
        from outside never sees the two rentals' processes at once."""
        self.together_allowed = 1
        co = self.coordinator(ttl=3.0, hardware=self_hardware())
        a = self.server(write_pair(self.dir / "a.toml", models=("text",)), backend="real",
                        coordinator=co.url, name="m1-first", ws="ws-a")
        b = self.server(write_pair(self.dir / "b.toml", models=("ears",)), backend="real",
                        coordinator=co.url, name="m1-second", ws="ws-b")
        pa = open_session(a, ["text"])
        got: dict = {}

        def second():
            try:
                got["pid"] = open_session(b, ["ears"], timeout=400)
            except Exception as e:                          # noqa: BLE001 - said below
                got["error"] = e
        t = threading.Thread(target=second, daemon=True)
        t.start()
        time.sleep(8)
        self.assertNotIn("pid", got, "the second application got the card while the first held it")
        self.assertNotIn("whisper", self.engines_up())
        close_session(a, pa)
        t.join(420)
        self.assertNotIn("error", got, got.get("error"))
        self.assertIn("pid", got)
        self.assertEqual(list(self.engines_up()), ["whisper"])
        self.note(f"the second application's whisper came up only after the first's model "
                  f"was gone; together at most {self.watch.peak_together}")
        close_session(b, got["pid"])
        self.all_gone()


def self_hardware():
    """The coordinator's machine: this one's card, read through nvidia-smi."""
    import coordinator as C
    return C.hardware_for("this-machine")


def main(argv) -> int:
    """unittest's own command line, and `--repeat N`: each named row or class
    (or the whole suite) N times over, one run after another."""
    repeat = 1
    if "--repeat" in argv:
        i = argv.index("--repeat")
        repeat = int(argv[i + 1])
        del argv[i:i + 2]
    names = [a for a in argv if not a.startswith("-")]
    loader = unittest.TestLoader()
    here = sys.modules[__name__]
    suite = unittest.TestSuite()
    for _ in range(max(1, repeat)):
        suite.addTests(loader.loadTestsFromNames(names, here) if names
                       else loader.loadTestsFromModule(here))
    print(f"evidence: {OUT}", flush=True)
    result = unittest.TextTestRunner(verbosity=2, failfast="--failfast" in argv).run(suite)
    # the tally, counted in rows, as every suite of this part ends: the gate
    # reads it, never unittest's own count
    from _suites import tally
    passed, failed, total = tally(types.SimpleNamespace(test=suite, result=result))
    print(f"{passed} passed, {failed} failed, {total} total", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
