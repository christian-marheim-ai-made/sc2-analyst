"""M2 - the measurement on the REAL card (PLAN-4 section 5), with the owner's go.

    set LLM_BOOTSTRAP_REAL_CARD=<runner settings: llama_server, models_dir, a text model>
    set LLM_BOOTSTRAP_REAL_WHISPER={"server": <whisper-server>, "audio": <a WAV>,
                                    "models": {"<key>": <model file>, ...}}
    py -3.12 measure_card.py [--runs 5] [--out <folder>]

A measurement, not a gate: it prints a table and keeps every number. Each arm
runs once thrown away, then `--runs` times. Every arm goes through the runner
and a real coordinator (this machine's card, read through nvidia-smi), with
the health poll at 0.1 s; the card is watched from outside (cardwatch.py)
throughout. What is measured, each run:

  * the load: from the session's open to every model up - the claim, and each
    load's own start to ready from the runner's record;
  * the first answer: the WAV on each whisper model, and a short prompt on the
    text model - from the request's own record (started to ended);
  * the unload: from the close to every process gone, and to the card's
    memory back within 300 MiB of where it was.

Then: the whisper models started ALL AT ONCE by a probe outside the runner,
and stopped one by one; the host's swap timed whole (the dictation pair
pinned, taken off, the text model up and answering, taken off, the pair up
again: "answer done -> whisper ready" as one number); and two answers at once
(S17) against the same two one after the other.

Each model file is read once before the first arm (the operating system's
cache), and free RAM and the card's baseline are written down with every run.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import test_real_card as R                                      # noqa: E402
from runner import read_events                                  # noqa: E402

EARS = json.loads(os.environ.get("LLM_BOOTSTRAP_REAL_WHISPER") or "null")
SHORT = R.SHORT
LONG = ("Write a short story of about one hundred and fifty words about a lighthouse "
        "keeper who finds a message in a bottle. Plain prose, no title.")


class _MemStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def free_ram_mib() -> int:
    st = _MemStatus()
    st.dwLength = ctypes.sizeof(_MemStatus)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
    return int(st.ullAvailPhys // (1024 * 1024))


class Case:
    """What the suite's server and client helpers need of a row."""

    def __init__(self, folder: Path, watch):
        self.dir, self.watch = folder, watch

    def check_foreign(self):
        if self.watch.foreign:
            raise RuntimeError("a model server this run did not start appeared: "
                               + "; ".join(self.watch.describe(p) for p in self.watch.foreign))


def write_settings(path: Path) -> Path:
    cfg, spec, _ = R.BASE
    r = {"backend": "real", "llama_server": cfg.runner["llama_server"],
         "whisper_server": EARS["server"], "models_dir": str(cfg.models_dir).replace("\\", "/"),
         "port": 0, "startup_timeout_s": 300.0, "health_poll_s": 0.1,
         "shutdown_timeout_s": 60.0, "idle_unload_s": 3600.0, "card_patience_s": 600.0,
         "max_loads": 4}
    lines = ["[runner]"] + [f"{k} = {R._toml(v)}" for k, v in r.items()]
    text = {"engine": "llamacpp", "label": "the text model", "file": spec.file, "ctx": spec.ctx,
            "n_gpu_layers": spec.n_gpu_layers, "cache_type_k": spec.cache_type_k,
            "cache_type_v": spec.cache_type_v, "temperature": spec.temperature,
            "disable_thinking": spec.disable_thinking}
    lines += ["", "[models.text]"] + [f"{k} = {R._toml(v)}" for k, v in text.items()]
    for key, file in EARS["models"].items():
        lines += ["", f"[models.{key}]", f"engine = {R._toml('whisper')}",
                  f"label = {R._toml(key)}", f"file = {R._toml(file)}"]
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return path


def read_through(path: str) -> float:
    """Read a file once, whole, so every arm meets it in the operating
    system's cache. -> seconds."""
    t = time.monotonic()
    with open(path, "rb") as f:
        while f.read(64 * 1024 * 1024):
            pass
    return time.monotonic() - t


# ------------------------------------------------------------------ the server

def wait_up(srv, models, timeout=300.0) -> float:
    t = time.monotonic()
    while True:
        pl = srv.health().get("pipeline") or {}
        if set(models) <= set(pl.get("up") or []):
            return time.monotonic() - t
        why = str(pl.get("why") or "")
        if "could not be loaded" in why:
            raise RuntimeError(why)
        if time.monotonic() - t > timeout:
            raise RuntimeError(f"not up in {timeout:g}s: {pl}")
        time.sleep(0.05)


def open_session(srv, models) -> str:
    status, out = srv.post("/api/pipeline/open", {"models": list(models), "keep_s": 0,
                                                  "label": "M2"}, timeout=90)
    if status != 200:
        raise RuntimeError(f"open answered {status}: {out}")
    return out["pipeline"]["id"]


