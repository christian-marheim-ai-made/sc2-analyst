"""The runner behind the queue: the adapter, and the server that holds it.

    py -3.12 test_adapter.py

No GPU. The model is the stand-in (`--backend mock`) or llama.cpp against the
stand-in llama-server (`--backend llamacpp`). The llama.cpp suite's two guards
come with its import: this suite starts nothing but the stand-in, and kills
nothing it did not start.

Two kinds of rows: CARRIED OVER BY MEANING (the two rows the runner's core
left for the slice that puts it behind the server) and NATIVE ones, marked
`Native`.
"""
from __future__ import annotations

import ast
import json
import re
import shutil
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

import test_llamacpp as TL                                      # noqa: E402  (installs the guards)
import runner.manager as M                                      # noqa: E402
import _suites                                                  # noqa: E402
_suites.forbid_the_card()        # a suite never asks the real card (nvidia-smi)
import ids                                                      # noqa: E402
import memory as MEM                                            # noqa: E402
import jobqueue as Q                                            # noqa: E402
import server as S                                              # noqa: E402
import validate as V                                            # noqa: E402
from runner import Backend, BackendError, Config, ModelManager, read_events  # noqa: E402
from runner.adapter import RunnerBackend                        # noqa: E402
from runner.base import Cancelled, PromptTooLong, as_messages   # noqa: E402
from runner.mock import TOKEN_RE, MockBackend, said             # noqa: E402

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

#: EVERY PORT THIS SUITE HAD A SERVER LISTEN ON - (the port asked for, the
#: port had) - read by its last row: a suite takes its ports from a range of
#: its own, never one the operating system picks, which may be anybody's the
#: moment after (H6's register 3). The server's own `listen`, recorded where
#: it is had
LISTENED: list = []
_listen = S.listen


def _listening(port, host="127.0.0.1"):
    got = _listen(port, host)
    LISTENED.append((port, got.server_address[1]))
    return got


S.listen = _listening

SHAPES, ROUTES = V.load(HERE.parent / "llm-bootstrap-contract")
FAST = dict(tick_s=0.02, supervise_s=0.02, cancel_wait_s=1.0, give_up_grace_s=0.3,
            stall_s=60.0, load_s=120.0, stale_head_s=600.0, recover_timeout_s=0.3)


def _timed(fn, *a):
    t = time.monotonic()
    fn(*a)
    return time.monotonic() - t


def health_shape():
    (route,) = [r for r in ROUTES.values() if r.get("id") == "server.health"]
    return route["responses"]["200"]


class FakeRun:
    """What the queue hands a backend for one generation."""

    def __init__(self):
        self.cancelled = threading.Event()
        self.handle = "srv-w5r8t2hh:j-0001#01"
        self.job_id = "j-0001"
        self.reports = []

    def stopped(self):
        return self.cancelled.is_set()

    def report(self, **fields):
        self.reports.append((time.monotonic(), dict(fields)))


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="llmb-adapter-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def config(self, text="") -> Config:
        base = ('[runner]\nbackend = "mock"\nidle_unload_s = 600.0\n\n'
                '[mock]\ntokens_per_second = 0\nload_seconds = 0\nunload_seconds = 0\n\n'
                '[models.demo]\nlabel = "Demo"\ntop_p = 0.9\nmin_p = 0.07\n'
                'repeat_penalty = 1.1\n')
        path = self.tmp / "runner.toml"
        path.write_text(base + text, encoding="utf-8")
        return Config.load(path)

    def adapter(self, cfg=None, folder="bound", holder="srv-w5r8t2hh", **kw) -> RunnerBackend:
        m = ModelManager(cfg or self.config(), (self.tmp / folder) if folder else None,
                         vram=lambda: None, **kw.pop("manager_kw", {}))
        a = RunnerBackend(m, log=kw.pop("log", lambda s: None), **kw)
        if holder:
            a.attach(holder)
        self.addCleanup(a.close)
        return a


# ============================================================ carried rows ===

#: every file the server runs - never a suite, the stand-in or a test parent
APP = [HERE / "server.py", HERE / "jobqueue.py", *sorted((HERE / "runner").glob("*.py"))]


class TheOneHolderIsTheServer(unittest.TestCase):
    """Carried over: the application has exactly ONE `acquire` caller, and it
    names the holder - the day there are two, the second must name itself
    too. Here the holder is the server's actor, handed over by `attach`."""

    def test_there_is_exactly_one_acquire_and_it_names_the_holder(self):
        calls = []
        for path in APP:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "acquire"
                        and ast.unparse(node.func.value).endswith("manager")):
                    calls.append((path.relative_to(HERE).as_posix(), node))
        self.assertEqual([c[0] for c in calls], ["runner/adapter.py"])
        (_, call), = calls
        self.assertEqual(ast.unparse(call.args[1]), "self.holder")


class TheServersHealthCarriesTheRunner(Case):
    """Carried over: the runner's status reaches the server's health
    verbatim - one door onto it, never a second answer built beside it."""

    def test_health_carries_the_runners_own_status(self):
        a = self.adapter(folder=None, holder=None)
        app = S.Server(backend=a, log=lambda s: None, **FAST)
        self.addCleanup(app.close)
        app.bind({"path": str(self.tmp)})
        a({"prompt": "hello"}, {"model": "demo", "max_tokens": 20}, FakeRun())
        _, h = app.health()
        mine = a.manager.status()
        self.assertEqual({k: v for k, v in h["runner"].items() if k != "lease"},
                         {k: v for k, v in mine.items() if k != "lease"})
        self.assertEqual(h["runner"]["lease"]["holder"], app.actor)
        self.assertEqual(h["runner"]["load"], mine["load"])


# ============================================================ the adapter ===

class NativeJobShareRows(Case):
    """PLAN-4 D12 (S11b): what the queue says was its own share of a job
    (`job_share`) is kept in the statistics of the model the job named - and
    one naming no model the runner has keeps nothing, and fails nothing."""

    def test_a_jobs_share_is_kept_under_the_model_it_named(self):
        a = self.adapter()
        a.job_share("demo", 0.031)
        a.job_share("nobody", 0.5)
        a.job_share(None, 0.5)
        lines = a.manager.stat_lines()
        self.assertEqual([(e["model_key"], e["what"], e["measures"]) for e in lines],
                         [("demo", "job", {"queue_s": 0.031})])


class NativeAdapterRows(Case):
    def test_a_generation_takes_the_model_the_job_names_as_the_servers_actor(self):
        a = self.adapter()
        out = a({"prompt": "Tell me about rain."}, {"model": "demo", "max_tokens": 200}, FakeRun())
        self.assertIn("(stand-in demo", out["text"])
        self.assertEqual((out["model"], out["finish_reason"]), ("demo", "stop"))
        self.assertTrue(ids.matches("actor", out["load"]) and out["load"].startswith("load-"))
        self.assertEqual(a.manager.status()["lease"]["holder"], "srv-w5r8t2hh")

    def test_a_string_request_is_the_prompt(self):
        out = self.adapter()("Tell me about rain.", {"model": "demo"}, FakeRun())
        self.assertIn("(stand-in demo", out["text"])

    def test_a_request_with_no_prompt_is_refused_never_guessed_at(self):
        with self.assertRaises(Q.GenerationFailed) as caught:
            self.adapter()({"text": "hello"}, {"model": "demo"}, FakeRun())
        self.assertIn("needs a prompt", str(caught.exception))

    def test_a_conversation_is_sent_as_it_was_built(self):
        """Messages with roles: a system message and the turns before
        this one, in their order - the request the application built."""
        a = self.adapter()
        out = a({"messages": TL.TALK}, {"model": "demo"}, FakeRun())
        self.assertEqual(a.manager._backend.last_call["messages"], TL.TALK)
        self.assertIn("(stand-in demo", out["text"])

    def test_a_conversation_with_a_role_no_template_takes_is_refused_before_the_model_is_asked(
            self):
        a = self.adapter()
        talk = [{"role": "sytem", "content": "Answer in one sentence."},
                {"role": "user", "content": "What is rain?"}]
        with self.assertRaises(Q.GenerationFailed) as caught:
            a({"messages": talk}, {"model": "demo"}, FakeRun())
        self.assertIn('$.messages[0].role: expected one of ["system", "user", "assistant"], '
                      'got "sytem"', str(caught.exception))
        self.assertEqual(a.manager.status()["loads_this_session"], 0, "the model was loaded")

    def test_a_request_that_is_a_prompt_and_a_conversation_at_once_is_refused(self):
        with self.assertRaises(Q.GenerationFailed) as caught:
            self.adapter()({"prompt": "hi", "messages": [{"role": "user", "content": "hi"}]},
                           {"model": "demo"}, FakeRun())
        self.assertIn('unexpected field "prompt"', str(caught.exception))

    def test_every_parameter_the_job_names_reaches_the_model_as_it_was_named(self):
        a = self.adapter()
        named = TL.every_parameter()
        a({"prompt": "hi"}, {"model": "demo", **named}, FakeRun())
        call = a.manager._backend.last_call
        for name, value in named.items():
            with self.subTest(parameter=name):
                self.assertTrue(TL.same(call.get(name), value), f"sent {call.get(name)!r}")

    def test_max_tokens_0_is_sent_as_0_never_replaced_by_the_default(self):
        """llama-server reads 0 as: read the prompt into the cache, write
        nothing. It is the job's to name, and it is sent as named."""
        a = self.adapter()
        a({"prompt": "hi"}, {"model": "demo", "max_tokens": 0}, FakeRun())
        self.assertEqual(a.manager._backend.last_call["max_tokens"], 0)

    def test_the_queues_own_parameters_never_reach_the_model(self):
        a = self.adapter()
        a({"prompt": "hi"}, {"model": "demo", "n": 3, "retries": 1, "top_k": 5}, FakeRun())
        call = a.manager._backend.last_call
        self.assertFalse({"n", "retries"} & set(call), call)
        self.assertEqual(call["top_k"], 5)

    def test_the_jobs_samplers_reach_the_model_and_the_models_own_fill_the_rest(self):
        a = self.adapter()
        a({"prompt": "hi"}, {"model": "demo", "temperature": 0.3, "seed": 9, "max_tokens": 50,
                             "min_p": 0.02}, FakeRun())
        call = a.manager._backend.last_call
        self.assertEqual((call["temperature"], call["seed"], call["max_tokens"]), (0.3, 9, 50))
        self.assertEqual(call["min_p"], 0.02, "the job's own sampler")
        self.assertEqual((call["top_p"], call["repeat_penalty"]), (0.9, 1.1),
                         "the model's settings, where the job said nothing")

    def test_a_job_that_names_no_temperature_runs_at_its_models_own(self):
        a = self.adapter(self.config("temperature = 0.4\n"))
        a({"prompt": "hi"}, {"model": "demo", "max_tokens": 20}, FakeRun())
        self.assertEqual(a.manager._backend.last_call["temperature"], 0.4)

    def test_a_temperature_given_as_none_is_unset_too(self):
        a = self.adapter(self.config("temperature = 0.4\n"))
        a({"prompt": "hi"}, {"model": "demo", "temperature": None}, FakeRun())
        self.assertEqual(a.manager._backend.last_call["temperature"], 0.4)

    def test_a_model_that_names_none_takes_the_settings_default(self):
        a = self.adapter(self.config("\n[defaults]\ntemperature = 0.6\n"))
        a({"prompt": "hi"}, {"model": "demo"}, FakeRun())
        self.assertEqual(a.manager._backend.last_call["temperature"], 0.6)

    def test_progress_is_reported_at_once_and_then_no_more_often_than_asked(self):
        cfg = self.config()
        cfg.mock["tokens_per_second"] = 200
        a = self.adapter(cfg, report_every_s=0.2)
        run = FakeRun()
        out = a({"prompt": "Tell me a long story."}, {"model": "demo", "max_tokens": 400}, run)
        # CHANGED ON PURPOSE (S16a R1): the stand-in's count is said first,
        # before its first word - the words are the progress this row reads
        moving = [(t, f) for t, f in run.reports if f.get("stage") != "done" and "count" not in f]
        self.assertGreaterEqual(len(moving), 1)
        self.assertEqual(moving[0][1]["tokens"], 1, "the first word is reported at once")
        gaps = [b[0] - a_[0] for a_, b in zip(moving, moving[1:])]
        self.assertTrue(all(g >= 0.19 for g in gaps), gaps)
        self.assertLess(len(moving), out["tokens"], "every token was a write of the store")
        self.assertEqual(run.reports[-1][1], {"tokens": out["tokens"], "stage": "done"})

    def test_thinking_is_progress_and_goes_beside_the_answer_not_into_it(self):
        a = self.adapter()
        run = FakeRun()
        out = a({"prompt": "[[MOCK_THINK]] think first"}, {"model": "demo", "max_tokens": 400}, run)
        # CHANGED ON PURPOSE (S16a R1): the stand-in's count comes first
        self.assertIn("count", run.reports[0][1])
        self.assertEqual(run.reports[1][1]["stage"], "thinking")
        self.assertIn("think", out["reasoning"])
        self.assertNotIn("<think>", out["text"])
        self.assertTrue(out["text"].startswith("(stand-in demo"), out["text"][:40])

    def test_a_cancel_mid_generation_is_the_queues_cancel(self):
        cfg = self.config()
        cfg.mock["tokens_per_second"] = 100
        a = self.adapter(cfg)
        run = FakeRun()
        threading.Timer(0.1, run.cancelled.set).start()
        with self.assertRaises(Q.Cancelled):
            a({"prompt": "Tell me a long story."}, {"model": "demo", "max_tokens": 1000}, run)

    def test_a_model_the_runner_does_not_know_fails_naming_the_ones_it_does(self):
        with self.assertRaises(Q.GenerationFailed) as caught:
            self.adapter()({"prompt": "hi"}, {"model": "dmeo"}, FakeRun())
        self.assertIn("demo", str(caught.exception))

    def test_a_load_that_fails_fails_the_generation_in_its_own_words(self):
        class Broken(Backend):
            def start(self):
                raise BackendError("llama-server did not come up")
        a = self.adapter(manager_kw={"backend_factory": lambda key, port: Broken(key)})
        with self.assertRaises(Q.GenerationFailed) as caught:
            a({"prompt": "hi"}, {"model": "demo"}, FakeRun())
        self.assertIn("did not come up", str(caught.exception))
        self.assertIsNone(a.manager.status()["lease"]["holder"])

    def test_a_generation_that_fails_fails_in_the_models_own_words(self):
        cfg = self.config()
        cfg.mock["fail_models"] = ["demo"]
        with self.assertRaises(Q.GenerationFailed) as caught:
            self.adapter(cfg)({"prompt": "hi"}, {"model": "demo"}, FakeRun())
        self.assertIn("out of memory", str(caught.exception))

    def test_loading_says_so_while_the_model_loads(self):
        cfg = self.config()
        cfg.mock["load_seconds"] = 0.6
        a = self.adapter(cfg)
        seen = []
        t = threading.Thread(target=lambda: a({"prompt": "hi"}, {"model": "demo"}, FakeRun()))
        t.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not seen:
            if a.loading():
                seen.append(True)
            time.sleep(0.02)
        t.join(10)
        self.assertEqual(seen, [True], "a load under way never read as loading")
        self.assertFalse(a.loading())

    def test_one_server_one_holder(self):
        a = self.adapter()
        with self.assertRaises(RuntimeError):
            a.attach("srv-k3x9m2pq")
        a.attach("srv-w5r8t2hh")                            # the same server again is nothing

    def test_a_runner_no_server_holds_cannot_take_the_card(self):
        with self.assertRaises(Q.GenerationFailed) as caught:
            self.adapter(holder=None)({"prompt": "hi"}, {"model": "demo"}, FakeRun())
        self.assertIn("holder", str(caught.exception))


