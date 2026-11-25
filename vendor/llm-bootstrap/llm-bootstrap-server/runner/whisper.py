"""whisper.cpp's whisper-server as a child process, one per load, and its transcription (PLAN-4 D9, D11).

  launch    `-m <file> --host 127.0.0.1 --port <p> --request-path /<load id>
            -t <threads> [--vad -vm <vad>]`, then the model's own extra
            arguments - never one of the flags above, which are this engine's
            to set (`engines.OWN_FLAGS`; the settings refuse them). `/load` is never
            used: it ends the whole process on a failed load (D11). Before the
            launch the model's file is read for its header - the ggml magic
            and eleven numbers (`runner/ggml.py`) - and a file that is not a
            whisper model is refused in words, with nothing started.
  proof     `--request-path /<load id>` (D9): every route of this load is
            under it, so a stranger that takes the port while the model comes
            up - nothing listens until then (measured on 1.9.1) - answers 404
            there. `engines.is_ours` reads the same path back off a command
            line.
  ready     when `/<load id>/health` answers 200, within the model's own
            `startup_timeout_s` (the runner's, unless it names one). Any exit
            before that is a failed start - a clean exit included:
            whisper-server exits 0 on an argument it does not know (measured)
            - and says the log's last lines. A 404 on its own path is a
            stranger on the port: the start is refused, and our process
            stopped.
  held off  a start that launched whisper-server and failed is not tried
            again for the model's `retry_after_s` (60 s): a load of it
            meanwhile is refused at once, saying why and what the failed one
            said, with nothing started - whisper-server's load is seconds of
            the card each time, and one that failed is likely to fail again.
            A give-up is not a failure. The failed starts are the manager's to
            keep (`starts`), so they outlive the load that failed.
  the answer  a transcription - `{"audio": <a WAV file on this machine>,
            "fields": {...}}` - sent as whisper-server reads it: the audio's
            bytes in a part of their own (`file`), every field as the form
            text its parser reads, and `response_format` the engine's own,
            `verbose_json`. Whisper-server's answer comes in ONE piece: the
            whole transcript, held to the contract's shape (`transcript`), its
            `text`, its `tokens` (every text token of every segment), and
            `audio_s`, the WAV's own length.
  its deadline  declared before it is asked (`answer_deadline_s`, PLAN-4 D3):
            `due_base_s` + `due_per_audio_s` seconds for each second of the
            audio - 30 s and one, by default: real time, and half a minute
            besides. Measured on the card by the application whose models
            these are, a warm whisper-server answered 11 s of speech in 0.74 s
            and 71 s in 3.75 s - about a twentieth of real time - so real time
            leaves room for a beam search, a decode tried again at a higher
            temperature, the language table's extra pass and a card shared
            with other work; and the half minute, for a first answer and a
            short recording's fixed costs. An answer slower than real time on
            the card is not running as it should: on the CPU it was 16 to 34
            times slower than on the card. The watch - the queue's stall
            watch, a coordinator's hang check - never calls the quiet model
            hung before it; and the answer itself is given up AT it: its
            socket is closed, which whisper-server hears as the client gone
            and stops its work (read from 1.9.1's source: its abort callback
            asks whether the connection is closed), and the answer fails
            saying so. One transcription at a time on one load, as
            whisper-server takes them: an answer that waits for another's
            declares that one's time left too.
  a cancel  closes the socket, at once: the same stop, heard by whisper-server.
  its log   read from this load's own launch for the lines that say the
            card's memory ran out - ggml's own, the lines memlog.py names,
            written by the ggml whisper.cpp 1.9.1 carries as by llama.cpp's
            (`ModelServer.memory_errors`).
  stop      terminate, wait until the process is gone AND its port is free
            (`ModelServer`, llama-server's process handling too).

`whisper_server` in the settings is a path, or a list - an interpreter and a
script - which is how a stand-in with the same command line takes its place.
It is started where every model server is started (`llamacpp.SPAWN`), so one
guard there holds every engine.
"""
from __future__ import annotations

import http.client
import json
import secrets
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator

