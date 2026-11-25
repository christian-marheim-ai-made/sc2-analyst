"""llama.cpp's llama-server as a child process: one per load.

A load is launched, waited for, IDENTIFIED, used and stopped:

  launch    the command line is built from the model's settings, on the port
            the manager chose for this load. `-fa` is given an explicit value
            (current builds require one). `--jinja` is the default and is not
            passed; `--no-jinja` would break `chat_template_kwargs`. An
            argument the build refuses ends the launch - so it is retried ONCE
            without the optional flags, and says which it dropped. The
            settings file's own extra arguments stay: a build that refuses one
            of those says so, rather than running without it.
  the job   on Windows the child is put in a kill-on-close job the moment it
            exists - before the health wait, which can take minutes - so a
            parent that dies by any means takes it along (see jobobject.py).
  health    `/health` answers 503 while the model loads; anything else but 200
            is a failure. The child exiting first is a failure, with its log.
            (Measured on the real card: the port is held, and 503 answered,
            from the first second of the load.)
  given up  a load can be given up from another thread (`abandon`): its
            process is ended at once, and `start` fails in the words it was
            given up with. A stop, or a card taken back, never waits one out.
  identity  every load is started with `--alias <load id>` and a key of its
            own (`--api-key`). Once healthy, `/v1/models` must name THIS load
            - a port answered by anything else is refused before one request
            is sent. And every request carries the key, so a runner that ever
            reaches the wrong port is refused 401 instead of being answered by
            the wrong model. The key is a guard against a misdirected request,
            not a secret from people at this machine: it is on the command line
            like every other argument. It is never written to the log, the
            record or an error.
  count     before it is asked, the request is counted by the model: the body
            the answer will be asked with, rendered by its own chat template
            (`/apply-template`, the chat route's own parse) and counted by its
            own tokenizer (`/tokenize`, with the first token and the special
            tokens, as the chat route tokenizes a prompt) - and the context
            its slot answers in, as the model says it (`/props`), which is
            not always the `-c` it was started with: llama-server caps it at
            the model's training context. Read from b10724's source; that the
            count equals the answer's `usage.prompt_tokens` on a real model is
            owed to the real card.
  stream    the request's messages as they were built - a system message and
            the turns before this one, or a prompt as one user message - and
            every parameter the job named, as it named it; server-sent events;
            reasoning streams in its own field. A cancel closes the socket -
            which is what aborts the slot in the server - and is honoured while
            the server is still working towards its first token, not only
            between tokens. A stream that ends with no finish reason and no
            [DONE] is a server that went away mid-answer: a failure, never a
            finished answer. A request longer than the context is refused by
            llama-server itself too: a PromptTooLong, the count's own reason.
  stop      terminate, wait until the process is gone AND its port is free:
            the next load must not start on top of a card still letting go.
  its log   every launch's output goes to its model's log in the bound folder
            (`logs/llama-server-<key>.log`), and this load's part of it -
            from its own launch on - is read for the lines that say the card's
            memory ran out, llama.cpp's own (`memory_errors`, `memlog`): the
            runner reports them, and the coordinator marks the rental degraded.

`llama_server` in the settings is a path, or a list - an interpreter and a
script - which is how a stand-in with the same command line takes its place.

What is not llama.cpp's own - the launch into the model's log, the
kill-on-close job, the give-up, the log read for the card's memory, the stop -
is `ModelServer`'s, which whisper-server's engine (`runner/whisper.py`) is
built on too: one process handling for every engine, started through the one
`SPAWN`.
"""
from __future__ import annotations

import http.client
import json
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator

from . import memlog
from .base import Backend, BackendError, Cancelled, GenParams, PromptTooLong, as_messages
from .gguf import GGUFError, check_against_spec, read_header
from .jobobject import JobObjectError, KillOnCloseJob

#: HOW LONG ONE OF A COUNT'S CALLS MAY TAKE. A count asks for no inference:
#: llama-server renders a template and tokenizes a text on its HTTP threads,
#: beside any generation, without the model's queue. A server silent this
#: long over that is not answering. How long a count takes on the longest
#: prompt a model here takes is owed to the real card.
COUNT_TIMEOUT_S = 60.0

