"""Spike: route between huts on the official swissTLM3D hiking network and compare with SAC times.

Needs data/tlm/SWISSTLM3D_WANDERWEGE.gpkg (swisstopo open data) and data/raw/sac/huts.json.
Builds a directed graph from the 3D trail lines (one edge per line and direction, weighted
by Swiss hiking time), snaps huts to the nearest graph node and runs Dijkstra.
"""

import json
import pickle
import statistics
from pathlib import Path

import networkx as nx
import numpy as np
import pyogrio.raw
from shapely import STRtree, from_wkb, points

ROOT = Path(__file__).parent.parent
GPKG = ROOT / "data/tlm/SWISSTLM3D_WANDERWEGE.gpkg"
GRAPH_CACHE = ROOT / "data/tlm/graph.pkl"
GRADE = {"Wanderweg": 1, "Bergwanderweg": 2, "Alpinwanderweg": 3}  # yellow, red-white, blue-white
GRADE_LABEL = {1: "yellow", 2: "red", 3: "blue"}
NODE_PRECISION_M = 1.0  # merge line endpoints closer than this
SAMPLE_M = 20.0  # merge vertices into ~20 m pieces before applying the slope formula

# (from hut, to hut, SAC route id). Route ids belong to the destination hut's route list.
PAIRS = [
    ("Brunnihütte", "Rugghubelhütte SAC", 359),
    ("Terrihütte SAC", "Capanna Motterascio CAS", 5553),
    ("Kesch-Hütte SAC", "Chamanna da Grialetsch CAS", 5377),
    ("Ringelspitzhütte SAC", "Calandahütte SAC", 5381),
    ("Etzlihütte SAC", "Treschhütte SAC", 4530),
    ("Capanna Tremorgio", "Capanna Campo Tencia CAS", 2144),
    ("Wildhornhütte SAC", "Geltenhütte SAC", 1521),
    ("Wildhornhütte SAC", "Wildstrubelhütte SAC", 40678),
    ("Cabane de Susanfe CAS", "Auberge de Salanfe", 6564),
    ("Cabane de Moiry CAS", "Cabane des Becs de Bosson", 6547),
    ("Sunniggrathütte", "Leutschachhütte SAC", 1241),
    ("Dossenhütte SAC", "Gaulihütte SAC", 41452),
    ("Gspaltenhornhütte SAC", "Blüemlisalphütte SAC", 1452),
    ("Bergseehütte SAC", "Voralphütte SAC", 899),
    ("Capanna di Sciora CAS", "Capanna Sasc Furä CAS", 100),
    ("Capanna da l'Albigna CAS", "Capanna di Sciora CAS", 24),
    ("Chelenalphütte SAC", "Dammahütte SAC", 40611),
]

# Swiss hiking time (Schweizer Wanderwege, "Wanderzeitberechnung" version 2020.2): minutes per
# horizontal km as a 15th-degree polynomial in slope (percent).
SWISS_COEFFS = [
    14.271, 0.36992, 0.025922, -0.0014384, 3.2105e-05, 8.1542e-06, -9.0261e-08, -2.0757e-08,
    1.0192e-10, 2.8588e-11, -5.7466e-14, -2.1842e-14, 1.5176e-17, 8.6894e-18, -1.3584e-21, -1.4026e-21,
]
SWISS_MAX_SLOPE = 40.0  # polynomial is unrealistic beyond ±40 %; extend linearly in slope


def swiss_min_per_km(slope_pct: float) -> float:
    clamped = max(-SWISS_MAX_SLOPE, min(SWISS_MAX_SLOPE, slope_pct))
    pace = sum(c * clamped**i for i, c in enumerate(SWISS_COEFFS))
    return pace * max(1.0, abs(slope_pct) / SWISS_MAX_SLOPE)


def din_time_min(distance_m: float, ascent_m: float, descent_m: float) -> float:
    """DIN 33466-style hiking time: 4 km/h, 300 m/h up, 500 m/h down."""
    horizontal = distance_m / 1000 / 4
    vertical = ascent_m / 300 + descent_m / 500
    return 60 * (max(horizontal, vertical) + min(horizontal, vertical) / 2)


def node_key(x: float, y: float) -> tuple[int, int]:
    return round(x / NODE_PRECISION_M), round(y / NODE_PRECISION_M)


def line_metrics(coords: np.ndarray) -> dict:
    """Length, ascent, descent and Swiss hiking time (both directions) of one 3D line."""
    pieces, start = [], coords[0]
    for i in range(1, len(coords)):
        p = coords[i]
        dl = float(np.hypot(*(p[:2] - start[:2])))
        if dl >= SAMPLE_M or i == len(coords) - 1:
            if dl > 0:
                pieces.append((dl, float(p[2] - start[2])))
            start = p
    length = sum(dl for dl, _ in pieces)
    up = sum(dz for _, dz in pieces if dz > 0)
    down = -sum(dz for _, dz in pieces if dz < 0)
    fwd = sum(dl / 1000 * swiss_min_per_km(dz / dl * 100) for dl, dz in pieces)
    back = sum(dl / 1000 * swiss_min_per_km(-dz / dl * 100) for dl, dz in pieces)
    return {"length": length, "up": up, "down": down, "t_fwd": fwd, "t_back": back}