def submit(srv, kind, model) -> str:
    if kind == "text":
        body = {"kind": "generate", "input": {"prompt": SHORT}, "gen": {"model": model,
                                                                        "max_tokens": 24}}
    elif kind == "long":
        body = {"kind": "generate", "input": {"prompt": LONG},
                "gen": {"model": model, "max_tokens": 220, "seed": 7}}
    else:
        body = {"kind": "transcribe", "gen": {"model": model},
                "input": {"audio": EARS["audio"], "fields": {"language": "en", "temperature": 0.0}}}
    status, out = srv.post("/api/jobs", body)
    if status != 200:
        raise RuntimeError(f"submit answered {status}: {out}")
    return out["job_id"]


def finish(srv, jid, timeout=300.0):
    t = time.monotonic()
    while True:
        job, cands = srv.job(jid)
        if job["status"] in R.FINISHED:
            if job["status"] != "done":
                raise RuntimeError(f"{jid} {job['status']}: {job.get('error')}")
            path = srv.ws / "requests" / job["correlation_id"] / "events.jsonl"
            # the record's `ended` line is written once the result is saved
            ev = R.until(lambda: (lambda x: x if any(e["event"] == "ended" for e in x)
                                  else None)(read_events(path)), 10.0) or read_events(path)
            s = next(e["ts"] for e in ev if e["event"] == "started")
            e = next(e["ts"] for e in ev if e["event"] == "ended")
            c = cands[0] if cands else {}
            return {"answer_s": round(e - s, 3), "started": s, "ended": e,
                    "tokens": c.get("tokens"), "text": (c.get("text") or "").strip()[:60]}
        if time.monotonic() - t > timeout:
            raise RuntimeError(f"{jid} still {job['status']} after {timeout:g}s")
        time.sleep(0.05)


def load_steps(srv, since: float) -> dict:
    """Each load since *since*, from the runner's record: {model: seconds from
    its load_start to its load_ok}, and the claim."""
    ev = [e for e in srv.events() if e.get("ts", 0) >= since]
    out, starts = {}, {}
    for e in ev:
        if e["event"] == "load_start":
            starts[e["model_key"]] = e["ts"]
        elif e["event"] == "load_ok" and e["model_key"] in starts:
            out[e["model_key"]] = round(e["ts"] - starts[e["model_key"]], 3)
    granted = next((e["ts"] for e in ev if e["event"] == "card_granted"), None)
    return {"loads": out, "claim_s": None if granted is None else round(granted - since, 3)}


def close(srv, pid, watch) -> dict:
    base_wall = time.time()
    t = time.monotonic()
    status, out = srv.post("/api/pipeline/close", {"pipeline": pid}, timeout=120)
    if status != 200:
        raise RuntimeError(f"close answered {status}: {out}")
    answered = time.monotonic() - t
    while watch.ours_alive():
        if time.monotonic() - t > 60:
            raise RuntimeError(f"still alive after 60s: {watch.ours_alive()}")
        time.sleep(0.05)
    gone = time.monotonic() - t
    return {"close_answered_s": round(answered, 3), "gone_s": round(gone, 3), "since": base_wall}


# ------------------------------------------------------------------ the arms

def arm(name, srv, watch, models, answers, runs) -> list:
    rows = []
    for i in range(runs + 1):
        base, ram = watch.used(), free_ram_mib()
        since = time.time()
        pid = open_session(srv, models)
        up = wait_up(srv, models)
        steps = load_steps(srv, since)
        peak = watch.peak_used(since)
        got = {f"{k}:{m}": finish(srv, submit(srv, k, m))["answer_s"] for k, m in answers}
        c = close(srv, pid, watch)
        back = watch.wait_used(base + 300, 30, since=c["since"])
        row = {"run": i, "thrown_away": i == 0, "up_s": round(up, 3), **steps,
               "answers_s": got, "close_answered_s": c["close_answered_s"],
               "gone_s": c["gone_s"], "memory_back_s": None if back is None else round(back, 3),
               "card_before_mib": base, "card_peak_mib": peak, "free_ram_mib": ram}
        print(f"  {name} run {i}{' (thrown away)' if i == 0 else ''}: up {row['up_s']}s "
              f"{steps['loads']}; answers {got}; gone {c['gone_s']}s; memory back "
              f"{row['memory_back_s']}s; peak {peak} MiB", flush=True)
        rows.append(row)
    return rows


