"""The runner's settings and the models it knows - one TOML file, read with
the standard library's `tomllib`.

    [runner]            how models are run (the backend, each engine's program, timeouts)
    [mock]              how the stand-in model behaves
    [defaults]          what every model gets unless it says otherwise - each
                        model the settings its engine reads
    [models.<key>]      one model: its engine, its file, its context, its samplers

AN ENGINE PER MODEL (PLAN-4 D10). A model names the engine it runs on -
`engine = "llamacpp"` or `"whisper"` (runner/engines.py) - and one that names
none is llama.cpp's: every settings file written before engines were named
ran its models there, and runs as it did. `backend = "real"` runs each model
on its own engine; `"llamacpp"`, the real backend's name from before, runs
every model on llama.cpp, and refuses a whisper model, naming `real`; `"mock"`
is the stand-in, answering in each model's engine's shape. Both engines run
on the card: a model set up to run on the CPU is refused (the owner's ruling,
no CPU models).

THERE IS NO DATA ROOT IN HERE, and there must never be one. Where the runner
writes - its events, its load record, llama-server's logs - is handed to the
manager by whoever bound the folder, so it follows the folder. A root written
in a settings file cannot follow anything, and a settings file that silently
answered one would write somewhere nobody chose.

A setting nobody reads is refused rather than ignored: a misspelt key that
loads without complaint reads as a setting that took effect.
"""
from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .engines import ENGINES, OWN_FLAGS, reads

#: `mock`, the stand-in; `real`, each model on its own engine; `llamacpp`, the
#: real backend's name from before engines were named: every model on llama.cpp
BACKENDS = ("mock", "real", "llamacpp")
#: the most models a runner keeps on the card at once: a coordinator's rental
#: names at most 8 processes (the coordinator's MAX_PROCESSES)
MAX_LOADS = 8
#: WHAT PUTS A MODEL ON THE CPU, per engine, in its `extra_args`: a flag and
#: the value that does it, or None for a switch. Refused as the settings load
#: (PLAN-4 D10: no CPU models) - with llama.cpp's `n_gpu_layers = 0`
CPU_FLAGS = {
    "llamacpp": {"-ngl": "0", "--n-gpu-layers": "0", "--gpu-layers": "0",
                 "-dev": "none", "--device": "none"},
    "whisper": {"-ng": None, "--no-gpu": None},
}
NO_CPU = ("the bootstrap runs no CPU models (the owner's ruling): an application that wants "
          "a CPU path keeps it outside the bootstrap")

