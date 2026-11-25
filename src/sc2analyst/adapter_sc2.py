"""Stage 1, the GameAdapter for StarCraft II: one anonymised replay -> one game record.

The record is plain JSON and the only thing the stages above read. Everything SC2-specific lives
here (R-C): which types are workers, town halls, producers, static defence; the clock; the raw
tracker fields sc2reader drops. Above this module a unit has a `type` (a label, shown to a person,
never branched on) and `kinds` (the shared concepts the repositories branch on).

The clock: every time in the record is the in-game clock in seconds, game loops / 22.4 (Faster).
sc2reader's own `event.second` is loops / 16 - normal speed - and is 1.4x the clock the player
sees (audit row R1); it is never used here.

The producer: tracker UnitBornEvent carries the producing unit's tag and ability (raw struct
fields 7, 8, 9: creator tag index, recycle, ability name). sc2reader 1.9 decodes and drops them;
`_keep_raw_born` keeps them. Verified on build 95299 (DL-2's RECALL, measured 2026-10-26).

The stats sample (v2, DL-23): every PlayerStatsEvent keeps the v1 names and `v`, all 39 numbers in
sc2reader's order (`STATS_FIELDS`): the game's own army / economy / technology values - used now,
in progress, lost, killed - that v1 summed or dropped (W0 premise I).
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

ADAPTER_VERSION = "sc2-game-v2"
LOOPS_PER_SECOND = 22.4

# the stats sample's 39 numbers, in sc2reader's index order (its PlayerStatsEvent, stats[0..38])
STATS_FIELDS = (
    "minerals_current", "vespene_current", "minerals_collection_rate", "vespene_collection_rate",
    "workers_active_count",
    "minerals_used_in_progress_army", "minerals_used_in_progress_economy",
    "minerals_used_in_progress_technology",
    "vespene_used_in_progress_army", "vespene_used_in_progress_economy",
    "vespene_used_in_progress_technology",
    "minerals_used_current_army", "minerals_used_current_economy", "minerals_used_current_technology",
    "vespene_used_current_army", "vespene_used_current_economy", "vespene_used_current_technology",
    "minerals_lost_army", "minerals_lost_economy", "minerals_lost_technology",
    "vespene_lost_army", "vespene_lost_economy", "vespene_lost_technology",
    "minerals_killed_army", "minerals_killed_economy", "minerals_killed_technology",
    "vespene_killed_army", "vespene_killed_economy", "vespene_killed_technology",
    "food_used", "food_made", "minerals_used_active_forces", "vespene_used_active_forces",
    "ff_minerals_lost_army", "ff_minerals_lost_economy", "ff_minerals_lost_technology",
    "ff_vespene_lost_army", "ff_vespene_lost_economy", "ff_vespene_lost_technology",
)
STAT = {name: i for i, name in enumerate(STATS_FIELDS)}

WORKERS = {"SCV", "Probe", "Drone"}
TOWNHALLS = {"CommandCenter", "CommandCenterFlying", "OrbitalCommand", "OrbitalCommandFlying",
             "PlanetaryFortress", "Nexus", "Hatchery", "Lair", "Hive"}
PRODUCERS = {"CommandCenter", "OrbitalCommand", "PlanetaryFortress", "Nexus",
             "Barracks", "Factory", "Starport", "Gateway", "WarpGate",
             "RoboticsFacility", "Stargate"}
LARVA_PRODUCERS = {"Hatchery", "Lair", "Hive"}
STATIC_DEFENSE = {"Bunker", "MissileTurret", "PlanetaryFortress", "PhotonCannon",
                  "ShieldBattery", "SpineCrawler", "SporeCrawler", "SpineCrawlerUprooted",
                  "SporeCrawlerUprooted"}
SUPPLY = {"SupplyDepot", "SupplyDepotLowered", "Pylon", "Overlord", "OverlordTransport"}
# made by an ability, gone by a timer, or a stage of something else: never army, never a loss
# a player chose to make
TRANSIENT = {"Larva", "Egg", "BanelingCocoon", "RavagerCocoon", "LurkerMPEgg", "BroodLordCocoon",
             "OverlordCocoon", "TransportOverlordCocoon", "Broodling", "BroodlingEscort",
             "LocustMP", "LocustMPFlying", "ForceField", "AutoTurret", "MULE", "Changeling",
             "ChangelingMarine", "ChangelingMarineShield", "ChangelingZealot",
             "ChangelingZergling", "ChangelingZerglingWings", "InfestedTerransEgg",
             "KD8Charge", "ParasiticBombDummy", "DisruptorPhased", "Interceptor",
             "AdeptPhaseShift", "CreepTumor", "CreepTumorQueen", "CreepTumorBurrowed",
             "InvisibleTargetDummy", "PointDefenseDrone"}
# owned by a player, never a unit: the minimap's beacons, the spray's invisible dummy
MARKER_PREFIXES = ("Beacon", "InvisibleTargetDummy")
COSMETIC_UPGRADE_PREFIXES = ("Spray", "RewardDance", "GameHeartActive")


def kinds_of(type_name: str, is_building: bool, is_worker: bool, owner: int) -> list[str]:
    k = []
    if owner == 0:
        k.append("neutral")
        if "Mineral" in type_name or "Geyser" in type_name or "Vespene" in type_name:
            k.append("resource")
        return k
    if type_name.startswith(MARKER_PREFIXES):
        return ["marker"]
    if type_name in TRANSIENT:
        k.append("transient")
    if is_worker or type_name in WORKERS:
        k.append("worker")
    if is_building:
        k.append("structure")
    if type_name in TOWNHALLS:
        k.append("townhall")
    if type_name in PRODUCERS:
        k.append("producer")
    if type_name in LARVA_PRODUCERS:
        k.append("larva_producer")
    if type_name in STATIC_DEFENSE:
        k.append("static_defense")
    if type_name in SUPPLY:
        k.append("supply")
    if not {"transient", "worker", "structure", "supply"} & set(k):
        k.append("army")
    return k


def _keep_raw_born():
    from sc2reader.events import tracker
    if getattr(tracker.UnitBornEvent, "_keeps_raw", False):
        return
    orig = tracker.UnitBornEvent.__init__

    def init(self, frames, data, build):
        orig(self, frames, data, build)
        self.creator_tag = None
        self.creator_ability = None
        if isinstance(data, dict) and data.get(7) is not None and data.get(8) is not None:
            self.creator_tag = (data[7] << 18) + data[8]
            ab = data.get(9)
            self.creator_ability = ab.decode("utf8", "replace") if isinstance(ab, bytes) else ab

    tracker.UnitBornEvent.__init__ = init
    tracker.UnitBornEvent._keeps_raw = True


def _t(frame: int) -> float:
    return round(frame / LOOPS_PER_SECOND, 2)


def _value(unit) -> tuple[int, int, float, bool, bool]:
    try:
        return (int(unit.minerals or 0), int(unit.vespene or 0), float(unit.supply or 0),
                bool(unit.is_building), bool(unit.is_worker))
    except Exception:  # an unknown type to this sc2reader build: no value, said by zeros
        return 0, 0, 0.0, False, False


def stats_row(e, t: float) -> dict:
    """One stats sample: the v1 names (kept unchanged) and `v`, every number in STATS_FIELDS order."""
    return {"t": t, "pid": e.pid,
            "food_used": e.food_used, "food_made": e.food_made,
            "m_rate": e.minerals_collection_rate,
            "g_rate": e.vespene_collection_rate,
            "workers": e.workers_active_count,
            "m_bank": e.minerals_current, "g_bank": e.vespene_current,
            "army_value": e.minerals_used_current_army + e.vespene_used_current_army,
            "lost": e.resources_lost, "killed": e.resources_killed,
            "v": [getattr(e, name) for name in STATS_FIELDS]}


def load_game(path: str | Path, owner_handle: str = "Player_1") -> dict:
    """The game record of one replay. `owner_handle` marks the owner's player (never anonymised)."""
    import sc2reader
    _keep_raw_born()
    r = sc2reader.load_replay(str(path), load_level=4)

    players = []
    for p in r.players:
        players.append({"pid": p.pid, "team": p.team_id, "race": p.play_race,
                        "result": p.result, "name": p.name,
                        "is_owner": p.name == owner_handle})
    pid_team = {p["pid"]: p["team"] for p in players}

    units: dict[int, dict] = {}

    def new_unit(e, how):
        u = e.unit
        owner = e.control_pid or 0
        m, g, sup, bld, wrk = _value(u)
        name = getattr(e, "unit_type_name", None) or (u.name if u else "?")
        rec = {"tag": e.unit_id, "type": name, "owner": owner, "born": _t(e.frame),
               "x": e.x, "y": e.y, "how": how, "done": None, "died": None,
               "dx": None, "dy": None, "kpid": None, "ktag": None, "ktype": None,
               "ctag": getattr(e, "creator_tag", None), "cab": getattr(e, "creator_ability", None),
               "m": m, "g": g, "sup": sup, "kinds": kinds_of(name, bld, wrk, owner),
               "types": [[_t(e.frame), name]]}
        if how == "born":
            rec["done"] = rec["born"]
        units[e.unit_id] = rec

    type_changes, owner_changes, positions, stats = [], [], [], []
    upgrades, camera, commands = [], [], []
    for e in r.events:
        n = type(e).__name__
        if n == "UnitBornEvent":
            new_unit(e, "born")
        elif n == "UnitInitEvent":
            new_unit(e, "init")
        elif n == "UnitDoneEvent":
            u = units.get(e.unit_id)
            if u is not None:
                u["done"] = _t(e.frame)
        elif n == "UnitDiedEvent":
            u = units.get(e.unit_id)
            if u is None:
                continue
            u["died"] = _t(e.frame)
            u["dx"], u["dy"] = e.x, e.y
            u["kpid"] = e.killer_pid
            u["ktag"] = e.killing_unit_id
            if e.killing_unit_id in units:
                u["ktype"] = units[e.killing_unit_id]["types"][-1][1]
            elif e.killing_unit is not None:
                u["ktype"] = e.killing_unit.name
        elif n == "UnitTypeChangeEvent":
            u = units.get(e.unit_id)
            if u is not None:
                u["types"].append([_t(e.frame), e.unit_type_name])
                m, g, sup, bld, wrk = _value(e.unit)
                if m or g:
                    u["m"], u["g"] = m, g
                u["sup"] = sup or u["sup"]
                u["kinds"] = kinds_of(e.unit_type_name, bld, wrk, u["owner"])
            type_changes.append([_t(e.frame), e.unit_id, e.unit_type_name])
        elif n == "UnitOwnerChangeEvent":
            u = units.get(e.unit_id)
            if u is not None:
                owner_changes.append([_t(e.frame), e.unit_id, u["owner"], e.control_pid])
                u["owner"] = e.control_pid
        elif n == "UnitPositionsEvent":
            t = _t(e.frame)
            for unit, (x, y) in e.units.items():
                positions.append([t, unit.id, x, y])
        elif n == "PlayerStatsEvent":
            stats.append(stats_row(e, _t(e.frame)))
        elif n == "UpgradeCompleteEvent":
            if not e.upgrade_type_name.startswith(COSMETIC_UPGRADE_PREFIXES):
                upgrades.append([_t(e.frame), e.pid, e.upgrade_type_name])
        elif n == "CameraEvent":
            if e.player is not None and getattr(e.player, "pid", None) in pid_team:
                camera.append([_t(e.frame), e.player.pid, round(e.x, 1), round(e.y, 1)])
        elif n.endswith("CommandEvent") and n != "CommandManagerStateEvent":
            if e.player is None or getattr(e.player, "pid", None) not in pid_team:
                continue
            ab = getattr(e, "ability_name", None)
            x = getattr(e, "x", None)
            y = getattr(e, "y", None)
            commands.append([_t(e.frame), e.player.pid, ab,
                             round(x, 1) if x is not None else None,
                             round(y, 1) if y is not None else None])

    for u in units.values():
        # a value is known when sc2reader's data for this build has a cost, or the unit is free by
        # what it is; a real unit with no cost in the data is UNKNOWN, never zero (measured
        # 2026-10-26: ShieldBattery, Liberator, Thor, Lurker have none on these builds)
        u["vk"] = bool(u["m"] or u["g"]) or bool({"transient", "marker", "neutral"} & set(u["kinds"]))

    return {
        "adapter": ADAPTER_VERSION,
        "game": "sc2",
        "map": r.map_name,
        "build": r.build,
        "end": _t(r.frames),
        "start_time": str(r.start_time),
        "players": players,
        "units": list(units.values()),
        "type_changes": type_changes,
        "owner_changes": owner_changes,
        "positions": positions,
        "stats": stats,
        "upgrades": upgrades,
        "camera": camera,
        "commands": commands,
    }


def save(record: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(record, f, separators=(",", ":"))
    tmp.replace(path)


def load(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)
