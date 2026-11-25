"""S3 · Build order, idle production, the larva bank, supply blocks (F4, DL-2, B1).

Build order: every structure STARTED (its construction began), every unit COMPLETED (born, or out
of its egg or cocoon), every morph and upgrade COMPLETED - with the second on the in-game clock and
the supply at the latest stats sample before it. The replay has no start event for a trained unit;
its start is `completion - build time`, and the build time is measured (below), not recalled.

Idle production (DL-2, the owner's Q6): a production structure is idle when it trains nothing.
Each unit names the structure that made it (the tracker's creator tag, kept by the adapter); a
unit occupies its producer for its build time before it is born. Idle = the producer's life
(construction done -> death or game end) not covered by any child's occupation. No threshold (the
ruling): the table reports seconds, the owner decides what is a mistake.
  - A reactor's second slot is not measured: a producer with one child in training is busy.
  - A town hall morphing (orbital, planetary) or a structure lifted off reads as idle.
  - Chrono boost shortens a build time; the measured time is the unboosted one, so a chrono'd
    unit's occupation is slightly overstated (idle understated). Chrono casts are counted apart.

Build times are MEASURED from the library: for each child type, the most common gap between two
consecutive births of that type from one producer (the producer trained back to back). The table
and how many gaps support each value: `measured_build_times`.

The larva bank (Zerg, his first prompt: *"3 larvas+ and not being used at all"*): larva alive per
hatchery over time (a larva is the hatchery's by its creator tag; it ends when used or killed);
the seconds a hatchery holds three or more.

Supply blocks: stats samples (every 160 game loops, 7.14 s) where supply used >= supply made and
supply made < 200. Resolution is the sample interval: a block is reported as the samples it spans.
"""
from __future__ import annotations

import collections
import itertools

STATS_INTERVAL_S = 160 / 22.4
# a warp-in names no producer (measured 2026-10-26: every warp-in is an init with no creator tag),
# so a warp gate's idle time is NOT COMPUTABLE from this data; `warp_ins` counts them instead
NOT_ATTRIBUTABLE = {"WarpGate"}
MODE_SUFFIXES = ("Sieged", "Burrowed", "Flying", "Lowered", "Uprooted", "Phasing", "SiegeMode",
                 "AG", "AP", "Assault", "Fighter")
COCOON_WORDS = ("Cocoon", "Egg")


def _is_mode(t: str) -> bool:
    return t.endswith(MODE_SUFFIXES)


def supply_at(game: dict, pid: int, t: float):
    best = None
    for s in game["stats"]:
        if s["pid"] == pid and s["t"] <= t:
            best = s
    return None if best is None else [int(best["food_used"]), int(best["food_made"])]


def build_order(game: dict, pid: int) -> list[dict]:
    out = []
    for u in game["units"]:
        if u["owner"] != pid or "marker" in u["kinds"] or "transient" in u["kinds"]:
            continue
        types = u["types"]
        first = types[0][1]
        if u["how"] == "init":
            # an init is a construction, or a unit warping in: the kinds say which
            out.append({"t": u["born"], "what": "structure" if "structure" in u["kinds"] else "unit",
                        "name": first, "event": "started"})
        elif u["born"] > 0 and "transient" not in _kinds_of_first(u) and \
                not first.endswith(COCOON_WORDS) and first != "Larva":
            out.append({"t": u["born"], "what": "unit", "name": first, "event": "completed"})
        # out of an egg or cocoon: the unit completes when its type leaves the cocoon
        prev = first
        for t, name in types[1:]:
            if prev in ("Egg", "Larva") or prev.endswith(COCOON_WORDS):
                if not name.endswith(COCOON_WORDS) and name not in ("Egg", "Larva"):
                    out.append({"t": t, "what": "unit" if prev == "Egg" else "morph",
                                "name": name, "event": "completed"})
            elif "structure" in u["kinds"] and not _is_mode(name) and not _is_mode(prev) and \
                    name not in ("WarpGate",) and prev not in ("WarpGate",):
                out.append({"t": t, "what": "morph", "name": name, "event": "completed"})
            elif name == "Archon":
                out.append({"t": t, "what": "morph", "name": name, "event": "completed"})
            prev = name
    for t, p, name in game["upgrades"]:
        if p == pid:
            out.append({"t": t, "what": "upgrade", "name": name, "event": "completed"})
    out.sort(key=lambda e: (e["t"], e["name"]))
    stats = sorted((s for s in game["stats"] if s["pid"] == pid), key=lambda s: s["t"])
    i = -1
    for e in out:
        while i + 1 < len(stats) and stats[i + 1]["t"] <= e["t"]:
            i += 1
        e["supply"] = None if i < 0 else "%d/%d" % (stats[i]["food_used"], stats[i]["food_made"])
    return out