DEFAULTS: dict[str, dict[str, Any]] = {
    "runner": {
        "backend": "mock",
        "llama_server": "llama-server",
        # whisper.cpp's whisper-server, for a model whose engine is whisper:
        # a path, or a list - an interpreter and a script - as llama_server
        "whisper_server": "whisper-server",
        # 0: each load takes a free port of the runner's own quiet range
        # (runner/manager.py's QUIET_PORTS, 27000-28499) - bind-checked, never
        # one the operating system picks - so two servers on one machine are
        # never pointed at one port by accident. A port named here is every
        # model's: one model on the card at a time
        "port": 0,
        "models_dir": "models",
        "startup_timeout_s": 600.0,
        # how often a load asks its model server whether it is up yet: a
        # tenth of a second, so a start is timed to a tenth (PLAN-4 D12)
        "health_poll_s": 0.1,
        "shutdown_timeout_s": 60.0,
        "idle_unload_s": 600.0,
        "warm_keep": True,
        "request_timeout_s": 1800.0,
        "flash_attn": "on",
        # how long a load waits for the card while another application has
        # it - an hour unless a job names its own (a job's `card_patience_s`);
        # then the job fails with its own reason, `card_patience`, naming
        # nobody. With a coordinator, 0 is now or not at all: a card not free
        # at once fails the job `card_busy`. The stall watch treats the wait as
        # a load, so this is the wait's one bound.
        "card_patience_s": 3600.0,
        # SCHEDULED WORK THAT DOES NOT FIT (S18, the owner's second ruling of
        # 2026-09-28: "Lets support both with a config"): "wait" - it waits
        # for room, within its patience, holding back nobody, and runs by
        # itself once its model fits - or "fail": it fails at once, `no_room`,
        # in the loud words live work does. A request may say its own
        # (`if_no_room` on a job or a pipeline). Live work always fails at
        # once: this never makes it wait
        "scheduled_if_no_room": "wait",
        # with no coordinator named, a real card's session looks at this
        # machine's lock (runner/cardlock.py): free, it takes it; held by a
        # coordinator, it goes through that coordinator; held by an application
        # run on its own, it waits. Empty is the machine's own, which every
        # server and coordinator on it shares - a suite names a file of its own
        "card_lock": "",
        # run on its own, the free memory this server keeps beside what a
        # model needs when it reads the card itself before a load (PLAN-4 D2:
        # the contract's one memory check). A coordinator keeps its own margin
        # (--margin). The contract's memory.MARGIN_MIB, the free memory under
        # which a rental is degraded; raise it on a card other programs share
        "card_margin_mib": 256,
        # the models the server's session opens with as it starts (PLAN-4 D6:
        # an application starts ready). None, the default: no session opens at
        # start, and the queue holds the card for its own work only. Named, the
        # server opens its pipeline as it starts - held until it is closed or
        # the server stops - and loads them at once, side by side, each once:
        # at most `max_loads`, and one on a configured `port`
        "session_models": [],
        # how many models this runner keeps on the card at once, side by side
        # (PLAN-4 D7): one load of each, loaded one at a time. 8, the most a
        # coordinator's rental names; lower it when the card holds fewer of
        # your models than the card source's fit check lets through. A model
        # a job needs past it takes an on-demand model off - the one used
        # longest ago - never a pinned one. A configured `port` is one model
        # at a time, whatever this says
        "max_loads": 8,
        # how long an application's pipeline stays open without a renewal,
        # unless the open names its own keep_s. 0 holds every pipeline until it
        # is closed or the server stops. Tune it to how long an application may
        # go quiet and still be alive
        "pipeline_keep_s": 120.0,
        # AN ANSWER FAR SLOWER THAN ITS MODEL'S NORMAL (PLAN-4 D3, D12) is
        # flagged: on the record, in the statistics, and counted on each
        # heartbeat - where a coordinator reading the card as full marks the
        # rental degraded. Far slower is BOTH: at most 1/factor of its normal
        # speed - tokens a second once it began, or for an answer in one
        # piece seconds taken per second of audio - AND at least floor_s
        # seconds lost over what its normal speed would have taken. Its normal
        # is the median of the model's own kept samples, three at least,
        # before it. Three: a memory spill to the machine's RAM makes a model
        # several times slower, while a long context or a grammar slows it
        # less than that. A quarter of a second: jitter on a machine that is
        # doing other things is less, and a person at a live command notices
        # more. Raise the factor if a model's answers flag that are not the
        # card's doing (the statistics show how each works out in use: D0)
        "slow_answer_factor": 3.0,
        "slow_answer_floor_s": 0.25,
        # THE CARD FULL, RECOVERED BY ITSELF, RUN ON ITS OWN (S5b; the owner,
        # 2026-09-27: "same behaviour as coordinator"). On the machine's lock
        # this server watches the card itself, as a coordinator does a
        # rental: read every card_watch_s while it holds the lock, and every
        # card_recover_watch_s while it is degraded - free memory under
        # card_floor_mib, or a memory error its models' logs said, degrades
        # it; the first read with room for what its models need clears it,
        # and the work held for the card's memory runs again by itself. A
        # coordinator keeps its own (--watch, --recover-watch, --floor).
        # Lower card_recover_watch_s to notice room sooner; raise the floor on
        # a card other programs share
        "card_watch_s": 30.0,
        "card_recover_watch_s": 2.0,
        "card_floor_mib": 256,
    },
    "mock": {
        "tokens_per_second": 45.0,
        "load_seconds": 1.2,
        "unload_seconds": 0.4,
        "fail_models": [],
        "fail_first_for": "",
        # the models whose every LOAD fails, once its load time is waited out
        # (S14b R3): a model's `failed` state, and why, shown by the stand-in
        # server from its command line - model keys these settings name
        "fail_load_models": [],
        "seed": 7,
        # an answer in one piece - a whisper model's transcription - takes
        # this long, and is declared due within `answer_due_s`: set that
        # below this to have the stand-in overrun its own deadline
        "answer_seconds": 0.5,
        "answer_due_s": 60.0,
        # THE STAND-IN'S CARD (S16a R2), so an application on the stand-ins
        # can try its card-full path. 0, the default: none - nothing is read,
        # and no lock is taken. Above 0: a card of that many MiB, read by the
        # stand-in alone - never the real card - and watched as a real card
        # is on the machine's lock (S5b: the card_watch_s, the floor, the
        # same degraded and recovered path), on a lock of its own, which
        # [runner] card_lock must name - never the machine's
        "card_mib": 0,
        # [after_s, for_s]: counted from the server's start, after after_s the
        # stand-in's card reads full - no MiB free - for for_s seconds, then
        # free again. [] never
        "card_full_s": [],
        # what each stand-in model needs on the stand-in's card, in MiB - a
        # table, model key = MiB (S18): a model named here is judged by the
        # memory check against the stand-in's card, as a real model is against
        # the real one, so an application on the stand-ins can see a model
        # that does not fit - fail at once, or wait for room. Only with a
        # card (card_mib); a model it does not name needs nothing
        "need_mib": {},
    },
}