def probe_all_at_once(folder: Path, watch, runs) -> list:
    """The whisper models started at once OUTSIDE the runner - each its own
    whisper-server - and stopped one by one, each by its handle."""
    rows = []
    for i in range(runs + 1):
        base, since = watch.used(), time.time()
        procs = {}
        for key, file in EARS["models"].items():
            port = R.quiet_port()
            log = open(folder / f"probe-{key}-{i}.log", "wb")
            p = subprocess.Popen([EARS["server"], "-m", file, "--host", "127.0.0.1",
                                  "--port", str(port)], stdout=log, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, cwd=str(folder),
                                 creationflags=R.NO_WINDOW)
            procs[key] = (p, port, log, time.monotonic())
        ready = {}
        deadline = time.monotonic() + 120
        while len(ready) < len(procs) and time.monotonic() < deadline:
            for key, (p, port, _, t0) in procs.items():
                if key in ready:
                    continue
                with socket.socket() as s:
                    s.settimeout(0.05)
                    if s.connect_ex(("127.0.0.1", port)) == 0:
                        ready[key] = round(time.monotonic() - t0, 3)
            time.sleep(0.05)
        all_ready = max(ready.values()) if len(ready) == len(procs) else None
        peak = watch.peak_used(since)
        stops = {}
        for key, (p, _, log, _) in procs.items():
            t = time.monotonic()
            p.kill()
            p.wait(30)
            stops[key] = round(time.monotonic() - t, 3)
            log.close()
        back = watch.wait_used(base + 300, 30, since=time.time())
        row = {"run": i, "thrown_away": i == 0, "ready_s": ready, "all_ready_s": all_ready,
               "stopped_s": stops, "memory_back_s": None if back is None else round(back, 3),
               "card_before_mib": base, "card_peak_mib": peak, "free_ram_mib": free_ram_mib()}
        print(f"  probe run {i}: ready {ready}, all {all_ready}s; stopped {stops}; peak {peak} MiB",
              flush=True)
        rows.append(row)
    return rows


def host_swap(srv, watch, pair, runs) -> list:
    rows = []
    for i in range(runs + 1):
        since = time.time()
        pid = open_session(srv, pair)
        first_up = wait_up(srv, pair)
        t = {}
        m = time.monotonic()
        for k in pair:
            status, out = srv.post(f"/api/models/{k}/unload", timeout=120)
            if status != 200:
                raise RuntimeError(f"unload {k}: {status} {out}")
        t["pair_off_s"] = round(time.monotonic() - m, 3)
        m = time.monotonic()
        srv.post("/api/models/text/load?wait_s=60", timeout=90)
        wait_up(srv, ["text"])
        t["text_up_s"] = round(time.monotonic() - m, 3)
        ans = finish(srv, submit(srv, "text", "text"))
        t["text_answer_s"] = ans["answer_s"]
        done = time.monotonic()
        m = time.monotonic()
        status, out = srv.post("/api/models/text/unload", timeout=120)
        if status != 200:
            raise RuntimeError(f"unload text: {status} {out}")
        t["text_off_s"] = round(time.monotonic() - m, 3)
        m = time.monotonic()
        for k in pair:
            srv.post(f"/api/models/{k}/load", timeout=30)
        wait_up(srv, pair)
        t["pair_up_again_s"] = round(time.monotonic() - m, 3)
        t["answer_done_to_whisper_ready_s"] = round(time.monotonic() - done, 3)
        t["pair_first_up_s"] = round(first_up, 3)
        t["loads"] = load_steps(srv, since)["loads"]
        close(srv, pid, watch)
        row = {"run": i, "thrown_away": i == 0, **t, "free_ram_mib": free_ram_mib()}
        print(f"  swap run {i}: {t}", flush=True)
        rows.append(row)
    return rows


def at_once(srv, watch, ears, runs) -> list:
    """Two answers at once (S17) against the same two one after the other: a
    text answer of ~200 tokens and a transcription, both models up."""
    rows = []
    pid = open_session(srv, ["text", ears])
    wait_up(srv, ["text", ears])
    for i in range(runs + 1):
        t = time.monotonic()
        a = finish(srv, submit(srv, "long", "text"))
        b = finish(srv, submit(srv, "ears", ears))
        serial = round(time.monotonic() - t, 3)
        t = time.monotonic()
        ja, jb = submit(srv, "long", "text"), submit(srv, "ears", ears)
        pa, pb = finish(srv, ja), finish(srv, jb)
        together = round(time.monotonic() - t, 3)
        row = {"run": i, "thrown_away": i == 0, "serial_s": serial, "at_once_s": together,
               "text_alone_s": a["answer_s"], "text_beside_s": pa["answer_s"],
               "text_tokens": [a["tokens"], pa["tokens"]],
               "ears_alone_s": b["answer_s"], "ears_beside_s": pb["answer_s"]}
        print(f"  at once run {i}: {row}", flush=True)
        rows.append(row)
    close(srv, pid, watch)
    return rows


# ------------------------------------------------------------------ the table

