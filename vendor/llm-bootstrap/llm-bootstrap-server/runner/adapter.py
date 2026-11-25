"""The queue's backend: a model manager behind the queue's one seam.

    backend(request, params, run) -> candidate

A generation takes the model the job names (`params["model"]`) under the
SERVER'S actor - the one holder there is, and the only `acquire` in the
server - beside the models already up, streams the request's prompt through
it, reports progress as it goes, and answers with the text. What it does,
and why:

  the request   a prompt - a string, or {"prompt": str}, sent as one user
                message - or a conversation, {"messages": [...]}: a system
                message and the turns before this one, sent as they were
                built, in their order. A conversation is the contract's
                (`shapes/conversation.json`) and is held to it: a role no chat
                template takes, or anything more in it, fails the job in the
                validator's words. Anything else is refused, never turned
                into a prompt by guessing: a model asked to continue a JSON
                dump answers something, and it is not what was meant. Either
                way, before any model is loaded.
  its engine    a text model (llama.cpp) is asked a prompt or a conversation;
                a whisper model is asked audio (PLAN-4 D10): {"audio": <a WAV
                file on this machine>, "fields": {...}}, the contract's
                (`shapes/audio_request.json`) and held to it, every field one
                whisper-server reads (runner/whisper_fields.py) - it ignores a
                field it does not, so a misspelt one is refused here, never
                sent - and `response_format` the engine's own. A request of
                the other kind fails, before any model is loaded.
  one piece     an engine that answers once (a transcription) declares its
                deadline before it is asked (`Backend.answer_deadline_s`),
                said on the job's progress, `due_s`, so the queue's stall
                watch never takes a quiet transcription back before it - and
                then gives its whole answer: the text, and the engine's own
                fields beside it (`transcript`, `audio_s`).
  parameters    every one the job named - the contract's list, llama-server's
                own names - handed on exactly as it was named, False and 0
                included; the queue's own three (`model`, `n`, `retries`)
                never. Where the job names nothing, the model's settings
                (`temperature`, `top_p`, `min_p`, `repeat_penalty`), then the
                bootstrap's (`max_tokens` 600). A job that names no
                temperature runs at its model's own - the queue stamps none -
                and a parameter given as None is unset: it names nothing.
  the count     once its model is up, and before it is asked, the request is
                counted by the model against the context the model says it
                has (`Backend.count`); the prompt and the room its answer
                needs (`answer_room`: its `max_tokens`, never less than one
                token) must fit, or the job fails `prompt_too_long`, in words
                and numbers - never cut to fit. The count goes on the job's
                progress (`count`) and in its record (`count.json`). A model
                that refuses a request longer than its context itself ends
                it the same way.
  progress      reported as the stream moves - thinking counts, because a
                model deliberating at a healthy rate is not a stalled one -
                at most every `report_every_s`, since every report is a write
                of the folder's store. The first report comes at once: it is
                what ends the queue's "still loading" patience. The stream is
                the manager's answer (`ModelManager.answer`): each token and
                the answer's end are the work a coordinator's watch sees move,
                and on a degraded card the answer ends, `card_degraded`.
  thinking      the text is the answer alone; what the model deliberated goes
                in `reasoning`, beside it, never wrapped into it.
  a cancel      the queue's own event, handed to the stream, which ends the
                socket at once. A generation that saw it raises Cancelled.
                One that lands while the model loads gives the load up -
                unless a job that can run next wants the same model, which the
                runner asks the server (`wants(model)`, set by the server:
                whether a queued job whose `after` have landed names it).
  at once       (S17) answers to different models run at the same time, each
                model one at a time: the queue asks which models are up
                (`models_up`) - a job on any other waits while answers run,
                since its load could need their room, and once it has started
                nothing behind it starts until its model is up (S17b): a job
                beside it could hold the model its load must take off - and
                whether a quiet
                job's own model is loading (`loading_for`).
  loading       `loading()` tells the queue's stall watch a model load is under
                way: a load is not a stall.
  the card      a job's terms go with the take its generation makes, by hand
                (`runner.Terms`): an interactive job asks as responsive, a
                batch job as scheduled, with the job's own patience or the
                server's. A job refused the card for a reason of its own - its
                patience ran out, or it would not wait for a busy card; its
                model does not fit the card now (`no_room`) or at all
                (`never_fits`); the card is over-full (`card_degraded`) -
                fails with that reason. S5b: on a card source that says when
                the card has room again, an answer the card's memory broke
                fails MARKED (`card_full`, the card's numbers) for the queue
                to run it again, and the run again is held until the card
                has room, saying so on the job's progress (`waiting for the
                card's memory`) - within the job's patience, counted from
                the first break. The card is held by the pipeline:
                the one an application opened, or the queue's own, opened by
                the job's take and closed once the queue owes no more work
                (`work_pending`, set by the server).

THE IDLE SWEEP LIVES HERE, on a small timer: a model unused for its window is
unloaded, and the lease goes with it. The runner has no loop of its own, and
the queue's worker dies with the folder - so a model left loaded after the
folder was let go would otherwise never come off the card. It cannot fire
mid-generation: every use holds the model from before `acquire` until the
stream has ended (`ModelManager.in_use`).

THE SERVER'S SIDE. The server calls `attach(actor)` once (the holder is the
server's actor id), then `start_session()` - the session the settings open as
the server starts, if they name one - `on_bind(folder)` and `on_unbind()` as
folders come and go - the runner's record lives in `<folder>/runner/` and
follows them without reloading the model - `status()` and
`pipeline_status()` for its health, the pipeline's three routes
(`open_pipeline`, `renew_pipeline`, `close_pipeline`, each answering
(status, body)), and, when it stops, `stopping(reason)` first - nothing more
is loaded, and a load under way is given up at once - then `close()`, which
takes the model off the card and closes the pipeline.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from . import stats as _stats
from .base import (BackendError, Cancelled, GenParams, PromptTooLong, answer_room,
                   as_messages, strip_thinking)
from .manager import ConcurrencyViolation, LeaseHeld, ModelManager, PipelineRefused, Terms
from .whisper import AudioError, audio_seconds

_SERVER = Path(__file__).resolve().parents[1]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))
import jobqueue as Q                                            # noqa: E402  (the queue's own two errors)

_CONTRACT = _SERVER.parent / "llm-bootstrap-contract"
if str(_CONTRACT) not in sys.path:
    sys.path.insert(0, str(_CONTRACT))
import validate as _V                                           # noqa: E402

#: A CONVERSATION IS THE CONTRACT'S, never this file's: the one shape a page,
#: a server and this runner hold a request's messages to
_SHAPES, _ = _V.load(_CONTRACT)
CONVERSATION = {"ref": "conversation"}
#: and what a whisper model is asked: audio, and whisper-server's own fields
AUDIO_REQUEST = {"ref": "audio_request"}
#: the fields of an answer in one piece that are the stream's own, not the
#: engine's: everything else in it goes beside the text
ONE_PIECE_OWN = ("type", "text", "tokens", "finish_reason", "usage", "timings")

#: the bootstrap's own `max_tokens`, where a job names none: the one the queue
#: stamps on every job it stores
MAX_TOKENS = Q.GEN_DEFAULTS["max_tokens"]

#: where the runner's record goes inside a bound folder
RUNNER_DIR = "runner"

#: the parameters a model's own settings fill where a job names none
MODEL_OWN = ("temperature", "top_p", "min_p", "repeat_penalty")
#: a held request's expected time is said once its model has this many of its
#: measure kept - as many as the slow-answer watch's normal needs
EXPECT_SAMPLES = 3


class RunnerBackend:
    def __init__(self, manager: ModelManager, *, reclaim: bool = False,
                 idle_poll_s: float = 5.0, report_every_s: float = 0.5, log=None):
        self.manager = manager
        self.holder: str | None = None
        self.reclaim = bool(reclaim)
        self.report_every_s = float(report_every_s)
        self._log = log or (lambda s: print(s, flush=True))
        # what a person must hear at once - a model that does not fit on the
        # card now (H9, "fail loudly") - the manager says in this server's log
        manager.on_say = self._say
        #: wants(model) -> does a job that can run next name *model*? - asked
        #: when a cancel lands during a load (E3). Set by the server, which
        #: knows its queue; None until then, and then nothing is kept for
        #: anybody. S17: jobs on different models run at once, so the question
        #: is whether ANY job that can run wants the model, never which one job
        #: is next
        self.wants = None
        self._stop = threading.Event()
        self._sweeper = threading.Thread(target=self._sweep, args=(float(idle_poll_s),),
                                         name="runner-idle", daemon=True)
        self._sweeper.start()

    # --------------------------------------------------------- the server's side

    def attach(self, actor: str) -> None:
        """The server whose actor holds the card. One server, one holder."""
        if self.holder is not None and self.holder != actor:
            raise RuntimeError(f"this runner already holds the card for {self.holder}; "
                               f"{actor} cannot share it")
        self.holder = actor

    def on_bind(self, folder) -> dict:
        """A folder was bound: the record follows it, and what a stopped server
        of that folder left running is looked for - reclaimed only if the
        server was started to (`--reclaim`), and only on proof."""
        self.manager.rebind(Path(folder) / RUNNER_DIR)
        found = self.manager.startup_check(reclaim=self.reclaim)
        for o in found["orphans"]:
            what = "reclaimed" if o["pid"] in found["reclaimed"] else (
                "left alone: not provably this runner's" if not o["ours"] else
                "left running: start with --reclaim to take it back")
            self._say(f"runner: a load a stopped server left behind - {o['model_key']} "
                      f"(pid {o['pid']}, port {o['port']}, {o['load']}): {what}")
        return found

    def on_unbind(self) -> None:
        self.manager.rebind(None)

    @property
    def work_pending(self):
        """Does the queue still owe work? Asked by the queue's own pipeline
        before it lets the card go. Set by the server, which knows its queue;
        None: nothing is owed."""
        return self.manager.work_pending

    @work_pending.setter
    def work_pending(self, ask) -> None:
        self.manager.work_pending = ask

    def status(self) -> dict:
        return self.manager.status()

    # ------------------------------------------------------- the pipeline (D6)

    def start_session(self):
        """The session the runner's settings open as the server starts
        (`session_models`): held until it is closed or the server stops -
        nothing to renew - and its models loaded at once. -> the pipeline,
        or None when the settings name none."""
        models = list(self.manager.cfg.runner.get("session_models") or [])
        if not models:
            return None
        return self.manager.open_pipeline(self._actor(), models=models, keep_s=0,
                                          opened_by="start")

    def open_pipeline(self, *, models=(), klass=None, label=None, keep_s=None,
                      wait_s: float = 0.0, if_no_room=None) -> tuple:
        """-> (status, body): the pipeline opened - once its models are up, or
        failed, or *wait_s* has passed - or the route's refusal."""
        try:
            return 200, {"pipeline": self.manager.open_pipeline(
                self._actor(), models=models, klass=klass, label=label, keep_s=keep_s,
                wait_s=wait_s, if_no_room=if_no_room)}
        except PipelineRefused as exc:
            return exc.answer()

    # ------------------------------------------------ scheduled work (S18)

    def room_look(self, want):
        """The queue's look for the scheduled job it waits to run - may it go
        now, and what room does it wait on (`ModelManager.room_look`)."""
        return self.manager.room_look(want, self._actor())

    def settle(self) -> None:
        """The queue's work changed - a job went back to wait for its time or
        for room (S18): the queue's own pipeline is asked at once whether its
        work is done, so nothing holds the card for a job that waits."""
        try:
            self.manager.settle()
        except Exception as exc:                            # noqa: BLE001 - said, never raised
            self._say(f"runner: the queue's own pipeline could not settle: {exc}")

    @property
    def card_patience_s(self) -> float:
        """The server's own patience for the card: a job that names none
        waits this long - counted, for scheduled work, from its time."""
        return self.manager.patience(None)

    def renew_pipeline(self, pipeline_id) -> tuple:
        try:
            return 200, {"pipeline": self.manager.renew_pipeline(pipeline_id)}
        except PipelineRefused as exc:
            return exc.answer()

    def close_pipeline(self, pipeline_id) -> tuple:
        try:
            return 200, {"pipeline": self.manager.close_pipeline(pipeline_id)}
        except PipelineRefused as exc:
            return exc.answer()

    def pipeline_status(self) -> dict:
        """Health's `pipeline`."""
        return self.manager.pipeline_status()

    # ------------------------------------------------ models side by side (D7)

    def load_model(self, key: str, wait_s: float = 0.0) -> tuple:
        """-> (status, body): *key* loaded and kept in the application's open
        pipeline - where it stands once it is up, or failed, or *wait_s* has
        passed - or the route's refusal."""
        try:
            return 200, self.manager.load_model(self._actor(), key, wait_s=wait_s)
        except KeyError as exc:
            return 404, {"error": str(exc).strip("'\""), "reason": "no_model"}
        except PipelineRefused as exc:
            return exc.answer()

    def unload_model(self, key: str) -> tuple:
        """-> (status, body): *key* taken off the card, or the route's
        refusal."""
        try:
            return 200, {"model": self.manager.unload_model(key)}
        except KeyError as exc:
            return 404, {"error": str(exc).strip("'\""), "reason": "no_model"}
        except PipelineRefused as exc:
            return exc.answer()

    def _actor(self) -> str:
        if self.holder is None:
            raise RuntimeError("no server has attached this runner: nobody holds the card")
        return self.holder

    def card_status(self):
        """Where the runner stands on the card - None when nothing
        coordinates it."""
        return self.manager.card_status()

    def models(self) -> dict:
        """Every model the runner's settings name, and its health."""
        return self.manager.models()

    def read_speed(self, key: str) -> tuple:
        """-> (status, body): a read-speed measure, or the route's refusal in
        its own words."""
        from .modelhealth import ModelRefused
        try:
            return 200, self.manager.read_speed(key)
        except KeyError as exc:
            return 404, {"error": str(exc).strip("'\""), "reason": "no_model"}
        except ModelRefused as exc:
            return exc.answer()

    def stopping(self, reason: str) -> None:
        """The server is going away: nothing more is loaded, and a load under
        way is given up at once - the server stops without waiting one out."""
        self.manager.stopping(reason)

    def close(self) -> None:
        """Stop the sweep, then take the model off the card."""
        self._stop.set()
        self._sweeper.join(timeout=5)
        self.manager.shutdown()

    # -------------------------------------------------------------- the seam

    def loading(self) -> bool:
        return self.manager.loading

    # S17: answers to different models run at once - the queue's questions

    def loading_for(self, model) -> bool:
        """Is *model*'s own load under way? A quiet answer beside another
        model's load is not loading - asked by the queue's stall watch."""
        return self.manager.loading_for(str(model or ""))

    def models_up(self) -> list:
        """The models up on the card now, handed out with no load: a job on
        one of these starts beside the answers under way; a job on any other
        waits for them to end, since its load could need their room - and
        while it loads, nothing starts behind it (S17b). Asked by
        the queue as it picks - it reads, and never waits on a load."""
        return self.manager.up_keys()

    def job_share(self, model, seconds) -> None:
        """The queue's own share of a job that named *model* - its time
        outside the model, in *seconds* - kept in that model's statistics
        (PLAN-4 D12). A model the runner does not have keeps nothing."""
        key = str(model or "")
        if key in self.manager.cfg.models:
            self.manager.note_job(key, seconds)
    def check_request(self, request, params) -> None:
        """AT SUBMIT (PLAN-4 D11), asked by the queue when the request is the
        job's input: a job for a WHISPER model is held to the audio request
        whisper-server reads - it ignores a field it does not, so a misspelt
        one would read as one that took effect - and one that is not such a
        request is refused `bad_audio_request`, in the words its turn would
        say, never stored. A text model's request is checked at its turn, as
        ever; a model the settings do not name is left to the turn to say so;
        the WAV itself is read at the turn, where it is transcribed."""
        model = str((params or {}).get("model") or "").strip()
        try:
            spec = self.manager.cfg.model(model)
        except KeyError:
            return None
        if spec.engine != "whisper":
            return None
        if not (isinstance(request, dict) and "audio" in request):
            raise Q.Refused("bad_audio_request", _transcribes(model), 400)
        try:
            _request(request)
        except Q.GenerationFailed as exc:
            raise Q.Refused("bad_audio_request", str(exc), 400) from None
        return None

    def __call__(self, request, params, run) -> dict:
        messages, model, spec = self._asked(request, params)
        gen = _gen(spec, params)
        with self.manager.in_use(model):
            terms = _terms(run)
            backend = self._acquire(model, run, terms, on_full=_full_reporter(run))
            self._check_fits(backend, model, messages, gen, run)
            return self._generate(backend, model, messages, gen, run, terms)

    def _asked(self, request, params):
        """-> (what the model is asked, its key, its settings): the request
        held to its engine's kind - a text model a prompt or a conversation,
        a whisper model audio it can read - before any model is loaded."""
        messages = _request(request)
        model = str(params.get("model") or "").strip()
        try:
            spec = self.manager.cfg.model(model)
        except KeyError as exc:
            raise Q.GenerationFailed(str(exc).strip("'\"")) from None
        _asked_as_its_engine_asks(model, spec.engine, messages)
        if spec.engine == "whisper":
            _audio_heard(messages)
        return messages, model, spec

    def _acquire(self, model, run, terms=None, on_full=None):
        """*model*, taken as a job takes it - its terms for the card, its
        waiting said, a cancel heard, and (S5b, a turn's) the card full said
        while it is held for the card (`on_full`) - or the job's failure, in
        its reason."""
        terms = terms if terms is not None else _terms(run)
        try:
            return self.manager.acquire(
                model, self.holder, for_=getattr(run, "correlation_id", None),
                cancel=run.cancelled, on_wait=_waiting_reporter(run),
                keep_loading=lambda: self._wanted_next(model), terms=terms,
                on_full=on_full)
        except Cancelled as exc:
            raise Q.Cancelled(str(exc) or "cancelled while waiting for the card") from None
        except (BackendError, LeaseHeld, ConcurrencyViolation, ValueError) as exc:
            raise self._failed(exc, f"the model could not be loaded: {exc}",
                               terms.refused or self._reason(exc), terms) from None

    def _failed(self, exc, said: str, reason, terms) -> Q.GenerationFailed:
        """The job's failure, with its reason - and, S5b, when the card's
        memory broke it and it may be held for the card (`holds_while_full`:
        a source that says when the card has room again, and patience left),
        the card's numbers as `card_full`: the queue runs it again, from the
        start, once the card has room - never counted as the model's failure.
        One held for its whole patience already (`final`) is not held again."""
        failed = Q.GenerationFailed(said, reason=reason)
        if reason == "card_degraded" and not getattr(exc, "final", False) \
                and self.manager.holds_while_full(terms):
            failed.card_full = dict(getattr(exc, "card", None) or {})
        if isinstance(getattr(exc, "room", None), dict):
            # S18: its model does not fit, and it waits for room - the queue
            # keeps it, and runs it once `room_look` says it may
            failed.waits_for_room = dict(exc.room)
        return failed


    # ------------------------------------------------ the request check (D16)

    def hold(self, request, params, run, named) -> dict:
        """A HELD REQUEST, AS THE RUNNER SAYS IT (PLAN-4 D16), asked by the
        queue at a marked job's turn, its request built: the parts of the
        contract's `held_request` the queue cannot say.
          - the engine, and what the model is asked: the messages by role (a
            prompt is one user message), or a whisper model's audio - its
            file and its length - and whisper-server's fields;
          - every parameter a text model will be sent, and where it came
            from: the application (`named`: the job's own and its sampling
            hook's), the model's settings (its sampling, and no thinking for
            a model its settings tell not to think), or the bootstrap's
            default (`max_tokens`) - anything no one names is llama-server's;
          - THE COUNT, made NOW, at the job's turn: its model taken as the
            turn takes it - the card asked for, waited on, with the job's
            terms - and counted by the same `_count` the turn's check uses.
            Only a loaded model can count, and the turn would load it next
            anyway; counting on demand, later, would load it while a person
            reads, or never. Once counted the model is let go: it is on
            demand, and idles off on its own clock - a held job holds no
            card. A request that does not fit is held all the same, `fits`
            false: the person may shorten it; sent unchanged, its turn
            counts it again and fails it `prompt_too_long`;
          - what the model's statistics expect (D12): the median and 90th
            percentile of its whole answers, or of its real-time factor times
            the audio's length - with three measured, as the slow-answer
            watch's normal;
          - `said`: why any of these is not known.
        What the turn would fail on - a request the model can never be asked,
        a card refused - fails here, in the same words and reason. The model
        is never asked."""
        messages, model, spec = self._asked(request, params)
        said = []
        if spec.engine == "whisper":
            seconds = audio_seconds(messages["audio"])
            parts = {"engine": "whisper", "messages": None,
                     "audio": {"path": messages["audio"], "seconds": seconds},
                     "fields": dict(messages["fields"]), "params": [], "count": None,
                     "fits": None,
                     # whisper reads none of the job's gen - only its fields
                     # (R-B1): the queue shows none of them
                     "reads": []}
            said.append("a transcription is not counted in tokens: its length is its audio")
        else:
            seconds = None
            gen = _gen(spec, params)
            with self.manager.in_use(model):
                backend = self._acquire(model, run)
                count = self._count(backend, messages, gen, run)
            if count is None:
                said.append(f"{model} cannot count a request: it is asked without a count, "
                            f"as at its turn")
            parts = {"engine": spec.engine, "messages": as_messages(messages), "audio": None,
                     "fields": None, "params": _provenance(spec, params, named),
                     "count": count, "fits": None if count is None else
                     count["prompt_tokens"] + count["room"] <= count["context"]}
        parts["expect"] = self._expect(model, seconds, said)
        parts["said"] = said
        return parts

    def _expect(self, model, seconds, said):
        """What *model*'s statistics expect of an answer (D12): its whole
        answers' median and 90th percentile (`answer_s`), or - *seconds* of
        audio - its real-time factor's, times the audio - once it has
        `EXPECT_SAMPLES` of them. None, said why, before."""
        measure = "answer_s" if seconds is None else "real_time_factor"
        kept = self.manager.kept_stats()
        if kept is None:
            said.append("no expected time: the model's statistics cannot be read")
            return None
        entry = kept.get(model, {}).get(measure)
        have = len(entry["samples"]) if entry else 0
        if have < EXPECT_SAMPLES:
            said.append(f"no expected time yet: {model} has {have} "
                        f"{'answer' if have == 1 else 'answers'} measured, and its statistics "
                        f"need {EXPECT_SAMPLES}")
            return None
        d = _stats.describe(entry)
        times = 1.0 if seconds is None else float(seconds)
        return {"measure": measure, "median_s": round(d["median"] * times, 3),
                "p90_s": round(d["p90"] * times, 3), "samples": int(d["kept"])}

    def check_sent(self, request, params) -> None:
        """A HELD REQUEST SENT CHANGED (PLAN-4 D16) is held to what its
        model can be asked, before it is queued again: a text model a prompt
        or a conversation - anything else refused `bad_request` in the words
        its turn would fail it with - and a whisper model the audio request
        whisper-server reads, as at submit (`bad_audio_request`). A model the
        settings do not name is left to its turn to say so."""
        model = str((params or {}).get("model") or "").strip()
        try:
            spec = self.manager.cfg.model(model)
        except KeyError:
            return None
        if spec.engine == "whisper":
            return self.check_request(request, params)
        try:
            _asked_as_its_engine_asks(model, spec.engine, _request(request))
        except Q.GenerationFailed as exc:
            raise Q.Refused("bad_request", str(exc), 400) from None
        return None

    def _reason(self, exc):
        """The job's own reason for model work that failed, when it has one
        (PLAN-4 D2, D3): the card's memory refused its model - `no_room`, not
        now; `never_fits`, not on this card at all - or the card is degraded,
        `card_degraded`: the failure's own word, or, for model work that
        failed while the card is degraded, the card's."""
        said = getattr(exc, "reason", None)
        if said in ("no_room", "never_fits", "card_degraded"):
            return said
        return "card_degraded" if isinstance(exc, BackendError) and self.manager.degraded \
            else None

    def _count(self, backend, messages, gen, run):
        """The request counted BY its model (`Backend.count`) against the
        context the model says it has, and the room its answer needs - on
        the job's progress - or None for a model that cannot count. A cancel
        during it is a cancel; a count that fails fails the job, never
        skipped."""
        try:
            counted = backend.count(messages, gen, run.cancelled)
        except Cancelled:
            raise Q.Cancelled("cancelled while the model counted the request") from None
        except BackendError as exc:
            why = self.manager.card_lost
            raise Q.GenerationFailed((f"the card was taken back ({why}): " if why else "")
                                     + f"the model could not count the request: {exc}") from None
        if counted is None:
            return None
        count = {"prompt_tokens": int(counted["prompt_tokens"]),
                 "room": answer_room(gen.max_tokens), "context": int(counted["context"])}
        run.report(count=count)
        return count

    def _check_fits(self, backend, model, messages, gen, run) -> None:
        """THE COUNT, at the job's turn: its model up, not yet asked. The
        request is counted BY the model (`Backend.count`: rendered with its
        own template, counted by its own tokenizer) against the context the
        model says it has, and the prompt and the room its answer needs
        (`answer_room`) must fit - or the job fails `prompt_too_long`, in
        words and numbers, and the model is never asked: never cut to fit.
        The count goes on the job's progress and in its record, whether it
        fits or not. A model that cannot count (the stand-in) is asked
        without one.

        The request check's held request (`hold`) reaches the count this
        same way: its model acquired as a job's is, then `_count` - never a
        count of its own."""
        count = self._count(backend, messages, gen, run)
        if count is None:
            return
        _keep(run, "count.json", count)
        if count["prompt_tokens"] + count["room"] > count["context"]:
            raise Q.GenerationFailed(_too_long(model, count, gen.max_tokens),
                                     reason="prompt_too_long")

    def _wanted_next(self, model: str) -> bool:
        """Does a job that can run next name *model* (`wants`)? A question
        nobody can answer is a no: nothing is kept on the card for a job not
        known."""
        ask = self.wants
        if ask is None:
            return False
        try:
            return bool(ask(model))
        except Exception:                                   # noqa: BLE001
            return False

    def _generate(self, backend, model, messages, gen, run, terms=None) -> dict:
        text, reasoning, n = [], [], 0
        finish, usage, timings = "stop", {}, {}
        whole = None
        started = time.monotonic()
        reported = None
        before = backend.last_call
        # an answer in one piece: its deadline, asked of its backend and on
        # the job's progress before the wait - a quiet model before it is
        # answering, never stalled (the backend's seam, `answer_deadline_s`)
        ask = getattr(backend, "answer_deadline_s", None)
        due = ask(messages, gen) if ask is not None else None
        if due is not None:
            run.report(stage="answering", due_s=round(float(due), 1))
        try:
            # the manager's answer is the backend's stream, WATCHED: its tokens
            # and its end are the work's progress (D3), and on a degraded card
            # it ends at once, saying so
            for ev in self.manager.answer(backend, messages, gen, run.cancelled):
                kind = ev.get("type")
                if kind in ("token", "reasoning"):
                    (text if kind == "token" else reasoning).append(ev["text"])
                    n += 1
                    now = time.monotonic()
                    if reported is None or now - reported >= self.report_every_s:
                        reported = now
                        run.report(tokens=n, stage="writing" if kind == "token" else "thinking",
                                   tok_s=round(n / max(now - started, 1e-6), 1))
                elif kind == "done":
                    finish = ev.get("finish_reason") or "stop"
                    usage = ev.get("usage") or {}
                    timings = ev.get("timings") or {}
                    if "text" in ev:
                        whole = ev                          # the answer, in one piece
        except Cancelled:
            raise Q.Cancelled("the runner saw the cancel") from None
        except BackendError as exc:
            # a model taken off because the card was taken back fails saying
            # so - not merely that the model went away. A request the model
            # refused as longer than its context - its own last line of
            # defence, behind the count - ends as one the count refused
            why = self.manager.card_lost
            raise self._failed(exc, (f"the card was taken back ({why}): " if why else "")
                               + str(exc),
                               "prompt_too_long" if isinstance(exc, PromptTooLong)
                               else self._reason(exc), terms) from None
        finally:
            _keep_call(backend, before, run)
        if run.stopped():
            raise Q.Cancelled("cancelled while generating")
        if whole is not None:
            n = int(whole.get("tokens") or 0)
            run.report(tokens=n, stage="done")
            out = {"text": str(whole["text"]), "tokens": n, "finish_reason": finish,
                   "model": model, "seed": gen.seed, "load": backend.load_id, "usage": usage,
                   "timings": timings}
            out.update({k: v for k, v in whole.items() if k not in ONE_PIECE_OWN})
            return out
        answer = strip_thinking("".join(text))
        run.report(tokens=n, stage="done")
        out = {"text": answer, "tokens": int(usage.get("completion_tokens") or n),
               "finish_reason": finish, "model": model, "seed": gen.seed,
               "load": backend.load_id, "usage": usage, "timings": timings}
        if reasoning:
            out["reasoning"] = "".join(reasoning)
        return out

    # ---------------------------------------------------------- the idle sweep

    def _sweep(self, every_s: float) -> None:
        """Each tick: a model idle past its window comes off - which lets the
        queue's own pipeline close, its work done - and a pipeline its
        application stopped renewing lapses."""
        while not self._stop.wait(every_s):
            for what in ("maybe_idle_unload", "maybe_lapse"):
                try:
                    getattr(self.manager, what)()
                except Exception as exc:                    # noqa: BLE001 - the sweep must live
                    self._say(f"runner: the idle sweep failed ({what}): {exc}")

    def _say(self, line: str) -> None:
        try:
            self._log(line)
        except Exception:                                   # noqa: BLE001
            pass