from . import llamacpp as _L
from .base import BackendError, Cancelled, GenParams
from .ggml import GGMLError, read_header
from .whisper_fields import AUDIO_PART, as_form

_CONTRACT = Path(__file__).resolve().parents[2] / "llm-bootstrap-contract"
if str(_CONTRACT) not in sys.path:
    sys.path.insert(0, str(_CONTRACT))
import validate as _V                                           # noqa: E402

#: THE MOST OF AN ANSWER READ. A verbose transcript of an hour of speech, every
#: word with its times and its probability, is a few MiB; this is room for
#: many hours, and a server answering more is not answering a transcription
ANSWER_CAP = 64 * 1024 * 1024
#: what a transcript is: the contract's, the answer held to it whole
_SHAPES = _V.load(_CONTRACT)[0]
TRANSCRIPT = {"ref": "transcript"}


class AudioError(ValueError):
    """The audio is not a WAV this machine can read, or its length cannot be
    told from it - said in words."""


def audio_seconds(path) -> float:
    """The length, in seconds, of the WAV at *path*, from its own header: the
    bytes of its `data` chunk over the bytes a second its `fmt ` chunk says -
    whatever its samples are (whole numbers, floats, several channels). A
    `data` chunk that says more than the file holds is the file's: a WAV
    still being written says a size it has not reached. Raises AudioError, in
    words, for a file that is not there, cannot be read, or is not a WAV."""
    p = Path(str(path))
    try:
        size = p.stat().st_size
        fh = open(p, "rb")
    except FileNotFoundError:
        raise AudioError(f"{p} is not there") from None
    except OSError as exc:
        raise AudioError(f"{p} cannot be read: {exc}") from None
    with fh:
        head = fh.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise AudioError(f"{p} is not a WAV: it does not begin RIFF, then WAVE "
                             f"(it begins {head[:12]!r})")
        per_second, pos = None, 12
        while pos + 8 <= size:
            fh.seek(pos)
            chunk = fh.read(8)
            name, n = chunk[:4], struct.unpack("<I", chunk[4:8])[0]
            if name == b"fmt ":
                fmt = fh.read(16)
                if len(fmt) < 16:
                    raise AudioError(f"{p} is not a WAV: its format is cut short")
                per_second = struct.unpack("<I", fmt[8:12])[0]
                if not per_second:
                    raise AudioError(f"{p} is not a WAV: its format says 0 bytes a second")
            elif name == b"data":
                if per_second is None:
                    raise AudioError(f"{p} is not a WAV: its sound comes before its format")
                return min(n, size - pos - 8) / per_second
            pos += 8 + n + (n & 1)                          # a chunk is padded to an even length
    raise AudioError(f"{p} is not a WAV: it has no "
                     f"{'sound (data)' if per_second else 'format (fmt)'} in it")


def _form(fields: dict, audio: bytes, boundary: str) -> bytes:
    """A multipart body as whisper-server reads one: every field a part of
    its own, and the audio in its part (`file`)."""
    out = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
           .encode("utf-8") for k, v in fields.items()]
    out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{AUDIO_PART}"; '
               f'filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode("utf-8")
               + audio + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(out)


def refusal(status: int, body: bytes) -> str:
    """whisper-server's refusal of a transcription, as a sentence: its own
    `error` when it answered its JSON (`failed to process audio`, 500), and
    otherwise its words as they came (its error handler's `Invalid request`
    for a 400)."""
    text = body.decode("utf-8", "replace").strip()
    try:
        said = json.loads(text).get("error")
    except (ValueError, AttributeError):
        said = None
    return (f"whisper-server refused the transcription (HTTP {status}): "
            f"{said if isinstance(said, str) else text[:300] or 'no words'}")