def build_graph() -> nx.DiGraph:
    if GRAPH_CACHE.exists():
        return pickle.loads(GRAPH_CACHE.read_bytes())
    _, _, geoms, (categories,) = pyogrio.raw.read(GPKG, layer="tlm_strassen_strasse", columns=["wanderwege"])
    graph = nx.DiGraph()
    for wkb, category in zip(geoms, categories):
        coords = np.asarray(from_wkb(wkb).coords)
        if len(coords) < 2:
            continue
        a, b = node_key(*coords[0][:2]), node_key(*coords[-1][:2])
        if a == b:
            continue
        m = line_metrics(coords)
        grade = GRADE.get(category, 1)
        for u, v, t, up, down in ((a, b, m["t_fwd"], m["up"], m["down"]), (b, a, m["t_back"], m["down"], m["up"])):
            if not graph.has_edge(u, v) or graph[u][v]["time"] > t:
                graph.add_edge(u, v, time=t, length=m["length"], up=up, down=down, grade=grade)
        graph.nodes[a]["z"], graph.nodes[b]["z"] = coords[0][2], coords[-1][2]
    GRAPH_CACHE.write_bytes(pickle.dumps(graph))
    return graph


def main() -> None:
    graph = build_graph()
    print(f"graph: {graph.number_of_nodes():,} nodes, {graph.number_of_edges():,} directed edges")
    largest = max(nx.weakly_connected_components(graph), key=len)
    print(f"largest connected component: {len(largest) / graph.number_of_nodes():.1%} of nodes")

    node_ids = list(graph.nodes)
    node_xy = np.array([(k[0] * NODE_PRECISION_M, k[1] * NODE_PRECISION_M) for k in node_ids])
    tree = STRtree(points(node_xy))
    huts = {h["display_name"]: h for h in json.loads((ROOT / "data/raw/sac/huts.json").read_text())}

    def snap(name: str) -> tuple[tuple[int, int], float]:
        xy = huts[name]["geom"]["coordinates"]
        i = int(tree.nearest(points(xy)))
        return node_ids[i], float(np.hypot(*(node_xy[i] - xy)))

    rows = []
    for src, dst, route_id in PAIRS:
        (a, snap_a), (b, snap_b) = snap(src), snap(dst)
        sac = next(r for r in huts[dst]["routes"] if r["id"] == route_id)
        row = {"from": src, "to": dst, "sac_grade": sac["main_difficulty"], "sac_time_max": sac["ascent_time_max"],
               "sac_ascent_m": sac["ascent_altitude"], "snap_m": (round(snap_a), round(snap_b))}
        try:
            path = nx.shortest_path(graph, a, b, weight="time")
        except nx.NetworkXNoPath:
            rows.append(row | {"error": "no path"})
            continue
        edges = [graph[u][v] for u, v in zip(path, path[1:])]
        row |= {
            "distance_m": sum(e["length"] for e in edges),
            "ascent_m": sum(e["up"] for e in edges),
            "descent_m": sum(e["down"] for e in edges),
            "swiss_time_min": sum(e["time"] for e in edges),
            "max_grade": max(e["grade"] for e in edges),
        }
        row["din_time_min"] = din_time_min(row["distance_m"], row["ascent_m"], row["descent_m"])
        rows.append(row)

    print(f"{'from → to':58} {'SAC':>4} {'grade':>5} | {'km':>5} {'up':>5} {'SACup':>5} {'CH':>4} {'DIN':>4} "
          f"{'TLM':>6} {'snap m':>10}")
    for r in rows:
        name = f"{r['from'][:27]} → {r['to'][:27]}"
        if "error" in r:
            print(f"{name:58} ERROR {r['error']} (snap {r['snap_m']})")
            continue
        print(f"{name:58} {r['sac_time_max']:>4} {r['sac_grade']:>5} | {r['distance_m'] / 1000:5.1f} "
              f"{r['ascent_m']:5.0f} {r['sac_ascent_m'] or 0:5} {r['swiss_time_min']:4.0f} {r['din_time_min']:4.0f} "
              f"{GRADE_LABEL[r['max_grade']]:>6} {str(r['snap_m']):>10}")

    ok = [r for r in rows if "error" not in r and r["sac_time_max"]]
    for key in ("swiss_time_min", "din_time_min"):
        ratios = [r[key] / r["sac_time_max"] for r in ok]
        print(f"{key}/SAC: median {statistics.median(ratios):.2f}, "
              f"range {min(ratios):.2f}–{max(ratios):.2f} (n={len(ratios)})")
    (ROOT / "data/raw/tlm_vs_sac.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
