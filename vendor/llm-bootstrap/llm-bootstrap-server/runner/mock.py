"""The stand-in model: no weights, no GPU, no network.

It exists so everything around a model - loading, the one-at-a-time rule, the
queue, streaming, cancel, a failing generation, the events file - can be run
and proven with nothing installed. It behaves like a model where that matters:

  * a LOAD takes `load_seconds` - unless it is given up (`abandon`), which
    ends it at once - and an unload `unload_seconds`;
  * tokens arrive at `tokens_per_second`, and `max_tokens` cuts the reply
    short with finish reason `length` - read as llama-server reads it: -1
    names no limit, and 0 writes nothing;
  * every one of those times is AT LEAST that long, by the monotonic clock
    (`_wait_out`): a timed wait can end before its time;
  * the reply is DETERMINISTIC in the prompt, the model, the seed and the
    configured seed - the same request gives the same text - and different
    models answer differently;
  * a request is a prompt - one user message - or a conversation, and the
    reply is drawn from all of it: a system message or an earlier turn
    changes it, as it would a model's, and a conversation of one user
    message answers as that prompt does;
  * a model named in `fail_models` always fails, `fail_first_for` fails once
    per process, and a prompt containing `[[MOCK_FAIL]]` fails;
  * a model named in `fail_load_models` fails every LOAD, once its load time
    is waited out - so a model's `failed` state, and why, can be shown by the
    stand-in server from its command line;
  * a prompt containing `[[MOCK_THINK]]` makes it think first - reasoning
    tokens, and a <think> block in the text - so the thinking path runs too;
  * it COUNTS a request as it answers it (S16a R1): each word a token,
    against the `ctx` its model's settings give - so a request past it
    fails `prompt_too_long` on the stand-in too, before it is asked.

IT ANSWERS IN ITS MODEL'S ENGINE'S SHAPE (PLAN-4 D10). A llama.cpp model's
answer is the stream above. A whisper model's is a transcription, in one
piece: its request is {"audio": <a WAV file on this machine>, "fields":
{...}}; it declares its deadline before it is asked (`answer_deadline_s`:
`answer_due_s`), takes `answer_seconds` - a cancel ends it - and answers
whole: `text`, `tokens`, `finish_reason`,
the transcript in the verbose shape whisper-server 1.9.1 was measured to
answer in (the whisper stand-in server's, `fake_whisper_server.py`: its fixed
words laid over the audio, the language named as the server names it, the
language table unless `no_language_probabilities` is true as the server reads
true), and `audio_s`. Silence is heard as silence: no words, no text - an
answer all the same.
"""
from __future__ import annotations

import hashlib
import random
import re
import threading
import time
import wave
from typing import Iterator

from .base import Backend, BackendError, Cancelled, GenParams, as_messages
from .engines import ENGINES
from .whisper_fields import as_form

TOKEN_RE = re.compile(r"\S+\s*")
FAIL_MARK, THINK_MARK = "[[MOCK_FAIL]]", "[[MOCK_THINK]]"
_FAILED_ONCE: set = set()           # models that have spent their one failure, process-wide

WORDS = ("the", "a", "light", "window", "morning", "river", "stone", "letter", "road",
         "voice", "held", "turned", "waited", "opened", "carried", "slowly", "again",
         "before", "after", "still", "quiet", "long", "small", "old", "open", "under",
         "across", "into", "never", "once")

#: WHAT THE STAND-IN HEARS in any sound: the whisper stand-in server's words
HEARD = (" Rain on the tin roof again,", " a letter came through the open window.")
#: whisper's own names for the languages the stand-in names; any other code is
#: answered as it was given
LANGUAGE_NAMES = {"en": "english", "no": "norwegian", "nn": "nynorsk", "sv": "swedish",
                  "da": "danish", "de": "german"}
LANGUAGE_TABLE = {"en": 0.9, "no": 0.05, "de": 0.02}
#: a field is true, to whisper-server, for these words alone
TRUE_WORDS = ("true", "1", "yes", "y")


