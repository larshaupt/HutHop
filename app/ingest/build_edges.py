"""Build the place graph (hut↔hut, stop→hut, hut→stop edges) on the swissTLM3D hiking network.

    uv run python -m app.ingest.build_edges                 # all huts (~2–4 min)
    uv run python -m app.ingest.build_edges --cutoff 480    # max hiking time per edge, minutes

Steps (see hut-tour-planner.md, "Routing approach"):
1. Trail graph from the 3D trail lines, one directed edge per line and direction, weighted by Swiss hiking time.
2. Snap huts and stops to the nearest trail node (snap distance stored on `place`).
3. From every hut, Dijkstra up to the cutoff, forward (hut → hut/stop) and on the reversed graph (stop → hut),
   once per trail category limit (yellow only, up to red-white, up to blue-white, up to unofficial paths).
4. Store edges with a simplified route line: all huts reached, and per hut and direction only the closest stop of
   each line in the summer timetable (`stop_line`, from load_timetable), so a bus line along the valley gives one
   stop, not all of them. Without a loaded timetable: the STOPS_PER_HUT fastest stops. Higher category limits only
   store edges that differ from the easier one.
5. Keep manual `excluded` flags and notes across rebuilds, and attach matching SAC hut-to-hut routes.

Huts too far from a signposted trail reach it over an OpenStreetMap path when osm_access found one: an extra
trail line of category 4 ("unofficial path") from the hut to the nearest trail node.
"""

import argparse
import json
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
import shapely
from pyproj import Transformer
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

from app.db import ROOT, connect
from app.ingest import osm_access, osm_trails
from app.planner import geometry, hiking_time, polyline

GPKG = ROOT / "data/tlm/SWISSTLM3D_WANDERWEGE.gpkg"
CATEGORY = {"Wanderweg": 1, "Bergwanderweg": 2, "Alpinwanderweg": 3}  # yellow, red-white, blue-white
OSM_CATEGORY = 4  # unofficial path from OpenStreetMap (osm_access), only to huts off the signposted network
LIMITS = (1, 2, 3, 4)
NODE_PRECISION_M = 1.0
CUTOFF_MIN = 600.0
STOP_MAX_SNAP_M = 300.0
# OpenStreetMap trails abroad meet swissTLM3D at the Swiss and Liechtenstein border (stitch): nodes within BORDER_M
# of it (countries.json is simplified to 5 m) are joined to the other network's nearest within STITCH_M.
BORDER_M = 5.0
STITCH_M = 50.0
# Most stops kept per hut, direction and category limit (after keeping only the closest stop of each line).
STOPS_PER_HUT = 40
# Huts further than this from a signposted trail (glacier and high-alpine huts, huts abroad) get no computed
# edges: the gap would be counted as flat walking and give misleading times.
HUT_MAX_SNAP_M = 500.0
HUT_REVIEW_SNAP_M = 200.0
SIMPLIFY_M = 10.0
SAME_EDGE_MIN = 1.0  # a higher category limit must save at least this much time to store a separate edge
BATCH = 8  # huts per Dijkstra call: each holds distances and predecessors for all ~1M trail nodes
LV95_TO_WGS84 = Transformer.from_crs("EPSG:2056", "EPSG:4326", always_xy=True)
WGS84_TO_LV95 = Transformer.from_crs("EPSG:4326", "EPSG:2056", always_xy=True)


@dataclass
class EdgeSet:
    """Directed trail edges allowed under one category limit, at most one per node pair (the fastest)."""

    src: np.ndarray
    dst: np.ndarray
    time: np.ndarray
    length: np.ndarray
    up: np.ndarray
    down: np.ndarray
    category: np.ndarray
    line: np.ndarray
    reverse: np.ndarray
    keys: np.ndarray  # src * n_nodes + dst, sorted
    graph: csr_matrix


@dataclass
class TrailNetwork:
    node_xy: np.ndarray
    node_z: np.ndarray
    lines: list[np.ndarray]
    edges: dict[int, EdgeSet]

    def lookup(self, limit: int, src: np.ndarray, dst: np.ndarray) -> np.ndarray:
        es = self.edges[limit]
        return np.searchsorted(es.keys, src.astype(np.int64) * len(self.node_xy) + dst)


