"""Tests for the route graph build on a tiny synthetic trail network.

    A ──yellow── J ──yellow── B ──yellow── C
     ╲________ blue (direct) ________╱

Huts at A and B, a stop at J (on the yellow detour) and a stop at C (beyond B).
All flat at 1000 m, so times follow the flat pace of 4.2 km/h (14.271 min/km).
"""

import numpy as np
import pytest

from app.ingest.build_edges import network_from_lines, search, snap
from app.planner import hiking_time, polyline

A, J, B, C = (2600000.0, 1200000.0), (2601000.0, 1200800.0), (2602000.0, 1200000.0), (2603000.0, 1200000.0)


def line(*pts, z=1000.0):
    return np.array([(x, y, z) for x, y in pts])


@pytest.fixture
def network():
    lines = [line(A, J), line(J, B), line(B, C), line(A, (2601000.0, 1200000.0), B)]
    return network_from_lines(lines, [1, 1, 1, 3])


@pytest.fixture
def places(network):
    places = [
        {"id": 1, "kind": "hut", "xy": A}, {"id": 2, "kind": "hut", "xy": B},
        {"id": 10, "kind": "stop", "xy": (J[0] + 30, J[1])}, {"id": 11, "kind": "stop", "xy": C},
    ]
    nodes, dists = snap(network, np.array([p["xy"] for p in places]))
    for p, node, d in zip(places, nodes, dists):
        p["node"], p["snap_m"] = int(node), float(d)
    return places


def edges_by_key(edges):
    return {(e["from_place"], e["to_place"], e["category_limit"]): e for e in edges}


def test_line_metrics_flat_and_uphill():
    flat = hiking_time.line_metrics(line((0, 0), (1000, 0)))
    assert flat["length"] == pytest.approx(1000)
    assert flat["t_fwd"] == pytest.approx(14.271, abs=0.01)
    up = hiking_time.line_metrics(np.array([(0, 0, 0), (1000, 0, 200)]))
    assert up["up"] == pytest.approx(200) and up["down"] == 0
    assert up["t_fwd"] > up["t_back"] > flat["t_fwd"] * 0.9


def test_polyline_roundtrip():
    pts = [(46.5, 7.9), (46.51234, 7.91234), (46.4, 8.0)]
    assert polyline.decode(polyline.encode(pts)) == pytest.approx(pts)


def test_snap_distance(places):
    assert places[2]["snap_m"] == pytest.approx(30)
    assert places[0]["snap_m"] == 0


def test_category_limits(network, places):
    edges = edges_by_key(search(network, places))
    yellow = edges[(1, 2, 1)]
    assert yellow["max_category"] == 1
    assert yellow["distance_m"] == pytest.approx(2 * np.hypot(1000, 800), abs=2)
    blue = edges[(1, 2, 3)]
    assert blue["max_category"] == 3 and blue["time_min"] < yellow["time_min"]
    assert blue["time_min"] == pytest.approx(2 * 14.271, abs=1)
    assert (1, 2, 2) not in edges  # same path as yellow, not stored again


def test_stop_edges_both_directions_no_stop_to_stop(network, places):
    edges = edges_by_key(search(network, places))
    assert (1, 10, 1) in edges and (10, 1, 1) in edges   # hut A ↔ stop J
    assert (1, 11, 1) in edges                            # A → C on yellow passes stop J; still kept
    assert (2, 11, 1) in edges and (11, 2, 1) in edges   # hut B ↔ stop C
    assert not any(e["from_place"] >= 10 and e["to_place"] >= 10 for e in edges.values())  # no stop→stop


def test_only_fastest_stops_per_hut(network, places, monkeypatch):
    monkeypatch.setattr("app.ingest.build_edges.STOPS_PER_HUT", 1)
    edges = edges_by_key(search(network, places))
    from_a = {to for (frm, to, lim) in edges if frm == 1 and to >= 10 and lim == 1}
    assert from_a == {10}  # J is closer to A than C


def test_snap_time_added_and_cutoff(network, places):
    edges = edges_by_key(search(network, places))
    assert edges[(1, 10, 1)]["distance_m"] == pytest.approx(np.hypot(1000, 800) + 30, abs=2)
    assert not search(network, places, cutoff=10)  # nothing within 10 minutes


def test_geometry_decodes_to_path(network, places):
    edges = edges_by_key(search(network, places))
    pts = polyline.decode(edges[(1, 2, 1)]["geometry"])
    assert len(pts) == 3  # A, J, B


def test_huts_far_from_trails_get_no_edges(network, places):
    places = places + [{"id": 3, "kind": "hut", "node": places[0]["node"], "snap_m": 2000.0}]
    edges = search(network, places)
    assert not any(3 in (e["from_place"], e["to_place"]) for e in edges)
