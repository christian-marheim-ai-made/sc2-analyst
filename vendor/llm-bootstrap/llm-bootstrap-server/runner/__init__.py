"""The runner: model backends, and the manager that keeps them side by side.

    base.py      what every backend is - GenParams, Backend, the two errors
    config.py    the runner's settings and its models, one TOML file
    mock.py      the stand-in model: no weights, no GPU, no network
    manager.py   models side by side - one load of each, loaded one at a
                 time - inside a pipeline that holds the card: the loads
                 table, the lease, the events file, load timing; and the
                 work, as a coordinator's watch reads it
    memlog.py    a model's log, read for the lines that say the card's
                 memory ran out - llama.cpp's own

The lease is exported beside the manager because it is the door a second
holder comes through: a seam reachable only by importing `runner.manager`
teaches the next arrival to reach past the package into the file. The
pipeline's refusals, a request's terms and a degraded card's refusal are
exported for the same reason, and the record's checks (`max_concurrency`,
`beside_faults`) with them.
"""
from __future__ import annotations

from .base import Backend, BackendError, Cancelled, GenParams, strip_thinking
from .config import Config, ModelSpec
from .manager import (ConcurrencyViolation, Degraded, Lease, LeaseHeld, ModelManager, NoCard,
                      PipelineRefused, Terms, beside_faults, max_concurrency, orphaned_loads,
                      read_events, side_by_side)
from .mock import MockBackend

__all__ = [
    "Backend", "BackendError", "Cancelled", "GenParams", "strip_thinking",
    "Config", "ModelSpec", "MockBackend",
    "ModelManager", "ConcurrencyViolation", "Degraded", "Lease", "LeaseHeld",
    "NoCard", "PipelineRefused", "Terms",
    "max_concurrency", "side_by_side", "beside_faults", "orphaned_loads", "read_events",
]