def network_from_lines(lines: list[np.ndarray], categories: list[int]) -> TrailNetwork:
    """Build the directed trail graph from 3D lines (LV95 metres) and their trail categories (1–3)."""
    # Nodes: line ends merged to NODE_PRECISION_M, numbered in order of first appearance. Vectorised, and the edge
    # rows kept in arrays: Python objects per line took ~1.7 GB for swissTLM3D alone.
    ids = np.array([i for i, c in enumerate(lines) if len(c) >= 2])
    ends = np.empty((2 * len(ids), 3))
    for k, i in enumerate(ids):
        ends[2 * k], ends[2 * k + 1] = lines[i][0], lines[i][-1]
    keys = np.round(ends[:, :2] / NODE_PRECISION_M).astype(np.int64)
    _, first, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    order = np.argsort(first)
    rank = np.empty_like(order)
    rank[order] = np.arange(len(order))
    node_of = rank[inverse.ravel()]
    node_xy, node_z = ends[first[order], :2], ends[first[order], 2]
    a, b = node_of[0::2], node_of[1::2]
    ids, a, b = ids[a != b], a[a != b], b[a != b]

    m = np.empty((len(ids), 5))
    for k, i in enumerate(ids):
        mi = hiking_time.line_metrics(lines[i])
        m[k] = mi["t_fwd"], mi["t_back"], mi["length"], mi["up"], mi["down"]
    t_fwd, t_back, length, up, down = m.T
    pair = lambda x, y: np.column_stack([x, y]).ravel()  # noqa: E731 (forward and reverse row of each line)
    arr = {"src": pair(a, b), "dst": pair(b, a), "time": pair(t_fwd, t_back), "length": np.repeat(length, 2),
           "up": pair(up, down), "down": pair(down, up), "category": np.repeat(np.asarray(categories)[ids], 2),
           "line": np.repeat(ids, 2), "reverse": np.tile([False, True], len(ids))}
    n = len(node_xy)
    edges = {}
    for limit in LIMITS:
        mask = arr["category"] <= limit
        sub = {k: v[mask] for k, v in arr.items()}
        order = np.lexsort((sub["time"], sub["dst"], sub["src"]))  # fastest edge first within each pair
        sub = {k: v[order] for k, v in sub.items()}
        keys = sub["src"].astype(np.int64) * n + sub["dst"]
        first = np.concatenate(([True], keys[1:] != keys[:-1]))
        sub = {k: v[first] for k, v in sub.items()}
        # csgraph treats explicit zeros as missing edges, so keep weights strictly positive.
        graph = csr_matrix((sub["time"] + 1e-6, (sub["src"], sub["dst"])), shape=(n, n))
        edges[limit] = EdgeSet(**sub, keys=keys[first], graph=graph)
    return TrailNetwork(node_xy, node_z, lines, edges)


def load_network(gpkg=GPKG, access: list[np.ndarray] = ()) -> TrailNetwork:
    """The trail graph: swissTLM3D, with OpenStreetMap's trails instead in the regions abroad when osm_trails has
    extracted them, plus access paths (3D lines from a hut towards the trails) as category-4 lines."""
    lines, categories = osm_trails.read_lines(gpkg, "wanderwege", layer="tlm_strassen_strasse")
    categories = [CATEGORY.get(c, 1) for c in categories]
    osm_lines, osm_categories, replaced = osm_trails.load()
    if osm_lines:
        lines, categories = merge_osm(lines, categories, osm_lines, osm_categories, replaced)
    return network_from_lines(*with_access(lines, categories, access))


