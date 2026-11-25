"""The server as a coordinator holder: the client half of the protocol, and the
runner holding the card through it.

    py -3.12 test_coordination.py

No GPU. The model is the stand-in; the coordinator is REAL - the coordinator
part's own, served over HTTP in this process, on a port the suite picks - so
every ticket below was asked for, waited on, claimed, heartbeated, reclaimed,
cancelled and released on the wire. The coordinator part is imported here, by
the suite only: the server's own code reaches it through the contract alone.

Every row is NATIVE: nothing in an existing application holds a rental.
"""
from __future__ import annotations

import http.server
import json
import queue as stdqueue
import shutil
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
sys.path.insert(0, str(HERE.parent / "llm-bootstrap-coordinator"))

import coordinator as C                                         # noqa: E402  (the suite's only)
import coordination as K                                        # noqa: E402
import ids                                                      # noqa: E402
import jobqueue as Q                                            # noqa: E402
import memory as MEM                                            # noqa: E402
import server as S                                              # noqa: E402
import validate as V                                            # noqa: E402
from runner import BackendError, Config, GenParams, ModelManager, Terms  # noqa: E402
from runner.adapter import RunnerBackend                        # noqa: E402
from runner.base import Cancelled                               # noqa: E402
from runner.manager import max_concurrency, side_by_side        # noqa: E402
from runner.mock import MockBackend                             # noqa: E402
from _suites import ASK_AGAIN_S, START_S, asked_again, killed, quiet_ports  # noqa: E402
import _suites                                                  # noqa: E402
_suites.forbid_the_card()        # a suite never asks the real card (nvidia-smi)

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

CORR = "c-20260924-090000-k3x9m2p7"


#: where a port a row starts something on LATER comes from, and the ranges its
#: coordinators hand out: the blocks of them this copy of the suite claimed
QUIET_PORTS, REGISTRY_PORTS, REGISTRY_BLOCK = (30500, 30999), (32000, 32767), 64


def free_port() -> int:
    """A port nothing listens on now - taken BELOW the range for passing
    connections, where the operating system hands a port to nobody who did not
    ask for it by number: a row that starts a coordinator on it LATER must not
    find it given to another suite's server in between (the coordinator's own
    suite lost a port that way, running beside this one) - and of the block of
    QUIET_PORTS this copy claimed, so another copy never hands it out."""
    return quiet_ports(*QUIET_PORTS).take()


_REGISTRIES = itertools.count()


