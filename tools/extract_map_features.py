#!/usr/bin/env python3
"""Extract map features from a .SC2Map archive.

Reproduces the structure of `white_chamber_map_features_mvp.json`, which was previously produced in
a chat session that no longer exists. Everything here is read from the archive itself:

    t3Terrain.xml   <heightMap>          -> height_map
                    <rampList>/<ramp>    -> ramps
    Objects         ObjectPoint[StartLoc]   -> start_locations
                    ObjectPoint[NoFlyZone]  -> no_fly_zones (with both pathing radii)
                    ObjectUnit[XelNagaTower]   -> xelnaga_towers
                    ObjectUnit[*Destructible*] -> destructibles
                    ObjectUnit[mineral/geyser] -> resource_clusters (single-linkage)
    (listfile)                           -> files_available
    Minimap.tga                          -> optional PNG

Usage:
    python tools/extract_map_features.py <map.SC2Map> [-o out.json] [--minimap out.png]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

from mpyq import MPQArchive

from resource_contents import (BASE_PATCH_MIN_MINERALS, PROVENANCE as CONTENTS_PROVENANCE,
                               gas_at_start, is_base_patch, minerals_at_start)

#: Single-linkage radius over MINERAL patches only, in map cells.
#:
#: Geysers are NOT clustered directly. They sit offset from the mineral line, and at any radius
#: large enough to hold a mineral line together a geyser will bridge two adjacent bases. Measured:
#: clustering all resource nodes at radius 9.0 merged base pairs into 20-node blobs on Baalpeak,
#: Incognito, Sentinel and Tacit Sanctity. Clustering minerals at 6.0 and then attaching each
#: geyser to its nearest mineral cluster removes every merge and leaves plausible per-map variation
#: (gold and pocket bases), while reproducing White Chamber's 26 x (8 minerals + 2 geysers) exactly.
CLUSTER_RADIUS = 6.0

#: A group of at most ATTACH_MAX_PATCHES base-material patches lying within ATTACH_RADIUS of a larger
#: group is part of that base, not a base of its own. Measured 2026-09-24 on all 38 maps: Sanctuary
#: III LE places one rich patch 6.71 cells from its mineral line (just outside CLUSTER_RADIUS), and
#: the next-closest gap between two DISTINCT groups anywhere on the pool is 7.07 cells (Desolator,
#: Megastructure) - so raising CLUSTER_RADIUS to 7.0 would have left a 0.07 margin. Attaching small
#: groups within 8.0 fixes Sanctuary III and merges nothing else: every other group is a full line,
#: and full lines are never attached to each other.
ATTACH_RADIUS = 8.0
ATTACH_MAX_PATCHES = 3

MINERAL_HINT = "mineral"
GEYSER_HINT = "geyser"


def pos(s):
    """'220,160,0' -> {x, y, z}"""
    p = [float(v) for v in (s or "0,0,0").split(",")]
    while len(p) < 3:
        p.append(0.0)
    return {"x": p[0], "y": p[1], "z": p[2]}


def parse_base(raw):
    """'... c=(9.500000e+01, 5.600000e+01) w=8.000000e+00 h=1.000000e+00' -> centre + extent."""
    if not raw:
        return None
    c = re.search(r"c=\(\s*([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*\)", raw)
    w = re.search(r"\bw=([-\d.eE+]+)", raw)
    h = re.search(r"\bh=([-\d.eE+]+)", raw)
    if not c:
        return None
    return {"x": float(c.group(1)), "y": float(c.group(2)),
            "w": float(w.group(1)) if w else 0.0,
            "h": float(h.group(1)) if h else 0.0}


def cluster(points, radius):
    """Single-linkage clustering on (x, y). Returns lists of indices."""
    n = len(points)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    r2 = radius * radius
    for i in range(n):
        xi, yi = points[i]
        for j in range(i + 1, n):
            xj, yj = points[j]
            if (xi - xj) ** 2 + (yi - yj) ** 2 <= r2:
                a, b = find(i), find(j)
                if a != b:
                    parent[a] = b
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def extract(map_path, cluster_radius=CLUSTER_RADIUS, minimap_out=None):
    a = MPQArchive(map_path)

    try:
        listfile = [x.strip() for x in
                    a.read_file("(listfile)").decode("utf-8", "replace").splitlines() if x.strip()]
    except Exception:
        listfile = []

    terrain = ET.fromstring(a.read_file("t3Terrain.xml").decode("utf-8", "replace"))
    hm = terrain.find("heightMap")
    height_map = dict(hm.attrib) if hm is not None else {}

    ramps = []
    ramp_list = hm.find("rampList") if hm is not None else None
    for i, r in enumerate(ramp_list if ramp_list is not None else []):
        raw = r.get("base")
        ramps.append({
            "id": i,
            "dir": int(r.get("dir", 0)),
            "hi": int(r.get("hi", 0)),
            "lo": int(r.get("lo", 0)),
            "base_center": parse_base(raw),
            "raw_base": raw,
        })

    root = ET.fromstring(a.read_file("Objects").decode("utf-8", "replace"))
    start_locations, no_fly_zones, xelnaga_towers, destructibles = [], [], [], []
    min_pts, min_types, gey_pts, gey_types = [], [], [], []

    for e in root:
        t = e.get("Type") or e.get("UnitType") or ""
        p = pos(e.get("Position"))
        if e.tag == "ObjectPoint":
            if t == "StartLoc":
                start_locations.append({"name": e.get("Name"), "position": p})
            elif t == "NoFlyZone":
                no_fly_zones.append({
                    "name": e.get("Name"), "position": p,
                    "hard_radius": e.get("PathingRadiusHard"),
                    "soft_radius": e.get("PathingRadiusSoft"),
                })
        elif e.tag == "ObjectUnit":
            low = t.lower()
            if t == "XelNagaTower":
                xelnaga_towers.append({"id": e.get("Id"), "type": t, "position": p})
            elif "destructible" in low or "collapsible" in low:
                # "Collapsible*Tower" units are destructible blockers whose names do not contain
                # the word: CollapsibleRockTower (Baalpeak x8), CollapsibleTerranTowerDiagonal
                # (Incognito x2). Omitting them was the only delta against the reference data.
                destructibles.append({"id": e.get("Id"), "type": t, "position": p})
            elif MINERAL_HINT in low:
                min_pts.append((p["x"], p["y"]))
                min_types.append(t)
            elif GEYSER_HINT in low:
                gey_pts.append((p["x"], p["y"]))
                gey_types.append(t)

    # Wall material vs base material is decided per PATCH from its minerals at map start (ruling
    # 2026-09-24): a 450-class patch is a wall, everything else is base material. Measured on all
    # 38 maps: the two never mix inside one group, and cluster TOTALS would not separate them
    # (a 12-patch wall = 5400 = a gasless gold pocket; a 16-patch wall = 7200 = every rich base).
    base_idx = [i for i, t in enumerate(min_types) if is_base_patch(t)]
    wall_idx = [i for i, t in enumerate(min_types) if not is_base_patch(t)]
    unknown_types = sorted({t for t in min_types if minerals_at_start(t) is None}
                           | {t for t in gey_types if gas_at_start(t) is None})

    def groups_of(idx):
        pts = [min_pts[i] for i in idx]
        return sorted(([idx[k] for k in g] for g in cluster(pts, cluster_radius)),
                      key=lambda g: min(g))

    def centroid(g):
        return (sum(min_pts[i][0] for i in g) / len(g), sum(min_pts[i][1] for i in g) / len(g))

    base_groups = groups_of(base_idx)
    wall_groups = groups_of(wall_idx)

    # A stray patch or two near a full line belongs to that line. See ATTACH_RADIUS.
    small = [g for g in base_groups if len(g) <= ATTACH_MAX_PATCHES]
    large = [g for g in base_groups if len(g) > ATTACH_MAX_PATCHES]
    for g in small:
        cx, cy = centroid(g)
        near = []
        for h in large:
            d = min(((min_pts[i][0] - cx) ** 2 + (min_pts[i][1] - cy) ** 2) ** 0.5 for i in h)
            if d <= ATTACH_RADIUS:
                near.append((d, h))
        if near:
            min(near, key=lambda x: x[0])[1].extend(g)
        else:
            large.append(g)
    base_groups = sorted((sorted(g) for g in large), key=lambda g: min(g))

    # Geysers attach to the nearest BASE; a wall never owns a geyser.
    centroids = [centroid(g) for g in base_groups]
    attached = [[] for _ in base_groups]
    for gi, (gx, gy) in enumerate(gey_pts):
        if not centroids:
            break
        j = min(range(len(centroids)),
                key=lambda k: (centroids[k][0] - gx) ** 2 + (centroids[k][1] - gy) ** 2)
        attached[j].append(gi)

    def record(cid, idxs, gey, kind):
        xs = [min_pts[i][0] for i in idxs] + [gey_pts[i][0] for i in gey]
        ys = [min_pts[i][1] for i in idxs] + [gey_pts[i][1] for i in gey]
        counts = {}
        for i in idxs:
            counts[min_types[i]] = counts.get(min_types[i], 0) + 1
        for i in gey:
            counts[gey_types[i]] = counts.get(gey_types[i], 0) + 1
        contents = [minerals_at_start(min_types[i]) for i in idxs]
        complete = all(c is not None for c in contents)
        return {
            "id": cid,
            "kind": kind,
            "center": {"x": round(sum(xs) / len(xs), 1), "y": round(sum(ys) / len(ys), 1)},
            "resource_counts": counts,
            "mineral_count": len(idxs),
            "geyser_count": len(gey),
            "point_count": len(idxs) + len(gey),
            "minerals_at_start": sum(contents) if complete else None,
            "patch_min_minerals": min(contents) if complete else None,
        }

    resource_clusters = [record(cid, g, attached[cid], "base") for cid, g in enumerate(base_groups)]
    mineral_walls = [record(cid, g, [], "wall") for cid, g in enumerate(wall_groups)]

    if minimap_out:
        try:
            from PIL import Image
            import io
            Image.open(io.BytesIO(a.read_file("Minimap.tga"))).convert("RGBA").save(minimap_out)
        except Exception as exc:
            print("  [warn] minimap: %s" % exc, file=sys.stderr)

    return {
        "map_file": os.path.basename(map_path),
        "files_available": listfile,
        "height_map": height_map,
        "start_locations": start_locations,
        "ramps": ramps,
        "xelnaga_towers": xelnaga_towers,
        "destructibles": destructibles,
        "no_fly_zones": no_fly_zones,
        "resource_clusters": resource_clusters,
        "mineral_walls": mineral_walls,
        "unknown_resource_types": unknown_types,
        "notes": [
            "Ramp/cliff info comes from t3Terrain.xml rampList.",
            "Object positions come from the Objects XML (ObjectPoint / ObjectUnit).",
            "Resource clusters are single-linkage groupings at radius %.1f map cells; a group of at "
            "most %d base-material patches within %.1f cells of a larger group is attached to it."
            % (cluster_radius, ATTACH_MAX_PATCHES, ATTACH_RADIUS),
            "resource_clusters are BASES; mineral_walls are groups of wall material - patches below "
            "%d minerals at map start, e.g. MineralField450. Classification is per patch from its "
            "minerals at map start, never updated as patches are mined out (ruling 2026-09-24)."
            % BASE_PATCH_MIN_MINERALS,
            "minerals_at_start / patch_min_minerals: " + CONTENTS_PROVENANCE,
            "start_locations are the map's StartLoc candidates, not the spawns: a map may carry more "
            "than the game has players (Old Sun Temple LE: four on a map played 1v1). Spawns come "
            "from the replay - tools/spawns_from_replay.py.",
        ],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("map")
    ap.add_argument("-o", "--out")
    ap.add_argument("--minimap")
    ap.add_argument("--cluster-radius", type=float, default=CLUSTER_RADIUS)
    args = ap.parse_args()

    d = extract(args.map, args.cluster_radius, args.minimap)
    text = json.dumps(d, indent=2)
    if args.out:
        open(args.out, "w", encoding="utf-8").write(text)
        print(args.out)
    else:
        print(text)
    print("  ramps=%d starts=%d nofly=%d towers=%d destructibles=%d bases=%d walls=%d"
          % (len(d["ramps"]), len(d["start_locations"]), len(d["no_fly_zones"]),
             len(d["xelnaga_towers"]), len(d["destructibles"]),
             len(d["resource_clusters"]), len(d["mineral_walls"])), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
