"""The whisper stand-in's rows, and the whisper model file's header - never a model.

    python test_whisper.py

No GPU, no weights, no whisper.cpp build. Every server this suite starts is
`fake_whisper_server.py`: whisper-server 1.9.1's command line and routes as
they were measured (a CPU build, 2026-09-25), with switches of its own to
misbehave on purpose. Every model file is a few bytes a row writes.

EVERY PROCESS THIS SUITE STARTS carries this run's token on its command line
(`--tmp-dir <token>-<n>`: an argument whisper-server takes, and the stand-in
ignores) and is ended by the row that started it, through the handle it was
started with - a handle names that process and no other, where a pid is
handed out again. The last row reads every command line on the machine for
the token: anything still running is ended, and fails the suite.

Its ports come from a range of its own, 28500-28999, bind-checked.

Every row here is native: nothing is carried over from another suite.
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from runner.ggml import GGMLError, read_header                  # noqa: E402  (puts the contract on the path)
from runner.whisper_fields import AUDIO_PART, INFERENCE_FIELDS  # noqa: E402
from _suites import KILL_WAIT_S, asked_again, killed, quiet_ports  # noqa: E402
import _suites                                                  # noqa: E402
_suites.forbid_the_card()        # a suite never asks the real card (nvidia-smi)
import ids                                                      # noqa: E402

FAKE = HERE / "fake_whisper_server.py"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
#: this run's token: on the command line of every process the suite starts
RUN = "llmb-whisper-" + ids.mint_actor("load")[len("load-"):]
#: every launch, in order: (its Popen, the marker on its command line)
LAUNCHED: list = []
#: below every common system's range for passing connections, and clear of
#: every other suite's range (GUIDE.md)
QUIET_PORTS = (28500, 28999)
MAGIC = 0x67676d6c
HEADER_FIELDS = ("n_vocab", "n_audio_ctx", "n_audio_state", "n_audio_head", "n_audio_layer",
                 "n_text_ctx", "n_text_state", "n_text_head", "n_text_layer", "n_mels", "ftype")
LARGE_V3 = dict(n_vocab=51866, n_audio_ctx=1500, n_audio_state=1280, n_audio_head=20,
                n_audio_layer=32, n_text_ctx=448, n_text_state=1280, n_text_head=20,
                n_text_layer=32, n_mels=128, ftype=1)
MEDIUM = dict(LARGE_V3, n_vocab=51865, n_audio_state=1024, n_audio_head=16, n_audio_layer=24,
              n_text_state=1024, n_text_head=16, n_text_layer=24, n_mels=80)
#: every flag whisper-server 1.9.1 printed in its usage when it was measured,
#: with a value where it takes one - `-h` aside, which prints the usage and exits
REAL_VALUED = ("-t", "--threads", "-p", "--processors", "-ot", "--offset-t", "-on", "--offset-n",
               "-d", "--duration", "-mc", "--max-context", "-ml", "--max-len", "-bo",
               "--best-of", "-bs", "--beam-size", "-ac", "--audio-ctx", "-wt", "--word-thold",
               "-et", "--entropy-thold", "-lpt", "--logprob-thold", "-l", "--language",
               "--prompt", "-oved", "--ov-e-device", "-dtw", "--dtw", "--public",
               "--inference-path", "-nth", "--no-speech-thold", "-dev", "--device", "-vm",
               "--vad-model", "-vt", "--vad-threshold", "-vspd", "--vad-min-speech-duration-ms",
               "-vsd", "--vad-min-silence-duration-ms", "-vmsd", "--vad-max-speech-duration-s",
               "-vp", "--vad-speech-pad-ms", "-vo", "--vad-samples-overlap")
REAL_SWITCHES = ("-sow", "--split-on-word", "-debug", "--debug-mode", "-tr", "--translate",
                 "-di", "--diarize", "-tdrz", "--tinydiarize", "-nf", "--no-fallback", "-ps",
                 "--print-special", "-pc", "--print-colors", "-pr", "--print-realtime", "-pp",
                 "--print-progress", "-nt", "--no-timestamps", "-dl", "--detect-language",
                 "--carry-initial-prompt", "--convert", "-sns", "--suppress-nst", "-ng",
                 "--no-gpu", "-fa", "--flash-attn", "-nfa", "--no-flash-attn", "-nlp",
                 "--no-language-probabilities", "--vad")
#: what each of the valued flags above is given: its own kind of value
REAL_VALUES = {"-l": "en", "--language": "en", "--prompt": "hello", "--inference-path": "/inference",
               "-oved": "CPU", "--ov-e-device": "CPU", "-dtw": "large.v3", "--dtw": "large.v3",
               "--public": ".", "-vm": "vad.bin", "--vad-model": "vad.bin"}
#: the fields whisper-server 1.9.1's request parser reads, in its order, as
#: its source was read twice (examples/server/server.cpp, get_req_parameters)
SOURCE_FIELDS = (
    "offset_t", "offset_n", "duration", "max_context", "max_len", "best_of", "beam_size",
    "audio_ctx", "word_thold", "entropy_thold", "logprob_thold", "no_speech_thold",
    "debug_mode", "translate", "diarize", "tinydiarize", "split_on_word", "no_timestamps",
    "token_timestamps", "language", "detect_language", "prompt", "carry_initial_prompt",
    "response_format", "temperature", "temperature_inc", "suppress_non_speech", "suppress_nst",
    "vad", "vad_threshold", "vad_min_speech_duration_ms", "vad_min_silence_duration_ms",
    "vad_max_speech_duration_s", "vad_speech_pad_ms", "vad_samples_overlap",
    "no_language_probabilities")


# ------------------------------------------------------------------- helpers

def quiet_port() -> int:
    """A port nothing listens on now, of the block of QUIET_PORTS this copy of
    the suite claimed: another copy never hands it out."""
    return quiet_ports(*QUIET_PORTS).take()


def accepts(port: int, timeout: float = 0.3) -> bool:
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ggml_bytes(magic: int = MAGIC, pad: int = 0, **over) -> bytes:
    """A whisper model file's header: the magic, then the eleven numbers."""
    numbers = dict(LARGE_V3, **over)
    return struct.pack("<I11i", magic, *(numbers[k] for k in HEADER_FIELDS)) + b"\0" * pad


def wav(seconds: float = 2.0, sound: bool = True, rate: int = 16000, width: int = 2) -> bytes:
    """Mono PCM, 16-bit or 8-bit: a square wave, or nothing but silence -
    zeros at 16 bits, and 0x80 at 8, where PCM is unsigned."""
    n = int(round(seconds * rate))
    if width == 1:
        frames = bytes((0x80 + (40 if i % 2 else -40)) if sound else 0x80 for i in range(n))
    else:
        step = struct.pack("<h", 1200) + struct.pack("<h", -1200)
        frames = (step * (n // 2 + 1))[: 2 * n] if sound else b"\0\0" * n
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


def form(fields, audio: bytes | None, quoted: bool = False) -> tuple:
    """A multipart body the way a client sends one: every field a part (a
    dict, or pairs when a name is sent twice), and the audio in its own part,
    `file`."""
    boundary = "----llmb" + ids.mint_actor("load")[len("load-"):]
    pairs = fields.items() if isinstance(fields, dict) else fields
    out = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
           .encode("utf-8") for k, v in pairs]
    if audio is not None:
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                   f'filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode("utf-8")
                   + audio + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode("utf-8"))
    shown = f'"{boundary}"' if quoted else boundary
    return b"".join(out), f"multipart/form-data; boundary={shown}"