def med(rows, key, sub=None):
    vals = []
    for r in rows:
        if r.get("thrown_away"):
            continue
        v = r.get(key)
        if sub is not None:
            v = (v or {}).get(sub)
        if isinstance(v, (int, float)):
            vals.append(v)
    if not vals:
        return "-"
    return f"{statistics.median(vals):.2f} ({min(vals):.2f}-{max(vals):.2f})"


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    if not (R.BASE and EARS):
        print("set LLM_BOOTSTRAP_REAL_CARD and LLM_BOOTSTRAP_REAL_WHISPER (the owner's go)")
        return 2
    out = Path(args.out or R.OUT)
    out.mkdir(parents=True, exist_ok=True)
    cfg, spec, _ = R.BASE
    keys = list(EARS["models"])
    print(f"evidence: {out}", flush=True)
    warm = {"text": read_through(str(spec.gguf_path(cfg.models_dir)))}
    for k, f in EARS["models"].items():
        warm[k] = read_through(f)
    print(f"files read once: { {k: round(v, 1) for k, v in warm.items()} } s", flush=True)
    # the watch runs an nvidia-smi of its own: it is stopped in `finally`
    # whatever fails after it starts (an early failure once left it polling the
    # card five times a second after this script had gone)
    watch = R.W.CardWatch(R.OURS).start()
    co = srv = None
    result = {"files_read_s": warm}
    try:
        case = Case(out, watch)
        co = R.Coord(out, 3.0, hardware=R.self_hardware())
        settings = write_settings(out / "settings.toml")
        (out / "ws").mkdir(exist_ok=True)
        srv = R.Srv(case, settings, out / "ws", coordinator=co.url, name="m2", backend="real")
        result.update({"settings": settings.read_text(encoding="utf-8"),
                       "machine": {"free_ram_mib_at_start": free_ram_mib(),
                                   "card_at_start": watch.used()}})
        print("arm: one big (the text model)", flush=True)
        result["big"] = arm("big", srv, watch, ["text"], [("text", "text")], args.runs)
        print(f"arm: {len(keys)} small side by side ({', '.join(keys)})", flush=True)
        result["small"] = arm("small", srv, watch, keys, [("ears", k) for k in keys], args.runs)
        print(f"arm: one small, the control ({keys[0]})", flush=True)
        result["control"] = arm("control", srv, watch, [keys[0]], [("ears", keys[0])], args.runs)
        print("probe: the whisper models at once, outside the runner", flush=True)
        result["probe"] = probe_all_at_once(out, watch, args.runs)
        print(f"the host's swap ({keys[0]}, {keys[1]} <-> text)", flush=True)
        result["swap"] = host_swap(srv, watch, keys[:2], args.runs)
        print(f"answers at once: text beside {keys[0]}", flush=True)
        result["at_once"] = at_once(srv, watch, keys[0], args.runs)
    finally:
        if srv is not None:
            srv.stop()
        if co is not None:
            co.stop()
        watch.stop()
        (out / "m2.json").write_bytes(json.dumps(result, indent=1).encode("utf-8"))
    lines = ["| arm | up (s) | first answer (s) | close to gone (s) | memory back (s) | card peak (MiB) |",
             "| --- | --- | --- | --- | --- | --- |"]
    for name in ("big", "small", "control"):
        rows = result[name]
        first = {}
        for r in rows:
            if not r["thrown_away"]:
                for k, v in r["answers_s"].items():
                    first.setdefault(k, []).append(v)
        ans = "; ".join(f"{k.split(':')[1]} {statistics.median(v):.2f}" for k, v in first.items())
        lines.append(f"| {name} | {med(rows, 'up_s')} | {ans} | {med(rows, 'gone_s')} | "
                     f"{med(rows, 'memory_back_s')} | {med(rows, 'card_peak_mib')} |")
    p = result["probe"]
    lines += ["", f"probe, all at once outside the runner: all ready {med(p, 'all_ready_s')} s; "
                  f"memory back {med(p, 'memory_back_s')} s"]
    s = result["swap"]
    lines += ["", "| swap step | median (range), s |", "| --- | --- |"]
    for k in ("pair_first_up_s", "pair_off_s", "text_up_s", "text_answer_s", "text_off_s",
              "pair_up_again_s", "answer_done_to_whisper_ready_s"):
        lines.append(f"| {k} | {med(s, k)} |")
    a = result["at_once"]
    lines += ["", "| two answers | median (range), s |", "| --- | --- |"]
    for k in ("serial_s", "at_once_s", "text_alone_s", "text_beside_s", "ears_alone_s",
              "ears_beside_s"):
        lines.append(f"| {k} | {med(a, k)} |")
    table = "\n".join(lines)
    (out / "m2-table.md").write_bytes((table + "\n").encode("utf-8"))
    print("\n" + table, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