def _gen(spec, params) -> GenParams:
    """What the model is sent: every parameter the job named, as it named it
    - None names nothing - and never the queue's own three; where it named
    none, the model's settings, then the bootstrap's `max_tokens`."""
    named = {k: v for k, v in params.items() if v is not None and k not in Q.GEN_OWN}
    return GenParams(
        temperature=float(named.pop("temperature", spec.temperature)),
        top_p=named.pop("top_p", spec.top_p),
        min_p=named.pop("min_p", spec.min_p),
        repeat_penalty=named.pop("repeat_penalty", spec.repeat_penalty),
        seed=named.pop("seed", None),
        max_tokens=int(named.pop("max_tokens", MAX_TOKENS)),
        chat_template_kwargs=dict(named.pop("chat_template_kwargs", {})),
        extra=named)


def _provenance(spec, params, named) -> list:
    """Every parameter a text model will be sent, as `_gen` and the engine
    fill it, each with where it came from (PLAN-4 D15, D16): the job's own,
    each as the queue hands it - the application's where it named it, else
    the bootstrap's (the queue's length, stamped on every job) - then the
    model's settings where the job named none of them, and its thinking
    switched off where its settings say and the job did not."""
    named = set(named)
    out = [{"name": k, "value": v, "from": "application" if k in named else "bootstrap"}
           for k, v in sorted(params.items()) if v is not None and k not in Q.GEN_OWN]
    out += [{"name": k, "value": getattr(spec, k), "from": "model"}
            for k in MODEL_OWN if params.get(k) is None]
    if spec.disable_thinking and "enable_thinking" not in (params.get("chat_template_kwargs")
                                                           or {}):
        out.append({"name": "chat_template_kwargs.enable_thinking", "value": False,
                    "from": "model"})
    return out


