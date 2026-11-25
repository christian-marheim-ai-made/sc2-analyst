"""A stand-in for whisper.cpp's whisper-server 1.9.1: its command line, its routes, no model.

    python fake_whisper_server.py -m <model file> --host 127.0.0.1 --port <n>
                                  --request-path /<load id> [whisper-server's other flags]
                                  [--fake-...]

It exists so a whisper engine - the launch, the wait for the port, a load
named by its path, a transcription, a failed start, a death or a hang
mid-answer - can be proven with no GPU, no weights and no whisper.cpp build.

AS WHISPER-SERVER 1.9.1 WAS MEASURED (a CPU build, 2026-09-25):

  * an argument it does not know: the usage printed, and exit 0 - a clean
    exit is not a start;
  * a model file that is not there: exit 3 at once, saying so;
  * the port is NOT bound while the model loads: nothing listens until the
    model is up, and then health answers 200 at once;
  * --request-path moves every route: health answers 200 {"status":"ok"}
    under it, its page answers under it, and anything outside it answers 404
    "File Not Found (<path>)";
  * health answers while a transcription runs: it takes no lock;
  * a request field it does not know is ignored, and answered 200;
  * the verbose answer: task, language, duration, text, segments (id, text,
    start, end, tokens, words - word, start, end, t_dtw, probability -
    temperature, avg_logprob, no_speech_prob), then the language table:
    detected_language, detected_language_probability, language_probabilities.
    The text is every segment's, each followed by a newline; silence is no
    text and no segments.

AS ITS SOURCE SAYS, where nothing measured it (whisper.cpp at v1.9.1):

  * `-h` and `--help` print the usage and exit 0;
  * a file without the ggml magic exits 3, as one that is not there does;
  * a port taken by the time the model is up ends it: "couldn't bind", exit 1;
  * one transcription at a time - the handler holds one lock - with health
    beside it;
  * json is the default answer, {"text": ...}: verbose_json is asked for;
  * no_language_probabilities is true for "true", "1", "yes" or "y" alone,
    and takes the language table out;
  * a segment's start and end are written only without no_timestamps; a
    word's start, end and t_dtw only without it and with token_timestamps -
    which, when a request does not send it, is the opposite of
    no_timestamps (both read as no_language_probabilities is);
  * the card's memory running out in the middle of an answer is said in
    ggml's own lines (`ggml_cuda_error`, its vendored ggml), and the process
    aborts (`--fake-oom-after`);
  * a request without its `file` part, or with audio it cannot read, is
    refused 400, and the words are the server's error handler's for a 400,
    "Invalid request". That its HTTP library (cpp-httplib 0.20.0) runs that
    handler on every answer of 400 or more, over the handler's own words, is
    recalled - neither read nor measured.

ITS OWN CHOICES, where the real server gives nothing to copy:

  * a flag given no value is refused as an unknown one is (the real one's
    parser takes the next argument without checking there is one);
  * of `-m` and `--model`, the last given wins; with neither, it looks for
    the real server's default, models/ggml-base.en.bin;
  * the transcript is fixed, and a field sent twice is read from its first
    part;
  * only response_format, language and no_language_probabilities change an
    answer - the command line's `-l` and `-nlp` are their defaults, and a
    language code it has no name for is answered as it was given - and every
    other field is taken, recorded and ignored;
  * the audio must be a PCM WAV (the real one decodes with miniaudio, which
    reads MP3 and FLAC as well, and resamples to 16 kHz);
  * text, srt and vtt are answered as json;
  * there is no /load route (the real one has it under the request path):
    nothing here uses it.

Its own switches - never whisper-server's - make it misbehave on purpose:

    --fake-load-s S       the model takes S seconds to come up: nothing listens meanwhile
    --fake-exit-code N    exit with N at once, before anything - 0 included
    --fake-answer-s S     a transcription takes S seconds
    --fake-die-after N    answer N transcriptions, then die in the middle of the next
    --fake-hang-after N   answer N transcriptions, then never answer again; health still does
    --fake-oom-after N    answer N transcriptions, then run out of the card's memory in the
                          middle of the next: ggml's own lines for it, and an abort (exit 3)
    --fake-record PATH    append every transcription as it is taken, and how it ended, as a
                          JSON line
"""
from __future__ import annotations

import io
import json
import os
import re
import struct
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

