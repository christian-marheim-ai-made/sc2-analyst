"""The coordinator's client half: what a server needs to rent the card, and
the ports it runs on.

    client = CoordinatorClient("http://127.0.0.1:7071", actor, name="writer-app")
    card = CoordinatorCard(client)            # the runner's card source
    ports = CoordinatorPorts(client)          # the runner's port source

It is written against the CONTRACT's routes (`coordinator/*`), not against
the coordinator's code: every body it sends is checked against its route's
request shape before it goes, and every answer against the shape the route
declares for that status. The two parts share the contract, never code.

ONE ACTOR. A client speaks for one actor - the server's own id - and every
ticket, rental and port it holds is held under it.

THE CARD BY TICKET, AND NOTHING POLLS. A take asks for a ticket ONCE, then
waits on it - each wait held open by the coordinator and answered the moment
there is news - and claims the card with the ticket's token the moment it is
offered. A server waiting an hour makes one request per hold (30 s), never
one a second. The token is the ticket's only key: it lives here, in memory,
and goes to the coordinator alone - never into a log, a record or an answer.

A HOLDER NEVER INFERS LOSS FROM SILENCE. A coordinator that cannot be reached
changes nothing about what is held: the card does not know the coordinator
went away, and neither should the work running on it. Loss is learnt only
from an ANSWER - 409 `reclaimed`, `released`, `unknown_ticket`... - and then
the card source says so at once (`on_lost`), because another holder may be
loading onto the card now. An answer the contract does not declare is not an
answer: it is treated as silence, and said in the log.

A RENEWAL SAYS WHETHER THE CARD IS WANTED - another ticket is in line - and
nothing more. The card source keeps the last word (`wanted`) and tells the
runner each time a renewal says so (`on_wanted`): the queue's own pipeline
lets the card go at once when its work is done and the card is wanted.

HEARTBEATS ARE BOUNDED BY THEIR INTERVAL. On Windows a connection to a port
nobody listens on takes about two seconds to be refused; unbounded, every
heartbeat during an outage would block that long, and a short-lived rental
could lapse after the coordinator came back. So a heartbeat waits at most one
interval: an outage costs one interval per attempt, and the cadence holds.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONTRACT = HERE.parent / "llm-bootstrap-contract"
if str(CONTRACT) not in sys.path:
    sys.path.insert(0, str(CONTRACT))
import ids                                                      # noqa: E402
import memory as MEM                                            # noqa: E402  (the amend's bound)
import validate as V                                            # noqa: E402

_SHAPES, _ALL = V.load(CONTRACT)
ROUTES = {r["id"]: r for key, r in _ALL.items() if key.startswith("coordinator/")}
#: the version of the contract this server runs - the one its coordinator's
#: /up is compared with (PLAN-4 D13)
CONTRACT_VERSION = V.contract_version(CONTRACT)

#: the reasons a heartbeat's answer gives when the rental is over - and only these
RENTAL_OVER = tuple(ROUTES["coordinator.card.heartbeat"]["responses"]["409"]["object"]
                    ["reason"]["enum"])
PORT_OVER = ("reclaimed", "released", "unknown_port", "not_yours")
#: how much longer than a wait's own hold its answer is waited for
WAIT_MARGIN_S = 5.0
#: how long an amend's answer is waited for: the coordinator may wait for a
#: memory just given back (its `amend_settle_s`, 5 s unless it was started
#: with another) and read the card several times first - the contract's own
#: bound, which every coordinator's settle is held within (`memory.py`)
AMEND_TIMEOUT_S = MEM.AMEND_ANSWER_S


class Unreachable(Exception):
    """The coordinator did not answer - or answered something the contract does
    not declare, which is no answer either. Never a reason to give anything up."""


class CardRefused(Exception):
    """The card was not had, for a reason the caller must hear: the patience
    ran out, the card was not free and the request would not wait, the wait
    was cancelled, the server is stopping, or the coordinator refused the ask
    itself. `reason` is the request's own reason for ending, when it has one -
    `card_patience` or `card_busy`; `no_room` or `never_fits`, the card's
    memory refusing what it asked for - as the contract declares them on a
    job. None names who has the card: the coordinator tells nobody."""

    def __init__(self, message: str, reason=None, room=None):
        super().__init__(message)
        self.reason = reason
        #: S18: the room scheduled work waits on, when its take was passed
        #: over for room and its work waits in its queue - its place in line
        #: kept (`CoordinatorCard.look`) - else None
        self.room = room


class CoordinatorClient:
    """The HTTP half, and the one heartbeat that keeps everything held alive."""

    def __init__(self, base: str, actor: str, name: str = "llm-bootstrap-server", *,
                 timeout_s: float = 5.0, log=None, clock=time.time):
        if not (ids.matches("actor", actor) and actor.startswith("srv-")):
            raise ValueError(f"a coordinator client speaks for a server's actor (srv-), "
                             f"not {actor!r}")
        self.base, self.actor, self.name = str(base).rstrip("/"), actor, str(name)
        self.timeout_s = float(timeout_s)
        self._log = log or (lambda s: print(s, flush=True))
        self._clock = clock
        #: None until anything was asked; then whether the LAST contact worked
        self.reachable: bool | None = None
        self.last_contact_at: float | None = None
        #: the version of the contract the coordinator runs, as its /up said -
        #: None until it was asked
        self.coordinator_version: str | None = None
        self._lock = threading.Lock()
        self._card = None                   # the card source holding a rental, if any
        self._ports: dict = {}              # port -> the lifetime it was granted for
        self._on_port_lost = {}             # port -> what to tell when it is over
        self._stop = threading.Event()
        #: set to send the next heartbeat now, not an interval from now
        self._now = threading.Event()
        self._beater: threading.Thread | None = None
        #: set by `stop_asking` and `close`: a client that closed asks for nothing more
        self.closed = False

    # ---------------------------------------------------------------- the wire

    def call(self, route_id: str, body=None, timeout: float | None = None):
        """-> (status, answer). Raises Unreachable for silence, and for an answer
        the contract does not declare for this route."""
        route = ROUTES[route_id]
        if body is not None:
            wrong = V.check(body, route["request"], _SHAPES)
            if wrong:
                # a bug on THIS side: the coordinator would refuse it anyway
                raise ValueError(f"{route_id}: not a body the contract allows: {wrong[0]}")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + route["path"], data=data,
                                     method=route["method"],
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout_s) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            with e:                                         # an answer is a response: closed
                status, raw = e.code, e.read()
        except OSError as e:                                # refused, reset, timed out
            self.reachable = False
            raise Unreachable(f"{self.base} did not answer: {e}") from None
        declared = route["responses"].get(str(status))
        try:
            answer = json.loads(raw.decode("utf-8"))
        except ValueError:
            answer = None
        wrong = ["no shape declared"] if declared is None else V.check(answer, declared, _SHAPES)
        if wrong:
            self.reachable = False
            self._say(f"coordinator: {route_id} answered {status}, which the contract "
                      f"does not declare ({wrong[0]}) - taken as no answer")
            raise Unreachable(f"{self.base} answered {status} outside the contract")
        self.reachable, self.last_contact_at = True, self._clock()
        return status, answer

    def probe(self) -> bool:
        """Is the coordinator there? Asked once, at start, so health can say -
        at its own route, which answers that and the version of the contract
        the coordinator runs (kept as `coordinator_version`, and said once
        when it is not this server's) and nothing more: the operator's /state
        names every application, and no answer to one names another."""
        try:
            _, up = self.call("coordinator.up")
        except Unreachable:
            return False
        theirs = up["contract_version"]
        if theirs != CONTRACT_VERSION and theirs != self.coordinator_version:
            # said once per version: an answer of theirs this server cannot
            # read is then no answer (`call`), and this line says why
            self._say(f"coordinator: {self.base} runs contract {theirs}, and this server "
                      f"contract {CONTRACT_VERSION}: an answer the two do not share is taken "
                      f"as no answer")
        self.coordinator_version = theirs
        return True

    def word(self) -> str:
        """Health's word for the coordinator: the last contact's result."""
        return "reachable" if self.reachable else "unreachable"

    def _say(self, line: str) -> None:
        try:
            self._log(line)
        except Exception:                                   # noqa: BLE001
            pass

    # ------------------------------------------------------------ holding

    def hold_rental(self, card) -> None:
        with self._lock:
            self._card = card
        self._ensure_beating()

    def drop_rental(self, card) -> None:
        with self._lock:
            if self._card is card:
                self._card = None

    def hold_port(self, port: int, ttl: float, on_lost=None) -> None:
        with self._lock:
            self._ports[int(port)] = float(ttl)
            self._on_port_lost[int(port)] = on_lost
        self._ensure_beating()

    def drop_port(self, port: int) -> None:
        with self._lock:
            self._ports.pop(int(port), None)
            self._on_port_lost.pop(int(port), None)

    def holding(self) -> dict:
        with self._lock:
            card = self._card
            return {"rental": dict(card.turn) if card is not None and card.turn else None,
                    "ports": sorted(self._ports)}

    def _ensure_beating(self) -> None:
        with self._lock:
            if self._beater is not None and self._beater.is_alive():
                return
            self._stop.clear()
            self._beater = threading.Thread(target=self._beat_loop, daemon=True,
                                            name="coordinator-heartbeat")
            self._beater.start()

    def _interval(self):
        """A third of the shortest lifetime held, or None when nothing is held."""
        with self._lock:
            lives = list(self._ports.values())
            card = self._card
        if card is not None and card.turn and card.life:
            lives.append(card.life)
        return max(0.02, min(lives) / 3) if lives else None

    def beat_now(self) -> None:
        """Send the next heartbeat at once: what is held has something new to
        say (the list of processes a rental put on the card changed)."""
        self._now.set()

    def _beat_loop(self) -> None:
        while not self._stop.is_set():
            interval = self._interval()
            if interval is None:
                if self._stop.wait(0.05):
                    return
                continue
            self._now.wait(interval)
            self._now.clear()
            if self._stop.is_set():
                return
            self._beat_once(interval)

    def _beat_once(self, interval: float) -> None:
        with self._lock:
            card = self._card
            ports = dict(self._on_port_lost)
        # one look at what the card holds: a give-back on another thread may
        # clear it between two looks
        rental, token = (card.turn, card._token) if card is not None else (None, None)
        if rental and token:
            # the whole list, every time - [] included: it replaces the one
            # the coordinator has, so a beat lost on the way is healed by this
            # - and what the rental's work says (D3): the coordinator watches it
            body = {"token": token, "processes": card.named(), **card.said()}
            try:
                status, answer = self.call("coordinator.card.heartbeat", body, timeout=interval)
            except Unreachable:
                status, answer = None, None                 # silence is not an answer
            if status == 200:
                card._renewed(answer["rental"], answer["wanted"])
            elif status == 409 and answer["reason"] in RENTAL_OVER:
                card._over(answer["reason"], answer["error"])
        for port, on_lost in ports.items():
            try:
                status, answer = self.call("coordinator.port.heartbeat",
                                           {"port": port, "actor": self.actor},
                                           timeout=interval)
            except Unreachable:
                continue
            if status == 409 and answer["reason"] in PORT_OVER:
                self.drop_port(port)
                self._say(f"coordinator: port {port} is no longer held - "
                          f"{answer['reason']}: {answer['error']}")
                if on_lost is not None:
                    try:
                        on_lost(port, answer["reason"])
                    except Exception:                       # noqa: BLE001
                        pass

    def stop_asking(self) -> None:
        """Ask for nothing more: a wait for the card under way ends at once, and
        gives its ticket up. What is held stays held - and heartbeated - until
        `close` gives it back: a stopping server's model is on the card until
        the server has taken it off."""
        self.closed = True

    def close(self) -> None:
        """Stop the heartbeat, then give back whatever is still held. Nothing
        here waits on a coordinator that is not there: what cannot be given
        back expires, and the coordinator takes it back on its own. A wait for
        the card still under way ends: a closed client asks for nothing more."""
        self.closed = True
        self._stop.set()
        self._now.set()
        if self._beater is not None:
            self._beater.join(timeout=2)
        with self._lock:
            card, ports = self._card, list(self._ports)
        if card is not None:
            card.give_back()
        for port in ports:
            try:
                self.call("coordinator.port.release", {"port": port, "actor": self.actor},
                          timeout=min(2.0, self.timeout_s))
            except Unreachable:
                pass
            self.drop_port(port)


class PortRefused(Exception):
    """No port was granted: the registry refused (full), or it could not be
    reached - and a load never listens on a port nobody granted it."""


class CoordinatorPorts:
    """The runner's port source, with a coordinator: a port from the registry
    for each load (`take`), kept alive by the one heartbeat while the load
    holds it, and given back with the load (`give_back`).

    NEVER A GUESS. A registry that cannot be reached, or that is full, fails
    the load in so many words; the load never falls back to a port nobody
    granted it. A port the registry takes back while its llama-server still
    listens on it is safe - the registry never hands out a port something is
    bound to - and it is said (`on_lost`), never acted on."""

    def __init__(self, client: CoordinatorClient, purpose: str = "model-server",
                 ttl_s: float | None = None, on_lost=None):
        self.client, self.purpose, self.ttl_s = client, str(purpose), ttl_s
        self.on_lost = on_lost

    def take(self, model_key: str, load=None) -> int:
        """A port for the load *load* - held under its own name: the registry
        gives one port per actor per purpose, and models side by side are
        loads side by side, so the purpose carries the load's id
        (`model-server load-...`), and no two loads are ever handed one
        port."""
        purpose = f"{self.purpose} {load}" if load else self.purpose
        body = {"actor": self.client.actor, "purpose": purpose}
        if self.ttl_s is not None:
            body["ttl_s"] = float(self.ttl_s)
        try:
            status, answer = self.client.call("coordinator.port.request", body)
        except Unreachable:
            raise PortRefused(f"the coordinator at {self.client.base} does not answer") from None
        if status != 200:
            raise PortRefused(answer["error"])              # full, or refused: its own words
        lease = answer["port"]
        # the lifetime is fixed at the grant, as a rental's is
        self.client.hold_port(lease["port"], max(0.01, lease["expires_at"] - lease["granted_at"]),
                              on_lost=self._lost)
        return int(lease["port"])

    def _lost(self, port, reason) -> None:
        if self.on_lost is not None:
            self.on_lost(port, reason)

    def give_back(self, port: int) -> None:
        """Never raises: a port that cannot be given back expires."""
        self.client.drop_port(int(port))
        try:
            self.client.call("coordinator.port.release",
                             {"port": int(port), "actor": self.client.actor},
                             timeout=min(2.0, self.client.timeout_s))
        except Unreachable:
            pass


class CoordinatorCard:
    """The runner's card source, with a coordinator: a RENTAL, had by ticket.

    `take` asks for a ticket once and waits on it - never asking again - and
    claims the card with the ticket's token the moment it is offered: never a
    load without a claim. While it waits it says only that it waits
    (`on_wait`): the coordinator tells no application who holds the card. A
    cancel, or the server stopping, ends the wait at once and gives the ticket
    up. The patience bounds it - the request's own, else the server's
    default - and when it runs out the ticket is given up and the take refused
    `card_patience`. A patience of 0 is now or not at all: a ticket not offered
    the card at once is given up, `card_busy`. `give_back` releases the rental.
    `lost` is the reason an ANSWER gave when the rental ended under it, and
    `on_lost` hears it the moment it arrives.

    A REQUEST'S TERMS come with the take they are for, BY HAND (`terms`, a
    `runner.Terms`): its class and its patience (None: responsive, and the
    server's own). The runner's adapter makes them from the job - an
    interactive job asks as responsive, a batch job as scheduled, with the
    job's own patience - and a pipeline from its own class; a pipeline takes
    the card on a thread of its own, where nothing left on a caller's thread
    could reach. A take refused for a reason of the request's own raises
    with it (`CardRefused.reason`), and the runner says which on the terms.

    WANTED. Each renewal's answer says whether another ticket is in line for
    the card: kept as `wanted`, and told to `on_wanted` each time it is so.

    ONE PLACE IN LINE. A server holds at most one ticket: a take while another
    take of this card is under way waits for it, and a take while this card
    holds a rental is handed that rental - what may load on it is the
    manager's to decide.

    EVERY PROCESS THE RENTAL PUT ON THE CARD IS NAMED. `running(pid, proof,
    model, ...)` adds one the moment it exists - its pid, the proof its
    command line carries (the id of the load that started it), its model,
    engine and port - and `stopped(proof)` takes it off once its stop has
    returned. Every heartbeat carries the whole list (`named()`), which
    replaces the one the coordinator has, and one goes at once whenever the
    list changes - so a coordinator can end each, on its proof, if this
    server hangs while it holds the card, and a beat lost on the way is
    healed by the next. A process whose stop failed is never taken off: it
    may still be on the card. A pid named again is one process: two that are
    alive never share a pid, so the one named before it has gone.

    THE COORDINATOR WATCHES THE RENTAL (PLAN-4 D3). Every heartbeat also says
    what the rental's work does (`said`): what the runner hands it to ask
    (`work`, a function: whether a model is answering, its progress - tokens
    and answers, which only real work moves - a one-piece answer's deadline,
    the memory errors its models' logs said), its counts from the rental's
    grant. A rental that says it is working and does not move is taken back
    as a crashed one is, and heard as a loss. Each renewal's rental says
    whether it is DEGRADED - the card over-full - and `on_degraded` hears it
    each time that changes: the words and numbers, or None once the card has
    room again. An amend refused `degraded` puts nothing on the card.

    `turn` is the rental held: the name the runner reads it by."""

    kind = "coordinator rental"   # what the runner's record calls this source
    #: the coordinator reads the card while the rental is DEGRADED and says
    #: when it has room again (S5b): work held for the card's memory waits
    #: for that word, within its patience, and runs again by itself
    recovers = True

    def __init__(self, client: CoordinatorClient, *, patience_s: float = 3600.0,
                 retry_s: float = 1.0, wait_s: float = 30.0, engine: str = "llamacpp",
                 on_lost=None, clock=time.monotonic, needs=None, engines=None):
        self.client = client
        #: what a model needs on the card, in MiB - the runner's estimate
        #: (`runner.needs`), declared on the ticket; None: nothing declared
        self.needs = needs
        #: the engine each model runs on, by its key (PLAN-4 D10) - declared
        #: on the ticket; None: every model on `engine`
        self.engines = engines
        #: the server's default patience; a request may name its own (`terms`)
        self.patience_s = float(patience_s)
        #: the pause before trying again when the coordinator did not answer
        self.retry_s = float(retry_s)
        #: the longest one wait is held - the coordinator holds at most its own
        self.wait_s = float(wait_s)
        #: the engine the runner runs its models with, as a ticket names it
        self.engine = str(engine)
        self.on_lost = on_lost
        self._clock = clock
        self.turn: dict | None = None
        #: how long the rental lives without a heartbeat, fixed at its claim.
        #: NOT re-read from a renewed rental: a renewal moves `expires_at` and
        #: keeps `granted_at`, so their difference grows the longer the card is
        #: held - an interval taken from it grew with it, until the heartbeats
        #: came slower than the rental's own lifetime and a live holder lost
        #: the card (found by building: a row holding 6.5 lifetimes went red).
        self.life: float | None = None
        self.lost: str | None = None
        self.waiting_since: float | None = None     # wall-clock, for health
        #: every process the rental put on the card, by pid: {"pid", "proof",
        #: "model", "engine", "port", "mib"} - what every heartbeat names
        self._named: dict = {}
        self._named_lock = threading.Lock()
        #: the rental's token: kept here, in memory, and sent to the coordinator alone
        self._token: str | None = None
        #: did the last renewal say another ticket is in line? Heard with each
        #: renewal that says so (`on_wanted`); a new rental starts unwanted
        self.wanted = False
        self.on_wanted = None
        #: how long the claim of the last take that claimed a rental took, in
        #: seconds - the rest of that take was its wait - or None: the last
        #: take claimed nothing (PLAN-4 D12, the runner's statistics)
        self.claim_s: float | None = None
        #: THE WATCH (D3): what the rental's work says - set by the runner, a
        #: function -> {working, progress, deadline_s, memory_errors,
        #: memory_error, slow_answers, slow_answer}, its counts the runner's
        #: own - and the counts at the
        #: grant, which every beat counts from; None: nothing is said
        self.work = None
        self._base = (0, 0, 0)
        #: the rental's `degraded` as its last renewal said it, and what hears
        #: it each time it changes (set by the runner)
        self.degraded = None
        self.on_degraded = None
        self._gate = threading.Condition()
        self._taking = False
        #: S18: A PLACE KEPT IN LINE for scheduled work that waits in its
        #: queue - planned for later, or passed over for room - {"ticket",
        #: "token", "model", "not_before"}, or None. Kept alive by the queue's
        #: looks (`look`), and claimed by the take its work makes
        self._parked = None
        self._park_lock = threading.Lock()

    def take(self, holder: str, model_key: str, for_=None, cancel=None, on_wait=None,
             terms=None) -> dict:
        if holder != self.client.actor:
            raise CardRefused(f"{holder} cannot take the card through {self.client.actor}'s "
                              f"client: one client, one actor")
        klass = getattr(terms, "klass", None) or "responsive"
        asked = getattr(terms, "patience_s", None)
        patience = self.patience_s if asked is None else max(0.0, float(asked))
        started = self._clock()
        self.claim_s = None
        held = self._one_at_a_time(cancel, patience, started)
        if held is not None:
            return held
        try:
            return self._take(holder, model_key, for_, cancel, on_wait, klass, patience,
                              started, getattr(terms, "label", None), terms)
        finally:
            with self._gate:
                self._taking = False
                self._gate.notify_all()

    def _one_at_a_time(self, cancel, patience, started):
        """Wait while another take of this card is under way. -> the rental this
        card holds, if it holds one; else None, with this take under way."""
        with self._gate:
            while self._taking:
                self._check(cancel, None)
                left = patience - (self._clock() - started)
                if left <= 0:
                    if patience <= 0:
                        raise CardRefused("the card is busy, and this request would not wait "
                                          "for it (card_busy)", "card_busy")
                    raise CardRefused(f"no card within its patience of {patience:g}s "
                                      f"(card_patience): {self._words(True)}", "card_patience")
                self._gate.wait(min(0.05, left))
            if self.turn is not None and not self.lost:
                return dict(self.turn)
            self._taking = True
            return None

    def _take(self, holder, model_key, for_, cancel, on_wait, klass, patience, started,
              label=None, terms=None):
        # a session that opens with no model asks for the card naming none;
        # a model is declared with what the runner estimates it needs (D2)
        engine = self.engine if self.engines is None or model_key is None \
            else self.engines(model_key)
        wants = [] if model_key is None else [{"key": model_key, "engine": engine,
                                               "need_mib": self._need(model_key)}]
        body = {"holder": self.client.name, "actor": holder, "class": klass, "models": wants}
        if for_ is not None:
            body["for"] = for_
        if label:
            # the pipeline's own label, so the operator can tell one session
            # from another (R-B4) - card-ask declares it
            body["label"] = str(label)
        if klass == "scheduled" and getattr(terms, "if_no_room", None) in ("wait", "fail"):
            # S18: what it does when its models do not fit at its claim - the
            # coordinator refuses it at once, or passes it over
            body["if_no_room"] = terms.if_no_room
        self.waiting_since = time.time()
        ticket = token = None
        #: S18: the room it waits on, once it was passed over for room
        room = None
        if getattr(terms, "parks", False):
            # a place kept in line for this work (`look`) is the one it claims
            with self._park_lock:
                kept, self._parked = self._parked, None
            if kept is not None:
                ticket, token = kept["ticket"], kept["token"]
                room = (ticket or {}).get("room")
        try:
            while True:
                self._check(cancel, token)
                left = patience - (self._clock() - started)
                reached = True
                if ticket is None:
                    status, answer = self._call("coordinator.card.ask", body)
                    if status is None:
                        reached = False
                    elif status == 200:
                        ticket, token = answer["ticket"], answer["token"]
                    else:
                        # waiting will not fix it - and more than the whole
                        # card is a reason of the request's own
                        raise CardRefused(answer["error"], answer["reason"]
                                          if answer["reason"] == "never_fits" else None)
                elif ticket["state"] == "offered":
                    claiming = time.monotonic()
                    status, answer = self._call("coordinator.card.claim",
                                                {"ticket_id": ticket["id"], "token": token})
                    if status == 200:
                        self.claim_s = time.monotonic() - claiming
                        return self._granted(answer["rental"], token)
                    if status is None:
                        reached = False
                    elif status == 409 and answer["reason"] in ("no_room", "never_fits"):
                        # the memory, verified at the grant (D2): not now - and
                        # this is work that is needed now - or never. In the
                        # coordinator's words and numbers, naming nobody; the
                        # ticket is over, and asking again would not change it.
                        # The request ends with the reason: its own
                        raise CardRefused(answer["error"], answer["reason"])
                    elif status == 409 and answer["reason"] == "not_offered":
                        # waiting - scheduled work waits for the room, too
                        ticket = {**ticket, "state": "waiting"}
                        if answer.get("memory") is not None:
                            # passed over for room (S18): the room it waits on,
                            # as its ticket says it - a job's work goes back to
                            # its queue just below; anything else waits here
                            ticket, room = self._passed_over(ticket, token, answer)
                            if not getattr(terms, "parks", False):
                                self._heard_room(terms, on_wait, model_key, room)
                    elif status == 409:
                        ticket = token = None           # the offer passed on: a new ticket
                        continue
                    else:
                        raise CardRefused(answer["error"])
                if ticket is not None and ticket["state"] == "offered" and reached:
                    continue                            # offered at once: claim it now
                if ticket is not None and ticket["state"] == "waiting" and room is not None \
                        and getattr(terms, "parks", False):
                    # S18: passed over for room at its claim - or a place kept
                    # for it, adopted still passed over: its work waits in its
                    # QUEUE, holding back nobody - its place in line kept, and
                    # claimed by its next take once it is offered
                    self._park(ticket, token, model_key, None)
                    ticket = token = None
                    raise CardRefused(f"{model_key} waits for room on the card: "
                                      f"{room['words']}", "no_room", room=room)
                if ticket is not None and ticket["state"] == "waiting" and patience <= 0:
                    self._give_up(token)
                    raise CardRefused("the card is busy, and this request would not wait for "
                                      "it (card_busy)", "card_busy")
                if left <= 0:
                    self._give_up(token)
                    if room is not None:
                        # S18: its patience ran out while it waited for room -
                        # said as loudly as no room, never only that it ran out
                        raise CardRefused(self._no_room_waited(model_key, room, patience,
                                                               self._clock() - started),
                                          "no_room")
                    raise CardRefused(f"no card within its patience of {patience:g}s "
                                      f"(card_patience): {self._words(reached)}",
                                      "card_patience")
                if room is None:
                    self._tell(on_wait, self._words(reached))
                if not reached:
                    self._pause(cancel, min(self.retry_s, left))
                    continue
                if ticket["state"] != "waiting":
                    continue
                status, answer = self._held_wait(ticket, token, min(self.wait_s, left), cancel)
                if status == 200:
                    ticket = answer["ticket"]
                    if ticket.get("room") and ticket["room"] != room:
                        room = ticket["room"]           # read again for it (S18)
                        self._heard_room(terms, on_wait, model_key, room)
                elif status == 409:
                    ticket = token = None               # it ended - dropped, say: ask again
                elif status is not None:
                    raise CardRefused(answer["error"])
                elif not self._stopped(cancel):
                    self._pause(cancel, min(self.retry_s, left))    # the coordinator is away
        finally:
            self.waiting_since = None

    # --------------------------------------------- scheduled work (S18)

    @property
    def parked(self):
        """The place kept in line for waiting work - its ticket - or None."""
        kept = self._parked
        return None if kept is None else dict(kept["ticket"])

    def _passed_over(self, ticket: dict, token: str, answer: dict) -> tuple:
        """A claim passed over for room: -> (the ticket, the room it waits on),
        as the coordinator keeps them - one look at the ticket - or, where that
        look is not answered, the claim's own numbers."""
        status, got = self._call("coordinator.card.wait",
                                 {"ticket_id": ticket["id"], "token": token, "wait_s": 0})
        if status == 200 and got["ticket"].get("room"):
            return got["ticket"], got["ticket"]["room"]
        numbers = dict(answer["memory"])
        words = str(answer.get("error") or "").split("waits for room on the card: ", 1)[-1]
        return ticket, {"memory": numbers, "outside_mib": None, "words": words}

    def _park(self, ticket, token, model_key, not_before) -> None:
        """Keep *ticket* as the place in line for waiting work. One place: one
        kept already is given up first."""
        with self._park_lock:
            was, self._parked = self._parked, {"ticket": dict(ticket), "token": token,
                                                "model": model_key, "not_before": not_before}
        if was is not None and was["token"] != token:
            self._give_up(was["token"])

    @staticmethod
    def _heard_room(terms, on_wait, model_key, room) -> None:
        """The room a take waits on, said as it waits in place (S18): to its
        terms' `on_room` - a pipeline's why - and its `on_wait`."""
        hear = getattr(terms, "on_room", None)
        if hear is not None:
            try:
                hear(dict(room))
            except Exception:                               # noqa: BLE001 - a report never fails
                pass
        CoordinatorCard._tell(on_wait, f"{model_key}: {room.get('words')}")

    @staticmethod
    def _no_room_waited(model_key, room, patience, waited) -> str:
        """A patience that ran out while its take waited for room, in the loud
        words of no room (the owner's second ruling of 2026-09-28)."""
        memory = room.get("memory") or {}
        verdict = {"verdict": "no_room", **memory}
        return (f"{model_key} did not fit on the card within its patience of "
                f"{MEM.span(patience)}: "
                f"{MEM.no_room_words(verdict, room.get('outside_mib'), waited_s=waited)}")

    def look(self, want, holder=None):
        """THE QUEUE'S LOOK (S18) for the scheduled work it waits to run -
        *want*: {job, model, not_before, for, if_no_room, waited} - or None:
        nothing waits, and a place kept in line is given up. ONE PLACE IN
        LINE is kept, for the work the queue names: asked for with its time
        (`not_before`) and whether it waits for room, the moment it is named
        - so the operator sees it planned from the morning, and it holds back
        nobody until its time - and kept alive by each look (a wait of 0 s).
        -> {"due": the place is offered the card - its work's take claims it
        - "room": what it waits on, once passed over}. While this server
        holds the card itself, the work goes on in its rental: due, and no
        place is kept. A coordinator that does not answer holds nothing back:
        due - its take waits for it within its patience. The work's TIME is
        its queue's to keep: the queue starts nothing before it, whatever
        this says."""
        with self._park_lock:
            kept = self._parked
            if want is None or (self.turn is not None and not self.lost):
                self._parked = None
        if kept is not None and (want is None or (self.turn is not None and not self.lost)):
            self._give_up(kept["token"])
        if want is None:
            return None
        if self.turn is not None and not self.lost:
            return {"due": True, "room": None}
        before = want.get("not_before")
        if kept is not None and (kept["model"] != want["model"]
                                 or kept["not_before"] != before):
            with self._park_lock:
                if self._parked is kept:
                    self._parked = None
            self._give_up(kept["token"])
            kept = None
        if kept is not None:
            status, got = self._call("coordinator.card.wait",
                                     {"ticket_id": kept["ticket"]["id"], "token": kept["token"],
                                      "wait_s": 0})
            if status == 200:
                kept["ticket"] = got["ticket"]
                return {"due": got["ticket"]["state"] == "offered",
                        "room": got["ticket"].get("room")}
            if status is None:
                return {"due": True, "room": kept["ticket"].get("room")}
            with self._park_lock:                   # it ended: dropped, say - ask again
                if self._parked is kept:
                    self._parked = None
        with self._gate:
            if self._taking:
                return {"due": False, "room": None}  # a take is under way: it goes first
        engine = self.engine if self.engines is None else self.engines(want["model"])
        body = {"holder": self.client.name, "actor": holder or self.client.actor,
                "class": "scheduled", "models": [{"key": want["model"], "engine": engine,
                                                   "need_mib": self._need(want["model"])}]}
        if want.get("for"):
            body["for"] = want["for"]
        if before is not None:
            body["not_before"] = before
        if want.get("if_no_room") in ("wait", "fail"):
            body["if_no_room"] = want["if_no_room"]
        status, got = self._call("coordinator.card.ask", body)
        if status != 200:
            # not answered, or refused - never_fits: its take says so
            return {"due": True, "room": None}
        self._park(got["ticket"], got["token"], want["model"], before)
        return {"due": got["ticket"]["state"] == "offered", "room": None}

    def _need(self, model_key):
        """What *model_key* needs, as the runner estimates it - or None: no
        estimate, or none can be made. Never fails a take."""
        if self.needs is None:
            return None
        try:
            got = self.needs(model_key)
        except Exception:                                   # noqa: BLE001
            return None
        return None if got is None else int(got)

    def fit(self, model, engine, need_mib, *, read=None, margin_mib=None, how="", hint=""):
        """May *model* go on the card during this rental? THE COORDINATOR
        DECIDES (D2): `/card/amend` - naming the rental's whole list, so a
        process that has just gone is known to be gone - and it reads the card
        afresh, waiting for a memory just given back. -> the verdict and its
        numbers, in its words; its own margin, not this server's. A
        coordinator that does not answer decides nothing (`unknown`): it
        never takes a rental's model away by its silence (rule 8). An answer
        that the rental is over puts nothing on the card - the heartbeat
        hears the loss, as ever. None: no rental held."""
        rental, token = self.turn, self._token
        if rental is None or token is None or self.lost:
            return None
        # the need its ticket was asked with, where this source was handed the
        # runner's estimate - one estimate for the ask and the amend
        if self.needs is not None:
            need_mib = self._need(model)
        body = {"token": token, "model": {"key": str(model), "engine": str(engine or self.engine),
                                          "need_mib": None if need_mib is None else int(need_mib)},
                "processes": self.named()}
        try:
            status, answer = self.client.call("coordinator.card.amend", body,
                                              timeout=AMEND_TIMEOUT_S)
        except Unreachable:
            return {"verdict": "unknown", "need_mib": body["model"]["need_mib"] or 0,
                    "free_mib": None, "margin_mib": None, "total_mib": None,
                    "words": f"the coordinator at {self.client.base} did not answer: "
                             f"the memory was not checked"}
        if status == 200:
            numbers = answer["memory"]
            return {"verdict": "fits" if numbers["free_mib"] is not None else "unknown",
                    **numbers, "words": "approved by the coordinator"}
        if status == 409 and answer["reason"] == "degraded":
            # the card is over-full (D3): nothing new goes on it until it has
            # room again - in the coordinator's words
            return {"verdict": "degraded", "need_mib": body["model"]["need_mib"] or 0,
                    "free_mib": None, "margin_mib": None, "total_mib": None,
                    "words": answer["error"]}
        if status == 409 and answer["reason"] in ("no_room", "never_fits"):
            words = answer["error"]
            if answer["reason"] == "never_fits" and hint:
                words = f"{words}. {hint}"
            return {"verdict": answer["reason"], **answer["memory"], "words": words}
        # the rental is over, or the ask was one this side should not have
        # made: nothing goes on the card. A loss is the heartbeat's to hear
        return {"verdict": "over", "need_mib": body["model"]["need_mib"] or 0, "free_mib": None,
                "margin_mib": None, "total_mib": None,
                "words": f"{model} was not put on the card: {answer['error']}"}

    def close(self) -> None:
        """This card source is done with - its client closed: its heartbeat
        stopped, and what it still held given back - a place kept in line for
        waiting work (S18) too."""
        with self._park_lock:
            kept, self._parked = self._parked, None
        if kept is not None and not self.client.closed:
            self._give_up(kept["token"])
        self.client.close()

    def _call(self, route_id: str, body: dict, timeout=None):
        """-> (status, answer), or (None, None) when the coordinator did not answer."""
        try:
            return self.client.call(route_id, body, timeout=timeout)
        except Unreachable:
            return None, None

    def _held_wait(self, ticket: dict, token: str, hold: float, cancel):
        """One wait, held open by the coordinator for up to *hold*, made on a
        thread of its own: a cancel or a stop never waits for its answer - it
        gives the ticket up, and the coordinator answers the wait at once.
        -> (status, answer), or (None, None): no answer, or interrupted."""
        body = {"ticket_id": ticket["id"], "token": token, "wait_s": round(max(0.0, hold), 3)}
        box, done = {}, threading.Event()

        def ask():
            try:
                box["got"] = self._call("coordinator.card.wait", body,
                                        timeout=hold + WAIT_MARGIN_S)
            finally:
                done.set()
        threading.Thread(target=ask, daemon=True, name="coordinator-wait").start()
        while not done.wait(0.05):
            if self._stopped(cancel):
                return None, None
        return box.get("got") or (None, None)

    def _stopped(self, cancel) -> bool:
        return (cancel is not None and cancel.is_set()) or self.client.closed

    def _check(self, cancel, token) -> None:
        """A cancel, or the server stopping, ends the wait - giving its ticket up."""
        if cancel is not None and cancel.is_set():
            self._give_up(token)
            raise CardRefused("cancelled while waiting for the card")
        if self.client.closed:
            self._give_up(token)
            raise CardRefused("the server is stopping: no card is asked for")

    def _pause(self, cancel, seconds: float) -> None:
        if cancel is not None:
            cancel.wait(max(0.0, seconds))
        else:
            time.sleep(max(0.0, seconds))

    def _give_up(self, token) -> None:
        """A ticket nobody will claim is given up, so the card is never offered
        to a server that stopped waiting. Never raises: one the coordinator
        does not hear of is dropped once nobody waits on it."""
        if token is None:
            return
        try:
            self.client.call("coordinator.card.cancel", {"token": token},
                             timeout=min(2.0, self.client.timeout_s))
        except Unreachable:
            pass

    def _words(self, reached: bool) -> str:
        """What a waiting job says: that it waits, and since when - never who
        has the card, which the coordinator tells nobody."""
        if not reached:
            return f"the coordinator at {self.client.base} does not answer"
        since = time.localtime(self.waiting_since or time.time())
        return f"waiting for the card, since {time.strftime('%H:%M', since)}"

    @staticmethod
    def _tell(on_wait, words: str) -> None:
        if on_wait is not None:
            try:
                on_wait(words)
            except Exception:                               # noqa: BLE001 - a report never fails a wait
                pass

    def _granted(self, rental: dict, token: str) -> dict:
        if not (self.turn and self.turn["id"] == rental["id"] and self.life):
            # a new rental: its lifetime is what it was granted for, its work
            # is counted from now, and it is not the last one's degraded
            self.life = max(0.01, rental["expires_at"] - rental["granted_at"])
            got = self._work_now()
            self._base = (got["progress"], got["memory_errors"],
                          got.get("slow_answers", 0)) if got else (0, 0, 0)
            self.degraded = None
        self.turn, self._token, self.lost, self.wanted = rental, token, None, False
        self.client.hold_rental(self)
        return dict(rental)

    def _work_now(self):
        """What the runner says the rental's work does, or None: it says
        nothing, or could not say. Never fails a heartbeat."""
        if self.work is None:
            return None
        try:
            return self.work()
        except Exception:                                   # noqa: BLE001
            return None

    def said(self) -> dict:
        """What this rental's heartbeat says of its work (D3): whether a model
        is answering, its progress and its memory errors since the rental was
        granted - the newest line with them - and a one-piece answer's
        deadline. {} when the runner says nothing: a heartbeat without them
        says it is not working."""
        got = self._work_now()
        if got is None:
            return {}
        base_p, base_e, base_s = self._base
        errors = max(0, int(got["memory_errors"]) - base_e)
        out = {"working": bool(got["working"]), "progress": max(0, int(got["progress"]) - base_p),
               "deadline_s": got["deadline_s"], "memory_errors": errors,
               "memory_error": got["memory_error"] if errors else None}
        if "slow_answers" in got:
            # the answers far slower than their model's normal, since the
            # grant too - the newest in words (D3, D12)
            slow = max(0, int(got["slow_answers"]) - base_s)
            out.update(slow_answers=slow, slow_answer=got.get("slow_answer") if slow else None)
        return out

    def beat_now(self) -> None:
        """Say it now: the next heartbeat goes at once (a memory error the
        runner found is news the coordinator should hear this beat)."""
        self.client.beat_now()

    def running(self, pid: int, proof: str, model: str, engine=None, port=None,
                mib=None) -> None:
        """A process this rental put on the card: its pid, the proof its
        command line carries - the id of the load that started it - its model,
        its engine (this card's, unless named), its port and memory (None
        where not known). Said on the next heartbeat, which goes now. A load
        runs only on a rental it holds, and `give_back` forgets the list with
        the rental."""
        with self._named_lock:
            self._named[int(pid)] = {
                "pid": int(pid), "proof": str(proof), "model": str(model),
                "engine": str(engine or self.engine),
                "port": None if port is None else int(port),
                "mib": None if mib is None else int(mib)}
        self.client.beat_now()

    def stopped(self, proof: str) -> None:
        """The process the load *proof* started has gone - its stop returned:
        off the list, said on the next heartbeat, which goes now. Never told
        for a stop that failed: that process may still be on the card."""
        with self._named_lock:
            self._named = {pid: x for pid, x in self._named.items() if x["proof"] != str(proof)}
        self.client.beat_now()

    def named(self) -> list:
        """Every process the rental names now, in the order they were named -
        the whole list, as each heartbeat carries it."""
        with self._named_lock:
            # each entry is replaced whole, never edited: the list is a snapshot
            return list(self._named.values())

    def give_back(self) -> None:
        """Release the rental. Never raises: a coordinator that cannot be
        reached lets the rental expire, and takes the card back on its own."""
        rental, token = self.turn, self._token
        self.turn, self._token, self.wanted, self.degraded = None, None, False, None
        with self._named_lock:
            self._named = {}
        self.client.drop_rental(self)
        if rental is None or token is None or self.lost:
            return
        try:
            self.client.call("coordinator.card.release", {"token": token},
                             timeout=min(2.0, self.client.timeout_s))
        except Unreachable:
            pass

    def status(self) -> dict:
        """Health's `card`: the contract's three fields."""
        return {"coordinator": self.client.word(),
                "holding": self.turn is not None and self.lost is None,
                "waiting_since": self.waiting_since}

    # the heartbeat's two answers
    def _renewed(self, rental: dict, wanted: bool = False) -> None:
        if self.turn is None or self.turn["id"] != rental["id"]:
            return
        self.turn, self.wanted = rental, bool(wanted)
        if self.wanted and self.on_wanted is not None:
            try:
                self.on_wanted()
            except Exception:                               # noqa: BLE001 - a heartbeat never fails
                pass
        # DEGRADED (D3), or not any more: heard each time it changes
        degraded = rental.get("degraded")
        if degraded != self.degraded:
            self.degraded = degraded
            if self.on_degraded is not None:
                try:
                    self.on_degraded(degraded)
                except Exception:                           # noqa: BLE001 - a heartbeat never fails
                    pass

    def _over(self, reason: str, error: str) -> None:
        if self.turn is None or self.lost:
            return
        self.lost, self.degraded = reason, None
        self.client.drop_rental(self)
        self.client._say(f"coordinator: the card was taken back - {reason}: {error}")
        if self.on_lost is not None:
            try:
                self.on_lost(reason, error)
            except Exception:                               # noqa: BLE001
                pass