#: flags a build may not know; a launch refused for one of them is retried once
#: without them. `--alias` is NOT one: it carries the load's identity, which a
#: startup check reads off the command line - a build too old for it is too old
#: to run here.
OPTIONAL_FLAG_NAMES = ("--reasoning", "-fa", "-ctk", "-ctv")
#: how llama-server says it did not understand an argument
BAD_FLAG_SIGNS = ("invalid argument", "unknown value", "unrecognized",
                  "error while handling argument")

#: THE ONE PLACE A MODEL SERVER IS STARTED. A suite may put a guard here that
#: lets nothing through but its own stand-in.
SPAWN = subprocess.Popen


def command_prefix(llama_server) -> list:
    """llama-server as the start of a command line: a path, or a list."""
    if isinstance(llama_server, (list, tuple)):
        return [str(a) for a in llama_server]
    return [str(llama_server)]


def port_in_use(host: str, port: int, timeout: float = 0.35) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, int(port))) == 0


def wait_exited(pid: int, timeout: float) -> bool:
    """Wait until process *pid* has really exited; -> whether it has.

    Popen's own `wait` cannot be trusted for this on Windows: when `terminate`
    finds the process already terminating - killed by somebody else, or
    crashing - it records the exit code the process already has, and `wait`
    then returns at once. Measured on the real card, a killed llama-server
    takes about two seconds to exit after that, and the next load started
    beside it. The process handle is signalled only when the last thread has
    gone, so that is what is waited on. The pid cannot have been reused: the
    caller's Popen still holds a handle to it. POSIX: Popen's `wait` reaps, and
    is enough."""
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k.OpenProcess(0x00100000, False, int(pid))            # SYNCHRONIZE
    if not handle:
        return True                                                 # nothing left to open
    try:
        return k.WaitForSingleObject(handle, int(max(0.0, timeout) * 1000)) == 0
    finally:
        k.CloseHandle(handle)


