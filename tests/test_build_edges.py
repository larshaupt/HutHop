"""Tests for the route graph build on a tiny synthetic trail network.

    A ──yellow── J ──yellow── B ──yellow── C
     ╲________ blue (direct) ________╱

Huts at A and B, a stop at J (on the yellow detour) and a stop at C (beyond B).
All flat at 1000 m, so times follow the flat pace of 4.2 km/h (14.271 min/km).
"""

import numpy as np
import pytest
import shapely
from scipy.sparse.csgraph import dijkstra

from app.ingest.build_edges import network_from_lines, search, snap, store
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


def test_closest_stop_per_line(network, places):
    places[2]["lines"], places[3]["lines"] = {"bus 1"}, {"bus 1"}  # J and C on the same bus line
    edges = edges_by_key(search(network, places))
    assert (1, 10, 1) in edges and (1, 11, 1) not in edges  # from A: only J, the closer stop of the line
    assert (2, 11, 1) in edges and (2, 10, 1) not in edges  # from B, C is the closer one


def test_stop_with_another_line_kept_and_unserved_dropped(network, places):
    places[2]["lines"], places[3]["lines"] = {"bus 1"}, {"bus 1", "train"}
    edges = edges_by_key(search(network, places))
    assert (1, 11, 1) in edges  # C also has the train: kept although further than J
    places[2]["lines"] = set()  # J has no summer service
    edges = edges_by_key(search(network, places))
    assert (1, 10, 1) not in edges and (10, 1, 1) not in edges


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


def test_values_roundtrip():
    heights = [1520, 1523, 1498, 2950, 2950, 0]
    assert polyline.decode_values(polyline.encode_values(heights)) == heights


def test_elevation_profile_per_point(network, places):
    edges = edges_by_key(search(network, places))
    e = edges[(1, 2, 1)]
    assert polyline.decode_values(e["elevation"]) == [1000, 1000, 1000]  # one height per route point


def test_added_column_on_old_database(tmp_path):
    from app.db import connect

    old = connect(tmp_path / "old.db")
    old.execute("ALTER TABLE edge DROP COLUMN elevation")  # as deployed before profiles
    old.close()
    conn = connect(tmp_path / "old.db")
    assert "elevation" in {r["name"] for r in conn.execute("PRAGMA table_info(edge)")}


def test_footprints_stored_and_backfilled(network, places, tmp_path):
    from app.db import connect
    from app.ingest import footprints
    from app.planner.geometry import footprint, unpack

    edges = search(network, places)
    cells, samples = unpack(edges[0]["footprint"], edges[0]["samples"])
    expected_cells, expected_samples = footprint(edges[0]["geometry"])
    assert (cells == expected_cells).all() and (samples == expected_samples).all()

    conn = connect(tmp_path / "test.db")  # an edge stored before footprints existed
    for kind in ("hut", "hut"):
        conn.execute("INSERT INTO place (kind, name, e_lv95, n_lv95, lat, lon) VALUES (?, 'p', 0, 0, 46, 8)", (kind,))
    conn.execute("INSERT INTO edge (from_place, to_place, category_limit, max_category, time_min, source, geometry) "
                 "VALUES (1, 2, 1, 1, 60, 'tlm_routed', ?)", (edges[0]["geometry"],))
    assert footprints.fill(conn) == 1 and footprints.fill(conn) == 0
    row = conn.execute("SELECT footprint, samples FROM edge").fetchone()
    assert (unpack(row["footprint"], row["samples"])[0] == expected_cells).all()


def test_exclusion_kept_when_its_limit_variant_is_gone(network, places, tmp_path):
    from app.db import connect

    conn = connect(tmp_path / "test.db")
    for p in places:
        conn.execute("INSERT INTO place (id, kind, name, e_lv95, n_lv95, lat, lon) VALUES (?, ?, 'p', ?, ?, 46, 8)",
                     (p["id"], p["kind"], *p["xy"]))
    edges = search(network, places)
    store(conn, places, edges, network)
    conn.execute("UPDATE edge SET excluded = 1, note = 'path closed' "
                 "WHERE from_place = 1 AND to_place = 2 AND category_limit = 3")
    store(conn, places, [e for e in edges if (e["from_place"], e["to_place"], e["category_limit"]) != (1, 2, 3)],
          network)  # the rebuild only finds the yellow route between the two huts
    rows = conn.execute("SELECT category_limit, excluded, note FROM edge WHERE from_place = 1 AND to_place = 2")
    assert [tuple(r) for r in rows] == [(1, 1, "path closed")]
    assert not conn.execute("SELECT 1 FROM edge WHERE from_place = 2 AND to_place = 1 AND excluded = 1").fetchone()