def _terms(run) -> Terms:
    """The job's terms for the card, handed to the take its acquire makes BY
    HAND: an interactive job asks as responsive, a batch job as scheduled - a
    run that names no priority, as the card's default - with the job's own
    patience, or the server's. A card source that has no classes (the
    machine's lock, the stand-in) reads what it can of them; one refused for
    a reason of the job's own says which on them (`refused`)."""
    klass = {"interactive": "responsive", "batch": "scheduled"}.get(getattr(run, "priority", None))
    # S18: a job's own word on waiting for room (None: the server's), and a
    # job WAITS FOR ROOM IN ITS QUEUE - never on its thread holding the card
    return Terms(klass=klass, patience_s=getattr(run, "card_patience_s", None),
                 waited_s=getattr(run, "card_waited_s", None),
                 if_no_room=getattr(run, "if_no_room", None), parks=klass == "scheduled")


def _full_reporter(run, every_s: float = 5.0):
    """What a job says while it is held for the card's memory (S5b): the
    words a person acts on - the card is full, how much is free and needed,
    how much programs outside the bootstrap use, and that it runs again by
    itself - and the numbers: how long it has waited and how much patience is
    left. Reported when the words change, and at most every `every_s`
    otherwise; the seconds waited move, so the stall watch sees a job that
    waits, never one stuck."""
    last = {"words": None, "at": None}

    def report(said: dict) -> None:
        now = time.monotonic()
        if said["words"] == last["words"] and last["at"] is not None \
                and now - last["at"] < every_s:
            return
        last.update(words=said["words"], at=now)
        run.report(stage="waiting for the card's memory", waiting_for=said["words"],
                   free_mib=said["free_mib"], need_mib=said["need_mib"],
                   outside_mib=said["outside_mib"], waited_s=said["waited_s"],
                   patience_left_s=said["patience_left_s"])
    return report