def _kinds_of_first(u: dict) -> list:
    # kinds are of the unit's last type; a larva that became a drone is not transient at its birth
    # for the purpose of "what completed" - the cocoon branch handles it
    return u["kinds"] if len(u["types"]) == 1 else []


# --- build times, measured ----------------------------------------------------------------------

def birth_gaps(game: dict):
    """(child type, gap) for consecutive same-type births from one producer."""
    by_parent: dict[int, list] = collections.defaultdict(list)
    for u in game["units"]:
        if u["ctag"] and u["how"] == "born" and "transient" not in u["kinds"]:
            by_parent[u["ctag"]].append((u["born"], u["types"][0][1]))
    for kids in by_parent.values():
        kids.sort()
        for (t0, a), (t1, b) in zip(kids, kids[1:]):
            if a == b:
                yield a, round(t1 - t0, 1)


def egg_times(game: dict):
    """(child type, seconds) from a larva's egg (its first change of type) to its child's birth."""
    larva = {u["tag"]: u for u in game["units"]
             if u["types"][0][1] == "Larva" and len(u["types"]) > 1}
    for u in game["units"]:
        lv = larva.get(u["ctag"])
        if lv is not None:
            yield u["types"][0][1], round(u["born"] - lv["types"][1][0], 1)


def measured_build_times(games) -> dict:
    gaps = collections.defaultdict(collections.Counter)
    for g in games:
        for typ, gap in itertools.chain(birth_gaps(g), egg_times(g)):
            if 3.0 <= gap <= 120.0:
                gaps[typ][round(gap)] += 1
    out = {}
    for typ, c in gaps.items():
        val, n = c.most_common(1)[0]
        out[typ] = {"seconds": val, "support": n, "gaps": sum(c.values())}
    return dict(sorted(out.items()))


# --- idle production ----------------------------------------------------------------------------

def _union(intervals):
    merged = []
    for a, b in sorted(intervals):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def idle_production(game: dict, pid: int, build_times: dict) -> list[dict]:
    units = game["units"]
    kids = collections.defaultdict(list)
    for u in units:
        if u["ctag"] and u["owner"] == pid:
            kids[u["ctag"]].append(u)
    out = []
    for p in units:
        if p["owner"] != pid or "producer" not in p["kinds"] or p["done"] is None:
            continue
        if p["types"][-1][1] in NOT_ATTRIBUTABLE:
            continue
        life0 = p["done"]
        life1 = p["died"] if p["died"] is not None else game["end"]
        if life1 <= life0:
            continue
        busy = []
        unknown = 0
        for k in kids.get(p["tag"], []):
            bt = build_times.get(k["types"][0][1], {}).get("seconds")
            if bt is None:
                unknown += 1
                continue
            busy.append([max(life0, k["born"] - bt), min(life1, k["born"])])
        cover = _union([b for b in busy if b[1] > b[0]])
        busy_s = sum(b - a for a, b in cover)
        gaps, cur = [], life0
        for a, b in cover:
            if a > cur:
                gaps.append((cur, a))
            cur = max(cur, b)
        if life1 > cur:
            gaps.append((cur, life1))
        longest = max(gaps, key=lambda g: g[1] - g[0]) if gaps else None
        out.append({"tag": p["tag"], "type": p["types"][-1][1],
                    "class": "townhall" if "townhall" in p["kinds"] else "production",
                    "from": life0, "to": round(life1, 2), "children": len(kids.get(p["tag"], [])),
                    "children_without_build_time": unknown,
                    "idle_s": round((life1 - life0) - busy_s, 1),
                    "idle_share": round(((life1 - life0) - busy_s) / (life1 - life0), 3),
                    "longest_idle": None if longest is None else
                    [round(longest[0], 1), round(longest[1], 1)]})
    return out


