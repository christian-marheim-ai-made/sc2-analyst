# Functional test suite

```bash
.venv/Scripts/python.exe tests/run.py        # 96 tests, ~7s
.venv/Scripts/python.exe tests/run.py -v
```

Stdlib `unittest`. No new dependencies. Tests **skip** rather than fail when an input is missing, so
a checkout without the gitignored `.SC2Replay` / `.SC2Map` files reports honestly instead of red.

---

## The design point: two kinds of assertion

| | |
|---|---|
| **REGRESSION** | our output vs the reference artifacts the owner supplied — catches drift |
| **INVARIANT** | a property that must hold regardless of any reference — catches the reference itself being wrong |

That distinction is not theoretical here. **Both kinds have already caught real defects, in opposite
directions.**

### An invariant caught the reference being wrong

`white_chamber_map_features_mvp.json` mis-splits five bases: it reports 27 resource clusters where
the map has 26, with six ragged groups of `[4, 5, 6, 8, 12, 15]` nodes. A regression-only suite would
have enshrined that. The invariant — *every cluster is a whole base* — is what proved it, and the
defect is now pinned in `test_reference_file_fails_this_invariant` so it cannot silently change.

### An invariant caught OUR code being wrong

The first run of this suite went **7 red**, and the failures were real:

- **Resource clustering merged base pairs** into 20-node blobs on Baalpeak, Incognito, Sentinel and
  Tacit Sanctity. Cause: geysers sit offset from the mineral line, so at any radius large enough to
  hold a mineral line together a geyser bridges two adjacent bases. Fix: cluster **minerals only**,
  then attach each geyser to its nearest mineral cluster.
- **Two of my own assertions were too strong**, and the data corrected me:
  - *"every cluster is 8 minerals + 2 geysers"* holds on **White Chamber only**. Across the pool
    there are `LabMineralField`, `RichMineralField`, `MineralField450` and four geyser skins
    (`SpacePlatformGeyser`, `VespeneGeyser`, `ShakurasVespeneGeyser`, `PurifierVespeneGeyser`).
  - *"every no-fly zone carries both radii"* — 33 of 34 do; `No Fly Zone 006` carries neither. The
    real invariant is **pairing**: both or neither, and never a fabricated default.

## Non-vacuity guards

A check that cannot fail teaches nothing, so several tests assert their own preconditions:

- `test_reference_is_not_vacuous` — the reference ramp lists must be non-empty and not all equal
- `test_every_resource_node_is_accounted_for` — the archive must actually contain minerals
- `test_no_fly_zone_radii_are_paired_and_not_invented` — at least one map must *have* zones; six of
  seven genuinely have none
- the replay tests assert the reference repository is non-empty before comparing counts
- `test_busiest_choke_is_the_named_tank_line` — asserts the death count is above zero

## Pinned defects

Known bugs are asserted **as present**, each naming its audit row, so that fixing one shows up as a
failing test rather than a silent behaviour change:

| test | row |
|---|---|
| `test_R1_two_clocks_still_present` | R1 — game time vs real time, ratio ≈ 1.4 |
| `test_R2_financial_snapshots_truncate` | R2 — snapshots stop ~72% through |
| `test_R20_ability_decodes_as_chrono_boost` | R20 — the decoder regression that produced a false conclusion |
| `test_resource_clusters_KNOWN_DIVERGENCE` | reference mis-splits five bases |

## Files

| | |
|---|---|
| `fixtures.py` | paths, skip-if-missing, reference loaders |
| `test_map_features.py` | extraction vs reference (the original seven), plus map invariants — pool-wide over every map in canon (38) since 2026-09-24, bases and mineral walls |
| `test_replay_and_join.py` | replay reproduction, the map↔replay join, pinned defects |
| `test_anonymiser.py` | keep rule, whole-word safety, reversibility |
| `test_discover_library.py` | the library inventory never carries a player (guarded against a real replay); cache import is idempotent and refuses version conflicts; extraction through the tool matches the tracked map layer |
| `test_spawns_and_intake_library.py` | spawns come from the replay, proven on White Chamber and the Old Sun Temple twins on L:; the library intake driver keeps every name out of its manifest |
| `test_intake_at_scale.py` | the base-62 alias fits every handle length and never collides; slot fit falls back to bzip2 — the two defects the 774-replay batch exposed |