@dataclass
class ModelSpec:
    """One model the runner can load."""
    key: str
    label: str = ""
    #: the engine it runs on (runner/engines.py): llama.cpp unless it says
    engine: str = "llamacpp"
    file: str = ""
    ctx: int = 8192
    n_gpu_layers: int = 99
    cache_type_k: str = "f16"
    cache_type_v: str = "f16"
    # the model's own sampling, and its thinking: DEFAULTS, for a job that
    # names none of them - a job's own value is sent instead, as it named it
    # (`enable_thinking` in its `chat_template_kwargs` for `disable_thinking`)
    temperature: float = 0.8
    min_p: float = 0.05
    top_p: float = 1.0
    repeat_penalty: float = 1.05
    disable_thinking: bool = False
    # A WHISPER MODEL'S OWN (PLAN-4 D11; runner/whisper.py says each):
    #: its CPU threads, `-t`: 4, whisper-server 1.9.1's own default on a
    #: machine of four threads or more. The card does the model's work; these
    #: prepare its sound
    threads: int = 4
    #: its voice detector's file (`--vad -vm`), in models_dir unless a path
    #: says otherwise; "" is none
    vad: str = ""
    #: how long its load may take to come up - the runner's startup_timeout_s
    #: unless it names its own
    startup_timeout_s: float | None = None
    #: a start that launched it and failed is not tried again for this long
    retry_after_s: float = 60.0
    #: a transcription is due within due_base_s, and due_per_audio_s more
    #: for each second of its audio: real time and half a minute besides
    due_base_s: float = 30.0
    due_per_audio_s: float = 1.0
    extra_args: list = field(default_factory=list)
    note: str = ""

    def __post_init__(self):
        self.label = self.label or self.key

    def gguf_path(self, models_dir) -> Path:
        return Path(models_dir) / self.file

    def is_present(self, models_dir) -> bool:
        return bool(self.file) and self.gguf_path(models_dir).exists()

    def public(self, models_dir) -> dict:
        return {"key": self.key, "label": self.label, "ctx": self.ctx,
                "present": self.is_present(models_dir),
                "disable_thinking": self.disable_thinking, "note": self.note}