def merge_osm(lines: list[np.ndarray], categories: list[int], osm_lines: list[np.ndarray], osm_categories: list[int],
              replaced: shapely.Geometry) -> tuple[list[np.ndarray], list[int]]:
    """swissTLM3D lines minus their parts in the replaced area (the regions abroad, where OpenStreetMap is used), plus
    the OpenStreetMap lines and the connectors that join the two where they meet (stitch): on the Swiss and
    Liechtenstein border, and where the region's extract ends next to swissTLM3D's coverage of Italy or Germany."""
    # Only lines whose bounding box touches the replaced area are clipped; the rest stay as they are.
    boxes = shapely.box(*np.array([(*c[:, :2].min(axis=0), *c[:, :2].max(axis=0)) if len(c) else (0, 0, 0, 0)
                                   for c in lines]).T)
    shapely.prepare(replaced)
    near = shapely.intersects(replaced, boxes) & np.array([len(c) >= 2 for c in lines])
    keep = np.flatnonzero(near)
    geoms = shapely.linestrings(np.concatenate([lines[i] for i in keep]),
                                indices=np.repeat(np.arange(len(keep)), [len(lines[i]) for i in keep]))
    index, kept = osm_trails.clip(geoms, replaced, inside=False)
    tlm_lines = [c for c, n in zip(lines, near) if not n] + osm_trails.split_coordinates(kept)
    tlm_categories = [c for c, n in zip(categories, near) if not n] + [categories[keep[i]] for i in index]
    connectors, connector_categories = stitch(tlm_lines, tlm_categories, osm_lines, osm_categories,
                                              shapely.boundary(replaced))
    print(f"trails: {len(tlm_lines):,} swissTLM3D lines (of {len(lines):,}), {len(osm_lines):,} OpenStreetMap lines "
          f"abroad, {len(connectors)} connectors at the border")
    return (tlm_lines + list(osm_lines) + connectors,
            tlm_categories + list(osm_categories) + connector_categories)


def border_nodes(lines: list[np.ndarray], categories: list[int],
                 near: shapely.Geometry) -> tuple[np.ndarray, np.ndarray]:
    """The graph nodes (line ends merged to NODE_PRECISION_M, N×3) within BORDER_M of `near`, and the easiest
    category of the lines ending at each."""
    if not len(lines):
        return np.empty((0, 3)), np.empty(0, dtype=int)
    ends = np.array([(c[0], c[-1]) for c in lines]).reshape(-1, 3)
    keys = np.round(ends[:, :2] / NODE_PRECISION_M).astype(np.int64)
    _, first, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    easiest = np.full(len(first), max(LIMITS))
    np.minimum.at(easiest, inverse.ravel(), np.repeat(categories, 2))
    near_border = shapely.dwithin(near, shapely.points(ends[first, :2]), BORDER_M)
    return ends[first][near_border], easiest[near_border]


def stitch(lines_a: list[np.ndarray], categories_a: list[int], lines_b: list[np.ndarray], categories_b: list[int],
           border: shapely.Geometry) -> tuple[list[np.ndarray], list[int]]:
    """Straight connectors between two trail networks at the border between them: each node of either network on
    the border is joined to the nearest node of the other on it, within STITCH_M. Both are cut at the same border
    line, but where a trail crosses it they rarely end at the same point (different mapping), and a junction on the
    border (Pfälzerhütte on Bettlerjoch) is a junction in both networks a few metres apart, so without these the
    two would not touch. A connector gets the harder of the easiest categories at its two nodes."""
    shapely.prepare(border)
    a_xyz, a_cat = border_nodes(lines_a, categories_a, border)
    b_xyz, b_cat = border_nodes(lines_b, categories_b, border)
    if not len(a_xyz) or not len(b_xyz):
        return [], []
    dist, nearest = cKDTree(b_xyz[:, :2]).query(a_xyz[:, :2], distance_upper_bound=STITCH_M)
    pairs = {(i, j) for i, (d, j) in enumerate(zip(dist, nearest)) if np.isfinite(d)}
    dist, nearest = cKDTree(a_xyz[:, :2]).query(b_xyz[:, :2], distance_upper_bound=STITCH_M)
    pairs |= {(i, j) for j, (d, i) in enumerate(zip(dist, nearest)) if np.isfinite(d)}
    a_key, b_key = (np.round(xyz[:, :2] / NODE_PRECISION_M) for xyz in (a_xyz, b_xyz))
    pairs = sorted((i, j) for i, j in pairs if (a_key[i] != b_key[j]).any())  # else already one node
    return [np.vstack([a_xyz[i], b_xyz[j]]) for i, j in pairs], [max(a_cat[i], b_cat[j]) for i, j in pairs]


