"""S8 · The model stage's one door: llm-bootstrap (vendored), never a runner of our own.

A `Session` starts the bootstrap's server as a child, opens one pipeline (the card held from open to
close, `keep_s` 0), asks, and in every case closes the pipeline and stops the server (a `finally`):
the vendored copy's H10 gap - `start_server` does not bind the server to its starter - is covered
here by never leaving that block without `stop()`.

Settings: `config/runner.local.toml` when it exists (this machine's paths), else `config/runner.toml`
(the real card: the 27B thinker and the 24B wordsmith, one at a time; llama-server in `llama/`, the
models in `models/`, both relative to the repository) or `config/runner.mock.toml` (the bootstrap's stand-in: no card, no lock, made-up words - for the
suite). The answer's JSON is parsed here; whether its content may be used is `assess.py`'s question.
"""
from __future__ import annotations

import json
import re
import sys
import time
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BOOT = REPO / "vendor/llm-bootstrap/llm-bootstrap-server"
REAL = REPO / "config/runner.local.toml" if (REPO / "config/runner.local.toml").exists() else REPO / "config/runner.toml"
MOCK = REPO / "config/runner.mock.toml"


def resolve(settings: Path) -> Path:
    """The settings file to hand the bootstrap. It joins `models_dir` with a file name as written, so a
    relative `models_dir`, or a `llama_server` given as a relative path, would depend on the server's
    working folder. Such a file is copied to `<data home>/runner.resolved.toml` with those two values
    made absolute against the repository root; any other file is passed as it is."""
    text = Path(settings).read_text(encoding="utf-8")
    runner = tomllib.loads(text).get("runner", {})
    changed = text
    for key in ("llama_server", "models_dir"):
        v = runner.get(key)
        if not isinstance(v, str) or Path(v).is_absolute():
            continue
        if key == "llama_server" and "/" not in v and "\\" not in v:
            continue                      # a bare command, found on PATH
        absolute = (REPO / v).as_posix()
        changed = re.sub(r'(?m)^(\s*%s\s*=\s*)"[^"]*"' % key, lambda m: m.group(1) + '"%s"' % absolute, changed)
    if changed == text:
        return Path(settings)
    from . import settings as S
    out = S.data_home() / "runner.resolved.toml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(changed, encoding="utf-8")
    return out


def _rc():
    if str(BOOT) not in sys.path:
        sys.path.insert(0, str(BOOT))
    import reference_client
    return reference_client


def _free_port() -> int:
    import socket
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


class ModelFailed(RuntimeError):
    pass


class Session:
    def __init__(self, models, settings: Path = REAL, workspace: Path | None = None,
                 backend: str | None = None, label: str = "sc2-analyst"):
        self.models = list(models)
        self.settings = settings
        self.workspace = workspace
        self.backend = backend or ("mock" if settings == MOCK else "llamacpp")
        self.label = label
        self.server = self.app = self.pipe = None
        self.log: list[dict] = []

    def __enter__(self):
        rc = _rc()
        # a port of its own: the server's default (7072) is held by any other session running -
        # measured 2026-10-26, the suite's stand-in could not start beside a run on the card
        args = ["--backend", self.backend, "--runner", str(resolve(self.settings)), "--port", str(_free_port())]
        if self.backend in ("real", "llamacpp"):  # the server refuses --name on the stand-in
            args += ["--name", self.label]
        if self.workspace is not None:
            self.workspace.mkdir(parents=True, exist_ok=True)
            args += ["--workspace", str(self.workspace)]
        self.server = rc.start_server(args, start_s=60.0)
        try:
            self.app = self.server.client()
            self.pipe = self.app.open(models=self.models[:1], keep_s=0, label=self.label, wait_s=60)
            for m in self.models[:1]:
                self.load(m)
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise
        return self

    def load(self, model: str, within_s: float = 900.0) -> float:
        """Bring `model` up inside the session; -> seconds it took. A wait is at most 60 s a call."""
        t0 = time.time()
        while time.time() - t0 < within_s:
            st = self.app.load(model, wait_s=60).get("model", {})
            if st.get("state") == "up":
                self.log.append({"event": "up", "model": model, "s": round(time.time() - t0, 1)})
                return time.time() - t0
            if st.get("state") == "failed":
                raise ModelFailed("%s failed to load: %s" % (model, st.get("why")))
        raise ModelFailed("%s not up within %g s" % (model, within_s))

    def unload(self, model: str) -> None:
        self.app.unload(model)

    def swap(self, old: str, new: str) -> float:
        self.unload(old)
        return self.load(new)

    def ask(self, model: str, system: str, user: str, schema: dict | None = None,
            max_tokens: int = 6000, reasoning_budget: int | None = 3000, temperature: float | None = None,
            name: str = "ask", within_s: float = 1800.0) -> dict:
        gen = {"model": model, "max_tokens": max_tokens}
        if schema is not None:
            gen["json_schema"] = schema
        if reasoning_budget is not None:
            gen["reasoning_budget_tokens"] = reasoning_budget
        if temperature is not None:
            gen["temperature"] = temperature
        t0 = time.time()
        job = self.app.submit(name, {"messages": [{"role": "system", "content": system},
                                                  {"role": "user", "content": user}]}, gen, name=name)
        res = self.app.wait(job, within_s=within_s)
        status = res.get("job", {}).get("status")
        cand = (res.get("candidates") or [{}])[0]
        try:
            self.app.take(job)
        except Exception:  # the answer is already in hand; a failed forget leaves only a record row
            pass
        out = {"status": status, "text": cand.get("text"), "usage": cand.get("usage"),
               "timings": cand.get("timings"), "seconds": round(time.time() - t0, 1),
               "error": res.get("job", {}).get("error"), "model": model}
        out["json"] = None
        if out["text"]:
            try:
                out["json"] = json.loads(out["text"])
            except ValueError:
                out["json"] = None
        self.log.append({"event": "ask", "name": name, "model": model, "status": status,
                         "seconds": out["seconds"], "usage": out["usage"]})
        if status != "done":
            raise ModelFailed("%s: job %s - %s" % (name, status, out["error"]))
        return out

    def __exit__(self, *exc):
        try:
            if self.app is not None and self.pipe is not None:
                try:
                    self.app.close(self.pipe["id"])
                except Exception:
                    pass
        finally:
            if self.server is not None:
                self.server.stop()
        return False
