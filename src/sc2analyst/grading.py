"""W10 · Grading and comparing: the owner's grades decide which prompt, model and route is kept.

  claims(records)      every kept claim of every landed run, each with a stable hash
  blind(claims, seed)  the same claims shuffled by a seed, the model and the version HIDDEN - the
                       owner grades what was said, not who said it (O-5)
  grade(...)           one grade appended to grades.jsonl - written ONLY by the owner's own click in
                       the workbench, never by a script: ✓ right · ✗ wrong · ~ right but useless ·
                       ★ insight (O-4)
  compare(records, grades)  per task × version × model: runs, the automatic pass rate (claims kept
                       of claims made), graded n, precision ((✓ + ★) / graded), insight rate, seconds
                       and tokens (medians)
"""
from __future__ import annotations

import collections
import hashlib
import json
import random
import time
from pathlib import Path

from . import library as L

GRADES = L.DATA_HOME / "grades" / "grades.jsonl"
SCALE = {"right": "✓", "wrong": "✗", "useless": "~", "insight": "★"}


def claim_hash(cid: str, text: str) -> str:
    return hashlib.sha256((cid + "\n" + text).encode("utf-8")).hexdigest()[:12]


def load_records(folders) -> list:
    out = []
    for d in folders:
        for f in sorted(Path(d).glob("*.json")):          # task runs (c-*) and the final passes (final-*)
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except ValueError:
                continue
    return out


def claims(records: list) -> list:
    out = []
    for r in records:
        facts = {f["id"]: f for f in r.get("facts", [])}
        for c in (r.get("verdicts") or {}).get("kept", []):
            out.append({"hash": claim_hash(r["correlation_id"], c["text"]), "cid": r["correlation_id"],
                        "task": r["task"], "version": r["version"], "model": r.get("model"),
                        "text": c["text"], "cites": [facts[x] for x in c.get("cites", []) if x in facts],
                        "game_key": r.get("game_key") or (r.get("target") or {}).get("label", "").split(" ")[0]})
    return out


def load_grades(path: Path = GRADES) -> dict:
    """claim hash -> its latest grade."""
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            g = json.loads(line)
            out[g["hash"]] = g
    return out


def blind(all_claims: list, graded: dict, seed: int, limit: int = 30) -> list:
    todo = [c for c in all_claims if c["hash"] not in graded]
    random.Random(seed).shuffle(todo)
    return [{k: v for k, v in c.items() if k not in ("model", "version")} for c in todo[:limit]]


def grade(h: str, cid: str, value: str, note: str = "", path: Path = GRADES, meta: dict | None = None) -> dict:
    if value not in SCALE:
        raise KeyError("no grade %s" % value)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = dict(meta or {}, hash=h, cid=cid, grade=value, note=note[:500], at=time.time())
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def compare(records: list, graded: dict) -> list:
    rows = collections.defaultdict(lambda: {"runs": 0, "made": 0, "kept": 0, "graded": 0, "good": 0,
                                            "insight": 0, "secs": [], "tokens": []})
    for r in records:
        k = (r["task"], r["version"], r.get("model") or "?")
        row = rows[k]
        row["runs"] += 1
        v = r.get("verdicts") or {}
        row["kept"] += len(v.get("kept", []))
        row["made"] += len(v.get("kept", [])) + len(v.get("dropped", []))
        if r.get("landed") is not None and r.get("submitted") is not None:
            row["secs"].append(r["landed"] - r["submitted"])
        if r.get("usage"):
            row["tokens"].append(r["usage"].get("completion_tokens") or 0)
        for c in v.get("kept", []):
            g = graded.get(claim_hash(r["correlation_id"], c["text"]))
            if g:
                row["graded"] += 1
                row["good"] += g["grade"] in ("right", "insight")
                row["insight"] += g["grade"] == "insight"
    out = []
    for (task, ver, model), row in sorted(rows.items()):
        pct = lambda a, b: None if not b else round(100 * a / b, 1)  # noqa: E731
        out.append({"task": task, "version": ver, "model": model, "runs": row["runs"],
                    "auto_pass": pct(row["kept"], row["made"]), "graded": row["graded"],
                    "precision": pct(row["good"], row["graded"]), "insight": pct(row["insight"], row["graded"]),
                    "seconds_p50": L.quantiles(row["secs"], (0.5,))[0] if row["secs"] else None,
                    "tokens_p50": L.quantiles(row["tokens"], (0.5,))[0] if row["tokens"] else None})
    return out