def ask(port: int, method: str, path: str, body: bytes | None = None, ctype: str | None = None,
        timeout: float = 10.0) -> tuple:
    """-> (status, content type, body). A request that gets no whole answer -
    refused, reset, timed out, or a status whose body was cut off - is status
    0, never an exception that ends the suite before its tally."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method=method,
                                 headers={"Content-Type": ctype} if ctype else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        with e:
            try:
                return e.code, e.headers.get("Content-Type", ""), \
                    e.read().decode("utf-8", "replace")
            except (OSError, http.client.HTTPException) as cut:
                return 0, f"{e.code}, then {type(cut).__name__}", ""
    except (OSError, http.client.HTTPException) as e:
        return 0, type(e).__name__, ""


def transcribe(port: int, prefix: str, fields: dict, audio: bytes | None = None,
               timeout: float = 10.0, route: str = "/inference") -> tuple:
    body, ctype = form(fields, wav() if audio is None else audio)
    return ask(port, "POST", prefix + route, body, ctype, timeout)


def verbose(port: int, prefix: str, audio: bytes | None = None, **fields) -> dict:
    status, _, body = transcribe(port, prefix, dict(response_format="verbose_json", **fields),
                                 audio)
    if status != 200:
        raise AssertionError(f"a transcription answered {status}: {body!r}")
    return json.loads(body)


def records(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_bytes().decode("utf-8").splitlines()
            if line.strip()]


def cmdlines_containing(needle: str) -> list | None:
    """(pid, command line) of every process whose command line holds *needle*
    - or None when the process list could not be asked: a query that did not
    answer, or answered with an error, is never "nothing found" (on a busy
    machine one read a live child as dead). BY COMMAND LINE, NEVER BY START
    TIME."""
    if sys.platform != "win32":
        found = []
        for d in Path("/proc").glob("[0-9]*"):
            try:
                cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            except OSError:
                continue
            if needle in cmd:
                found.append((int(d.name), cmd.strip()))
        return found
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "
             f"'*{needle}*' }} | ForEach-Object {{ \"$($_.ProcessId)`t$($_.CommandLine)\" }}"],
            capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
    except Exception:                                           # noqa: BLE001
        return None
    if done.returncode != 0:
        return None
    found = []
    for line in done.stdout.splitlines():
        pid, _, cmd = line.partition("\t")
        if pid.strip().isdigit() and "powershell" not in cmd.lower():
            found.append((int(pid.strip()), cmd.strip()))
    return found


def end_listed(listed, mark: str) -> list:
    """End every process of *listed* ((pid, command line), a listing's) that
    carries *mark* - NEVER BY A PID READ FROM A LISTING ALONE (H4's R5): a pid
    is handed on the moment its process ends, so between the listing and the
    kill it may be somebody else's. Each is held open first - a pid cannot be
    handed on while a handle to its process is open - and the list is asked
    for again: only a process held open AND still listed carrying the mark is
    ended, through the handle held, and waited for. -> the pids ended."""
    pids = sorted({pid for pid, cmd in listed if mark in cmd})
    if not pids:
        return []
    if sys.platform != "win32":
        again = asked_again(lambda: cmdlines_containing(mark), lambda got: got is not None,
                            "the process list, before anything is ended")
        ended = [pid for pid, _ in again if pid in pids]
        for pid in ended:
            os.kill(pid, 9)
        return ended
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    # PROCESS_TERMINATE | SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
    held = {pid: h for pid in pids if (h := k32.OpenProcess(0x0001 | 0x00100000 | 0x1000,
                                                             False, pid))}
    try:
        again = asked_again(lambda: cmdlines_containing(mark), lambda got: got is not None,
                            "the process list, with what it named held open")
        ended = []
        for pid in sorted({pid for pid, cmd in again if mark in cmd and pid in held}):
            k32.TerminateProcess(held[pid], 1)
            if k32.WaitForSingleObject(held[pid], int(KILL_WAIT_S * 1000)) != 0:
                raise AssertionError(f"pid {pid} ({mark}) was killed and had not ended "
                                     f"{KILL_WAIT_S:g}s later")
            ended.append(pid)
        return ended
    finally:
        for h in held.values():
            k32.CloseHandle(h)


@contextlib.contextmanager
def suspended(proc):
    """The process frozen whole - no thread of it runs, so nothing accepts a
    connection - and thawed on the way out, whatever happened inside."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    nt = ctypes.WinDLL("ntdll")
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    nt.NtSuspendProcess.argtypes = [wintypes.HANDLE]
    nt.NtResumeProcess.argtypes = [wintypes.HANDLE]
    # the pid cannot have been handed on: this suite still holds the process's
    # own handle (its Popen), and a process object lives while a handle does
    handle = k32.OpenProcess(0x0800, False, proc.pid)          # PROCESS_SUSPEND_RESUME
    if not handle:
        raise AssertionError(f"could not open the stand-in to suspend it: {ctypes.get_last_error()}")
    try:
        nt.NtSuspendProcess(handle)
        try:
            yield
        finally:
            nt.NtResumeProcess(handle)
    finally:
        k32.CloseHandle(handle)


class Stand:
    """One stand-in this suite started: its process, its log, the marker on
    its command line. It runs in the row's own folder, so a relative path it
    is given - or its default model's - can reach nothing outside it."""

    def __init__(self, args: list, log: Path):
        self.marker = f"{RUN}-{len(LAUNCHED)}"
        self.log_path = log
        self._log = open(log, "wb")
        self.proc = subprocess.Popen([sys.executable, str(FAKE), "--tmp-dir", self.marker,
                                      *[str(a) for a in args]],
                                     stdout=self._log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, cwd=log.parent,
                                     creationflags=NO_WINDOW)
        LAUNCHED.append((self.proc, self.marker))

    def exited(self, timeout: float):
        """Its exit code once it has exited, or None if it is still running
        after `timeout` seconds."""
        try:
            return self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def log(self) -> str:
        return self.log_path.read_bytes().decode("utf-8", "replace")

    def end(self) -> None:
        """Ended through the handle it was started with, and waited for."""
        try:
            killed(self.proc)
        finally:
            self._log.close()


def wait_ready(stand: Stand, port: int, prefix: str, patience: float = 20.0) -> None:
    deadline = time.monotonic() + patience
    while time.monotonic() < deadline:
        if stand.proc.poll() is not None:
            raise AssertionError(f"the stand-in exited {stand.proc.returncode} before it was "
                                 f"ready; its log:\n{stand.log()}")
        if accepts(port) and ask(port, "GET", prefix + "/health", timeout=5)[0] == 200:
            return
        time.sleep(0.05)
    raise AssertionError(f"the stand-in was not ready after {patience} s; its log:\n{stand.log()}")


class Case(unittest.TestCase):
    """A folder of the row's own, with a model file in it that is a whisper
    model's header and nothing more."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="llmb-whisper-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.model = self.tmp / "ggml-model.bin"
        self.model.write_bytes(ggml_bytes(pad=4096))

    def launch(self, *args) -> Stand:
        stand = Stand(list(args), self.tmp / f"stand-in-{len(LAUNCHED)}.log")
        self.addCleanup(stand.end)
        return stand

    def ready(self, *args, prefix: str | None = None) -> tuple:
        """A stand-in on a port of its own, under a load's path unless told
        otherwise, waited for until health answers -> (stand, port, prefix)."""
        port = quiet_port()
        prefix = "/" + ids.mint_actor("load") if prefix is None else prefix
        path = ["--request-path", prefix] if prefix else []
        stand = self.launch("-m", self.model, "--host", "127.0.0.1", "--port", port, *path, *args)
        wait_ready(stand, port, prefix)
        return stand, port, prefix


# ============================================================ the header ===

class HeaderRows(unittest.TestCase):
    """runner/ggml.py: a whisper model file's header, from synthetic bytes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="llmb-ggml-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, data: bytes, name: str = "model.bin") -> Path:
        path = self.tmp / name
        path.write_bytes(data)
        return path

    def refused(self, data: bytes) -> str:
        with self.assertRaises(GGMLError) as caught:
            read_header(self.write(data))
        return str(caught.exception)

    def test_the_eleven_numbers_are_read_in_whisper_cpps_order_little_endian(self):
        """Every field a different number, none of them its own byte-swap, so
        a field read from another's place or in the other byte order shows."""
        numbers = dict(zip(HEADER_FIELDS, (51866, 1500, 1280, 20, 32, 448, 1024, 16, 24, 128, 7)))
        info = read_header(self.write(ggml_bytes(**numbers)))
        self.assertEqual({k: info[k] for k in HEADER_FIELDS}, numbers)

    def test_it_says_how_big_the_file_is(self):
        """What the memory a load needs is worked out from, until measured."""
        info = read_header(self.write(ggml_bytes(pad=1000)))
        self.assertEqual(info["file_size"], 48 + 1000)

    def test_128_mels_and_32_layers_in_both_stacks_is_large_v3(self):
        self.assertEqual(read_header(self.write(ggml_bytes()))["shape"], "large v3")

    def test_80_mels_and_24_layers_in_both_stacks_is_medium(self):
        self.assertEqual(read_header(self.write(ggml_bytes(**MEDIUM)))["shape"], "medium")

    def test_any_other_shape_is_said_in_its_own_numbers(self):
        """80 mels and 32 layers in both stacks: neither name - said as the
        numbers it has, never a guess at a name."""
        info = read_header(self.write(ggml_bytes(n_mels=80)))
        self.assertEqual(info["shape"], "80 mels, 32 audio layers, 32 text layers")

    def test_large_v3s_mels_and_encoder_with_a_small_decoder_is_not_large_v3(self):
        """Both stacks decide: a file with large v3's 128 mels and 32 audio
        layers but 4 text layers is another model, and is not called large v3."""
        info = read_header(self.write(ggml_bytes(n_text_layer=4)))
        self.assertEqual(info["shape"], "128 mels, 32 audio layers, 4 text layers")

    def test_a_file_without_the_magic_is_refused_naming_the_magic_and_what_it_found(self):
        """A GGUF - llama.cpp's format - named as a whisper model by mistake."""
        text = self.refused(b"GGUF" + struct.pack("<I", 3) + bytes(64))
        self.assertIn("0x67676d6c", text)
        self.assertIn("GGUF", text)

    def test_an_empty_file_is_refused_in_words(self):
        self.assertIn("0x67676d6c", self.refused(b""))

    def test_a_header_cut_short_is_refused_saying_where_it_ends(self):
        self.assertIn("30 of 48 bytes", self.refused(ggml_bytes()[:30]))

    def test_numbers_no_model_has_are_refused_naming_each(self):
        self.assertIn("n_mels is 0", self.refused(ggml_bytes(n_mels=0)))
        self.assertIn("n_audio_layer is -5", self.refused(ggml_bytes(n_audio_layer=-5)))
        self.assertIn("ftype is -1", self.refused(ggml_bytes(ftype=-1)))
        # a file of 32-bit floats has type 0, and is a model
        self.assertEqual(read_header(self.write(ggml_bytes(ftype=0)))["ftype"], 0)

    def test_a_file_that_is_not_there_is_not_called_a_bad_model(self):
        """Not there is not the same as not a whisper model: the one is the
        operating system's answer, the other the file's."""
        with self.assertRaises(OSError) as caught:
            read_header(self.tmp / "not-there.bin")
        self.assertNotIsInstance(caught.exception, GGMLError)


# ============================================================ the fields ===

class FieldsRows(unittest.TestCase):
    """runner/whisper_fields.py: what whisper-server 1.9.1 reads from a request."""

    def test_the_fields_are_the_ones_its_request_parser_reads_and_no_other(self):
        self.assertIsInstance(INFERENCE_FIELDS, tuple)
        self.assertEqual(INFERENCE_FIELDS, SOURCE_FIELDS)
        self.assertEqual(len(set(INFERENCE_FIELDS)), 36)

    def test_the_audio_travels_in_a_part_of_its_own_not_as_a_field(self):
        self.assertEqual(AUDIO_PART, "file")
        self.assertNotIn(AUDIO_PART, INFERENCE_FIELDS)

    def test_the_contracts_audio_request_names_every_field_it_reads_but_the_engines_own(self):
        """S12b R3: the contract's `audio_request` - what an application may
        send - and this list - what whisper-server reads - are one list, less
        `response_format`, which the engine sets itself. A field only the
        list had would be refused at the submit though whisper-server reads
        it; one only the contract had would be sent, and ignored without a
        word. Every one of them optional, in the contract's `optional`."""
        import validate as V
        shapes, _ = V.load(HERE.parent / "llm-bootstrap-contract")
        fields = shapes["audio_request"]["object"]["fields"]
        own = {"response_format"}
        self.assertLessEqual(own, set(INFERENCE_FIELDS), "the engine's own is a field it reads")
        self.assertEqual(sorted(fields["object"]), sorted(set(INFERENCE_FIELDS) - own))
        self.assertEqual(sorted(fields["optional"]), sorted(set(INFERENCE_FIELDS) - own))


# ======================================================= the command line ===

class CommandLineRows(Case):
    def test_an_unknown_argument_prints_the_usage_and_exits_0(self):
        """Measured: exit 0 in 0.06 s, the usage printed. A clean exit is not
        a start."""
        port = quiet_port()
        s = self.launch("--no-such-flag", "x", "-m", self.model, "--port", port)
        self.assertEqual(s.exited(15), 0)
        log = s.log()
        self.assertIn("error: unknown argument: --no-such-flag", log)
        self.assertIn("usage:", log)
        self.assertFalse(accepts(port))

    def test_help_prints_the_usage_and_exits_0(self):
        for flag in ("-h", "--help"):
            s = self.launch(flag, "-m", self.model)
            self.assertEqual(s.exited(15), 0, flag)
            self.assertIn("usage:", s.log(), flag)
            self.assertNotIn("unknown argument", s.log(), flag)

    def test_a_flag_given_no_value_is_refused_like_an_unknown_one(self):
        s = self.launch("-m", self.model, "--port")
        self.assertEqual(s.exited(15), 0)
        self.assertIn("error: --port needs a value", s.log())
        self.assertIn("usage:", s.log())

    def test_every_flag_whisper_server_printed_in_its_usage_is_taken(self):
        """A load may be given any of them in its settings' extra arguments:
        the stand-in starts with every one, as the real server would."""
        args = []
        for flag in REAL_VALUED:
            args += [flag, REAL_VALUES.get(flag, "1")]
        args += list(REAL_SWITCHES) + ["--model", self.model]
        stand, port, prefix = self.ready(*args)
        self.assertIsNone(stand.proc.poll())

    def test_of_the_two_spellings_of_the_model_the_last_given_wins(self):
        missing = self.tmp / "not-there.bin"
        port = quiet_port()
        s = self.launch("--model", missing, "-m", self.model, "--port", port)
        wait_ready(s, port, "")
        s = self.launch("-m", self.model, "--model", missing, "--port", quiet_port())
        self.assertEqual(s.exited(15), 3)

    def test_the_exit_switch_ends_it_at_once_with_its_code_0_included(self):
        for code in (0, 7):
            port = quiet_port()
            s = self.launch("--fake-exit-code", code, "-m", self.model, "--port", port)
            self.assertEqual(s.exited(15), code)
            self.assertFalse(accepts(port))
            self.assertNotIn("usage:", s.log())


# ========================================================= the model file ===

class ModelFileRows(Case):
    def test_a_model_file_that_is_not_there_exits_3_saying_so(self):
        """Measured: exit 3 in 0.05 s - with --tmp-dir and --request-path
        taken first - and the words it printed."""
        port = quiet_port()
        missing = self.tmp / "not-there.bin"
        s = self.launch("--request-path", "/" + ids.mint_actor("load"), "-m", missing,
                        "--port", port)
        self.assertEqual(s.exited(15), 3)
        log = s.log()
        self.assertIn(f"failed to open '{missing}'", log)
        self.assertIn("error: failed to initialize whisper context", log)
        self.assertFalse(accepts(port))

    def test_with_no_model_named_it_looks_for_the_real_servers_default(self):
        """whisper-server's usage: models/ggml-base.en.bin - not there, from
        where this suite runs."""
        s = self.launch("--port", quiet_port())
        self.assertEqual(s.exited(15), 3)
        self.assertIn("failed to open 'models/ggml-base.en.bin'", s.log())

    def test_a_file_that_is_not_a_whisper_model_exits_3_too(self):
        port = quiet_port()
        bad = self.tmp / "model.gguf"
        bad.write_bytes(b"GGUF" + bytes(60))
        s = self.launch("-m", bad, "--port", port)
        self.assertEqual(s.exited(15), 3)
        self.assertIn("invalid model data (bad magic)", s.log())
        self.assertFalse(accepts(port))


# ================================================================ the port ===

class PortRows(Case):
    def test_nothing_listens_until_the_model_is_up_and_then_health_answers_200_at_once(self):
        """Measured: the port free until +2.92 s, then accepting and answering
        200 at once. A process cannot listen before it has started, and it
        started after the spawn: a first accept sooner than the load after the
        spawn is a port taken before the model was up."""
        port = quiet_port()
        prefix = "/" + ids.mint_actor("load")
        t0 = time.monotonic()
        s = self.launch("-m", self.model, "--port", port, "--request-path", prefix,
                        "--fake-load-s", "2")
        first = None
        while time.monotonic() - t0 < 30 and s.proc.poll() is None:
            if accepts(port):
                first = (time.monotonic() - t0, ask(port, "GET", prefix + "/health")[0])
                break
            time.sleep(0.05)
        self.assertIsNotNone(first, f"it never listened; its log:\n{s.log()}")
        self.assertGreaterEqual(first[0], 2.0)
        self.assertEqual(first[1], 200)

    def test_a_port_taken_by_the_time_the_model_is_up_ends_it_with_1(self):
        """The stranger takes the port the way another program's server would:
        with SO_REUSEADDR set, as Python's own http.server sets it. On Windows
        a second socket with that option too could share the port (measured):
        the stand-in must not set it there, or it would never see the stranger."""
        port = quiet_port()
        with socket.socket() as stranger:
            stranger.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            stranger.bind(("127.0.0.1", port))
            stranger.listen()
            s = self.launch("-m", self.model, "--port", port, "--fake-load-s", "0.3")
            self.assertEqual(s.exited(15), 1)
        self.assertIn("couldn't bind", s.log())

    @unittest.skipUnless(sys.platform == "win32", "a full backlog is refused only on Windows; "
                                                  "Linux holds the connection instead")
    def test_it_keeps_a_backlog_of_64_connections(self):
        """Frozen whole, it accepts nothing, so every connection waits in the
        backlog: twenty all get in at 64; at the standard library's 5, fifteen
        time out (measured on this machine)."""
        stand, port, prefix = self.ready()
        socks = []
        try:
            with suspended(stand.proc):
                for _ in range(20):
                    c = socket.socket()
                    c.settimeout(0.5)
                    try:
                        c.connect(("127.0.0.1", port))
                        socks.append(c)
                    except OSError:
                        c.close()
        finally:
            for c in socks:
                c.close()
        self.assertEqual(len(socks), 20)


# ============================================================== the routes ===

class RouteRows(Case):
    def test_every_route_is_under_the_request_path_and_anything_else_is_not_found(self):
        """Measured with --request-path /load-...: its health answers 200
        there, and /health, /inference, a POST outside - 404 "File Not Found
        (<path>)". A stranger's path is anything else."""
        stand, port, prefix = self.ready()
        status, ctype, body = ask(port, "GET", prefix + "/health")
        self.assertEqual((status, json.loads(body)), (200, {"status": "ok"}))
        self.assertTrue(ctype.startswith("application/json"), ctype)
        other = "/" + ids.mint_actor("load")
        for method, path in (("GET", "/health"), ("GET", "/inference"),
                             ("GET", other + "/health"), ("POST", prefix + "/health"),
                             ("GET", prefix + "/inference")):
            got = ask(port, method, path, b"" if method == "POST" else None)
            self.assertEqual((got[0], got[2]), (404, f"File Not Found ({path})"), (method, path))
        # a whole transcription sent outside the path is read, and refused 404 -
        # 8 MB of it: answered without being read, a 64 KB body came through
        # whole 9 times in 10 on this machine, an 8 MB one never (measured)
        got = transcribe(port, "", {"response_format": "json"}, wav(270.0))
        self.assertEqual((got[0], got[2]), (404, "File Not Found (/inference)"), got)

    def test_its_page_is_under_the_request_path_too(self):
        """Measured: GET /<load id>/ answered 200."""
        stand, port, prefix = self.ready()
        status, ctype, body = ask(port, "GET", prefix + "/")
        self.assertEqual(status, 200)
        self.assertTrue(ctype.startswith("text/html"), ctype)
        self.assertEqual(ask(port, "GET", "/")[0], 404)

    def test_with_no_request_path_every_route_is_at_the_root(self):
        stand, port, prefix = self.ready(prefix="")
        self.assertEqual(ask(port, "GET", "/health")[0], 200)
        self.assertEqual(transcribe(port, "", {})[0], 200)

    def test_the_inference_path_moves_the_transcription_and_only_it(self):
        stand, port, prefix = self.ready("--inference-path", "/transcribe")
        self.assertEqual(transcribe(port, prefix, {}, route="/transcribe")[0], 200)
        self.assertEqual(transcribe(port, prefix, {})[0], 404)
        self.assertEqual(ask(port, "GET", prefix + "/health")[0], 200)


# ======================================================= a transcription ===

class TranscriptionRows(unittest.TestCase):
    """One stand-in for every row here, recording what it was sent."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="llmb-whisper-"))
        model = cls.tmp / "ggml-model.bin"
        model.write_bytes(ggml_bytes(pad=4096))
        cls.record = cls.tmp / "record.jsonl"
        cls.port, cls.prefix = quiet_port(), "/" + ids.mint_actor("load")
        cls.stand = Stand(["-m", model, "--host", "127.0.0.1", "--port", cls.port,
                           "--request-path", cls.prefix, "--fake-record", cls.record],
                          cls.tmp / "stand-in.log")
        try:
            wait_ready(cls.stand, cls.port, cls.prefix)
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.stand.end()
        shutil.rmtree(cls.tmp, True)

    def verbose(self, audio: bytes | None = None, **fields) -> dict:
        return verbose(self.port, self.prefix, audio, **fields)

    def post(self, fields: dict, audio: bytes | None = None) -> tuple:
        return transcribe(self.port, self.prefix, fields, audio)

    def test_speech_is_answered_in_the_verbose_shape_measured(self):
        """The keys, in the order whisper-server 1.9.1 answered them, and the
        text: every segment's, each followed by a newline."""
        doc = self.verbose(wav(2.0))
        self.assertEqual(list(doc), ["task", "language", "duration", "text", "segments",
                                     "detected_language", "detected_language_probability",
                                     "language_probabilities"])
        self.assertEqual((doc["task"], doc["language"], doc["duration"]),
                         ("transcribe", "english", 2.0))
        self.assertGreaterEqual(len(doc["segments"]), 2)
        self.assertEqual([s["id"] for s in doc["segments"]], list(range(len(doc["segments"]))))
        for seg in doc["segments"]:
            self.assertEqual(list(seg), ["id", "text", "start", "end", "tokens", "words",
                                         "temperature", "avg_logprob", "no_speech_prob"])
            self.assertTrue(seg["tokens"] and all(isinstance(t, int) for t in seg["tokens"]))
            self.assertTrue(0 <= seg["start"] < seg["end"] <= doc["duration"], seg)
            for word in seg["words"]:
                self.assertEqual(list(word), ["word", "start", "end", "t_dtw", "probability"])
                self.assertTrue(seg["start"] <= word["start"] <= word["end"] <= seg["end"], word)
            self.assertEqual("".join(w["word"] for w in seg["words"]), seg["text"])
        self.assertEqual(doc["text"], "".join(s["text"] + "\n" for s in doc["segments"]))
        for seg in doc["segments"]:
            for word in seg["words"]:
                self.assertIsInstance(word["t_dtw"], int)
                self.assertTrue(0.0 <= word["probability"] <= 1.0, word)
            for key in ("temperature", "avg_logprob", "no_speech_prob"):
                self.assertIsInstance(seg[key], float, key)
        # the source: the detected language's own probability, and every
        # language above 0.001
        self.assertEqual(doc["detected_language"], "english")
        table = doc["language_probabilities"]
        self.assertEqual(doc["detected_language_probability"], table["en"])
        self.assertTrue(table and all(p > 0.001 for p in table.values()), table)

    def test_the_answer_is_compact_json_as_measured(self):
        status, _, body = self.post({"response_format": "verbose_json"}, wav(2.0))
        self.assertTrue(body.startswith('{"task":"transcribe","language":"english",'
                                        '"duration":2.0,"text":" '), body[:80])

    def test_the_duration_is_the_audios_own_at_any_rate(self):
        self.assertEqual(self.verbose(wav(3.5, rate=8000))["duration"], 3.5)

    def test_silence_is_an_empty_text_and_no_segments(self):
        """Measured: 4 s of silence answered "" and no segments (and once,
        not deterministically, " Thank you." - the stand-in keeps to the one)."""
        doc = self.verbose(wav(4.0, sound=False))
        self.assertEqual((doc["text"], doc["segments"], doc["duration"]), ("", [], 4.0))

    def test_silence_at_8_bits_is_silence_too(self):
        """8-bit PCM is unsigned: its silence is 0x80, not zero - and its
        sound is heard."""
        self.assertEqual(self.verbose(wav(1.0, sound=False, width=1))["segments"], [])
        self.assertTrue(self.verbose(wav(1.0, width=1))["segments"])

    def test_with_no_response_format_the_answer_is_the_text_alone(self):
        """json is the server's default: only verbose_json carries segments."""
        status, _, body = self.post({})
        self.assertEqual(status, 200)
        text = self.verbose()["text"]
        self.assertTrue(text)
        self.assertEqual(json.loads(body), {"text": text})

    def test_the_language_table_goes_only_when_the_server_would_parse_true(self):
        """"true", "1", "yes" or "y", exactly: anything else is false."""
        table = ("detected_language", "detected_language_probability", "language_probabilities")
        for value, gone in (("true", True), ("1", True), ("yes", True), ("y", True),
                            ("false", False), ("0", False), ("TRUE", False), ("on", False)):
            doc = self.verbose(no_language_probabilities=value)
            for key in table:
                self.assertEqual(key not in doc, gone, (value, key))

    def test_the_language_is_named_as_the_server_names_it(self):
        """A code it has no name for is answered as it was given."""
        for code, name in (("en", "english"), ("no", "norwegian"), ("auto", "english"),
                           ("xx", "xx")):
            self.assertEqual(self.verbose(language=code)["language"], name, code)

    def test_a_field_the_server_does_not_know_is_ignored_and_arrives(self):
        """Measured: an unknown field answered 200. It changes nothing in the
        answer, and the record shows it was sent all the same - with the
        audio's name and every one of its bytes."""
        plain = self.verbose()
        odd = self.verbose(no_such_field="1")
        self.assertEqual(odd, plain)
        sent = [e for e in records(self.record) if e["event"] == "request"][-1]
        self.assertEqual(sent["fields"].get("no_such_field"), "1")
        self.assertEqual(sent["audio"], {"name": "audio.wav", "bytes": len(wav())})

    def test_a_field_sent_twice_is_read_from_its_first_part(self):
        body, ctype = form([("response_format", "verbose_json"), ("language", "no"),
                            ("language", "en")], wav())
        status, _, text = ask(self.port, "POST", self.prefix + "/inference", body, ctype)
        self.assertEqual((status, json.loads(text)["language"]), (200, "norwegian"))

    def test_a_boundary_in_quotes_is_read_as_well(self):
        body, ctype = form({"response_format": "verbose_json"}, wav(), quoted=True)
        status, _, text = ask(self.port, "POST", self.prefix + "/inference", body, ctype)
        self.assertEqual((status, json.loads(text)["duration"]), (200, 2.0))

    def test_a_request_without_its_audio_part_is_refused_400(self):
        body, ctype = form({"response_format": "verbose_json"}, None)
        status, _, text = ask(self.port, "POST", self.prefix + "/inference", body, ctype)
        self.assertEqual((status, text), (400, "Invalid request"))
        last = records(self.record)[-1]
        self.assertEqual((last["event"], last["status"]), ("refused", 400))

    def test_audio_that_is_not_a_wav_is_refused_400(self):
        status, _, text = self.post({"response_format": "verbose_json"}, b"ID3 not a wav")
        self.assertEqual((status, text), (400, "Invalid request"))

    # ---- times, as 1.9.1's source writes them (S12b: the stand-in's fidelity)

    def test_with_no_timestamps_no_segment_and_no_word_has_times(self):
        """Its verbose answer (1.9.1's source): a segment's start and end are
        written only without no_timestamps, and a word's start, end and t_dtw
        only without it and with token_timestamps - which, not sent, is the
        opposite of no_timestamps."""
        doc = self.verbose(wav(2.0), no_timestamps="true")
        self.assertTrue(doc["segments"])
        for seg in doc["segments"]:
            self.assertEqual(list(seg), ["id", "text", "tokens", "words", "temperature",
                                         "avg_logprob", "no_speech_prob"])
            for word in seg["words"]:
                self.assertEqual(list(word), ["word", "probability"])

    def test_without_token_timestamps_a_segment_keeps_its_times_and_its_words_have_none(self):
        doc = self.verbose(wav(2.0), token_timestamps="false")
        self.assertTrue(doc["segments"])
        for seg in doc["segments"]:
            self.assertEqual(list(seg)[:4], ["id", "text", "start", "end"])
            for word in seg["words"]:
                self.assertEqual(list(word), ["word", "probability"])
        # asked for with no_timestamps, it is still none: both are read
        both = self.verbose(wav(2.0), no_timestamps="true", token_timestamps="true")
        self.assertEqual({tuple(w) for s in both["segments"] for w in s["words"]},
                         {("word", "probability")})

    def test_every_answer_is_a_transcript_the_contract_declares(self):
        """Whatever the fields asked, the answer is the contract's transcript
        - its times there or not, its language table there or not."""
        import validate as V                                # on the path: runner.ggml put it there
        shapes = V.load(HERE.parent / "llm-bootstrap-contract")[0]
        for fields in ({}, {"no_timestamps": "true"}, {"token_timestamps": "false"},
                       {"no_language_probabilities": "true"}):
            with self.subTest(fields=fields):
                self.assertEqual(V.check(self.verbose(wav(2.0), **fields),
                                         {"ref": "transcript"}, shapes), [])
        self.assertEqual(V.check(self.verbose(wav(1.0, sound=False)), {"ref": "transcript"},
                                 shapes), [])


class DefaultRows(Case):
    def test_the_command_lines_language_and_table_are_defaults_and_a_field_wins(self):
        stand, port, prefix = self.ready("-l", "no", "-nlp")
        doc = verbose(port, prefix)
        self.assertEqual(doc["language"], "norwegian")
        self.assertNotIn("language_probabilities", doc)
        doc = verbose(port, prefix, language="en", no_language_probabilities="false")
        self.assertEqual(doc["language"], "english")
        self.assertIn("language_probabilities", doc)


# ================================================================= timing ===

class TimingRows(Case):
    def started(self, record: Path, answered: int = 0, patience: float = 20.0) -> None:
        """Wait until the stand-in has taken a transcription beyond the
        `answered` ones - its record says so - so what the row does next
        happens during it."""
        deadline = time.monotonic() + patience
        while time.monotonic() < deadline:
            if sum(e["event"] == "request" for e in records(record)) > answered:
                return
            time.sleep(0.02)
        raise AssertionError("the transcription never started")

    def test_health_answers_while_a_transcription_runs(self):
        """Measured: in 1-26 ms, all through a 37 s transcription - health
        takes no lock."""
        record = self.tmp / "record.jsonl"
        stand, port, prefix = self.ready("--fake-answer-s", "2", "--fake-record", record)
        done = {}

        def run():
            done["answer"] = transcribe(port, prefix, {}, timeout=30)
            done["at"] = time.monotonic()
        th = threading.Thread(target=run, daemon=True)
        th.start()
        self.started(record)
        polls = []
        while th.is_alive() and len(polls) < 200:
            asked = time.monotonic()
            status = ask(port, "GET", prefix + "/health", timeout=30)[0]
            polls.append((status, asked, time.monotonic()))
            time.sleep(0.1)
        th.join(30)
        self.assertEqual(done["answer"][0], 200)
        # answered while the transcription still ran: a health that waited for
        # it would answer only as it ends
        during = [p for p in polls if p[2] < done["at"] - 0.3]
        self.assertGreaterEqual(len(during), 3, polls)
        self.assertTrue(all(p[0] == 200 for p in during), during)

    def test_one_transcription_at_a_time(self):
        """Two sent at once: the second is taken only once the first has
        been answered."""
        record = self.tmp / "record.jsonl"
        stand, port, prefix = self.ready("--fake-answer-s", "1.5", "--fake-record", record)
        answers = []
        threads = [threading.Thread(target=lambda: answers.append(
            transcribe(port, prefix, {}, timeout=30)[0]), daemon=True) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(30)
        self.assertEqual(answers, [200, 200])
        seen = records(record)
        self.assertEqual([e["event"] for e in seen], ["request", "answered", "request", "answered"])
        # each took its 1.5 s, and the second was taken after the first was answered
        self.assertGreaterEqual(seen[1]["at"] - seen[0]["at"], 1.5)
        self.assertGreaterEqual(seen[3]["at"] - seen[2]["at"], 1.5)
        self.assertGreaterEqual(seen[2]["at"], seen[1]["at"])

    def test_a_death_mid_answer_answers_nothing_and_ends_the_process(self):
        record = self.tmp / "record.jsonl"
        stand, port, prefix = self.ready("--fake-die-after", "1", "--fake-record", record)
        self.assertEqual(transcribe(port, prefix, {})[0], 200)
        self.assertEqual(transcribe(port, prefix, {})[0], 0)
        self.assertEqual(stand.exited(15), 1)
        self.assertIn("dying mid-answer", stand.log())
        self.assertEqual([e["event"] for e in records(record)], ["request", "answered", "request"])

    def test_a_hang_never_answers_while_health_still_does(self):
        record = self.tmp / "record.jsonl"
        stand, port, prefix = self.ready("--fake-hang-after", "1", "--fake-record", record)
        self.assertEqual(transcribe(port, prefix, {})[0], 200)
        self.assertEqual(transcribe(port, prefix, {}, timeout=1.5)[0], 0)
        self.assertEqual(ask(port, "GET", prefix + "/health")[0], 200)
        self.assertIsNone(stand.proc.poll())
        self.assertIn("hanging mid-answer", stand.log())
        self.assertEqual([e["event"] for e in records(record)], ["request", "answered", "request"])

    def test_the_cards_memory_running_out_is_said_in_ggmls_words_and_ends_it(self):
        """`--fake-oom-after N`: N answers, then the card's memory runs out in
        the middle of the next - said in the lines ggml's CUDA backend writes
        when a CUDA call fails for it (read from whisper.cpp 1.9.1's vendored
        ggml: `ggml_cuda_error`), and the process ends (its abort) - no
        answer. Each line is one runner/memlog.py looks for."""
        from runner.memlog import says_out_of_memory
        record = self.tmp / "record.jsonl"
        stand, port, prefix = self.ready("--fake-oom-after", "1", "--fake-record", record)
        self.assertEqual(transcribe(port, prefix, {})[0], 200)
        self.assertEqual(transcribe(port, prefix, {})[0], 0)
        code = stand.exited(15)
        self.assertNotIn(code, (None, 0), "it did not end, or ended as if nothing went wrong")
        said = [line for line in stand.log().splitlines() if says_out_of_memory(line)]
        self.assertEqual(said, ["CUDA error: out of memory"], stand.log())
        self.assertEqual([e["event"] for e in records(record)], ["request", "answered", "request"])


# ======================================== the whisper engine (S8, S12b) ===
#
# PLAN-4 D9, D10, D11: the engine's process - its command line, its proof,
# its start, its give-up and its stop - and its transcription, its deadline
# and its log's memory lines (S12b); started by the runner through its one
# place a model server is started (`runner.llamacpp.SPAWN`), which this suite
# opens to its own stand-ins alone: this interpreter running the whisper
# stand-in, or the llama.cpp one beside it, with this run's token on the
# command line (in the models' folder's name) - every one kept in LAUNCHED,
# so the last row ends it if a row did not. It kills nothing through the
# runner, and its lock and ports are its own.

import atexit                                                   # noqa: E402
import itertools                                                # noqa: E402
import runner.cardlock as _CL                                   # noqa: E402
import runner.llamacpp as L                                     # noqa: E402
import runner.manager as M                                      # noqa: E402
from runner.base import BackendError, Cancelled                 # noqa: E402

LLAMA_FAKE = HERE / "fake_llama_server.py"
_real_spawn = L.SPAWN
#: every launch through the runner's own spawn, in order: its argv
SPAWNED: list = []


def _same(a, b) -> bool:
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def _only_this_runs_stand_ins(args, *a, **k):
    argv = [str(x) for x in args]
    if not (len(argv) >= 2 and _same(argv[0], sys.executable)
            and (_same(argv[1], FAKE) or _same(argv[1], LLAMA_FAKE))
            and RUN in " ".join(argv)):
        raise AssertionError(f"this suite starts nothing but its own stand-ins, carrying its "
                             f"run's token, and was asked to start {argv[:3]}")
    proc = _real_spawn(args, *a, **k)
    SPAWNED.append(argv)
    LAUNCHED.append((proc, RUN))
    return proc


def _kills_nothing(pid):
    raise AssertionError(f"this suite kills nothing through the runner, and was asked to kill "
                         f"pid {pid}")


L.SPAWN = _only_this_runs_stand_ins
M.kill_pid = _kills_nothing
M.free_port = lambda host="127.0.0.1": quiet_port()
_LOCKS = Path(tempfile.mkdtemp(prefix="llmb-locks-"))
_LOCK_N = itertools.count()
_CL.machine_lock = lambda: _LOCKS / f"card-{next(_LOCK_N)}.lock"
atexit.register(shutil.rmtree, _LOCKS, True)


def tiny_gguf(path: Path) -> Path:
    """A GGUF header and nothing more: all llama.cpp's stand-in asks of it."""
    key, value = b"general.architecture", b"llama"
    path.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + struct.pack("<Q", len(key)) + key
                     + struct.pack("<I", 8) + struct.pack("<Q", len(value)) + value)
    return path


def raised_by(fn):
    """What *fn* raised, or None."""
    try:
        fn()
    except BaseException as exc:                            # noqa: BLE001 - the row reads it
        return exc
    return None


def give_up_timed(backend, under_way, patience: float = 20.0) -> tuple:
    """Start *backend* on a thread of its own, give its load up once
    `under_way()` says so, and time how long its `start` takes to end after
    that -> (seconds, what it raised). Bounded: a start that never ends is None
    seconds, never a hung suite."""
    from runner import base
    got: dict = {}

    def run():
        got["raised"] = raised_by(backend.start)
        got["ended"] = time.monotonic()
    t = threading.Thread(target=run, daemon=True, name="give-up-timed")
    t.start()
    deadline = time.monotonic() + patience
    while not under_way() and time.monotonic() < deadline and t.is_alive():
        time.sleep(0.01)
    asked = time.monotonic()
    backend.abandon("the pipeline is closing")
    t.join(base.GIVE_UP_S + 10.0)
    if "ended" not in got:
        return None, None
    return got["ended"] - asked, got["raised"]


class NativeWhisperEngineRows(unittest.TestCase):
    """whisper-server run by the runner, as the stand-in behaves: its command
    line carries the load's own path, it is ready when health answers under
    that path, and it is proven ours - or refused - by it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="llmb-whisper-engine-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.models = self.tmp / f"{RUN}-models"          # the run's token, on every command line
        self.models.mkdir()
        (self.models / "ears.bin").write_bytes(ggml_bytes(pad=4096))

    def backend(self, *extra, file="ears.bin", starts=None, spec=None, **cfg):
        from runner.config import ModelSpec
        from runner.whisper import WhisperBackend
        spec = ModelSpec("ears", engine="whisper", file=file, extra_args=list(extra),
                         **(spec or {}))
        runner_cfg = {"whisper_server": [sys.executable, str(FAKE)], "startup_timeout_s": 20.0,
                      "health_poll_s": 0.05, "shutdown_timeout_s": 10.0, **cfg}
        b = WhisperBackend(spec, self.models, runner_cfg, self.tmp / "logs", quiet_port(),
                           starts=starts)
        b.load_id = ids.mint_actor("load")
        self.addCleanup(lambda: raised_by(b.stop) and b._hard_stop())
        return b

    def test_its_command_line_is_its_program_its_model_its_port_and_its_load_on_its_path(self):
        """[S12b, on purpose: `-t <threads>` joined it, D11's command line.]"""
        b = self.backend("--fake-load-s", "0")
        self.assertEqual(b.build_command(),
                         [sys.executable, str(FAKE), "-m", str(self.models / "ears.bin"),
                          "--host", "127.0.0.1", "--port", str(b.port),
                          "--request-path", f"/{b.load_id}", "-t", "4", "--fake-load-s", "0"])

    def test_its_threads_and_its_voice_detector_are_settings_and_its_extra_args_come_last(self):
        """D11 (S8 R7): `-t <threads>` and `--vad -vm <vad>` from the model's
        own settings - the detector's file in the models' folder - then the
        model's extra arguments; and it starts on them."""
        (self.models / "silero.bin").write_bytes(b"a voice detector")
        b = self.backend("-fa", spec={"threads": 8, "vad": "silero.bin"})
        tail = ["--request-path", f"/{b.load_id}", "-t", "8", "--vad", "-vm",
                str(self.models / "silero.bin"), "-fa"]
        self.assertEqual(b.build_command()[8:], tail)
        b.start()
        self.assertEqual(SPAWNED[-1][8:], tail, "it was launched on another command line")
        self.assertTrue(b.is_alive())

    def test_a_voice_detector_that_is_not_there_is_refused_before_anything_starts(self):
        b = self.backend(spec={"vad": "gone-vad.bin"})
        before = len(SPAWNED)
        with self.assertRaises(BackendError) as caught:
            b.start()
        self.assertIn("voice detector file not found", str(caught.exception))
        self.assertIn("gone-vad.bin", str(caught.exception))
        self.assertEqual(len(SPAWNED), before, "a process was started for a detector not there")

    def test_a_file_that_is_not_a_whisper_model_is_refused_by_its_header_before_it_starts(self):
        """D11: the file's header - the ggml magic, eleven numbers - is read
        before the launch; a GGUF, or a download cut short, is refused in
        words, with nothing started."""
        tiny_gguf(self.models / "story.gguf")
        (self.models / "cut.bin").write_bytes(ggml_bytes()[:20])
        for name, said in (("story.gguf", "is not a whisper model"), ("cut.bin", "cut short")):
            with self.subTest(file=name):
                b = self.backend(file=name)
                before = len(SPAWNED)
                with self.assertRaises(BackendError) as caught:
                    b.start()
                self.assertIn(said, str(caught.exception))
                self.assertIn(f"[models.ears] file", str(caught.exception))
                self.assertEqual(len(SPAWNED), before, "a process was started for a file that "
                                                       "is not a whisper model")
        b = self.backend()
        b.start()
        self.assertEqual((b.header["n_mels"], b.header["shape"]), (128, "large v3"))

    def test_its_own_startup_timeout_bounds_its_start(self):
        """The model's `startup_timeout_s`, where it names one, over the
        runner's (20 s here): a start not ready within it fails, saying so."""
        b = self.backend("--fake-load-s", "30", spec={"startup_timeout_s": 1.0})
        t0 = time.monotonic()
        with self.assertRaises(BackendError) as caught:
            b.start()
        self.assertLess(time.monotonic() - t0, 10.0, "the runner's timeout was used")
        self.assertIn("not ready within 1s", str(caught.exception))
        self.assertIsNone(b.proc)

    def test_a_failed_start_is_not_tried_again_within_its_retry_after(self):
        """D11: a start that launched whisper-server and failed is not tried
        again for `retry_after_s` - refused at once, nothing started, saying
        when and what the failed one said - and is tried once that has
        passed. The failed starts are the manager's (`starts`): they outlive
        the load that failed."""
        starts: dict = {}
        with self.assertRaises(BackendError) as first:
            self.backend("--fake-exit-code", "3", starts=starts).start()
        self.assertIn("exited with code 3", str(first.exception))
        before = len(SPAWNED)
        again = self.backend(starts=starts)
        with self.assertRaises(BackendError) as caught:
            again.start()
        said = str(caught.exception)
        self.assertIn("ears is not started again yet: its last start failed", said)
        self.assertIn("[models.ears] retry_after_s", said)
        self.assertIn("exited with code 3", said, "what the failed start said was not said")
        self.assertEqual(len(SPAWNED), before, "a start was tried within its retry_after_s")
        later = self.backend(starts=starts, spec={"retry_after_s": 0.3})
        time.sleep(0.4)
        later.start()
        self.assertEqual(len(SPAWNED), before + 1)
        self.assertTrue(later.is_alive())
        # up again: a failure after it counts afresh
        self.assertIsNone(raised_by(self.backend(starts=starts).start))

    def test_a_given_up_start_is_not_a_failed_one(self):
        """A load given up was not a start that failed: the next is tried at
        once."""
        starts: dict = {}
        b = self.backend("--fake-load-s", "60", starts=starts)
        took, raised = give_up_timed(b, lambda: b.proc is not None)
        self.assertIn("given up", str(raised))
        self.assertIsNone(raised_by(self.backend(starts=starts).start))

    # ----------------------------------------------------------- the answer

    def audio(self, name: str = "take.wav", seconds: float = 2.0, sound: bool = True) -> str:
        path = self.tmp / name
        path.write_bytes(wav(seconds, sound=sound))
        return str(path)

    def answer(self, b, request, cancel=None) -> list:
        from runner.base import GenParams
        return list(b.stream(request, GenParams(), cancel))

    def test_a_transcription_answers_its_text_tokens_transcript_and_the_audios_length(self):
        """D11: `{"audio": <a WAV>, "fields": {...}}` - every field sent as
        whisper-server's parser reads it, and `response_format` the engine's
        own - answered in one piece: its text, its tokens, `stop`, the
        transcript whole (the contract's), and the audio's own length."""
        import validate as V
        record = self.tmp / "record.jsonl"
        b = self.backend("--fake-record", str(record))
        b.start()
        take = self.audio(seconds=2.0)
        events = self.answer(b, {"audio": take, "fields": {"language": "no", "beam_size": 5,
                                                           "temperature": 0.2,
                                                           "no_language_probabilities": True}})
        self.assertEqual([e["type"] for e in events], ["done"])
        done = events[0]
        doc = done["transcript"]
        shapes = V.load(HERE.parent / "llm-bootstrap-contract")[0]
        self.assertEqual(V.check(doc, {"ref": "transcript"}, shapes), [])
        self.assertEqual((done["finish_reason"], done["audio_s"], doc["language"]),
                         ("stop", 2.0, "norwegian"))
        self.assertTrue(done["text"].strip())
        self.assertEqual(done["text"], doc["text"])
        self.assertEqual(done["tokens"], sum(len(s["tokens"]) for s in doc["segments"]))
        self.assertGreater(done["tokens"], 0)
        self.assertNotIn("language_probabilities", doc, "a field asked for was not sent")
        sent = [e for e in records(record) if e["event"] == "request"][-1]
        self.assertEqual(sent["fields"], {"language": "no", "beam_size": "5",
                                          "temperature": "0.2",
                                          "no_language_probabilities": "true",
                                          "response_format": "verbose_json"})
        self.assertEqual(sent["audio"]["bytes"], Path(take).stat().st_size)

    def test_silence_is_an_answer_with_no_words(self):
        b = self.backend()
        b.start()
        done = self.answer(b, {"audio": self.audio(sound=False)})[-1]
        self.assertEqual((done["type"], done["text"], done["tokens"], done["audio_s"]),
                         ("done", "", 0, 2.0))

    def test_whisper_servers_refusal_is_said_in_its_own_words(self):
        """A WAV this engine can measure but whisper-server refuses - here
        float samples, which the stand-in does not decode - fails saying what
        whisper-server said, and its status."""
        b = self.backend()
        b.start()
        rate, n = 16000, 16000
        fmt = struct.pack("<HHIIHH", 3, 1, rate, rate * 4, 4, 32)
        data = b"\0\0\0\0" * n
        body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt \
            + b"data" + struct.pack("<I", len(data)) + data
        path = self.tmp / "float.wav"
        path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
        with self.assertRaises(BackendError) as caught:
            self.answer(b, {"audio": str(path)})
        self.assertEqual(str(caught.exception),
                         "whisper-server refused the transcription (HTTP 400): Invalid request")

    def test_an_answer_the_contract_does_not_declare_is_a_failure_never_an_answer(self):
        """The transcript is held to the contract's shape whole: an answer
        without segments - whatever else it says - fails, saying where."""
        b = self.backend()
        b.start()
        b._transcribe = lambda *a, **k: {"text": " rain\n"}
        with self.assertRaises(BackendError) as caught:
            self.answer(b, {"audio": self.audio()})
        self.assertIn("whisper-server answered a transcript the contract does not declare",
                      str(caught.exception))

    def test_its_deadline_is_its_base_and_its_audio_and_the_time_left_of_one_under_way(self):
        """D3, the one-piece seam (`answer_deadline_s`): `due_base_s` +
        `due_per_audio_s` for each second of the audio; and whisper-server
        takes one at a time, so one asked while another runs on the load is
        due after that one's time left too."""
        from runner.base import GenParams
        record = self.tmp / "record.jsonl"
        b = self.backend("--fake-answer-s", "4", "--fake-record", str(record),
                         spec={"due_base_s": 10.0, "due_per_audio_s": 2.0})
        b.start()
        take = self.audio(seconds=3.0)
        self.assertEqual(b.answer_deadline_s({"audio": take}, GenParams()), 16.0)
        first = threading.Thread(target=lambda: self.answer(b, {"audio": self.audio(
            "one.wav", seconds=1.0)}), daemon=True)
        first.start()
        deadline = time.monotonic() + 20
        while not records(record) and time.monotonic() < deadline:
            time.sleep(0.02)
        due = b.answer_deadline_s({"audio": take}, GenParams())
        self.assertGreater(due, 16.0 + 8.0, "the answer under way was not counted")
        self.assertLessEqual(due, 16.0 + 12.0)
        first.join(20)
        self.assertEqual(b.answer_deadline_s({"audio": take}, GenParams()), 16.0,
                         "an answer that ended was still counted")

    def test_an_answer_past_its_deadline_is_given_up_and_says_so(self):
        """At its deadline the answer is given up - its socket closed, which
        whisper-server hears as its client gone - and fails saying why:
        never a wait past the time it declared."""
        b = self.backend("--fake-answer-s", "30",
                         spec={"due_base_s": 1.0, "due_per_audio_s": 0.0})
        b.start()
        t0 = time.monotonic()
        with self.assertRaises(BackendError) as caught:
            self.answer(b, {"audio": self.audio()})
        took = time.monotonic() - t0
        self.assertGreaterEqual(took, 0.9)
        self.assertLess(took, 5.0, "the answer was waited for past its deadline")
        self.assertIn("did not answer within its deadline: 1s for 2s of audio", str(caught.exception))
        self.assertTrue(b.is_alive(), "a late answer is not a dead model")

    def test_a_cancel_ends_the_answer_at_once(self):
        record = self.tmp / "record.jsonl"
        b = self.backend("--fake-answer-s", "30", "--fake-record", str(record))
        b.start()
        cancel, got = threading.Event(), {}

        def run():
            got["raised"] = raised_by(lambda: self.answer(b, {"audio": self.audio()}, cancel))
            got["at"] = time.monotonic()
        t = threading.Thread(target=run, daemon=True)
        t.start()
        deadline = time.monotonic() + 20
        while not records(record) and time.monotonic() < deadline:
            time.sleep(0.02)
        asked = time.monotonic()
        cancel.set()
        t.join(10)
        self.assertFalse(t.is_alive(), "the answer went on after its cancel")
        self.assertIsInstance(got["raised"], Cancelled)
        self.assertLess(got["at"] - asked, 2.0)

    def test_the_cards_memory_running_out_is_read_from_its_own_log_each_line_once(self):
        """D3, S5's seam (`Backend.memory_errors`): the lines of this load's
        own log that say the card's memory ran out - ggml's, as whisper.cpp
        1.9.1 writes them - each once."""
        b = self.backend("--fake-oom-after", "0")
        b.start()
        self.assertEqual(b.memory_errors(), [])
        with self.assertRaises(BackendError):
            self.answer(b, {"audio": self.audio()})
        deadline = time.monotonic() + 15
        said: list = []
        while not said and time.monotonic() < deadline:
            said = b.memory_errors()
            time.sleep(0.05)
        self.assertEqual(said, ["CUDA error: out of memory"])
        self.assertEqual(b.memory_errors(), [], "a line was said twice")

    def test_a_load_is_ready_when_health_answers_under_its_own_path_and_is_ours_by_it(self):
        from runner.engines import is_ours
        b = self.backend()
        b.start()
        self.assertTrue(b.is_alive())
        self.assertEqual(ask(b.port, "GET", f"/{b.load_id}/health")[0], 200)
        self.assertEqual(ask(b.port, "GET", "/health")[0], 404,
                         "a route answered outside the load's own path")
        line = subprocess.list2cmdline(SPAWNED[-1])          # as the operating system holds it
        program = [sys.executable, str(FAKE)]
        model = self.models / "ears.bin"
        self.assertTrue(is_ours(line, program, model, b.port, b.load_id, engine="whisper"))
        self.assertFalse(is_ours(line, program, model, b.port, ids.mint_actor("load"),
                                 engine="whisper"), "another load's id was taken for its own")

    def test_any_exit_before_it_is_ready_is_a_failed_start_a_clean_one_included(self):
        """M0: whisper-server exits 0 on an argument it does not know - a clean
        exit is not a start - and the failure says the log's last lines."""
        for code in ("0", "3"):
            with self.subTest(code=code):
                b = self.backend("--fake-exit-code", code)
                with self.assertRaises(BackendError) as caught:
                    b.start()
                said = str(caught.exception)
                self.assertIn(f"exited with code {code} before it was ready", said)
                self.assertIn(f"exiting with {code} as asked", said)
                self.assertIsNone(b.proc)

    def test_a_model_file_that_is_not_there_is_refused_before_anything_starts(self):
        b = self.backend(file="gone.bin")
        before = len(SPAWNED)
        with self.assertRaises(BackendError) as caught:
            b.start()
        self.assertIn("model file not found", str(caught.exception))
        self.assertEqual(len(SPAWNED), before, "a process was started for a file not there")

    def test_a_stranger_on_its_port_answers_404_and_the_start_is_refused_and_ours_ended(self):
        """D11: nothing of a whisper load listens until its model is up, so a
        stranger can take its port meanwhile. At the load's own path it
        answers 404: the start is refused, and our process ended - never a
        load handed to a stranger."""
        b = self.backend("--fake-load-s", "30")
        got: dict = {}
        t = threading.Thread(target=lambda: got.update(r=raised_by(b.start)), daemon=True)
        t.start()
        deadline = time.monotonic() + 20
        while b.proc is None and time.monotonic() < deadline:
            time.sleep(0.01)
        ours = b.proc
        self.assertIsNotNone(ours, "our process never started")
        stranger = Stand(["-m", self.models / "ears.bin", "--host", "127.0.0.1", "--port", b.port],
                         self.tmp / "stranger.log")
        self.addCleanup(stranger.end)
        t.join(20)
        self.assertFalse(t.is_alive(), "the start never ended")
        self.assertIsInstance(got["r"], BackendError)
        self.assertIn(f"answers 404 at /{b.load_id}/health: it is not {b.load_id}", str(got["r"]))
        self.assertIsNotNone(ours.poll(), "our process was left running")
        self.assertIsNone(stranger.proc.poll(), "the stranger was touched")

    def test_a_give_up_ends_its_start_within_the_rule(self):
        """S7 R3, the rule every engine is held to (`base.GIVE_UP_S`): a load
        given up ends its start at once - its process ended - well inside the
        time a pipeline's close waits for it."""
        from runner import base
        b = self.backend("--fake-load-s", "60")
        took, raised = give_up_timed(b, lambda: b.proc is not None)
        self.assertIsNotNone(took, "the start never ended")
        self.assertLess(took, base.GIVE_UP_S)
        self.assertIsInstance(raised, BackendError)
        self.assertIn("given up", str(raised))
        self.assertIsNone(b.proc)

    def test_a_stop_ends_its_process_and_frees_its_port(self):
        b = self.backend()
        b.start()
        proc = b.proc
        b.stop()
        self.assertIsNotNone(proc.poll())
        self.assertFalse(accepts(b.port))
        self.assertFalse(b.is_alive())

    def test_a_real_runner_runs_each_model_on_its_own_engine_side_by_side(self):
        """`--backend real`: llama.cpp's stand-in for the text model and
        whisper's for the transcriber, each its own process with its own
        proof, side by side on the card the machine's lock holds (the suite's
        own lock file) - and the record names each load's engine."""
        from runner import Config, ModelManager, read_events
        tiny_gguf(self.models / "story.gguf")
        settings = self.tmp / "real.toml"
        py = Path(sys.executable).as_posix()
        settings.write_text(
            '[runner]\nbackend = "real"\n'
            f'llama_server = ["{py}", "{LLAMA_FAKE.as_posix()}"]\n'
            f'whisper_server = ["{py}", "{FAKE.as_posix()}"]\n'
            f'models_dir = "{self.models.as_posix()}"\n'
            "startup_timeout_s = 20.0\nhealth_poll_s = 0.05\nshutdown_timeout_s = 10.0\n"
            "card_patience_s = 5.0\n\n"
            '[models.story]\nfile = "story.gguf"\n\n'
            '[models.ears]\nengine = "whisper"\nfile = "ears.bin"\n', encoding="utf-8")
        m = ModelManager(Config.load(settings), self.tmp / "bound", vram=lambda: None)
        self.addCleanup(m.shutdown)
        story = m.acquire("story", "srv-w5r8t2hh")
        ears = m.acquire("ears", "srv-w5r8t2hh")
        self.assertTrue(story.is_alive() and ears.is_alive())
        launched = {Path(argv[1]).name: argv for argv in SPAWNED[-2:]}
        self.assertEqual(sorted(launched), ["fake_llama_server.py", "fake_whisper_server.py"])
        self.assertIn(story.load_id, launched["fake_llama_server.py"])
        heard = launched["fake_whisper_server.py"]
        self.assertEqual(heard[heard.index("--request-path") + 1], f"/{ears.load_id}")
        events = read_events(m.events_path)
        oks = {e["model_key"]: e for e in events if e["event"] == "load_ok"}
        self.assertEqual((oks["story"]["engine"], oks["ears"]["engine"]), ("llamacpp", "whisper"))
        # ADDED (S11b, PLAN-4 D12): each start timed in the steps its engine
        # shows - whisper-server listens only once its model is up, on this
        # load's own path, so its port, health and identity are one step
        steps = {e["model_key"]: list(e["measures"]) for e in m.stat_lines()
                 if e["what"] == "load"}
        self.assertEqual(steps, {"story": ["load_wait_s", "load_spawn_s", "load_port_s",
                                           "load_healthy_s", "load_identity_s", "load_s"],
                                 "ears": ["load_spawn_s", "load_healthy_s", "load_s"]})
        self.assertEqual({e.get("source") for e in events if e["event"] == "card_granted"},
                         {"machine lock"})
        m.shutdown()
        self.assertFalse(story.is_alive() or ears.is_alive())

    def test_a_real_runners_failed_start_outlives_its_load_and_holds_the_next_off(self):
        """The failed starts are the manager's (D11): a load of the model
        after one that failed - a new load, a new backend - is refused at once
        within `retry_after_s`, nothing started, saying what the failed one
        said."""
        from runner import Config, ModelManager
        settings = self.tmp / "real.toml"
        py = Path(sys.executable).as_posix()
        settings.write_text(
            '[runner]\nbackend = "real"\n'
            f'whisper_server = ["{py}", "{FAKE.as_posix()}"]\n'
            f'models_dir = "{self.models.as_posix()}"\n'
            "startup_timeout_s = 20.0\nhealth_poll_s = 0.05\nshutdown_timeout_s = 10.0\n"
            "card_patience_s = 5.0\n\n"
            '[models.ears]\nengine = "whisper"\nfile = "ears.bin"\n'
            'extra_args = ["--fake-exit-code", "3"]\n', encoding="utf-8")
        m = ModelManager(Config.load(settings), self.tmp / "bound", vram=lambda: None)
        self.addCleanup(m.shutdown)
        first = raised_by(lambda: m.acquire("ears", "srv-w5r8t2hh"))
        self.assertIn("exited with code 3", str(first))
        before = len(SPAWNED)
        again = raised_by(lambda: m.acquire("ears", "srv-w5r8t2hh"))
        self.assertIn("ears is not started again yet", str(again))
        self.assertIn("exited with code 3", str(again))
        self.assertEqual(len(SPAWNED), before, "a load was started within its retry_after_s")

    def test_a_real_runner_answers_through_its_one_watched_path_and_hears_the_card_run_out(self):
        """`ModelManager.answer`, the one path every generation takes (S5):
        a transcription answered through it whole; the next, as the card's
        memory runs out mid-answer, fails as the card over-full - `Degraded`,
        in ggml's own line - never as a model that merely went away."""
        from runner import Config, ModelManager
        from runner.base import GenParams
        settings = self.tmp / "real.toml"
        py = Path(sys.executable).as_posix()
        settings.write_text(
            '[runner]\nbackend = "real"\n'
            f'whisper_server = ["{py}", "{FAKE.as_posix()}"]\n'
            f'models_dir = "{self.models.as_posix()}"\n'
            "startup_timeout_s = 20.0\nhealth_poll_s = 0.05\nshutdown_timeout_s = 10.0\n"
            "card_patience_s = 5.0\n\n"
            '[models.ears]\nengine = "whisper"\nfile = "ears.bin"\n'
            'extra_args = ["--fake-oom-after", "1"]\n', encoding="utf-8")
        m = ModelManager(Config.load(settings), self.tmp / "bound", vram=lambda: None)
        self.addCleanup(m.shutdown)
        ears = m.acquire("ears", "srv-w5r8t2hh")
        take = {"audio": self.audio(seconds=1.0), "fields": {"language": "en"}}
        done = list(m.answer(ears, take, GenParams()))[-1]
        self.assertEqual((done["type"], done["audio_s"]), ("done", 1.0))
        self.assertTrue(done["text"].strip())
        with self.assertRaises(M.Degraded) as caught:
            list(m.answer(ears, take, GenParams()))
        self.assertIn("its log said the card's memory ran out: CUDA error: out of memory",
                      str(caught.exception))


# ============================================================ its ports ===

class NativeQuietPortRows(unittest.TestCase):
    def test_every_port_handed_out_is_of_this_copys_own_block(self):
        """Every copy of this suite on the machine takes its ports from one
        range - a sweep's copies, a gate beside another agent's - and two that
        bind-checked one free port in the same moment both launched on it. So
        each copy claims a block of the range for as long as it runs
        (`_suites.QuietPorts`, whose rows are the llama.cpp suite's) and hands
        out only ports of its block."""
        first, last = quiet_ports(*QUIET_PORTS).block
        ports = {quiet_port() for _ in range(50)}
        self.assertTrue(ports <= set(range(first, last + 1)),
                        f"{sorted(ports - set(range(first, last + 1)))} are not of this "
                        f"copy's block {first}-{last}")


# ===================================================== a machine that is busy ===

class NativeBusyMachineRows(unittest.TestCase):
    """What the process list could not answer is never "nothing found": on a
    busy machine its query did not answer, and a row read that as its answer
    (wave 3). Provoked here, the query stood in for."""

    def listing_when(self, run, went: str):
        """`cmdlines_containing`, its query answered by *run* - which must go
        the way the row provokes (`went`): a stand-in that itself failed
        reads as a query that did not answer, and a row proves nothing."""
        module, real, said = sys.modules[__name__], subprocess, []

        class Busy:
            def __getattr__(self, name):
                return getattr(real, name)

        def asked(args, *a, **k):
            try:
                got = run(real, args, *a, **k)
            except real.TimeoutExpired:
                said.append("did not answer")
                raise
            said.append("answered")
            return got
        busy = Busy()
        busy.run = asked
        self.addCleanup(setattr, module, "subprocess", real)
        module.subprocess = busy
        listed = cmdlines_containing("llmb-busy")
        self.assertEqual(said, [went], "the query was not asked as the row provoked")
        return listed

    @unittest.skipUnless(sys.platform == "win32", "the process list is a query on Windows")
    def test_a_listing_the_process_list_could_not_give_is_none_never_empty(self):
        def not_answering(real, args, *a, **k):
            raise real.TimeoutExpired(args, k.get("timeout"))
        self.assertIsNone(self.listing_when(not_answering, "did not answer"),
                          "a query that did not answer read as nothing found")

    @unittest.skipUnless(sys.platform == "win32", "the process list is a query on Windows")
    def test_a_listing_answered_with_an_error_is_none_never_empty(self):
        self.assertIsNone(self.listing_when(lambda real, args, *a, **k: real.CompletedProcess(
            args, 1, "", "Get-CimInstance : Call cancelled"), "answered"),
            "an error read as nothing found")


# ================================================ ended from a listing ===

class NativeEndListedRows(unittest.TestCase):
    def test_a_listed_pid_is_ended_only_while_it_still_carries_the_mark(self):
        """H4's R5: the last row ended by the pid a listing gave. A pid is
        handed on the moment its process ends - on Windows 91 of 300 were in
        one run - so the listing's pid may be somebody else's by the time of
        the kill. Provoked: a listing names a pid that, by the kill, is a
        process that does not carry the mark. It is left alone; the one that
        still carries it is ended."""
        mark = f"{RUN}-listed"
        sleeper = [sys.executable, "-c", "import time; time.sleep(120)"]
        other = subprocess.Popen(sleeper, creationflags=NO_WINDOW)
        self.addCleanup(killed, other)
        mine = subprocess.Popen(sleeper + [mark], creationflags=NO_WINDOW)
        self.addCleanup(killed, mine)
        stale = [(other.pid, f"python -c ... {mark}"), (mine.pid, f"python -c ... {mark}")]
        ended = end_listed(stale, mark)
        self.assertEqual(ended, [mine.pid], "not the process that still carries the mark")
        self.assertIsNone(other.poll(), "a process the listing's pid was handed on to was ended")
        self.assertIsNotNone(mine.wait(KILL_WAIT_S), "the process carrying the mark lives on")


# ========================================================== nothing left ===

class ZZNothingIsLeftRunning(unittest.TestCase):
    """Last, by name: every stand-in this suite started has been ended."""

    def test_no_stand_in_this_suite_started_is_still_running(self):
        left = [marker for proc, marker in LAUNCHED if proc.poll() is None]
        for proc, _ in LAUNCHED:
            killed(proc)
        # and on the machine, by the token on the command line: nothing of this
        # run may be running that the suite did not hold a handle to - a
        # listing the process list could not give is asked for again, never
        # read as "nothing left running"
        listed = asked_again(lambda: cmdlines_containing(RUN), lambda got: got is not None,
                             "whether anything of this run is still running")
        # CHANGED ON PURPOSE (H6, H4's R5): ended through a handle held while
        # the list is asked again - never by a pid a listing gave
        end_listed(listed, RUN)
        self.assertEqual(left, [], "a stand-in outlived its row")
        self.assertEqual(listed, [], "a process carrying this run's token was still running")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from _suites import report                             # the tally, counted in rows
    report(unittest.main(exit=False, verbosity=1))
