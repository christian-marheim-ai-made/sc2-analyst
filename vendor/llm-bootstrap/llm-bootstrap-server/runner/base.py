"""What every model backend is: the contract the mock and each engine share.

ONE CALL, TWO KINDS (PLAN-4 D10). A backend answers through `stream`, and
says which kind of answer it gives (`Backend.answers`):

    "stream"   token by token - llama.cpp's: `token` and `reasoning` events,
               then one `done`. Each token is movement a stall watch sees.
    "once"     the whole answer in one piece - whisper's transcription: one
               `done` carrying the whole answer, its `text` and the engine's
               own fields beside it. Nothing moves before it, so before each
               answer the backend declares the deadline it is due by -
               `answer_deadline_s`, THE SEAM, asked before each answer - and
               before it a quiet model is answering, never hanging: the
               queue's stall watch reads it from the job's progress (`due_s`,
               which the runner's adapter reports), the coordinator's hang
               check from the heartbeat (PLAN-4 D3).

"The model returned nothing" is the text engine's rule: a transcript of
silence - no words, no text - is an answer.

A GIVE-UP ENDS A START AT ONCE. `abandon` must end a `start` under way within
`GIVE_UP_S`, whatever the engine: a pipeline's close waits at most
`ModelManager.CLOSE_JOIN_S` for a load under way to give up (S7 R3), and a
card given back under a load still going up is a card given back twice. Every
engine is held to it by a row against its stand-in.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator

THINK_RE = re.compile(r"<think\b[^>]*>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
OPEN_THINK_RE = re.compile(r"<think\b[^>]*>.*\Z", re.DOTALL | re.IGNORECASE)

#: HOW SOON A GIVE-UP ENDS A START: `abandon` ends a `start` under way within
#: this, whatever the engine - well inside the `ModelManager.CLOSE_JOIN_S` a
#: pipeline's close waits for a load under way to give up
GIVE_UP_S = 5.0


class BackendError(RuntimeError):
    """A load or a generation failed, and says why."""


class Cancelled(RuntimeError):
    """A generation was asked to stop, and did."""


class PromptTooLong(BackendError):
    """The model refused a request longer than its context - its own last
    line of defence, behind the count the runner makes before it asks (the
    job then ends `prompt_too_long`, as a count that does not fit ends it)."""


@dataclass
class GenParams:
    """The parameters half of a request. Only what llama-server accepts, spelled
    the way it spells it: `repeat_penalty`, never `repetition_penalty`.

    The fields are what the model's own settings - or the bootstrap - fill in
    where a job names nothing. `extra` is every other parameter the job named,
    as it named it: it goes into the body unchanged, False and 0 included."""
    temperature: float = 0.8
    top_p: float | None = None
    min_p: float | None = None
    repeat_penalty: float | None = None
    seed: int | None = None
    #: the bootstrap's own length, where a job names none: the queue's
    #: (`jobqueue.GEN_DEFAULTS`), one number in two places, held equal by a row
    max_tokens: int = 600
    chat_template_kwargs: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    def as_body(self) -> dict:
        body: dict = {"temperature": self.temperature, "max_tokens": self.max_tokens}
        for name in ("top_p", "min_p", "repeat_penalty", "seed"):
            value = getattr(self, name)
            if value is not None:
                body[name] = value
        if self.chat_template_kwargs:
            body["chat_template_kwargs"] = dict(self.chat_template_kwargs)
        body.update(json.loads(json.dumps(self.extra)))
        return body


def answer_room(max_tokens) -> int:
    """The room a request's answer needs in the model's context, in tokens:
    its `max_tokens`, and never less than one. A request fits when its
    prompt and this room are no more than the context - the most llama-server
    lets an answer have (b10724, without --context-shift: an answer stops
    when the prompt and it fill the context).

      -1  names no limit: the answer needs room to BEGIN, and may run to the
          end of the context, where llama-server stops it ("length"). Any
          larger room would be a limit the application declined to set, and
          would refuse a request that could have been answered in full.
       0  writes nothing, but the prompt must still leave the position after
          it: llama-server refuses a prompt of the whole context (b10724:
          `exceed_context_size_error` at n_prompt >= n_ctx)."""
    return max(1, int(max_tokens))


def as_messages(request) -> list:
    """What a request sends as its `messages`: a conversation as it was built
    - a system message and the turns before this one, in their order - or a
    string, which is one user message, as a prompt always was. A copy: the
    record of a call never shares a list with the application."""
    if isinstance(request, str):
        return [{"role": "user", "content": request}]
    return json.loads(json.dumps(list(request)))


def strip_thinking(text: str) -> str:
    """Remove a reasoning model's <think> blocks, including one left open by a
    stream that was cut short. The last line of defence: thinking is also
    switched off at launch and per request when a model is told to."""
    out = THINK_RE.sub("", text)
    out = OPEN_THINK_RE.sub("", out)
    return out.lstrip()


class Backend:
    """One loaded model. Made by the manager, used, then stopped.

    `stream` takes a conversation - or a string, one user message (see
    `as_messages`) - and yields {"type": "token" | "reasoning", "text"} and
    finally one {"type": "done", "finish_reason", "usage", "timings"}; it
    raises BackendError on failure and Cancelled when `cancel` is set. An
    engine that answers in one piece takes its own request (a transcription:
    {"audio", "fields"}), declares its deadline (`answer_deadline_s`), and
    yields its whole answer in one `done` (see the module's docstring)."""

    kind = "base"
    #: how this backend answers: "stream" - token by token - or "once" - the
    #: whole answer in one piece, due by the deadline it declares first
    answers = "stream"

    def __init__(self, model_key: str, label: str = ""):
        self.model_key = model_key
        self.label = label or model_key
        self.pid: int | None = None
        self.port: int | None = None
        #: this load's own actor id (`load-xxxxxxxx`), given by the manager
        #: before `start()`: what the events file and a startup check know
        #: this load by
        self.load_id: str | None = None
        #: THE STEPS OF ITS START, as its engine saw each happen (PLAN-4 D12):
        #: "spawn" - its process exists; "port" - its port answers; "healthy"
        #: - its model is up; "identity" - it proved to be this load. Each ->
        #: the monotonic time it was marked (`mark`); a step an engine cannot
        #: tell apart from another is not marked. The manager times the start
        #: from them: each step from the one before it
        self.steps: dict = {}

    def mark(self, step: str) -> None:
        """A step of this load's start has happened, now (`steps`)."""
        self.steps[step] = time.monotonic()

    # THE CALL A GENERATION MADE is its own, never another's: two generations
    # on one model - side by side, on two threads - each keep the call they
    # made (`take_call`), while `last_call` is the last any made. A stream
    # runs on the thread that reads it, so the thread says whose call it is.

    @property
    def last_call(self):
        """The exact request body of the last generation on this backend, on
        any thread - the API call."""
        return self.__dict__.get("_last_call")

    @last_call.setter
    def last_call(self, call) -> None:
        self.__dict__["_last_call"] = call
        self._calls().call = call

    def take_call(self):
        """The call the generation on THIS thread made since the last take -
        its own, for its request record - and forget it: None when it made
        none (a stream that failed before its request)."""
        calls = self._calls()
        call, calls.call = getattr(calls, "call", None), None
        return call

    def _calls(self) -> threading.local:
        calls = self.__dict__.get("_thread_calls")
        if calls is None:
            calls = self.__dict__.setdefault("_thread_calls", threading.local())
        return calls


    def start(self) -> None:                                # pragma: no cover
        raise NotImplementedError

    def stop(self) -> None:                                 # pragma: no cover
        raise NotImplementedError

    def stream(self, messages, params: GenParams,
               cancel: threading.Event | None = None) -> Iterator[dict]:  # pragma: no cover
        raise NotImplementedError

    def count(self, messages, params: GenParams,
              cancel: threading.Event | None = None) -> dict | None:
        """The request as this model will read it, counted BY the model:
        rendered with its own chat template - the one `stream` would send, to
        the byte - and counted by its own tokenizer; and the context it
        answers in, as the model itself says. Asked once it is loaded, before
        it is asked to answer.

        -> {"prompt_tokens": int, "context": int}, or None: this backend
        cannot count (the stand-in model), and nothing is checked before it
        is asked. Raises BackendError when the model will not count the
        request - it would not answer it either - and Cancelled when
        `cancel` is set."""
        return None

    def answer_deadline_s(self, messages, params: GenParams) -> float | None:
        """THE SEAM FOR AN ANSWER THAT COMES IN ONE PIECE (PLAN-4 D3, D10). A
        model whose answer streams moves as it goes - each token is progress
        the coordinator's watch sees. One that answers ONCE - whisper's
        transcript - moves nothing until it is done, so it declares how long
        this request may take, in seconds from when it is asked: the watch
        does not call its rental hung before then. -> the seconds, or None
        (the default): the answer streams. Asked before each answer; an
        engine that answers in one piece fills it (S8, S12b)."""
        return None

    def memory_errors(self) -> list:
        """The lines of THIS LOAD'S own log, since it was last asked, that say
        the card's memory ran out - each line once, in the log's own words
        (`runner/memlog.py` names llama.cpp's). The runner reports them and
        the coordinator marks the rental degraded. [] (the default): this
        backend keeps no such log. Asked after each answer ends and after a
        load fails - cheap: only what was written since."""
        return []

    def is_alive(self) -> bool:
        """Can this backend still serve? Asked before a warm one is handed out.

        TRUE BY DEFAULT, and that is the safe direction: a backend with nothing
        to die (the mock) cannot be dead, and one that cannot tell must never
        report a working model as a corpse. A wrong TRUE costs one failed
        generation with a clear error; a wrong FALSE costs a reload of the
        whole model. It must be cheap: no network, no blocking."""
        return True

    def describe(self) -> str:
        return self.label