class ModelServer(Backend):
    """A MODEL'S SERVER AS A CHILD PROCESS, one per load - what every engine's
    server shares (PLAN-4 D9, D10): llama-server's (`LlamaCppBackend`, below)
    and whisper-server's (`runner/whisper.py`). An engine brings its command
    line, its start - how it is known to be up, and proven its own - and its
    answer; this is the rest, the same for both:

      the launch  its output goes to its model's log in the bound folder
                  (`logs/<program>-<key>.log`), each launch under a line of
                  its own; it is started where every model server is started
                  (`SPAWN`), a console of its own it never shows; put in a
                  kill-on-close job the moment it exists; and named to whoever
                  asked (`on_spawn`) with the proof its command line carries
      given up    from another thread (`abandon`): the process is ended at
                  once, and `start` fails in the words it was given up with,
                  within `base.GIVE_UP_S`
      its log     this load's own part of it - from its own launch on - is read
                  for the lines that say the card's memory ran out, ggml's own
                  (`memory_errors`, `memlog`): the runner reports them, and the
                  coordinator marks the rental degraded
      stop        terminate, wait until the process is gone AND its port is
                  free: the next load must not start on top of a card still
                  letting go
    """

    #: the program, as a person reads it in what a load says
    program_name = "model-server"
    #: the `[runner]` setting that names it
    program_setting = ""

    def __init__(self, spec, models_dir, runner_cfg: dict, log_dir, port):
        super().__init__(spec.key, spec.label)
        self.spec = spec
        self.models_dir = Path(models_dir)
        self.cfg = runner_cfg
        self.host = "127.0.0.1"
        self.port = int(port) if port else None
        self.log_dir = Path(log_dir) if log_dir else None
        self.proc: subprocess.Popen | None = None
        self._log_fh = None
        #: the command as launched (a secret on it blanked out)
        self.command: list = []
        #: what this load had to say - a problem with its file, a flag
        #: dropped, a job that could not be built. The manager puts them on
        #: the load's line.
        self.notes: list = []
        self._job: KillOnCloseJob | None = None
        self.job_kill_on_close = False
        #: why this load was given up (`abandon`), once it was
        self._given_up: str | None = None
        self._starting = False
        #: where this load's own part of its model's log begins - the log is
        #: the model's, appended to by every load of it - and then how far it
        #: has been read for the lines that say the card's memory ran out
        #: (`memory_errors`); None until its first launch
        self._log_read: int | None = None
        self._log_read_lock = threading.Lock()

    @property
    def log_path(self):
        return (self.log_dir / f"{self.program_name}-{self.spec.key}.log"
                if self.log_dir else None)

    def _blank(self, text: str) -> str:
        """*text* with any secret of this load's blanked out: none here."""
        return text

    # ----------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._starting = True
        try:
            self._start()
        finally:
            self._starting = False

    def _start(self) -> None:                               # pragma: no cover
        raise NotImplementedError

    def abandon(self, reason: str) -> None:
        """Give this load up where it stands - asked from ANOTHER thread while
        `start` waits for the model to come up. The process is ended at once,
        which lets the card go at once (measured on the real card: its memory
        is back within a third of a second); `start` sees it end, and fails in
        these words - never taken for a start that failed on its own. A load
        already up is not touched: taking a model off is `stop`'s."""
        self._given_up = str(reason)
        proc = self.proc
        if self._starting and proc is not None:
            try:
                proc.kill()
            except Exception:                               # noqa: BLE001
                pass

    def _mark_log(self) -> None:
        """This load's part of its model's log begins where the log ends now:
        an earlier load's lines are not this one's. Once, before its first
        launch."""
        with self._log_read_lock:
            if self._log_read is None:
                try:
                    self._log_read = self.log_path.stat().st_size
                except OSError:
                    self._log_read = 0

    def _spawn(self, argv: list, banner: str) -> None:
        """Launch *argv*, its output into the model's log under a line of its
        own (*banner*, and the command as `self.command` shows it), through
        the one place a model server is started (`SPAWN`); then hold it in a
        kill-on-close job and name it to whoever asked - before the wait for
        it to come up, which can take minutes."""
        self._log_fh = open(self.log_path, "a", encoding="utf-8", errors="replace")
        self._log_fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {self.load_id} "
                           f"{banner} =====\n{' '.join(self.command)}\n")
        self._log_fh.flush()
        kwargs: dict = {"stdout": self._log_fh, "stderr": subprocess.STDOUT,
                        "stdin": subprocess.DEVNULL}
        if sys.platform == "win32":
            # CREATE_NO_WINDOW is not cosmetic: without it the child is a
            # console process attached to OUR console, and a parent started
            # from a shell that has since exited leaves it a console whose
            # server is gone - the child then dies in the loader, writing
            # nothing. CREATE_NEW_PROCESS_GROUP keeps our Ctrl-C off it: it
            # is stopped explicitly.
            kwargs["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                       | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        else:
            kwargs["start_new_session"] = True
        try:
            self.proc = SPAWN(argv, **kwargs)
        except FileNotFoundError:
            self._hard_stop()
            raise BackendError(f"could not run {argv[0]!r}: put {self.program_name} on PATH, or "
                               f"set [runner] {self.program_setting} to its full path") from None
        self.pid = self.proc.pid
        self.mark("spawn")                              # the steps of its start (D12)
        self._hold_in_kill_on_close_job()               # before the wait, which takes minutes
        self._tell_spawned()

    def _tell_spawned(self) -> None:
        """Say what is on the card now - this process, and the proof its
        command line carries (this load's id) - to whoever asked (`on_spawn`),
        before the model goes up: a coordinator told at once can end it, on
        that proof, if this server hangs mid-load. Never fails the load."""
        tell = getattr(self, "on_spawn", None)
        if tell is None or self.proc is None:
            return
        try:
            tell(self.proc.pid, self.load_id)
        except Exception:                                   # noqa: BLE001
            pass

    def _hold_in_kill_on_close_job(self) -> None:
        """Put the child just spawned in a kill-on-close job.

        AFTER `Popen`, and that ordering matters twice over: the job handle
        does not exist while `CreateProcess` runs, so the child cannot inherit
        a handle to its own job; and the one window it leaves - between the
        process starting and the assignment - holds nothing, since a model
        server starts no children of its own.

        A FAILURE HERE IS NOT FATAL, on purpose: refusing to load a model
        because a second line of defence could not be built would trade a
        stranded card on an ABNORMAL exit for no model on a normal one. It is
        said out loud, and on the load's line in the record."""
        if self.proc is None or sys.platform != "win32":
            return                                          # POSIX: its own session, above
        try:
            job = KillOnCloseJob()
            job.add(self.proc.pid)
        except JobObjectError as exc:
            self.job_kill_on_close = False
            self._note(f"could NOT put {self.program_name} (pid {self.proc.pid}) in a "
                       f"kill-on-close job: {exc}. The model loads, but if this process is "
                       f"killed or crashes, {self.program_name} will keep the card.")
            return
        self._job, self.job_kill_on_close = job, True

    def _release_job(self) -> None:
        """Close our handle to the job - only once the child is known to be
        gone: closing it first would kill the server by kernel decree instead
        of letting its own shutdown finish."""
        if self._job is not None:
            self._job.close()
            self._job = None
        self.job_kill_on_close = False

    def _note(self, message: str) -> None:
        message = self._blank(message)
        self.notes.append(message)
        print(f"[runner] {message}", flush=True)
        if self._log_fh:
            self._log_fh.write(f"[runner] {message}\n")
            self._log_fh.flush()

    def memory_errors(self) -> list:
        """The lines of this load's own part of its log - from its launch on,
        whole lines, each once - that say the card's memory ran out, in
        ggml's own words (`memlog`: the lines llama.cpp b10724 and the ggml
        whisper.cpp 1.9.1 carries write)."""
        with self._log_read_lock:
            if self.log_path is None or self._log_read is None:
                return []
            lines, self._log_read = memlog.read_new(self.log_path, self._log_read)
        return lines

    def _log_tail(self, lines: int = 25) -> str:
        try:
            return "\n".join(self.log_path.read_text(encoding="utf-8", errors="replace")
                             .splitlines()[-lines:])
        except (OSError, AttributeError):
            return "(no log)"

    def _hard_stop(self) -> None:
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=15)
                wait_exited(self.proc.pid, 15)
            except Exception:                               # noqa: BLE001
                pass
        self._release_job()                                 # after the wait - see there
        if self._log_fh:
            try:
                self._log_fh.close()
            except Exception:                               # noqa: BLE001
                pass
            self._log_fh = None
        self.proc = None
        self.pid = None

    def is_alive(self) -> bool:
        """Is the server this object started still running - AND still
        listening? Asked before every warm reuse, so it sends no request: the
        process answers, and so does its port, with a connection opened and
        closed that a live server accepts at once. The port is asked because a
        process is not a server: measured on the real card, a llama-server
        killed underneath takes about two seconds to finish exiting and
        answers nothing the whole time, and a reuse that asked only the
        process handed the next job that corpse. It is sound because every
        backend here owns its process: an orphan is reclaimed, never
        adopted."""
        return (self.proc is not None and self.proc.poll() is None
                and port_in_use(self.host, self.port))

    def stop(self) -> None:
        """Terminate, and do not return until the process is gone AND its port
        is free."""
        if self.proc is None:
            return
        timeout = float(self.cfg.get("shutdown_timeout_s", 60))
        proc = self.proc
        try:
            proc.terminate()
        except Exception:                                   # noqa: BLE001
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._note(f"{self.program_name} (pid {proc.pid}) ignored the terminate; killing it")
            try:
                proc.kill()
                proc.wait(timeout=timeout)
            except Exception as exc:                        # noqa: BLE001
                raise BackendError(f"could not stop {self.program_name} pid {proc.pid}: "
                                   f"{exc}") from None
        if not wait_exited(proc.pid, timeout):
            raise BackendError(f"{self.program_name} pid {proc.pid} was stopped and has not "
                               f"exited after {timeout:g}s - refusing to load another model "
                               f"beside it")
        self.proc = None
        self.pid = None
        self._release_job()                                 # the process is gone: it reaps a corpse
        if self._log_fh:
            try:
                self._log_fh.close()
            finally:
                self._log_fh = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not port_in_use(self.host, self.port):
                return
            time.sleep(0.05)
        raise BackendError(f"{self.program_name} exited but port {self.port} is still held - "
                           f"refusing to load another model on top of it")


