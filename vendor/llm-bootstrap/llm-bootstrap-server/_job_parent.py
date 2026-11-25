"""The PARENT test_llamacpp.py kills the hard way.

    python _job_parent.py job|bare <marker> <out-file>
    python _job_parent.py backend|backend-bare <out-file> <models-dir> <gguf> <log-dir> <port>

A SEPARATE PROCESS BECAUSE THE MEASUREMENT NEEDS ONE: the question is what
happens when a parent dies by TerminateProcess - no atexit, no finally - and a
test cannot terminate itself and then go on to assert anything. So the thing
that dies is here, and the thing that measures is there.

    job / bare              one long-sleeping child, in a kill-on-close job or
                            not; `<marker>-child` and the tree's folder are on
                            the child's command line
    backend / backend-bare  the runner's own llama.cpp backend, started against
                            the stand-in llama-server and held MID-LOAD (the
                            stand-in answers 503 for a minute) - so a kill lands
                            inside the health wait, the window that matters. In
                            backend-bare the job is switched off: the control.
                            The stand-in listens on <port>: the row's, of the
                            block of the suite's range its copy claimed - never
                            one picked here from the whole range, which can be
                            of another copy's block (`_suites.QuietPorts`).

The out-file gets the child's pid (job/bare), or "<load id>\\t<pid>" once the
stand-in is running (backend modes). Then this sleeps until it is killed.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
#: longer than any run of the suite: a child that exits by itself would be
#: measuring the sleep, not the job
CHILD_SLEEP_S = 900


def sleeping_child(mode: str, marker: str, out: Path) -> None:
    # its tree's folder on its command line too, beside its marker: a child a
    # run left behind is found by the run's folder - a sweep's reap finds
    # what outlived it that way - never by time (H4's R7)
    child = subprocess.Popen([sys.executable, "-c",
                              f"import time; time.sleep({CHILD_SLEEP_S})", f"{marker}-child",
                              str(HERE)], creationflags=NO_WINDOW)
    job = None
    if mode == "job":
        from runner.jobobject import KillOnCloseJob
        job = KillOnCloseJob()
        job.add(child.pid)
    out.write_bytes(f"{child.pid}\n".encode("utf-8"))      # LF: write_text is CRLF on Windows
    while True:                                             # `job` stays referenced: the mechanism
        time.sleep(1.0)
        assert job is None or job.open


def backend_child(mode: str, out: Path, models_dir: str, gguf: str, log_dir: str,
                  port: int) -> None:
    from runner import manager as _manager                  # noqa: F401  (puts the contract on the path)
    import ids                                              # noqa: E402
    from runner.config import ModelSpec
    from runner.llamacpp import LlamaCppBackend

    if mode == "backend-bare":
        LlamaCppBackend._hold_in_kill_on_close_job = lambda self: None
    spec = ModelSpec(key="fake", label="fake", file=gguf, extra_args=["--fake-load-s", "60"])
    cfg = {"llama_server": [sys.executable, str(HERE / "fake_llama_server.py")],
           "startup_timeout_s": 120.0, "health_poll_s": 0.05, "shutdown_timeout_s": 10.0}
    backend = LlamaCppBackend(spec, models_dir, cfg, log_dir, port)
    backend.load_id = ids.mint_actor("load")
    threading.Thread(target=backend.start, daemon=True).start()
    # reported as soon as it EXISTS - not once the job holds it: a backend
    # that put it in the job late (after the health wait, say) must be seen
    # to fail. The test's kill lands more than a second later - it gives the
    # backend's own assignment that long - so the assignment has had time.
    deadline = time.monotonic() + 60
    while backend.pid is None and time.monotonic() < deadline:
        time.sleep(0.02)
    out.write_bytes(f"{backend.load_id}\t{backend.pid}\n".encode("utf-8"))   # LF, never CRLF
    while True:
        time.sleep(1.0)


def main() -> int:
    mode = sys.argv[1]
    if mode in ("job", "bare"):
        sleeping_child(mode, sys.argv[2], Path(sys.argv[3]))
    elif mode in ("backend", "backend-bare"):
        backend_child(mode, Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5],
                      int(sys.argv[6]))
    else:
        raise SystemExit(f"no mode {mode!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
