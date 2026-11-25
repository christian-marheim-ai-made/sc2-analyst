"""Resource contents per mineral-field / geyser unit type — the first column of the stats table.

PROVENANCE: NOMINAL, read from the unit type name (the `750` / `450` suffixes and the `Rich` prefix
are Blizzard's own naming). NOT verified against game data on 2026-09-24: sc2reader's data files
carry ids and names only, Liquipedia refuses automated fetches, and the game's own data sits in a
CASC store nobody here can read yet (`docs/DECISION-LEDGER.md` DL-9). The owner can verify any row
in seconds by hovering a patch in the client; until then every number carries `PROVENANCE` below.

Sources seen 2026-09-24 (web search, not verified against the client): Legacy of the Void's beta
notes set half the patches per base to 900; two summaries of patch 5.0.16 (2026-06-22) disagree on
what changed - one says small patches 900 -> 1200, another says large 1800 -> 1600 and small
900 -> 1100. So the full-patch values are per build and unsettled (the library spans ten builds);
the classification threshold below is not, because it only has to sit between the 450 wall patch
and the smallest base patch.

Why it exists now: the owner's ruling of 2026-09-24 makes *mineral content at map start* the thing
that separates a base from a mineral wall (`docs/canon/rulings-2026-09-24.md`), and the measurement
behind `docs/REPLAY-LIBRARY-2026-09-23.md` L-1..L-3 showed that only the per-PATCH content separates
them cleanly on the 38-map pool: every wall patch is a 450, every base patch is a 750/900-class or
above, and cluster TOTALS overlap (a 12-patch 450 wall = 5400 = a gasless gold pocket; a 16-patch
450 wall = 7200 = every rich base). The classification threshold therefore sits between 450 and
750 and does not depend on which of 750 / 900 the small patch really holds today.

Contents are a map-start property: they are never updated as patches get mined out (his words).
"""
from __future__ import annotations

PROVENANCE = "nominal-by-type-name; unverified against game data (2026-09-24)"

#: Minerals per patch at map start, by unit type. Unknown types -> None (never a silent default).
MINERALS_AT_START = {
    "MineralField": 1800,
    "LabMineralField": 1800,
    "PurifierMineralField": 1800,
    "MineralField750": 900,          # named 750; widely reported as 900 since a 2016 patch - UNVERIFIED
    "LabMineralField750": 900,
    "PurifierMineralField750": 900,
    "RichMineralField": 1500,
    "RichMineralField750": 900,
    "MineralField450": 450,          # the wall / path-blocker patch; never part of a base on the pool
}

#: Gas per geyser at map start, by unit type. Same provenance.
GAS_AT_START = {
    "VespeneGeyser": 2250,
    "SpacePlatformGeyser": 2250,
    "ShakurasVespeneGeyser": 2250,
    "PurifierVespeneGeyser": 2250,
    "ProtossVespeneGeyser": 2250,
    "RichVespeneGeyser": 2500,       # UNVERIFIED
}

#: A patch holding fewer minerals than this at map start is wall material, not base material.
#: Measured gap on the 38-map pool: walls are 450, the smallest base patch is the 750/900 class.
BASE_PATCH_MIN_MINERALS = 500


def minerals_at_start(unit_type: str):
    return MINERALS_AT_START.get(unit_type)


def gas_at_start(unit_type: str):
    return GAS_AT_START.get(unit_type)


def is_base_patch(unit_type: str) -> bool:
    """True for base material; False for wall material. Unknown types count as base material and
    are reported by the extractor as `unknown_resource_types` rather than silently classified."""
    m = minerals_at_start(unit_type)
    return m is None or m >= BASE_PATCH_MIN_MINERALS