class LlamaCppBackend(ModelServer):
    kind = "llamacpp"
    program_name = "llama-server"
    program_setting = "llama_server"

    def __init__(self, spec, models_dir, runner_cfg: dict, log_dir, port):
        super().__init__(spec, models_dir, runner_cfg, log_dir, port)
        self._key: str | None = None
        self.dropped_flags: list = []
        #: the context this load's slot answers in, as the model says it -
        #: read at its first count, and the same for as long as it is up
        self._n_ctx: int | None = None

    # -------------------------------------------------------- command line

    def build_command(self, drop_optional: bool = False, key: str | None = None) -> list:
        gguf = self.spec.gguf_path(self.models_dir)
        cmd = [*command_prefix(self.cfg.get("llama_server", "llama-server")),
               "-m", str(gguf),
               "--host", self.host,
               "--port", str(self.port),
               "-ngl", str(self.spec.n_gpu_layers),
               "-c", str(self.spec.ctx),
               "--alias", str(self.load_id)]
        if key:
            cmd += ["--api-key", key]
        if not drop_optional:
            cmd += ["-ctk", self.spec.cache_type_k, "-ctv", self.spec.cache_type_v]
            cmd += ["-fa", str(self.cfg.get("flash_attn", "on"))]   # an explicit value, always
            if self.spec.disable_thinking:
                cmd += ["--reasoning", "off"]
        cmd += [str(a) for a in (self.spec.extra_args or [])]
        return cmd

    def _blank(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    # ----------------------------------------------------------- lifecycle

    def _start(self) -> None:
        if self._given_up:
            raise BackendError(f"the load was given up: {self._given_up}")
        if self.log_dir is None:
            raise BackendError("a real model needs a bound folder: its launch is logged "
                               "there, and a launch nobody can read cannot be diagnosed")
        if not self.load_id or not self.port:
            raise BackendError("a load needs its id and its port before it starts; "
                               "the manager gives both")
        gguf = self.spec.gguf_path(self.models_dir)
        if not gguf.is_file():
            raise BackendError(f"model file not found: {gguf}. Point [models.{self.spec.key}] "
                               f"file at a GGUF you have.")
        try:
            info = read_header(gguf, read_tensors=False)
        except GGUFError as exc:
            raise BackendError(f"{gguf.name} is not a usable GGUF: {exc}. The download is "
                               f"probably truncated - fetch it again.") from None
        except OSError as exc:
            raise BackendError(f"cannot read {gguf}: {exc}") from None
        for problem in check_against_spec(info, self.spec):
            self._note(f"{gguf.name}: {problem}")
        if port_in_use(self.host, self.port):
            raise BackendError(f"port {self.port} is already in use - something else holds it")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._mark_log()
        self._key = secrets.token_urlsafe(24)
        for attempt, drop in enumerate((False, True)):
            real = self.build_command(drop_optional=drop, key=self._key)
            self.command = ["***" if a == self._key else a for a in real]
            if drop:
                full = self.build_command(False)
                self.dropped_flags = [f for f in OPTIONAL_FLAG_NAMES if f in full]
            self._spawn(real, f"launch attempt {attempt + 1}")
            try:
                if self._given_up:
                    # given up while the process was being started, before
                    # there was one to end
                    raise BackendError(f"the load was given up: {self._given_up}")
                self._await_health()
                self._check_identity()
                self.mark("identity")
            except BackendError as exc:
                tail = self._blank(self._log_tail())
                self._hard_stop()
                if self._given_up:
                    # in its own words: not a server that failed, and never a
                    # build to try again without the optional flags
                    raise BackendError(f"the load was given up: {self._given_up}") from None
                if attempt == 0 and any(s in tail.lower() for s in BAD_FLAG_SIGNS):
                    self._note(f"llama-server refused an argument; trying once more without "
                               f"the optional flags ({', '.join(OPTIONAL_FLAG_NAMES)}) - this "
                               f"build may predate them. Log: {self.log_path}")
                    continue
                raise BackendError(f"{self._blank(str(exc))}\n--- llama-server log tail ---\n"
                                   f"{tail}") from None
            return

    def _url(self, path: str) -> str:
        return f"http://{self.host}:{self.port}{path}"

    def _headers(self, **more) -> dict:
        return {"Authorization": f"Bearer {self._key}", **more}

    def _await_health(self) -> None:
        limit = float(self.cfg.get("startup_timeout_s", 600))
        poll = float(self.cfg.get("health_poll_s", 0.1))
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise BackendError(f"llama-server exited with code {self.proc.returncode} "
                                   f"before becoming healthy")
            try:
                req = urllib.request.Request(self._url("/health"), headers=self._headers())
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        if "port" not in self.steps:
                            self.mark("port")               # it answered: its port is bound
                        self.mark("healthy")
                        return
            except urllib.error.HTTPError as exc:
                exc.close()                                 # a refusal is a response too
                if "port" not in self.steps:
                    self.mark("port")                       # it answered: its port is bound
                if exc.code != 503:                         # 503 is "still loading"
                    raise BackendError(f"/health answered HTTP {exc.code}") from None
            except (urllib.error.URLError, http.client.HTTPException, OSError):
                pass                                        # not listening yet
            time.sleep(poll)
        raise BackendError(f"llama-server did not become healthy within {limit:g}s")

    def _check_identity(self) -> None:
        """The server that answered `/health` must be THIS load. Anything else
        on the port - another load, another program - is refused here, before
        a request is sent. The key is proven by every request after this one."""
        try:
            req = urllib.request.Request(self._url("/v1/models"), headers=self._headers())
            with urllib.request.urlopen(req, timeout=5) as resp:
                doc = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 401:
                raise BackendError(f"the server on port {self.port} refused this load's key "
                                   f"(401): it is not {self.load_id}") from None
            raise BackendError(f"/v1/models answered HTTP {exc.code}") from None
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            raise BackendError(f"could not ask the server on port {self.port} what it is: "
                               f"{exc}") from None
        names = [m.get("id") for m in (doc.get("data") or []) if isinstance(m, dict)] \
            if isinstance(doc, dict) else []
        if self.load_id not in names:
            raise BackendError(f"the server on port {self.port} answers as "
                               f"{', '.join(map(str, names)) or 'nothing'}, not {self.load_id}")

    # ---------------------------------------------------------- generation

    def _body(self, messages, params: GenParams) -> dict:
        """The chat route's body for this request - ONE builder for the answer
        and for its count, so the count renders exactly what the answer will
        be asked with: the messages as they were built, every parameter the
        job named, and the model's own defaults where it named none."""
        body = {"model": self.load_id,
                "messages": as_messages(messages),      # as the request was built
                "stream": True,
                "stream_options": {"include_usage": True},
                **params.as_body()}
        if self.spec.disable_thinking:
            # the model's setting, where the job named none: a default, not a lid
            kwargs = dict(body.get("chat_template_kwargs") or {})
            kwargs.setdefault("enable_thinking", False)    # a JSON boolean, not "false"
            body["chat_template_kwargs"] = kwargs
        return body

    def count(self, messages, params: GenParams,
              cancel: threading.Event | None = None) -> dict:
        """The request as this model will read it, counted by the model -
        llama-server b10724, as its source reads: `/apply-template` parses the
        body as the chat route does and answers the prompt its template
        renders, and `/tokenize` - with the first token and the special
        tokens, as the chat route tokenizes a prompt - counts it. The context
        is the model's own word (`/props`), read once a load."""
        body = self._body(messages, params)
        rendered = self._ask("/apply-template", body, cancel)
        prompt = rendered.get("prompt") if isinstance(rendered, dict) else None
        if not isinstance(prompt, str):
            raise BackendError(f"llama-server on port {self.port} rendered no prompt to count "
                               f"(/apply-template answered {str(rendered)[:200]})")
        said = self._ask("/tokenize", {"content": prompt, "add_special": True,
                                       "parse_special": True}, cancel)
        tokens = said.get("tokens") if isinstance(said, dict) else None
        if not isinstance(tokens, list):
            raise BackendError(f"llama-server on port {self.port} counted no tokens "
                               f"(/tokenize answered {str(said)[:200]})")
        return {"prompt_tokens": len(tokens), "context": self._context(cancel)}

    def _context(self, cancel) -> int:
        """The context this load's slot answers in, as the model says it:
        `/props`' `default_generation_settings.n_ctx` - b10724's slot context,
        the `-c` it was started with capped at the model's training
        context. Read once: it is the same for as long as the load is up."""
        if self._n_ctx is None:
            props = self._ask("/props", None, cancel)
            n = ((props.get("default_generation_settings") or {}).get("n_ctx")
                 if isinstance(props, dict) else None)
            if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
                raise BackendError(f"llama-server on port {self.port} did not say its context "
                                   f"(/props answered {str(props)[:200]})")
            self._n_ctx = n
        return self._n_ctx

    def _ask(self, path: str, doc, cancel):
        """One of the count's calls: `doc` POSTed as JSON, or a GET without
        one; with this load's key; bounded by COUNT_TIMEOUT_S; -> the answer's
        JSON. A refusal raises in llama-server's own words, as the chat route's
        does. A cancel is heard before each call."""
        if cancel is not None and cancel.is_set():
            raise Cancelled("cancelled before the model counted the request")
        data = None if doc is None else json.dumps(doc).encode("utf-8")
        req = urllib.request.Request(
            self._url(path), data=data, method="GET" if doc is None else "POST",
            headers=self._headers(**({"Content-Type": "application/json"} if data else {})))
        try:
            with urllib.request.urlopen(req, timeout=COUNT_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            with exc:
                try:
                    detail = self._blank(exc.read().decode("utf-8", "replace")[:600])
                except (OSError, http.client.HTTPException):
                    detail = ""
            if exc.code == 401:
                raise BackendError(f"the server on port {self.port} refused this load's key "
                                   f"(401): it is not {self.load_id}") from None
            raise refused(exc.code, detail, self.spec.key) from None
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            raise BackendError(f"could not ask llama-server on port {self.port} to count the "
                               f"request ({path}): {exc}") from None

    def stream(self, messages, params: GenParams,
               cancel: threading.Event | None = None) -> Iterator[dict]:
        body = self._body(messages, params)
        # the exact call, for the request record; the key travels in a header
        self.last_call = json.loads(json.dumps(body))
        if cancel is not None and cancel.is_set():
            raise Cancelled("cancelled before the request was sent")
        # THE SOCKET IS OURS, not urllib's: a cancel must be able to end it
        # from another thread (see `_shut`). Closing a response object does
        # not wake a read already waiting on its socket - measured on
        # Windows, a cancel that way waited out the server's whole silence -
        # and before the first token the server can be silent a long time.
        timeout = float(self.cfg.get("request_timeout_s", 1800))
        try:
            sock = socket.create_connection((self.host, self.port), timeout=timeout)
        except OSError as exc:
            raise BackendError(f"cannot reach llama-server on port {self.port}: {exc}") from None
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        conn.sock = sock
        try:
            conn.request("POST", "/v1/chat/completions", body=json.dumps(body).encode("utf-8"),
                         headers=self._headers(**{"Content-Type": "application/json",
                                                  "Accept": "text/event-stream"}))
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            _shut(sock)
            raise BackendError(f"cannot reach llama-server on port {self.port}: {exc}") from None
        if resp.status != 200:
            try:
                detail = self._blank(resp.read().decode("utf-8", "replace")[:600])
            except (OSError, http.client.HTTPException):
                detail = ""
            _shut(sock)
            if resp.status == 401:
                raise BackendError(f"the server on port {self.port} refused this load's key "
                                   f"(401): it is not {self.load_id}, and nothing it would "
                                   f"have said is used")
            raise refused(resp.status, detail, self.spec.key)
        done = threading.Event()

        def watch():
            while not done.is_set():
                if cancel.wait(0.05):
                    _shut(sock)                             # unblocks the read; aborts the slot
                    return

        if cancel is not None:
            threading.Thread(target=watch, name=f"cancel-{self.load_id}", daemon=True).start()
        finish_reason, usage, timings, said_done = None, {}, {}, False
        try:
            try:
                for raw in resp:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled("cancelled during generation")
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        said_done = True
                        break
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("usage"):
                        usage = obj["usage"]
                    if obj.get("timings"):
                        timings = obj["timings"]
                    for choice in obj.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            yield {"type": "token", "text": delta["content"]}
                        # a reasoning model's deliberation streams in its OWN field
                        if delta.get("reasoning_content"):
                            yield {"type": "reasoning", "text": delta["reasoning_content"]}
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
            except (OSError, ValueError, AttributeError, http.client.HTTPException) as exc:
                if cancel is not None and cancel.is_set():
                    raise Cancelled("cancelled during generation") from None
                raise BackendError(f"the stream from llama-server broke - it went away "
                                   f"mid-answer: {exc}") from None
            if cancel is not None and cancel.is_set():
                raise Cancelled("cancelled during generation")
            if not (said_done or finish_reason):
                # the socket closed with no finish reason and no [DONE]: the
                # server went away mid-answer, and a cut-off answer must not
                # be handed on as a finished one
                raise BackendError("the stream from llama-server ended before the server said "
                                   "it was done - it went away mid-answer")
        finally:
            done.set()
            try:
                resp.close()
            except Exception:                               # noqa: BLE001
                pass
            _shut(sock)
        yield {"type": "done", "finish_reason": finish_reason or "stop",
               "usage": usage, "timings": timings}


def refused(status: int, body: str, model_key: str) -> BackendError:
    """llama-server's refusal as the error to raise: a request longer than
    the context is a PromptTooLong - its job ends `prompt_too_long`, as one
    the count refused does - and any other a BackendError, in `refusal`'s
    words."""
    said = refusal(status, body, model_key)
    try:
        err = json.loads(body).get("error")
    except (ValueError, AttributeError):
        err = None
    if isinstance(err, dict) and err.get("type") == "exceed_context_size_error":
        return PromptTooLong(said)
    return BackendError(said)


def refusal(status: int, body: str, model_key: str) -> str:
    """llama-server's refusal of a request, as a sentence: its own `message`,
    never its JSON - and, for a request longer than the model's context, what
    to change. A body that is not llama-server's error JSON is passed on as it
    came: it is all there is to say."""
    try:
        err = json.loads(body).get("error")
    except (ValueError, AttributeError):
        err = None
    if not isinstance(err, dict) or not isinstance(err.get("message"), str):
        return f"HTTP {status} from llama-server: {body}"
    said = f"llama-server refused the request (HTTP {status}): {err['message']}"
    n, ctx = err.get("n_prompt_tokens"), err.get("n_ctx")
    if err.get("type") == "exceed_context_size_error" and isinstance(n, int) \
            and isinstance(ctx, int):
        # the count is the whole request - the chat template's own tokens too
        return (f"the request is {n:,} tokens and {model_key}'s context is {ctx:,}: "
                f"shorten the prompt, or raise [models.{model_key}] ctx. ({said})")
    return said


def _shut(sock) -> None:
    """End a socket so that a thread blocked reading it wakes NOW - which
    takes two different things on two systems, both measured:

      * POSIX wakes the reader on `shutdown`; closing the descriptor does not.
      * Windows does NOT wake it on `shutdown` (measured: a read blocked on a
        silent server returned only when the server next wrote, seconds
        later); only a real `closesocket` cancels it. And while a response
        still holds the socket, `sock.close()` is deferred - so the handle is
        DETACHED (the socket object forgets it, and can never close it twice)
        and closed here, directly. Measured: the blocked read then fails at
        once, and the server sees the client go.

    Safe to call twice: the second call finds nothing left to close."""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        fd = sock.detach()
        if fd >= 0:
            socket.close(fd)
    except OSError:
        pass
