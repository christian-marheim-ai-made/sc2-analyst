"""W8 · A task run: built here, queued on llm-bootstrap, answered by the local model, checked here.

  submit(client, task, target)   facts built and rendered (DL-31), a correlation id minted with the
                                 bootstrap's own `ids.mint_correlation`, the job queued with `name`,
                                 `scope` (the game or study), and `check: true` in debug - then it
                                 waits HELD in the bootstrap's Check until a person sends it
  land(client, run)              wait for the answer (or its being held), take it, parse the JSON,
                                 run the task's check, write the record and its index line
  recheck(record)                the checks again over a stored answer - offline, any time (DL-30)

The record: <data home>/llm-workspace/sc2/<correlation id>.json - task, version, target,
facts with their sources, the request, the answer, every claim's verdict - beside the bootstrap's own
request record (`requests/<correlation id>/`) in the same workspace. `index.jsonl` lists them.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from . import library as L, llm, tasks as TK

WORKSPACE = L.DATA_HOME / "llm-workspace"
RECORDS = WORKSPACE / "sc2"
CONTRACT = llm.REPO / "vendor/llm-bootstrap/llm-bootstrap-contract"


def mint() -> str:
    if str(CONTRACT) not in sys.path:
        sys.path.insert(0, str(CONTRACT))
    import ids
    return ids.mint_correlation()


def _target_view(target: dict) -> dict:
    """What the record keeps of a target: never the whole game."""
    return {k: v for k, v in target.items() if k not in ("game",)}


def submit(client, task: TK.Task, target: dict, model: str = "thinker", debug: bool = False,
           scope: str | None = None, batch_at: float | None = None) -> dict:
    facts, truth = task.facts(target)
    cid = mint()
    gen = {"model": model, "max_tokens": task.gen["max_tokens"], "json_schema": task.schema}
    if task.gen.get("reasoning_budget") is not None:
        gen["reasoning_budget_tokens"] = task.gen["reasoning_budget"]
    request = {"messages": [{"role": "system", "content": task.system},
                            {"role": "user", "content": task.prompt(facts)}]}
    name = "%s · %s" % (task.id, target.get("label") or scope or "")
    extra = {"name": name[:120], "correlation_id": cid}
    if scope:
        extra["scope"] = scope
    if debug:
        extra["check"] = True
    if batch_at is not None:
        job = client.plan(task.id, request, gen, not_before=batch_at, if_no_room="wait", card_patience_s=7200, **extra)
    else:
        job = client.submit(task.id, request, gen, **extra)
    return {"job": job, "correlation_id": cid, "task": task.id, "version": task.version, "model": model,
            "target": _target_view(target), "facts": facts.rows, "truth": truth, "request": request,
            "submitted": time.time(), "debug": debug}


def held(client, run: dict) -> dict:
    return client.call("server.jobs.held", params={"id": run["job"]})[1]


def send(client, run: dict, request: dict | None = None) -> dict:
    return client.call("server.jobs.send", params={"id": run["job"]}, body={"request": request} if request else {})[1]


def land(client, run: dict, within_s: float = 1800.0, records: Path = RECORDS) -> dict:
    """-> the record (status held / done / failed); written when the job has ended."""
    res = client.wait(run["job"], within_s=within_s)
    job = res.get("job", {})
    status = job.get("status")
    if status == "held":
        return dict(run, status="held")
    cand = (res.get("candidates") or [{}])[0]
    text = cand.get("text")
    try:
        answer = json.loads(text) if text else None
    except ValueError:
        answer = None
    rec = dict(run, status=status, error=job.get("error"), reason=job.get("reason"), text=text, answer=answer,
               usage=cand.get("usage"), timings=cand.get("timings"), landed=time.time())
    rec["verdicts"] = recheck(rec)
    try:
        client.take(run["job"])
    except Exception:
        pass
    write(rec, records)
    return rec


def recheck(rec: dict) -> dict:
    task = TK.TASKS[rec["task"]]
    facts = {r["id"]: r for r in rec["facts"]}
    v = task.check(rec.get("answer"), facts, rec.get("truth") or {})
    v["checked_with"] = task.version
    return v


def write(rec: dict, records: Path = RECORDS) -> Path:
    records.mkdir(parents=True, exist_ok=True)
    p = records / ("%s.json" % rec["correlation_id"])
    p.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    line = {k: rec.get(k) for k in ("correlation_id", "task", "version", "model", "status", "submitted", "landed")}
    line["target"] = rec.get("target")
    line["kept"] = len(rec["verdicts"]["kept"])
    line["dropped"] = len(rec["verdicts"]["dropped"])
    with open(records / "index.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
    return p