#: every flag whisper-server 1.9.1 printed in its usage that takes a value
VALUED = {"-t", "--threads", "-p", "--processors", "-ot", "--offset-t", "-on", "--offset-n",
          "-d", "--duration", "-mc", "--max-context", "-ml", "--max-len", "-bo", "--best-of",
          "-bs", "--beam-size", "-ac", "--audio-ctx", "-wt", "--word-thold", "-et",
          "--entropy-thold", "-lpt", "--logprob-thold", "-l", "--language", "--prompt", "-m",
          "--model", "-oved", "--ov-e-device", "-dtw", "--dtw", "--host", "--port", "--public",
          "--request-path", "--inference-path", "--tmp-dir", "-nth", "--no-speech-thold",
          "-dev", "--device", "-vm", "--vad-model", "-vt", "--vad-threshold", "-vspd",
          "--vad-min-speech-duration-ms", "-vsd", "--vad-min-silence-duration-ms", "-vmsd",
          "--vad-max-speech-duration-s", "-vp", "--vad-speech-pad-ms", "-vo",
          "--vad-samples-overlap"}
#: and every one that takes none
SWITCHES = {"-sow", "--split-on-word", "-debug", "--debug-mode", "-tr", "--translate", "-di",
            "--diarize", "-tdrz", "--tinydiarize", "-nf", "--no-fallback", "-ps",
            "--print-special", "-pc", "--print-colors", "-pr", "--print-realtime", "-pp",
            "--print-progress", "-nt", "--no-timestamps", "-dl", "--detect-language",
            "--carry-initial-prompt", "--convert", "-sns", "--suppress-nst", "-ng", "--no-gpu",
            "-fa", "--flash-attn", "-nfa", "--no-flash-attn", "-nlp",
            "--no-language-probabilities", "--vad"}
FAKE = {"--fake-load-s", "--fake-exit-code", "--fake-answer-s", "--fake-die-after",
        "--fake-hang-after", "--fake-oom-after", "--fake-record"}
#: WHAT GGML'S CUDA BACKEND WRITES when a CUDA call fails for want of the
#: card's memory - `ggml_cuda_error`, in whisper.cpp 1.9.1's vendored ggml
#: (ggml/src/ggml-cuda/ggml-cuda.cu): the error, where it was, the call; then
#: GGML_ABORT's own line, and the process ends. The device, the function, the
#: call and the line numbers are this stand-in's
OUT_OF_MEMORY = ("CUDA error: out of memory",
                 "  current device: 0, in function ggml_cuda_pool_vmm::alloc at "
                 "ggml/src/ggml-cuda/ggml-cuda.cu:420",
                 "  cuMemCreate(&handle, reserve_size, &prop, 0)",
                 "ggml/src/ggml-cuda/ggml-cuda.cu:88: CUDA error")
#: the short spellings of the flags the stand-in reads: the last one given wins
LONG = {"-m": "--model", "-l": "--language", "-nlp": "--no-language-probabilities"}
MAGIC = struct.pack("<I", 0x67676d6c)
#: whisper's own names for the languages this stand-in names; any other code
#: is answered as it was given
NAMES = {"en": "english", "no": "norwegian", "nn": "nynorsk", "sv": "swedish", "da": "danish",
         "de": "german"}
TRANSCRIPT = (" Rain on the tin roof again,", " a letter came through the open window.")
LANGUAGES = {"en": 0.9, "no": 0.05, "de": 0.02}
PAGE = b"<!doctype html><title>fake whisper-server</title><p>a stand-in: no model</p>\n"


