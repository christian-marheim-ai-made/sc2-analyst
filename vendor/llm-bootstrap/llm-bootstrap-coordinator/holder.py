"""A holder: the client half of asking the coordinator for the card.

    with Holder("http://127.0.0.1:7071", "writer-app", "demo-7b") as rental:
        ...use the card...

    python holder.py --url http://127.0.0.1:7071 --name writer-app --model demo-7b
                     [--model more ...] [--engine llamacpp] [--need-mib N]
                     [--class responsive|scheduled] [--label L] [--patience S] [--hold S]

It asks for a ticket ONCE, then waits on it - each wait held open by the
coordinator, and answered the moment there is news - and claims the card with
the ticket's token the moment it is offered. It keeps its rental alive with
heartbeats, by the token alone. It gives the card back on every way out. Every
process it puts on the card, `running(pid, proof)` names - the pid, and the id
of the load that started it, which its command line carries and only this
holder put there - so the coordinator can end it, on that proof, if this
holder hangs; `stopped(pid)` takes one off the list. Every heartbeat carries
the whole list, and one goes at once whenever it changes.

IT SAYS ITS WORK, so the coordinator can watch it for a hang (PLAN-4 D3), as
the server's card source does: `work`, the application's own function ->
{working, progress, deadline_s, memory_errors, memory_error} - whether a model
is answering, a count only real work moves (tokens, answers), a one-piece
answer's deadline, and the memory errors its models' logs said. Every
heartbeat carries it, its counts counted from the grant. A holder given no
`work` says nothing of it, and is never found hung.

HOW LONG IT WAITS is its own choice: `patience_s`, None for as long as it
takes. When the patience runs out it gives its ticket up and says so,
`card_patience` - and past it asks nothing more: a ticket that ends, a claim
the coordinator does not answer, sends it back to asking or claiming only
within its patience. A patience of 0 is now or not at all: a ticket not offered
the card at once is given up, `card_busy`. Neither names who has the card: the
coordinator tells no application about another. Refused for the memory - an
ask that could never fit (`never_fits`), a claim the card has no room for now
(`no_room`) - it hears it at once, in numbers, and asks no more.

IT NEVER INFERS LOSS FROM SILENCE. If the coordinator cannot be reached, the
holder keeps its rental and keeps trying: the card does not know the
coordinator went away, and neither should the work running on it. It learns
it lost the card only from an answer - 409 with a reason - and then it stops.

THE TOKEN is the ticket's only key: it is in the ask's answer and nowhere
else, and this holder keeps it in memory and sends it to the coordinator
alone.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "llm-bootstrap-contract"))
import ids                                                      # noqa: E402

#: how much longer than a wait's own hold its answer is waited for
WAIT_MARGIN_S = 5.0


class Refused(Exception):
    """The card was not had: the coordinator refused the ask (its own reason),
    the patience ran out (`card_patience`), or the card was not free now and
    this holder would not wait (`card_busy`). `reason` says which."""

    def __init__(self, message: str, reason=None):
        super().__init__(message)
        self.reason = reason


def call(base: str, method: str, path: str, body=None, timeout: float = 5.0):
    """-> (status, body). A coordinator that cannot be reached raises OSError,
    and what that means is the caller's decision, not this function's."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read().decode("utf-8"))


