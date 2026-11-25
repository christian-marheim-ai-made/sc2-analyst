"""Timing statistics, per model and per measure, kept in the bound folder.

    <folder>/runner/model_stats.json

Whatever the runner measures - how long a load took, the first token, a
whole answer, an unload - is one SAMPLE of one MEASURE of one MODEL, and each
(model, measure) keeps:

    over all time   count, mean, least, most, and the last (and when)
    the last 200    the samples themselves, oldest first - for the median and
                    the 90th percentile (`describe`)

    {"version": 1,
     "models": {"<model key>": {"<measure>": {
         "count": 12, "mean": 4.21, "least": 3.9, "most": 5.6,
         "last": 4.0, "last_at": 1790172000.0, "samples": [3.9, ..., 4.0]}}}}

WHAT RAN BESIDE EACH SAMPLE (S17): answers to different models run at the same
time, so a sample may say how many other answers ran beside it - `beside`, a
list in step with `samples` (null for a sample that did not say), kept only
by a measure that was ever told it - so a lone answer is told from a shared
one.

THE RECORDS STAY. The summary is folded from records, one sample each -
`{"model", "measure", "value", "at"}`, and `beside` when it says it - and `rebuild(records)` folds a list of
them into the whole summary again, the same way `note` folds one: a summary
that is lost, or doubted, is rebuilt from what it was summed from.

WRITTEN WHOLE, OR NOT AT ALL:
  * one write at a time in this process, under a lock - and one process per
    folder, which the queue's folder lock already promises;
  * to a temporary name of its own (process, thread, random), so no two
    writes ever share one - beside the file, so the swap is a rename;
  * swapped in with `os.replace`, and the swap retried for a moment when
    Windows refuses it because a reader holds the file;
  * as bytes, LF: `Path.write_text` turns every newline CRLF on Windows.

A file that cannot be read as a summary is never written over: `note`
refuses (StatsUnreadable) and leaves its bytes where they are.

What is measured, and when, is the runner's (`manager.py`, PLAN-4 D12): its
records are the `timed` lines of its record, and it folds each into the
summary as it writes it (`note_many`: one line's measures, in one write).
"""
from __future__ import annotations

import json
import math
import os
import random
import threading
import time
from pathlib import Path

#: where the summary is kept, in the runner's folder of the bound folder
FILE = "model_stats.json"
VERSION = 1
#: samples kept per model and measure, for the median and the 90th percentile;
#: the count, the mean, the least and the most are over all time
SAMPLES_KEPT = 200
#: the swap, when Windows refuses it for a moment: this many tries, waiting a
#: little longer each time - about a second and a half in all
SWAP_TRIES = 8
#: one write at a time, in this process
_LOCK = threading.Lock()


class StatsUnreadable(Exception):
    """The kept summary is there and cannot be read as one."""


def empty() -> dict:
    return {"version": VERSION, "models": {}}