MODEL_SETTINGS = tuple(f.name for f in fields(ModelSpec) if f.name != "key")


class Config:
    def __init__(self, data: dict, models: dict, root: Path, path: Path | None = None):
        self.data, self.models, self.root, self.path = data, models, Path(root), path

    @property
    def runner(self) -> dict:
        return self.data["runner"]

    @property
    def mock(self) -> dict:
        return self.data["mock"]

    @property
    def backend(self) -> str:
        return self.runner["backend"]

    def engine(self, key: str) -> str:
        """What runs model *key*: its own engine on the real card - or the
        stand-in, `mock`, under the mock backend, with nothing on the card."""
        return "mock" if self.backend == "mock" else self.model(key).engine

    @property
    def models_dir(self) -> Path:
        p = Path(str(self.runner["models_dir"])).expanduser()
        return p if p.is_absolute() else self.root / p

    def model(self, key: str) -> ModelSpec:
        if key not in self.models:
            raise KeyError(f"no model {key!r} here; the runner knows "
                           f"{', '.join(sorted(self.models)) or 'none'}")
        return self.models[key]

    # ---------------------------------------------------------------- loading

    @classmethod
    def default(cls) -> "Config":
        """No file at all: the stand-in model, and one model called `demo`."""
        return cls._build({}, Path.cwd(), None)

    @classmethod
    def load(cls, path) -> "Config":
        path = Path(path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"no runner settings file at {path}")
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
        return cls._build(raw, path.resolve().parent, path.resolve())

    @classmethod
    def _build(cls, raw: dict, root: Path, path) -> "Config":
        where = str(path) if path else "the defaults"
        unknown = set(raw) - {"runner", "mock", "defaults", "models"}
        if unknown:
            raise ValueError(f"{where}: unknown section(s) {sorted(unknown)}")
        data = {}
        for section, defaults in DEFAULTS.items():
            given = raw.get(section) or {}
            wrong = set(given) - set(defaults)
            if wrong:
                raise ValueError(f"{where} [{section}]: unknown setting(s) {sorted(wrong)}")
            data[section] = {**defaults, **given}
        if data["runner"]["backend"] not in BACKENDS:
            raise ValueError(f"{where} [runner]: backend must be one of {BACKENDS}, "
                             f"not {data['runner']['backend']!r}")
        shared = raw.get("defaults") or {}
        wrong = set(shared) - set(MODEL_SETTINGS)
        if wrong:
            raise ValueError(f"{where} [defaults]: unknown setting(s) {sorted(wrong)}")
        models = {}
        for key, entry in (raw.get("models") or {}).items():
            own = entry or {}
            wrong = set(own) - set(MODEL_SETTINGS)
            if wrong:
                raise ValueError(f"{where} [models.{key}]: unknown setting(s) {sorted(wrong)}")
            models[key] = _model(where, key, own, shared, data["runner"]["backend"])
        if not models and not raw.get("models"):
            models = {"demo": ModelSpec("demo", label="demo (stand-in)")}
        failing = data["mock"]["fail_load_models"]
        if not isinstance(failing, list) or not all(isinstance(k, str) for k in failing):
            raise ValueError(f"{where} [mock]: fail_load_models is a list of model keys, "
                             f"not {failing!r}")
        unknown = [k for k in failing if k not in models]
        if unknown:
            raise ValueError(f"{where} [mock]: fail_load_models names {unknown}, which the "
                             f"settings do not; they name {sorted(models)}")
        runner = data["runner"]
        _stand_in_card(where, data["mock"], runner)
        _stand_in_needs(where, data["mock"], models)
        if runner["scheduled_if_no_room"] not in ("wait", "fail"):
            raise ValueError(f"{where} [runner]: scheduled_if_no_room is what scheduled work "
                             f"whose model does not fit does - \"wait\" for room, or \"fail\" at "
                             f"once - not {runner['scheduled_if_no_room']!r}")
        start = runner["session_models"]
        if not isinstance(start, list) or not all(isinstance(k, str) for k in start):
            raise ValueError(f"{where} [runner]: session_models is a list of model keys, "
                             f"not {start!r}")
        unknown = [k for k in start if k not in models]
        if unknown:
            raise ValueError(f"{where} [runner]: session_models names {unknown}, which the "
                             f"settings do not; they name {sorted(models)}")
        most = runner["max_loads"]
        if isinstance(most, bool) or not isinstance(most, int) or not 1 <= most <= MAX_LOADS:
            raise ValueError(f"{where} [runner]: max_loads is a whole number from 1 to "
                             f"{MAX_LOADS} - a rental names at most {MAX_LOADS} processes - "
                             f"not {most!r}")
        twice = next((k for i, k in enumerate(start) if k in start[:i]), None)
        if twice is not None:
            raise ValueError(f"{where} [runner]: session_models names {twice} twice: a model is "
                             f"loaded once")
        if len(start) > most:
            raise ValueError(f"{where} [runner]: session_models names {len(start)} models, and "
                             f"this runner keeps at most {most} on the card at once (max_loads)")
        port = runner.get("port")
        if len(start) > 1 and runner["backend"] != "mock" and isinstance(port, int) \
                and not isinstance(port, bool) and port > 0:
            # a port serves one load at a time: the manager keeps one model
            # on the card there (`_limit`), and the session could never open
            raise ValueError(f"{where} [runner]: session_models names {len(start)} models, and "
                             f"this runner keeps one on the card at a time: its settings name "
                             f"one port for every model ([runner] port)")
        runner["session_models"] = list(start)              # never the defaults' own list
        margin = runner["card_margin_mib"]
        if isinstance(margin, bool) or not isinstance(margin, int) or margin < 0:
            raise ValueError(f"{where} [runner]: card_margin_mib is a whole number of MiB, 0 or "
                             f"more, not {margin!r}")
        for name in ("card_watch_s", "card_recover_watch_s"):
            every = runner[name]
            if isinstance(every, bool) or not isinstance(every, (int, float)) \
                    or not math.isfinite(every) or every <= 0:
                raise ValueError(f"{where} [runner]: {name} is how often the card is read, a "
                                 f"number of seconds above 0, not {every!r}")
        floor_mib = runner["card_floor_mib"]
        if isinstance(floor_mib, bool) or not isinstance(floor_mib, int) or floor_mib < 0:
            raise ValueError(f"{where} [runner]: card_floor_mib is a whole number of MiB, 0 or "
                             f"more, not {floor_mib!r}")
        keep = runner["pipeline_keep_s"]
        if isinstance(keep, bool) or not isinstance(keep, (int, float)) or keep < 0 \
                or keep != keep or keep == float("inf"):
            raise ValueError(f"{where} [runner]: pipeline_keep_s is 0 - held until closed - or "
                             f"a number of seconds above 0, not {keep!r}")
        factor = runner["slow_answer_factor"]
        if isinstance(factor, bool) or not isinstance(factor, (int, float)) \
                or not math.isfinite(factor) or factor <= 1:
            raise ValueError(f"{where} [runner]: slow_answer_factor is how many times slower "
                             f"than its normal an answer is far slower - a number above 1, "
                             f"not {factor!r}")
        floor = runner["slow_answer_floor_s"]
        if isinstance(floor, bool) or not isinstance(floor, (int, float)) \
                or not math.isfinite(floor) or floor < 0:
            raise ValueError(f"{where} [runner]: slow_answer_floor_s is the seconds a far slower "
                             f"answer lost at least - 0 or more, not {floor!r}")
        return cls(data, models, root, path)


