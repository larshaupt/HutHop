"""Build the place graph (hut↔hut, stop→hut, hut→stop edges) on the swissTLM3D hiking network.

    uv run python -m app.ingest.build_edges                 # all huts (~2–4 min)
    uv run python -m app.ingest.build_edges --cutoff 480    # max hiking time per edge, minutes

Steps (see hut-tour-planner.md, "Routing approach"):
1. Trail graph from the 3D trail lines, one directed edge per line and direction, weighted by Swiss hiking time.
2. Snap huts and stops to the nearest trail node (snap distance stored on `place`).
3. From every hut, Dijkstra up to the cutoff, forward (hut → hut/stop) and on the reversed graph (stop → hut),
   once per trail category limit (yellow only, up to red-white, up to blue-white).
4. Store edges with a simplified route line: all huts reached, and the STOPS_PER_HUT fastest stops per hut and
   direction. Higher category limits only store edges that differ from the easier one.
5. Keep manual `excluded` flags and notes across rebuilds, and attach matching SAC hut-to-hut routes.
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
import pyogrio.raw
import shapely
from pyproj import Transformer
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

from app.db import ROOT, connect
from app.planner import hiking_time, polyline

GPKG = ROOT / "data/tlm/SWISSTLM3D_WANDERWEGE.gpkg"
CATEGORY = {"Wanderweg": 1, "Bergwanderweg": 2, "Alpinwanderweg": 3}  # yellow, red-white, blue-white
LIMITS = (1, 2, 3)
NODE_PRECISION_M = 1.0
CUTOFF_MIN = 600.0
STOP_MAX_SNAP_M = 300.0
# Fastest stops kept per hut, direction and category limit. Enough to include the stations of a village with
# many bus stops (e.g. Grindelwald), while keeping the table small.
STOPS_PER_HUT = 40
# Huts further than this from a signposted trail (glacier and high-alpine huts, huts abroad) get no computed
# edges: the gap would be counted as flat walking and give misleading times.
HUT_MAX_SNAP_M = 500.0
HUT_REVIEW_SNAP_M = 200.0
SIMPLIFY_M = 10.0
SAME_EDGE_MIN = 1.0  # a higher category limit must save at least this much time to store a separate edge
BATCH = 32
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
    node_index: dict[tuple[int, int], int] = {}
    node_xy, node_z = [], []

    def node(pt: np.ndarray) -> int:
        key = (round(pt[0] / NODE_PRECISION_M), round(pt[1] / NODE_PRECISION_M))
        if key not in node_index:
            node_index[key] = len(node_xy)
            node_xy.append(pt[:2])
            node_z.append(pt[2])
        return node_index[key]

    rows = []
    for i, (coords, category) in enumerate(zip(lines, categories)):
        if len(coords) < 2:
            continue
        a, b = node(coords[0]), node(coords[-1])
        if a == b:
            continue
        m = hiking_time.line_metrics(coords)
        rows.append((a, b, m["t_fwd"], m["length"], m["up"], m["down"], category, i, False))
        rows.append((b, a, m["t_back"], m["length"], m["down"], m["up"], category, i, True))

    cols = list(zip(*rows))
    arr = {name: np.array(col) for name, col in zip(
        ("src", "dst", "time", "length", "up", "down", "category", "line", "reverse"), cols)}
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
    return TrailNetwork(np.array(node_xy), np.array(node_z), lines, edges)


def load_network(gpkg=GPKG) -> TrailNetwork:
    _, _, geoms, (categories,) = pyogrio.raw.read(gpkg, layer="tlm_strassen_strasse", columns=["wanderwege"])
    shapes = shapely.from_wkb(geoms)
    lines = [shapely.get_coordinates(g, include_z=True) for g in shapes]
    return network_from_lines(lines, [CATEGORY.get(c, 1) for c in categories])


def snap(network: TrailNetwork, xy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Nearest trail node and distance (m) for each point."""
    tree = shapely.STRtree(shapely.points(network.node_xy))
    (_, nodes), dist = tree.query_nearest(shapely.points(xy), return_distance=True, all_matches=False)
    return nodes, dist


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