class KeepingRun(FakeRun):
    """A run with somewhere to keep things: what the queue's `record` does,
    minus the folder."""

    def __init__(self):
        super().__init__()
        self.kept = {}

    def record(self, name, data):
        self.kept[name] = json.loads(json.dumps(data))
        return True


class NativeRecordRows(Case):
    """The runner keeps its exact call in the request's record."""

    def test_the_exact_call_is_kept_for_its_attempt(self):
        a = self.adapter()
        run = KeepingRun()
        a({"prompt": "Tell me about rain."}, {"model": "demo", "temperature": 0.3, "seed": 9},
          run)
        self.assertEqual(run.kept["call.json"], a.manager._backend.last_call)
        self.assertEqual((run.kept["call.json"]["temperature"], run.kept["call.json"]["seed"]),
                         (0.3, 9))
        self.assertEqual(run.kept["call.json"]["messages"],
                         [{"role": "user", "content": "Tell me about rain."}])

    def test_two_generations_on_one_model_each_keep_the_call_they_made(self):
        """S7: a call is its generation's own. Two generations on one model at
        once - each on its own thread - each keep the call they made: the
        backend's one `last_call` was the other's by the time the first
        ended, and its record kept the other's call as its own."""
        cfg = self.config()
        cfg.mock["tokens_per_second"] = 50.0                # slow enough to overlap
        a = self.adapter(cfg)
        a({"prompt": "warm it up"}, {"model": "demo"}, KeepingRun())
        long_run, short_run = KeepingRun(), KeepingRun()
        longer = threading.Thread(target=a, args=({"prompt": "the long one"},
                                                  {"model": "demo", "max_tokens": 40}, long_run),
                                  daemon=True)
        longer.start()
        time.sleep(0.2)                                     # the long one is streaming
        a({"prompt": "the short one"}, {"model": "demo", "max_tokens": 2}, short_run)
        longer.join(10)
        self.assertFalse(longer.is_alive())
        self.assertEqual([r.kept["call.json"]["messages"][0]["content"]
                          for r in (long_run, short_run)], ["the long one", "the short one"])
        self.assertEqual(len(a.manager._loads), 1, "one model, one load")

    def test_a_generation_that_fails_still_keeps_the_call_it_made(self):
        cfg = self.config()
        cfg.mock["fail_models"] = ["demo"]
        run = KeepingRun()
        with self.assertRaises(Q.GenerationFailed):
            self.adapter(cfg)({"prompt": "hi"}, {"model": "demo"}, run)
        self.assertEqual(run.kept["call.json"]["messages"], [{"role": "user", "content": "hi"}])

    def test_a_call_this_generation_never_made_is_never_kept_as_its_own(self):
        class Once(Backend):
            """Answers once; the second time it fails before it makes a call."""
            calls = 0

            def start(self):
                pass

            def stop(self):
                pass

            def stream(self, user_message, params, cancel=None):
                Once.calls += 1
                if Once.calls > 1:
                    raise BackendError("the server went away before the request")
                self.last_call = {"messages": [{"role": "user", "content": user_message}]}
                return iter([{"type": "token", "text": "Fine."},
                             {"type": "done", "finish_reason": "stop"}])
        a = self.adapter(manager_kw={"backend_factory": lambda key, port: Once(key)})
        first, second = KeepingRun(), KeepingRun()
        a({"prompt": "the first"}, {"model": "demo"}, first)
        with self.assertRaises(Q.GenerationFailed):
            a({"prompt": "the second"}, {"model": "demo"}, second)
        self.assertEqual(first.kept["call.json"]["messages"][0]["content"], "the first")
        self.assertEqual(second.kept, {}, "the first generation's call was kept as the second's")


class Counting(MockBackend):
    """The stand-in model, counting as a model does: its own tokens - a word
    each, as it reports its usage - and `template` more for its chat
    template, against a context of `context`. It counts how often it was
    asked to answer; a count it is told to refuse raises in these words."""

    def __init__(self, key, context, template=4, refuse=None, stream_refuses=None,
                 on_count=None):
        super().__init__(key, key, {})
        self.context, self.template = context, template
        self.refuse, self.stream_refuses, self.on_count = refuse, stream_refuses, on_count
        self.answered = 0

    def count(self, messages, params, cancel=None):
        if self.on_count:
            self.on_count()
        if self.refuse:
            raise BackendError(self.refuse)
        words = len(TOKEN_RE.findall(said(as_messages(messages))))
        return {"prompt_tokens": words + self.template, "context": self.context}

    def stream(self, messages, params, cancel=None):
        self.answered += 1
        if self.stream_refuses:
            raise PromptTooLong(self.stream_refuses)
        yield from super().stream(messages, params, cancel)


def words(n: int) -> str:
    """A prompt of n words: n + 4 tokens as `Counting` reads it."""
    return " ".join(["word"] * n)


class NativeCountRows(Case):
    """THE COUNT: at the job's turn, with its model up and before it is
    asked, the request is counted by the model against the context it says
    it has. The prompt and the room its answer needs - its `max_tokens`, and
    never less than one token - must fit, or the job fails `prompt_too_long`,
    saying the three numbers: never cut to fit. The count is on the job's
    progress, and in its record, whether it fits or not."""

    def counted(self, backend, prompt, max_tokens, run=None):
        """-> (the answer, or the failure) for a prompt with this length."""
        a = self.adapter(manager_kw={"backend_factory": lambda key, port: backend})
        try:
            return a({"prompt": prompt}, {"model": "demo", "max_tokens": max_tokens},
                     run or FakeRun())
        except Q.GenerationFailed as exc:
            return exc

    def test_a_request_that_fits_its_models_context_to_the_token_is_asked(self):
        b = Counting("demo", context=64)
        out = self.counted(b, words(10), 50)                    # 14 + 50 = 64
        self.assertIsInstance(out, dict, out)
        self.assertEqual((b.answered, out["model"]), (1, "demo"))
        self.assertIn("(stand-in demo", out["text"])

    def test_one_token_more_than_its_context_is_refused_before_the_model_is_asked(self):
        b = Counting("demo", context=64)
        out = self.counted(b, words(10), 51)                    # 14 + 51 = 65
        self.assertIsInstance(out, Q.GenerationFailed)
        self.assertEqual((str(out), out.reason), (
            "the request does not fit demo's context of 64 tokens: the prompt is 14 tokens as "
            "the model reads it, and its answer needs room for 51 more (max_tokens 51) - 65 in "
            "all. Shorten the prompt, ask for a shorter answer, or give the model a larger "
            "context ([models.demo] ctx)", "prompt_too_long"))
        self.assertEqual(b.answered, 0, "the model was asked")

    def test_an_answer_that_names_no_limit_needs_room_to_begin(self):
        """-1: the answer may run to the end of the context - where
        llama-server stops it - and needs one token to begin."""
        b = Counting("demo", context=64)
        self.assertIsInstance(self.counted(b, words(59), -1), dict)    # 63 + 1 = 64
        out = self.counted(Counting("demo", context=64), words(60), -1)
        self.assertEqual(out.reason, "prompt_too_long")
        self.assertIn("the prompt is 64 tokens as the model reads it, and its answer needs room "
                      "for 1 more (max_tokens -1 names no limit: room to begin) - 65 in all. "
                      "Shorten the prompt, or give the model a larger context", str(out))

    def test_an_answer_of_nothing_still_needs_the_position_after_its_prompt(self):
        """0 writes nothing, but llama-server refuses a prompt of the whole
        context (b10724: n_prompt >= n_ctx)."""
        self.assertIsInstance(self.counted(Counting("demo", context=64), words(59), 0), dict)
        out = self.counted(Counting("demo", context=64), words(60), 0)
        self.assertEqual(out.reason, "prompt_too_long")
        self.assertIn("room for 1 more (max_tokens 0 writes nothing, and the prompt must still "
                      "leave one) - 65 in all", str(out))

    def test_the_count_is_on_the_jobs_progress_and_in_its_record_whether_it_fits_or_not(self):
        for max_tokens, fits in ((50, True), (51, False)):
            with self.subTest(max_tokens=max_tokens):
                run = KeepingRun()
                out = self.counted(Counting("demo", context=64), words(10), max_tokens, run)
                self.assertEqual(isinstance(out, dict), fits)
                want = {"prompt_tokens": 14, "room": max_tokens, "context": 64}
                self.assertEqual([f["count"] for _, f in run.reports if "count" in f], [want])
                self.assertEqual(V.check(run.reports[0][1], SHAPES["progress"], SHAPES), [])
                self.assertEqual(run.kept["count.json"], want)

    def test_a_model_that_cannot_count_is_asked_without_a_count(self):
        """A model that counts nothing: nothing is checked before it is
        asked, and no count is said. CHANGED ON PURPOSE (S16a R1): the
        stand-in counts now - a model that cannot is one whose backend says
        so (None)."""
        class Mute(MockBackend):
            def count(self, messages, params, cancel=None):
                return None
        run = KeepingRun()
        out = self.counted(Mute("demo", "demo", self.config().mock), words(10), 51, run)
        self.assertIn("(stand-in demo", out["text"])
        self.assertEqual([f for _, f in run.reports if "count" in f], [])
        self.assertNotIn("count.json", run.kept)

    def test_a_model_that_refuses_a_request_longer_than_its_context_ends_it_the_same_way(self):
        """llama-server's own refusal is the last line of defence, behind the
        count: its job ends `prompt_too_long` too, in the model's words."""
        b = Counting("demo", context=64, stream_refuses="the request is 70 tokens and demo's "
                                                         "context is 64")
        out = self.counted(b, words(10), 50)
        self.assertEqual((str(out), out.reason), ("the request is 70 tokens and demo's context "
                                                  "is 64", "prompt_too_long"))

    def test_a_count_the_model_refuses_fails_the_job_in_its_words_and_nothing_is_asked(self):
        b = Counting("demo", context=64, refuse="llama-server refused the request (HTTP 400): "
                                                "Cannot use both json_schema and grammar")
        out = self.counted(b, words(10), 50)
        self.assertEqual((str(out), out.reason), (
            "the model could not count the request: llama-server refused the request (HTTP 400): "
            "Cannot use both json_schema and grammar", None))
        self.assertEqual(b.answered, 0)

    def test_a_cancel_while_the_model_counts_is_the_queues_cancel(self):
        """A job cancelled while its model counts it is cancelled - never
        failed - and its model is never asked."""
        def cancelled():
            raise Cancelled("cancelled before the model counted the request")
        b = Counting("demo", context=64, on_count=cancelled)
        a = self.adapter(manager_kw={"backend_factory": lambda key, port: b})
        with self.assertRaises(Q.Cancelled):
            a({"prompt": words(10)}, {"model": "demo", "max_tokens": 50}, FakeRun())
        self.assertEqual(b.answered, 0)

    def test_a_count_cut_short_by_the_card_taken_back_says_so(self):
        """The card taken back while the model counts - a coordinator
        reclaiming it - fails the job saying so, as a generation it cuts
        short does: not merely that the model did not answer the count."""
        class Card:
            lost = None

            def take(self, holder, model_key, for_=None, cancel=None, on_wait=None,
                     terms=None):
                return None

            def give_back(self):
                pass

            def status(self):
                return None
        card = Card()
        b = Counting("demo", context=64, refuse="could not ask llama-server to count the "
                                                "request: connection refused",
                     on_count=lambda: setattr(card, "lost", "reclaimed"))
        a = self.adapter(manager_kw={"backend_factory": lambda key, port: b, "card": card})
        with self.assertRaises(Q.GenerationFailed) as caught:
            a({"prompt": words(10)}, {"model": "demo", "max_tokens": 50}, FakeRun())
        self.assertEqual(str(caught.exception), "the card was taken back (reclaimed): the model "
                                                "could not count the request: could not ask "
                                                "llama-server to count the request: connection "
                                                "refused")