def test_osm_trails_replace_tlm_abroad_and_are_stitched_at_the_border():
    """A Swiss trail running east across the border at E 2 610 000 and an OpenStreetMap trail abroad that ends 20 m
    off it: the Swiss trail is cut at the border, and a connector joins the two."""
    from app.ingest.build_edges import merge_osm

    abroad = shapely.box(2_610_000, 1_190_000, 2_630_000, 1_210_000)
    tlm = [line((2_605_000, 1_200_000), (2_608_000, 1_200_000), (2_615_000, 1_200_000)),  # crosses into the region
           line((2_620_000, 1_205_000), (2_625_000, 1_205_000))]   # wholly abroad: dropped
    osm = [line((2_610_000, 1_200_020), (2_612_000, 1_200_020)),   # starts at the border, 20 m north
           line((2_610_000, 1_203_000), (2_611_000, 1_203_000))]   # no Swiss trail within 50 m
    lines, categories = merge_osm(tlm, [1, 1], osm, [2, 4], abroad)
    assert len(lines) == 4 and categories == [1, 2, 4, 2]
    assert lines[0][-1].tolist() == [2_610_000, 1_200_000, 1000]  # cut at the border
    assert lines[3].tolist() == [[2_610_000, 1_200_000, 1000], [2_610_000, 1_200_020, 1000]]  # the connector
    network = network_from_lines(lines, categories)
    nodes, _ = snap(network, np.array([(2_605_000, 1_200_000), (2_612_000, 1_200_020)]))
    dist = dijkstra(network.edges[2].graph, indices=nodes[0])
    assert dist[nodes[1]] == pytest.approx((5000 + 20 + 2000) * hiking_time.FLAT_MIN_PER_KM / 1000, abs=0.1)
    assert np.isinf(dijkstra(network.edges[1].graph, indices=nodes[0])[nodes[1]])  # red-white beyond the border


def test_stitch_joins_nodes_on_the_border():
    """Ends and junctions on the border (x = 0) are joined to the other network's nearest node within 50 m."""
    from app.ingest.build_edges import stitch

    border = shapely.LineString([(0, -1000), (0, 1000)])
    swiss = [line((-500, 0), (0, 0)),
             line((-500, 100), (0, 100)), line((0, 100), (-300, 400)),  # a junction on the border (Bettlerjoch)
             line((-500, 300), (-10, 300)),  # ends 10 m before the border: not on it
             line((-500, 500), (0, 500))]  # the other network has a node at the same point already
    other = [line((0, 30), (500, 30)),  # 30 m along the border from the Swiss end
             line((3, 104), (500, 104)), line((3, 104), (300, 400)),  # its junction, 5 m from the Swiss one
             line((2, 300), (500, 300)),
             line((0.2, 500), (500, 500)),
             line((0, -60), (500, -60))]  # 60 m from the nearest Swiss node: too far
    connectors, categories = stitch(swiss, [2, 1, 3, 1, 1], other, [3, 2, 1, 1, 1, 1], border)
    assert [c[:, :2].tolist() for c in connectors] == [[[0, 0], [0, 30]], [[0, 100], [3, 104]]]
    assert categories == [3, 1]  # the harder of the two ends; at a junction, the easiest trail there


def test_without_osm_trails_the_network_is_swisstlm3d_only(monkeypatch):
    from app.ingest import build_edges

    monkeypatch.setattr(build_edges.osm_trails, "load", lambda: ([], [], None))
    monkeypatch.setattr(build_edges.osm_trails, "read_lines", lambda *a, **k: (
        [line(A, J), line(J, B)], np.array(["Wanderweg", "x"])))
    network = build_edges.load_network()
    assert len(network.lines) == 2 and len(network.node_xy) == 3
