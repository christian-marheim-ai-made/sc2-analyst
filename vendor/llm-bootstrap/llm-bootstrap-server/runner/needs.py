"""What a model needs on the card: the runner's estimate, per engine (PLAN-4
D2). A ticket declares it to the coordinator, and each load asks the card
source whether it fits (`manager._check_vram`) - the coordinator by an amend,
the machine's lock by reading the card here - by the contract's one memory
check (`memory.fit`).

    llamacpp  its weights (the file), its cache at its context, and 1536 MiB
              of slack: llama.cpp puts compute buffers and a CUDA context on
              top of the weights and the cache - as the runner has always
              estimated it
    whisper   w + w/4 + 600 MiB, w its file: the Dictaphone's calibrated rule.
              The engine itself is runner/whisper.py; its estimate is here
    mock      nothing: the stand-in puts nothing on the card

A model whose file is not there has no estimate: its load fails at its start,
saying so, and better there than as a guess.

THE MEASURED NEED (D2, D12). Once a model has three MEASURED LOADS, its need
is the largest of them plus 256 MiB, in place of any estimate above. A
measured load is the card's memory its load took once it had answered once -
the free memory its fit check read before it, less what the card had free
after its first answer (`first_answer_mib`, the manager's statistics): what
the model holds while it works, the buffers its first answer puts on the card
included (the Dictaphone measured 350-550 MiB more after the first request
than at ready). The manager hands the model's kept statistic in (`measured`:
its `count` and its `most`); with fewer than three, the estimate stands.
"""
from __future__ import annotations

import math
from pathlib import Path

MIB = 1024 * 1024
#: llama.cpp's compute buffers and CUDA context, over the weights and the cache
LLAMA_SLACK_MIB = 1536
#: what whisper-server holds beside its model and a quarter more of it
WHISPER_OVERHEAD_MIB = 600
#: a model's measured loads that make its need: the largest of them, over
#: all time, and this beside it - the coordinator's settle slack, and the
#: floor under which a rental is degraded (PLAN-4 D2)
MEASURED_LOADS = 3
MEASURED_SLACK_MIB = 256
#: how to lower a llama.cpp model's need, said when it can never fit
LLAMA_HINT = ("Lower [models.{key}] ctx, or quantize its cache (cache_type_k and cache_type_v "
              "= \"q8_0\").")


def llama_need_mib(file_bytes: int, cache_bytes: int = 0) -> int:
    """Weights and cache, in whole MiB, and the slack."""
    return math.ceil((int(file_bytes) + int(cache_bytes)) / MIB) + LLAMA_SLACK_MIB


def whisper_need_mib(file_bytes: int) -> int:
    """w + w/4 + 600 MiB, w the model's file - in whole MiB."""
    w = int(file_bytes) / MIB
    return math.ceil(w + w / 4 + WHISPER_OVERHEAD_MIB)


def need_mib(cfg, key: str, engine: str, measured=None) -> tuple:
    """What model *key* needs on the card when *engine* runs it: -> (MiB or
    None, how it was reckoned, what to change should it never fit). None is
    no estimate: the stand-in's, or a file that is not there. *measured* is
    the model's kept statistic of its measured loads (`count`, `most`): three
    of them or more, and the need is the largest + 256 MiB."""
    spec = cfg.model(key)
    count = (measured or {}).get("count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= MEASURED_LOADS:
        most = int(math.ceil(float(measured["most"])))
        return (most + MEASURED_SLACK_MIB,
                f"measured: the largest of its {count} measured loads, {most} MiB after its "
                f"first answer, + {MEASURED_SLACK_MIB} MiB",
                LLAMA_HINT.format(key=key) if engine == "llamacpp" else "")
    stand_in = (getattr(cfg, "mock", None) or {}).get("need_mib") or {}
    if cfg.backend == "mock" and key in stand_in:
        # S18: what the stand-in's settings say this model needs on the
        # stand-in's own card - judged as a real model's need is
        return (int(stand_in[key]), "the stand-in's settings say it ([mock] need_mib)",
                "lower [mock] need_mib, or raise card_mib")
    if engine not in ("llamacpp", "whisper") or not spec.file:
        return None, "the stand-in puts nothing on the card", ""
    path = Path(spec.gguf_path(cfg.models_dir))
    try:
        size = path.stat().st_size
    except OSError:
        return None, f"its file is not there ({path})", ""
    if engine == "whisper":
        need = whisper_need_mib(size)
        return need, (f"its file {size / MIB:.0f} MiB, a quarter more, and "
                      f"{WHISPER_OVERHEAD_MIB} MiB"), ""
    cache = 0
    try:
        from .gguf import kv_cache_bytes, read_header
        per = 1.0 if str(spec.cache_type_k).startswith("q8") else 2.0
        cache = int(kv_cache_bytes(read_header(path), int(spec.ctx), per) or 0)
    except Exception:                                       # noqa: BLE001
        cache = 0                                           # the weights alone catch the big cases
    return (llama_need_mib(size, cache),
            f"weights {size / MIB:.0f} MiB + cache {cache / MIB:.0f} MiB at context {spec.ctx} + "
            f"{LLAMA_SLACK_MIB} MiB slack", LLAMA_HINT.format(key=key))