class NativeHoldRows(Case):
    """THE REQUEST CHECK'S VIEW OF A REQUEST (PLAN-4 D16), as the runner
    says it (`hold`): the model and its engine; the messages by role as the
    model will be asked them, or a whisper model's audio and fields; every
    parameter it will be sent and where it came from - the application,
    the model's settings or the bootstrap's default; the count, made by the
    model as a turn makes it, and whether it fits - held all the same when
    it does not; and what the model's statistics expect. The model is never
    asked."""

    def held(self, backend=None, cfg=None, request=None, params=None, named=("model",),
             run=None):
        kw = {"manager_kw": {"backend_factory": lambda key, port: backend}} if backend else {}
        a = self.adapter(cfg, **kw)
        return a.hold(request if request is not None else {"prompt": words(10)},
                      params if params is not None else {"model": "demo", "max_tokens": 600,
                                                         "seed": None},
                      run or FakeRun(), list(named)), a

    def test_a_prompt_is_one_user_message_and_every_parameter_says_where_it_came_from(self):
        cfg = self.config('disable_thinking = true\n')
        h, _ = self.held(cfg=cfg, params={"model": "demo", "max_tokens": 600, "seed": None,
                                          "temperature": 0.3, "stop": ["##"], "retries": 1},
                         named=("model", "temperature", "stop", "retries"))
        self.assertEqual((h["engine"], h["messages"], h["audio"], h["fields"]),
                         ("llamacpp", [{"role": "user", "content": words(10)}], None, None))
        self.assertEqual(h["params"], [
            {"name": "max_tokens", "value": 600, "from": "bootstrap"},
            {"name": "stop", "value": ["##"], "from": "application"},
            {"name": "temperature", "value": 0.3, "from": "application"},
            {"name": "top_p", "value": 0.9, "from": "model"},
            {"name": "min_p", "value": 0.07, "from": "model"},
            {"name": "repeat_penalty", "value": 1.1, "from": "model"},
            {"name": "chat_template_kwargs.enable_thinking", "value": False, "from": "model"}])

    def test_the_applications_own_thinking_switch_is_the_applications(self):
        cfg = self.config('disable_thinking = true\n')
        h, _ = self.held(cfg=cfg, params={"model": "demo", "max_tokens": 5,
                                          "chat_template_kwargs": {"enable_thinking": True}},
                         named=("model", "max_tokens", "chat_template_kwargs"))
        self.assertEqual([(x["name"], x["from"]) for x in h["params"]
                          if "thinking" in x["name"] or x["name"] == "chat_template_kwargs"],
                         [("chat_template_kwargs", "application")])

    def test_a_conversation_is_shown_as_it_was_built(self):
        talk = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Rain?"}]
        h, _ = self.held(request={"messages": talk})
        self.assertEqual(h["messages"], talk)

    def test_it_is_counted_by_its_model_as_a_turn_counts_it_and_the_model_is_never_asked(self):
        for max_tokens, fits in ((50, True), (51, False)):
            with self.subTest(max_tokens=max_tokens):
                b = Counting("demo", context=64)
                run = FakeRun()
                h, _ = self.held(b, params={"model": "demo", "max_tokens": max_tokens},
                                 run=run)
                want = {"prompt_tokens": 14, "room": max_tokens, "context": 64}
                self.assertEqual((h["count"], h["fits"]), (want, fits))
                self.assertEqual([f["count"] for _, f in run.reports if "count" in f], [want])
                self.assertEqual(b.answered, 0, "the model was asked")

    def test_the_stand_in_counts_as_a_turn_counts_it(self):
        """S16a R1: the stand-in counts - each word it is asked a token -
        against its model's context, so its held request says a count."""
        h, _ = self.held(cfg=self.config('ctx = 64\n'), request={"prompt": words(10)},
                         params={"model": "demo", "max_tokens": 20, "seed": None})
        self.assertEqual((h["count"], h["fits"]),
                         ({"prompt_tokens": 10, "room": 20, "context": 64}, True))

    def test_on_the_stand_in_a_request_past_its_context_fails_prompt_too_long(self):
        """S16a R1: before, no application on the stand-ins ever saw
        `prompt_too_long`. A request that does not fit its model's context
        fails it now, before the stand-in is asked; one that fits runs."""
        a = self.adapter(self.config('ctx = 16\n'))
        with self.assertRaises(Q.GenerationFailed) as caught:
            a({"prompt": words(12)}, {"model": "demo", "max_tokens": 5}, FakeRun())
        self.assertEqual(caught.exception.reason, "prompt_too_long", str(caught.exception))
        out = a({"prompt": words(10)}, {"model": "demo", "max_tokens": 5}, FakeRun())
        self.assertEqual(out["model"], "demo")

    def test_a_model_that_cannot_count_says_so(self):
        # CHANGED ON PURPOSE (S16a R1): the stand-in counts now - a model that
        # cannot is one whose backend says so (None)
        class Mute(MockBackend):
            def count(self, messages, params, cancel=None):
                return None
        h, _ = self.held(Mute("demo", "demo", self.config().mock))
        self.assertEqual((h["count"], h["fits"]), (None, None))
        self.assertIn("demo cannot count a request: it is asked without a count, as at its "
                      "turn", h["said"])

    def test_a_count_the_model_refuses_fails_the_check_in_its_words(self):
        b = Counting("demo", context=64, refuse="Cannot use both json_schema and grammar")
        with self.assertRaises(Q.GenerationFailed) as caught:
            self.held(b)
        self.assertIn("the model could not count the request: Cannot use both json_schema and "
                      "grammar", str(caught.exception))

    def test_what_the_statistics_expect_once_the_model_has_three_answers(self):
        a = self.adapter()
        h = a.hold({"prompt": "rain"}, {"model": "demo", "max_tokens": 5}, FakeRun(), ["model"])
        self.assertIsNone(h["expect"])
        self.assertIn("no expected time yet: demo has 0 answers measured, and its statistics "
                      "need 3", h["said"])
        # its answers as the statistics keep them: a median (2) that is not
        # their mean (3), and a 90th percentile that is the slowest
        kept = M._stats.ModelStats(a.manager.data_dir / M.STATS_NAME)
        for seconds in (1.0, 6.0):
            kept.note("demo", "answer_s", seconds)
        h = a.hold({"prompt": "rain"}, {"model": "demo", "max_tokens": 5}, FakeRun(), ["model"])
        self.assertIsNone(h["expect"], "an expectation from two answers")
        self.assertIn("no expected time yet: demo has 2 answers measured, and its statistics "
                      "need 3", h["said"])
        kept.note("demo", "answer_s", 2.0)
        h = a.hold({"prompt": "rain"}, {"model": "demo", "max_tokens": 5}, FakeRun(), ["model"])
        self.assertEqual(h["expect"], {"measure": "answer_s", "median_s": 2.0, "p90_s": 6.0,
                                       "samples": 3})
        a({"prompt": "rain"}, {"model": "demo", "max_tokens": 5}, FakeRun())
        h = a.hold({"prompt": "rain"}, {"model": "demo", "max_tokens": 5}, FakeRun(), ["model"])
        self.assertEqual(h["expect"]["samples"], 4, "an answer the runner timed was not counted")
        self.assertEqual(V.check(h["expect"], SHAPES["held_request"]["object"]["expect"],
                                 SHAPES), [])

    def test_statistics_that_cannot_be_read_are_said_so_never_as_none_measured(self):
        """H8 (S15 RS3): the expected time is read through the manager's
        public `kept_stats` - None when the summary cannot be read, which is
        said as that, never as a model with no answers measured."""
        a = self.adapter()
        (a.manager.data_dir / M.STATS_NAME).parent.mkdir(parents=True, exist_ok=True)
        (a.manager.data_dir / M.STATS_NAME).write_bytes(b"{ not json")
        self.assertIsNone(a.manager.kept_stats())
        h = a.hold({"prompt": "rain"}, {"model": "demo", "max_tokens": 5}, FakeRun(), ["model"])
        self.assertIsNone(h["expect"])
        self.assertIn("no expected time: the model's statistics cannot be read", h["said"])
        self.assertFalse([s for s in h["said"] if "answers measured" in s], h["said"])

    def test_the_contracts_held_request_names_the_parameters_in_the_order_they_are_given(self):
        """H8 (S15 RS1): the contract's example is a real answer - the
        application's parameters in the order the runner gives them."""
        ex = next(e for e in ROUTES["server/job-held"]["examples"] if e["status"] == 200
                  and e["response"]["held"]["params"])["response"]["held"]["params"]
        own = [p for p in ex if p["from"] == "application"]
        h = self.adapter().hold({"prompt": "rain"},
                                {"model": "demo", **{p["name"]: p["value"] for p in own}},
                                FakeRun(), ["model", *(p["name"] for p in own)])
        self.assertEqual([p["name"] for p in h["params"] if p["from"] == "application"],
                         [p["name"] for p in own])

    def test_the_adapter_asks_the_manager_only_through_its_public_names(self):
        """H8 (S15 RS3): a private method of the manager is the manager's to
        rework; the adapter reads it through what the manager offers."""
        src = (HERE / "runner" / "adapter.py").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"\bmanager\._\w+", src), [])

    def test_a_transcription_shows_its_audio_its_fields_and_no_count(self):
        path = self.tmp / "ears.toml"
        path.write_text(EARS.format(answer=0.0, due=60.0), encoding="utf-8")
        a = self.adapter(Config.load(path))
        wav = _wav(self.tmp / "take.wav", seconds=1.5)
        req = {"audio": str(wav), "fields": {"language": "no"}}
        h = a.hold(req, {"model": "ears", "max_tokens": 600}, FakeRun(), ["model"])
        self.assertEqual((h["engine"], h["messages"], h["audio"], h["fields"], h["params"],
                          h["count"], h["fits"]),
                         ("whisper", None, {"path": str(wav), "seconds": 1.5},
                          {"language": "no"}, [], None, None))
        self.assertIn("a transcription is not counted in tokens: its length is its audio",
                      h["said"])
        self.assertEqual(h.get("reads"), [],
                         "R-B1: whisper reads none of a job's gen - the queue shows none")
        kept = M._stats.ModelStats(a.manager.data_dir / M.STATS_NAME)
        for factor in (0.1, 0.9, 0.2):
            kept.note("ears", "real_time_factor", factor)
        kept.note("ears", "answer_s", 99.0)
        h = a.hold(req, {"model": "ears", "max_tokens": 600}, FakeRun(), ["model"])
        self.assertEqual(h["expect"], {"measure": "real_time_factor", "median_s": 0.3,
                                       "p90_s": 1.35, "samples": 3})

    def test_a_request_its_model_could_never_be_asked_fails_before_any_load(self):
        loads = []
        a = self.adapter(manager_kw={"backend_factory": lambda key, port: loads.append(key)})
        for request, words_ in (({"nope": 1}, "a runner generation needs a prompt"),
                                ({"audio": "x.wav"}, "demo answers text")):
            with self.subTest(request=request):
                with self.assertRaises(Q.GenerationFailed) as caught:
                    a.hold(request, {"model": "demo", "max_tokens": 5}, FakeRun(), ["model"])
                self.assertIn(words_, str(caught.exception))
        self.assertEqual(loads, [], "a model was loaded for a request it could never be asked")

    def test_a_send_is_refused_what_its_model_could_never_be_asked(self):
        path = self.tmp / "ears.toml"
        path.write_text(EARS.format(answer=0.0, due=60.0), encoding="utf-8")
        a = self.adapter(Config.load(path))
        wav = str(_wav(self.tmp / "take.wav"))
        for request, model, reason, words_ in (
                ({"nope": 1}, "demo", "bad_request", "a runner generation needs a prompt"),
                ({"audio": wav}, "demo", "bad_request", "demo answers text"),
                ({"audio": wav, "fields": {"langauge": "no"}}, "ears", "bad_audio_request",
                 '"langauge"'),
                ({"prompt": "rain"}, "ears", "bad_audio_request", "ears transcribes audio")):
            with self.subTest(request=request, model=model):
                with self.assertRaises(Q.Refused) as caught:
                    a.check_sent(request, {"model": model})
                self.assertEqual((caught.exception.code, caught.exception.reason), (400, reason))
                self.assertIn(words_, caught.exception.error)
        self.assertIsNone(a.check_sent({"prompt": "rain"}, {"model": "demo"}))
        self.assertIsNone(a.check_sent({"audio": wav}, {"model": "ears"}))
        self.assertIsNone(a.check_sent({"prompt": "rain"}, {"model": "nobody"}),
                          "a model the settings do not name is its turn's to say")