# --- the larva bank -----------------------------------------------------------------------------

def larva_bank(game: dict, pid: int, at_least: int = 3) -> list[dict]:
    hatch = {u["tag"]: u for u in game["units"] if u["owner"] == pid and "larva_producer" in u["kinds"]}
    events = collections.defaultdict(list)
    for u in game["units"]:
        if u["owner"] == pid and u["types"][0][1] == "Larva" and u["ctag"] in hatch:
            events[u["ctag"]].append((u["born"], +1))
            # used = its first change of type (Larva -> Egg, measured 2026-10-26); the death comes
            # later, when the egg hatches, and must not count as a larva waiting
            end = u["types"][1][0] if len(u["types"]) > 1 else u["died"]
            events[u["ctag"]].append((end if end is not None else game["end"], -1))
    out = []
    for tag, ev in events.items():
        ev.sort()
        n, last, banked = 0, None, 0.0
        peak = 0
        for t, d in ev:
            if last is not None and n >= at_least:
                banked += t - last
            n += d
            peak = max(peak, n)
            last = t
        h = hatch[tag]
        life = (h["died"] if h["died"] is not None else game["end"]) - (h["done"] or h["born"])
        out.append({"tag": tag, "type": h["types"][-1][1], "larva": len(ev) // 2,
                    "banked_s": round(banked, 1), "life_s": round(life, 1),
                    "banked_share": round(banked / life, 3) if life > 0 else None, "peak": peak})
    return out


# --- supply blocks ------------------------------------------------------------------------------

def supply_blocks(game: dict, pid: int) -> list[dict]:
    stats = sorted((s for s in game["stats"] if s["pid"] == pid), key=lambda s: s["t"])
    blocks, cur = [], None
    for s in stats:
        blocked = s["food_made"] < 200 and s["food_used"] >= s["food_made"] and s["food_made"] > 0
        if blocked:
            if cur is None:
                cur = {"from": s["t"], "to": s["t"], "samples": 1, "supply": int(s["food_made"])}
            else:
                cur["to"] = s["t"]
                cur["samples"] += 1
        elif cur is not None:
            blocks.append(cur)
            cur = None
    if cur is not None:
        blocks.append(cur)
    for b in blocks:
        b["at_least_s"] = round((b["samples"] - 1) * STATS_INTERVAL_S, 1)
        b["at_most_s"] = round((b["samples"] + 1) * STATS_INTERVAL_S, 1)
    return blocks


def warp_ins(game: dict, pid: int) -> dict:
    gates = [u for u in game["units"] if u["owner"] == pid and u["types"][-1][1] == "WarpGate"]
    gate_s = sum(((u["died"] if u["died"] is not None else game["end"]) -
                  next((t for t, n in u["types"] if n == "WarpGate"), game["end"])) for u in gates)
    n = sum(1 for u in game["units"] if u["owner"] == pid and u["how"] == "init"
            and "army" in u["kinds"] and u["ctag"] is None and u["types"][0][1] != "Archon")
    return {"warp_gates": len(gates), "warp_gate_seconds": round(gate_s, 1), "warp_ins": n}


def chrono_casts(game: dict, pid: int) -> int:
    return sum(1 for c in game["commands"] if c[1] == pid and c[2] and "ChronoBoost" in c[2])
