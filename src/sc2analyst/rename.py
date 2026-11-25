"""Rename names in text, deterministically: a table of (find, write) and the old player labels.

The table is `config/rename.local.json` - gitignored, so the names never enter a tracked file:

    [["myfriend", "Ally_2"], ["myhandle", "Player_1"]]

  * finds are applied longest first, as substrings, case-insensitive; a name glued to other text or
    standing after an escape (`\\u2014Name`) is still found;
  * the case is copied from the match: all lower -> the write lowered, ALL UPPER -> uppered, any other
    -> the write as given (`Myhandle` -> `Player_1`, `myhandle` -> `player_1`, `MYHANDLE` -> `PLAYER_1`).

The old labels: until 2026-11-25 `pick.side_names` wrote `Ally 1`, `Ally 2`, `Enemy 1`... (each side
from 1). Since then it writes the demo replays' names, `Ally_2`, `Ally_3`, `Enemy_1`... (the owner is
1 of his team). `Ally N` becomes `Ally_{N+1}`, `Enemy N` becomes `Enemy_N`: whole labels, case-
sensitive, never after a letter unless that letter ends an escape (`\\nAlly 1`).

Everything runs in ONE pass, so nothing written is renamed again. A `data:...;base64,...` run (an
embedded minimap) is copied untouched: renaming inside it would break the picture.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

DATA_URI = re.compile(r"data:[\w/+.-]+;base64,[A-Za-z0-9+/=]+")
LABEL = r"(?:(?<![A-Za-z])|(?<=\\[ntr]))(?P<kind>Ally|Enemy) (?P<n>\d+)(?!\d)"


def load_table(path: str | Path) -> list[tuple[str, str]]:
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    table = [(str(f), str(w)) for f, w in rows]
    if any(not f for f, _ in table):
        raise ValueError("an empty find in the rename table")
    return table


class Rules:
    """A compiled table (and the old-label rule); counts what it replaced."""

    def __init__(self, table: list[tuple[str, str]], relabel: bool = True):
        self.write = {f.lower(): w for f, w in table}
        finds = sorted(self.write, key=lambda f: (-len(f), f))
        parts = []
        if finds:
            parts.append("(?P<name>(?i:%s))" % "|".join(re.escape(f) for f in finds))
        if relabel:
            parts.append(LABEL)
        self.rx = re.compile("|".join(parts)) if parts else None
        self.counts: dict[str, int] = {}

    def _one(self, m: re.Match) -> str:
        got = m.groupdict().get("name")
        if got:
            write = self.write[got.lower()]
            out = write.lower() if got.islower() else write.upper() if got.isupper() else write
            key = got.lower()
        else:
            n = int(m.group("n"))
            out = "Ally_%d" % (n + 1) if m.group("kind") == "Ally" else "Enemy_%d" % n
            key = "%s N" % m.group("kind")
        self.counts[key] = self.counts.get(key, 0) + 1
        return out

    def text(self, s: str) -> str:
        """Rename outside base64 data URIs."""
        if self.rx is None or not s:
            return s
        out, at = [], 0
        for m in DATA_URI.finditer(s):
            out.append(self.rx.sub(self._one, s[at:m.start()]))
            out.append(m.group(0))
            at = m.end()
        out.append(self.rx.sub(self._one, s[at:]))
        return "".join(out)

    def json(self, obj):
        """Every string of a decoded JSON value, keys included."""
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, list):
            return [self.json(x) for x in obj]
        if isinstance(obj, dict):
            return {self.text(k): self.json(v) for k, v in obj.items()}
        return obj


def apply(text: str, table: list[tuple[str, str]], relabel: bool = True) -> str:
    return Rules(table, relabel).text(text)