def registry() -> tuple:
    """Sixteen ports for one coordinator to hand out: the four ranges of the
    block of REGISTRY_PORTS this copy claimed, in turn. Another copy's never
    meet them."""
    first, _ = quiet_ports(*REGISTRY_PORTS, size=REGISTRY_BLOCK).block
    start = first + 16 * (next(_REGISTRIES) % (REGISTRY_BLOCK // 16))
    return start, start + 15


#: EVERY PORT THIS SUITE HAD SOMETHING LISTEN ON - (what listened, the port
#: asked for, the port had) - read by its last row: the servers', the
#: coordinators', and the plain server's. A suite takes its ports from ranges
#: of its own, never one the operating system picks, which may be anybody's
#: the moment after (H6's register 3). Each part's own `listen`, recorded
#: where it is had
LISTENED: list = []
_listens = {"server": S.listen, "coordinator": C.listen}


def _recorded(part):
    def listening(port, host="127.0.0.1"):
        got = _listens[part](port, host)
        LISTENED.append((part, port, got.server_address[1]))
        return got
    return listening


S.listen, C.listen = _recorded("server"), _recorded("coordinator")


class FakeClock:
    """The coordinator's clock, moved by the row: a rental lapses when the row
    says so, and never because the machine was slow."""

    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now

    def advance(self, s):
        self.now += s


class Coord:
    """A real coordinator on a real port, which the row can stop and start."""

    def __init__(self, tmp: Path, ttl=0.6, clock=None, port=None, ports=None, hardware=None,
                 hold=30.0, **settings):
        self.data, self.ttl, self.clock, self.hold = tmp / "coordinator", ttl, clock, hold
        #: the rest of its settings, as `Coordinator` takes them: the watch's
        #: (hang_s, watch_s, floor_mib) among them
        self.settings = settings
        #: what it asks of the machine - by default nothing: no row asks the card
        self.hardware = hardware
        #: its port: the row's, or one of this copy's block (H6's register
        #: 3: it listened on port 0), kept across a restart
        self.port, self.srv = (port if port is not None else free_port()), None
        #: the range it hands ports out of - by default sixteen of its own, in
        #: 32000-32767: below the passing-connection range, clear of the
        #: coordinator's default, and never a copy's beside it (a fixed block
        #: of ten choices once handed two copies one port; a random start was
        #: only seldom the same) - `registry()`
        self.ports = ports or registry()
        self.start()

    def start(self, data=None):
        self.c = C.Coordinator(data or self.data, ttl_s=self.ttl, clock=self.clock or time.time,
                               ports=self.ports, hardware=self.hardware, hold_s=self.hold,
                               **self.settings)
        self.srv = C.serve(self.c, self.port)
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        return self

    def stop(self):
        if self.srv is not None:
            C.stop(self.srv)
            self.srv = None

    def kinds(self):
        return [e["kind"] for e in self.c.events]

    def of(self, kind):
        return [e for e in self.c.events if e["kind"] == kind]

    def rental(self):
        """Who holds the card, as the operator's /state says."""
        return self.c.state()[1]["rental"]

    def tickets(self):
        return self.c.state()[1]["tickets"]

    def ports_held(self):
        return self.c.state()[1]["ports"]


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="llmb-coordination-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.said = []

    def coord(self, **kw) -> Coord:
        c = Coord(self.tmp, **kw)
        self.addCleanup(c.stop)
        return c

    def config(self, **mock) -> Config:
        m = {"tokens_per_second": 0, "load_seconds": 0, "unload_seconds": 0, **mock}
        text = ('[runner]\nbackend = "mock"\nidle_unload_s = 600.0\n\n[mock]\n'
                + "".join(f"{k} = {v}\n" for k, v in m.items())
                + '\n[models.alpha]\nlabel = "Alpha"\n\n[models.beta]\nlabel = "Beta"\n')
        path = self.tmp / f"runner-{len(list(self.tmp.glob('runner-*.toml')))}.toml"
        path.write_text(text, encoding="utf-8")
        return Config.load(path)

    def client(self, url, actor=None, name="writer-app") -> K.CoordinatorClient:
        c = K.CoordinatorClient(url, actor or ids.mint_actor("srv"), name, log=self.said.append)
        self.addCleanup(c.close)
        return c

    def holder(self, url, *, patience_s=10.0, retry_s=0.05, cfg=None, factory=None,
               actor=None, name="writer-app"):
        """A manager holding the card through a coordinator: -> (manager, card)."""
        client = self.client(url, actor, name)
        card = K.CoordinatorCard(client, patience_s=patience_s, retry_s=retry_s)
        m = ModelManager(cfg or self.config(), self.tmp / f"folder-{client.actor}",
                         vram=lambda: None, card=card,
                         **({"backend_factory": factory} if factory else {}))
        self.addCleanup(m.shutdown)
        return m, card

    def timed_beats(self, client, route_id) -> list:
        """Every call of *route_id* the client makes from now: -> a list that
        fills with (how long it took, the timeout it was given)."""
        tries, real = [], client.call

        def timed(route, body=None, timeout=None):
            if route != route_id:
                return real(route, body, timeout)
            t0 = time.monotonic()
            try:
                return real(route, body, timeout)
            finally:
                tries.append((time.monotonic() - t0, timeout))
        client.call = timed
        return tries

    def assert_bounded_by_the_interval(self, tries, interval: float) -> None:
        """While the coordinator is away, heartbeats go on - three of them, and
        each bounded by the interval: given it as its timeout, and ended well
        inside the two seconds Windows takes to refuse a closed port. Each
        heartbeat's own time, never how many fit in a window: a heartbeat
        scheduled late on a busy machine is not a heartbeat left unbounded."""
        self.wait_for(lambda: len(tries) >= 3, "three heartbeats while the coordinator is away",
                      timeout=10.0)
        got = list(tries[:3])
        self.assertTrue(all(timeout is not None and timeout <= interval + 1e-9
                            for _, timeout in got), f"a heartbeat not bounded by its interval: {got}")
        self.assertTrue(all(took < 1.0 for took, _ in got),
                        f"a heartbeat waited past its bound: {got}")

    def wait_for(self, pred, what, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            got = pred()
            if got:
                return got
            time.sleep(0.01)
        self.fail(f"timed out after {timeout}s waiting for {what}")

    @staticmethod
    def events(m):
        return [e["event"] for e in m.recent_events()]

    @staticmethod
    def suppress(fn, *a, **kw):
        """-> what *fn* returned, or the exception it raised."""
        try:
            return fn(*a, **kw)
        except Exception as e:                              # noqa: BLE001
            return e

    def bounded(self, fn, within=5.0) -> dict:
        """Run *fn* on a daemon thread: -> {"value"} or {"error"}. A call that a
        broken bound would hold for minutes fails the row instead of hanging
        the suite (the sweep found one that did)."""
        box = {}

        def go():
            try:
                box["value"] = fn()
            except BaseException as e:                      # noqa: BLE001
                box["error"] = e
        t = threading.Thread(target=go, daemon=True)
        t.start()
        t.join(within)
        self.assertFalse(t.is_alive(), f"still waiting after {within}s")
        return box

    @staticmethod
    def counted(client) -> list:
        """Every route the client asks from now on, in order - counted as the
        calls are made."""
        calls, real = [], client.call

        def counting(route_id, body=None, timeout=None):
            calls.append(route_id)
            return real(route_id, body, timeout)
        client.call = counting
        return calls

    def assert_names_no_one(self, text, *others):
        for other in others:
            self.assertNotIn(other, str(text), f"an application was told of another: {other}")


# ============================================================= the rental ===

class NativeRentalRows(Case):
    def test_a_load_takes_the_card_before_the_weights_go_up_naming_its_request(self):
        co = self.coord()
        seen = {}

        class Probe(MockBackend):
            def start(self_):
                seen["rental"] = co.rental()
                super().start()
        cfg = self.config()
        m, card = self.holder(co.url, cfg=cfg,
                              factory=lambda key, port: Probe(key, key, cfg.mock))
        m.acquire("alpha", card.client.actor, for_=CORR)
        self.assertIsNotNone(seen["rental"], "the weights went up with no rental")
        self.assertEqual((seen["rental"]["actor"], [x["key"] for x in seen["rental"]["models"]],
                          seen["rental"]["for"]), (card.client.actor, ["alpha"], CORR))
        ev = self.events(m)
        self.assertLess(ev.index("card_granted"), ev.index("load_start"))
        granted = [e for e in m.recent_events() if e["event"] == "card_granted"]
        self.assertEqual([e.get("source") for e in granted], ["coordinator rental"],
                         "the record does not say the card was a rental")

    def test_the_card_goes_back_with_the_model_every_way_it_comes_off(self):
        co = self.coord()
        cases = {"unload": lambda m: m.unload(),
                 "idle": lambda m: (m.cfg.runner.__setitem__("idle_unload_s", 0.001),
                                    time.sleep(0.01), m.maybe_idle_unload()),
                 "shutdown": lambda m: m.shutdown()}
        for how, off in cases.items():
            with self.subTest(how=how):
                m, card = self.holder(co.url)
                m.acquire("alpha", card.client.actor)
                self.assertEqual(co.rental()["actor"], card.client.actor)
                off(m)
                self.assertIsNone(co.rental(), f"{how}: the card stayed taken")
                self.assertEqual(co.kinds()[-1], "rental_released")
                self.assertIn("card_given_back", self.events(m))

    def test_a_load_that_fails_gives_the_card_back(self):
        co = self.coord()

        class Broken(MockBackend):
            def start(self_):
                raise BackendError("the weights would not go up")
        cfg = self.config()
        m, card = self.holder(co.url, cfg=cfg,
                              factory=lambda key, port: Broken(key, key, cfg.mock))
        with self.assertRaises(BackendError):
            m.acquire("alpha", card.client.actor)
        self.assertIsNone(co.rental())
        # CHANGED ON PURPOSE (PLAN-4 D2, and H3): before its weights went up,
        # the load asked the coordinator whether the model fits - an amend, on
        # the record - and a release is on the record the moment it is asked
        # (`rental_releasing`), so a coordinator stopped in the middle of its
        # check rebuilds it as being given back; still nothing named
        self.assertEqual(co.kinds()[-4:], ["rental_granted", "rental_amended",
                                           "rental_releasing", "rental_released"])

    def test_a_model_switch_inside_a_pipeline_never_gives_the_card_back(self):
        """CHANGED ON PURPOSE (PLAN-4 S6, D6). This row was "a model switch
        gives the card back and asks again", kept so by S2 until the pipeline
        came. Reversed: the card is held from the pipeline's open to its close
        - here the queue's own, opened by the first load - so a switch keeps
        the one rental, and asks for no second place in the line. The card
        goes back when the pipeline closes: its work done, nothing loaded."""
        co = self.coord()
        m, card = self.holder(co.url)
        m.acquire("alpha", card.client.actor)
        first = co.rental()["id"]
        m.acquire("beta", card.client.actor)
        self.assertEqual(m.status()["loaded"], "beta")
        self.assertEqual((co.rental() or {}).get("id"), first, "the switch took a new rental")
        self.assertEqual((co.kinds().count("ticket_asked"), co.kinds().count("rental_granted")),
                         (1, 1), co.kinds())
        self.assertNotIn("rental_released", co.kinds(), "the switch gave the card back")
        m.unload()
        self.assertIsNone(co.rental(), "the queue's own pipeline kept the card with no work")
        self.assertEqual(co.kinds()[-1], "rental_released")

    def test_a_rental_held_past_its_lifetime_stays_held_while_it_is_heartbeated(self):
        co = self.coord(ttl=0.6)
        m, card = self.holder(co.url)
        m.acquire("alpha", card.client.actor)
        granted = co.rental()["id"]
        time.sleep(4.0)                                     # six and a half lifetimes
        self.assertEqual((co.rental() or {}).get("id"), granted, "a live holder lost its rental")
        self.assertNotIn("rental_reclaimed", co.kinds())
        self.assertEqual(m.status()["loaded"], "alpha")

    def test_a_factory_that_fails_gives_the_card_back(self):
        co = self.coord()

        def factory(key, port):
            raise BackendError("no backend could be made")
        m, card = self.holder(co.url, factory=factory)
        with self.assertRaises(BackendError):
            m.acquire("alpha", card.client.actor)
        self.assertIsNone(co.rental())

    def test_a_warm_model_is_reused_on_the_rental_it_already_has(self):
        co = self.coord()
        m, card = self.holder(co.url)
        m.acquire("alpha", card.client.actor)
        m.acquire("alpha", card.client.actor)
        self.assertEqual(co.kinds().count("rental_granted"), 1)
        self.assertEqual(co.kinds().count("ticket_asked"), 1)

    def test_the_local_card_asks_nobody(self):
        """CHANGED ON PURPOSE (S7): health's `card.holding` comes from the
        pipeline, so with no coordinator the manager says where it stands -
        holding, while its pipeline holds the card - where it once said
        nothing. The local card still asks nobody."""
        m = ModelManager(self.config(), None, vram=lambda: None)
        self.addCleanup(m.shutdown)
        m.acquire("alpha", ids.mint_actor("srv"))
        self.assertEqual(m.card_status(), {"coordinator": "none", "holding": True,
                                           "waiting_since": None})
        self.assertNotIn("card_granted", self.events(m))


# ============================================================ the waiting ===

class NativeWaitRows(Case):
    def second(self, co, **kw):
        """CHANGED ON PURPOSE (PLAN-4 S6): the first application holds the
        card in a session of its own, with its model up - where it once held
        it by a warm model alone. A warm model whose work is done now lets
        the card go at once when another application wants it (D6); a
        session keeps it until it is closed (`free`)."""
        m1, c1 = self.holder(co.url, name="first-app")
        m1.open_pipeline(c1.client.actor, models=["alpha"], keep_s=0)
        self.wait_for(lambda: m1.status()["loaded"] == "alpha", "the first's session and model")
        m2, c2 = self.holder(co.url, name="second-app", **kw)
        return m1, c1, m2, c2

    @staticmethod
    def free(m1) -> None:
        """The first lets the card go: its session closes (once `unload`)."""
        m1.close_pipeline(m1.pipeline_status()["id"])

    def test_a_second_holder_waits_while_the_first_holds_and_is_told_only_that_it_waits(self):
        co = self.coord()
        m1, c1, m2, c2 = self.second(co)
        heard, box = [], {}
        t = threading.Thread(daemon=True, target=lambda: box.update(
            b=m2.acquire("beta", c2.client.actor, on_wait=heard.append)))
        t.start()
        self.wait_for(lambda: heard, "the second holder to be told to wait")
        self.assertIn("waiting for the card, since ", heard[0])
        self.assert_names_no_one(heard, c1.client.actor, "first-app", "alpha")
        self.assertTrue(m2.loading, "waiting for the card read as not loading")
        self.assertIsNotNone(c2.status()["waiting_since"])
        self.assertEqual(co.rental()["actor"], c1.client.actor)
        self.assertEqual([t_["actor"] for t_ in co.tickets()], [c2.client.actor])
        self.free(m1)
        t.join(5)
        self.assertIn("b", box, "the second holder never got the card")
        self.assertEqual(co.rental()["actor"], c2.client.actor)
        self.assertIsNone(c2.status()["waiting_since"])
        self.assertFalse(m2.loading)
        waited = [e for e in m2.recent_events() if e["event"] == "card_waited"]
        self.assertEqual(len(waited), 1, waited)
        self.assertIn("waiting for the card", waited[0]["waiting_for"])
        self.assert_names_no_one(json.dumps(waited), c1.client.actor, "first-app")

    def test_a_cancel_ends_the_wait_at_once_and_gives_its_ticket_up(self):
        co = self.coord()
        m1, c1, m2, c2 = self.second(co)
        cancel, box = threading.Event(), {}

        def wait():
            try:
                m2.acquire("beta", c2.client.actor, cancel=cancel)
            except Exception as e:                          # noqa: BLE001
                box["e"] = e
        t = threading.Thread(target=wait, daemon=True)
        t.start()
        self.wait_for(lambda: co.tickets(), "the ticket to be in line")
        t0 = time.monotonic()
        cancel.set()
        t.join(5)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertIsInstance(box.get("e"), Cancelled)
        self.assertIn("card_wait_cancelled", self.events(m2))
        self.assertEqual(co.rental()["actor"], c1.client.actor)
        self.assertEqual(co.tickets(), [], "a cancelled wait left its ticket in line")
        self.assertEqual([e["actor"] for e in co.of("ticket_cancelled")], [c2.client.actor])

    def test_the_wait_for_the_card_holds_no_lock(self):
        """A shutdown, a rebind, a line of the record: none of them may queue
        behind a wait that can last the whole patience."""
        co = self.coord()
        m1, c1, m2, c2 = self.second(co)
        t = threading.Thread(target=lambda: self.suppress(m2.acquire, "beta", c2.client.actor),
                             daemon=True)
        t.start()
        self.wait_for(lambda: c2.status()["waiting_since"], "the wait to start")
        for name, call in (("rebind", lambda: m2.rebind(self.tmp / "elsewhere")),
                           ("shutdown", m2.shutdown)):
            with self.subTest(what=name):
                got = self.bounded(call, within=1.0)
                self.assertNotIn("error", got)
        self.assertTrue(t.is_alive(), "the wait ended on its own")

    def test_a_client_that_closed_ends_the_wait_and_asks_for_nothing(self):
        co = self.coord()
        m1, c1, m2, c2 = self.second(co)
        box = {}
        t = threading.Thread(daemon=True, target=lambda: box.update(
            e=self.suppress(m2.acquire, "beta", c2.client.actor)))
        t.start()
        self.wait_for(lambda: co.tickets(), "the ticket to be in line")
        c2.client.close()
        t.join(2)
        self.assertFalse(t.is_alive(), "a closed client went on waiting")
        self.assertIn("stopping", str(box.get("e")))
        self.assertEqual([e["actor"] for e in co.of("ticket_cancelled")], [c2.client.actor],
                         "a client that closed left its ticket in line")
        self.free(m1)                                       # the card is free now
        time.sleep(0.2)
        self.assertNotIn(c2.client.actor, [e.get("actor") for e in co.of("rental_granted")])

    def test_a_second_load_that_waited_goes_up_beside_the_first(self):
        """CHANGED ON PURPOSE (S7; was "a second load that waited never joins
        the first", which refused it "loaded while it waited"): two acquires
        on one runner wait for one card - on ONE ticket: a server has one
        place in the line. Once it is had, both load, one after the other,
        side by side on the one rental - and neither gives the card back."""
        co = self.coord()
        m1, c1, m2, c2 = self.second(co)
        outcomes = []
        threads = [threading.Thread(daemon=True, target=lambda key=key: outcomes.append(
            self.suppress(m2.acquire, key, c2.client.actor))) for key in ("alpha", "beta")]
        for th in threads:
            th.start()
        self.wait_for(lambda: c2.status()["waiting_since"], "the waits to start")
        time.sleep(0.3)
        self.free(m1)
        for th in threads:
            th.join(5)
        refused = [o for o in outcomes if isinstance(o, Exception)]
        self.assertEqual(refused, [], outcomes)
        self.assertEqual(sorted(b.model_key for b in outcomes), ["alpha", "beta"])
        self.assertEqual(m2.status()["loads_this_session"], 2)
        self.assertEqual(co.rental()["actor"], c2.client.actor,
                         "a load gave back the card the other one holds")
        self.assertEqual([e["actor"] for e in co.of("ticket_asked")].count(c2.client.actor), 1,
                         "one server took two places in the line")

    def test_patience_runs_out_as_card_patience_naming_no_one(self):
        co = self.coord()
        m1, c1, m2, c2 = self.second(co, patience_s=0.3)
        box = {}

        def wait():
            # CHANGED ON PURPOSE (S6, S2's register): the terms go with the
            # take by hand, never through a context left on the thread
            try:
                box["terms"] = terms = Terms()
                m2.acquire("beta", c2.client.actor, terms=terms)
            except Exception as e:                          # noqa: BLE001
                box["e"] = e
        t = threading.Thread(target=wait, daemon=True)
        t.start()
        t.join(3)
        self.assertFalse(t.is_alive(), "the wait outlived its patience")
        self.assertIsInstance(box.get("e"), BackendError)
        self.assertIn("no card within its patience of 0.3s (card_patience)", str(box["e"]))
        self.assertEqual(box["terms"].refused, "card_patience")
        self.assert_names_no_one(box["e"], c1.client.actor, "first-app")
        self.assertIn("card_refused", self.events(m2))
        self.assertIsNone(m2.status()["loaded"])
        self.assertEqual(co.tickets(), [], "a wait that ran out left its ticket in line")

    def test_a_patience_of_zero_is_now_or_not_at_all_card_busy_at_once_naming_no_one(self):
        co = self.coord()
        m1, c1, m2, c2 = self.second(co, patience_s=0)
        t0 = time.monotonic()
        terms = Terms()                                     # CHANGED ON PURPOSE (S6): by hand
        with self.assertRaises(BackendError) as caught:
            m2.acquire("beta", c2.client.actor, terms=terms)
        self.assertLess(time.monotonic() - t0, 1.0, "now or not at all waited")
        self.assertIn("card_busy", str(caught.exception))
        self.assertEqual(terms.refused, "card_busy")
        self.assert_names_no_one(caught.exception, c1.client.actor, "first-app", "alpha")
        self.assertEqual(co.tickets(), [], "a ticket that would not wait stayed in line")
        self.assertIsNone(m2.status()["loaded"])
        self.free(m1)
        self.assertTrue(m2.acquire("beta", c2.client.actor).is_alive(),
                        "now or not at all was refused a card that was free")

    def test_the_wait_asks_once_and_never_polls(self):
        """A long wait is ONE ask, then waits the coordinator holds open - a
        wait each hold, never a request a second."""
        co = self.coord(hold=0.3)                           # a short hold: a long wait is many
        m1, c1, m2, c2 = self.second(co)
        calls = self.counted(c2.client)
        box = {}
        t = threading.Thread(daemon=True, target=lambda: box.update(
            b=self.suppress(m2.acquire, "beta", c2.client.actor)))
        t.start()
        time.sleep(3.0)                                     # ten holds long
        asks, waits = calls.count("coordinator.card.ask"), calls.count("coordinator.card.wait")
        self.assertEqual(asks, 1, f"it asked {asks} times while it waited: {calls}")
        self.assertGreaterEqual(waits, 3, f"{waits} waits in 3 s of 0.3 s holds")
        self.assertLessEqual(waits, 12, f"{waits} waits in 3 s of 0.3 s holds: it polls")
        self.free(m1)
        t.join(5)
        self.assertNotIsInstance(box.get("b"), Exception, str(box.get("b")))
        self.assertEqual(calls.count("coordinator.card.ask"), 1)
        self.assertEqual(calls.count("coordinator.card.claim"), 1)

    def test_the_class_and_the_patience_are_the_requests(self):
        co = self.coord()
        m1, c1, m2, c2 = self.second(co, patience_s=30)
        box = {}

        def wait():
            # CHANGED ON PURPOSE (S6, S2's register): the terms by hand
            box["terms"] = terms = Terms(klass="scheduled", patience_s=0.4)
            box["got"] = self.suppress(m2.acquire, "beta", c2.client.actor, terms=terms)
        t = threading.Thread(target=wait, daemon=True)
        t0 = time.monotonic()
        t.start()
        self.wait_for(lambda: co.tickets(), "the ticket to be in line")
        self.assertEqual(co.tickets()[0]["class"], "scheduled")
        t.join(5)
        took = time.monotonic() - t0
        self.assertIsInstance(box.get("got"), BackendError)
        self.assertEqual(box["terms"].refused, "card_patience")
        self.assertLess(took, 3.0, "the request's own patience was not the one waited")
        box.clear()
        t = threading.Thread(daemon=True, target=lambda: box.update(
            got=self.suppress(m2.acquire, "beta", c2.client.actor)))
        t.start()
        self.wait_for(lambda: co.tickets(), "the ticket to be in line")
        self.assertEqual(co.tickets()[0]["class"], "responsive",
                         "a request that names no class is not responsive")
        time.sleep(0.6)
        self.assertTrue(t.is_alive(), "a request that names no patience did not wait the "
                                      "server's own")
        self.free(m1)
        t.join(5)
        self.assertNotIsInstance(box.get("got"), Exception, str(box.get("got")))

    def test_a_request_the_coordinator_refuses_is_refused_at_once(self):
        co = self.coord()
        m, card = self.holder(co.url, name="")               # a holder with no name
        t0 = time.monotonic()
        with self.assertRaises(BackendError) as caught:
            m.acquire("alpha", card.client.actor)
        self.assertLess(time.monotonic() - t0, 1.0, "it waited out its patience")
        self.assertIn("a ticket needs a holder", str(caught.exception))

    def test_an_unreachable_coordinator_is_never_proceeded_without(self):
        port = free_port()
        loads = []
        cfg = self.config()

        class Counted(MockBackend):
            def start(self_):
                loads.append(1)
                super().start()
        m, card = self.holder(f"http://127.0.0.1:{port}", patience_s=0.4, cfg=cfg,
                              factory=lambda key, port_: Counted(key, key, cfg.mock))
        got = self.bounded(lambda: m.acquire("alpha", card.client.actor))
        self.assertIsInstance(got.get("error"), BackendError)
        self.assertIn("does not answer", str(got["error"]))
        self.assertEqual(loads, [], "a load went ahead with no grant")
        self.assertEqual(card.status()["coordinator"], "unreachable")

    def test_a_coordinator_that_comes_up_during_the_wait_grants_it(self):
        port = free_port()
        m, card = self.holder(f"http://127.0.0.1:{port}")
        box = {}
        t = threading.Thread(daemon=True, target=lambda: box.update(b=m.acquire("alpha", card.client.actor)))
        t.start()
        self.wait_for(lambda: card.status()["waiting_since"], "the wait to start")
        time.sleep(0.2)
        co = self.coord(port=port)
        t.join(5)
        self.assertIn("b", box)
        self.assertEqual(co.rental()["actor"], card.client.actor)
        self.assertEqual(card.status()["coordinator"], "reachable")


# ================================================================ the loss ===

class NativeLossRows(Case):
    def test_silence_never_takes_the_model_off(self):
        co = self.coord(ttl=0.6)
        m, card = self.holder(co.url)
        m.acquire("alpha", card.client.actor)
        co.stop()
        time.sleep(1.5)                                     # well past the rental's lifetime
        self.assertEqual(m.status()["loaded"], "alpha", "silence took the model off")
        self.assertEqual(card.status(), {"coordinator": "unreachable", "holding": True,
                                         "waiting_since": None})
        self.assertNotIn("card_lost", self.events(m))
        co.start()                                          # the same record, the same port
        self.wait_for(lambda: card.status()["coordinator"] == "reachable",
                      "a heartbeat to reach the coordinator again")
        self.assertEqual(co.rental()["actor"], card.client.actor,
                         "the coordinator did not carry the rental over")
        self.assertEqual(m.status()["loaded"], "alpha")

    def test_a_rental_taken_back_takes_the_model_off_at_once(self):
        clock = FakeClock()
        co = self.coord(clock=clock)
        m, card = self.holder(co.url)
        backend = m.acquire("alpha", card.client.actor)
        clock.advance(10)
        co.c.reap()                                         # the coordinator takes it back
        self.wait_for(lambda: m.status()["loaded"] is None, "the model to come off")
        self.assertEqual(card.lost, "reclaimed")
        self.assertFalse(card.status()["holding"], "a card taken back still reads as held")
        self.assertTrue(backend.taken_off)
        ev = m.recent_events()
        (lost,) = [e for e in ev if e["event"] == "card_lost"]
        self.assertEqual(lost["reason"], "reclaimed")
        self.assertNotIn("card_given_back", [e["event"] for e in ev],
                         "a rental that was taken back was 'given back'")
        self.assertTrue(any("taken back" in s for s in self.said), self.said)

    def test_a_card_taken_back_reads_as_not_held_while_the_model_comes_off(self):
        """A real stop can take seconds. In between, health must not say
        `holding` for a card that was already taken back."""
        clock = FakeClock()
        co = self.coord(clock=clock)
        release = threading.Event()
        self.addCleanup(release.set)

        class Slow(MockBackend):
            def stop(self_):
                release.wait(5)
                super().stop()
        cfg = self.config()
        m, card = self.holder(co.url, cfg=cfg,
                              factory=lambda key, port: Slow(key, key, cfg.mock))
        m.acquire("alpha", card.client.actor)
        clock.advance(10)
        co.c.reap()
        self.wait_for(lambda: "unload_start" in self.events(m), "the model to start coming off")
        self.assertEqual(m.status()["loaded"], "alpha", "the stop was not held open")
        self.assertFalse(card.status()["holding"], "a card taken back reads as held")
        release.set()
        self.wait_for(lambda: m.status()["loaded"] is None, "the model to come off")

    def test_a_rental_the_coordinator_no_longer_knows_is_a_loss_too(self):
        co = self.coord(ttl=0.6)
        m, card = self.holder(co.url)
        m.acquire("alpha", card.client.actor)
        co.stop()
        co.start(data=self.tmp / "a-new-record")            # its record is gone
        self.wait_for(lambda: m.status()["loaded"] is None, "the model to come off")
        self.assertEqual(card.lost, "unknown_ticket")

    def test_a_card_taken_back_during_a_load_gives_the_load_up_at_once(self):
        """Found on the real card: the coordinator lost its record while a model
        loaded, and the next heartbeat's answer said the rental was unknown -
        another holder may be loading onto the card now - yet the model stayed
        until its own load had finished, 9.4 s later. The loss now ends the
        load where it stands."""
        co = self.coord(ttl=0.6)
        m, card = self.holder(co.url, cfg=self.config(load_seconds=20))
        box = {}
        t = threading.Thread(daemon=True, target=lambda: box.update(
            got=self.suppress(m.acquire, "alpha", card.client.actor)))
        t.start()
        self.wait_for(lambda: m.status()["loading"] == "alpha", "the load to start")
        co.stop()
        co.start(data=self.tmp / "a-new-record")            # its record is gone
        started = time.monotonic()
        t.join(5)
        self.assertFalse(t.is_alive(), "the load went on after the card was taken back")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertIsInstance(box["got"], BackendError)
        self.assertIn("the card was taken back (unknown_ticket)", str(box["got"]))
        self.assertIsNone(m.status()["loaded"])

    def test_a_card_taken_back_before_the_weights_go_up_is_not_loaded_onto(self):
        """The same loss, heard in the moment between the grant and the load:
        the weights never go up."""
        started = []

        class Card:
            turn, lost, on_lost = None, None, "heard elsewhere"

            def take(self_, holder, model_key, for_=None, cancel=None, on_wait=None):
                self_.turn, self_.lost = {"id": "t-1-abcdefgh"}, "reclaimed"
                return self_.turn

            def give_back(self_):
                self_.turn = None

            def status(self_):
                return None

        class Probe(MockBackend):
            def start(self_):
                started.append(self_.model_key)
                super().start()
        cfg = self.config()
        m = ModelManager(cfg, None, vram=lambda: None, card=Card(),
                         backend_factory=lambda key, port: Probe(key, key, cfg.mock))
        self.addCleanup(m.shutdown)
        with self.assertRaises(BackendError) as caught:
            m.acquire("alpha", ids.mint_actor("srv"))
        self.assertIn("taken back", str(caught.exception))
        self.assertEqual(started, [], "the weights went up on a card taken back")
        self.assertIsNone(m.status()["lease"]["holder"])

    def test_after_a_loss_the_next_load_asks_again(self):
        clock = FakeClock()
        co = self.coord(clock=clock)
        m, card = self.holder(co.url)
        m.acquire("alpha", card.client.actor)
        clock.advance(10)
        co.c.reap()
        self.wait_for(lambda: m.status()["loaded"] is None, "the model to come off")
        m.acquire("alpha", card.client.actor)
        self.assertIsNone(card.lost)
        self.assertEqual(co.rental()["actor"], card.client.actor)
        self.assertEqual(co.kinds().count("rental_granted"), 2)
        self.assertEqual(co.kinds().count("ticket_asked"), 2)

    def test_a_model_whose_card_was_taken_back_is_never_reused(self):
        """Even in the moment before the loss has taken it off: the next
        generation must not run on a card another holder may be loading."""
        class Card:
            turn, lost, on_lost = None, None, "heard elsewhere"

            def take(self_, holder, model_key, for_=None, cancel=None, on_wait=None):
                self_.turn, self_.lost = {"id": "t-1-abcdefgh"}, None
                return self_.turn

            def give_back(self_):
                self_.turn = None

            def status(self_):
                return None
        card = Card()
        m = ModelManager(self.config(), None, vram=lambda: None, card=card)
        self.addCleanup(m.shutdown)
        actor = ids.mint_actor("srv")
        first = m.acquire("alpha", actor)
        card.lost = "reclaimed"                             # heard, not yet acted on
        second = m.acquire("alpha", actor)
        self.assertIsNot(second, first, "a model on a card taken back was handed out")
        self.assertTrue(first.taken_off)

    def test_a_rental_kept_after_a_failed_stop_is_used_again_and_keeps_its_lifetime(self):
        """A stop that failed keeps the card; the next load - once the model
        that would not stop has gone - uses the rental the server still holds,
        asking for no second place in the line, and the heartbeat keeps the
        lifetime it was granted with: an interval read off a renewed rental
        grew until a live holder lost the card. (Rewritten on purpose by S3:
        the next load no longer goes on beside a model whose stop failed, so
        this model's stop fails once, and goes the next time it is asked.)"""
        co = self.coord(ttl=0.6)
        fails = [1]

        class Stuck(MockBackend):
            def stop(self_):
                if fails:
                    fails.pop()
                    raise BackendError("it would not stop")
                super().stop()
        cfg = self.config()
        m, card = self.holder(co.url, cfg=cfg,
                              factory=lambda key, port: Stuck(key, key, cfg.mock))
        m.acquire("alpha", card.client.actor)
        held = co.rental()["id"]
        with self.assertRaises(BackendError):
            m.unload()
        time.sleep(2.0)                                     # held, and heartbeated, a while
        m.acquire("alpha", card.client.actor)               # the same rental, used again
        self.assertEqual(co.rental()["id"], held)
        time.sleep(2.5)
        self.assertEqual((co.rental() or {}).get("id"), held, "a rental used again lapsed")
        self.assertNotIn("rental_reclaimed", co.kinds())
        self.assertEqual(co.kinds().count("ticket_asked"), 1)

    def test_a_stop_that_fails_keeps_the_card_until_the_client_closes(self):
        co = self.coord()

        class Stuck(MockBackend):
            def stop(self_):
                raise BackendError("it would not stop")
        cfg = self.config()
        m, card = self.holder(co.url, cfg=cfg,
                              factory=lambda key, port: Stuck(key, key, cfg.mock))
        m.acquire("alpha", card.client.actor)
        with self.assertRaises(BackendError):
            m.unload()
        self.assertEqual(co.rental()["actor"], card.client.actor,
                         "the card was given back while the model may still be on it")
        self.assertIn("card_kept", self.events(m))
        card.client.close()
        self.assertIsNone(co.rental())


# =========================================================== the pipeline ===

class NativePipelineRows(Case):
    """PLAN-4 D6, S6, with a real coordinator: the pipeline holds one rental
    from its open to its close; a session with nothing loaded keeps it while
    another application asks; the queue's own lets the card go at once when a
    renewal says it is wanted; a pipeline that lost the card asks for a new
    ticket by itself."""

    def pipe(self, m, state, what):
        return self.wait_for(lambda: m.pipeline_status()["state"] == state
                             and m.pipeline_status(), what)

    def test_an_open_pipeline_with_nothing_loaded_keeps_the_card_while_another_application_asks(self):
        """The Dictaphone's H-2: zero loads is not a release, a heartbeat that
        names no process is a heartbeat, and "the card is wanted" is for the
        queue's own pipeline alone."""
        co = self.coord(ttl=0.6)
        m1, c1 = self.holder(co.url, name="desk-app")
        opened = m1.open_pipeline(c1.client.actor, keep_s=0)
        self.pipe(m1, "holding", "the pipeline to hold the card")
        held = co.rental()["id"]
        m2, c2 = self.holder(co.url, name="second-app")
        box = {}
        t = threading.Thread(daemon=True, target=lambda: box.update(
            got=self.suppress(m2.acquire, "beta", c2.client.actor)))
        t.start()
        self.wait_for(lambda: co.tickets(), "the other application's ticket")
        self.wait_for(lambda: c1.wanted, "a renewal to say the card is wanted")
        time.sleep(1.8)                                     # three lifetimes, heartbeated
        self.assertEqual((co.rental() or {}).get("id"), held, "the empty session lost the card")
        self.assertNotIn("rental_reclaimed", co.kinds())
        self.assertEqual(co.rental()["processes"], [], "a process was named with nothing loaded")
        self.assertEqual((m1.pipeline_status()["state"], m1.status()["loaded"]),
                         ("holding", None))
        self.assertTrue(t.is_alive(), "the other application was given the card")
        m1.close_pipeline(opened["id"])
        t.join(5)
        self.assertNotIsInstance(box.get("got"), Exception, str(box.get("got")))
        self.assertEqual(co.rental()["actor"], c2.client.actor)

    def test_the_queues_own_pipeline_lets_the_card_go_at_once_when_it_is_wanted(self):
        co = self.coord(ttl=0.6)
        m1, c1 = self.holder(co.url, name="first-app")
        m1.acquire("alpha", c1.client.actor)
        time.sleep(0.8)                                     # heartbeats, nobody in line
        self.assertEqual((m1.status()["loaded"], co.rental()["actor"]), ("alpha", c1.client.actor),
                         "a warm model nobody else wanted was taken off")
        m2, c2 = self.holder(co.url, name="second-app")
        t0 = time.monotonic()
        m2.acquire("beta", c2.client.actor)
        took = time.monotonic() - t0
        self.assertLess(took, 3.0, "the queue's own pipeline kept the card it was wanted for")
        self.assertEqual(co.rental()["actor"], c2.client.actor)
        closed = m1.pipeline_status()
        self.assertEqual((closed["state"], closed["opened_by"], m1.status()["loaded"]),
                         ("closed", "queue", None))
        self.assertIn("the card is wanted", closed["why"])
        self.assert_names_no_one(json.dumps(m1.recent_events()), c2.client.actor, "second-app",
                                 "beta")

    def test_a_pipelines_class_is_its_tickets_taken_on_its_own_thread(self):
        """S2's register: the terms went through a context on the caller's
        thread, which a pipeline's own thread never has."""
        co = self.coord()
        m1, c1 = self.holder(co.url, name="first-app")
        m1.open_pipeline(c1.client.actor, keep_s=0)
        self.pipe(m1, "holding", "the first application's session")
        m2, c2 = self.holder(co.url, name="second-app")
        m2.open_pipeline(c2.client.actor, klass="scheduled", keep_s=0)
        self.wait_for(lambda: co.tickets(), "the pipeline's ticket")
        self.assertEqual((co.tickets()[0]["actor"], co.tickets()[0]["class"]),
                         (c2.client.actor, "scheduled"))

    def test_a_pipelines_label_reaches_its_ticket_and_the_coordinators_record(self):
        """R-B4: a pipeline's label never reached the coordinator's ticket
        (`ticket_asked` had none), though card-ask declares it - an operator
        could not tell replay 1 from replay 2. It is passed on the ask."""
        co = self.coord()
        m1, c1 = self.holder(co.url, name="first-app")
        m1.open_pipeline(c1.client.actor, keep_s=0, label="replay 1")
        self.pipe(m1, "holding", "the first application's session")
        m2, c2 = self.holder(co.url, name="second-app")
        m2.open_pipeline(c2.client.actor, klass="scheduled", keep_s=0, label="replay 2")
        self.wait_for(lambda: co.tickets(), "the second pipeline's ticket")
        self.assertEqual(co.tickets()[0]["label"], "replay 2")
        asked = {e["actor"]: e.get("label") for e in co.of("ticket_asked")}
        self.assertEqual(asked, {c1.client.actor: "replay 1", c2.client.actor: "replay 2"},
                         "the record cannot tell the two apart")

    def test_a_pipeline_that_lost_the_card_asks_for_a_new_ticket_by_itself(self):
        clock = FakeClock()
        co = self.coord(clock=clock)
        m, card = self.holder(co.url)
        opened = m.open_pipeline(card.client.actor, models=["alpha"], keep_s=0)
        self.wait_for(lambda: m.status()["loaded"] == "alpha", "the pipeline's model")
        first = co.rental()["id"]
        clock.advance(10)
        co.c.reap()                                         # the coordinator takes it back
        self.wait_for(lambda: (co.rental() or {}).get("id") not in (None, first),
                      "a new rental, asked for by the pipeline itself")
        self.wait_for(lambda: m.status()["loaded"] == "alpha", "its model up again")
        again = m.pipeline_status()
        self.assertEqual((again["id"], again["state"]), (opened["id"], "holding"))
        self.assertEqual(co.kinds().count("ticket_asked"), 2)
        ev = self.events(m)
        lost = ev.index("pipeline_lost")
        self.assertIn("unload_ok", ev[lost:], "the model did not come off after the loss")
        after = ev[lost:]
        self.assertLess(after.index("card_granted"), after.index("load_start"),
                        "a model went up again before a new claim")

    def test_a_pipeline_nobody_renews_lapses_and_its_rental_goes_back(self):
        co = self.coord()
        m, card = self.holder(co.url)
        a = RunnerBackend(m, log=self.said.append, idle_poll_s=0.05)
        a.attach(card.client.actor)
        self.addCleanup(a.close)
        s, out = a.open_pipeline(models=["alpha"], keep_s=1.0)
        self.assertEqual(s, 200, out)
        self.wait_for(lambda: m.status()["loaded"] == "alpha", "the pipeline's model")
        self.wait_for(lambda: m.pipeline_status()["state"] == "closed", "the lapse", timeout=5)
        self.assertIn("lapsed: not renewed", m.pipeline_status()["why"])
        self.assertIsNone(co.rental(), "a lapsed pipeline kept its rental")
        self.assertEqual(co.kinds()[-1], "rental_released")
        self.assertIn("pipeline_lapsed", self.events(m))


# ============================================================== the ports ===

class NativePortRows(Case):
    """A load's port from the registry - the llama.cpp kind of runner, so a
    load takes one, with the stand-in model behind it, so nothing listens."""

    def llama_config(self) -> Config:
        text = ('[runner]\nbackend = "llamacpp"\nidle_unload_s = 600.0\n\n'
                '[mock]\ntokens_per_second = 0\nload_seconds = 0\nunload_seconds = 0\n\n'
                '[models.alpha]\nlabel = "Alpha"\nfile = "alpha.gguf"\n\n'
                '[models.beta]\nlabel = "Beta"\nfile = "beta.gguf"\n')
        path = self.tmp / f"llama-{len(list(self.tmp.glob('llama-*.toml')))}.toml"
        path.write_text(text, encoding="utf-8")
        return Config.load(path)

    def porter(self, url, *, card=None, factory=None):
        """A runner whose loads take their ports from *url*: -> (manager, client, seen)."""
        client = self.client(url)
        cfg = self.llama_config()
        seen = []

        def made(key, port):
            if factory is not None:
                return factory(key, port)
            b = MockBackend(key, key, cfg.mock)
            b.port = port
            seen.append(port)
            return b
        m = ModelManager(cfg, self.tmp / f"folder-{client.actor}", vram=lambda: None,
                         ports=K.CoordinatorPorts(client), card=card, backend_factory=made)
        self.addCleanup(m.shutdown)
        return m, client, seen

    def test_a_load_listens_on_a_port_the_registry_granted_it(self):
        """CHANGED ON PURPOSE (S7): the port is held under the load's own name
        - the purpose carries its id - where it was "model-server" for every
        load, and two loads side by side were handed one port."""
        co = self.coord()
        m, client, seen = self.porter(co.url)
        up = m.acquire("alpha", client.actor)
        (port,) = seen
        held = [(p["port"], p["actor"], p["purpose"]) for p in co.ports_held()]
        self.assertEqual(held, [(port, client.actor, f"model-server {up.load_id}")])
        self.assertTrue(co.ports[0] <= port <= co.ports[1])
        self.assertEqual(m.status()["port"], port)

    def test_models_side_by_side_are_granted_a_port_each(self):
        """S7: the registry gives one port per actor per purpose, and every
        load once asked with the same purpose - so a second model beside the
        first was handed the first one's port. Each asks under its own load's
        name now: two ports, each going back with its own load."""
        co = self.coord()
        m, client, seen = self.porter(co.url)
        a = m.acquire("alpha", client.actor)
        b = m.acquire("beta", client.actor)
        self.assertEqual(len(set(seen)), 2, f"two loads side by side shared a port: {seen}")
        held = sorted((p["port"], p["purpose"]) for p in co.ports_held())
        self.assertEqual(held, sorted([(seen[0], f"model-server {a.load_id}"),
                                       (seen[1], f"model-server {b.load_id}")]))
        m.unload("alpha")
        self.assertEqual([p["port"] for p in co.ports_held()], [seen[1]])

    def test_the_port_is_kept_while_the_load_holds_it(self):
        co = self.coord(ttl=0.6)
        m, client, seen = self.porter(co.url)
        m.acquire("alpha", client.actor)
        time.sleep(2.5)                                     # four lifetimes
        self.assertEqual([p["port"] for p in co.ports_held()], seen)
        self.assertNotIn("port_reclaimed", co.kinds())

    def test_the_port_goes_back_with_the_model(self):
        co = self.coord()
        m, client, seen = self.porter(co.url)
        m.acquire("alpha", client.actor)
        m.unload()
        self.assertEqual(co.ports_held(), [])
        self.assertIn("port_released", co.kinds())
        time.sleep(0.5)                                     # beats that should not happen
        self.assertNotIn("port_lost", self.events(m),
                         "a port given back went on being heartbeated")

    def test_port_heartbeats_are_bounded_by_their_interval(self):
        """The cadence, measured where it can fail: while the coordinator is
        away. (The restart row below cannot see an unbounded heartbeat - on
        Windows a connection retried while the coordinator comes back simply
        connects - so this counts the attempts, as the rental's row does.)"""
        co = self.coord(ttl=0.6)
        m, client, seen = self.porter(co.url)
        m.acquire("alpha", client.actor)
        # CHANGED ON PURPOSE (H6, H4's R3): each heartbeat's own time is
        # measured, and the bound it was given - never how many fit in 1.3 s,
        # which heartbeats scheduled 0.2 s late (a busy machine) made 3
        tries = self.timed_beats(client, "coordinator.port.heartbeat")
        co.stop()
        self.assert_bounded_by_the_interval(tries, 0.6 / 3)

    def test_a_port_outlives_a_coordinator_that_restarts(self):
        """While the coordinator is away the port's heartbeats keep their
        cadence - each waits one interval, not the two seconds Windows takes
        to refuse a closed port - so the lifetime the restart gives it is met."""
        co = self.coord(ttl=0.6)
        m, client, seen = self.porter(co.url)
        m.acquire("alpha", client.actor)
        co.stop()
        time.sleep(1.5)                                     # away for two and a half lifetimes
        co.start()                                          # the same record, the same port
        self.wait_for(lambda: client.reachable, "a heartbeat to reach it again")
        time.sleep(1.5)
        self.assertEqual([x["port"] for x in co.ports_held()], seen,
                         "the port lapsed after the coordinator came back")
        self.assertNotIn("port_reclaimed", co.kinds())

    def test_a_full_registry_fails_the_load_in_its_own_words_and_gives_the_card_back(self):
        lo = free_port()
        co = self.coord(ports=(lo, lo))                     # one port in the range
        other = self.client(co.url)
        self.assertEqual(other.call("coordinator.port.request",
                                    {"actor": other.actor, "purpose": "model-server"})[0], 200)
        card_client = self.client(co.url)
        card = K.CoordinatorCard(card_client, patience_s=5, retry_s=0.05)
        m, client, seen = self.porter(co.url, card=card)
        with self.assertRaises(BackendError) as caught:
            m.acquire("alpha", card_client.actor)
        self.assertIn("no port for alpha", str(caught.exception))
        self.assertIn("no free port", str(caught.exception))
        self.assertEqual(seen, [], "a load went ahead with no port")
        self.assertIsNone(co.rental(), "the card stayed taken by a load that never happened")

    def test_an_unreachable_registry_fails_the_load_never_a_guess(self):
        m, client, seen = self.porter(f"http://127.0.0.1:{free_port()}")
        got = self.bounded(lambda: m.acquire("alpha", client.actor))
        self.assertIsInstance(got.get("error"), BackendError)
        self.assertIn("does not answer", str(got["error"]))
        self.assertEqual(seen, [], "a load listened on a port nobody granted")

    def test_a_port_taken_back_is_said_and_the_model_stays(self):
        clock = FakeClock()
        co = self.coord(clock=clock)
        m, client, seen = self.porter(co.url)
        m.acquire("alpha", client.actor)
        clock.advance(10)
        co.c.reap()                                         # the registry takes it back
        self.wait_for(lambda: "port_lost" in self.events(m), "the loss to be said")
        time.sleep(0.5)                                     # and more heartbeats
        lost = [e for e in m.recent_events() if e["event"] == "port_lost"]
        self.assertEqual([(e["port"], e["reason"]) for e in lost], [(seen[0], "reclaimed")])
        self.assertEqual(m.status()["loaded"], "alpha", "a port's loss took the model off")

    def test_a_stop_that_fails_keeps_the_port(self):
        co = self.coord()
        cfg = self.llama_config()

        class Stuck(MockBackend):
            def stop(self_):
                raise BackendError("it would not stop")

        def factory(key, port):
            b = Stuck(key, key, cfg.mock)
            b.port = port
            return b
        m, client, seen = self.porter(co.url, factory=factory)
        m.acquire("alpha", client.actor)
        with self.assertRaises(BackendError):
            m.unload()
        self.assertEqual(len(co.ports_held()), 1,
                         "the port went back while what would not stop may listen on it")
        client.close()
        self.assertEqual(co.ports_held(), [], "closing kept a port")


# ============================================================= the client ===

class NativeClientRows(Case):
    def test_heartbeats_are_bounded_by_their_interval(self):
        """On Windows a closed port takes about two seconds to refuse. A rental
        living 0.6 s beats every 0.2 s - and must go on doing so while the
        coordinator is away, or it could lapse after it came back."""
        co = self.coord(ttl=0.6)
        m, card = self.holder(co.url)
        m.acquire("alpha", card.client.actor)
        # CHANGED ON PURPOSE (H6, H4's R3): as the port's row, above
        tries = self.timed_beats(card.client, "coordinator.card.heartbeat")
        co.stop()
        self.assert_bounded_by_the_interval(tries, 0.6 / 3)

    def test_an_answer_outside_the_contract_is_no_answer(self):
        class NotACoordinator(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "no such route"}')
        asked = free_port()
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", asked), NotACoordinator)
        LISTENED.append(("plain", asked, srv.server_address[1]))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        m, card = self.holder(f"http://127.0.0.1:{srv.server_address[1]}", patience_s=0.3)
        got = self.bounded(lambda: m.acquire("alpha", card.client.actor))
        self.assertIsInstance(got.get("error"), BackendError)
        self.assertEqual(card.status()["coordinator"], "unreachable")
        self.assertTrue(any("does not declare" in s for s in self.said), self.said)

    def test_a_body_the_contract_refuses_is_never_sent(self):
        c = self.client("http://127.0.0.1:9")
        with self.assertRaises(ValueError):
            c.call("coordinator.card.ask", {"holder": "x", "actor": "not-an-actor",
                                            "models": []})
        self.assertIsNone(c.reachable, "it was sent")

    def test_one_client_one_actor(self):
        co = self.coord()
        m, card = self.holder(co.url)
        with self.assertRaises(BackendError):
            m.acquire("alpha", ids.mint_actor("srv"))       # somebody else's actor
        self.assertIsNone(co.rental())


# ============================================================ the adapter ===

class Run:
    def __init__(self, priority="batch", card_patience_s=None):
        self.cancelled = threading.Event()
        self.correlation_id = CORR
        self.handle, self.job_id, self.sample = "srv-w5r8t2hh:j-0001#01", "j-0001", 1
        self.priority, self.card_patience_s = priority, card_patience_s
        self.reports = []

    def stopped(self):
        return self.cancelled.is_set()

    def report(self, **fields):
        self.reports.append(dict(fields))


class NativeAdapterRows(Case):
    def runner(self, co, name="writer-app", patience_s=10.0, **cfg):
        m, card = self.holder(co.url, name=name, patience_s=patience_s,
                              cfg=self.config(**cfg) if cfg else None)
        a = RunnerBackend(m, log=self.said.append, idle_poll_s=3600)
        a.attach(card.client.actor)
        self.addCleanup(a.close)
        return a, card

    def holding(self, co):
        """The first application, holding the card in a session of its own,
        has run a job in it: -> (runner, card). CHANGED ON PURPOSE (PLAN-4 S6):
        the first once held the card by the warm model its job left; a warm
        model whose work is done now lets the card go at once when another
        application wants it (D6), and a session keeps it until it is closed
        (`free`)."""
        first, c1 = self.runner(co, name="first-app")
        s, out = first.open_pipeline(keep_s=0)
        self.assertEqual(s, 200, out)
        self.wait_for(lambda: first.pipeline_status()["state"] == "holding", "the session")
        first({"prompt": "hi"}, {"model": "alpha", "max_tokens": 5}, Run())
        return first, c1

    @staticmethod
    def free(first) -> None:
        """The first lets the card go: its session closes (once `unload`)."""
        first.close_pipeline(first.pipeline_status()["id"])

    def test_the_rental_names_the_request_whose_generation_loaded_the_model(self):
        co = self.coord()
        a, card = self.runner(co)
        a({"prompt": "hi"}, {"model": "alpha", "max_tokens": 5}, Run())
        self.assertEqual(co.rental()["for"], CORR)

    def test_a_job_waiting_for_the_card_says_only_that_it_waits(self):
        co = self.coord()
        first, c1 = self.holding(co)
        second, _ = self.runner(co, name="second-app")
        run, box = Run(), {}
        t = threading.Thread(daemon=True, target=lambda: box.update(
            out=second({"prompt": "hi"}, {"model": "beta", "max_tokens": 5}, run)))
        t.start()
        self.wait_for(lambda: run.reports, "a report while waiting")
        self.assertEqual(run.reports[0]["stage"], "waiting for the card")
        self.assertIn("waiting for the card, since ", run.reports[0]["waiting_for"])
        self.assert_names_no_one(run.reports, c1.client.actor, "first-app", "alpha")
        time.sleep(0.4)                                     # a held wait: nothing new to say
        self.assertEqual(len(run.reports), 1, "a wait with nothing new was a write of the store")
        self.assertTrue(second.loading(), "the stall watch would call the wait a stall")
        self.free(first)
        t.join(5)
        self.assertIn("out", box)

    def test_an_interactive_job_asks_as_responsive_and_a_batch_job_as_scheduled(self):
        co = self.coord()
        first, _ = self.holding(co)
        classes = {}
        for priority, runner_name in (("interactive", "live-app"), ("batch", "night-app")):
            a, card = self.runner(co, name=runner_name)
            run = Run(priority=priority)
            t = threading.Thread(daemon=True, target=lambda a=a, run=run: self.suppress(
                a, {"prompt": "hi"}, {"model": "beta", "max_tokens": 5}, run))
            t.start()
            self.wait_for(lambda card=card: [x for x in co.tickets()
                                             if x["actor"] == card.client.actor],
                          f"the {priority} job's ticket")
            (ticket,) = [x for x in co.tickets() if x["actor"] == card.client.actor]
            classes[priority] = ticket["class"]
            run.cancelled.set()
            t.join(5)
        self.assertEqual(classes, {"interactive": "responsive", "batch": "scheduled"})

    def test_a_cancelled_wait_is_the_queues_cancel(self):
        co = self.coord()
        first, _ = self.holding(co)
        second, c2 = self.runner(co, name="second-app")
        run, box = Run(), {}

        def go():
            try:
                second({"prompt": "hi"}, {"model": "beta"}, run)
            except Exception as e:                          # noqa: BLE001
                box["e"] = e
        t = threading.Thread(target=go, daemon=True)
        t.start()
        self.wait_for(lambda: c2.status()["waiting_since"], "the wait to start")
        run.cancelled.set()
        t.join(5)
        self.assertIsInstance(box.get("e"), Q.Cancelled)

    def test_a_jobs_own_patience_of_zero_fails_it_card_busy_at_once(self):
        co = self.coord()
        first, c1 = self.holding(co)
        second, _ = self.runner(co, name="second-app", patience_s=30.0)
        t0 = time.monotonic()
        with self.assertRaises(Q.GenerationFailed) as caught:
            second({"prompt": "hi"}, {"model": "beta"}, Run("interactive", card_patience_s=0))
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(caught.exception.reason, "card_busy")
        self.assert_names_no_one(caught.exception, c1.client.actor, "first-app")

    def test_a_jobs_own_patience_running_out_fails_it_card_patience(self):
        co = self.coord()
        first, _ = self.holding(co)
        second, _ = self.runner(co, name="second-app", patience_s=30.0)
        t0 = time.monotonic()
        got = self.bounded(lambda: second({"prompt": "hi"}, {"model": "beta"},
                                          Run(card_patience_s=0.4)))
        self.assertIsInstance(got.get("error"), Q.GenerationFailed)
        self.assertEqual(got["error"].reason, "card_patience")
        self.assertLess(time.monotonic() - t0, 3.0, "the job's own patience was not the one")

    def test_a_failure_that_is_not_the_cards_has_no_reason_of_its_own(self):
        co = self.coord()
        a, _ = self.runner(co)
        with self.assertRaises(Q.GenerationFailed) as caught:
            a({"prompt": "hi"}, {"model": "no-such-model"}, Run())
        self.assertIsNone(caught.exception.reason)

    def test_a_generation_cut_off_by_the_cards_loss_says_so(self):
        clock = FakeClock()
        co = self.coord(clock=clock)
        a, card = self.runner(co, tokens_per_second=40)
        run, box = Run(), {}

        def go():
            try:
                a({"prompt": "Tell me a long story."}, {"model": "alpha", "max_tokens": 400},
                  run)
            except Exception as e:                          # noqa: BLE001
                box["e"] = e
        t = threading.Thread(target=go, daemon=True)
        t.start()
        self.wait_for(lambda: run.reports and a.manager.status()["loaded"],
                      "the generation to be under way")
        clock.advance(10)
        co.c.reap()
        t.join(10)
        self.assertIsInstance(box.get("e"), Q.GenerationFailed)
        self.assertIn("the card was taken back (reclaimed)", str(box["e"]))


# ============================================================= the server ===

SHAPES, ROUTES = V.load(HERE.parent / "llm-bootstrap-contract")


def health_shape():
    (route,) = [r for r in ROUTES.values() if r.get("id") == "server.health"]
    return route["responses"]["200"]


def result_shape():
    (route,) = [r for r in ROUTES.values() if r.get("id") == "server.jobs.result"]
    return route["responses"]["200"]


class ServerCase(Case):
    def runner_toml(self, **runner) -> Path:
        runner = {"card_patience_s": 60.0, **runner}
        text = ('[runner]\nbackend = "mock"\n'
                + "".join(f"{k} = {v}\n" for k, v in runner.items())
                + '\n[mock]\ntokens_per_second = 0\nload_seconds = 0\nunload_seconds = 0\n\n'
                  '[models.demo]\nlabel = "Demo"\n')
        path = self.tmp / f"server-runner-{len(list(self.tmp.glob('server-runner-*')))}.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def folder(self, name) -> Path:
        f = self.tmp / name
        f.mkdir()
        return f

    def start(self, *args):
        httpd = S.start_server(S.parse_args(list(args)), log=self.said.append)
        self.addCleanup(S.stop, httpd)
        return httpd

    @staticmethod
    def http(url, method, path, body=None):
        req = urllib.request.Request(url + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def submit(self, url, prompt="hi", **job):
        status, out = self.http(url, "POST", "/api/jobs",
                                {"kind": "generate", "input": {"prompt": prompt},
                                 "gen": {"model": "demo", "max_tokens": 5}, **job})
        self.assertEqual(status, 200, out)
        return out["job_id"]

    def job(self, url, jid):
        status, out = self.http(url, "GET", f"/api/jobs/{jid}/result")
        self.assertEqual(V.check(out, result_shape(), SHAPES), [], "a result left the contract")
        return out["job"]

    def ended(self, url, jid, timeout=15.0):
        return self.wait_for(lambda: self.job(url, jid)["status"] in Q.FINISHED
                             and self.job(url, jid), f"{jid} to end", timeout)

    def health(self, url):
        status, h = self.http(url, "GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(V.check(h, health_shape(), SHAPES), [], "health left the contract")
        return h

    def session(self, url) -> str:
        """The application holds the card in a session of its own (PLAN-4 D6),
        over its server's route: -> the pipeline's id, once it holds the card.
        A warm model whose work is done lets the card go at once when another
        application wants it; a session keeps it until it is closed."""
        status, out = self.http(url, "POST", "/api/pipeline/open", {"keep_s": 0})
        self.assertEqual(status, 200, out)
        self.wait_for(lambda: self.health(url)["pipeline"]["state"] == "holding",
                      "the session to hold the card")
        return out["pipeline"]["id"]

    def close_session(self, url, pipeline_id) -> None:
        status, out = self.http(url, "POST", "/api/pipeline/close", {"pipeline": pipeline_id})
        self.assertEqual(status, 200, out)


class NativeServerRows(ServerCase):
    def test_a_server_takes_its_own_port_from_the_coordinator(self):
        co = self.coord()
        httpd = self.start("--coordinator", co.url, "--backend", "echo")
        port = httpd.server_address[1]
        self.assertTrue(co.ports[0] <= port <= co.ports[1], port)
        self.assertEqual([(x["port"], x["actor"], x["purpose"]) for x in co.ports_held()],
                         [(port, httpd.app.actor, "server")])
        self.assertEqual(self.health(httpd.app.url)["card"],
                         {"coordinator": "reachable", "holding": False, "waiting_since": None},
                         "a server with no runner said nothing of the coordinator")
        S.stop(httpd)
        self.assertEqual(co.ports_held(), [], "a stopped server kept its port")

    def test_an_explicit_port_is_the_applications_and_is_not_registered(self):
        co = self.coord()
        port = free_port()
        httpd = self.start("--coordinator", co.url, "--backend", "echo", "--port", str(port))
        self.assertEqual(httpd.server_address[1], port)
        self.assertEqual(co.ports_held(), [])
        # nothing has been asked of the coordinator yet but the start's look
        self.assertEqual(self.health(httpd.app.url)["card"]["coordinator"], "reachable")

    def test_a_start_the_coordinator_cannot_serve_stops_saying_so(self):
        saved = S.OWN_PORT_PATIENCE_S
        S.OWN_PORT_PATIENCE_S = 0.3
        self.addCleanup(setattr, S, "OWN_PORT_PATIENCE_S", saved)
        dead = f"http://127.0.0.1:{free_port()}"
        got = self.bounded(lambda: S.start_server(S.parse_args(
            ["--coordinator", dead, "--backend", "echo"]), log=self.said.append))
        self.assertIsInstance(got.get("error"), S.CannotStart)
        self.assertIn("no port from the coordinator", str(got["error"]))

    def test_health_says_where_the_server_stands_on_the_card(self):
        co = self.coord()
        httpd = self.start("--coordinator", co.url, "--backend", "mock",
                           "--runner", str(self.runner_toml()),
                           "--workspace", str(self.folder("book")))
        url = httpd.app.url
        self.assertEqual(self.health(url)["card"],
                         {"coordinator": "reachable", "holding": False, "waiting_since": None})
        jid = self.submit(url)
        self.assertEqual(self.ended(url, jid)["status"], "done")
        self.assertEqual(self.health(url)["card"],
                         {"coordinator": "reachable", "holding": True, "waiting_since": None})
        self.assertEqual(co.rental()["actor"], httpd.app.actor)
        self.assertEqual(co.rental()["for"], self.job(url, jid)["correlation_id"])
        co.stop()
        self.wait_for(lambda: self.health(url)["card"]["coordinator"] == "unreachable",
                      "health to say the coordinator does not answer")
        self.assertTrue(self.health(url)["card"]["holding"], "silence took the card away")

    def test_two_servers_one_coordinator_the_second_waits_then_loads(self):
        co = self.coord(ttl=1.0)
        first = self.start("--coordinator", co.url, "--name", "first-app", "--backend", "mock",
                           "--runner", str(self.runner_toml(idle_unload_s=1.0)),
                           "--workspace", str(self.folder("one")))
        second = self.start("--coordinator", co.url, "--name", "second-app", "--backend", "mock",
                            "--runner", str(self.runner_toml()),
                            "--workspace", str(self.folder("two")))
        a, b = first.app.url, second.app.url
        # CHANGED ON PURPOSE (PLAN-4 S6): the first holds the card in a session
        # - its warm model alone would let a wanted card go at once - and the
        # second loads once that session closes. Its model's idle window
        # passing inside the session takes the model off, and gives nothing back
        session = self.session(a)
        self.assertEqual(self.ended(a, self.submit(a))["status"], "done")
        waiting = self.submit(b)
        seen = self.wait_for(lambda: (self.job(b, waiting).get("progress") or {})
                             .get("stage") == "waiting for the card" and self.job(b, waiting),
                             "the second server's job to wait for the card")
        self.assertIn("waiting for the card, since ", seen["progress"]["waiting_for"])
        self.assert_names_no_one(json.dumps(seen), first.app.actor, "first-app")
        self.assertIsNotNone(self.health(b)["card"]["waiting_since"])
        self.wait_for(lambda: self.health(a)["runner"]["loaded"] is None,
                      "the first's model to leave after its idle window", timeout=15)
        self.assertTrue(self.health(a)["card"]["holding"], "the empty session let the card go")
        self.assertEqual(self.job(b, waiting)["status"], "running")
        self.close_session(a, session)
        self.assertEqual(self.ended(b, waiting, timeout=20)["status"], "done")
        self.assertEqual(co.rental()["actor"], second.app.actor)

    def test_a_servers_own_pipeline_keeps_the_card_between_its_jobs_and_lets_it_go_after(self):
        """PLAN-4 D6: without an open pipeline the queue holds the card for
        its own work. While a job of its folder is still queued it keeps the
        card, even when another application wants it; its work done, it lets
        the card go at once."""
        co = self.coord(ttl=1.0)
        slow = self.tmp / "slow.toml"
        slow.write_text('[runner]\nbackend = "mock"\ncard_patience_s = 60.0\n\n'
                        '[mock]\ntokens_per_second = 20\nload_seconds = 0\nunload_seconds = 0\n\n'
                        '[models.demo]\nlabel = "Demo"\n', encoding="utf-8")
        first = self.start("--coordinator", co.url, "--name", "first-app", "--backend", "mock",
                           "--runner", str(slow), "--workspace", str(self.folder("one")))
        second = self.start("--coordinator", co.url, "--name", "second-app", "--backend", "mock",
                            "--runner", str(self.runner_toml()),
                            "--workspace", str(self.folder("two")))
        a, b = first.app.url, second.app.url
        mine = [self.submit(a) for _ in range(6)]
        self.wait_for(lambda: (co.rental() or {}).get("actor") == first.app.actor,
                      "the first to hold the card")
        theirs = self.submit(b)
        for jid in mine:
            self.assertEqual(self.ended(a, jid)["status"], "done")
        self.assertEqual(self.ended(b, theirs, timeout=20)["status"], "done")
        self.assertEqual([e["actor"] for e in co.of("rental_granted")],
                         [first.app.actor, second.app.actor],
                         "the first let the card go between its own jobs")
        pipe = self.health(a)["pipeline"]
        self.assertEqual((pipe["state"], pipe["opened_by"]), ("closed", "queue"))
        self.assertIn("the card is wanted", pipe["why"])

    def test_a_job_taken_and_not_yet_generating_is_work_its_pipeline_keeps_the_card_for(self):
        """The job the worker has taken - running, its request still being
        built, no generation yet - is work too: the queue's own pipeline
        keeps the card for it while another application wants it (found by a
        control of the mutation run: a job taken in the moment between two
        generations lost the card it was about to use)."""
        co = self.coord(ttl=1.0)
        gate, building = threading.Event(), threading.Event()

        class Slow(Q.Hooks):
            def build_request(self_, job, after_results):
                if (job.get("input") or {}).get("prompt") == "second":
                    building.set()
                    gate.wait(10)
                return job.get("input")
        client = K.CoordinatorClient(co.url, ids.mint_actor("srv"), "first-app",
                                     log=self.said.append)
        backend = S.build_backend("mock", self.runner_toml(), log=self.said.append,
                                  coordinator=client)
        first = S.serve(S.own_port(client), hooks=Slow(), backend=backend,
                        log=self.said.append, actor=client.actor, coordinator=client)
        self.addCleanup(S.stop, first)
        self.addCleanup(gate.set)                           # before the stop: LIFO
        status, out = first.app.bind({"path": str(self.folder("one"))})
        self.assertEqual(status, 200, out)
        second = self.start("--coordinator", co.url, "--name", "second-app", "--backend", "mock",
                            "--runner", str(self.runner_toml()),
                            "--workspace", str(self.folder("two")))
        a, b = first.app.url, second.app.url
        self.assertEqual(self.ended(a, self.submit(a, prompt="first"))["status"], "done")
        taken = self.submit(a, prompt="second")
        self.assertTrue(building.wait(5), "the second job's request was never built")
        theirs = self.submit(b)
        self.wait_for(lambda: co.tickets(), "the other application's ticket")
        time.sleep(1.0)                                     # three renewals saying it is wanted
        self.assertEqual((co.rental() or {}).get("actor"), client.actor,
                         "a job taken and not yet generating lost its card")
        gate.set()
        self.assertEqual(self.ended(a, taken)["status"], "done")
        self.assertEqual(self.ended(b, theirs, timeout=20)["status"], "done")
        self.assertEqual([e["actor"] for e in co.of("rental_granted")],
                         [client.actor, second.app.actor])

    def test_a_server_stopped_while_it_waits_for_the_card_stops_at_once(self):
        """Its job waits for the card another server holds; it is stopped. It
        gives its ticket up and stops at once - it does not sit out the queue's
        grace for a worker that is only waiting on somebody else - and it
        never loads."""
        co = self.coord()
        first = self.start("--coordinator", co.url, "--name", "first-app", "--backend", "mock",
                           "--runner", str(self.runner_toml()),
                           "--workspace", str(self.folder("one")))
        a = first.app.url
        self.session(a)                                     # CHANGED ON PURPOSE (S6): it holds
        self.assertEqual(self.ended(a, self.submit(a))["status"], "done")
        second = S.start_server(S.parse_args(
            ["--coordinator", co.url, "--name", "second-app", "--backend", "mock",
             "--runner", str(self.runner_toml()), "--workspace", str(self.folder("two"))]),
            log=self.said.append)
        stopped = []
        self.addCleanup(lambda: stopped or S.stop(second))
        b = second.app.url
        waiting = self.submit(b)
        self.wait_for(lambda: (self.job(b, waiting).get("progress") or {})
                      .get("stage") == "waiting for the card",
                      "the second server's job to wait for the card")
        started = time.monotonic()
        got = self.bounded(lambda: S.stop(second), within=12.0)
        stopped.append(True)
        self.assertNotIn("error", got)
        self.assertLess(time.monotonic() - started, 3.0, "the stop sat out the wait")
        self.assertIsNone(second.app.backend.manager.status()["loaded"])
        self.assertEqual(co.rental()["actor"], first.app.actor, "the waiter took the card")
        self.assertEqual([e["actor"] for e in co.of("ticket_cancelled")], [second.app.actor],
                         "a stopped server left its ticket in line")

    def test_a_holder_killed_while_it_holds_the_card_the_second_proceeds(self):
        """R-B25's two holders and a kill: the first is a real process, killed
        where it stands; the coordinator takes the card back by liveness, and
        the second - which never heard a word of the first - proceeds."""
        co = self.coord(ttl=1.0)
        port = free_port()
        proc = subprocess.Popen(
            [sys.executable, str(HERE / "server.py"), "--coordinator", co.url,     # whole path:
             "--name", "doomed-app",                         # what mutate.py reaps by
             "--backend", "mock", "--runner", str(self.runner_toml()),
             "--workspace", str(self.folder("one")), "--port", str(port)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=HERE, encoding="utf-8")
        lines = stdqueue.Queue()
        reader = threading.Thread(target=lambda: [lines.put(x) for x in proc.stdout],
                                  daemon=True)
        reader.start()

        def end():
            killed(proc)
            reader.join(5)                                  # the pipe's end, then closed
            proc.stdout.close()
        self.addCleanup(end)
        self.assertIn(str(HERE), subprocess.list2cmdline(proc.args),
                      "a process this suite starts names this copy's folder on its command "
                      "line - what mutate.py finds it by when the suite is timed out")
        said = []

        def started():
            while not lines.empty():
                said.append(lines.get_nowait())
            return any("listening on" in x for x in said)
        self.wait_for(started, "the first server to start", START_S)
        a = f"http://127.0.0.1:{port}"
        self.session(a)                                     # CHANGED ON PURPOSE (S6): it holds
        self.assertEqual(self.ended(a, self.submit(a))["status"], "done")
        held_by = co.rental()["actor"]
        second = self.start("--coordinator", co.url, "--name", "second-app", "--backend", "mock",
                            "--runner", str(self.runner_toml()),
                            "--workspace", str(self.folder("two")))
        b = second.app.url
        waiting = self.submit(b)
        self.wait_for(lambda: (self.job(b, waiting).get("progress") or {}).get("stage")
                      == "waiting for the card", "the second server to wait")
        killed(proc)                                        # the process WE started, by its handle
        self.assertEqual(self.ended(b, waiting, timeout=20)["status"], "done")
        (taken,) = co.of("rental_reclaimed")
        self.assertEqual(taken["actor"], held_by, "the card was taken back from somebody else")
        self.assertEqual(co.rental()["actor"], second.app.actor)

    def test_a_server_started_before_its_coordinator_waits_for_it(self):
        saved = S.OWN_PORT_PATIENCE_S
        S.OWN_PORT_PATIENCE_S = 5.0
        self.addCleanup(setattr, S, "OWN_PORT_PATIENCE_S", saved)
        port = free_port()
        box = {}
        t = threading.Thread(daemon=True, target=lambda: box.update(h=self.suppress(
            S.start_server, S.parse_args(["--coordinator", f"http://127.0.0.1:{port}",
                                          "--backend", "echo"]), self.said.append)))
        t.start()
        time.sleep(0.8)                                     # it asks, and nobody answers
        co = self.coord(port=port)
        t.join(8)
        httpd = box.get("h")
        self.assertNotIsInstance(httpd, Exception, str(httpd))
        self.assertIsNotNone(httpd, "the start never finished")
        self.addCleanup(S.stop, httpd)
        self.assertTrue(co.ports[0] <= httpd.server_address[1] <= co.ports[1])

    def test_a_start_asks_again_until_a_port_comes_free(self):
        """The start's own patience, exercised by an ANSWER: a registry of one
        port, held by another application that lets go of it a second later.
        (A coordinator that is merely not up yet cannot show this on Windows:
        the first connection retries while it comes up, and simply connects.)"""
        saved = S.OWN_PORT_PATIENCE_S
        S.OWN_PORT_PATIENCE_S = 5.0
        self.addCleanup(setattr, S, "OWN_PORT_PATIENCE_S", saved)
        lo = free_port()
        co = self.coord(ttl=5.0, ports=(lo, lo))
        other = self.client(co.url)
        self.assertEqual(other.call("coordinator.port.request",
                                    {"actor": other.actor, "purpose": "server"})[0], 200)
        box = {}
        t = threading.Thread(daemon=True, target=lambda: box.update(h=self.suppress(
            S.start_server, S.parse_args(["--coordinator", co.url, "--backend", "echo"]),
            self.said.append)))
        t.start()
        time.sleep(1.0)                                     # refused: the range is full
        self.assertTrue(t.is_alive(), f"the start gave up at the first refusal: {box}")
        other.call("coordinator.port.release", {"port": lo, "actor": other.actor})
        t.join(5)
        httpd = box.get("h")
        self.assertNotIsInstance(httpd, Exception, str(httpd))
        self.assertIsNotNone(httpd, "the start never finished")
        self.addCleanup(S.stop, httpd)
        self.assertEqual(httpd.server_address[1], lo)

    def test_a_job_that_waits_past_its_patience_fails_card_patience_naming_no_one(self):
        co = self.coord(ttl=1.0)
        first = self.start("--coordinator", co.url, "--name", "first-app", "--backend", "mock",
                           "--runner", str(self.runner_toml()),
                           "--workspace", str(self.folder("one")))
        second = self.start("--coordinator", co.url, "--name", "second-app", "--backend", "mock",
                            "--runner", str(self.runner_toml(card_patience_s=0.5)),
                            "--workspace", str(self.folder("two")))
        a, b = first.app.url, second.app.url
        self.session(a)                                     # CHANGED ON PURPOSE (S6): it holds
        self.assertEqual(self.ended(a, self.submit(a))["status"], "done")
        job = self.ended(b, self.submit(b), timeout=10)
        self.assertEqual((job["status"], job.get("reason")), ("failed", "card_patience"))
        self.assert_names_no_one(job["error"], first.app.actor, "first-app")
        self.assertEqual(co.tickets(), [], "a job that gave up left its ticket in line")

    def test_a_job_may_name_its_own_patience_and_zero_is_card_busy_at_once(self):
        co = self.coord(ttl=1.0)
        first = self.start("--coordinator", co.url, "--name", "first-app", "--backend", "mock",
                           "--runner", str(self.runner_toml(idle_unload_s=1.5)),
                           "--workspace", str(self.folder("one")))
        second = self.start("--coordinator", co.url, "--name", "second-app", "--backend", "mock",
                            "--runner", str(self.runner_toml()),
                            "--workspace", str(self.folder("two")))
        a, b = first.app.url, second.app.url
        self.assertEqual(self.ended(a, self.submit(a))["status"], "done")
        t0 = time.monotonic()
        live = self.ended(b, self.submit(b, priority="interactive", card_patience_s=0),
                          timeout=10)
        self.assertLess(time.monotonic() - t0, 3.0, "now or not at all waited")
        self.assertEqual((live["status"], live.get("reason")), ("failed", "card_busy"))
        self.assert_names_no_one(live["error"], first.app.actor, "first-app")
        patient = self.submit(b)                            # the server's own patience: 60 s
        self.assertEqual(self.ended(b, patient, timeout=20)["status"], "done")
        self.assertNotIn("reason", self.job(b, patient))

    def test_a_job_that_names_no_priority_asks_the_coordinator_as_responsive(self):
        """S18b, over HTTP (PLAN-4 D0, "class default RESPONSIVE"): an
        application that does not say its job is batch has it asked as LIVE
        work - its ticket `responsive`, never `scheduled`, so it is never
        passed over for room at the coordinator's claim."""
        co = self.coord(ttl=1.0)
        first = self.start("--coordinator", co.url, "--name", "first-app", "--backend", "mock",
                           "--runner", str(self.runner_toml()),
                           "--workspace", str(self.folder("one")))
        second = self.start("--coordinator", co.url, "--name", "second-app", "--backend", "mock",
                            "--runner", str(self.runner_toml()),
                            "--workspace", str(self.folder("two")))
        a, b = first.app.url, second.app.url
        session = self.session(a)
        plain = self.submit(b)
        ticket = self.wait_for(lambda: next((t for t in co.tickets()
                                              if t["actor"] == second.app.actor), None),
                               "the second server's job to ask for the card")
        self.assertEqual(ticket["class"], "responsive", ticket)
        self.assertEqual(self.job(b, plain)["priority"], "interactive")
        self.close_session(a, session)
        self.assertEqual(self.ended(b, plain, timeout=20)["status"], "done")

    def test_the_servers_own_patience_is_an_hour_unless_its_settings_say(self):
        self.assertEqual(Config.default().runner["card_patience_s"], 3600.0)
        client = self.client("http://127.0.0.1:9")
        plain = self.tmp / "plain.toml"
        plain.write_text('[runner]\nbackend = "mock"\n', encoding="utf-8")
        # CHANGED ON PURPOSE (S6): the adapter keeps no card source of its own
        # any more - a job's terms go with its take by hand - so the manager's
        # is read
        backend = S.build_backend("mock", plain, log=self.said.append, coordinator=client)
        self.addCleanup(backend.close)
        self.assertEqual(backend.manager._card.patience_s, 3600.0)
        set_ = S.build_backend("mock", self.runner_toml(card_patience_s=7.5),
                               log=self.said.append, coordinator=client)
        self.addCleanup(set_.close)
        self.assertEqual(set_.manager._card.patience_s, 7.5)

    def test_the_token_never_leaves_the_servers_memory(self):
        co = self.coord()
        httpd = self.start("--coordinator", co.url, "--backend", "mock",
                           "--runner", str(self.runner_toml()),
                           "--workspace", str(self.folder("book")))
        tokens, real = [], httpd.app.coordinator.call

        def keep(route_id, body=None, timeout=None):
            status, answer = real(route_id, body, timeout)
            if route_id == "coordinator.card.ask" and status == 200:
                tokens.append(answer["token"])
            return status, answer
        httpd.app.coordinator.call = keep
        jid = self.submit(httpd.app.url)
        self.assertEqual(self.ended(httpd.app.url, jid)["status"], "done")
        (secret,) = tokens
        written = "".join(p.read_bytes().decode("utf-8", "replace")
                          for p in (self.tmp / "book").rglob("*") if p.is_file())
        told = json.dumps([self.health(httpd.app.url), self.job(httpd.app.url, jid)])
        self.assertNotIn(secret, written, "the token was written into the bound folder")
        self.assertNotIn(secret, told, "the token was in an answer")
        self.assertNotIn(secret, "\n".join(self.said), "the token was in the log")

    def test_a_ticket_names_each_models_own_engine_and_what_that_engine_needs(self):
        """PLAN-4 D10 under D2: on `--backend real` each model is declared to
        the coordinator with the engine it runs on - its own - and that
        engine's estimate of what it needs (a whisper model's: its file, a
        quarter more, and 600 MiB). Never one engine for the whole runner."""
        from runner import needs as N
        models = self.folder("models")
        (models / "ears.bin").write_bytes(b"lmgg" + b"\0" * (3 * 1024 * 1024))
        settings = self.tmp / "real.toml"
        settings.write_text('[runner]\nbackend = "real"\n'
                            f'models_dir = "{models.as_posix()}"\n\n'
                            '[models.story]\nfile = "story.gguf"\n\n'
                            '[models.ears]\nengine = "whisper"\nfile = "ears.bin"\n',
                            encoding="utf-8")
        co = self.coord(ttl=2.0)
        client = self.client(co.url)
        backend = S.build_backend("real", settings, log=self.said.append, coordinator=client,
                                  vram=lambda: None)
        self.addCleanup(backend.close)
        card = backend.manager._card
        card.take(client.actor, "ears")
        self.addCleanup(card.give_back)
        need = N.need_mib(backend.manager.cfg, "ears", "whisper")[0]
        self.assertEqual(need, N.whisper_need_mib(3 * 1024 * 1024 + 4))
        self.assertEqual([x["models"] for x in co.of("ticket_asked")],
                         [[{"key": "ears", "engine": "whisper", "need_mib": need}]])

    def test_a_ticket_declares_the_measured_need_once_the_model_has_three(self):
        """PLAN-4 D2 with D12 (S11b): once a model's statistics hold three
        measured loads - the card's memory it took after its first answer -
        its ticket declares the largest of them + 256 MiB, in place of its
        engine's estimate."""
        from runner import stats as ST
        models = self.folder("models")
        (models / "ears.bin").write_bytes(b"lmgg" + b"\0" * (3 * 1024 * 1024))
        settings = self.tmp / "real.toml"
        settings.write_text('[runner]\nbackend = "real"\n'
                            f'models_dir = "{models.as_posix()}"\n\n'
                            '[models.ears]\nengine = "whisper"\nfile = "ears.bin"\n',
                            encoding="utf-8")
        co = self.coord(ttl=2.0)
        client = self.client(co.url)
        backend = S.build_backend("real", settings, log=self.said.append, coordinator=client,
                                  vram=lambda: None)
        self.addCleanup(backend.close)
        backend.manager.rebind(self.folder("book") / "runner")
        store = ST.ModelStats(backend.manager.stats_path)
        for i, mib in enumerate((900, 1210, 1100)):
            store.note("ears", "first_answer_mib", mib, at=float(i))
        card = backend.manager._card
        card.take(client.actor, "ears")
        self.addCleanup(card.give_back)
        self.assertEqual([x["models"] for x in co.of("ticket_asked")],
                         [[{"key": "ears", "engine": "whisper", "need_mib": 1210 + 256}]])

    def test_a_load_that_took_the_card_is_timed_with_its_wait_and_its_claim(self):
        """PLAN-4 D12 (S11b): the load a rental was taken for carries, on the
        statistics' record, the ticket's wait and the claim - how long the
        claim itself took, as the card source timed it - both within its
        total."""
        co = self.coord(ttl=2.0)
        client = self.client(co.url)
        backend = S.build_backend("mock", self.runner_toml(), log=self.said.append,
                                  coordinator=client)
        self.addCleanup(backend.close)
        m = backend.manager
        m.rebind(self.folder("book") / "runner")
        m.acquire("demo", client.actor)
        loads = [e["measures"] for e in m.stat_lines() if e["what"] == "load"]
        self.assertEqual(len(loads), 1)
        got = loads[0]
        self.assertEqual(list(got)[:2], ["load_wait_s", "load_claim_s"])
        self.assertGreater(got["load_claim_s"], 0, "the claim is a call to the coordinator")
        self.assertGreaterEqual(got["load_s"], got["load_wait_s"] + got["load_claim_s"])
        self.assertEqual(co.of("rental_granted")[0]["actor"], client.actor)

    def test_a_llama_cpp_load_listens_on_a_port_the_registry_granted(self):
        """The wiring for a real model server: the stand-in llama-server,
        under its suite's two guards, listening on the port the registry
        granted the load. The card is never asked how much of it is free."""
        import test_llamacpp as TL                          # the stand-in, and its guards
        models = self.folder("models")
        TL.build_gguf(models, name="model.gguf", tensor_elems=(1024 * 1024,),
                      bits_per_weight=5.5)
        settings = self.tmp / "llama.toml"
        settings.write_text(
            "[runner]\nbackend = \"llamacpp\"\n"
            f'llama_server = ["{Path(TL.LAUNCHER[0]).as_posix()}", "{TL.FAKE.as_posix()}"]\n'
            f'models_dir = "{models.as_posix()}"\n'
            "startup_timeout_s = 20.0\nhealth_poll_s = 0.05\nshutdown_timeout_s = 10.0\n\n"
            '[models.story]\nfile = "model.gguf"\n', encoding="utf-8")
        co = self.coord(ttl=2.0)
        client = self.client(co.url)
        backend = S.build_backend("llamacpp", settings, log=self.said.append,
                                  coordinator=client, vram=lambda: None)
        httpd = S.serve(free_port(), backend=backend, log=self.said.append, actor=client.actor,
                        coordinator=client)
        self.addCleanup(S.stop, httpd)
        self.assertEqual(httpd.app.bind({"path": str(self.folder("book"))})[0], 200)
        url = httpd.app.url
        status, out = self.http(url, "POST", "/api/jobs",
                                {"kind": "generate", "input": {"prompt": "hi"},
                                 "gen": {"model": "story", "max_tokens": 10}})
        self.assertEqual(status, 200, out)
        self.assertEqual(self.ended(url, out["job_id"], timeout=30)["status"], "done")
        port = backend.manager.status()["port"]
        self.assertTrue(co.ports[0] <= port <= co.ports[1], port)
        # CHANGED ON PURPOSE (S7): held under the load's own name
        load = backend.manager.status()["load"]
        self.assertIn((port, client.actor, f"model-server {load}"),
                      [(x["port"], x["actor"], x["purpose"]) for x in co.ports_held()])
        self.assertEqual(co.rental()["actor"], client.actor)
        # CHANGED ON PURPOSE (PLAN-4 D2): the ticket declares what the runner
        # estimates the model needs - it declared nothing (null) before
        from runner import needs as N
        need = N.need_mib(backend.manager.cfg, "story", "llamacpp")[0]
        self.assertGreater(need, 1536)
        self.assertEqual([x["models"] for x in co.of("ticket_asked")],
                         [[{"key": "story", "engine": "llamacpp", "need_mib": need}]],
                         "the ticket did not declare the estimate - an amend's approval is not "
                         "what the ask said")
        self.assertEqual(co.rental()["models"], [{"key": "story", "engine": "llamacpp",
                                                  "need_mib": need}])
        S.stop(httpd)
        self.assertEqual(co.ports_held(), [], "the load's port stayed taken")
        self.assertIsNone(co.rental())

    def test_one_server_one_actor(self):
        client = self.client("http://127.0.0.1:9")
        with self.assertRaises(ValueError):
            S.Server(actor=ids.mint_actor("srv"), coordinator=client, log=lambda s: None)

    def test_a_name_with_no_coordinator_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            S.parse_args(["--name", "writer-app"])
        self.assertEqual(caught.exception.code, 2)

    def test_a_name_is_read_by_a_server_that_finds_its_coordinator_through_the_lock(self):
        """PLAN-4 D5: a server with a real card finds the coordinator through
        the machine's lock, and tells it its name - nothing else to configure."""
        self.assertEqual(S.parse_args(["--name", "writer-app", "--backend", "llamacpp"]).name,
                         "writer-app")


# ========================================================== the memory (D2) ===

class Machine:
    """The card as a coordinator's machine reads it, set by the row: `used` of
    `total` MiB in use - or one reading after another at each fresh look, the
    last one staying. It asks nothing of the real card, and ends nothing."""

    def __init__(self, used=1000, total=24576):
        self.used, self.total, self.readings, self.fresh = used, total, [], 0

    def probe(self, fresh=False):
        if fresh:
            self.fresh += 1
            if self.readings:
                self.used = self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]
        return {"known": True, "used_mib": self.used, "total_mib": self.total}


class Ending(Machine):
    """A coordinator's machine that writes down every list of processes it is
    handed to end on their proof - and ends none, and says none is alive:
    ending on proof is proven on real processes in the coordinator's own
    suite. It asks nothing of the real card."""

    def __init__(self):
        super().__init__(used=1000)
        self.ended = []

    def alive(self, processes):
        return [False] * len(processes)

    def end(self, processes):
        self.ended.append([dict(p) for p in processes])
        return [{"outcome": "ended", "detail": f"ended its process {p['pid']} ({p['proof']})"}
                for p in processes]


class Spawning(MockBackend):
    """The stand-in model, naming a process the moment it starts - as a real
    load does - so its rental names it, and drops it once it has stopped."""
    pids = itertools.count(52001)

    def start(self):
        super().start()
        self.pid = next(Spawning.pids)
        if getattr(self, "on_spawn", None) is not None:
            self.on_spawn(self.pid, self.load_id)


class NativeMemoryRows(Case):
    """PLAN-4 D2: the coordinator verifies the memory. A ticket declares what
    the runner estimates its models need; before any grant, and before each
    model a rental adds (an amend), the coordinator reads the card itself and
    approves - or refuses, in words and numbers, naming nobody."""

    def memory_holder(self, co, needs=None, patience_s=10.0, factory=None):
        client = self.client(co.url)
        card = K.CoordinatorCard(client, patience_s=patience_s, retry_s=0.05, engine="mock",
                                 needs=needs)
        cfg = self.config()
        m = ModelManager(cfg, self.tmp / f"folder-{client.actor}", vram=lambda: None, card=card,
                         **({"backend_factory": factory(cfg)} if factory else {}))
        self.addCleanup(m.shutdown)
        return m, card

    def test_an_amend_is_waited_for_as_long_as_the_contract_says_its_answer_may_take(self):
        """S4 R9: an amend right after an unload waits for the memory - the
        coordinator's amend_settle_s - and one this server never hears
        decides nothing: the model goes on unverified. So the server waits
        for its answer as long as the contract says one may take
        (memory.AMEND_ANSWER_S), within which every coordinator answers."""
        co = self.coord(hardware=Machine())
        m, card = self.memory_holder(co, needs=lambda key: 1000)
        waited = []
        real = card.client.call

        def timing(route_id, body=None, timeout=None):
            if route_id == "coordinator.card.amend":
                waited.append(timeout)
            return real(route_id, body, timeout)
        card.client.call = timing
        m.acquire("alpha", card.client.actor)
        self.assertEqual(waited, [MEM.AMEND_ANSWER_S])

    def test_a_ticket_declares_what_the_runner_estimates(self):
        co = self.coord(hardware=Machine())
        m, card = self.memory_holder(co, needs=lambda key: {"alpha": 5000}[key])
        m.acquire("alpha", card.client.actor)
        asked = co.of("ticket_asked")
        self.assertEqual([x["models"] for x in asked],
                         [[{"key": "alpha", "engine": "mock", "need_mib": 5000}]])
        self.assertEqual(co.rental()["models"], [{"key": "alpha", "engine": "mock",
                                                  "need_mib": 5000}])

    def test_before_each_load_the_coordinator_decides_on_a_fresh_read(self):
        machine = Machine()
        co = self.coord(hardware=machine)
        m, card = self.memory_holder(co, needs=lambda key: {"alpha": 5000, "beta": 7000}[key])
        m.acquire("alpha", card.client.actor)
        machine.readings = [3000, 1000]             # the second, the release's: back
        reads = machine.fresh
        m.acquire("beta", card.client.actor)
        self.assertEqual(machine.fresh - reads, 1, "the switch's model was not judged on a fresh read")
        self.assertEqual([[x["key"] for x in e["models"]] for e in co.of("rental_amended")],
                         [["alpha"], ["beta"]])
        checks = [e for e in m.recent_events() if e["event"] == "vram_check"]
        self.assertEqual([(e["model_key"], e["source"], e["need_mib"], e["free_mib"], e["card_mib"])
                          for e in checks],
                         [("alpha", "coordinator rental", 5000, 24576 - 1000, 24576),
                          ("beta", "coordinator rental", 7000, 24576 - 3000, 24576)])

    def test_a_model_the_card_has_no_room_for_is_refused_in_the_coordinators_words(self):
        machine = Machine()
        co = self.coord(hardware=machine)
        needs = {"alpha": 1000, "beta": 20000}
        m, card = self.memory_holder(co, needs=lambda key: needs[key])
        opened = m.open_pipeline(card.client.actor, models=["alpha"], keep_s=0)
        self.wait_for(lambda: m.status()["loaded"] == "alpha", "alpha, loaded by the pipeline")
        machine.readings = [10000]                  # something outside took the card
        with self.assertRaises(BackendError) as caught:
            m.acquire("beta", card.client.actor)
        said = str(caught.exception)
        self.assertIn("beta does not fit on the card now", said)
        self.assertIn("20000 MiB", said)
        self.assertIn(f"{24576 - 10000} MiB free", said)
        # CHANGED ON PURPOSE (merging S4 with S7): models go side by side, so
        # alpha - pinned by the pipeline - stays up; the refused one never went up
        up = [x["key"] for x in m.models()["models"] if x["loaded"]]
        self.assertEqual(up, ["alpha"], "a refused model is on the card, or the pinned one came off")
        self.assertEqual((co.rental() or {}).get("state"), "held",
                         "a refused model gave the session's card back")
        needs["beta"] = 30000
        with self.assertRaises(BackendError) as caught:
            m.acquire("beta", card.client.actor)
        self.assertIn("beta can never fit on this card", str(caught.exception))
        self.assertEqual([e["models"][0]["key"] for e in co.of("amend_refused")], ["beta", "beta"])
        got = card.fit("beta", "llamacpp", 30000, hint="Lower [models.beta] ctx.")
        self.assertEqual(got["verdict"], "never_fits")
        self.assertTrue(got["words"].endswith("Lower [models.beta] ctx."),
                        f"what to change is not said: {got['words']}")
        machine.readings = [1000]                   # back, for the release's check
        m.close_pipeline(opened["id"])

    def test_an_amend_the_coordinator_does_not_answer_decides_nothing(self):
        """Rule 8: a coordinator that does not answer takes nothing away - and
        decides nothing: the model goes on, and the record says the memory was
        not checked."""
        co = self.coord(hardware=Machine())
        m, card = self.memory_holder(co, needs=lambda key: 1000)
        opened = m.open_pipeline(card.client.actor, models=["alpha"], keep_s=0)
        self.wait_for(lambda: m.status()["loaded"] == "alpha", "alpha, loaded by the pipeline")
        co.stop()
        m.acquire("beta", card.client.actor)
        self.assertEqual(m.status()["loaded"], "beta")
        unknown = [e for e in m.recent_events() if e["event"] == "vram_unknown"]
        self.assertEqual([e["model_key"] for e in unknown], ["beta"])
        self.assertIn("did not answer", unknown[0].get("detail", ""))
        m.close_pipeline(opened["id"])

    def test_a_stopping_server_ends_a_wait_it_makes_through_a_coordinator_it_found(self):
        from runner.cardlock import MachineCard
        path = self.tmp / "card.lock"
        c = C.Coordinator(self.tmp / "found-wait", hardware=Machine(), ports=registry())
        srv = C.serve(c, free_port(), card_lock=path)
        self.addCleanup(C.stop, srv)
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        other = K.CoordinatorCard(self.client(url), engine="mock")
        other.take(other.client.actor, "alpha")     # another application holds the card
        card = MachineCard(path, through=lambda u, holder: K.CoordinatorCard(
            K.CoordinatorClient(u, holder, "stopping-app", log=self.said.append),
            engine="mock", retry_s=0.05))
        self.addCleanup(card.give_back)
        heard, box = [], {}
        t = threading.Thread(daemon=True, target=lambda: box.update(got=self.suppress(
            card.take, ids.mint_actor("srv"), "beta", on_wait=heard.append)))
        t.start()
        self.wait_for(lambda: heard, "the wait, through the coordinator the lock names")
        t0 = time.monotonic()
        card.stop_asking()
        t.join(5)
        self.assertFalse(t.is_alive(), "the wait went on once the server began to stop")
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertIsInstance(box.get("got"), K.CardRefused, str(box.get("got")))
        self.assertIn("stopping", str(box["got"]))

    def test_after_a_switch_the_rental_names_what_is_on_the_card(self):
        """S6 R5: after a model switch inside a pipeline, the rental still
        named its first model."""
        co = self.coord(hardware=Machine())
        m, card = self.memory_holder(co, needs=lambda key: 1000)
        opened = m.open_pipeline(card.client.actor, models=["alpha"], keep_s=0)
        self.wait_for(lambda: m.status()["loaded"] == "alpha", "alpha, loaded by the pipeline")
        m.acquire("beta", card.client.actor)
        self.assertEqual([x["key"] for x in co.rental()["models"]], ["beta"])
        m.close_pipeline(opened["id"])

    def test_the_amend_carries_the_rentals_list_so_a_process_just_gone_is_known(self):
        """The Dictaphone's H-3, end to end: the switch's amend names the
        rental's whole list - the first model's process gone from it - so the
        coordinator knows it has just gone, and waits for its memory to come
        back before it judges."""
        machine = Machine()
        co = self.coord(hardware=machine, ttl=5.0)   # CHANGED (H4): a late beat is no lapse
        m, card = self.memory_holder(co, needs=lambda key: 8000,
                                     factory=lambda cfg: (lambda key, port: Spawning(key, key,
                                                                                     cfg.mock)))
        bodies = []
        real = card.client.call

        def seeing(route_id, body=None, timeout=None):
            if route_id == "coordinator.card.amend":
                bodies.append(json.loads(json.dumps(body)))
            return real(route_id, body, timeout)
        card.client.call = seeing
        # a busy machine's heartbeat, provoked: the one that names alpha's
        # process is a second late
        beat, late = card.client._beat_once, []

        def late_once(interval):
            if not late and card.named():
                late.append(1)
                time.sleep(1.0)
            return beat(interval)
        card.client._beat_once = late_once
        # CHANGED ON PURPOSE (merging S4 with S7): models go side by side, so a
        # host's swap takes the first model off itself (the Dictaphone's H-5) -
        # an unload, then the next load at once - inside the session it holds,
        # which keeps the card while nothing is loaded (S6)
        m.open_pipeline(card.client.actor, keep_s=0)
        self.wait_for(lambda: m.pipeline_status()["state"] == "holding",
                      "the session to hold the card")
        first = m.acquire("alpha", card.client.actor)
        # CHANGED ON PURPOSE (H4): alpha's process is on the coordinator's list
        # before it is taken off - the heartbeat that names it goes on a thread
        # of its own, and on a busy machine the unload once cleared the list
        # before that thread had read it: the coordinator never knew a process
        # had gone, and judged at once (red 2 in 6 on master, beside sweeps)
        self.wait_for(lambda: first.pid in [x["pid"] for x in (co.rental() or {})
                                            .get("processes") or []],
                      "alpha's process to be named on the rental")
        self.assertEqual(late, [1], "the heartbeat naming alpha was never late: the row proved "
                                    "nothing")
        m.unload("alpha")
        machine.readings = [24576 - 3000, 24576 - 3000, 1000]   # alpha's memory, coming back
        m.acquire("beta", card.client.actor)
        self.assertEqual([b["processes"] for b in bodies][1], [],
                         "the switch's amend did not say alpha had gone")
        self.assertEqual([p["pid"] for p in bodies[0]["processes"]], [],
                         "the first amend named a process before any load")
        self.assertEqual([[x["key"] for x in e["models"]] for e in co.of("rental_amended")],
                         [["alpha"], ["beta"]], "the switch waited, and was approved")
        self.assertGreaterEqual(machine.fresh, 3)
        self.assertIn(first.pid, [x["pid"] for e in co.of("rental_processes")
                                  for x in e["processes"]])

    def test_responsive_work_the_card_has_no_room_for_is_refused_at_once(self):
        co = self.coord(hardware=Machine(used=20000))
        m, card = self.memory_holder(co, needs=lambda key: 8000)
        calls = self.counted(card.client)
        t0 = time.monotonic()
        with self.assertRaises(BackendError) as caught:
            m.acquire("alpha", card.client.actor)
        self.assertLess(time.monotonic() - t0, 3.0)
        said = str(caught.exception)
        self.assertIn("does not fit on the card now", said)
        self.assertIn("8000 MiB", said)
        self.assertEqual(calls.count("coordinator.card.ask"), 1, f"it asked again: {calls}")

    def test_scheduled_work_waits_for_the_room_within_its_patience(self):
        co = self.coord(hardware=Machine(used=20000))
        m, card = self.memory_holder(co, needs=lambda key: 8000)
        calls = self.counted(card.client)
        terms = Terms(klass="scheduled", patience_s=1.0)
        t0 = time.monotonic()
        with self.assertRaises(BackendError) as caught:
            m.acquire("alpha", card.client.actor, terms=terms)
        self.assertGreaterEqual(time.monotonic() - t0, 1.0, "it did not wait for the room")
        # CHANGED ON PURPOSE (S18, the owner's second ruling of 2026-09-28): a
        # patience that runs out while scheduled work waits for room fails in
        # the loud words of no room - never only that its patience ran out
        self.assertEqual((caught.exception.reason, terms.refused), ("no_room", "no_room"))
        self.assertIn("It waited 1 s for room", str(caught.exception))
        self.assertEqual(calls.count("coordinator.card.ask"), 1, f"it asked again: {calls}")
        self.assertEqual(len(co.of("ticket_passed_over")), 1)

    def test_a_server_finds_its_coordinator_through_the_machines_lock(self):
        """PLAN-4 D5: each time a session opens, the lock says who holds the
        card at the top - here a coordinator, whose address its note gives."""
        from runner.cardlock import MachineCard
        path = self.tmp / "card.lock"
        c = C.Coordinator(self.tmp / "found", hardware=Machine(), ports=registry())
        srv = C.serve(c, free_port(), card_lock=path)
        self.addCleanup(lambda: C.stop(srv) if srv.card_lock.held else None)
        made = []

        def through(url, holder):
            made.append(url)
            client = K.CoordinatorClient(url, holder, "found-app", log=self.said.append)
            return K.CoordinatorCard(client, engine="mock", needs=lambda key: 1000)
        card = MachineCard(path, through=through)
        m = ModelManager(self.config(), self.tmp / "found-folder", vram=lambda: None, card=card)
        self.addCleanup(m.shutdown)
        actor = ids.mint_actor("srv")
        m.acquire("alpha", actor)
        self.assertEqual(made, [f"http://127.0.0.1:{srv.server_address[1]}"])
        rental = c.state()[1]["rental"]
        self.assertEqual((rental["actor"], rental["holder"]), (actor, "found-app"))
        granted = [e for e in m.recent_events() if e["event"] == "card_granted"]
        self.assertEqual([e.get("source") for e in granted], ["coordinator rental"])
        m.unload()                                  # its work done: the queue's own pipeline closes
        self.assertIsNone(c.state()[1]["rental"], "the rental was not given back")
        C.stop(srv)                                 # the coordinator stops: its lock is free
        m.acquire("alpha", actor)
        granted = [e for e in m.recent_events() if e["event"] == "card_granted"]
        self.assertEqual([e.get("source") for e in granted],
                         ["coordinator rental", "machine lock"],
                         "with no coordinator, the next session did not take the lock itself")

    def test_build_backend_finds_the_coordinator_through_the_lock_by_itself(self):
        """Nothing needs configuring: a real card's server is made to find the
        coordinator through the machine's lock, naming itself as it was told,
        and declaring what the runner estimates."""
        from runner import needs as N
        from runner.cardlock import MachineCard
        models = self.tmp / "models"
        models.mkdir()
        (models / "plain.bin").write_bytes(b"\0" * (2 * 1024 * 1024))
        settings = self.tmp / "llama.toml"
        settings.write_text('[runner]\nbackend = "llamacpp"\nmodels_dir = "models"\n'
                            f'card_lock = "{(self.tmp / "b.lock").as_posix()}"\n\n'
                            '[models.story]\nfile = "plain.bin"\n', encoding="utf-8")
        backend = S.build_backend("llamacpp", settings, log=self.said.append, name="writer-app",
                                  vram=lambda: None)
        self.addCleanup(backend.close)
        card = backend.manager._card
        self.assertIsInstance(card, MachineCard)
        self.assertEqual(card.path, self.tmp / "b.lock")
        actor = ids.mint_actor("srv")
        via = card._through("http://127.0.0.1:9", actor)
        self.addCleanup(via.close)
        self.assertIsInstance(via, K.CoordinatorCard)
        self.assertEqual((via.client.base, via.client.actor, via.client.name),
                         ("http://127.0.0.1:9", actor, "writer-app"))
        self.assertEqual(via.needs("story"), N.need_mib(backend.manager.cfg, "story",
                                                        "llamacpp")[0])
        self.assertEqual(via.needs("story"), 2 + 1536)
        from runner.manager import LocalCard
        plain = S.build_backend("mock", settings, log=self.said.append)
        self.addCleanup(plain.close)
        self.assertIsInstance(plain.manager._card, LocalCard,
                              "the stand-in took a lock, or went looking for a coordinator")

    def test_a_real_server_finds_the_coordinator_through_the_lock_and_names_each_engine(self):
        """PLAN-4 D10: `--backend real` holds the real card as `llamacpp` does
        - through the machine's lock, finding the coordinator there - whatever
        its models' engines, and declares each model there with its own
        engine and that engine's estimate."""
        from runner import needs as N
        from runner.cardlock import MachineCard
        models = self.tmp / "models"
        models.mkdir()
        (models / "plain.bin").write_bytes(b"\0" * (2 * 1024 * 1024))
        (models / "ears.bin").write_bytes(b"lmgg" + b"\0" * (4 * 1024 * 1024))
        settings = self.tmp / "real.toml"
        settings.write_text('[runner]\nbackend = "real"\nmodels_dir = "models"\n'
                            f'card_lock = "{(self.tmp / "r.lock").as_posix()}"\n\n'
                            '[models.story]\nfile = "plain.bin"\n\n'
                            '[models.ears]\nengine = "whisper"\nfile = "ears.bin"\n',
                            encoding="utf-8")
        backend = S.build_backend("real", settings, log=self.said.append, name="desk",
                                  vram=lambda: None)
        self.addCleanup(backend.close)
        card = backend.manager._card
        self.assertIsInstance(card, MachineCard)
        self.assertEqual(card.path, self.tmp / "r.lock")
        via = card._through("http://127.0.0.1:9", ids.mint_actor("srv"))
        self.addCleanup(via.close)
        cfg = backend.manager.cfg
        self.assertEqual((via.client.name, via.engines("story"), via.engines("ears")),
                         ("desk", "llamacpp", "whisper"))
        self.assertEqual((via.needs("story"), via.needs("ears")),
                         (N.need_mib(cfg, "story", "llamacpp")[0],
                          N.need_mib(cfg, "ears", "whisper")[0]))
        self.assertEqual(via.needs("ears"), N.whisper_need_mib(4 * 1024 * 1024 + 4))


# ======================================================== a named process ===

class NativeNamedProcessRows(Case):
    """A real load names its process on its rental the moment it exists -
    before the weights are up - and a holder that HANGS has it ended by the
    coordinator, on that proof. The kernel's kill-on-close covers a holder
    that dies; nothing covered one alive and silent, with its model up."""

    def llama(self, co, load_s=0, args=()):
        """A llama.cpp runner holding the card through *co*, on the stand-in
        llama-server under its suite's guards - *args*, the stand-in's own
        switches: -> (manager, client, card)."""
        import test_llamacpp as TL                          # the stand-in, and its guards
        models = self.tmp / "models"
        if not models.exists():
            models.mkdir()
            TL.build_gguf(models, name="model.gguf", tensor_elems=(1024 * 1024,),
                          bits_per_weight=5.5)
        n = len(list(self.tmp.glob("named-*.toml")))
        settings = self.tmp / f"named-{n}.toml"
        extra = [*(["--fake-load-s", str(load_s)] if load_s else []), *args]
        slow = f"extra_args = {json.dumps(extra)}\n" if extra else ""
        settings.write_text(
            "[runner]\nbackend = \"llamacpp\"\n"
            f'llama_server = ["{Path(TL.LAUNCHER[0]).as_posix()}", "{TL.FAKE.as_posix()}"]\n'
            f'models_dir = "{models.as_posix()}"\n'
            "startup_timeout_s = 20.0\nhealth_poll_s = 0.05\nshutdown_timeout_s = 10.0\n\n"
            f'[models.story]\nfile = "model.gguf"\n{slow}', encoding="utf-8")
        client = self.client(co.url)
        card = K.CoordinatorCard(client, patience_s=30.0, retry_s=0.05)
        m = ModelManager(Config.load(settings), self.tmp / f"named-bound-{n}", vram=lambda: None,
                         card=card, ports=K.CoordinatorPorts(client))
        self.addCleanup(m.shutdown)
        return m, client, card

    def test_a_load_names_its_process_on_its_rental_before_the_weights_are_up(self):
        """At once - not on the next heartbeat, which this long a lifetime
        would send ten seconds later, long after the weights were up."""
        co = self.coord(ttl=30.0)
        m, client, _ = self.llama(co, load_s=4)
        box = {}
        t = threading.Thread(target=lambda: box.update(got=m.acquire("story", client.actor)),
                             daemon=True)
        t.start()

        def seen():
            """What the rental names and what the record says - read under the
            coordinator's own lock: the rental is set a moment before its line
            is written, and a row reading between the two sees half an
            answer."""
            with co.c._lock:
                named = [dict(x) for x in (co.rental() or {}).get("processes") or []]
                return named, [e["processes"] for e in co.of("rental_processes")]
        deadline = time.monotonic() + 10
        while not all(seen()):
            self.assertLess(time.monotonic(), deadline, "the rental never named its process")
            time.sleep(0.02)
        named, told = seen()
        self.assertEqual(m.status()["loading"], "story", "it was named only once the load was up")
        loading = m._loading_backend
        self.assertEqual([{k: v for k, v in x.items() if k != "since"} for x in named],
                         [{"pid": loading.pid, "proof": loading.load_id, "model": "story",
                           "engine": "llamacpp", "port": loading.port, "mib": None}])
        self.assertEqual(told, [named])
        t.join(20)
        self.assertTrue(box["got"].is_alive())

    def test_a_rental_given_back_names_no_process_on_the_next(self):
        co = self.coord(ttl=2.0)
        client = self.client(co.url)
        card = K.CoordinatorCard(client, patience_s=5.0, retry_s=0.05)
        card.take(client.actor, "story")
        card.running(4242, "load-k3x9m2p7", "story")
        self.assertEqual(card.named(), [{"pid": 4242, "proof": "load-k3x9m2p7", "model": "story",
                                         "engine": "llamacpp", "port": None, "mib": None}])
        card.give_back()
        card.take(client.actor, "story")
        self.addCleanup(card.give_back)
        self.assertEqual(card.named(), [], "a new rental carried the last one's processes")
        client.beat_now()
        time.sleep(0.3)
        self.assertEqual(co.rental()["processes"], [])

    def test_a_card_names_each_process_at_once_and_takes_it_off_at_once(self):
        """The heartbeat carries the whole list, and one goes the moment the
        list changes - after a spawn and after a stop - not on the next
        beat, which a lifetime this long would send ten seconds later."""
        co = self.coord(ttl=30.0)
        client = self.client(co.url)
        card = K.CoordinatorCard(client, patience_s=5.0, retry_s=0.05)
        card.take(client.actor, "alpha")
        self.addCleanup(card.give_back)

        def named():
            return [(x["pid"], x["proof"], x["model"]) for x in (co.rental() or {})
                    .get("processes") or []]
        card.running(4242, "load-k3x9m2p7", "alpha", port=31500)
        self.wait_for(lambda: named() == [(4242, "load-k3x9m2p7", "alpha")],
                      "the first process to be named", timeout=2.0)
        card.running(4343, "load-m4n8q2r5", "beta")
        self.wait_for(lambda: named() == [(4242, "load-k3x9m2p7", "alpha"),
                                          (4343, "load-m4n8q2r5", "beta")],
                      "the second to be named beside it", timeout=2.0)
        self.assertEqual((co.rental()["processes"][0]["port"], co.rental()["processes"][0]["engine"]),
                         (31500, "llamacpp"))
        card.stopped("load-k3x9m2p7")
        self.wait_for(lambda: named() == [(4343, "load-m4n8q2r5", "beta")],
                      "the stopped one to be off the list", timeout=2.0)
        card.running(4343, "load-p7q2x3k9", "beta")         # a pid named again is one process
        self.wait_for(lambda: named() == [(4343, "load-p7q2x3k9", "beta")],
                      "a pid named again to replace what it was", timeout=2.0)

    def test_every_heartbeat_carries_the_whole_list_so_a_lost_one_is_healed(self):
        # CHANGED ON PURPOSE (H6, H4's R3): a lifetime of 3 s, not 0.6 - on a
        # busy machine, heartbeats 0.2 s late around the lost one let a 0.6 s
        # rental lapse, which is not what this row is about - and its waits
        # are a lifetime and more
        co = self.coord(ttl=3.0)
        client = self.client(co.url)
        card = K.CoordinatorCard(client, patience_s=5.0, retry_s=0.05)
        card.take(client.actor, "alpha")
        self.addCleanup(card.give_back)
        lost, real = [], client.call

        def losing(route_id, body=None, timeout=None):
            if route_id == "coordinator.card.heartbeat" and not lost \
                    and (body or {}).get("processes"):
                lost.append(body)
                raise K.Unreachable("a heartbeat lost on the way")
            return real(route_id, body, timeout)
        client.call = losing
        card.running(4242, "load-k3x9m2p7", "alpha")
        self.wait_for(lambda: [x["pid"] for x in (co.rental() or {}).get("processes") or []]
                      == [4242], "the next heartbeat to carry the list", timeout=10.0)
        self.assertEqual(len(lost), 1, "no heartbeat was lost: the row proved nothing")
        beats = []

        def counting(route_id, body=None, timeout=None):
            if route_id == "coordinator.card.heartbeat":
                beats.append(body)
            return real(route_id, body, timeout)
        client.call = counting
        self.wait_for(lambda: len(beats) >= 2, "two more heartbeats", timeout=10.0)
        self.assertTrue(all([x["pid"] for x in b["processes"]] == [4242] for b in beats),
                        f"a heartbeat did not carry the whole list: {beats}")

    def test_a_process_whose_stop_failed_stays_named_and_nothing_loads_beside_it(self):
        """S1's register row 3, closed: after a stop that failed, the next load
        went on beside the model the record still held open - two models on
        the card. Now the process stays named on the rental until it is
        confirmed gone, its stop is asked again before any load, and a load is
        refused while it will not go."""
        co = self.coord(ttl=0.6)
        fails, pids = [1, 1], iter(range(42001, 42100))

        class Spawning(MockBackend):
            def start(self_):
                self_.pid = next(pids)
                self_.on_spawn(self_.pid, self_.load_id)
                super().start()

            def stop(self_):
                if fails:
                    fails.pop()
                    raise BackendError("it would not stop")
                super().stop()
        cfg = self.config()
        m, card = self.holder(co.url, cfg=cfg,
                              factory=lambda key, port: Spawning(key, key, cfg.mock))
        a = m.acquire("alpha", card.client.actor)

        def named():
            return [x["pid"] for x in (co.rental() or {}).get("processes") or []]
        self.wait_for(lambda: named() == [42001], "the first load to be named")
        with self.assertRaises(BackendError):
            m.unload()
        time.sleep(0.8)                                     # several heartbeats
        self.assertEqual(named(), [42001], "a process whose stop failed was taken off the list")
        with self.assertRaises(BackendError) as caught:
            m.acquire("beta", card.client.actor)            # its stop fails again
        self.assertIn("did not come off the card", str(caught.exception))
        self.assertNotIn("beta", [e.get("model_key") for e in m.recent_events()
                                  if e["event"] == "load_start"], "beta was loaded beside it")
        self.assertEqual(named(), [42001])
        m.acquire("beta", card.client.actor)                # its stop works now
        self.wait_for(lambda: named() == [42002], "only the new load to be named")
        events = m.recent_events()
        self.assertEqual(max_concurrency(events), 1, "two models were on the card at once")
        closed = [e["event"] for e in events if e.get("load") == a.load_id]
        self.assertEqual(closed[-1], "unload_ok", "the model that went is still open on the record")
        self.assertEqual(co.kinds().count("rental_granted"), 1, "the card was let go in between")

    def test_every_process_side_by_side_is_named_and_one_whose_stop_fails_stays(self):
        """S7: models side by side put several processes on one rental. Every
        one is named on the heartbeat from its spawn; a stop that works takes
        its own off at once, and one whose stop failed stays named - the
        others untouched - until its stop, asked again before the next load,
        works."""
        co = self.coord(ttl=0.6)
        pids, stubborn = iter(range(43001, 43100)), {"beta": 1}

        class Spawning(MockBackend):
            def start(self_):
                self_.pid = next(pids)
                self_.on_spawn(self_.pid, self_.load_id)
                super().start()

            def stop(self_):
                if stubborn.get(self_.model_key):
                    stubborn[self_.model_key] -= 1
                    raise BackendError("it would not stop")
                super().stop()
        cfg = self.config()
        m, card = self.holder(co.url, cfg=cfg,
                              factory=lambda key, port: Spawning(key, key, cfg.mock))
        a = m.acquire("alpha", card.client.actor)
        b = m.acquire("beta", card.client.actor)

        def named():
            return [(x["pid"], x["proof"], x["model"])
                    for x in (co.rental() or {}).get("processes") or []]
        self.wait_for(lambda: named() == [(a.pid, a.load_id, "alpha"), (b.pid, b.load_id, "beta")],
                      "both processes to be named")
        with self.assertRaises(BackendError):
            m.unload()                                      # alpha goes; beta will not
        self.wait_for(lambda: named() == [(b.pid, b.load_id, "beta")],
                      "alpha off the list, beta still on it")
        time.sleep(0.8)                                     # several heartbeats
        self.assertEqual(named(), [(b.pid, b.load_id, "beta")],
                         "a process whose stop failed was taken off the list")
        again = m.acquire("alpha", card.client.actor)       # beta's stop, asked again, works
        self.wait_for(lambda: named() == [(again.pid, again.load_id, "alpha")],
                      "beta off the list once it went, the new alpha on it")
        events = m.recent_events()
        self.assertEqual((max_concurrency(events), side_by_side(events)), (1, 2))
        self.assertEqual(co.kinds().count("rental_granted"), 1, "the card was let go in between")

    def this_machine(self):
        """The coordinator's machine as its command line builds it - the card
        stood in for - with one thing of the row's own: its query of the named
        processes' command lines is asked again when the process list could not
        answer. On a busy machine one did not answer within its 30 s, and the
        reclaim left alone the process it could not read the proof of (that
        reading is the coordinator suite's to prove); what these rows end is
        decided by the proof, never by a question nobody answered. And the
        row's folder outlives the reclaim: its thread, still ending, once
        wrote into a folder the cleanup had removed.
        -> (the machine, how long its reclaim may take by its own bounds)"""
        import machine                                      # the coordinator's, beside it

        def read(pids):
            return asked_again(lambda: machine.command_lines(pids),
                               lambda lines: None not in lines.values(),
                               f"the command lines of {sorted(pids)}")
        bound = ASK_AGAIN_S + machine.QUERY_S + machine.END_WAIT_S + machine.END_JOIN_MARGIN_S
        self.addCleanup(self.reclaims_ended, bound)
        return machine.ThisMachine(query=lambda: None,
                                   end=lambda named: machine.end_all_on_proof(named, read=read)), \
            bound + 10.0

    @staticmethod
    def reclaims_ended(within_s: float) -> None:
        deadline = time.monotonic() + within_s
        for thread in threading.enumerate():
            if thread.name.startswith("reclaim-"):
                thread.join(max(0.0, deadline - time.monotonic()))

    def test_a_holder_that_hangs_has_its_model_server_ended_on_proof(self):
        hardware, reclaim_s = self.this_machine()
        co = self.coord(ttl=1.0, hardware=hardware)
        m, client, _ = self.llama(co)
        up = m.acquire("story", client.actor)
        pid, load = up.pid, up.load_id
        deadline = time.monotonic() + 5
        while [(x["pid"], x["proof"]) for x in (co.rental() or {}).get("processes") or []] \
                != [(pid, load)]:
            self.assertLess(time.monotonic(), deadline, "the rental never named its process")
            time.sleep(0.02)
        # the holder HANGS: it says nothing more, and nothing else moves
        client._stop.set()
        client._now.set()
        deadline = time.monotonic() + reclaim_s
        while not co.of("rental_reclaimed"):
            self.assertLess(time.monotonic(), deadline, "the silent holder's rental was never "
                                                        "taken back")
            time.sleep(0.05)
        (rec,) = co.of("rental_reclaimed")
        self.assertIn(f"ended its process {pid} ({load})", rec["detail"])
        self.assertEqual([e["process"]["pid"] for e in co.of("process_ended")], [pid])
        self.assertIsNotNone(up.proc.poll(), "the hung holder's llama-server is still running")
        # and the next holder is handed a card with nothing left on it
        m2, client2, _ = self.llama(co)
        self.assertTrue(m2.acquire("story", client2.actor).is_alive())

    def test_a_reclaim_whose_query_could_not_answer_asks_it_again(self):
        """Provoked: the process list takes 21 s to answer the reclaim's first
        query - longer than the 20 s this row once gave the whole reclaim - and
        then answers nothing, as a query that ran out its bound does (wave 3,
        beside three sweeps). Asked again, the proof is read, and the hung
        holder's server is ended on it."""
        import machine
        real, refused = machine.command_lines, []

        def slow_and_silent_once(pids):
            if not refused:
                refused.append(sorted(pids))
                time.sleep(21.0)
                return {int(pid): None for pid in pids}
            return real(pids)
        self.addCleanup(setattr, machine, "command_lines", real)
        machine.command_lines = slow_and_silent_once
        self.test_a_holder_that_hangs_has_its_model_server_ended_on_proof()
        self.assertEqual(len(refused), 1, "the process list was never asked")

    def test_a_process_its_rental_did_not_start_is_never_ended(self):
        """A pid named with a proof its command line does not carry - a process
        that is not the holder's - is left alone when the rental lapses.
        CHANGED ON PURPOSE (H4): because its command line does not carry the
        proof - "left alone" alone was also what a query that could not
        answer says."""
        hardware, reclaim_s = self.this_machine()
        co = self.coord(ttl=1.0, hardware=hardware)
        m, client, card = self.llama(co)
        up = m.acquire("story", client.actor)
        stranger = ids.mint_actor("load")                   # a load's id its command line lacks
        card.running(up.pid, stranger, "story")             # the same pid, named anew
        deadline = time.monotonic() + 5
        while [(x["pid"], x["proof"]) for x in (co.rental() or {}).get("processes") or []] \
                != [(up.pid, stranger)]:
            self.assertLess(time.monotonic(), deadline, "the rental never named the process")
            time.sleep(0.02)
        client._stop.set()
        client._now.set()
        deadline = time.monotonic() + reclaim_s
        while not co.of("rental_reclaimed"):
            self.assertLess(time.monotonic(), deadline, "the rental was never taken back")
            time.sleep(0.05)
        (rec,) = co.of("rental_reclaimed")
        self.assertIn(f"does not carry {stranger} on its command line", rec["detail"])
        self.assertIn("left alone", rec["detail"])
        self.assertEqual([e["process"]["pid"] for e in co.of("process_left_alone")], [up.pid])
        self.assertIsNone(up.proc.poll(), "a process that did not carry the proof was ended")

    def test_the_probe_asks_up_never_the_operators_state(self):
        """Whether the coordinator answers is asked at a route of its own: no
        answer to an application names another, and /state names them all."""
        co = self.coord()
        client = self.client(co.url)
        calls = self.counted(client)
        self.assertTrue(client.probe())
        self.assertEqual(calls, ["coordinator.up"])
        dead = self.client(f"http://127.0.0.1:{free_port()}")
        self.assertFalse(dead.probe())


class NativeVersionRows(Case):
    """PLAN-4 D13: the coordinator carries the contract's version, at the one
    route a server asks it (`/up`), as a server's health carries its own - so
    a server can tell whether it and its coordinator speak one contract."""

    def test_the_probe_keeps_the_coordinators_contract_version_and_says_nothing_when_it_is_ours(self):
        co = self.coord()
        client = self.client(co.url)
        self.assertIsNone(client.coordinator_version, "a version known before anything was asked")
        self.assertTrue(client.probe())
        ours = V.contract_version(K.CONTRACT)
        self.assertEqual((client.coordinator_version, co.c.contract_version), (ours, ours))
        self.assertFalse([s for s in self.said if "contract" in s], self.said)

    def test_a_coordinator_of_another_contract_is_said_naming_both_versions(self):
        co = self.coord()
        co.c.contract_version = "0123456789ab"             # a coordinator built from another
        client = self.client(co.url)
        self.assertTrue(client.probe(), "a coordinator of another contract is still up")
        self.assertEqual(client.coordinator_version, "0123456789ab")
        said = [s for s in self.said if "0123456789ab" in s]
        self.assertEqual(len(said), 1, self.said)
        self.assertIn(V.contract_version(K.CONTRACT), said[0], "the line names only one side")
        client.probe()
        self.assertEqual(len([s for s in self.said if "0123456789ab" in s]), 1,
                         "said again for the same version")


class NativeWatchRows(Case):
    """PLAN-4 D3, end to end: the coordinator watches every rental. Each
    heartbeat says the work - whether a model answers, its progress, which
    only real work moves - and a rental that says it is working and does not
    move is taken back as a crashed one is, its model's server ended on
    proof. A card over-full - its free memory under the floor, or a memory
    error a model's log said - marks the rental degraded: the server's work
    fails saying so, and nothing new loads, until the card has room again;
    the rental is never ended for it. And a job refused for the card's memory
    ends with a reason of its own."""

    def llama(self, co, **kw):
        return NativeNamedProcessRows.llama(self, co, **kw)

    def pipeline_state(self, m, state, timeout=5.0):
        return self.wait_for(lambda: m.pipeline_status()["state"] == state and m.pipeline_status(),
                             f"the pipeline to be {state}", timeout)

    def test_each_heartbeat_says_the_work_and_the_coordinator_knows_it(self):
        co = self.coord()
        m, card = self.holder(co.url, cfg=self.config(tokens_per_second=40))
        b = m.acquire("alpha", card.client.actor)
        box = {}
        t = threading.Thread(target=lambda: box.update(got=list(m.answer(
            b, "Tell me a long story.", GenParams(max_tokens=40)))), daemon=True)
        t.start()
        watch = self.wait_for(lambda: (co.c.state()[1]["watch"] or {}).get("working")
                              and (co.c.state()[1]["watch"]["progress"] or 0) > 0
                              and co.c.state()[1]["watch"], "the coordinator to see it work")
        self.assertTrue(watch["working"])
        t.join(10)
        tokens = sum(1 for ev in box["got"] if ev["type"] == "token")
        self.wait_for(lambda: co.c.state()[1]["watch"]["progress"] == tokens + 1
                      and not co.c.state()[1]["watch"]["working"],
                      "its last beat: done, every token and the answer counted")
        self.assertFalse(co.of("rental_hung"))

    def test_a_rental_whose_work_does_not_move_is_taken_back_as_for_a_crash(self):
        """The coordinator's machine here writes down what it is handed to end
        on proof, and ends nothing: ending a process on its proof is the
        coordinator's own suite's, on real processes (S3). The stand-in's
        answer never comes (its first token is 30 s away): the rental says it
        is working, and its progress does not move."""
        ending = Ending()
        co = self.coord(ttl=1.0, hardware=ending, hang_s=1.0)
        m, client, card = self.llama(co, args=["--fake-first-token-s", "30"])
        up = m.acquire("story", client.actor)
        pid, load = up.pid, up.load_id
        box = {}

        def answer():
            try:
                list(m.answer(up, "Tell me about rain.", GenParams(max_tokens=20)))
            except Exception as exc:                        # noqa: BLE001 - read below
                box["exc"] = exc
        t = threading.Thread(target=answer, daemon=True)
        t.start()
        deadline = time.monotonic() + 20
        while not co.of("rental_reclaimed"):
            self.assertLess(time.monotonic(), deadline, "the rental that did not move was never "
                                                        "taken back")
            time.sleep(0.05)
        (hung,) = co.of("rental_hung")
        self.assertIn("progress had not moved for 1s", hung["detail"])
        (rec,) = co.of("rental_reclaimed")
        self.assertTrue(rec["detail"].startswith("hung: "), rec["detail"])
        self.assertEqual([[(x["pid"], x["proof"]) for x in named] for named in ending.ended],
                         [[(pid, load)]], "its model's server was not handed over to be ended "
                                          "on its proof")
        self.wait_for(lambda: m.card_lost == "reclaimed", "the server to hear it lost the card")
        t.join(15)
        self.assertIsInstance(box.get("exc"), BackendError, "its answer did not end")
        self.wait_for(lambda: "unload_ok" in self.events(m), "its model to come off")
        self.assertFalse(co.of("rental_released"), "it was released, not taken back")

    def test_a_session_that_says_it_is_not_working_is_never_hung(self):
        co = self.coord(hang_s=0.3)
        m, card = self.holder(co.url)
        m.open_pipeline(card.client.actor, models=["alpha"], keep_s=0, wait_s=5)
        time.sleep(1.5)                                     # five hang limits, beats all along
        self.assertEqual(co.rental()["state"], "held")
        self.assertFalse(co.of("rental_hung"))

    def test_a_card_over_full_degrades_the_rental_and_its_work_fails_saying_why(self):
        hw = Machine(used=1000)
        co = self.coord(hardware=hw, watch_s=0.1, floor_mib=256)
        m, card = self.holder(co.url)
        opened = m.open_pipeline(card.client.actor, models=["alpha"], keep_s=0, wait_s=5)
        self.assertEqual(opened["up"], ["alpha"])
        hw.readings = [24400]                               # 176 MiB free
        st = self.pipeline_state(m, "degraded")
        self.assertIn("176 MiB free of 24576, under the floor of 256 MiB", st["why"])
        self.assertEqual(co.rental()["state"], "held", "the rental was ended for it")
        with self.assertRaises(BackendError) as caught:
            m.acquire("alpha", card.client.actor)
        self.assertEqual(getattr(caught.exception, "reason", None), "card_degraded")
        with self.assertRaises(BackendError):
            m.acquire("beta", card.client.actor)
        self.assertFalse(co.of("rental_amended")[1:], "a model was put on a degraded card")
        hw.readings = [2000]
        self.pipeline_state(m, "holding")
        self.assertTrue(m.acquire("beta", card.client.actor).is_alive(),
                        "work goes on once the card has room again")
        self.assertEqual([e["kind"] for e in co.c.events
                          if e["kind"] in ("rental_degraded", "rental_restored")],
                         ["rental_degraded", "rental_restored"])
        # given back with its memory back where it was granted: the release's
        # check ends at once, never after the row's folder is gone
        hw.readings = [1000]
        m.close_pipeline(opened["id"])

    def test_a_memory_error_in_a_models_log_degrades_the_rental(self):
        co = self.coord(ttl=1.0)
        m, client, card = self.llama(co, args=["--fake-oom-after", "1"])
        up = m.acquire("story", client.actor)
        with self.assertRaises(BackendError) as caught:
            list(m.answer(up, "Tell me about rain.", GenParams(max_tokens=20)))
        self.assertEqual(getattr(caught.exception, "reason", None), "card_degraded")
        d = self.wait_for(lambda: (co.rental() or {}).get("degraded"),
                          "the coordinator to hear the memory error")
        self.assertEqual((d["reason"], d["error"]), ("memory_error", "CUDA error: out of memory"))
        self.wait_for(lambda: m.degraded, "the server to be told on its next heartbeat")
        self.assertEqual(co.rental()["state"], "held")

    def test_the_card_full_under_a_coordinator_the_job_runs_again_once_it_has_room(self):
        """S5b, end to end with a real coordinator: the stand-in runs out of
        memory mid-answer; its job fails marked for the queue to run it again
        (`card_full`, the coordinator's numbers); run again, it is held - the
        coordinator reading the card every recover_watch_s, its progress
        saying so - and once the card has room for what the rental needs it
        runs by itself, its model loaded again first."""
        hw = Machine(used=1000)
        co = self.coord(ttl=1.0, hardware=hw, watch_s=30, recover_watch_s=0.05)
        m, client, card = self.llama(co, args=["--fake-oom-after", "1", "--fake-oom-once",
                                               str(self.tmp / "broke-once")])
        a = RunnerBackend(m, log=self.said.append, idle_poll_s=3600)
        a.attach(client.actor)
        self.addCleanup(a.close)
        m.acquire("story", client.actor)
        hw.readings = [24400]                               # 176 MiB free: the card is full
        with self.assertRaises(Q.GenerationFailed) as caught:
            a({"prompt": "Tell me about rain."}, {"model": "story", "max_tokens": 20},
              Run(priority="interactive"))
        self.assertEqual(caught.exception.reason, "card_degraded")
        self.assertIsNotNone(caught.exception.card_full, "not marked to be run again")
        d = self.wait_for(lambda: (co.rental() or {}).get("degraded"), "the coordinator's mark")
        self.assertEqual(d["reason"], "memory_error")
        run, box = Run(priority="interactive"), {}
        run.card_waited_s = 0.0

        def again():
            try:
                box["out"] = a({"prompt": "Tell me about rain."},
                               {"model": "story", "max_tokens": 20}, run)
            except Exception as exc:                        # noqa: BLE001 - read below
                box["exc"] = exc
        t = threading.Thread(target=again, daemon=True)
        t.start()
        said = self.wait_for(lambda: next((r for r in run.reports if r.get("stage")
                                           == "waiting for the card's memory"
                                           and r.get("free_mib") == 176), None),
                             "the job to say it waits for the card's memory, in the numbers "
                             "of the coordinator's newest read")
        self.assertIn("Close something, and this runs again by itself.", said["waiting_for"])
        time.sleep(0.3)
        self.assertTrue(t.is_alive(), f"it did not wait: {box}")
        hw.readings = [1000]                                # the programs are closed
        t.join(20)
        self.assertEqual(box.get("out", {}).get("model"), "story", box)
        back = co.of("rental_restored")
        self.assertEqual(len(back), 1)
        self.assertIn("MiB needed", back[0]["detail"])

    def test_a_new_rental_counts_its_work_from_its_grant(self):
        """What a runner counted before a rental - its progress, its memory
        errors - is not that rental's: a new rental is never degraded for an
        error an earlier one's model had."""
        co = self.coord()
        client = self.client(co.url)
        card = K.CoordinatorCard(client, patience_s=10.0, retry_s=0.05)
        said = {"working": False, "progress": 40, "deadline_s": None, "memory_errors": 3,
                "memory_error": "CUDA error: out of memory"}
        card.work = lambda: dict(said)
        card.take(client.actor, "alpha")
        self.wait_for(lambda: (co.c.state()[1]["watch"] or {}).get("progress") == 0,
                      "its first heartbeat, counting from the grant")
        self.assertIsNone(co.rental()["degraded"], "degraded for errors from before it")
        said.update(progress=45, memory_errors=4)
        self.wait_for(lambda: (co.rental() or {}).get("degraded"), "the new error")
        self.assertEqual(co.c.state()[1]["watch"]["progress"], 5)

    def test_a_slow_answer_travels_on_the_heartbeat_counted_from_the_grant(self):
        """PLAN-4 D3 with D12 (S11b): the runner's count of answers far slower
        than their model's normal travels on each heartbeat, counted from the
        rental's grant as its memory errors are, with the newest in words -
        and on a card the coordinator last read as full, one more marks the
        rental degraded (`slow_answer`); the ones from before it never do."""
        hw = Machine(used=24576 - 800)                      # 800 MiB free: full, over the floor
        co = self.coord(hardware=hw, watch_s=0.1)
        client = self.client(co.url)
        card = K.CoordinatorCard(client, patience_s=10.0, retry_s=0.05)
        slow = "an answer of alpha was 4.2 times slower than its normal"
        said = {"working": False, "progress": 40, "deadline_s": None, "memory_errors": 0,
                "memory_error": None, "slow_answers": 3, "slow_answer": "an older one"}
        card.work = lambda: dict(said)
        card.take(client.actor, "alpha")
        self.wait_for(lambda: (co.c.state()[1]["watch"] or {}).get("read_at"),
                      "the coordinator's look at the card during the rental")
        said.update(progress=41)
        self.wait_for(lambda: (co.c.state()[1]["watch"] or {}).get("progress") == 1,
                      "a heartbeat after that look")
        self.assertIsNone(co.rental()["degraded"], "degraded for slow answers from before it")
        said.update(progress=42, slow_answers=4, slow_answer=slow)
        d = self.wait_for(lambda: (co.rental() or {}).get("degraded"), "the slow answer")
        self.assertEqual((d["reason"], d["error"], d["free_mib"]), ("slow_answer", slow, 800))

    def test_an_amend_refused_degraded_refuses_the_load_before_a_heartbeat_says_so(self):
        """The coordinator's refusal is the card's own line of defence: a
        server not yet told - its heartbeat minutes away - puts nothing on a
        degraded card either."""
        hw = Machine(used=1000)
        co = self.coord(ttl=600.0, hardware=hw, watch_s=0.1)
        m, card = self.holder(co.url)
        m.acquire("alpha", card.client.actor)
        hw.readings = [24400]
        self.wait_for(lambda: (co.rental() or {}).get("degraded"), "the coordinator's look")
        self.assertIsNone(m.degraded, "told already: this row needs a server not yet told")
        with self.assertRaises(BackendError) as caught:
            m.acquire("beta", card.client.actor)
        self.assertEqual(getattr(caught.exception, "reason", None), "card_degraded")
        self.assertIn("beta may not go on the card now: the rental is degraded",
                      str(caught.exception))
        self.assertEqual(m.status()["loaded"], "alpha",
                         "a model was taken off for room a degraded card does not have")
        hw.readings = [1000]

    def test_through_the_lock_the_work_and_the_degraded_card_reach_its_coordinator(self):
        """A server that found its coordinator through the machine's lock
        says its work to it, and hears the card degraded from it, as one
        named on its command line does."""
        from runner.cardlock import MachineCard
        path = self.tmp / "card.lock"
        hw = Machine(used=1000)
        c = C.Coordinator(self.tmp / "found", hardware=hw, ports=registry(), ttl_s=0.6,
                          watch_s=0.1)
        srv = C.serve(c, free_port(), card_lock=path)
        self.addCleanup(lambda: C.stop(srv) if srv.card_lock.held else None)

        def through(url, holder):
            client = K.CoordinatorClient(url, holder, "found-app", log=self.said.append)
            return K.CoordinatorCard(client, engine="mock")
        card = MachineCard(path, through=through)
        m = ModelManager(self.config(tokens_per_second=200), self.tmp / "found-folder",
                         vram=lambda: None, card=card)
        self.addCleanup(m.shutdown)
        actor = ids.mint_actor("srv")
        opened = m.open_pipeline(actor, models=["alpha"], keep_s=0, wait_s=5)
        b = m.acquire("alpha", actor)
        # ADDED (S11b, PLAN-4 D12): the take its pipeline made for its first
        # model is that model's load's - its wait, and its claim as the
        # coordinator found through the lock timed it
        load = next(e["measures"] for e in m.stat_lines() if e["what"] == "load")
        self.assertEqual(list(load)[:2], ["load_wait_s", "load_claim_s"])
        self.assertGreater(load["load_claim_s"], 0)
        n = sum(1 for ev in m.answer(b, "hi", GenParams(max_tokens=10)) if ev["type"] == "token")
        self.wait_for(lambda: (c.state()[1]["watch"] or {}).get("progress") == n + 1,
                      "the work said to the coordinator found through the lock")
        hw.readings = [24400]
        self.wait_for(lambda: m.degraded, "the card degraded, heard from it")
        hw.readings = [1000]
        self.wait_for(lambda: m.degraded is None, "room again")
        m.close_pipeline(opened["id"])

    def test_a_job_refused_for_the_cards_memory_ends_with_a_reason_of_its_own(self):
        for used, need, reason in ((24000, 9000, "no_room"), (1000, 30000, "never_fits")):
            with self.subTest(reason=reason):
                co = self.coord(hardware=Machine(used=used))
                client = self.client(co.url)
                card = K.CoordinatorCard(client, patience_s=10.0, retry_s=0.05,
                                         needs=lambda key, n=need: n)
                m = ModelManager(self.config(), self.tmp / f"folder-{client.actor}",
                                 vram=lambda: None, card=card)
                self.addCleanup(m.shutdown)
                a = RunnerBackend(m, log=self.said.append, idle_poll_s=3600)
                a.attach(client.actor)
                self.addCleanup(a.close)
                with self.assertRaises(Q.GenerationFailed) as caught:
                    a({"prompt": "hi"}, {"model": "alpha", "max_tokens": 5},
                      Run(priority="interactive"))
                self.assertEqual(caught.exception.reason, reason)
                self.assertIn(f"it needs {need} MiB", str(caught.exception))

    def test_no_room_now_under_a_coordinator_fails_at_once_loudly_in_the_job_and_the_log(self):
        """H9, the owner's ruling of 2026-09-28 ("Fail loudly"): a model that
        does not fit on a card that is not over-full, under a coordinator -
        at its ticket's claim (responsive work) and at an amend (a second
        model, the rental held) - fails at once, `no_room`, in the
        coordinator's words: what programs outside the bootstrap hold and
        what to do. The job's failure says them, and so does the server's
        log."""
        for at in ("claim", "amend"):
            with self.subTest(at=at):
                hw = Machine(used=24000 if at == "claim" else 1000)
                co = self.coord(hardware=hw)
                client = self.client(co.url)
                card = K.CoordinatorCard(client, patience_s=10.0, retry_s=0.05,
                                         needs=lambda key: 9000)
                m = ModelManager(self.config(), self.tmp / f"folder-loud-{at}",
                                 vram=lambda: None, card=card)
                self.addCleanup(m.shutdown)
                a = RunnerBackend(m, log=self.said.append, idle_poll_s=3600)
                a.attach(client.actor)
                self.addCleanup(a.close)
                if at == "amend":
                    a({"prompt": "hi"}, {"model": "alpha", "max_tokens": 5},
                      Run(priority="interactive"))
                    hw.readings = [20000]           # a program outside took the card since
                del self.said[:]
                began = time.monotonic()
                with self.assertRaises(Q.GenerationFailed) as caught:
                    a({"prompt": "hi"}, {"model": "beta", "max_tokens": 5},
                      Run(priority="interactive"))
                self.assertLess(time.monotonic() - began, 5.0, "it waited for room")
                said = str(caught.exception)
                outside = 24000 if at == "claim" else 20000
                self.assertEqual(caught.exception.reason, "no_room", said)
                for words in (f"programs outside the bootstrap hold {outside} MiB",
                              "Close programs that use the card, or reboot"):
                    self.assertIn(words, said)
                lines = [line for line in self.said if "Close programs that use the card" in line]
                self.assertEqual(len(lines), 1, f"the server's log: {self.said}")
                self.assertIn(f"programs outside the bootstrap hold {outside} MiB", lines[0])


class NativeScheduledRows(Case):
    """S18, the owner's second ruling of 2026-09-28, under a coordinator:
    scheduled work whose model does not fit at its claim waits for room - or,
    by the server's setting or its own `if_no_room`, fails at once, loudly.
    A JOB that waits for room does not wait on a thread holding the server's
    one place in line: its ticket is kept by the card source (PARKED), the
    job goes back to its queue, and the queue's look (`room_look`) says when
    the ticket is offered - then the job's take claims it. A job planned for
    later keeps such a place in line from the morning, holding back nobody
    until its time. A patience that runs out while it waits fails in the
    loud words, never only "patience ran out"."""

    def scheduled(self, co, cfg=None, **kw):
        client = self.client(co.url)
        card = K.CoordinatorCard(client, patience_s=kw.pop("patience_s", 10.0), retry_s=0.05,
                                 wait_s=0.2, engine="mock", needs=lambda key: 8000)
        m = ModelManager(cfg or self.config(), self.tmp / f"folder-{client.actor}",
                         vram=lambda: None, card=card)
        self.addCleanup(m.shutdown)
        return m, card

    def test_scheduled_work_that_asked_to_fail_is_refused_at_once_loudly(self):
        co = self.coord(hardware=Machine(used=20000))
        m, card = self.scheduled(co)
        calls = self.counted(card.client)
        terms = Terms(klass="scheduled", if_no_room="fail")
        t0 = time.monotonic()
        with self.assertRaises(BackendError) as caught:
            m.acquire("alpha", card.client.actor, terms=terms)
        self.assertLess(time.monotonic() - t0, 3.0, "it waited for room")
        said = str(caught.exception)
        self.assertEqual((caught.exception.reason, terms.refused), ("no_room", "no_room"))
        for words in ("programs outside the bootstrap hold 20000 MiB", "does not wait for room",
                      "Close programs that use the card, or reboot"):
            self.assertIn(words, said)
        self.assertEqual(calls.count("coordinator.card.ask"), 1)
        self.assertEqual([e.get("if_no_room") for e in co.of("ticket_asked")], ["fail"])

    def test_the_servers_setting_decides_for_scheduled_work_that_names_none(self):
        for setting, asked in (("fail", "fail"), ("wait", "wait")):
            with self.subTest(setting=setting):
                co = self.coord(hardware=Machine(used=1000))
                cfg = self.config()
                cfg.runner["scheduled_if_no_room"] = setting
                m, card = self.scheduled(co, cfg)
                m.acquire("alpha", card.client.actor, terms=Terms(klass="scheduled"))
                m.acquire("beta", card.client.actor, terms=Terms(klass="responsive"))
                self.assertEqual([e.get("if_no_room") for e in co.of("ticket_asked")
                                  if e.get("actor") == card.client.actor], [asked],
                                 "the server's setting was not what its ticket said")
                co.stop()

    def test_a_pipelines_patience_that_runs_out_waiting_for_room_fails_loudly(self):
        machine = Machine(used=20000)
        co = self.coord(hardware=machine, recheck_s=0.05)
        m, card = self.scheduled(co)
        terms = Terms(klass="scheduled", patience_s=2.0)
        heard = []
        t0 = time.monotonic()
        threading.Timer(0.5, lambda: setattr(machine, "used", 22000)).start()
        with self.assertRaises(BackendError) as caught:
            m.acquire("alpha", card.client.actor, terms=terms, on_wait=heard.append)
        self.assertGreaterEqual(time.monotonic() - t0, 2.0, "it did not wait for the room")
        said = str(caught.exception)
        self.assertEqual((caught.exception.reason, terms.refused), ("no_room", "no_room"), said)
        # the room it waits on, read again while it waited: the numbers at the end
        for words in ("did not fit on the card within its patience of 2 s", "8000 MiB",
                      "programs outside the bootstrap hold 22000 MiB", "It waited 2 s for room",
                      "none came", "or reboot"):
            self.assertIn(words, said)
        self.assertTrue(any("starts by itself once it fits" in h for h in heard),
                        f"while it waited, it did not say the room it waits on: {heard}")

    def test_a_job_that_finds_its_place_still_passed_over_goes_back_at_once(self):
        co = self.coord(hardware=Machine(used=20000))
        m, card = self.scheduled(co)
        calls = self.counted(card.client)
        for _ in range(2):
            t0 = time.monotonic()
            with self.assertRaises(BackendError) as caught:
                m.acquire("alpha", card.client.actor, terms=Terms(klass="scheduled", parks=True))
            self.assertLess(time.monotonic() - t0, 3.0, "it waited for room on its thread")
            self.assertIsInstance(getattr(caught.exception, "room", None), dict)
        self.assertEqual(calls.count("coordinator.card.ask"), 1, f"it asked again: {calls}")
        self.assertEqual(len(co.tickets()), 1)

    def test_a_take_under_way_goes_first_and_no_second_place_is_asked_meanwhile(self):
        co = self.coord(hardware=Machine(used=1000))
        other, other_card = self.scheduled(co)
        other.acquire("alpha", other_card.client.actor)     # the card is another's
        m, card = self.scheduled(co)
        me = card.client.actor
        taking = threading.Thread(target=lambda: self.suppress(m.acquire, "alpha", me),
                                  daemon=True)
        taking.start()
        self.wait_for(lambda: [t for t in co.tickets() if t["actor"] == me],
                      "this server's take in line")
        look = m.room_look({"job": "j-0002", "model": "beta", "not_before": None, "for": CORR,
                            "if_no_room": None, "waited": False}, me)
        self.assertEqual(look, {"due": False, "room": None})
        self.assertEqual(len([e for e in co.of("ticket_asked") if e.get("actor") == me]), 1,
                         "a second place in line was asked while a take was under way")
        other_card.give_back()
        taking.join(10)
        self.assertFalse(taking.is_alive(), "the take never had the card")

    def test_the_servers_setting_is_what_a_place_kept_in_line_asks_with(self):
        co = self.coord(hardware=Machine(used=1000))
        cfg = self.config()
        cfg.runner["scheduled_if_no_room"] = "fail"
        m, card = self.scheduled(co, cfg)
        m.room_look({"job": "j-0001", "model": "alpha", "not_before": None, "for": CORR,
                     "if_no_room": None, "waited": False}, card.client.actor)
        self.assertEqual([e.get("if_no_room") for e in co.of("ticket_asked")], ["fail"])

    def test_while_this_server_holds_the_card_its_waiting_work_goes_on_in_its_rental(self):
        clock = FakeClock()
        co = self.coord(hardware=Machine(used=1000), clock=clock, drop_s=10 ** 6)
        m, card = self.scheduled(co)
        want = {"job": "j-0001", "model": "beta", "not_before": clock.now + 3600, "for": CORR,
                "if_no_room": None, "waited": False}
        m.room_look(want, card.client.actor)
        self.assertEqual(len(co.of("ticket_asked")), 1)
        other, other_card = self.scheduled(co)
        other.acquire("alpha", other_card.client.actor)     # the card is another's
        other_card.give_back()
        m.acquire("alpha", card.client.actor)            # ...then this server's own rental
        look = m.room_look(want, card.client.actor)
        self.assertEqual(look["due"], True, "its work waits while its own server holds the card")
        self.assertEqual(len(co.of("ticket_cancelled")), 1, "a place was kept that nothing needs")

    def test_a_place_is_asked_again_when_the_work_it_keeps_changes_and_given_up_on_close(self):
        clock = FakeClock()
        co = self.coord(hardware=Machine(used=1000), clock=clock, drop_s=10 ** 6)
        m, card = self.scheduled(co)
        want = {"job": "j-0001", "model": "alpha", "not_before": clock.now + 3600, "for": CORR,
                "if_no_room": None, "waited": False}
        m.room_look(want, card.client.actor)
        m.room_look(dict(want, not_before=clock.now + 7200), card.client.actor)
        self.assertEqual([e.get("not_before") for e in co.of("ticket_asked")],
                         [clock.now + 3600, clock.now + 7200])
        self.assertEqual(len(co.of("ticket_cancelled")), 1, "the first place was kept too")
        m.room_look(dict(want, model="beta", not_before=clock.now + 7200), card.client.actor)
        self.assertEqual(len(co.of("ticket_asked")), 3, "a place kept for another model")
        card.close()
        self.assertEqual(len(co.of("ticket_cancelled")), 3, "a place outlived its card source")

    def test_through_the_machines_lock_the_same_card_source_keeps_the_place_and_claims_it(self):
        from runner.cardlock import MachineCard
        path = self.tmp / "card.lock"
        c = C.Coordinator(self.tmp / "via", hardware=Machine(used=1000), ports=registry())
        srv = C.serve(c, free_port(), card_lock=path)
        self.addCleanup(lambda: C.stop(srv) if srv.card_lock.held else None)
        made = []

        def through(url, holder):
            client = K.CoordinatorClient(url, holder, "night-app", log=self.said.append)
            made.append(client)
            return K.CoordinatorCard(client, engine="mock", needs=lambda key: 1000, wait_s=0.2)
        card = MachineCard(path, through=through)
        m = ModelManager(self.config(), self.tmp / "via-folder", vram=lambda: None, card=card)
        self.addCleanup(m.shutdown)
        actor = ids.mint_actor("srv")
        want = {"job": "j-0001", "model": "alpha", "not_before": None, "for": CORR,
                "if_no_room": None, "waited": False}
        look = m.room_look(want, actor)
        self.assertEqual(len([e for e in c.events if e["kind"] == "ticket_asked"]), 1,
                         "no place was kept through the coordinator the lock names")
        self.assertTrue(look["due"] or m.room_look(want, actor)["due"])
        m.acquire("alpha", actor, terms=Terms(klass="scheduled", parks=True))
        asked = [e for e in c.events if e["kind"] == "ticket_asked"]
        self.assertEqual(len(asked), 1, "its take asked again, not claiming the place kept")
        self.assertEqual(c.state()[1]["rental"]["id"], asked[0]["ticket_id"])
        self.assertEqual(len(made), 1, "a second card source was made for the take")

    def test_through_the_machines_lock_a_place_kept_outlives_a_session(self):
        from runner.cardlock import MachineCard
        path = self.tmp / "card-kept.lock"
        clock = FakeClock()
        c = C.Coordinator(self.tmp / "via-kept", hardware=Machine(used=1000), ports=registry(),
                          clock=clock, drop_s=10 ** 6)
        srv = C.serve(c, free_port(), card_lock=path)
        self.addCleanup(lambda: C.stop(srv) if srv.card_lock.held else None)

        def through(url, holder):
            client = K.CoordinatorClient(url, holder, "night-app", log=self.said.append)
            return K.CoordinatorCard(client, engine="mock", needs=lambda key: 1000, wait_s=0.2)
        card = MachineCard(path, through=through)
        m = ModelManager(self.config(), self.tmp / "via-kept-folder", vram=lambda: None,
                         card=card)
        self.addCleanup(m.shutdown)
        actor = ids.mint_actor("srv")
        m.room_look({"job": "j-0001", "model": "beta", "not_before": clock.now + 3600,
                     "for": CORR, "if_no_room": None, "waited": False}, actor)
        m.acquire("alpha", actor)                          # live work, meanwhile
        m.unload("alpha")
        card.give_back()
        self.assertEqual([e for e in c.events if e["kind"] == "ticket_cancelled"], [],
                         "the place kept for tonight went with the live session")
        self.assertIsNotNone(getattr(card._via, "parked", None))

    def test_a_job_waiting_for_room_parks_its_ticket_and_claims_it_once_offered(self):
        machine = Machine(used=20000)
        co = self.coord(hardware=machine, recheck_s=0.05)
        m, card = self.scheduled(co)
        calls = self.counted(card.client)
        t0 = time.monotonic()
        with self.assertRaises(BackendError) as caught:
            m.acquire("alpha", card.client.actor, terms=Terms(klass="scheduled", parks=True))
        self.assertLess(time.monotonic() - t0, 3.0, "a job waited for room on its thread")
        room = getattr(caught.exception, "room", None)
        self.assertIsInstance(room, dict, "the refusal does not say the room it waits on")
        self.assertEqual((room["memory"]["need_mib"], room["memory"]["free_mib"],
                          room["outside_mib"]), (8000, 24576 - 20000, 20000))
        self.assertIn("starts by itself once it fits", room["words"])
        (ticket,) = co.tickets()
        self.assertEqual((ticket["state"], ticket["class"]), ("waiting", "scheduled"),
                         "the ticket did not stay in line")
        self.assertEqual(m.pipeline_status()["state"], "closed", "the card is held for a job "
                                                                  "that waits for room")
        want = {"job": "j-0001", "model": "alpha", "not_before": None, "for": CORR,
                "if_no_room": None, "waited": True}
        look = m.room_look(want, card.client.actor)
        self.assertEqual(look["due"], False)
        self.assertEqual(look["room"]["outside_mib"], 20000)
        machine.readings = [1000]
        self.wait_for(lambda: m.room_look(want, card.client.actor)["due"],
                      "the parked ticket offered once the card has room")
        m.acquire("alpha", card.client.actor, terms=Terms(klass="scheduled", parks=True))
        self.assertEqual(calls.count("coordinator.card.ask"), 1, f"it asked again: {calls}")
        self.assertEqual(co.rental()["id"], ticket["id"], "the parked ticket was not claimed")

    def test_a_planned_job_keeps_a_place_in_line_from_the_morning_holding_back_nobody(self):
        clock = FakeClock()
        co = self.coord(hardware=Machine(used=1000), clock=clock, drop_s=10 ** 6)
        m, card = self.scheduled(co)
        tonight = clock.now + 9 * 3600
        want = {"job": "j-0001", "model": "alpha", "not_before": tonight, "for": CORR,
                "if_no_room": "wait", "waited": False}
        look = m.room_look(want, card.client.actor)
        self.assertEqual(look["due"], False)
        (asked,) = co.of("ticket_asked")
        self.assertEqual((asked.get("not_before"), asked.get("class"), asked.get("for")),
                         (tonight, "scheduled", CORR))
        other, other_card = self.scheduled(co)
        other.acquire("beta", other_card.client.actor)
        self.assertEqual(co.rental()["holder"], "writer-app")
        other_card.give_back()
        m.room_look(want, card.client.actor)
        self.assertEqual(len(co.of("ticket_asked")), 2, "a look asked a second ticket for the "
                                                        "same job")
        clock.advance(9 * 3600)
        co.c.reap()
        self.assertTrue(m.room_look(want, card.client.actor)["due"],
                        "its time came on a free card, and it was not offered")
        m.acquire("alpha", card.client.actor, terms=Terms(klass="scheduled", parks=True))
        self.assertEqual(co.rental()["id"], asked["ticket_id"])

    def test_a_place_kept_in_line_is_given_up_once_nothing_waits_and_asked_again_if_dropped(self):
        clock = FakeClock()
        co = self.coord(hardware=Machine(used=1000), clock=clock)
        m, card = self.scheduled(co)
        want = {"job": "j-0001", "model": "alpha", "not_before": clock.now + 3600, "for": CORR,
                "if_no_room": None, "waited": False}
        m.room_look(want, card.client.actor)
        m.room_look(None, card.client.actor)
        self.assertEqual(len(co.of("ticket_cancelled")), 1, "the place was kept for nobody")
        m.room_look(want, card.client.actor)
        clock.advance(8 * 3600)                  # the machine slept: nobody looked
        co.c.reap()
        self.assertEqual(len(co.of("ticket_dropped")), 1)
        look = m.room_look(want, card.client.actor)
        self.assertEqual(len(co.of("ticket_asked")), 3, "a dropped place was not asked again")
        co.c.reap()
        self.assertTrue(look["due"] or m.room_look(want, card.client.actor)["due"],
                        "asked again for its time, now past, it was not offered at once")


class NativeQuietPortRows(Case):
    """Every copy of this suite on the machine takes its ports from the same
    ranges - a sweep's copies, a gate beside another agent's - and two copies
    that bind-checked one free port in the same moment both started on it; two
    coordinators whose ranges met handed one port to two holders. So each copy
    claims a block of each range for as long as it runs (`_suites.QuietPorts`,
    whose rows are the llama.cpp suite's) and hands out only its own."""

    def test_every_port_this_copy_hands_out_is_of_its_own_block(self):
        first, last = quiet_ports(*QUIET_PORTS).block
        stray = sorted({free_port() for _ in range(50)} - set(range(first, last + 1)))
        self.assertEqual(stray, [], f"not of this copy's block {first}-{last}")

    def test_every_coordinator_hands_out_a_range_of_this_copys_own_block(self):
        first, last = quiet_ports(*REGISTRY_PORTS, size=REGISTRY_BLOCK).block
        for _ in range(6):
            lo, hi = self.coord().ports
            self.assertTrue(first <= lo <= hi <= last,
                            f"{lo}-{hi} is not of this copy's block {first}-{last}")


class ZZEveryPortItsOwn(unittest.TestCase):
    """Last, by name: everything this suite had listen - a server, a
    coordinator, the plain server that is not one - listened on a port of this
    copy's own blocks: QUIET_PORTS for what a row starts, REGISTRY_PORTS for a
    port one of its coordinators handed out - never on port 0 (H6's register
    3)."""

    def test_everything_this_suite_had_listen_listened_on_a_port_of_its_own_blocks(self):
        quiet = quiet_ports(*QUIET_PORTS).block
        handed = quiet_ports(*REGISTRY_PORTS, size=REGISTRY_BLOCK).block
        stray = [(part, asked, had) for part, asked, had in LISTENED
                 if not (quiet[0] <= had <= quiet[1] or handed[0] <= had <= handed[1])]
        self.assertEqual({part for part, _, _ in LISTENED}, {"server", "coordinator", "plain"},
                         "something that listened was not recorded")
        self.assertEqual(stray, [], f"{len(stray)} of {len(LISTENED)} listened outside this "
                                    f"copy's blocks {quiet} and {handed}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from _suites import report                             # the tally, counted in rows
    report(unittest.main(exit=False, verbosity=1))