def say(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def usage() -> None:
    say("usage: fake_whisper_server.py [options]\n\n"
        "a stand-in for whisper-server 1.9.1: every flag that server takes is taken here, and\n"
        "these change what the stand-in does:\n"
        "  -m FNAME, --model FNAME             the model file: there, and a ggml one\n"
        "  --host HOST, --port PORT            where it listens, once the model is up\n"
        "  --request-path PATH                 every route under PATH\n"
        "  --inference-path PATH               [/inference] the transcription's route\n"
        "  -l LANG, --language LANG            [en] a request's language when it names none\n"
        "  -nlp, --no-language-probabilities   no language table unless a request asks\n"
        "  -h, --help                          this, and exit 0\n\n"
        "and its own, to misbehave on purpose:\n"
        "  --fake-load-s S --fake-exit-code N --fake-answer-s S --fake-die-after N\n"
        "  --fake-hang-after N --fake-oom-after N --fake-record PATH")


def parse(argv: list) -> tuple:
    """-> (the valued flags by their long names, the switches given)."""
    opts: dict = {}
    switches: set = set()
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            usage()
            sys.exit(0)
        if a in VALUED or a in FAKE:
            if i + 1 >= len(argv):
                say(f"error: {a} needs a value")
                usage()
                sys.exit(0)
            opts[LONG.get(a, a)] = argv[i + 1]
            i += 2
            continue
        if a in SWITCHES:
            switches.add(LONG.get(a, a))
            i += 1
            continue
        say(f"error: unknown argument: {a}")
        usage()
        sys.exit(0)
    return opts, switches


def truthy(value: str) -> bool:
    """whisper-server's own reading of a true field, and no other."""
    return value in ("true", "1", "yes", "y")


def form_parts(body: bytes, content_type: str) -> dict:
    """A multipart/form-data body -> {name: (file name or None, bytes)}. The
    boundary may be quoted. A name sent twice is read from its first part -
    the stand-in's choice: the real server's library reads either."""
    m = re.search(r'boundary=(?:"([^"]+)"|([^;\s]+))', content_type or "")
    if not m:
        return {}
    delimiter = b"--" + (m.group(1) or m.group(2)).encode("latin-1")
    parts: dict = {}
    for chunk in body.split(delimiter)[1:]:
        if chunk.startswith(b"--"):
            break
        head, sep, content = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        if content.endswith(b"\r\n"):
            content = content[:-2]
        text = head.decode("utf-8", "replace")
        name = re.search(r';\s*name="([^"]*)"', text)
        if name:
            filename = re.search(r';\s*filename="([^"]*)"', text)
            parts.setdefault(name.group(1), (filename.group(1) if filename else None, content))
    return parts


def audio_of(raw: bytes):
    """A PCM WAV -> (its seconds, whether any sample is not silence), or None
    for anything that is not one."""
    try:
        with wave.open(io.BytesIO(raw), "rb") as w:
            seconds = w.getnframes() / w.getframerate()
            zero = b"\x80" if w.getsampwidth() == 1 else b"\x00"   # 8-bit PCM is unsigned
            return seconds, bool(w.readframes(w.getnframes()).strip(zero))
    except Exception:                                   # noqa: BLE001 - unreadable, whatever it is
        return None


def transcript(seconds: float) -> list:
    """The fixed transcript, laid over the audio: its segments share the
    length, its words share their segment, all in hundredths of a second as
    whisper counts them."""
    segments = []
    n = len(TRANSCRIPT)
    for i, text in enumerate(TRANSCRIPT):
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
    return segments


class Fake:
    def __init__(self, opts: dict, switches: set):
        self.prefix = opts.get("--request-path", "")
        self.inference = self.prefix + opts.get("--inference-path", "/inference")
        self.language = opts.get("--language", "en")
        self.no_table = "--no-language-probabilities" in switches
        self.answer_s = float(opts.get("--fake-answer-s", 0))
        self.die_after = int(opts["--fake-die-after"]) if "--fake-die-after" in opts else None
        self.hang_after = int(opts["--fake-hang-after"]) if "--fake-hang-after" in opts else None
        self.oom_after = int(opts["--fake-oom-after"]) if "--fake-oom-after" in opts else None
        self.record_path = opts.get("--fake-record")
        #: whisper-server's own: one transcription at a time
        self.lock = threading.Lock()
        self.record_lock = threading.Lock()
        self.answered = 0

    def record(self, entry: dict) -> None:
        if not self.record_path:
            return
        with self.record_lock, open(self.record_path, "ab") as fh:
            fh.write(json.dumps(entry).encode("utf-8") + b"\n")

    def answer(self, fields: dict, seconds: float, sound: bool) -> dict:
        segments = transcript(seconds) if sound else []
        text = "".join(s["text"] + "\n" for s in segments)
        if fields.get("response_format", "json") != "verbose_json":
            return {"text": text}
        # its times, as 1.9.1's source writes them: a segment's without
        # no_timestamps; a word's without it and with token_timestamps, which,
        # not sent, is the opposite of no_timestamps
        untimed = truthy(fields.get("no_timestamps", "false"))
        word_times = truthy(fields["token_timestamps"]) if "token_timestamps" in fields \
            else not untimed
        for seg in segments:
            if untimed:
                del seg["start"], seg["end"]
            if untimed or not word_times:
                for word in seg["words"]:
                    del word["start"], word["end"], word["t_dtw"]
        language = fields.get("language", self.language)
        doc = {"task": "transcribe",
               "language": "english" if language == "auto" else NAMES.get(language, language),
               "duration": seconds, "text": text, "segments": segments}
        no_table = truthy(fields["no_language_probabilities"]) \
            if "no_language_probabilities" in fields else self.no_table
        if not no_table:
            doc["detected_language"] = "english"
            doc["detected_language_probability"] = LANGUAGES["en"]
            doc["language_probabilities"] = dict(LANGUAGES)
        return doc


def handler_for(fake: Fake):
    class Handler(BaseHTTPRequestHandler):
        def log_request(self, code="-", size="-"):          # one short line per request
            say(f"request: {self.command} {self.path} {getattr(code, 'value', code)}")

        def log_message(self, fmt, *args):
            say("fake: " + (fmt % args))

        def send(self, status: int, raw: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def not_found(self, path: str) -> None:
            self.send(404, f"File Not Found ({path})".encode("utf-8"), "text/plain")

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == fake.prefix + "/health":
                return self.send(200, b'{"status":"ok"}', "application/json")
            if path == fake.prefix + "/":
                return self.send(200, PAGE, "text/html")
            self.not_found(path)

        def do_POST(self):
            path = urlsplit(self.path).path
            # the whole request is read before it is answered, as the real
            # server's HTTP library reads it: a refusal is never a reset
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if path != fake.inference:
                return self.not_found(path)
            parts = form_parts(body, self.headers.get("Content-Type", ""))
            fields = {k: v.decode("utf-8", "replace") for k, (_, v) in parts.items()
                      if k != "file"}
            with fake.lock:
                audio = parts.get("file")
                fake.record({"event": "request", "at": time.monotonic(), "fields": fields,
                             "audio": None if audio is None else {"name": audio[0],
                                                                  "bytes": len(audio[1])}})
                heard = audio_of(audio[1]) if audio is not None else None
                if heard is None:
                    fake.record({"event": "refused", "at": time.monotonic(), "status": 400})
                    return self.send(400, b"Invalid request", "text/plain")
                if fake.die_after is not None and fake.answered >= fake.die_after:
                    say("fake whisper-server: dying mid-answer, as asked")
                    os._exit(1)
                if fake.oom_after is not None and fake.answered >= fake.oom_after:
                    for line in OUT_OF_MEMORY:
                        say(line)
                    os._exit(3)                             # its abort
                if fake.hang_after is not None and fake.answered >= fake.hang_after:
                    say("fake whisper-server: hanging mid-answer, as asked")
                    threading.Event().wait()
                time.sleep(fake.answer_s)
                doc = fake.answer(fields, *heard)
                fake.answered += 1
                fake.record({"event": "answered", "at": time.monotonic(), "status": 200})
            # compact, as whisper-server answered
            self.send(200, json.dumps(doc, separators=(",", ":")).encode("utf-8"),
                      "application/json")

    return Handler


class _Listening(ThreadingHTTPServer):
    """A backlog of 64, not the standard library's 5 - the stand-in
    llama-server's, for the same reason: a burst of connections from several
    suites at once must wait, not be refused. And no SO_REUSEADDR on Windows,
    where the standard library's http.server sets it: there a second socket
    with it set may bind a port the first holds (measured 2026-09-25), so a
    stand-in could share a stranger's port instead of stopping, as
    whisper-server stops when its bind fails."""
    request_queue_size = 64
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True


def main(argv: list) -> int:
    opts, switches = parse(argv)
    if "--fake-exit-code" in opts:
        say(f"fake whisper-server: exiting with {opts['--fake-exit-code']} as asked")
        return int(opts["--fake-exit-code"])
    model = opts.get("--model", "models/ggml-base.en.bin")
    say(f"whisper_init_from_file_with_params_no_state: loading model from '{model}'")
    try:
        with open(model, "rb") as fh:
            magic = fh.read(4)
    except OSError:
        say(f"whisper_init_from_file_with_params_no_state: failed to open '{model}'")
        say("error: failed to initialize whisper context")
        return 3
    if magic != MAGIC:
        say("whisper_model_load: invalid model data (bad magic)")
        say("error: failed to initialize whisper context")
        return 3
    time.sleep(float(opts.get("--fake-load-s", 0)))
    host, port = opts.get("--host", "127.0.0.1"), int(opts.get("--port", 8080))
    fake = Fake(opts, switches)
    try:
        server = _Listening((host, port), handler_for(fake))
    except OSError:
        say(f"couldn't bind to server socket: hostname={host} port={port}")
        return 1
    say(f"fake whisper-server listening at http://{host}:{port}{fake.prefix}/ - a stand-in, "
        f"no model")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