def _waiting_reporter(run, every_s: float = 5.0):
    """What a job says while its model waits for the card: the card source's
    own words - with a coordinator, only that it waits and since when, for the
    coordinator names no other application - and how long it has waited,
    reported when either changes, at most every `every_s`, since each report
    is a write of the folder's store. The wait is bounded by the card's
    patience, not by the stall watch."""
    last = {"text": None, "at": None}
    started = time.monotonic()

    def report(text: str) -> None:
        now = time.monotonic()
        if text == last["text"] and last["at"] is not None and now - last["at"] < every_s:
            return
        last.update(text=text, at=now)
        run.report(stage="waiting for the card", waiting_for=text,
                   waited_s=round(now - started))
    return report


def _keep_call(backend, before, run) -> None:
    """The exact call, in the request's record (`call.json` beside the queue's
    own files for this attempt) - whether the stream answered or not, and
    never a call this generation did not make: a stream that failed before
    it made one leaves the last generation's call where it was, and that one
    is not this attempt's; and a generation beside it on the same model makes
    its own - the call is the one this thread's stream made (`take_call`).
    The key is not in it: it travels in a header."""
    take = getattr(backend, "take_call", None)
    call = take() if take is not None else backend.last_call
    if call is None or call is before:
        return
    _keep(run, "call.json", call)