class NativeIdleRows(Case):
    """The model leaves the card by itself - watched, with nothing running but
    the runner's own sweep - and never while it is in use."""

    def idle(self, window, poll=0.05, **mock):
        cfg = self.config()
        cfg.runner["idle_unload_s"] = window
        cfg.mock.update(mock)
        return self.adapter(cfg, idle_poll_s=poll)

    def test_an_idle_model_leaves_the_card_and_takes_the_lease_with_it(self):
        a = self.idle(0.3)
        a({"prompt": "hi"}, {"model": "demo", "max_tokens": 20}, FakeRun())
        self.assertEqual(a.manager.status()["loaded"], "demo")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and a.manager.status()["loaded"]:
            time.sleep(0.02)
        st = a.manager.status()
        self.assertIsNone(st["loaded"], "the model stayed on the card past its window")
        self.assertIsNone(st["lease"]["holder"])
        self.assertIn("idle_unload", [e["event"] for e in a.manager.recent_events()])

    def test_a_model_inside_its_window_is_left_alone(self):
        a = self.idle(3600)
        a({"prompt": "hi"}, {"model": "demo", "max_tokens": 20}, FakeRun())
        time.sleep(0.5)                                     # ten passes of the sweep
        self.assertEqual(a.manager.status()["loaded"], "demo")

    def test_the_window_starts_again_when_a_use_ends(self):
        """A use longer than the window: the model must then get its whole
        window again - not be taken the moment the use ends because the clock
        still counted from the load."""
        a = self.idle(0.4, tokens_per_second=40)          # ~1.5 s of use, a 0.4 s window
        a({"prompt": "Tell me a long story."}, {"model": "demo", "max_tokens": 60}, FakeRun())
        time.sleep(0.2)                                     # half the window, four sweeps
        self.assertEqual(a.manager.status()["loaded"], "demo",
                         "taken before its window: the use did not start it again")

    def test_a_model_in_use_is_never_taken_however_long_it_takes(self):
        """A generation longer than the whole window: the sweep runs through
        it, finds the model in use every time, and leaves it."""
        a = self.idle(0.2, tokens_per_second=40)          # ~1.5 s against a 0.2 s window
        out = a({"prompt": "Tell me a long story."}, {"model": "demo", "max_tokens": 60},
                FakeRun())
        # read at once: the use has just ended, so the window starts again now
        events = [e["event"] for e in a.manager.recent_events()]
        self.assertEqual(out["tokens"], 60)
        self.assertNotIn("idle_unload", events, "the sweep took the model while it was in use")
        self.assertEqual(a.manager.status()["in_use"], 0)
        self.assertEqual(a.manager.status()["loaded"], "demo")


class WatchedCard:
    """A card source a coordinator's watch stands behind (PLAN-4 D3): it holds
    a rental once taken, the runner hands it the work to say (`work`), and
    it tells the runner the card is degraded (`on_degraded`). `verdict`, when
    set, is its memory check's answer before a load."""
    kind = "watched card"

    def __init__(self, verdict=None):
        self.turn = self.lost = None
        self.on_lost = self.on_wanted = self.on_degraded = self.work = None
        self.wanted = False
        self.verdict = verdict

    def take(self, holder, model_key, for_=None, cancel=None, on_wait=None, terms=None):
        self.turn = {"id": "t-0007-wtch"}
        return self.turn

    def give_back(self):
        self.turn = None

    def status(self):
        return None

    def fit(self, model, engine, need_mib, *, read=None, margin_mib=None, how="", hint=""):
        if self.verdict is None:
            return None
        return {"verdict": self.verdict, "need_mib": 9216, "free_mib": 6100, "margin_mib": 256,
                "total_mib": 24576, "words": f"{model}: {self.verdict}, in numbers"}


class RecoveringCard(WatchedCard):
    """A card source that says when the card has room again (S5b)."""
    recovers = True


class LoudCard(WatchedCard):
    """No room now, said as the machine's lock says it (H9): the contract's
    numbers, what programs outside the bootstrap hold, and what to do."""

    VERDICT = {"verdict": "no_room", "need_mib": 9000, "free_mib": 4000, "margin_mib": 256,
               "total_mib": 24576}

    def fit(self, model, engine, need_mib, *, read=None, margin_mib=None, how="", hint=""):
        return {**self.VERDICT, "outside_mib": 20576,
                "words": f"not enough free VRAM for {model}: "
                         f"{MEM.no_room_words(self.VERDICT, 20576)}"}


class TermsRun(FakeRun):
    """A run with the job's terms: its patience, and how long it has been
    held for the card's memory already."""

    def __init__(self, patience=None, waited=None):
        super().__init__()
        self.card_patience_s, self.card_waited_s = patience, waited


FULL = {"reason": "memory_error", "why": "a model's log said the card's memory ran out (CUDA "
        "error: out of memory): nothing new goes on it until it has room again",
        "since": 1790172090.0, "free_mib": 1229, "floor_mib": 256, "total_mib": 24576,
        "error": "CUDA error: out of memory", "need_mib": 6554, "outside_mib": 10035}


class NativeCardFullRows(Case):
    """S5b, through the adapter: an answer the card's memory broke, on a card
    source that says when it has room again, fails `card_degraded` MARKED for
    the queue to run it again (`card_full`, the card's numbers) - never on a
    source that does not say, with no patience left, or once it waited out
    its whole patience; and a job's acquire held for the card's memory says
    so on its progress, in words and numbers, and runs once it has room."""

    def test_an_answer_the_card_broke_is_marked_for_the_queue_to_run_again(self):
        for card, run, marked in ((RecoveringCard(), TermsRun(), True),
                                  (WatchedCard(), TermsRun(), False),
                                  (RecoveringCard(), TermsRun(patience=0), False),
                                  (RecoveringCard(), TermsRun(patience=20, waited=20), False)):
            with self.subTest(card=type(card).__name__, patience=run.card_patience_s,
                              waited=run.card_waited_s):
                cfg = self.config()
                cfg.mock.update(tokens_per_second=5)
                a = self.adapter(cfg, folder=f"f{id(run)}", manager_kw={"card": card})
                box = {}

                def job():
                    try:
                        a({"prompt": "Tell me a long story."},
                          {"model": "demo", "max_tokens": 200}, run)
                    except Exception as exc:                # noqa: BLE001 - read below
                        box["exc"] = exc
                t = threading.Thread(target=job, daemon=True)
                t.start()
                deadline = time.monotonic() + 5
                while not a.manager.work()["working"] and time.monotonic() < deadline:
                    time.sleep(0.02)
                card.on_degraded(dict(FULL))
                t.join(5)
                exc = box.get("exc")
                self.assertIsInstance(exc, Q.GenerationFailed, box)
                self.assertEqual(exc.reason, "card_degraded")
                if marked:
                    self.assertEqual((exc.card_full["free_mib"], exc.card_full["need_mib"],
                                      exc.card_full["outside_mib"]), (1229, 6554, 10035))
                else:
                    self.assertIsNone(exc.card_full)

    def test_a_job_held_for_the_cards_memory_says_so_and_runs_once_it_has_room(self):
        card = RecoveringCard()
        a = self.adapter(manager_kw={"card": card})
        a({"prompt": "hi"}, {"model": "demo", "max_tokens": 5}, FakeRun())
        card.on_degraded(dict(FULL))
        run, box = TermsRun(patience=60), {}

        def job():
            try:
                box["out"] = a({"prompt": "hi"}, {"model": "demo", "max_tokens": 5}, run)
            except Exception as exc:                        # noqa: BLE001 - read below
                box["exc"] = exc
        t = threading.Thread(target=job, daemon=True)
        t.start()
        deadline = time.monotonic() + 5
        while not run.reports and time.monotonic() < deadline:
            time.sleep(0.02)
        said = run.reports[0][1]
        self.assertEqual(said, {"stage": "waiting for the card's memory",
                                "waiting_for": "The card is full: 1.2 GB free, 6.4 GB needed. "
                                               "9.8 GB is used by programs outside the "
                                               "bootstrap. Close something, and this runs "
                                               "again by itself.",
                                "free_mib": 1229, "need_mib": 6554, "outside_mib": 10035,
                                "waited_s": 0, "patience_left_s": 60})
        self.assertTrue(t.is_alive(), box)
        card.on_degraded(None)
        t.join(5)
        self.assertEqual(box.get("out", {}).get("model"), "demo", box)

    def test_a_job_that_waited_out_its_patience_fails_saying_so(self):
        card = RecoveringCard()
        a = self.adapter(manager_kw={"card": card})
        a({"prompt": "hi"}, {"model": "demo", "max_tokens": 5}, FakeRun())
        card.on_degraded(dict(FULL))
        with self.assertRaises(Q.GenerationFailed) as caught:
            a({"prompt": "hi"}, {"model": "demo", "max_tokens": 5},
              TermsRun(patience=0.3, waited=None))
        self.assertEqual(caught.exception.reason, "card_degraded")
        self.assertIsNone(caught.exception.card_full, "held again after its whole patience")
        self.assertIn("the card stayed full for the whole of this request's patience of 0.3s",
                      str(caught.exception))


class NativeWatchRows(Case):
    """PLAN-4 D3, through the adapter: a generation's tokens and its end move
    the runner's progress; a job refused for the card's memory, or on a card
    gone over-full, fails with a reason of its own - `no_room`, `never_fits`,
    `card_degraded` - never a failure a program cannot tell from any other."""

    DEGRADED = {"reason": "under_floor", "why": "the card has 176 MiB free of 24576, under the "
                "floor of 256 MiB a rental keeps free: nothing new goes on it until it has room "
                "again", "since": 1790172090.0, "free_mib": 176, "floor_mib": 256,
                "total_mib": 24576, "error": None}

    def test_a_generation_moves_the_progress_by_each_token_and_its_end(self):
        a = self.adapter()
        out = a({"prompt": "Tell me about rain."}, {"model": "demo", "max_tokens": 200}, FakeRun())
        self.assertEqual(a.manager.work()["progress"], out["tokens"] + 1)
        self.assertFalse(a.manager.work()["working"])

    def test_a_job_refused_for_the_cards_memory_fails_with_a_reason_of_its_own(self):
        for verdict in ("no_room", "never_fits"):
            with self.subTest(verdict=verdict):
                a = self.adapter(folder=verdict,
                                 manager_kw={"card": WatchedCard(verdict)})
                with self.assertRaises(Q.GenerationFailed) as caught:
                    a({"prompt": "hi"}, {"model": "demo"}, FakeRun())
                self.assertEqual(caught.exception.reason, verdict)
                self.assertIn(f"demo: {verdict}, in numbers", str(caught.exception))

    def test_a_job_on_a_degraded_card_fails_card_degraded_saying_why(self):
        card = WatchedCard()
        a = self.adapter(manager_kw={"card": card})
        a({"prompt": "hi"}, {"model": "demo", "max_tokens": 20}, FakeRun())
        card.on_degraded(dict(self.DEGRADED))
        with self.assertRaises(Q.GenerationFailed) as caught:
            a({"prompt": "hi"}, {"model": "demo", "max_tokens": 20}, FakeRun())
        self.assertEqual(caught.exception.reason, "card_degraded")
        self.assertIn(self.DEGRADED["why"], str(caught.exception))
        card.on_degraded(None)
        out = a({"prompt": "hi"}, {"model": "demo", "max_tokens": 20}, FakeRun())
        self.assertEqual(out["model"], "demo", "work goes on once the card has room again")

    def test_a_load_under_way_on_a_card_gone_over_full_fails_its_job_card_degraded(self):
        """A load given up because the card is degraded fails its job with the
        card's reason - never a load that merely failed."""
        card = WatchedCard()
        cfg = self.config()
        cfg.mock.update(load_seconds=30)
        a = self.adapter(cfg, manager_kw={"card": card})
        run, box = FakeRun(), {}

        def job():
            try:
                a({"prompt": "hi"}, {"model": "demo", "max_tokens": 5}, run)
            except Exception as exc:                        # noqa: BLE001 - read below
                box["exc"] = exc
        t = threading.Thread(target=job, daemon=True)
        t.start()
        deadline = time.monotonic() + 5
        while a.manager.status()["loading"] != "demo" and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(a.manager.status()["loading"], "demo", "the load was not under way")
        card.on_degraded(dict(self.DEGRADED))
        t.join(10)
        self.assertIsInstance(box.get("exc"), Q.GenerationFailed, box)
        self.assertEqual(box["exc"].reason, "card_degraded")
        self.assertIn("degraded", str(box["exc"]))

    def test_the_answer_under_way_ends_card_degraded_instead_of_crawling(self):
        card = WatchedCard()
        cfg = self.config()
        cfg.mock.update(tokens_per_second=5)
        a = self.adapter(cfg, manager_kw={"card": card})
        run, box = FakeRun(), {}

        def job():
            try:
                a({"prompt": "Tell me a long story."}, {"model": "demo", "max_tokens": 200}, run)
            except Exception as exc:                        # noqa: BLE001 - read below
                box["exc"] = exc
        t = threading.Thread(target=job, daemon=True)
        t.start()
        deadline = time.monotonic() + 5
        while not any("tokens" in r for _, r in run.reports) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(a.manager.work()["working"], "the answer was not under way")
        ended = time.monotonic()
        card.on_degraded(dict(self.DEGRADED))
        t.join(5)
        self.assertLess(time.monotonic() - ended, 2.0, "the answer crawled on")
        self.assertIsInstance(box.get("exc"), Q.GenerationFailed, box)
        self.assertEqual(box["exc"].reason, "card_degraded")
        self.assertIn(self.DEGRADED["why"], str(box["exc"]))
        self.assertFalse(run.cancelled.is_set(), "the job was cancelled, not failed saying why")


# ============================================================ the server ===

EARS = ('[runner]\nbackend = "mock"\nidle_unload_s = 600.0\n\n'
        '[mock]\ntokens_per_second = 0\nload_seconds = 0\nunload_seconds = 0\n'
        'answer_seconds = {answer}\nanswer_due_s = {due}\n\n'
        '[models.demo]\nlabel = "Demo"\n\n[models.ears]\nlabel = "Ears"\nengine = "whisper"\n')


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