class WhisperBackend(_L.ModelServer):
    kind = "whisper"
    answers = "once"
    program_name = "whisper-server"
    program_setting = "whisper_server"

    def __init__(self, spec, models_dir, runner_cfg: dict, log_dir, port, *, starts=None):
        super().__init__(spec, models_dir, runner_cfg, log_dir, port)
        #: THE FAILED STARTS, by model: (when, what it said). The manager's to
        #: keep, so that they outlive the load that failed (`held off`)
        self.starts = starts if starts is not None else {}
        #: the model file's header, as read before its launch
        self.header: dict | None = None
        #: the answers under way on this load, and when each is due
        self._dues: dict = {}
        self._dues_lock = threading.Lock()
        #: whisper-server takes one transcription at a time
        self._turn = threading.Lock()

    @property
    def path(self) -> str:
        """This load's own request path: every route of it is under it."""
        return f"/{self.load_id}"

    # -------------------------------------------------------- command line

    def build_command(self) -> list:
        """D11's command line: the model, where it listens, the load's own
        path, its threads and its voice detector - then the model's own extra
        arguments."""
        model = self.spec.gguf_path(self.models_dir)
        cmd = [*_L.command_prefix(self.cfg.get("whisper_server", "whisper-server")),
               "-m", str(model),
               "--host", self.host,
               "--port", str(self.port),
               "--request-path", self.path,
               "-t", str(self.spec.threads)]
        if self.spec.vad:
            cmd += ["--vad", "-vm", str(self.models_dir / self.spec.vad)]
        return cmd + [str(a) for a in (self.spec.extra_args or [])]

    # ----------------------------------------------------------- lifecycle

    def _start(self) -> None:
        if self._given_up:
            raise BackendError(f"the load was given up: {self._given_up}")
        if self.log_dir is None:
            raise BackendError("a real model needs a bound folder: its launch is logged "
                               "there, and a launch nobody can read cannot be diagnosed")
        if not self.load_id or not self.port:
            raise BackendError("a load needs its id and its port before it starts; "
                               "the manager gives both")
        self._held_off()
        key = self.spec.key
        model = self.spec.gguf_path(self.models_dir)
        if not model.is_file():
            raise BackendError(f"model file not found: {model}. Point [models.{key}] "
                               f"file at a whisper model you have.")
        try:
            self.header = read_header(model)
        except GGMLError as exc:
            raise BackendError(f"{exc}. Point [models.{key}] file at a whisper model - a "
                               f"download cut short is fetched again") from None
        except OSError as exc:
            raise BackendError(f"cannot read {model}: {exc}") from None
        if self.spec.vad and not (self.models_dir / self.spec.vad).is_file():
            raise BackendError(f"voice detector file not found: {self.models_dir / self.spec.vad}"
                               f". Point [models.{key}] vad at the one you have, or leave it out.")
        if _L.port_in_use(self.host, self.port):
            raise BackendError(f"port {self.port} is already in use - something else holds it")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._mark_log()
        self.command = self.build_command()
        self._spawn(self.command, "launch")
        try:
            if self._given_up:
                # given up while the process was being started, before there
                # was one to end
                raise BackendError(f"the load was given up: {self._given_up}")
            self._await_ready()
        except BackendError as exc:
            tail = self._log_tail()
            self._hard_stop()
            if self._given_up:
                # in its own words: not a start that failed on its own
                raise BackendError(f"the load was given up: {self._given_up}") from None
            said = f"{exc}\n--- whisper-server log tail ---\n{tail}"
            self.starts[key] = (time.monotonic(), said)
            raise BackendError(said) from None
        self.starts.pop(key, None)                          # up: a later failure counts afresh

    def _held_off(self) -> None:
        """A start that launched whisper-server and failed is not tried again
        for `retry_after_s`: refused at once, before anything is read or
        started, saying when, and what the failed one said."""
        failed = self.starts.get(self.spec.key)
        hold = float(self.spec.retry_after_s)
        if failed is None or hold <= 0:
            return
        ago = time.monotonic() - failed[0]
        if ago < hold:
            raise BackendError(f"{self.spec.key} is not started again yet: its last start failed "
                               f"{ago:.0f}s ago, and a failed start is tried again only after "
                               f"{hold:g}s ([models.{self.spec.key}] retry_after_s). It said: "
                               f"{failed[1][:600]}")

    def _await_ready(self) -> None:
        """Until `/<load id>/health` answers 200: nothing listens while the
        model comes up, so a refused connection is waited through; an exit is
        a failed start, whatever its code; a 404 there is not this load."""
        own = self.spec.startup_timeout_s
        limit = float(own if own is not None else self.cfg.get("startup_timeout_s", 600))
        poll = float(self.cfg.get("health_poll_s", 0.1))
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise BackendError(f"whisper-server exited with code {self.proc.returncode} "
                                   f"before it was ready")
            try:
                with urllib.request.urlopen(self._url("/health"), timeout=5) as resp:
                    if resp.status == 200:
                        # its port, its model and its identity in one answer:
                        # nothing listens until the model is up, and the path
                        # is this load's - healthy is the one step marked (D12)
                        self.mark("healthy")
                        return
            except urllib.error.HTTPError as exc:
                exc.close()                                 # a refusal is a response too
                if exc.code == 404:
                    raise BackendError(f"the server on port {self.port} answers 404 at "
                                       f"{self.path}/health: it is not {self.load_id} - "
                                       f"something else has the port") from None
                raise BackendError(f"{self.path}/health answered HTTP {exc.code}") from None
            except (urllib.error.URLError, http.client.HTTPException, OSError):
                pass                                        # not listening yet
            time.sleep(poll)
        raise BackendError(f"whisper-server was not ready within {limit:g}s")

    def _url(self, route: str) -> str:
        return f"http://{self.host}:{self.port}{self.path}{route}"

    # ------------------------------------------------------------ the answer

    def answer_deadline_s(self, request, params: GenParams) -> float:
        """THE DEADLINE this transcription is due by, in seconds from now:
        `due_base_s` + `due_per_audio_s` for each second of its audio - and,
        when another answer on this load is under way, that one's time left
        too: whisper-server takes one at a time. Audio whose length cannot be
        told is reckoned as none: its answer fails, saying why, before
        anything is sent."""
        try:
            seconds = audio_seconds(request["audio"])
        except (AudioError, KeyError, TypeError):
            seconds = 0.0
        own = float(self.spec.due_base_s) + float(self.spec.due_per_audio_s) * seconds
        now = time.monotonic()
        with self._dues_lock:
            ahead = max((due - now for due in self._dues.values()), default=0.0)
        return round(own + max(0.0, ahead), 3)

    def stream(self, request, params: GenParams,
               cancel: threading.Event | None = None) -> Iterator[dict]:
        """The transcription, in one piece: one `done` with the whole answer,
        once whisper-server has given it - or a failure, saying why."""
        if not (isinstance(request, dict) and isinstance(request.get("audio"), str)):
            raise BackendError(f'{self.model_key} transcribes audio: its request is {{"audio": '
                               f'<a WAV file on this machine>, "fields": {{...}}}}')
        path = request["audio"]
        try:
            seconds = audio_seconds(path)
            audio = Path(path).read_bytes()
        except AudioError as exc:
            raise BackendError(f"the audio cannot be transcribed: {exc}") from None
        except OSError as exc:
            raise BackendError(f"the audio cannot be transcribed: {path} cannot be read: "
                               f"{exc}") from None
        fields = {k: as_form(v) for k, v in dict(request.get("fields") or {}).items()}
        fields["response_format"] = "verbose_json"         # the engine's own: every field kept
        # the exact call, for the request record: the audio by its path
        self.last_call = {"model": self.load_id, "audio": path, "fields": dict(fields)}
        if cancel is not None and cancel.is_set():
            raise Cancelled("cancelled before the audio was sent")
        due = self.answer_deadline_s(request, params)
        deadline = time.monotonic() + due
        me = object()
        with self._dues_lock:
            self._dues[me] = deadline
        try:
            self._wait_turn(deadline, cancel, due, seconds)
            try:
                doc = self._transcribe(audio, fields, deadline, cancel, due, seconds)
            finally:
                self._turn.release()
        finally:
            with self._dues_lock:
                self._dues.pop(me, None)
        wrong = _V.check(doc, TRANSCRIPT, _SHAPES)
        if wrong:
            raise BackendError(f"whisper-server answered a transcript the contract does not "
                               f"declare: {wrong[0]}")
        yield {"type": "done", "finish_reason": "stop", "text": doc["text"],
               "tokens": sum(len(s.get("tokens") or []) for s in doc["segments"]),
               "transcript": doc, "audio_s": seconds}

    def _overdue(self, due: float, seconds: float) -> BackendError:
        return BackendError(f"whisper-server did not answer within its deadline: {due:g}s for "
                            f"{seconds:g}s of audio ([models.{self.spec.key}] due_base_s "
                            f"{self.spec.due_base_s:g}, and due_per_audio_s "
                            f"{self.spec.due_per_audio_s:g} for each second of it) - the answer "
                            f"was given up, and whisper-server told so")

    def _wait_turn(self, deadline: float, cancel, due: float, seconds: float) -> None:
        """This load's turn to transcribe: one at a time, as whisper-server
        takes them - waited for within the deadline, a cancel heard."""
        while not self._turn.acquire(timeout=0.05):
            if cancel is not None and cancel.is_set():
                raise Cancelled("cancelled while another transcription on this model ran")
            if time.monotonic() >= deadline:
                raise self._overdue(due, seconds)

    def _transcribe(self, audio: bytes, fields: dict, deadline: float, cancel, due: float,
                    seconds: float) -> dict:
        """One POST to `/<load id>/inference`, on a socket of our own - a
        cancel, and the deadline, end it from another thread at once (the
        read is blocked until whisper-server answers, and only a real close
        wakes it: `llamacpp._shut`) - and its answer, read whole."""
        boundary = "llmb-" + secrets.token_hex(12)
        body = _form(fields, audio, boundary)
        left = deadline - time.monotonic()
        if left <= 0:
            raise self._overdue(due, seconds)
        try:
            sock = socket.create_connection((self.host, self.port), timeout=min(left, 10.0))
        except OSError as exc:
            raise BackendError(f"cannot reach whisper-server on port {self.port}: {exc}") from None
        # a backstop behind the watch below, never the bound itself
        sock.settimeout(left + 5.0)
        conn = http.client.HTTPConnection(self.host, self.port, timeout=left + 5.0)
        conn.sock = sock
        done, ended = threading.Event(), {}

        def watch():
            while not done.is_set():
                if cancel is not None and cancel.wait(0.05):
                    ended["by"] = "cancel"
                elif cancel is None:
                    done.wait(0.05)
                if time.monotonic() >= deadline and "by" not in ended:
                    ended["by"] = "deadline"
                if "by" in ended:
                    _L._shut(sock)                          # whisper-server hears the client go
                    return

        threading.Thread(target=watch, name=f"answer-{self.load_id}", daemon=True).start()
        try:
            try:
                conn.request("POST", f"{self.path}/inference", body=body,
                             headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
                resp = conn.getresponse()
                data = resp.read(ANSWER_CAP + 1)
                status = resp.status
            except (OSError, http.client.HTTPException) as exc:
                if ended.get("by") == "cancel" or (cancel is not None and cancel.is_set()):
                    raise Cancelled("cancelled while transcribing") from None
                if ended.get("by") == "deadline" or time.monotonic() >= deadline:
                    raise self._overdue(due, seconds) from None
                raise BackendError(f"the answer from whisper-server broke - it went away "
                                   f"mid-answer: {exc}") from None
        finally:
            done.set()
            _L._shut(sock)
        if cancel is not None and cancel.is_set():
            raise Cancelled("cancelled while transcribing")
        if len(data) > ANSWER_CAP:
            raise BackendError(f"whisper-server's answer was larger than "
                               f"{ANSWER_CAP // (1024 * 1024)} MiB: not a transcription")
        if status != 200:
            raise BackendError(refusal(status, data))
        try:
            doc = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            raise BackendError(f"whisper-server answered something that is not JSON: "
                               f"{data[:200]!r}") from None
        return doc