def _stand_in_card(where: str, mock: dict, runner: dict) -> None:
    """S16a R2: the stand-in's card - `card_mib`, a whole number of MiB, 0
    for none; `card_full_s`, [] or [after_s, for_s], only with a card - on a
    lock of its own that [runner] card_lock names, never the machine's."""
    mib, full = mock["card_mib"], mock["card_full_s"]
    if isinstance(mib, bool) or not isinstance(mib, int) or mib < 0:
        raise ValueError(f"{where} [mock]: card_mib is the stand-in card's size, a whole number "
                         f"of MiB - 0 for none - not {mib!r}")
    if not isinstance(full, list) or len(full) not in (0, 2) or any(
            isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
            or x < 0 for x in full):
        raise ValueError(f"{where} [mock]: card_full_s is [] - never full - or [after_s, for_s], "
                         f"two numbers of seconds, 0 or more, not {full!r}")
    if full and not mib:
        raise ValueError(f"{where} [mock]: card_full_s fills the stand-in's card, and it has "
                         f"none: give it one (card_mib)")
    if not mib or runner["backend"] != "mock":
        return
    lock = str(runner.get("card_lock") or "")
    if not lock:
        raise ValueError(f"{where} [mock]: card_mib - the stand-in's card is watched as a real "
                         f"card is, on a lock of its own: name a file of your own in [runner] "
                         f"card_lock, never the machine's")
    from .cardlock import LOCK_NAME
    import tempfile
    if Path(lock).resolve() == (Path(tempfile.gettempdir()) / LOCK_NAME).resolve():
        raise ValueError(f"{where} [runner]: card_lock names the machine's own lock ({lock}), "
                         f"which another application may hold: the stand-in's card takes a "
                         f"lock of its own - name a file of your own")


