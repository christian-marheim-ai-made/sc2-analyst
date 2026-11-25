"""Shared paths and helpers for the functional suite.

Everything here resolves against the repo root. Tests skip (never fail) when an input is absent,
so the suite still runs on a checkout without the gitignored .SC2Replay / .SC2Map files.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "src"))

from sc2analyst import library as _library  # noqa: E402  (the data home, from config)

MAPS = REPO / "canon/maps/source"
REPLAYS = REPO / "canon/replays"
VENV_PY = REPO / ".venv/Scripts/python.exe"

# --- reference artifacts: the "original results" the owner supplied -----------
REF_MAP_AWARENESS = REPO / "archive/reference/maps/sc2_3v3_map_awareness_repositories.json"
REF_WC_FEATURES = REPO / "archive/reference/maps/white_chamber_map_features_mvp.json"
REF_REPLAY_EXTRACT = REPO / "docs/context/reference/andre_gamet_extracted_repositories.json"
REF_TANKLINES = REPO / "archive/reference/examples/white_chamber_decisive_terran_tanklines_summary.json"

EXTRACTOR_VER3 = REPO / "archive/reference/extractor/ver3.py"
POSITION_WARFARE = REPO / "archive/reference/extractor/white_chamber_position_warfare.RUNNABLE.py"

MAP_NAMES = ["Baalpeak LE", "Incognito LE", "Radiator LE", "Sandstorm CE",
             "Sentinel CE", "Tacit Sanctity LE", "White Chamber"]

#: Every map archive present in canon - 38 as of 2026-09-24. Pool-wide invariants run over these;
#: reference regressions and pinned facts about the original pool stay on MAP_NAMES.
LIBRARY_MAP_NAMES = sorted(p.stem for p in MAPS.glob("*.SC2Map")) if MAPS.exists() else []

#: The anonymised twins of the owner's replay library - ruling 2026-09-24: data unfit for git lives
#: in the data home (config/analyst*.toml). Written by tools/intake_library.py; tests skip when absent.
LIBRARY_TWINS = _library.DATA_HOME / "replays"


def need(path: Path, what: str = ""):
    """Skip rather than fail when an input is missing."""
    if not Path(path).exists():
        raise unittest.SkipTest("missing input: %s %s" % (path, what))
    return Path(path)


def load(path: Path):
    return json.loads(need(path).read_text(encoding="utf-8"))


def reference_map_repositories():
    """{map_name: repositories} from the supplied 7-map awareness file."""
    doc = load(REF_MAP_AWARENESS)
    return {m["map_name"]: m["repositories"] for m in doc["maps"]}


def count_events(doc: dict, repo: str) -> int:
    """Events in a second-keyed repository of a replay extract."""
    v = doc.get(repo)
    if not isinstance(v, dict):
        return -1
    return sum(len(x) if isinstance(x, list) else 1 for x in v.values())