class StandInCard:
    """THE STAND-IN'S CARD (S16a R2): `[mock] card_mib` MiB, read by the
    stand-in alone - never the real card - so an application on the
    stand-ins can try its card-full path. `card_full_s` [after_s, for_s]:
    counted from when it was made (the server's start), it reads full - no
    MiB free - for for_s seconds after after_s, then free again. The runner
    watches it as it watches a real card on the machine's lock (S5b), so the
    card read full is the same degraded and recovered path, in the same
    words. -> `read()`: {"free", "total"} in MiB, as the runner reads a
    card."""

    def __init__(self, cfg_mock: dict, clock=time.monotonic):
        self.total = int(cfg_mock.get("card_mib") or 0)
        full = list(cfg_mock.get("card_full_s") or [])
        self.full_from, self.full_for = (float(full[0]), float(full[1])) if full else (None, 0.0)
        self._clock = clock
        self._started = clock()
        #: S18: what the stand-in's own models on it hold, in MiB - a function
        #: its runner hands in (`[mock] need_mib`); None: they hold nothing
        self.held = None

    def full(self) -> bool:
        """Does it read full now?"""
        if self.full_from is None:
            return False
        at = self._clock() - self._started
        return self.full_from <= at < self.full_from + self.full_for

    def read(self) -> dict:
        held = int(self.held() or 0) if self.held is not None else 0
        return {"free": 0 if self.full() else max(0, self.total - held), "total": self.total}


def _wait_out(seconds: float, given_up: threading.Event | None = None) -> bool:
    """Wait `seconds` - AT LEAST that long, by the monotonic clock - or until
    `given_up` is set. -> whether it was given up.

    A timed wait can end before its time: measured on this machine, the first
    timed `Event.wait(0.05)` of a fresh process ended under 50 ms in 34 of 200
    processes, and a stand-in load of 0.05 s was timed at 0.04. So the wait is
    on a deadline, and one that ends early is waited again for the rest."""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        left = deadline - time.monotonic()
        if given_up is not None:
            if given_up.wait(max(0.0, left)):
                return True
        elif left > 0:
            time.sleep(left)
        if time.monotonic() >= deadline:
            return False


def said(messages: list) -> str:
    """All a request says, as one text: what the reply is drawn from and its
    markers are looked for in. One user message is its words alone - a
    prompt, as it always was; a conversation is every turn, with its role."""
    if len(messages) == 1 and messages[0].get("role") == "user":
        return str(messages[0].get("content", ""))
    return "\n".join(f"{m.get('role')}: {m.get('content', '')}" for m in messages)


def audio_of(path: str):
    """A PCM WAV on this machine -> (its seconds, whether any sample is not
    silence), or None for anything that is not one it can read."""
    try:
        with wave.open(str(path), "rb") as w:
            seconds = w.getnframes() / w.getframerate()
            zero = b"\x80" if w.getsampwidth() == 1 else b"\x00"   # 8-bit PCM is unsigned
            return seconds, bool(w.readframes(w.getnframes()).strip(zero))
    except Exception:                                   # noqa: BLE001 - unreadable, whatever it is
        return None


