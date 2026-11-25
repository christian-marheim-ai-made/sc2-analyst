"""What can be known of a model without loading it: the answer to
`GET /api/models`, one model at a time.

    its file      there or not, its size, the disk it sits on
    its header    its engine's: a llama.cpp model's GGUF against the settings -
                  the architecture, the context it was trained for, a chat
                  template, bits per weight - and a whisper model's ggml
                  header, its eleven numbers and its shape in words
                  (runner/ggml.py); each read from the header alone, never
                  the weights. A whisper model's context
                  is null: it has none to set
    its problems  the sentences a load would log (`check_against_spec`), and
                  the file's own: not there, not its engine's, not readable
    its loads     cold and warm, from the bound folder's record: how many, the
                  normal (the median, once there are three), the last, and
                  whether the last was slow against the normal BEFORE it - the
                  judgement that load itself was given
    its measure   the last read-speed measure this server made (diskread.py)
    its stats     its timing statistics, as the manager keeps them in the bound
                  folder (PLAN-4 D12): each measure described (`stats.describe`)

Nothing here loads, locks or waits: it reads settings, a header and a record.
"""
from __future__ import annotations

from .diskread import disk_of
from .ggml import FIELDS as GGML_FIELDS, GGMLError, read_header as read_ggml
from .gguf import GGUFError, check_against_spec, read_header
from .manager import load_baseline, load_bucket, load_is_slow


class ModelRefused(Exception):
    """A model route's refusal, in the route's own words."""

    def __init__(self, status: int, reason: str, text: str):
        super().__init__(text)
        self.status, self.reason, self.text = status, reason, text

    def answer(self) -> tuple:
        return self.status, {"error": self.text, "reason": self.reason}


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def history(buckets: dict, key: str, cold: bool) -> dict:
    """One model's loads of one class, from the record's bucket. A record
    somebody broke reads as no loads, never as a crash."""
    entry = buckets.get(load_bucket(key, cold))
    entry = entry if isinstance(entry, dict) else {}
    samples = [float(s) for s in (entry.get("samples") or []) if _number(s)]
    last = float(entry["last"]) if _number(entry.get("last")) else None
    normal = load_baseline(samples)
    return {
        "runs": int(entry["n"]) if _number(entry.get("n")) else 0,
        "normal": round(normal, 2) if normal is not None else None,
        "last": last,
        "last_at": float(entry["last_at"]) if _number(entry.get("last_at")) else None,
        # the last sample IS the last load: it is judged against those before it
        "slow": bool(last is not None and load_is_slow(last, load_baseline(samples[:-1]))),
    }


def _read(path, engine: str) -> tuple:
    """The file's header as its ENGINE reads it -> (info, None), or (None, why)
    when the file is not that engine's, in its reader's words. A llama.cpp
    model's file is a GGUF; a whisper model's starts with the ggml magic, and
    its reader names the file itself. OSError passes: not readable is not the
    same as not a model."""
    if engine == "whisper":
        try:
            return read_ggml(path), None
        except GGMLError as exc:
            return None, str(exc)
    try:
        return read_header(path), None
    except GGUFError as exc:
        return None, f"{path.name} is not a usable GGUF: {exc}"


def _header(path, stat, headers, engine: str = "llamacpp"):
    """The file's header, read once for as long as the file is the same file:
    `headers` keeps one answer per engine and path, keyed by its size and
    mtime - one file named by two models of two engines is read by each
    engine's own reader. A GGUF header is a walk over every tokenizer string -
    tenths of a second a model - and the list is asked every few seconds
    while it is shown. An error reading it is never kept: it may pass.
    -> (info, None) or (None, why)."""
    stamp, key = (stat.st_size, stat.st_mtime_ns), (engine, str(path))
    kept = headers.get(key) if headers is not None else None
    if kept is not None and kept[0] == stamp:
        return kept[1]
    try:
        got = _read(path, engine)
    except OSError as exc:
        return None, f"cannot read {path}: {exc}"
    if headers is not None:
        headers[key] = (stamp, got)
    return got


def model_health(spec, models_dir, backend_kind: str, buckets, *, loaded: bool,
                 measure, headers: dict | None = None, stats=None) -> dict:
    """One model, as `shapes/model.json` says - with the engine it runs on.
    `buckets` is the bound folder's load record, or None with nothing bound:
    then there is no history, which is not the same as an empty one.
    `headers` is the caller's own keeping of the headers it has read
    (`_header`). A GGUF header is llama.cpp's: a whisper model's file is not
    one, and its header is its own - the ggml magic and eleven numbers
    (PLAN-4 D11: shown in the Models widget). `stats` is its statistics,
    described - or None: nothing bound, or a summary that cannot be read
    (`ModelManager.models`). Where the model stands - its state, and whether
    a pipeline keeps it - is the manager's to add."""
    file = header = None
    problems: list = []
    engine = getattr(spec, "engine", "llamacpp")
    if spec.file:
        path = spec.gguf_path(models_dir)
        stat = None
        try:
            if path.is_file():
                stat = path.stat()
        except OSError:
            stat = None
        size = stat.st_size if stat is not None else None
        file = {"name": spec.file, "path": str(path), "present": size is not None,
                "size": size, "disk": disk_of(path)}
        if size is None:
            problems.append(f"the file is not there: {path}")
        else:
            info, why = _header(path, stat, headers, engine)
            if why:
                problems.append(why)
            elif engine == "whisper":
                # the contract's ggml_header: its shape in words, and the
                # eleven numbers as the file stores them, under their names
                header = {"shape": str(info["shape"]),
                          **{name: int(info[name]) for name in GGML_FIELDS}}
            else:
                ctx, bpw = info.get("context_length"), info.get("bits_per_weight")
                header = {"architecture": str(info["architecture"]),
                          "trained_context": int(ctx) if _number(ctx) and ctx == int(ctx) else None,
                          "chat_template": bool(info["has_chat_template"]),
                          "bits_per_weight": round(bpw, 2) if bpw else None}
                problems += check_against_spec(info, spec)
    elif backend_kind != "mock":
        problems.append(f"no file named: llama.cpp needs [models.{spec.key}] file to point "
                        "at a GGUF" if engine == "llamacpp" else
                        f"no file named: whisper-server needs [models.{spec.key}] file to point "
                        "at a whisper model")
    return {"key": spec.key, "label": spec.label, "engine": engine, "note": spec.note,
            # a whisper model has no context to set: its settings never read one
            "context": None if engine == "whisper" else int(spec.ctx),
            "loaded": bool(loaded), "file": file, "header": header, "problems": problems,
            "loads": None if buckets is None else {"cold": history(buckets, spec.key, True),
                                                    "warm": history(buckets, spec.key, False)},
            "read_speed": measure,
            "stats": stats}
