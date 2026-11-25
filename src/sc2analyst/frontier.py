"""W11 · The frontier comparison: the same task on ChatGPT Pro, by the owner's own paste (O-5).

  packet(task, target)  one text to paste: the same system prompt, facts and question the local model
                        gets, the JSON shape in words, and "answer only with this JSON". Kept as a
                        packet record (<data home>/frontier/packets/<id>.json) with the
                        facts and the truth its checks need - nothing is sent anywhere by the tool
  parse(reply)          the JSON out of a pasted reply: a ```json fence, or the first balanced {...}
  import_reply(...)     the reply checked with the SAME checks and recorded like a local run, its
                        model "chatgpt-pro (pasted <date>)", so Grade (blind) and Compare see both

No API, no keys: the owner pastes the packet into ChatGPT Pro himself and pastes the reply back.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date
from pathlib import Path

from . import library as L, runs as RN, tasks as TK

FRONTIER = L.DATA_HOME / "frontier"
PACKETS = FRONTIER / "packets"


def _schema_words(schema: dict) -> str:
    return json.dumps(schema, indent=1)


def packet(task: TK.Task, target: dict, label: str = "", packets: Path = PACKETS) -> dict:
    facts, truth = task.facts(target)
    pid = RN.mint()
    text = ("%s\n\n%s\n\nANSWER ONLY WITH ONE JSON OBJECT OF THIS SHAPE (no prose before or after it):\n%s\n"
            % (task.system, task.prompt(facts), _schema_words(task.schema)))
    rec = {"packet_id": pid, "task": task.id, "version": task.version, "label": label,
           "target": {k: v for k, v in target.items() if k != "game"}, "game_key": target.get("key"),
           "facts": facts.rows, "truth": truth, "text": text, "made": time.time()}
    packets.mkdir(parents=True, exist_ok=True)
    (packets / ("%s.json" % pid)).write_text(json.dumps(rec, indent=1), encoding="utf-8")
    return rec


FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def parse(reply: str):
    """The JSON object in a pasted reply, or None."""
    if not reply:
        return None
    m = FENCE.search(reply)
    candidates = [m.group(1)] if m else []
    start = reply.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(reply)):
            ch = reply[i]
            if in_str:
                esc = (ch == "\\" and not esc)
                if ch == '"' and not esc:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(reply[start:i + 1])
                    break
        start = reply.find("{", start + 1) if not candidates else -1
    for c in candidates:
        try:
            v = json.loads(c)
            if isinstance(v, dict):
                return v
        except ValueError:
            continue
    return None


def import_reply(packet_id: str, reply: str, packets: Path = PACKETS, records: Path = RN.RECORDS) -> dict:
    p = json.loads((packets / ("%s.json" % packet_id)).read_text(encoding="utf-8"))
    rec = {"correlation_id": RN.mint(), "packet_id": packet_id, "task": p["task"], "version": p["version"],
           "model": "chatgpt-pro (pasted %s)" % date.today().isoformat(), "target": p["target"],
           "game_key": p.get("game_key"), "facts": p["facts"], "truth": p["truth"],
           "request": {"pasted_packet": packet_id}, "submitted": p["made"], "landed": time.time(),
           "status": "done", "text": reply, "answer": parse(reply), "usage": None}
    rec["verdicts"] = RN.recheck(rec)
    RN.write(rec, records)
    return rec