def _keep(run, name: str, data) -> None:
    """Something of this attempt's, in the request's record, beside the
    queue's own files - never failing the generation it records."""
    keep = getattr(run, "record", None)
    if keep is None:
        return
    try:
        keep(name, data)
    except Exception:                                       # noqa: BLE001 - a record never fails
        pass


def _too_long(model: str, count: dict, max_tokens: int) -> str:
    """A request that does not fit its model's context, in words and
    numbers: the prompt's tokens, the room its answer needs and why, the
    context - and what would make it fit."""
    p, r, c = count["prompt_tokens"], count["room"], count["context"]
    why = (f"max_tokens {max_tokens}" if max_tokens > 0 else
           "max_tokens 0 writes nothing, and the prompt must still leave one" if max_tokens == 0
           else f"max_tokens {max_tokens} names no limit: room to begin")
    shorter = ", ask for a shorter answer," if r > 1 else ","
    return (f"the request does not fit {model}'s context of {c:,} tokens: the prompt is {p:,} "
            f"tokens as the model reads it, and its answer needs room for {r:,} more ({why}) - "
            f"{p + r:,} in all. Shorten the prompt{shorter} or give the model a larger context "
            f"([models.{model}] ctx)")


def _request(request):
    """What the model is asked: a prompt (a string, one user message), a
    conversation's messages (a list, as they were built) - or, for a whisper
    model, audio ({"audio", "fields"}, a copy). A request with `messages` is a
    conversation and nothing else: it is held to the contract's shape whole,
    so a misspelt role - which a chat template would render as it came, or
    drop - is refused, and so is a prompt beside it. One with `audio` is held
    to the audio request's the same way."""
    if isinstance(request, str):
        return request
    if isinstance(request, dict) and "messages" in request:
        wrong = _V.check(request, CONVERSATION, _SHAPES)
        if wrong:
            raise Q.GenerationFailed(f"the request is not a conversation: {wrong[0]}")
        return request["messages"]
    if isinstance(request, dict) and "audio" in request:
        fields = request.get("fields")
        if isinstance(fields, dict) and "response_format" in fields:
            # before the shape, which would only say the field is unexpected
            raise Q.GenerationFailed("response_format is the engine's own: it asks "
                                     "whisper-server for the verbose answer, and every field of "
                                     "that answer is kept")
        wrong = _V.check(request, AUDIO_REQUEST, _SHAPES)
        if wrong:
            raise Q.GenerationFailed(f"the request is not an audio request: {wrong[0]}")
        return {"audio": request["audio"], "fields": dict(request.get("fields") or {})}
    if isinstance(request, dict) and isinstance(request.get("prompt"), str):
        return request["prompt"]
    raise Q.GenerationFailed('a runner generation needs a prompt or a conversation - or, for a '
                             'whisper model, audio: the request is a string, {"prompt": "..."}, '
                             '{"messages": [...]} or {"audio": "..."}, and this one is none of '
                             'them')