def transcript(seconds: float, sound: bool, fields: dict) -> dict:
    """What the stand-in hears, in whisper-server's verbose shape: its words
    laid over the audio - segments sharing its length, words sharing their
    segment, in hundredths of a second as whisper counts them - or, in
    silence, none. *fields* as whisper-server reads them from its form."""
    segments = []
    for i, text in enumerate(HEARD if sound else ()):
        n = len(HEARD)
        a, b = round(seconds * 100 * i / n), round(seconds * 100 * (i + 1) / n)
        words = [" " + w for w in text.split()]
        k = len(words)
        segments.append({
            "id": i, "text": text, "start": a / 100, "end": b / 100,
            "tokens": [100 * (i + 1) + j for j in range(k)],
            "words": [{"word": w, "start": (a + (b - a) * j // k) / 100,
                       "end": (a + (b - a) * (j + 1) // k) / 100, "t_dtw": -1,
                       "probability": 0.9} for j, w in enumerate(words)],
            "temperature": 0.0, "avg_logprob": -0.2, "no_speech_prob": 0.01})
    language = fields.get("language", "en")
    doc = {"task": "transcribe",
           "language": "english" if language == "auto" else LANGUAGE_NAMES.get(language, language),
           "duration": seconds, "text": "".join(s["text"] + "\n" for s in segments),
           "segments": segments}
    if fields.get("no_language_probabilities") not in TRUE_WORDS:
        doc["detected_language"] = "english"
        doc["detected_language_probability"] = LANGUAGE_TABLE["en"]
        doc["language_probabilities"] = dict(LANGUAGE_TABLE)
    return doc


class MockBackend(Backend):
    kind = "mock"

    def __init__(self, model_key: str, label: str, cfg_mock: dict, engine: str = "llamacpp",
                 ctx: int = 8192):
        super().__init__(model_key, label)
        self.cfg = cfg_mock
        #: the engine whose shape it answers in
        self.engine = engine
        #: the context its settings give the model (`ctx`), which it counts
        #: a request against (S16a R1)
        self.ctx = int(ctx)
        self.tps = float(cfg_mock.get("tokens_per_second") or 0.0)
        self.load_seconds = float(cfg_mock.get("load_seconds") or 0.0)
        self.unload_seconds = float(cfg_mock.get("unload_seconds") or 0.0)
        self.answer_seconds = float(cfg_mock.get("answer_seconds") or 0.0)
        self.answer_due_s = float(cfg_mock.get("answer_due_s") or 0.0)
        self.base_seed = int(cfg_mock.get("seed") or 0)
        self.started = False
        #: stopped after it was started: a model taken off the card mid-answer
        #: cannot go on answering
        self.taken_off = False
        self._given_up = threading.Event()
        self._why: str | None = None

    def describe(self) -> str:
        return f"{self.label} (stand-in)"

    @property
    def answers(self) -> str:
        """As its engine answers: token by token, or once."""
        return ENGINES[self.engine].answers

    def answer_deadline_s(self, messages, params: GenParams) -> float | None:
        """An answer in one piece is due within the stand-in's `answer_due_s`;
        one that streams declares none."""
        return self.answer_due_s if self.answers == "once" else None

    def start(self) -> None:
        if _wait_out(self.load_seconds, self._given_up):
            raise BackendError(f"the load was given up: {self._why}")
        if self.model_key in set(self.cfg.get("fail_load_models") or []):
            raise BackendError(f"the stand-in failed the load of {self.model_key}, as its "
                               f"settings ask ([mock] fail_load_models)")
        self.started, self.taken_off = True, False

    def abandon(self, reason: str) -> None:
        """Give the load up: a `start` still waiting out its load time fails at
        once, in these words."""
        self._why = str(reason)
        self._given_up.set()

    def stop(self) -> None:
        _wait_out(self.unload_seconds)
        self.started, self.taken_off = False, True

    def _rng(self, message: str, params: GenParams) -> random.Random:
        digest = hashlib.sha256(
            f"{self.base_seed}|{self.model_key}|{params.seed}|{message}".encode("utf-8"))
        return random.Random(int(digest.hexdigest()[:16], 16))

    def compose(self, message: str, params: GenParams) -> str:
        """The whole reply this request gets - the same every time it is asked."""
        rng = self._rng(message, params)
        sentences = []
        for _ in range(rng.randint(3, 7)):
            words = [rng.choice(WORDS) for _ in range(rng.randint(5, 12))]
            sentences.append(" ".join(words).capitalize() + ".")
        head = f"(stand-in {self.model_key}: no model was involved)\n\n"
        return head + " ".join(sentences)

    def _should_fail(self, message: str) -> bool:
        if self.model_key in set(self.cfg.get("fail_models") or []) or FAIL_MARK in message:
            return True
        once = str(self.cfg.get("fail_first_for") or "")
        if once and once == self.model_key and once not in _FAILED_ONCE:
            _FAILED_ONCE.add(once)
            return True
        return False

    def _transcribe(self, request, cancel) -> Iterator[dict]:
        """A whisper model's answer: in one piece, due by the deadline it
        declared before it was asked (`answer_deadline_s`)."""
        if not (isinstance(request, dict) and isinstance(request.get("audio"), str)):
            raise BackendError(f'{self.model_key} transcribes audio: its request is {{"audio": '
                               f'<a WAV file on this machine>, "fields": {{...}}}}')
        fields = {k: as_form(v) for k, v in dict(request.get("fields") or {}).items()}
        # the call as whisper-server is asked: the audio, by its path, and
        # every field - the verbose answer, which is the engine's to ask for
        self.last_call = {"model": self.model_key, "audio": request["audio"],
                          "fields": {**fields, "response_format": "verbose_json"}}
        if self._should_fail(""):
            raise BackendError(f"the stand-in failed on purpose for {self.model_key}: "
                               f"CUDA error: out of memory (simulated)")
        heard = audio_of(request["audio"])
        if heard is None:
            raise BackendError(f"the stand-in reads a PCM WAV, and {request['audio']} is none "
                               f"it could read")
        seconds, sound = heard
        if _wait_out(self.answer_seconds, cancel):
            raise Cancelled("cancelled while transcribing")
        if self.taken_off:
            raise BackendError(f"{self.model_key} was taken off the card mid-answer")
        doc = transcript(seconds, sound, fields)
        yield {"type": "done", "finish_reason": "stop", "text": doc["text"],
               "tokens": sum(len(s["tokens"]) for s in doc["segments"]),
               "transcript": doc, "audio_s": seconds}

    def count(self, messages, params: GenParams,
              cancel: threading.Event | None = None) -> dict | None:
        """THE STAND-IN COUNTS (S16a R1), as it answers: each word of what it
        is asked is a token - the number its answer's `usage` says - against
        the context its settings give the model. So a request past it fails
        `prompt_too_long` on the stand-in too, before it is asked. A
        transcription is not counted: its length is its audio."""
        if self.answers == "once":
            return None
        if cancel is not None and cancel.is_set():
            raise Cancelled("cancelled while the stand-in counted the request")
        return {"prompt_tokens": len(TOKEN_RE.findall(said(as_messages(messages)))),
                "context": self.ctx}

    def stream(self, messages, params: GenParams,
               cancel: threading.Event | None = None) -> Iterator[dict]:
        if self.answers == "once":
            yield from self._transcribe(messages, cancel)
            return
        sent = as_messages(messages)
        self.last_call = {"model": self.model_key, "messages": sent, **params.as_body()}
        user_message = said(sent)
        if self._should_fail(user_message):
            raise BackendError(f"the stand-in failed on purpose for {self.model_key}: "
                               f"CUDA error: out of memory (simulated)")
        delay = 1.0 / self.tps if self.tps > 0 else 0.0
        started = time.time()
        if THINK_MARK in user_message:
            for piece in ("Let ", "me ", "think ", "about ", "this."):
                if cancel is not None and cancel.is_set():
                    raise Cancelled("cancelled while thinking")
                yield {"type": "reasoning", "text": piece}
        text = self.compose(user_message, params)
        if THINK_MARK in user_message:
            text = "<think>considering the request</think>\n" + text
        tokens = TOKEN_RE.findall(text)
        # as llama-server reads it: -1 names no limit, 0 reads the prompt and
        # writes nothing, finish "length" - the llama.cpp stand-in's reading
        most = int(params.max_tokens)
        limit = len(tokens) if most < 0 else most
        finish = "stop"
        if len(tokens) > limit:
            tokens, finish = tokens[:limit], "length"
        for token in tokens:
            if cancel is not None and cancel.is_set():
                raise Cancelled("cancelled during generation")
            if self.taken_off:
                raise BackendError(f"{self.model_key} was taken off the card mid-answer")
            if delay:
                _wait_out(delay)
            yield {"type": "token", "text": token}
        elapsed = max(time.time() - started, 1e-6)
        prompt_tokens = len(TOKEN_RE.findall(user_message))
        yield {"type": "done", "finish_reason": finish,
               "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": len(tokens),
                         "total_tokens": prompt_tokens + len(tokens)},
               "timings": {"predicted_n": len(tokens),
                           "predicted_per_second": len(tokens) / elapsed}}