def _stand_in_needs(where: str, mock: dict, models: dict) -> None:
    """S18: `need_mib`, what each stand-in model needs on the stand-in's card -
    a table of the settings' own model keys, each a whole number of MiB, 0
    or more - and only with a card of the stand-in's own (`card_mib`)."""
    needs = mock["need_mib"]
    if not isinstance(needs, dict):
        raise ValueError(f"{where} [mock]: need_mib is a table - model key = MiB - not "
                         f"{needs!r}")
    unknown = sorted(k for k in needs if k not in models)
    if unknown:
        raise ValueError(f"{where} [mock]: need_mib names {unknown}, which the settings do not; "
                         f"they name {sorted(models)}")
    for key, mib in needs.items():
        if isinstance(mib, bool) or not isinstance(mib, int) or mib < 0:
            raise ValueError(f"{where} [mock]: need_mib.{key} is a whole number of MiB, 0 or "
                             f"more, not {mib!r}")
    if needs and not mock["card_mib"]:
        raise ValueError(f"{where} [mock]: need_mib is what the stand-in's models need on its "
                         f"card, and it has none: give it one (card_mib)")
    mock["need_mib"] = dict(needs)                          # never the defaults' own table


def _model(where: str, key: str, own: dict, shared: dict, backend: str) -> ModelSpec:
    """One model: its engine (llama.cpp unless it or the defaults say), the
    settings that engine reads - its own, then the defaults' - and never one
    set up to run on the CPU."""
    engine = own.get("engine", shared.get("engine", "llamacpp"))
    if not isinstance(engine, str) or engine not in ENGINES:
        raise ValueError(f"{where} [models.{key}]: engine is llamacpp or whisper - the engines "
                         f"that run on the card - not {engine!r}; {NO_CPU}")
    if backend == "llamacpp" and engine != "llamacpp":
        raise ValueError(f'{where} [models.{key}]: engine {engine} is not llama.cpp, and backend '
                         f'"llamacpp" runs every model on llama.cpp: say backend = "real", to '
                         f'run each model on its own engine')
    readable = reads(engine)
    unread = sorted(k for k in own if k not in readable)
    if unread:
        raise ValueError(f"{where} [models.{key}]: engine {engine} never reads "
                         f"{', '.join(unread)} - a setting nothing reads is refused, not ignored")
    # a default is every model's that reads it: a whisper model passes a
    # llama.cpp default by, and every llama.cpp model still has it
    spec = ModelSpec(key=key, **{**{k: v for k, v in shared.items() if k in readable}, **own})
    cpu = _on_the_cpu(spec)
    if cpu:
        raise ValueError(f"{where} [models.{key}]: {cpu} runs it on the CPU, and {NO_CPU}")
    wrong = _own_flag(spec) or (_whisper_setting(spec) if engine == "whisper" else None)
    if wrong:
        raise ValueError(f"{where} [models.{key}]: {wrong}")
    return spec