def path_geometry(network: TrailNetwork, limit: int, nodes: list[int]) -> str:
    """Encoded WGS84 polyline of a node path (original direction), simplified in LV95."""
    es = network.edges[limit]
    if len(nodes) < 2:
        nodes = nodes * 2
    ids = network.lookup(limit, np.array(nodes[:-1]), np.array(nodes[1:])) if nodes[0] != nodes[1] else []
    parts = []
    for e in ids:
        coords = network.lines[es.line[e]][:, :2]
        parts.append(coords[::-1] if es.reverse[e] else coords)
    xy = np.concatenate([parts[0]] + [p[1:] for p in parts[1:]]) if parts else network.node_xy[nodes]
    simple = shapely.simplify(shapely.linestrings(xy), SIMPLIFY_M)
    lon, lat = LV95_TO_WGS84.transform(*shapely.get_coordinates(simple).T)
    return polyline.encode(list(zip(lat, lon)))


class Heights:
    """Height (m) of route line points, from the nearest vertex of the 3D trail lines.

    Route lines are simplified trail lines, so every point is a trail vertex (to within the polyline's ~1 m
    rounding)."""

    def __init__(self, network: TrailNetwork):
        coords = np.concatenate([c for c in network.lines if len(c)])
        self.tree, self.z = cKDTree(coords[:, :2]), coords[:, 2]

    def profile(self, geometry: str) -> str:
        """Encoded heights, one per point of the encoded route line."""
        lat, lon = np.array(polyline.decode(geometry)).T
        _, idx = self.tree.query(np.column_stack(WGS84_TO_LV95.transform(lon, lat)))
        return polyline.encode_values([round(float(z)) for z in self.z[idx]])


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
    heights = Heights(network)
    for (_, _, limit), e in found.items():
        e["geometry"] = path_geometry(network, limit, e.pop("_nodes"))
        e["elevation"] = heights.profile(e["geometry"])
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

    for total, other, t in huts_reached + stops_reached[:STOPS_PER_HUT]:
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
    with conn:
        conn.executemany("UPDATE place SET snap_m = :snap_m WHERE id = :id",
                         [{"id": p["id"], "snap_m": round(p["snap_m"], 1)} for p in places])
        conn.executemany("UPDATE place SET altitude_m = :z WHERE id = :id AND altitude_m IS NULL",
                         [{"id": p["id"], "z": round(float(network.node_z[p["node"]]))}
                          for p in places if p["kind"] == "stop" and p["snap_m"] <= STOP_MAX_SNAP_M])
        conn.execute("DELETE FROM edge WHERE source = 'tlm_routed'")
        conn.executemany(
            "INSERT INTO edge (from_place, to_place, distance_m, ascent_m, descent_m, time_min, max_category, "
            "category_limit, source, geometry, elevation, excluded, note, computed_at) VALUES (:from_place, "
            ":to_place, :distance_m, :ascent_m, :descent_m, :time_min, :max_category, :category_limit, 'tlm_routed', "
            ":geometry, :elevation, :excluded, :note, :computed_at)",
            [e | dict(zip(("excluded", "note"), kept.get((e["from_place"], e["to_place"], e["category_limit"]),
                                                          (0, None))))
             | {"computed_at": computed_at} for e in edges],
        )
        matched = match_sac_routes(conn)
    print(f"stored {len(edges)} edges ({len(kept)} manual flags kept, {matched} matched to SAC routes)")


def build(conn: sqlite3.Connection, cutoff: float = CUTOFF_MIN) -> int:
    """Rebuild all computed edges. Returns the number of edges stored."""
    started = time.time()
    network = load_network()
    print(f"trail graph: {len(network.node_xy):,} nodes, {len(network.edges[3].src):,} directed edges "
          f"({time.time() - started:.0f} s)")
    places = [dict(r) for r in conn.execute(
        "SELECT p.id, p.kind, p.name, p.e_lv95, p.n_lv95 FROM place p "
        "LEFT JOIN hut h ON h.place_id = p.id LEFT JOIN stop s ON s.place_id = p.id")]
    nodes, dists = snap(network, np.array([(p["e_lv95"], p["n_lv95"]) for p in places]))
    for p, node, d in zip(places, nodes, dists):
        p["node"], p["snap_m"] = int(node), float(d)
    review = sum(1 for p in places if p["kind"] == "hut" and HUT_REVIEW_SNAP_M < p["snap_m"] <= HUT_MAX_SNAP_M)
    off = sum(1 for p in places if p["kind"] == "hut" and p["snap_m"] > HUT_MAX_SNAP_M)
    print(f"snapped {len(places):,} places; huts: {review} to review (>{HUT_REVIEW_SNAP_M:.0f} m from a trail), "
          f"{off} off the network (>{HUT_MAX_SNAP_M:.0f} m, no edges)")
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
