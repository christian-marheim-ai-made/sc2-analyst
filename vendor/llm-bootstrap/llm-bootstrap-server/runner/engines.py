"""The engines a model runs on (PLAN-4 D9, D10), and what each is on the machine.

    llamacpp   llama.cpp's llama-server: a text model, answering token by token
    whisper    whisper.cpp's whisper-server: a transcription, answered in one piece

A model names its engine (`[models.<key>] engine`, llama.cpp unless it says
whisper), `--backend real` runs each model on its own, and the stand-in model
(`mock`) answers in each engine's shape. Both engines run on the card: there
is no CPU engine, and the settings refuse a model set up to run on the CPU -
the owner's ruling: an application that wants a CPU path keeps it outside the
bootstrap.

WHAT EACH ENGINE IS, in one table (`ENGINES`), so nothing else branches on
its name for these:

    program    the `[runner]` setting naming its executable (`llama_server`,
               `whisper_server`): a path, or a list - an interpreter and a
               script, which is how a stand-in with the same command line
               takes its place
    names      its executable, as the operating system lists its processes:
               what the real card's instrument (cardwatch.py) watches for
    reads      the model settings it reads, beside those every engine reads
               (`COMMON`); a model naming one its engine does not read is
               refused as its settings load
    answers    "stream": token by token, each token movement a stall watch
               sees; "once": the whole answer in one piece, due by a deadline
               it declares as it begins (runner/base.py: `Backend.answers`)
    proof      how a load's own id is carried on its command line

A PROOF ON EVERY PROCESS, WHATEVER THE ENGINE (D9, the GUIDE's rule 9). A
process is this runner's only when its command line names the engine's
program, the model's file, the port the record gave it, and - carried by the
engine's own proof - the load's id:
    llama-server     `--alias <load id>`, and a key of its own per load
                     (`--api-key`), as ever: the id anywhere on the line, as it
                     always was read; a record from before loads had ids has
                     none, and its program, file and port were its proof
    whisper-server   `--request-path /<load id>`: every route of that load is
                     under the path, so a stranger that takes the port while
                     the load comes up (nothing listens until its model is up)
                     answers 404 there. Exactly that path - never one inside a
                     longer one - and never without an id: every whisper load
                     had one
The command lines themselves are each engine's own (`LlamaCppBackend` and
`WhisperBackend`'s `build_command`); a row holds each one's `is_ours` to the
command line it builds.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Engine:
    name: str
    program: str
    names: tuple
    reads: tuple
    answers: str
    proof: str


#: the model settings every engine reads - besides `key`
COMMON = ("label", "file", "note", "extra_args", "engine")
#: what llama.cpp reads beside them: its context, its layers on the card, its
#: cache, its sampling and its thinking
LLAMA_READS = ("ctx", "n_gpu_layers", "cache_type_k", "cache_type_v", "temperature", "min_p",
               "top_p", "repeat_penalty", "disable_thinking")

#: what whisper-server reads beside them (PLAN-4 D11): its threads, its voice
#: detector, how long its start may take, how soon a failed one is tried
#: again, and the deadline of its answer in one piece
WHISPER_READS = ("threads", "vad", "startup_timeout_s", "retry_after_s", "due_base_s",
                 "due_per_audio_s")

#: THE FLAGS AN ENGINE SETS ITSELF, each from a setting of the model's (named
#: here) or of the load's own (None: its port, its proof): never in a model's
#: `extra_args`, where the program would take the last one given, and a
#: setting would read as one that took effect when it did not. The settings
#: refuse them as they load
OWN_FLAGS = {
    "llamacpp": {},
    "whisper": {"-m": "file", "--model": "file", "--host": None, "--port": None,
                "--request-path": None, "-t": "threads", "--threads": "threads",
                "--vad": "vad", "-vm": "vad", "--vad-model": "vad"},
}

ENGINES = {
    "llamacpp": Engine("llamacpp", "llama_server", ("llama-server.exe", "llama-server"),
                       LLAMA_READS, "stream", "--alias <load id>"),
    "whisper": Engine("whisper", "whisper_server", ("whisper-server.exe", "whisper-server"),
                      WHISPER_READS, "once", "--request-path /<load id>"),
}


def reads(engine: str) -> tuple:
    """Every model setting *engine* reads."""
    return COMMON + ENGINES[engine].reads


def engine_of_process(name) -> str | None:
    """The engine whose executable a process list names *name* - or None."""
    low = str(name or "").lower()
    return next((e.name for e in ENGINES.values() if low in e.names), None)


def _program_name(program) -> str:
    """The executable a command line must name: a path, or a list whose LAST
    part is the program (an interpreter and a script)."""
    last = program[-1] if isinstance(program, (list, tuple)) and program else program
    exe = Path(str(last).replace("\\", "/")).name.lower()
    return exe[:-4] if exe.endswith(".exe") else exe


def is_ours(command_line, program, model_file, port, load=None, *, engine="llamacpp") -> bool:
    """Is this the command line of a process this runner started on *engine*
    - ITS program, the model file the record names, the port the record gave
    it and, carried by the engine's own proof, the load's id? Any one
    missing: no. An engine this runner does not know: no."""
    if engine not in ENGINES or not command_line or not model_file or not port:
        return False
    line = str(command_line).replace("\\", "/").lower()
    exe = _program_name(program)
    if not exe or exe not in line:
        return False
    if Path(str(model_file).replace("\\", "/")).name.lower() not in line:
        return False
    # a port is a whole number: 8901 must not be found inside 18901 or 89012
    if not re.search(rf"--port[\s=\"']+{int(port)}(?![0-9])", line):
        return False
    if engine == "whisper":
        # exactly its own path - never inside a longer one - and never none
        return load is not None and re.search(
            rf"--request-path[\s=\"']+/{re.escape(str(load).lower())}(?=[\s\"']|$)",
            line) is not None
    return load is None or str(load).lower() in line