def _own_flag(spec: ModelSpec) -> str | None:
    """A flag its engine sets itself, from a setting (`engines.OWN_FLAGS`),
    given in its `extra_args` - where the program takes the last one given,
    and the setting would read as one that took effect when it did not - in
    the settings' own words; or None."""
    own = OWN_FLAGS.get(spec.engine, {})
    args = [str(a) for a in spec.extra_args] if isinstance(spec.extra_args, (list, tuple)) else []
    for arg in args:
        name = arg.partition("=")[0]
        if name in own:
            said = (f"say it with [models.{spec.key}] {own[name]}" if own[name] else
                    "the runner sets it for each load")
            return f"extra_args {arg!r} is {spec.engine}'s own flag: {said}"
    return None


def _seconds(value, *, above_zero: bool) -> bool:
    """A number of seconds: never true or false, never NaN or infinite."""
    return (not isinstance(value, bool) and isinstance(value, (int, float)) and value == value
            and value != float("inf") and (value > 0 if above_zero else value >= 0))


def _whisper_setting(spec: ModelSpec) -> str | None:
    """What of a whisper model's own settings (PLAN-4 D11) no run could use,
    in words; or None."""
    threads = spec.threads
    if isinstance(threads, bool) or not isinstance(threads, int) or threads < 1:
        return f"threads is a whole number of threads, 1 or more, not {threads!r}"
    if not isinstance(spec.vad, str):
        return f"vad names a voice detector's file, or is \"\" for none, not {spec.vad!r}"
    if spec.startup_timeout_s is not None and not _seconds(spec.startup_timeout_s,
                                                           above_zero=True):
        return (f"startup_timeout_s is a number of seconds above 0, not "
                f"{spec.startup_timeout_s!r}")
    for name, above_zero, why in (("retry_after_s", False, "0 tries a failed start again at once"),
                                  ("due_base_s", True, "an answer is due some time after it "
                                                       "is asked"),
                                  ("due_per_audio_s", False, "0 gives long audio no more time "
                                                             "than short")):
        value = getattr(spec, name)
        if not _seconds(value, above_zero=above_zero):
            return (f"{name} is a number of seconds {'above 0' if above_zero else '0 or more'} "
                    f"({why}), not {value!r}")
    return None


def _on_the_cpu(spec: ModelSpec) -> str | None:
    """What in *spec* runs it on the CPU, in its settings' own words - or
    None: llama.cpp with no layer on the card, or an engine's own flag for
    the CPU in its extra arguments (`CPU_FLAGS`)."""
    layers = spec.n_gpu_layers
    if spec.engine == "llamacpp" and not isinstance(layers, bool) \
            and isinstance(layers, (int, float)) and layers == 0:
        return "n_gpu_layers = 0"
    args = [str(a) for a in spec.extra_args] if isinstance(spec.extra_args, (list, tuple)) else []
    flags = CPU_FLAGS.get(spec.engine, {})
    for i, arg in enumerate(args):
        name, eq, value = arg.partition("=")
        if name not in flags:
            continue
        if flags[name] is None:
            return f"extra_args {arg!r}"
        given = value if eq else (args[i + 1] if i + 1 < len(args) else "")
        if given.strip().lower() == flags[name]:
            return f"extra_args {name} {given}"
    return None