def with_access(lines: list[np.ndarray], categories: list[int], access) -> tuple[list[np.ndarray], list[int]]:
    """Trail lines plus each access path, extended to the nearest trail line end so that it joins the graph there."""
    if not len(access):
        return lines, categories
    ends = np.concatenate([[c[0], c[-1]] for c in lines if len(c) >= 2])
    _, nearest = cKDTree(ends[:, :2]).query([a[-1, :2] for a in access])
    return lines + [np.vstack([a, ends[i]]) for a, i in zip(access, nearest)], categories + [OSM_CATEGORY] * len(access)


def snap(network: TrailNetwork, xy: np.ndarray, allowed: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Nearest trail node (of the `allowed` ones, a boolean mask; default all) and distance (m) for each point."""
    candidates = np.flatnonzero(allowed) if allowed is not None else np.arange(len(network.node_xy))
    tree = shapely.STRtree(shapely.points(network.node_xy[candidates]))
    (_, nodes), dist = tree.query_nearest(shapely.points(xy), return_distance=True, all_matches=False)
    return candidates[nodes], dist


def tree_aggregates(pred: np.ndarray, source: int, edge_vals: dict[str, np.ndarray],
                    reached: np.ndarray) -> dict[str, np.ndarray]:
    """Sum/max edge values from the source to every reached node of a shortest-path tree.
    Uses pointer doubling on the reached nodes only."""
    idx = np.flatnonzero(reached)
    k = len(idx)
    pos = np.full(len(pred), k, dtype=np.int64)  # k = sentinel (the source and unreached nodes)
    pos[idx] = np.arange(k)
    has_parent = pred[idx] >= 0  # False only for the source
    parent = np.where(has_parent, pos[np.where(has_parent, pred[idx], source)], k)
    anc = np.append(parent, k)
    sums = {name: np.append(np.where(has_parent, v[idx], 0.0), 0.0) for name, v in edge_vals.items()
            if name != "category"}
    cat = np.append(np.where(has_parent, edge_vals["category"][idx], 0), 0)
    while (anc[:k] != k).any():
        for name in sums:
            sums[name] = sums[name] + np.where(anc != k, sums[name][anc], 0.0)
        cat = np.maximum(cat, cat[anc])
        anc = anc[anc]
    out = {name: np.zeros(len(pred)) for name in sums}
    for name in sums:
        out[name][idx] = sums[name][:k]
    out["category"] = np.zeros(len(pred), dtype=int)
    out["category"][idx] = cat[:k]
    return out


def path_nodes(pred: np.ndarray, source: int, target: int) -> list[int]:
    nodes = [target]
    while nodes[-1] != source:
        nodes.append(int(pred[nodes[-1]]))
    return nodes[::-1]


def path_geometry(network: TrailNetwork, limit: int, nodes: list[int]) -> tuple[str, str]:
    """Encoded WGS84 polyline of a node path (original direction), simplified in LV95, and the encoded height of
    each of its points. The simplified line keeps a subset of the trail vertices, with their heights: no lookup of
    the nearest of all ~20M vertices (which took ~800 MB with Austria)."""
    es = network.edges[limit]
    if len(nodes) < 2:
        nodes = nodes * 2
    ids = network.lookup(limit, np.array(nodes[:-1]), np.array(nodes[1:])) if nodes[0] != nodes[1] else []
    parts = []
    for e in ids:
        coords = network.lines[es.line[e]]
        parts.append(coords[::-1] if es.reverse[e] else coords)
    xyz = (np.concatenate([parts[0]] + [p[1:] for p in parts[1:]]) if parts
           else np.column_stack([network.node_xy[nodes], network.node_z[nodes]]))
    e, n, z = shapely.get_coordinates(shapely.simplify(shapely.linestrings(xyz), SIMPLIFY_M), include_z=True).T
    lon, lat = LV95_TO_WGS84.transform(e, n)
    return polyline.encode(list(zip(lat, lon))), polyline.encode_values([round(float(h)) for h in z])


def search(network: TrailNetwork, places: list[dict], cutoff: float = CUTOFF_MIN) -> list[dict]:
    """All hut→hut, hut→stop and stop→hut edges within the cutoff, per category limit."""
    max_snap = {"hut": HUT_MAX_SNAP_M, "stop": STOP_MAX_SNAP_M}
    hut_sources = [p for p in places if p["kind"] == "hut" and p["snap_m"] <= HUT_MAX_SNAP_M]
    by_node: dict[int, list[dict]] = {}
    for p in places:
        if p["snap_m"] <= max_snap[p["kind"]]:
            by_node.setdefault(p["node"], []).append(p)
    target_nodes = np.array(sorted(by_node))

    found: dict[tuple[int, int, int], dict] = {}
    for limit in LIMITS:
        graph = network.edges[limit].graph
        for reverse in (False, True):
            searched = graph.T.tocsr() if reverse else graph
            for b in range(0, len(hut_sources), BATCH):
                batch = hut_sources[b:b + BATCH]
                dist, pred = dijkstra(searched, indices=[h["node"] for h in batch], return_predecessors=True,
                                      limit=cutoff)
                for row, hut in enumerate(batch):
                    _collect_from_tree(network, limit, reverse, hut, dist[row], pred[row], by_node, target_nodes,
                                       cutoff, found)
            print(f"  limit {limit} {'reverse' if reverse else 'forward'}: {len(found)} edges so far", flush=True)

    edges = []
    for (_, _, limit), e in found.items():
        e["geometry"], e["elevation"] = path_geometry(network, limit, e.pop("_nodes"))
        e["max_altitude_m"] = max(polyline.decode_values(e["elevation"]), default=None)
        e["footprint"], e["samples"] = geometry.pack(*geometry.footprint(e["geometry"]))
        edges.append(e)
    return edges


def _collect_from_tree(network: TrailNetwork, limit: int, reverse: bool, hut: dict, dist: np.ndarray,
                       pred: np.ndarray, by_node: dict[int, list[dict]], target_nodes: np.ndarray, cutoff: float,
                       found: dict[tuple[int, int, int], dict]) -> None:
    """Add the edges of one hut's shortest-path tree to `found`: all huts reached, and the STOPS_PER_HUT
    fastest stops. Skips edges that an easier category limit already found with (almost) the same time."""
    es = network.edges[limit]
    n = len(network.node_xy)
    src_node = hut["node"]
    reached = np.isfinite(dist)
    with_parent = reached & (pred >= 0)
    tree_parent = np.where(pred >= 0, pred, 0)
    # tree edge parent→node in the searched graph = original edge parent→node (forward) or node→parent (reverse)
    a, c = (np.arange(n), tree_parent) if reverse else (tree_parent, np.arange(n))
    eid = np.zeros(n, dtype=np.int64)
    eid[with_parent] = network.lookup(limit, a[with_parent], c[with_parent])
    agg = tree_aggregates(pred, src_node, {"length": es.length[eid], "up": es.up[eid], "down": es.down[eid],
                                           "category": es.category[eid]}, reached)

    flat_min_per_m = hiking_time.FLAT_MIN_PER_KM / 1000
    candidates = []  # (total minutes, other place, node)
    for t in target_nodes[reached[target_nodes]]:
        for other in by_node[t]:
            if other["id"] == hut["id"] or (reverse and other["kind"] == "hut"):
                continue
            total = dist[t] + (hut["snap_m"] + other["snap_m"]) * flat_min_per_m
            if total <= cutoff:
                candidates.append((total, other, int(t)))
    huts_reached = [cand for cand in candidates if cand[1]["kind"] == "hut"]
    stops_reached = sorted((cand for cand in candidates if cand[1]["kind"] == "stop"), key=lambda cand: cand[0])

    for total, other, t in huts_reached + closest_per_line(stops_reached):
        frm, to = (other, hut) if reverse else (hut, other)
        easier = next((found[k] for k in ((frm["id"], to["id"], lim) for lim in range(limit - 1, 0, -1))
                       if k in found), None)
        if easier and easier["time_min"] - total < SAME_EDGE_MIN:
            continue  # same path as with an easier limit
        nodes = path_nodes(pred, src_node, t)
        snap_m = hut["snap_m"] + other["snap_m"]
        found[(frm["id"], to["id"], limit)] = {
            "from_place": frm["id"], "to_place": to["id"], "category_limit": limit,
            "time_min": round(total), "distance_m": round(agg["length"][t] + snap_m),
            "ascent_m": round(agg["up"][t]), "descent_m": round(agg["down"][t]),
            "max_category": int(agg["category"][t]) or 1,
            "_nodes": nodes[::-1] if reverse else nodes,
        }


def closest_per_line(stops_reached: list[tuple]) -> list[tuple]:
    """Of the stops reached (fastest first), those that serve a line no faster stop serves, at most STOPS_PER_HUT.
    A stop's `lines` is None when no timetable is loaded: then simply the fastest stops."""
    kept, covered = [], set()
    for cand in stops_reached:
        lines = cand[1].get("lines")
        if lines is None or lines - covered:
            kept.append(cand)
            covered |= lines or set()
    return kept[:STOPS_PER_HUT]


def name_key(name: str) -> str:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    n = re.sub(r"\b(sac|cas|cai|hutte|huette|hut|cabane|capanna|chamanna|rifugio|refuge|biwak|bivouac|"
               r"camona|berghaus|de|la|da|di|du|d|l)\b", " ", n)
    return " ".join(w for w in re.split(r"[^a-z]+", n) if len(w) > 2)


def match_sac_routes(conn: sqlite3.Connection) -> int:
    """Attach SAC hut-to-hut routes ("From <hut> ...") to computed hut→hut edges, for reference."""
    huts = {r["id"]: r for r in conn.execute("SELECT id, name, routes FROM hut_effective")}
    keys = {hid: name_key(h["name"]) for hid, h in huts.items()}
    matched = 0
    for e in conn.execute("SELECT e.id, e.from_place, e.to_place FROM edge e JOIN hut h ON h.place_id = e.from_place "
                          "WHERE e.to_place IN (SELECT place_id FROM hut)").fetchall():
        key = keys[e["from_place"]]
        if len(key) < 5:
            continue
        routes = json.loads(huts[e["to_place"]]["routes"] or "[]")
        route = next((r for r in routes if r["discipline"] == "mountain_hiking" and key in name_key(r["title"])), None)
        if route:
            conn.execute("UPDATE edge SET sac_route_id = ?, sac_time_min = ?, sac_grade = ? WHERE id = ?",
                         (route["id"], route["ascent_time_max"] or route["ascent_time_min"],
                          (route["main_difficulty"] or "").strip() or None, e["id"]))
            matched += 1
    return matched


def store(conn: sqlite3.Connection, places: list[dict], edges: list[dict], network: TrailNetwork) -> None:
    computed_at = datetime.now(UTC).isoformat(timespec="seconds")
    kept = {(r["from_place"], r["to_place"], r["category_limit"]): (r["excluded"], r["note"]) for r in conn.execute(
        "SELECT from_place, to_place, category_limit, excluded, note FROM edge "
        "WHERE source = 'tlm_routed' AND (excluded = 1 OR note IS NOT NULL)")}
    # A connection whose limit variant is gone (e.g. new trails made the easier route as fast) keeps its pair's flags
    by_pair = {(f, t): flags for (f, t, _), flags in sorted(kept.items(), key=lambda kv: kv[1][0])}
    with conn:
        conn.executemany("UPDATE place SET snap_m = :snap_m WHERE id = :id",
                         [{"id": p["id"], "snap_m": round(p.get("trail_snap_m", p["snap_m"]), 1)} for p in places])
        conn.executemany("UPDATE place SET altitude_m = :z WHERE id = :id AND altitude_m IS NULL",
                         [{"id": p["id"], "z": round(float(network.node_z[p["node"]]))}
                          for p in places if p["kind"] == "stop" and p["snap_m"] <= STOP_MAX_SNAP_M])
        conn.execute("DELETE FROM edge WHERE source = 'tlm_routed'")
        conn.executemany(
            "INSERT INTO edge (from_place, to_place, distance_m, ascent_m, descent_m, time_min, max_category, "
            "category_limit, source, geometry, elevation, max_altitude_m, footprint, samples, excluded, note, "
            "computed_at) "
            "VALUES (:from_place, :to_place, :distance_m, :ascent_m, :descent_m, :time_min, :max_category, "
            ":category_limit, 'tlm_routed', :geometry, :elevation, :max_altitude_m, :footprint, :samples, :excluded, "
            ":note, "
            ":computed_at)",
            [e | dict(zip(("excluded", "note"), kept.get((e["from_place"], e["to_place"], e["category_limit"]))
                          or by_pair.get((e["from_place"], e["to_place"]), (0, None))))
             | {"computed_at": computed_at} for e in edges],
        )
        matched = match_sac_routes(conn)
    print(f"stored {len(edges)} edges ({len(kept)} manual flags kept, {matched} matched to SAC routes)")


def attach_access(network: TrailNetwork, places: list[dict], access: dict[int, dict]) -> None:
    """Snap places to the signposted trails; a far hut with an access path starts at that path's first node
    (the hut itself) instead. `trail_snap_m` keeps the distance to the signposted trails (stored on `place`)."""
    starts = np.array([], dtype=int)
    if access:
        starts, _ = snap(network, np.array([a["coords"][0, :2] for a in access.values()]))
    trails = np.ones(len(network.node_xy), bool)
    trails[starts] = False
    nodes, dists = snap(network, np.array([(p["e_lv95"], p["n_lv95"]) for p in places]), trails)
    start_of = dict(zip(access, starts.tolist()))
    for p, node, d in zip(places, nodes, dists):
        p["node"], p["snap_m"] = int(node), float(d)
        if p["kind"] == "hut" and d > HUT_MAX_SNAP_M and p["id"] in start_of:
            p["trail_snap_m"], p["node"], p["snap_m"] = p["snap_m"], start_of[p["id"]], 0.0


def build(conn: sqlite3.Connection, cutoff: float = CUTOFF_MIN) -> int:
    """Rebuild all computed edges. Returns the number of edges stored."""
    started = time.time()
    access = osm_access.load()
    network = load_network(access=[a["coords"] for a in access.values()])
    print(f"trail graph: {len(network.node_xy):,} nodes, {len(network.edges[OSM_CATEGORY].src):,} directed edges, "
          f"{len(access)} OSM access paths ({time.time() - started:.0f} s)")
    places = [dict(r) for r in conn.execute(
        "SELECT p.id, p.kind, p.name, p.e_lv95, p.n_lv95 FROM place p "
        "LEFT JOIN hut h ON h.place_id = p.id LEFT JOIN stop s ON s.place_id = p.id")]
    attach_access(network, places, access)
    lines: dict[int, set[str]] = {}
    for r in conn.execute("SELECT stop_place, line FROM stop_line WHERE departures > 0"):
        lines.setdefault(r["stop_place"], set()).add(r["line"])
    if lines:  # stops without summer service get no edges
        for p in places:
            if p["kind"] == "stop":
                p["lines"] = lines.get(p["id"], set())
    print(f"timetable: {len(lines):,} stops with summer service" if lines else
          "timetable: not loaded, keeping the fastest stops (run app.ingest.load_timetable)")
    review = sum(1 for p in places if p["kind"] == "hut" and HUT_REVIEW_SNAP_M < p["snap_m"] <= HUT_MAX_SNAP_M)
    off = sum(1 for p in places if p["kind"] == "hut" and p["snap_m"] > HUT_MAX_SNAP_M)
    via_osm = sum(1 for p in places if "trail_snap_m" in p)
    print(f"snapped {len(places):,} places; huts: {review} to review (>{HUT_REVIEW_SNAP_M:.0f} m from a trail), "
          f"{via_osm} over an OSM path, {off} off the network (>{HUT_MAX_SNAP_M:.0f} m, no edges)")
    edges = search(network, places, cutoff)
    store(conn, places, edges, network)
    print(f"done in {time.time() - started:.0f} s")
    return len(edges)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cutoff", type=float, default=CUTOFF_MIN, help="max hiking time per edge (minutes)")
    build(connect(), parser.parse_args().cutoff)


if __name__ == "__main__":
    main()