def _number(v) -> bool:
    """A finite number. A bool is not one (Python's True is an int), and NaN
    or infinity would poison every mean after it."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _sample(v, what: str) -> float:
    if not _number(v):
        raise ValueError(f"{what} is a finite number, not {v!r}")
    return float(v)


def _name(v, what: str) -> str:
    if not isinstance(v, str) or not v:
        raise ValueError(f"a {what} is a name, not {v!r}")
    return v


def _count(v) -> bool:
    """A whole number, 0 or more - never a bool."""
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _beside(v):
    """S17: how many other answers ran beside a sample - a whole number, 0 or
    more - or None: not said."""
    if v is not None and not _count(v):
        raise ValueError(f"what ran beside a sample is a count, 0 or more, not {v!r}")
    return v


def fold(summary: dict, model, measure, value, at, beside=None) -> dict:
    """One sample into *summary*, in place - checked before anything moves.
    -> that measure's entry. The mean runs: each sample moves it by its share,
    so it never needs the samples that have left the window.

    BESIDE EACH SAMPLE (S17): *beside*, how many other answers ran beside the
    one measured, kept in `beside`, in step with `samples` - null for a
    sample that did not say - so a lone answer is told from a shared one. A
    measure never told keeps no such list."""
    model, measure = _name(model, "model"), _name(measure, "measure")
    x, at = _sample(value, "a sample"), _sample(at, "its time")
    beside = _beside(beside)
    measures = summary["models"].setdefault(model, {})
    entry = measures.get(measure)
    if entry is None:
        entry = measures[measure] = {"count": 0, "mean": 0.0, "least": x, "most": x,
                                     "last": x, "last_at": at, "samples": []}
    entry["count"] += 1
    entry["mean"] += (x - entry["mean"]) / entry["count"]
    entry["least"] = min(entry["least"], x)
    entry["most"] = max(entry["most"], x)
    entry["last"], entry["last_at"] = x, at
    kept = entry.get("beside")
    if beside is not None or kept is not None:
        before = kept if kept is not None else [None] * len(entry["samples"])
        entry["beside"] = (before + [beside])[-SAMPLES_KEPT:]
    entry["samples"] = (entry["samples"] + [x])[-SAMPLES_KEPT:]
    return entry


def rebuild(records) -> dict:
    """The whole summary, folded again from its records in their order - each
    `{"model", "measure", "value", "at"}`, and `beside` when it says it - by
    the same fold as `note`. A summary kept faithfully is equal to its
    rebuild; a record `note` would refuse is refused here too."""
    summary = empty()
    for r in records:
        fold(summary, r["model"], r["measure"], r["value"], r["at"], r.get("beside"))
    return summary


def describe(entry: dict) -> dict:
    """What a person reads of one measure: the figures over all time, and the
    median and the 90th percentile of the samples kept (`kept` of them). The
    90th percentile is a sample that happened - the nearest rank: the least
    sample with nine in ten of them at or below it - never one made up
    between two."""
    s = sorted(entry["samples"])
    n = len(s)
    median = p90 = None
    if n:
        mid = n // 2
        median = s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0
        p90 = s[(9 * n + 9) // 10 - 1]                  # rank ceil(0.9 n), in integers
    return {"count": entry["count"], "mean": entry["mean"], "least": entry["least"],
            "most": entry["most"], "last": entry["last"], "last_at": entry["last_at"],
            "median": median, "p90": p90, "kept": n}


def _entry_ok(e) -> bool:
    return (isinstance(e, dict) and isinstance(e.get("count"), int)
            and not isinstance(e.get("count"), bool) and e["count"] >= 1
            and all(_number(e.get(k)) for k in ("mean", "least", "most", "last", "last_at"))
            and isinstance(e.get("samples"), list)
            and 1 <= len(e["samples"]) <= SAMPLES_KEPT
            and all(_number(x) for x in e["samples"])
            and ("beside" not in e
                 or (isinstance(e["beside"], list) and len(e["beside"]) == len(e["samples"])
                     and all(b is None or _count(b) for b in e["beside"]))))


def _summary_ok(doc) -> bool:
    models = doc.get("models") if isinstance(doc, dict) else None
    return (isinstance(doc, dict) and doc.get("version") == VERSION
            and isinstance(models, dict)
            and all(isinstance(key, str) and key and isinstance(measures, dict)
                    and all(isinstance(name, str) and name and _entry_ok(e)
                            for name, e in measures.items())
                    for key, measures in models.items()))


class ModelStats:
    """The summary kept at *path* - `<folder>/runner/model_stats.json`."""

    def __init__(self, path):
        self.path = Path(path)

    def replace(self, src, dst) -> None:
        """The swap. A row makes it fail, to see nothing half written."""
        os.replace(src, dst)

    def read(self) -> dict:
        """The summary as kept; an empty one when nothing was ever kept.
        StatsUnreadable when the file is there and is not a summary."""
        with _LOCK:
            return self._read()

    def note(self, model, measure, value, at=None, beside=None) -> dict:
        """Fold one sample in, and keep the summary whole. -> the measure's
        entry as kept. A sample that is not a finite number, or a model or
        measure that is not a name, is refused (ValueError) before the file
        is touched; so is a file that cannot be read (StatsUnreadable), and
        a `beside` that is not a count (S17)."""
        at = time.time() if at is None else at
        _beside(beside)
        with _LOCK:
            summary = self._read()
            entry = fold(summary, model, measure, value, at, beside)
            self._write(summary)
            return json.loads(json.dumps(entry))

    def note_many(self, model, measures: dict, at=None, beside=None) -> dict:
        """Everything one thing measured - `{measure: value}`, all at *at*,
        each with the one *beside* (S17) - folded in the order given, in ONE
        write: never half of it kept. Each is checked as `note` checks one,
        all of them before the file is touched. -> each measure's entry as
        kept, in that order."""
        at = time.time() if at is None else at
        _name(model, "model")
        _sample(at, "its time")
        _beside(beside)
        for measure, value in measures.items():
            _name(measure, "measure")
            _sample(value, "a sample")
        with _LOCK:
            summary = self._read()
            got = {m: fold(summary, model, m, v, at, beside) for m, v in measures.items()}
            self._write(summary)
            return json.loads(json.dumps(got))

    def _read(self) -> dict:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return empty()
        except OSError as e:
            raise StatsUnreadable(f"{self.path} cannot be read: {e}") from None
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise StatsUnreadable(f"{self.path} is not JSON: {e}") from None
        if not _summary_ok(doc):
            raise StatsUnreadable(f"{self.path} is not a summary this runner can read "
                                  f"(version {VERSION})")
        return doc

    def _write(self, summary: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}."
                                  f"{threading.get_ident()}.{random.randrange(16 ** 8):08x}.tmp")
        tmp.write_bytes((json.dumps(summary, indent=1, ensure_ascii=False) + "\n")
                        .encode("utf-8"))
        try:
            for attempt in range(SWAP_TRIES):
                try:
                    self.replace(tmp, self.path)
                    return
                except PermissionError:
                    if attempt == SWAP_TRIES - 1:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        finally:
            tmp.unlink(missing_ok=True)          # gone already once it was swapped in