def _transcribes(model: str) -> str:
    """What a whisper model is asked, said to a request that is not it."""
    return (f'{model} transcribes audio: its request is {{"audio": <a WAV file on this '
            f'machine>, "fields": {{...}}}}, and this one is not one')


def _asked_as_its_engine_asks(model: str, engine: str, asked) -> None:
    """The request is of the kind its model's engine answers (PLAN-4 D10): a
    text model is asked a prompt or a conversation, a whisper model audio -
    held to the contract's audio request by `_request`, every field one
    whisper-server reads, of its kind, and `response_format` the engine's
    own. Refused before any model is loaded."""
    heard = isinstance(asked, dict)
    if engine == "whisper" and not heard:
        raise Q.GenerationFailed(_transcribes(model))
    if engine != "whisper" and heard:
        raise Q.GenerationFailed(f"{model} answers text: its request is a prompt or a "
                                 f"conversation, and this one is audio")


def _audio_heard(asked) -> None:
    """The audio of a transcription is a WAV this machine can read, and its
    length can be told - what its answer's deadline is reckoned from - or the
    job fails here, before any model is loaded, in words."""
    try:
        audio_seconds(asked["audio"])
    except AudioError as exc:
        raise Q.GenerationFailed(f"the audio cannot be transcribed: {exc}") from None