class Holder:
    def __init__(self, base: str, name: str, models, *, klass=None, label=None, for_=None,
                 actor=None, patience_s: float | None = None, retry_s: float = 0.2,
                 wait_s: float = 30.0, engine: str = "llamacpp", need_mib=None, work=None):
        """`models` is what the rental will run: a model's key, a list of keys,
        or a list of {key, engine, need_mib}. `actor` is who is asking - its
        identity to the coordinator, minted here when not given; `name` is only
        what it calls itself. `klass` is responsive (None) or scheduled. `for_`
        is the correlation id of the request the rental is for. `work` says
        what the rental's work does (see the module's docstring)."""
        self.base, self.name = base.rstrip("/"), name
        wanted = [models] if isinstance(models, (str, dict)) else list(models)
        self.models = [m if isinstance(m, dict) else
                       {"key": str(m), "engine": engine, "need_mib": need_mib} for m in wanted]
        self.actor, self.for_ = actor or ids.mint_actor("srv"), for_
        self.klass, self.label = klass, label
        self.patience_s, self.retry_s, self.wait_s = patience_s, retry_s, wait_s
        self.ticket: dict | None = None  # the ticket, as the last answer had it
        self.rental: dict | None = None  # the rental, once claimed
        self.lost: str | None = None     # the reason, once an ANSWER says the rental is over
        self.unreachable = 0             # heartbeats that could not reach the coordinator
        self.asks = self.waits = 0       # counted as they are made
        self.processes: dict = {}        # pid -> a process the rental put on the card
        self._token: str | None = None
        self._stop = threading.Event()
        self._beater: threading.Thread | None = None
        #: THE WATCH (D3): a function -> {working, progress, deadline_s,
        #: memory_errors, memory_error}, the application's own counts - and
        #: the counts at the grant, which every beat counts from
        self.work = work
        self._base = (0, 0)

    def _pause(self, left) -> None:
        """The coordinator did not answer: wait a moment before trying again."""
        time.sleep(self.retry_s if left is None else max(0.0, min(self.retry_s, left)))

    def _give_up(self) -> None:
        """The ticket is no longer wanted: tell the coordinator, so the card is
        never offered to a holder that has stopped waiting. Never raises."""
        if self._token is not None:
            try:
                call(self.base, "POST", "/card/cancel", {"token": self._token}, timeout=2.0)
            except OSError:
                pass                                  # it is dropped once nobody waits on it
        self.ticket = self._token = None

    def _within(self, give_up) -> None:
        """PAST ITS PATIENCE A HOLDER ASKS NOTHING MORE - no ask, no claim, no
        wait: it gives its ticket up, and says so (`card_patience`). Checked
        on every way back to asking or claiming: a claim or a wait answered for
        a ticket that ended, a claim the coordinator did not answer. `give_up`
        is when the patience ends (monotonic); None, never."""
        if give_up is not None and time.monotonic() >= give_up:
            self._give_up()
            raise Refused(f"no card within its patience of {self.patience_s:g}s "
                          f"(card_patience)", "card_patience")

    def acquire(self) -> dict:
        give_up = None if self.patience_s is None else \
            time.monotonic() + max(0.0, float(self.patience_s))
        body = {"holder": self.name, "actor": self.actor, "models": self.models}
        for key, value in (("class", self.klass), ("label", self.label), ("for", self.for_)):
            if value is not None:
                body[key] = value
        self.ticket = self._token = None
        while True:
            left = None if give_up is None else give_up - time.monotonic()
            if self.ticket is None:
                self.asks += 1
                try:
                    status, answer = call(self.base, "POST", "/card/ask", body)
                except OSError:
                    status, answer = None, None       # away: wait, ask again
                if status in (400, 409):
                    # a body it cannot take - or more than the whole card
                    # (never_fits): asking again would never change it
                    raise Refused(answer["error"], answer.get("reason"))
                if status == 200:
                    self.ticket, self._token = answer["ticket"], answer["token"]
            if self.ticket is not None and self.ticket["state"] == "offered":
                try:
                    status, answer = call(self.base, "POST", "/card/claim",
                                          {"ticket_id": self.ticket["id"], "token": self._token})
                except OSError:
                    # the offer lasts its window: claimed again after a moment -
                    # within the patience
                    self._within(give_up)
                    self._pause(None if give_up is None else give_up - time.monotonic())
                    continue
                if status == 200:
                    self.rental, self.lost = answer["rental"], None
                    got = self._work_now()
                    self._base = (got["progress"], got["memory_errors"]) if got else (0, 0)
                    self._stop.clear()
                    self._beater = threading.Thread(target=self._beat, daemon=True)
                    self._beater.start()
                    return self.rental
                if status != 409 or answer["reason"] in ("no_room", "never_fits"):
                    # refused for the memory, in numbers: the card has not the
                    # room now, or never will - the holder hears it at once
                    raise Refused(answer["error"], answer.get("reason"))
                if answer["reason"] == "not_offered":
                    self.ticket = {**self.ticket, "state": "waiting"}
                else:
                    self.ticket = self._token = None  # the ticket ended: ask again,
                    self._within(give_up)             # ...within the patience
                continue
            if self.ticket is not None and self.patience_s is not None and self.patience_s <= 0:
                self._give_up()
                raise Refused("the card is busy, and this holder would not wait for it "
                              "(card_busy)", "card_busy")
            self._within(give_up)
            if self.ticket is None:
                self._pause(left)
                continue
            hold = self.wait_s if left is None else min(self.wait_s, left)
            self.waits += 1
            try:
                status, answer = call(self.base, "POST", "/card/wait",
                                      {"ticket_id": self.ticket["id"], "token": self._token,
                                       "wait_s": hold}, timeout=hold + WAIT_MARGIN_S)
            except OSError:
                self._pause(left)
                continue
            if status == 200:
                self.ticket = answer["ticket"]
            elif status == 409:
                self.ticket = self._token = None      # it ended - dropped, say: ask again,
                self._within(give_up)                 # ...within the patience
            else:
                raise Refused(answer["error"], answer.get("reason"))

    def _beat(self) -> None:
        interval = max(0.02, (self.rental["expires_at"] - self.rental["granted_at"]) / 3)
        # THE TIMEOUT IS THE INTERVAL. On Windows a connection to a port nobody
        # is listening on takes about two seconds to be refused, so without a
        # bound every heartbeat during a coordinator outage blocks for two
        # seconds - and a holder with a short lifetime would lose its rental
        # after the coordinator came back, purely because the platform was
        # slow to say "nobody is there". Bounded, an outage costs one
        # interval per attempt, and the cadence holds.
        while not self._stop.wait(interval) and not self.lost:
            self._heartbeat(interval)

    def _heartbeat(self, timeout: float) -> None:
        # one snapshot of the list: `running` and `stopped` change it from the
        # caller's thread while this one beats
        body = {"token": self._token, "processes": [dict(p) for p in list(self.processes.values())],
                **self.said()}
        try:
            status, answer = call(self.base, "POST", "/card/heartbeat", body, timeout=timeout)
        except OSError:
            self.unreachable += 1
            return                                    # silence is not an answer
        if status == 200:
            self.rental = answer["rental"]
        elif status == 409:
            self.lost = answer["reason"]

    def _work_now(self):
        """What `work` says, whole - or None: there is none, or it could not
        say. Never fails a heartbeat."""
        if self.work is None:
            return None
        try:
            got = self.work()
            return {"working": bool(got.get("working", False)),
                    "progress": int(got.get("progress") or 0),
                    "deadline_s": None if got.get("deadline_s") is None
                    else float(got["deadline_s"]),
                    "memory_errors": int(got.get("memory_errors") or 0),
                    "memory_error": got.get("memory_error")}
        except Exception:                                   # noqa: BLE001
            return None

    def said(self) -> dict:
        """What this rental's heartbeat says of its work (D3): whether it is
        working, its progress and its memory errors since the grant - the
        newest line with them - and a one-piece answer's deadline. {} when
        `work` says nothing: a heartbeat without them says it is not working."""
        got = self._work_now()
        if got is None:
            return {}
        base_p, base_e = self._base
        errors = max(0, got["memory_errors"] - base_e)
        line = got["memory_error"] if errors else None
        return {"working": got["working"], "progress": max(0, got["progress"] - base_p),
                "deadline_s": got["deadline_s"], "memory_errors": errors,
                "memory_error": None if line is None else str(line)}

    def running(self, pid: int, proof: str, model=None, engine=None, port=None,
                mib=None) -> None:
        """Name a process this rental put on the card, and the proof its
        command line carries - the id of the load that started it - with the
        model it runs (by default the first this holder asked for), its
        engine, port and memory. Said at once, and on every heartbeat after:
        the heartbeat carries the whole list."""
        first = self.models[0]
        self.processes[int(pid)] = {
            "pid": int(pid), "proof": str(proof), "model": str(model or first["key"]),
            "engine": str(engine or first["engine"]),
            "port": None if port is None else int(port), "mib": None if mib is None else int(mib)}
        self._say_now()

    def stopped(self, pid: int) -> None:
        """A process this rental put on the card is gone: off the list, said
        at once."""
        self.processes.pop(int(pid), None)
        self._say_now()

    def _say_now(self) -> None:
        if self.rental is not None and not self.lost:
            self._heartbeat(max(1.0, self.retry_s))

    def release(self):
        """Give the card back. -> (status, body), or None if the coordinator
        could not be reached - in which case the rental simply expires and the
        coordinator takes the card back on its own."""
        self._stop.set()
        if self._beater:
            self._beater.join(timeout=2)
        if self.rental is None or self.lost:
            return None
        try:
            return call(self.base, "POST", "/card/release", {"token": self._token})
        except OSError:
            return None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--model", required=True, action="append",
                    help="a model the rental will run; repeat for more")
    ap.add_argument("--engine", default="llamacpp", help="the engine that runs them")
    ap.add_argument("--need-mib", type=int, default=None, help="the memory each needs, if known")
    ap.add_argument("--class", dest="klass", default=None, choices=("responsive", "scheduled"))
    ap.add_argument("--label", default=None)
    ap.add_argument("--actor", default=None, help="who is asking; minted when not given")
    ap.add_argument("--for", dest="for_", default=None,
                    help="the correlation id of the request the rental is for")
    ap.add_argument("--patience", type=float, default=None,
                    help="seconds to wait for the card; 0 is now or not at all; omit to wait "
                         "for as long as it takes")
    ap.add_argument("--hold", type=float, default=None,
                    help="seconds to hold the card; omit to hold until killed")
    a = ap.parse_args()
    holder = Holder(a.url, a.name, a.model, klass=a.klass, label=a.label, for_=a.for_,
                    actor=a.actor, patience_s=a.patience, engine=a.engine, need_mib=a.need_mib)
    try:
        with holder as rental:
            print(f"holding {rental['id']}", flush=True)
            if a.hold is None:
                threading.Event().wait()
            time.sleep(a.hold)
    except Refused as e:
        print(f"refused ({e.reason}): {e}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
