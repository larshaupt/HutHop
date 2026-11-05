"""OSM access paths for huts far from the signposted trails: path finding and how build_edges uses them.

    far hut H ── OSM path (T5) ── X ── 20 m ── end of the trail at A ──yellow── B
                                   ╲── dead end
"""

import numpy as np
import pytest
from pyproj import Transformer

from app.ingest.build_edges import OSM_CATEGORY, attach_access, network_from_lines, search, store, with_access
from app.ingest.osm_access import access_paths

TO_WGS84 = Transformer.from_crs("EPSG:2056", "EPSG:4326", always_xy=True)
A, B = (2600000.0, 1200000.0), (2602000.0, 1200000.0)
H, M, X, DEAD = (2600000.0, 1203000.0), (2600000.0, 1201500.0), (2600000.0, 1200020.0), (2601000.0, 1201500.0)


def osm(*ways):
    """Overpass-style elements: nodes 1… at the given LV95 points, ways as (node ids, tags)."""
    pts = [H, M, X, DEAD]
    nodes = [{"type": "node", "id": i, "lon": lon, "lat": lat}
             for i, (lon, lat) in enumerate(zip(*TO_WGS84.transform(*np.array(pts).T)), 1)]
    return nodes + [{"type": "way", "id": 100 + i, "nodes": nd, "tags": tags} for i, (nd, tags) in enumerate(ways)]


def test_shortest_path_to_a_trail_end_with_its_grade():
    elements = osm(([1, 2], {"highway": "path", "sac_scale": "demanding_alpine_hiking"}),
                   ([2, 3], {"highway": "path"}), ([2, 4], {"highway": "path"}))
    paths = access_paths(elements, [(7, H[0] + 40, H[1])], np.array([A, B]))
    path = paths[7]
    assert path["grade"] == "T5"
    assert path["xy"][0] == pytest.approx((H[0] + 40, H[1]))  # starts at the hut itself
    assert path["xy"][-1] == pytest.approx(X, abs=0.01)  # ends next to the trail end at A, not at the dead end
    assert len(path["xy"]) == 4  # hut, H, M, X


def test_no_path_when_too_far_closed_or_not_reaching_a_trail():
    reaching = ([1, 2, 3], {"highway": "path"})
    assert not access_paths(osm(reaching), [(7, H[0] + 400, H[1])], np.array([A]))  # hut 400 m from any path
    assert not access_paths(osm(([1, 2, 3], {"highway": "path", "access": "no"})), [(7, *H)], np.array([A]))
    assert not access_paths(osm(([1, 2, 4], {"highway": "path"})), [(7, *H)], np.array([A]))  # only the dead end


def test_far_hut_connected_only_with_the_unofficial_limit(tmp_path):
    from app.db import connect

    trail = [np.array([(*A, 1000.0), (*B, 1000.0)])]
    access = {3: {"coords": np.array([(*H, 1000.0), (*M, 1000.0), (*X, 1000.0)]), "grade": "T5"}}
    network = network_from_lines(*with_access(trail, [1], [a["coords"] for a in access.values()]))
    places = [{"id": 1, "kind": "hut", "e_lv95": A[0], "n_lv95": A[1]},
              {"id": 2, "kind": "hut", "e_lv95": B[0], "n_lv95": B[1]},
              {"id": 3, "kind": "hut", "e_lv95": H[0], "n_lv95": H[1]},  # 3 km off the trail
              {"id": 10, "kind": "stop", "e_lv95": H[0] + 100, "n_lv95": H[1]}]  # next to H, but no trail
    attach_access(network, places, access)
    hut, stop = places[2], places[3]
    assert hut["snap_m"] == 0 and hut["trail_snap_m"] == pytest.approx(3000)
    assert stop["snap_m"] == pytest.approx(np.hypot(100, 3000))  # stops don't snap onto an access path

    edges = {(e["from_place"], e["to_place"], e["category_limit"]): e for e in search(network, places)}
    assert (1, 2, 1) in edges and (1, 2, OSM_CATEGORY) not in edges  # unchanged between the trail huts
    e = edges[(3, 2, OSM_CATEGORY)]
    assert e["max_category"] == OSM_CATEGORY
    assert e["distance_m"] == pytest.approx(3000 + 2000, abs=2)  # access path, 20 m to A, then the trail
    assert not any(k[2] < OSM_CATEGORY and 3 in k[:2] for k in edges)

    conn = connect(tmp_path / "test.db")
    for p in places:
        conn.execute("INSERT INTO place (id, kind, name, e_lv95, n_lv95, lat, lon) VALUES (?, ?, 'p', ?, ?, 46, 8)",
                     (p["id"], p["kind"], p["e_lv95"], p["n_lv95"]))
    store(conn, places, list(edges.values()), network)
    # the distance to the signposted trails is kept, so osm_access still sees the hut as far
    assert conn.execute("SELECT snap_m FROM place WHERE id = 3").fetchone()[0] == pytest.approx(3000)