class ServerCase(Case):
    def serve(self, backend, **kw):
        httpd = S.serve(TL.quiet_port(), backend=backend, log=kw.pop("log", lambda s: None),
                        **FAST)
        self.addCleanup(S.stop, httpd)
        return httpd.app

    def http(self, app, method, path, body=None):
        req = urllib.request.Request(app.url + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def run_job(self, app, prompt="Tell me about rain.", model="demo", input=None, job=None,
                **gen):
        _, sub = self.http(app, "POST", "/api/jobs",
                           {"kind": "generate",
                            "input": {"prompt": prompt} if input is None else input,
                            "gen": {"model": model, **gen}, **(job or {})})
        jid = sub["job_id"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            _, res = self.http(app, "GET", f"/api/jobs/{jid}/result")
            if res.get("job", {}).get("status") in ("done", "failed", "cancelled"):
                return res
            time.sleep(0.05)
        self.fail("the job never ended")


class NativeServerRows(ServerCase):
    def test_a_job_runs_end_to_end_on_the_stand_in(self):
        app = self.serve(self.adapter(folder=None, holder=None))
        folder = self.tmp / "book"
        folder.mkdir()
        self.assertEqual(self.http(app, "POST", "/api/workspace", {"path": str(folder)})[0], 200)
        res = self.run_job(app)
        self.assertEqual(res["job"]["status"], "done", res)
        cand = res["candidates"][0]
        self.assertIn("(stand-in demo", cand["text"])
        self.assertTrue(cand["load"].startswith("load-"))
        self.assertTrue((folder / "runner" / M.EVENTS_NAME).exists())

    def test_no_room_now_is_said_whole_in_the_jobs_record_in_health_and_in_the_log(self):
        """H9, the owner's ruling of 2026-09-28 ("Fail loudly"): over HTTP, a
        job whose model does not fit on a card that is not over-full fails
        at once, `no_room`. Its record says the words WHOLE - the numbers,
        what programs outside the bootstrap hold, what to do - and so do
        health's last failure (what the progress widget shows) and the
        server's log."""
        said = []
        app = self.serve(self.adapter(folder=None, holder=None, log=said.append,
                                      manager_kw={"card": LoudCard()}))
        folder = self.tmp / "book"
        folder.mkdir()
        self.http(app, "POST", "/api/workspace", {"path": str(folder)})
        began = time.monotonic()
        # CHANGED ON PURPOSE (S18b): a job that names NO priority is live, and
        # this row asks with none, as an application that does not say its
        # work is batch does. (S18 had it say `interactive`, when a job that
        # named none was batch and waited for room.)
        res = self.run_job(app)
        self.assertLess(time.monotonic() - began, 10.0, "it waited for room")
        job = res["job"]
        self.assertEqual((job["status"], job.get("reason")), ("failed", "no_room"), job)
        for words in ("9000 MiB", "4000 MiB free of 24576",
                      "programs outside the bootstrap hold 20576 MiB",
                      "Close programs that use the card, or reboot, then ask again"):
            self.assertIn(words, job["error"])
        _, h = self.http(app, "GET", "/api/health")
        self.assertEqual(h["queue"]["last"]["error"], job["error"],
                         "health's last failure is not the job's words, whole")
        self.assertGreaterEqual(h["queue"]["consecutive_failures"], 1)
        lines = [line for line in said if "or reboot, then ask again" in line]
        self.assertEqual(len(lines), 1, f"the server's log: {said}")

    def test_a_batch_job_with_no_room_waits_for_it_queued_or_fails_at_once_as_it_asked(self):
        """S18, over HTTP: a batch job whose model does not fit waits for room
        - queued, holding back nobody, its progress the numbers in words - by
        default; one that asked to fail (`if_no_room: fail`) fails at once,
        `no_room`, in the loud words a live job does. Nothing loud is said
        for the one that waits: waiting is no failure. CHANGED ON PURPOSE
        (S18b): each job says `priority: batch` - one that names none is live."""
        said = []
        app = self.serve(self.adapter(folder=None, holder=None, log=said.append,
                                      manager_kw={"card": LoudCard()}))
        folder = self.tmp / "book"
        folder.mkdir()
        self.http(app, "POST", "/api/workspace", {"path": str(folder)})
        _, sub = self.http(app, "POST", "/api/jobs", {"kind": "generate",
                                                       "input": {"prompt": "rain"},
                                                       "gen": {"model": "demo"},
                                                       "priority": "batch"})
        deadline = time.monotonic() + 15
        while True:
            _, res = self.http(app, "GET", f"/api/jobs/{sub['job_id']}/result")
            if res["job"]["progress"].get("stage") == "waiting for room on the card":
                break
            self.assertLess(time.monotonic(), deadline, f"it never waited for room: {res}")
            time.sleep(0.05)
        job = res["job"]
        self.assertEqual(job["status"], "queued", job)
        for words in ("demo: it needs 9000 MiB", "programs outside the bootstrap hold 20576 MiB",
                      "starts by itself once it fits"):
            self.assertIn(words, job["progress"]["waiting_for"])
        _, h = self.http(app, "GET", "/api/health")
        self.assertEqual((h["queue"]["scheduled"]["for_room"], h["queue"]["scheduled"]["room"]),
                         (1, job["progress"]["waiting_for"]))
        self.assertEqual([line for line in said if "or reboot" in line], [],
                         "waiting for room was said as a failure")
        res = self.run_job(app, job={"priority": "batch", "if_no_room": "fail"})
        self.assertEqual((res["job"]["status"], res["job"].get("reason"), res["job"]["if_no_room"]),
                         ("failed", "no_room", "fail"), res["job"])
        self.assertIn("does not wait for room", res["job"]["error"])

    def test_the_stand_ins_card_read_full_holds_a_job_and_it_runs_again_by_itself(self):
        """S16a R2, over HTTP, from the stand-in's settings alone - as its
        command line runs them: a session's card read full is DEGRADED in
        health, a job submitted then is HELD for the card's memory, saying
        so, and once the card reads free again the session holds it again
        and the job runs by itself. Never the real card: the stand-in reads
        its own, on a lock of its own."""
        path = self.tmp / "stand-in-card.toml"
        path.write_text(
            f'[runner]\nbackend = "mock"\ncard_lock = "{(self.tmp / "own.lock").as_posix()}"\n'
            'card_watch_s = 0.05\ncard_recover_watch_s = 0.05\nsession_models = ["demo"]\n'
            '[mock]\ntokens_per_second = 0\nload_seconds = 0\nunload_seconds = 0\n'
            'card_mib = 24576\ncard_full_s = [1.0, 2.5]\n[models.demo]\nlabel = "Demo"\n',
            encoding="utf-8")
        app = self.serve(S.build_backend("mock", path, log=lambda s: None))
        folder = self.tmp / "book"
        folder.mkdir()
        self.http(app, "POST", "/api/workspace", {"path": str(folder)})
        deadline = time.monotonic() + 10
        while self.http(app, "GET", "/api/health")[1]["pipeline"]["state"] != "degraded":
            self.assertLess(time.monotonic(), deadline, "the stand-in's card never read full")
            time.sleep(0.05)
        _, h = self.http(app, "GET", "/api/health")
        self.assertIn("The card is full", h["pipeline"]["why"])
        _, sub = self.http(app, "POST", "/api/jobs", {"kind": "generate",
                                                       "input": {"prompt": "rain"},
                                                       "gen": {"model": "demo", "max_tokens": 5}})
        jid, stages = sub["job_id"], set()
        deadline = time.monotonic() + 15
        while True:
            _, res = self.http(app, "GET", f"/api/jobs/{jid}/result")
            stages.add(res["job"]["progress"].get("stage"))
            if res["job"]["status"] in ("done", "failed", "cancelled"):
                break
            self.assertLess(time.monotonic(), deadline, f"the job never ended: {res['job']}")
            time.sleep(0.05)
        self.assertEqual(res["job"]["status"], "done", res["job"])
        self.assertIn("waiting for the card's memory", stages)
        self.assertEqual(self.http(app, "GET", "/api/health")[1]["pipeline"]["state"], "holding")

    def test_health_says_what_the_runner_holds_and_answers_the_contract(self):
        app = self.serve(self.adapter(folder=None, holder=None))
        folder = self.tmp / "book"
        folder.mkdir()
        self.http(app, "POST", "/api/workspace", {"path": str(folder)})
        self.run_job(app)
        status, h = self.http(app, "GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(V.check(h, health_shape(), SHAPES), [])
        self.assertEqual((h["runner"]["loaded"], h["runner"]["lease"]["holder"]),
                         ("demo", app.actor))

    def test_a_server_with_no_runner_says_null(self):
        app = self.serve(S.EchoBackend(delay_s=0.001))
        _, h = self.http(app, "GET", "/api/health")
        self.assertIsNone(h["runner"])
        self.assertEqual(V.check(h, health_shape(), SHAPES), [])

    def test_a_rebind_moves_the_record_and_keeps_the_model(self):
        app = self.serve(self.adapter(folder=None, holder=None))
        first, second = self.tmp / "one", self.tmp / "two"
        first.mkdir()
        second.mkdir()
        self.http(app, "POST", "/api/workspace", {"path": str(first)})
        self.run_job(app)
        self.http(app, "POST", "/api/workspace", {"path": str(second)})
        self.run_job(app)
        st = app.backend.manager.status()
        self.assertEqual(st["loads_this_session"], 1, "a rebind reloaded the model")
        kinds = [e["event"] for e in read_events(second / "runner" / M.EVENTS_NAME)]
        self.assertEqual(kinds[:2], ["session_start", "load_carried"])
        self.assertIn("rebound_away",
                      [e["event"] for e in read_events(first / "runner" / M.EVENTS_NAME)])

    def test_letting_the_folder_go_stops_the_record_there(self):
        app = self.serve(self.adapter(folder=None, holder=None))
        folder = self.tmp / "book"
        folder.mkdir()
        self.http(app, "POST", "/api/workspace", {"path": str(folder)})
        self.run_job(app)
        self.http(app, "POST", "/api/workspace/unload", {})
        events = folder / "runner" / M.EVENTS_NAME
        before = events.read_bytes()
        self.assertEqual(read_events(events)[-1]["event"], "rebound_away")
        app.backend.manager.unload()
        self.assertEqual(events.read_bytes(), before, "the record went on after the folder was let go")

    def test_the_server_stopping_takes_the_model_off_the_card(self):
        a = self.adapter(folder=None, holder=None)
        httpd = S.serve(TL.quiet_port(), backend=a, log=lambda s: None, **FAST)
        folder = self.tmp / "book"
        folder.mkdir()
        self.http(httpd.app, "POST", "/api/workspace", {"path": str(folder)})
        self.run_job(httpd.app)
        self.assertEqual(a.manager.status()["loaded"], "demo")
        S.stop(httpd)
        st = a.manager.status()
        self.assertEqual((st["loaded"], st["lease"]["holder"]), (None, None))

    def bound(self, adapter):
        """A served adapter bound to a folder of its own: -> (httpd, folder)."""
        httpd = S.serve(TL.quiet_port(), backend=adapter, log=lambda s: None, **FAST)
        folder = self.tmp / "book"
        folder.mkdir()
        self.http(httpd.app, "POST", "/api/workspace", {"path": str(folder)})
        return httpd, folder

    def test_the_server_stopping_puts_the_unload_on_the_folders_record(self):
        """Found on the real card: the stop let the folder go FIRST, so its
        record said the load had left for somewhere else (`rebound_away`), and
        the unload itself was written nowhere."""
        httpd, folder = self.bound(self.adapter(folder=None, holder=None))
        self.run_job(httpd.app)
        S.stop(httpd)
        events = read_events(folder / "runner" / M.EVENTS_NAME)
        kinds = [e["event"] for e in events]
        self.assertIn("unload_ok", kinds)
        self.assertLess(kinds.index("load_ok"), kinds.index("unload_ok"))
        self.assertNotIn("rebound_away", kinds, "a model that came off was said to have left")
        self.assertEqual(M.orphaned_loads(events), [])

    def test_an_unload_that_fails_at_the_stop_stays_open_on_the_folders_record(self):
        """What that record is for: a model that would not come off at the stop
        is still open there, so the next start on the folder finds it."""
        cfg = self.config()

        class Stuck(MockBackend):
            def stop(self_):
                raise BackendError("it would not stop")
        a = self.adapter(cfg, folder=None, holder=None,
                         manager_kw={"backend_factory": lambda key, port: Stuck(key, key,
                                                                                cfg.mock)})
        httpd, folder = self.bound(a)
        self.run_job(httpd.app)
        S.stop(httpd)
        events = read_events(folder / "runner" / M.EVENTS_NAME)
        self.assertIn("unload_failed", [e["event"] for e in events])
        self.assertEqual([o["model_key"] for o in M.orphaned_loads(events + [
            {"event": "session_start"}])], ["demo"], "the next start would not find it")

    def test_a_stop_while_the_model_loads_gives_the_load_up_at_once(self):
        """Found on the real card: a stop during a load waited the load out -
        10.2 s, for a load of 8.2 - and a load that never came up would have
        held the stop for the whole startup timeout."""
        path = self.tmp / "slow.toml"
        path.write_text('[runner]\nbackend = "mock"\n\n[mock]\ntokens_per_second = 0\n'
                        'load_seconds = 20\nunload_seconds = 0\n\n[models.demo]\n',
                        encoding="utf-8")
        a = self.adapter(Config.load(path), folder=None, holder=None)
        httpd, folder = self.bound(a)
        stopped = []
        self.addCleanup(lambda: stopped or S.stop(httpd))
        self.http(httpd.app, "POST", "/api/jobs", {"kind": "generate", "input": {"prompt": "x"},
                                                   "gen": {"model": "demo"}})
        deadline = time.monotonic() + 10
        while a.manager.status()["loading"] != "demo":
            self.assertLess(time.monotonic(), deadline, "the load never started")
            time.sleep(0.01)
        box = {}
        t = threading.Thread(target=lambda: box.update(took=_timed(S.stop, httpd)), daemon=True)
        t.start()
        t.join(8)
        stopped.append(True)
        self.assertFalse(t.is_alive(), "the stop waited out the load")
        self.assertLess(box["took"], 3.0, f"the stop took {box['took']:.1f}s")
        failed = [e for e in read_events(folder / "runner" / M.EVENTS_NAME)
                  if e["event"] == "load_failed"]
        self.assertTrue(failed and "the server is stopping" in failed[-1]["detail"], failed)
        self.assertIsNone(a.manager.status()["loaded"])

    def slow_models(self, load_seconds):
        """A served stand-in with two models whose loads take *load_seconds*:
        -> (httpd, folder, adapter)."""
        path = self.tmp / "slow.toml"
        path.write_text('[runner]\nbackend = "mock"\n\n[mock]\ntokens_per_second = 0\n'
                        f'load_seconds = {load_seconds}\nunload_seconds = 0\n\n'
                        '[models.demo]\n\n[models.other]\n', encoding="utf-8")
        a = self.adapter(Config.load(path), folder=None, holder=None)
        httpd, folder = self.bound(a)
        self.addCleanup(S.stop, httpd)
        return httpd, folder, a

    def submit_and_wait_for_the_load(self, httpd, a, *models):
        jids = [self.http(httpd.app, "POST", "/api/jobs",
                          {"kind": "generate", "input": {"prompt": "x"},
                           "gen": {"model": m}})[1]["job_id"] for m in models]
        deadline = time.monotonic() + 10
        while a.manager.status()["loading"] != models[0]:
            self.assertLess(time.monotonic(), deadline, "the load never started")
            time.sleep(0.01)
        return jids

    def ended(self, httpd, jid, within):
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            job = self.http(httpd.app, "GET", f"/api/jobs/{jid}/result")[1]["job"]
            if job["status"] in ("done", "failed", "cancelled"):
                return job
            time.sleep(0.02)
        self.fail(f"{jid} had not ended {within}s later")

    def test_a_job_cancelled_while_its_model_loads_ends_at_once_with_nothing_on_the_card(self):
        """Found on the real card: the job ended only once the weights were up
        - 7.0-7.4 s after the cancel, for a load of about 8 - and the model
        then sat on the card for nobody."""
        httpd, folder, a = self.slow_models(20)
        (jid,) = self.submit_and_wait_for_the_load(httpd, a, "demo")
        started = time.monotonic()
        self.http(httpd.app, "POST", f"/api/jobs/{jid}/cancel", {})
        job = self.ended(httpd, jid, 3.0)
        self.assertEqual(job["status"], "cancelled", job)
        self.assertLess(time.monotonic() - started, 3.0)
        st = a.manager.status()
        self.assertEqual((st["loaded"], st["loading"], st["lease"]["holder"]), (None, None, None))
        failed = [e for e in read_events(folder / "runner" / M.EVENTS_NAME)
                  if e["event"] == "load_failed"]
        self.assertTrue(failed and "its job was cancelled" in failed[-1]["detail"], failed)

    def test_the_next_job_wanting_the_same_model_gets_the_load_the_cancelled_one_began(self):
        httpd, folder, a = self.slow_models(2)
        first, second = self.submit_and_wait_for_the_load(httpd, a, "demo", "demo")
        self.http(httpd.app, "POST", f"/api/jobs/{first}/cancel", {})
        self.assertEqual(self.ended(httpd, first, 10)["status"], "cancelled")
        self.assertEqual(self.ended(httpd, second, 10)["status"], "done")
        kinds = [e["event"] for e in read_events(folder / "runner" / M.EVENTS_NAME)]
        self.assertEqual(kinds.count("load_start"), 1, "the model was loaded twice")
        self.assertIn("load_kept", kinds)
        self.assertLess(kinds.index("load_kept"), kinds.index("reuse"))

    def test_the_next_job_wanting_another_model_does_not_keep_the_load(self):
        httpd, folder, a = self.slow_models(20)
        first, second = self.submit_and_wait_for_the_load(httpd, a, "demo", "other")
        started = time.monotonic()
        self.http(httpd.app, "POST", f"/api/jobs/{first}/cancel", {})
        self.assertEqual(self.ended(httpd, first, 3.0)["status"], "cancelled")
        self.assertLess(time.monotonic() - started, 3.0, "the load went on for a job that "
                                                         "wants another model")
        deadline = time.monotonic() + 10
        while a.manager.status()["loading"] != "other":
            self.assertLess(time.monotonic(), deadline, "the next job's load never started")
            time.sleep(0.01)
        self.http(httpd.app, "POST", f"/api/jobs/{second}/cancel", {})
        self.assertEqual(self.ended(httpd, second, 3.0)["status"], "cancelled")

    def test_the_request_record_holds_the_runners_exact_call(self):
        app = self.serve(self.adapter(folder=None, holder=None))
        folder = self.tmp / "book"
        folder.mkdir()
        self.http(app, "POST", "/api/workspace", {"path": str(folder)})
        res = self.run_job(app)
        self.assertEqual(res["job"]["status"], "done", res)
        rec = Q.request_record(folder, res["job"]["correlation_id"])
        self.assertEqual(rec["samples"]["01"]["call.json"], app.backend.manager._backend.last_call)
        self.assertEqual(rec["samples"]["01"]["candidate.json"]["load"],
                         res["candidates"][0]["load"])
        self.assertEqual(rec["events"][-1]["event"], "ended")

    def test_a_served_job_that_names_no_temperature_runs_at_its_models_own(self):
        """The owner's use case, read off the request record: a model set to
        0.4 in the runner's settings, and a job that names no temperature."""
        app = self.serve(self.adapter(self.config("temperature = 0.4\n"),
                                      folder=None, holder=None))
        folder = self.tmp / "book"
        folder.mkdir()
        self.http(app, "POST", "/api/workspace", {"path": str(folder)})
        res = self.run_job(app)
        self.assertEqual(res["job"]["status"], "done", res)
        sample = Q.request_record(folder, res["job"]["correlation_id"])["samples"]["01"]
        self.assertNotIn("temperature", sample["params.json"]["params"])
        self.assertEqual(sample["call.json"]["temperature"], 0.4)

    def test_runner_flags_on_a_backend_with_no_runner_are_refused(self):
        for argv in (["--backend", "echo", "--runner", "x.toml"], ["--reclaim"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as caught:
                    S.parse_args(argv)
                self.assertEqual(caught.exception.code, 2)
        self.assertEqual(S.parse_args(["--backend", "mock", "--reclaim"]).reclaim, True)

    def test_the_command_line_backend_is_what_runs(self):
        path = self.tmp / "llama.toml"
        path.write_text('[runner]\nbackend = "llamacpp"\n', encoding="utf-8")
        said = []
        b = S.build_backend("mock", path, log=said.append)
        self.addCleanup(b.close)
        self.assertEqual(b.manager.backend_kind, "mock")
        self.assertTrue(any("llamacpp" in s and "mock" in s for s in said), said)

    def test_llamacpp_on_the_command_line_refuses_settings_that_name_a_whisper_model(self):
        """The command line's backend is what runs (the row above), and
        `llamacpp` runs every model on llama.cpp: settings that name a whisper
        model are refused as the server starts, naming `--backend real` -
        never a whisper model run by a backend that says it runs none (found
        by review: the settings' own check had passed them under `real`)."""
        path = self.tmp / "real.toml"
        path.write_text('[runner]\nbackend = "real"\n\n[models.story]\n\n'
                        '[models.ears]\nengine = "whisper"\n', encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            S.build_backend("llamacpp", path, log=lambda s: None)
        self.assertIn("[models.ears]", str(caught.exception))
        self.assertIn("--backend real", str(caught.exception))
        stand_in = S.build_backend("mock", path, log=lambda s: None)
        self.addCleanup(stand_in.close)
        self.assertEqual(stand_in.manager.backend_kind, "mock")

    def test_real_is_a_backend_the_command_line_names_and_it_holds_the_machines_card(self):
        """`--backend real` (PLAN-4 D10): the runner, each model on its own
        engine - read by `--runner`, `--reclaim` and `--name` as `llamacpp`
        is - and, with no coordinator named, the card through the machine's
        lock, whatever each model's engine (here the suite's own lock file)."""
        a = S.parse_args(["--backend", "real", "--runner", "x.toml", "--reclaim",
                          "--name", "desk"])
        self.assertEqual((a.backend, a.runner, a.reclaim, a.name),
                         ("real", "x.toml", True, "desk"))
        path = self.tmp / "real.toml"
        path.write_text('[runner]\nbackend = "real"\n\n[models.ears]\nengine = "whisper"\n',
                        encoding="utf-8")
        b = S.build_backend("real", path, log=lambda s: None)
        self.addCleanup(b.close)
        self.assertEqual((b.manager.backend_kind, b.manager.status()["backend"]),
                         ("real", "real"))
        self.assertEqual(b.manager._card.kind, "machine lock")

    def test_a_real_servers_lock_watches_the_card_as_its_settings_say(self):
        """S5b: run on its own, the server's lock watches the card as its
        `[runner]` settings say - how often, how often while degraded, its
        floor."""
        path = self.tmp / "watch.toml"
        path.write_text('[runner]\nbackend = "real"\ncard_watch_s = 7.0\n'
                        'card_recover_watch_s = 0.7\ncard_floor_mib = 700\n\n'
                        '[models.ears]\nengine = "whisper"\n', encoding="utf-8")
        b = S.build_backend("real", path, log=lambda s: None)
        self.addCleanup(b.close)
        card = b.manager._card
        self.assertEqual((card.watch_s, card.recover_watch_s, card.floor_mib), (7.0, 0.7, 700))


# ============================================== one call, two kinds (S8) ===

class NativeOnePieceRows(ServerCase):
    """PLAN-4 D10, behind the queue: a whisper model's request is audio, and
    its answer comes in one piece - its deadline said on the job's progress
    first, then the answer whole, whisper-server's own fields beside the
    text. A transcript of silence is an answer."""

    def ears(self, answer=0.0, due=60.0, **kw) -> RunnerBackend:
        path = self.tmp / "ears.toml"
        path.write_text(EARS.format(answer=answer, due=due), encoding="utf-8")
        return self.adapter(Config.load(path), **kw)

    def test_a_transcription_says_its_deadline_then_answers_whole(self):
        """Its deadline - the one its model declares before it is asked
        (`answer_deadline_s`) - is on the job's progress BEFORE the wait for
        the answer, which takes the stand-in 1.2 s here: said then, not with
        the answer."""
        a = self.ears(answer=1.2, due=9)
        run = FakeRun()
        out = a({"audio": str(_wav(self.tmp / "said.wav"))},
                {"model": "ears", "max_tokens": 600}, run)
        said = [f for _, f in run.reports]
        self.assertEqual(said, [{"stage": "answering", "due_s": 9.0},
                                {"tokens": out["tokens"], "stage": "done"}],
                         "a transcription said something between its deadline and its answer")
        self.assertGreaterEqual(run.reports[1][0] - run.reports[0][0], 1.0,
                                "the deadline was said with the answer, not before the wait")
        self.assertEqual((out["model"], out["finish_reason"], out["audio_s"]),
                         ("ears", "stop", 1.5))
        self.assertTrue(out["text"].strip())
        self.assertEqual(out["text"], out["transcript"]["text"])
        self.assertEqual(out["tokens"],
                         sum(len(s["tokens"]) for s in out["transcript"]["segments"]))
        self.assertTrue(out["load"].startswith("load-"))
        self.assertEqual(V.check(out, {"ref": "candidate"}, SHAPES), [])

    def test_a_transcription_of_silence_is_a_job_done_end_to_end(self):
        """Over HTTP, through the queue: a transcript with no words is a job
        done, never "the model returned nothing" - that is the text engine's
        rule - and its answer is the contract's."""
        app = self.serve(self.ears(folder=None, holder=None))
        folder = self.tmp / "book"
        folder.mkdir()
        self.assertEqual(self.http(app, "POST", "/api/workspace", {"path": str(folder)})[0], 200)
        (route,) = [r for r in ROUTES.values() if r.get("id") == "server.jobs.result"]
        quiet = self.run_job(app, model="ears",
                             input={"audio": str(_wav(self.tmp / "quiet.wav", 2.0, sound=False))})
        self.assertEqual(quiet["job"]["status"], "done", quiet)
        cand = quiet["candidates"][0]
        self.assertEqual((cand["text"], cand["transcript"]["segments"], cand["audio_s"]),
                         ("", [], 2.0))
        self.assertEqual(V.check(quiet, route["responses"]["200"], SHAPES), [])
        spoken = self.run_job(app, model="ears",
                              input={"audio": str(_wav(self.tmp / "said.wav"))})
        self.assertEqual(spoken["job"]["status"], "done", spoken)
        self.assertTrue(spoken["candidates"][0]["text"].strip())

    def test_a_request_of_the_wrong_kind_for_its_models_engine_fails_before_any_load(self):
        a = self.ears()
        wav = str(_wav(self.tmp / "said.wav"))
        for request, model, words in (
                ({"audio": wav}, "demo", "a prompt or a conversation"),
                ("Tell me about rain.", "ears", "transcribes audio"),
                ({"messages": [{"role": "user", "content": "hi"}]}, "ears", "transcribes audio")):
            with self.subTest(model=model, request=request):
                with self.assertRaises(Q.GenerationFailed) as caught:
                    a(request, {"model": model}, FakeRun())
                self.assertIn(words, str(caught.exception))
        self.assertEqual(a.manager.status()["loads_this_session"], 0,
                         "a model was loaded for a request it could never answer")

    def test_an_audio_request_is_the_contracts_and_names_only_whisper_servers_own_fields(self):
        """A transcription's request is the contract's (`audio_request`), held
        to it whole; every field it names is one whisper-server reads - which
        ignores one it does not, so a misspelt field is refused here, never
        sent - and `response_format` is the engine's own."""
        a = self.ears()
        wav = str(_wav(self.tmp / "said.wav"))
        for request, words in (({"audio": 3}, "$.audio"),
                               ({"audio": wav, "fields": ["language"]}, "$.fields"),
                               ({"audio": wav, "extra": 1}, "extra"),
                               ({"audio": wav, "fields": {"langauge": "en"}}, "langauge"),
                               ({"audio": wav, "fields": {"response_format": "json"}},
                                "response_format")):
            with self.subTest(request=request):
                with self.assertRaises(Q.GenerationFailed) as caught:
                    a(request, {"model": "ears"}, FakeRun())
                self.assertIn(words, str(caught.exception))
        out = a({"audio": wav, "fields": {"language": "no", "temperature": 0.2,
                                          "no_language_probabilities": True}},
                {"model": "ears"}, FakeRun())
        self.assertEqual(out["transcript"]["language"], "norwegian")
        self.assertNotIn("language_probabilities", out["transcript"])

    def test_response_format_is_refused_in_the_engines_own_words(self):
        """`response_format` is not one of the fields: the engine asks for the
        verbose answer itself, and a request naming it is told so - not merely
        that the field is unexpected."""
        a = self.ears()
        wav = str(_wav(self.tmp / "said.wav"))
        with self.assertRaises(Q.GenerationFailed) as caught:
            a({"audio": wav, "fields": {"response_format": "verbose_json"}}, {"model": "ears"},
              FakeRun())
        self.assertIn("response_format is the engine's own", str(caught.exception))

    def test_a_field_of_the_wrong_kind_is_refused_never_sent(self):
        """Each field is of the kind whisper-server's parser reads it as (the
        contract's `audio_request`): a whole number, a number, true or false,
        or text. One of another kind would be read as garbage there - its
        integer parse throws, and the answer is a 500 - so it is refused."""
        a = self.ears()
        wav = str(_wav(self.tmp / "said.wav"))
        for fields, words in (({"beam_size": "five"}, "$.fields.beam_size: expected integer"),
                              ({"beam_size": 2.5}, "$.fields.beam_size: expected integer"),
                              ({"temperature": "0.2"}, "$.fields.temperature: expected number"),
                              ({"vad": "true"}, "$.fields.vad: expected boolean"),
                              ({"language": None}, "$.fields.language: expected string")):
            with self.subTest(fields=fields):
                with self.assertRaises(Q.GenerationFailed) as caught:
                    a({"audio": wav, "fields": fields}, {"model": "ears"}, FakeRun())
                self.assertIn(words, str(caught.exception))
        self.assertEqual(a.manager.status()["loads_this_session"], 0)

    def test_audio_that_cannot_be_read_fails_before_any_model_is_loaded(self):
        """The WAV is read before the model is loaded - its length is what
        the answer's deadline is reckoned from - and one that cannot be read
        fails the job there, in words, with nothing loaded for it."""
        a = self.ears()
        not_wav = self.tmp / "notes.txt"
        not_wav.write_bytes(b"not a sound at all, only words")
        cut = self.tmp / "cut.wav"
        cut.write_bytes(_wav(self.tmp / "whole.wav").read_bytes()[:30])
        for path, words in ((self.tmp / "gone.wav", "not there"), (not_wav, "not a WAV"),
                            (cut, "not a WAV")):
            with self.subTest(path=path.name):
                with self.assertRaises(Q.GenerationFailed) as caught:
                    a({"audio": str(path)}, {"model": "ears"}, FakeRun())
                self.assertIn(words, str(caught.exception))
                self.assertIn(str(path), str(caught.exception))
        self.assertEqual(a.manager.status()["loads_this_session"], 0,
                         "a model was loaded for audio that could not be read")


class NativeSubmitCheckRows(ServerCase):
    """PLAN-4 D11 (S8 R8): with the default hooks - where the request is the
    job's input - a job for a whisper model is checked AS IT IS SUBMITTED: an
    input that is not an audio request whisper-server reads is refused
    `bad_audio_request`, in the words the job's turn would say, never stored
    and never loaded for. A text model's request is not checked then (it is
    at the job's turn, as ever), and a model the settings do not name is left
    to the job's turn to say so."""

    def ears(self) -> RunnerBackend:
        path = self.tmp / "ears.toml"
        path.write_text(EARS.format(answer=0.0, due=60.0), encoding="utf-8")
        return self.adapter(Config.load(path))

    def test_the_runner_refuses_a_whisper_models_request_it_could_never_send(self):
        a = self.ears()
        wav = str(_wav(self.tmp / "said.wav"))
        for request, words in (
                ({"audio": wav, "fields": {"langauge": "no"}}, 'unexpected field "langauge"'),
                ({"audio": wav, "fields": {"beam_size": "five"}}, "expected integer"),
                ({"audio": wav, "fields": {"response_format": "json"}},
                 "response_format is the engine's own"),
                ({"fields": {"language": "no"}}, "transcribes audio"),
                ("Tell me about rain.", "transcribes audio")):
            with self.subTest(request=request):
                with self.assertRaises(Q.Refused) as caught:
                    a.check_request(request, {"model": "ears"})
                self.assertEqual((caught.exception.reason, caught.exception.code),
                                 ("bad_audio_request", 400))
                self.assertIn(words, caught.exception.error)
        for request, model in (({"audio": wav, "fields": {"language": "no", "beam_size": 5,
                                                          "vad": True}}, "ears"),
                               ({"audio": str(self.tmp / "written-later.wav")}, "ears"),
                               ({"audio": wav, "fields": {"langauge": "no"}}, "demo"),
                               ("anything at all", "nobody")):
            with self.subTest(model=model, request=request):
                self.assertIsNone(a.check_request(request, {"model": model}))
        self.assertEqual(a.manager.status()["loads_this_session"], 0)

    def test_a_submit_with_the_default_hooks_is_refused_and_stores_nothing(self):
        a = self.ears()
        folder = self.tmp / "desk"
        folder.mkdir()
        q = Q.Queue(folder, Q.Hooks(), a, start=False, log=lambda s: None)
        self.addCleanup(q.close, 3.0)
        wav = str(_wav(self.tmp / "said.wav"))
        with self.assertRaises(Q.Refused) as caught:
            q.submit("transcribe", {"audio": wav, "fields": {"langauge": "no"}},
                     {"model": "ears"})
        self.assertEqual((caught.exception.reason, caught.exception.error),
                         ("bad_audio_request",
                          'the request is not an audio request: $.fields: unexpected field '
                          '"langauge"'))
        self.assertEqual(q.jobs(), [])
        self.assertTrue(q.submit("transcribe", {"audio": wav, "fields": {"language": "no"}},
                                 {"model": "ears"})["job_id"])


# ======================================================= llama.cpp, served ===

class NativeLlamaServedRows(ServerCase):
    """`--backend llamacpp` end to end, over HTTP, against the stand-in."""

    def setUp(self):
        super().setUp()
        self.models = self.tmp / "models"
        self.models.mkdir()
        TL.build_gguf(self.models, name="model.gguf", tensor_elems=(1024 * 1024,),
                      bits_per_weight=5.5)
        path = self.tmp / "llama.toml"
        path.write_text(
            "[runner]\nbackend = \"llamacpp\"\n"
            f'llama_server = ["{Path(TL.LAUNCHER[0]).as_posix()}", "{TL.FAKE.as_posix()}"]\n'
            f'models_dir = "{self.models.as_posix()}"\n'
            "startup_timeout_s = 20.0\nhealth_poll_s = 0.05\nshutdown_timeout_s = 10.0\n\n"
            '[models.story]\nfile = "model.gguf"\n', encoding="utf-8")
        self.settings = path

    def runner(self, reclaim=False):
        m = ModelManager(Config.load(self.settings), None, vram=lambda: None)
        return RunnerBackend(m, reclaim=reclaim, log=self.said.append)

    def test_a_job_runs_end_to_end_on_llama_cpp(self):
        self.said = []
        before = len(TL.SPAWNS)
        a = self.runner()
        httpd = S.serve(TL.quiet_port(), backend=a, log=lambda s: None, **FAST)
        stopped = []
        self.addCleanup(lambda: stopped or S.stop(httpd))
        folder = self.tmp / "book"
        folder.mkdir()
        self.http(httpd.app, "POST", "/api/workspace", {"path": str(folder)})
        res = self.run_job(httpd.app, model="story", max_tokens=30)
        self.assertEqual(res["job"]["status"], "done", res)
        self.assertTrue(res["candidates"][0]["text"])
        _, h = self.http(httpd.app, "GET", "/api/health")
        self.assertEqual(V.check(h, health_shape(), SHAPES), [])
        self.assertTrue(h["runner"]["port"] and h["runner"]["pid"])
        ((pid, load),) = TL.launched_since(before)
        S.stop(httpd)
        stopped.append(True)
        # CHANGED: asked by the load's id, never its pid alone (GUIDE rule 9)
        self.assertTrue(TL.gone(pid, load), "the server stopped and left its llama-server running")

    def test_two_servers_with_no_coordinator_given_jobs_at_once_take_turns_on_the_card(self):
        """The race the machine's lock closes: two servers, one card, no
        coordinator, each handed a job in the same instant. Without the lock,
        both passed the free-memory check and both loaded."""
        self.said = []
        lock = self.tmp / "card.lock"
        self.settings.write_text(self.settings.read_text(encoding="utf-8").replace(
            "[runner]\n", f'[runner]\ncard_lock = "{lock.as_posix()}"\nidle_unload_s = 0.3\n', 1),
            encoding="utf-8")
        servers = []
        for name in ("one", "two"):
            a = RunnerBackend(ModelManager(Config.load(self.settings), None, vram=lambda: None),
                              log=self.said.append, idle_poll_s=0.05)
            httpd = S.serve(TL.quiet_port(), backend=a, log=lambda s: None, **FAST)
            self.addCleanup(S.stop, httpd)
            folder = self.tmp / name
            folder.mkdir()
            self.http(httpd.app, "POST", "/api/workspace", {"path": str(folder)})
            servers.append((httpd, folder))
        go, results = threading.Barrier(2), {}

        def run(i):
            # bounded (H6's register 5): a thread whose partner never came
            # says so, and never waits on for ever
            try:
                go.wait(60)
            except threading.BrokenBarrierError:
                results[i] = {"job": {"status": "its partner never came to the start, in 60 s"}}
                return
            results[i] = self.run_job(servers[i][0].app, model="story", max_tokens=10)
        threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual([results[i]["job"]["status"] for i in range(2)], ["done", "done"],
                         results)
        spans = []
        for _, folder in servers:
            events = read_events(folder / "runner" / M.EVENTS_NAME)
            spans.append((next(e["ts"] for e in events if e["event"] == "load_start"),
                          next((e["ts"] for e in events if e["event"] == "unload_ok"), None)))
        (first_up, first_off), (second_up, _) = sorted(spans, key=lambda s: s[0])
        self.assertIsNotNone(first_off, "the first model never came off the card")
        self.assertGreaterEqual(second_up, first_off, "two models were on the card at once")

    def test_the_record_keeps_the_call_llama_server_got_and_never_its_key(self):
        got = self.tmp / "received.jsonl"
        self.settings.write_text(self.settings.read_text(encoding="utf-8")
                                 + f'extra_args = ["--fake-record", "{got.as_posix()}"]\n',
                                 encoding="utf-8")
        self.said = []
        a = self.runner()
        httpd = S.serve(TL.quiet_port(), backend=a, log=lambda s: None, **FAST)
        self.addCleanup(S.stop, httpd)
        folder = self.tmp / "book"
        folder.mkdir()
        self.http(httpd.app, "POST", "/api/workspace", {"path": str(folder)})
        res = self.run_job(httpd.app, model="story", max_tokens=30)
        self.assertEqual(res["job"]["status"], "done", res)
        key = a.manager._backend._key
        received = [e["body"] for e in Q.read_jsonl(got) if e.get("event") == "request"]
        rec = Q.request_record(folder, res["job"]["correlation_id"])
        self.assertEqual(len(received), 1)
        self.assertEqual(rec["samples"]["01"]["call.json"], received[0],
                         "the record's call is not the one llama-server received")
        files = [f for f in rec["dir"].rglob("*") if f.is_file()]
        self.assertGreaterEqual(len(files), 5)
        for f in files:
            self.assertNotIn(key, f.read_text(encoding="utf-8", errors="replace"), f.name)

    def recording(self, *fake):
        """A served runner whose stand-in llama-server records every request
        it RECEIVES - started with `fake`'s switches too - bound to a folder
        (`book`): -> (app, the record's path)."""
        got = self.tmp / "received.jsonl"
        args = ", ".join(f'"{a}"' for a in ("--fake-record", got.as_posix(), *fake))
        self.settings.write_text(self.settings.read_text(encoding="utf-8")
                                 + f"extra_args = [{args}]\n", encoding="utf-8")
        self.said = []
        httpd = S.serve(TL.quiet_port(), backend=self.runner(), log=lambda s: None, **FAST)
        self.addCleanup(S.stop, httpd)
        folder = self.tmp / "book"
        folder.mkdir()
        self.http(httpd.app, "POST", "/api/workspace", {"path": str(folder)})
        return httpd.app, got

    @staticmethod
    def received(got) -> list:
        return [e["body"] for e in Q.read_jsonl(got) if e.get("event") == "request"]

    def test_a_conversation_and_every_parameter_reach_llama_server_as_they_were_sent(self):
        """Every text parameter and messages with roles, end to end:
        submitted over HTTP, read off what the stand-in llama-server received."""
        from runner.llama_fields import CHAT_FIELDS
        app, got = self.recording()
        named = TL.every_parameter()
        res = self.run_job(app, model="story", input={"messages": TL.TALK}, **named)
        self.assertEqual(res["job"]["status"], "done", res)
        (body,) = self.received(got)
        self.assertEqual(body["messages"], TL.TALK)
        for name, value in named.items():
            with self.subTest(parameter=name):
                self.assertTrue(TL.same(body.get(name), value), f"received {body.get(name)!r}")
        self.assertEqual(body["model"], res["candidates"][0]["load"], "the load it was for")
        self.assertEqual(sorted(set(body) - set(CHAT_FIELDS)), [],
                         "llama-server was sent a field it does not read")

    def test_a_misspelt_parameter_never_leaves_the_bootstrap(self):
        app, got = self.recording()
        self.assertEqual(self.run_job(app, model="story", max_tokens=10)["job"]["status"], "done")
        s, out = self.http(app, "POST", "/api/jobs",
                           {"kind": "generate", "input": {"prompt": "hi"},
                            "gen": {"model": "story", "temprature": 0.7}})
        self.assertEqual((s, out.get("reason"), out.get("error")),
                         (400, "bad_request", '$.gen: unexpected field "temprature"'))
        bodies = self.received(got)
        self.assertEqual(len(bodies), 1, "llama-server was asked for the refused job")
        self.assertNotIn("temprature", json.dumps(bodies))

    def test_a_request_counted_too_long_for_its_models_context_is_never_asked(self):
        """The count end to end, over HTTP: a model whose slot has a context
        of 40 (not the 8192 its settings name) counts a prompt of ten words
        as 14 tokens - its template's too. With room for 26 more it fits to
        the token and is answered; with 27 its job fails `prompt_too_long`,
        saying the three numbers, and llama-server is never asked to answer.
        Each job's count is on its progress and in its record."""
        app, got = self.recording("--fake-n-ctx", "40")
        prompt = " ".join(["word"] * 10)
        self.assertEqual(TL.stand_in_tokens([{"role": "user", "content": prompt}]), 14)
        fits = self.run_job(app, prompt, model="story", max_tokens=26)
        over = self.run_job(app, prompt, model="story", max_tokens=27)
        shape = ROUTES["server/job-result"]["responses"]["200"]
        for res in (fits, over):
            self.assertEqual(V.check(res, shape, SHAPES), [])
        self.assertEqual(fits["job"]["status"], "done", fits)
        self.assertEqual((over["job"]["status"], over["job"].get("reason"), over["job"]["error"]), (
            "failed", "prompt_too_long",
            "the request does not fit story's context of 40 tokens: the prompt is 14 tokens as "
            "the model reads it, and its answer needs room for 27 more (max_tokens 27) - 41 in "
            "all. Shorten the prompt, ask for a shorter answer, or give the model a larger "
            "context ([models.story] ctx)"))
        for res, room in ((fits, 26), (over, 27)):
            count = {"prompt_tokens": 14, "room": room, "context": 40}
            self.assertEqual(res["job"]["progress"].get("count"), count)
            rec = Q.request_record(self.tmp / "book", res["job"]["correlation_id"])
            self.assertEqual(rec["samples"]["01"]["count.json"], count)
        self.assertEqual(fits["candidates"][0]["usage"]["prompt_tokens"], 14,
                         "the count is not the number the model read the request as")
        self.assertEqual(len(self.received(got)), 1, "llama-server was asked to answer the "
                                                     "request that does not fit")

    def test_what_a_stopped_server_left_running_is_reported_and_reclaimed_only_when_asked(self):
        self.said = []
        folder = self.tmp / "book"
        folder.mkdir()
        # a server that dies while bound: its folder's lock goes with the
        # process, its record is left open - and its llama-server runs on
        dead = S.Server(backend=self.runner(), log=lambda s: None, **FAST)
        dead.bind({"path": str(folder)})
        dead_backend = dead.backend
        self.addCleanup(dead_backend.close)
        dead_backend({"prompt": "hi"}, {"model": "story", "max_tokens": 10}, FakeRun())
        up = dead_backend.manager.status()
        pid, load = up["pid"], up["load"]
        dead.queue.close()
        # the next server, NOT asked to reclaim: it reports, and leaves it
        heir = S.Server(backend=self.runner(reclaim=False), log=lambda s: None, **FAST)
        self.addCleanup(heir.close)
        heir.bind({"path": str(folder)})
        self.assertTrue(any("left running" in s and str(pid) in s for s in self.said), self.said)
        # CHANGED, both below: asked by the load's id, never its pid alone
        self.assertFalse(TL.gone(pid, load), "reported only - it must still run")
        heir.close()
        # and one that IS: it takes it back, on proof
        self.said.clear()
        taker = S.Server(backend=self.runner(reclaim=True), log=lambda s: None, **FAST)
        self.addCleanup(taker.close)
        taker.bind({"path": str(folder)})
        self.assertTrue(any("reclaimed" in s and str(pid) in s for s in self.said), self.said)
        self.assertTrue(TL.gone(pid, load, within_s=TL.DEATH_S), "the reclaim killed nothing")


TWO = ('[runner]\nbackend = "mock"\nidle_unload_s = 600.0\n\n'
       '[mock]\ntokens_per_second = {tps}\nload_seconds = 0\nunload_seconds = 0\n'
       'answer_seconds = {answer}\nanswer_due_s = 60\n\n'
       '[models.demo]\nlabel = "Demo"\n\n[models.other]\nlabel = "Other"\n\n'
       '[models.ears]\nlabel = "Ears"\nengine = "whisper"\n')


class NativeAnswersAtOnceRows(ServerCase):
    """S17, the owner's ruling of 2026-09-28, behind the queue: answers to
    different models run at the same time - a recording transcribed while the
    text before it is translated - and every one of them is acted on by what
    acts on the card: the card taken back, the card full, the server's stop."""

    bound = NativeServerRows.bound
    ended = NativeServerRows.ended

    def two(self, tps=0, answer=0.0, **kw) -> RunnerBackend:
        path = self.tmp / "two.toml"
        path.write_text(TWO.format(tps=tps, answer=answer), encoding="utf-8")
        return self.adapter(Config.load(path), **kw)

    def up(self, httpd, *models):
        """The session's models, loaded and kept, as an application's session
        keeps them: a job on a model already up answers beside the others."""
        s, out = self.http(httpd.app, "POST", "/api/pipeline/open?wait_s=10",
                           {"models": list(models), "label": "dictation", "keep_s": 0})
        self.assertEqual(s, 200, out)
        self.assertEqual(sorted(out["pipeline"]["up"] if "pipeline" in out else out["up"]),
                         sorted(models), out)

    def at_once(self, a, run_a, run_b, prompt_model="demo", other_model="other"):
        """Two text answers started together on two threads: -> (threads, box)."""
        box = {}

        def job(name, model, run):
            try:
                box[name] = a({"prompt": "Tell me a long story."},
                              {"model": model, "max_tokens": 200}, run)
            except Exception as exc:                        # noqa: BLE001 - read below
                box[name] = exc
        threads = [threading.Thread(target=job, args=("a", prompt_model, run_a), daemon=True),
                   threading.Thread(target=job, args=("b", other_model, run_b), daemon=True)]
        for t in threads:
            t.start()
        deadline = time.monotonic() + 5
        while a.manager.status()["in_use_models"] != sorted([prompt_model, other_model]) \
                or len(a.manager._answers) < 2:
            self.assertLess(time.monotonic(), deadline, "both answers never began")
            time.sleep(0.02)
        return threads, box

    def test_the_runner_answers_the_queues_three_questions(self):
        a = self.two()
        self.assertEqual(a.models_up(), [])
        a({"prompt": "hi"}, {"model": "demo", "max_tokens": 5}, FakeRun())
        self.assertEqual(a.models_up(), ["demo"])
        self.assertEqual((a.loading_for("demo"), a.loading_for("other")), (False, False))
        a.manager.cfg.mock["load_seconds"] = 1.0
        t = threading.Thread(target=a.manager.acquire, args=("other", a.holder), daemon=True)
        t.start()
        deadline = time.monotonic() + 5
        while a.manager.status()["loading"] != "other":
            self.assertLess(time.monotonic(), deadline, "the load never started")
            time.sleep(0.01)
        self.assertEqual((a.loading_for("other"), a.loading_for("demo")), (True, False))
        self.assertEqual(a.models_up(), ["demo"], "a model loading is not up")
        t.join(5)
        self.assertFalse(a._wanted_next("demo"), "nothing asked is nothing wanted")
        a.wants = lambda model: model == "demo"
        self.assertEqual((a._wanted_next("demo"), a._wanted_next("other")), (True, False))

    def test_a_transcription_and_a_translation_answer_at_once_end_to_end(self):
        """The owner's case over HTTP: a recording transcribed (1.5 s, in one
        piece) while a text model translates (15 tokens at 10 a second).
        The request record's own times overlap; health's runner names both
        models while both answer; each answer's timed line names the model
        beside it, and its statistics count one beside it."""
        a = self.two(tps=10, answer=1.5, folder=None, holder=None)
        httpd, folder = self.bound(a)
        self.addCleanup(S.stop, httpd)
        self.up(httpd, "ears", "demo")
        wav = _wav(self.tmp / "take.wav")
        _, heard = self.http(httpd.app, "POST", "/api/jobs",
                             {"kind": "transcribe", "input": {"audio": str(wav), "fields": {}},
                              "gen": {"model": "ears"}})
        _, said = self.http(httpd.app, "POST", "/api/jobs",
                            {"kind": "translate", "input": {"prompt": "Oversett dette."},
                             "gen": {"model": "demo", "max_tokens": 15}})
        seen, deadline = [], time.monotonic() + 10
        while time.monotonic() < deadline and seen != ["demo", "ears"]:
            _, h = self.http(httpd.app, "GET", "/api/health")
            self.assertEqual(V.check(h, health_shape(), SHAPES), [])
            seen = h["runner"]["in_use_models"]
            time.sleep(0.02)
        self.assertEqual(seen, ["demo", "ears"], "health never named both answering")
        spans = {}
        for jid in (heard["job_id"], said["job_id"]):
            job = self.ended(httpd, jid, 15)
            self.assertEqual(job["status"], "done", job.get("error"))
            deadline = time.monotonic() + 5
            while True:
                ev = Q.request_record(folder, job["correlation_id"])["events"]
                if any(e["event"] == "ended" for e in ev) or time.monotonic() > deadline:
                    break
                time.sleep(0.02)
            began = [e["ts"] for e in ev if e["event"] == "attempt_started"]
            ended = [e["ts"] for e in ev if e["event"] == "attempt_ended"]
            spans[jid] = (began[0], ended[-1])
        (a0, a1), (b0, b1) = spans.values()
        self.assertGreater(min(a1, b1) - max(a0, b0), 1.0, spans)
        lines = [e for e in read_events(folder / "runner" / M.STATS_RECORDS_NAME)
                 if e.get("what") == "answer"]
        self.assertEqual(sorted((e["model_key"], e["beside"]) for e in lines),
                         [("demo", ["ears"]), ("ears", ["demo"])])
        kept = json.loads((folder / "runner" / M.STATS_NAME).read_text(encoding="utf-8"))
        self.assertEqual([kept["models"][m]["answer_s"]["beside"] for m in ("demo", "ears")],
                         [[1], [1]])

    def test_the_card_taken_back_ends_every_answer_under_way(self):
        card = WatchedCard()
        a = self.two(tps=10, manager_kw={"card": card})
        threads, box = self.at_once(a, FakeRun(), FakeRun())
        card.lost = "reclaimed"
        card.on_lost("reclaimed", "no heartbeat for 16s")
        for t in threads:
            t.join(5)
        for name in ("a", "b"):
            self.assertIsInstance(box.get(name), Q.GenerationFailed, box)
            self.assertIn("the card was taken back (reclaimed)", str(box[name]))

    def test_the_card_full_ends_every_answer_under_way_each_marked_to_run_again(self):
        card = RecoveringCard()
        a = self.two(tps=10, manager_kw={"card": card})
        threads, box = self.at_once(a, TermsRun(), TermsRun())
        card.on_degraded(dict(FULL))
        for t in threads:
            t.join(5)
        for name in ("a", "b"):
            exc = box.get(name)
            self.assertIsInstance(exc, Q.GenerationFailed, box)
            self.assertEqual(exc.reason, "card_degraded")
            self.assertEqual(exc.card_full["need_mib"], 6554, "not marked to run again")

    def test_the_servers_stop_leaves_every_answer_to_its_next_start(self):
        """H8's stop, asked over HTTP, with two answers under way: answered at
        once, the server gone within its bounds, and both jobs run again, and
        done, by the next server of the folder."""
        a = self.two(tps=4, folder=None, holder=None)
        httpd, folder = self.bound(a)
        self.addCleanup(S.stop, httpd)
        self.up(httpd, "demo", "other")
        jids = [self.http(httpd.app, "POST", "/api/jobs",
                          {"kind": "generate", "input": {"prompt": "Tell me a long story."},
                           "gen": {"model": m, "max_tokens": 200}})[1]["job_id"]
                for m in ("demo", "other")]
        deadline = time.monotonic() + 10
        while a.manager.status()["in_use_models"] != ["demo", "other"]:
            self.assertLess(time.monotonic(), deadline, "both answers never began")
            time.sleep(0.02)
        self.assertEqual(self.http(httpd.app, "POST", "/api/stop")[0], 202)
        self.assertTrue(httpd.stopped.wait(20), "the server did not stop")
        b = self.two(folder=None, holder=None)
        again = S.serve(TL.quiet_port(), backend=b, log=lambda s: None, **FAST)
        self.addCleanup(S.stop, again)
        self.assertEqual(self.http(again.app, "POST", "/api/workspace",
                                   {"path": str(folder)})[0], 200)
        for jid in jids:
            self.assertEqual(self.ended(again, jid, 15)["status"], "done")


class ZZEveryPortItsOwn(unittest.TestCase):
    """Last, by name: every server this suite started listened on a port of
    this copy's block of the llama.cpp suite's range - the range its stand-in
    llama-servers take theirs from - never on port 0 (H6's register 3)."""

    def test_every_server_this_suite_started_listened_on_a_port_of_its_own_block(self):
        first, last = TL.QUIET.block
        stray = [(asked, had) for asked, had in LISTENED if not first <= had <= last]
        self.assertTrue(LISTENED, "no server was recorded listening: the record is not read")
        self.assertEqual(stray, [], f"{len(stray)} of {len(LISTENED)} servers listened outside "
                                    f"this copy's block {first}-{last}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from _suites import report                             # the tally, counted in rows
    report(unittest.main(exit=False, verbosity=1))
