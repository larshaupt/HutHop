"""Tests for the OpenStreetMap trail extraction: categories, splitting at junctions, clipping (no download)."""

import numpy as np
import osmium
import pyogrio.raw
import pytest
import shapely

from app.ingest import osm_trails


@pytest.mark.parametrize("tags, in_route, expected", [
    ({"highway": "path", "sac_scale": "hiking"}, True, 1),
    ({"highway": "path", "sac_scale": "mountain_hiking"}, True, 2),
    ({"highway": "path", "sac_scale": "demanding_mountain_hiking"}, False, 2),  # graded path: signposted
    ({"highway": "path", "sac_scale": "alpine_hiking"}, True, 3),
    ({"highway": "path", "sac_scale": "difficult_alpine_hiking"}, False, 3),
    ({"highway": "path"}, True, 2),
    ({"highway": "residential"}, True, 1),  # routes through villages
    ({"highway": "track"}, True, 1),
    ({"highway": "track", "sac_scale": "hiking"}, False, 1),  # graded hut access track
    ({"highway": "path"}, False, 4),  # unofficial
    ({"highway": "track"}, False, None),
    ({"highway": "residential", "sac_scale": "hiking"}, False, None),
    ({"highway": "via_ferrata"}, True, 3),
    ({"highway": "via_ferrata"}, False, None),
    ({"highway": "path", "access": "private"}, False, None),
    ({"highway": "path", "access": "no", "foot": "yes"}, True, 2),
    ({"highway": "path", "foot": "no"}, True, None),
    ({"highway": "motorway"}, True, None),
    ({"building": "yes"}, True, None),
])
def test_category(tags, in_route, expected):
    assert osm_trails.category(tags, in_route) == expected


def test_split_at_shared_nodes_and_closed_ways():
    ways = [np.array([1, 2, 3, 4, 5]), np.array([10, 3, 11]), np.array([20, 21, 22, 20]), np.array([5, 30])]
    pieces = osm_trails.split_at_junctions(ways)
    assert [p for p in pieces if p[0] == 0] == [(0, 0, 2), (0, 2, 4)]  # at node 3; node 5 is already an end
    assert [p for p in pieces if p[0] == 1] == [(1, 0, 1), (1, 1, 2)]
    assert [p for p in pieces if p[0] == 2] == [(2, 0, 2), (2, 2, 3)]  # a loop is cut in two
    assert [p for p in pieces if p[0] == 3] == [(3, 0, 1)]


def test_clip_keeps_heights_and_whole_lines():
    area = shapely.box(0, -10, 100, 10)
    lines = np.array([shapely.LineString([(-50, 0, 100), (50, 0, 200)]),   # crosses the edge at x=0
                      shapely.LineString([(10, 0, 0), (20, 0, 0)]),         # inside
                      shapely.LineString([(200, 0, 0), (300, 0, 0)])])      # outside
    index, inside = osm_trails.clip(lines, area, inside=True)
    assert index.tolist() == [0, 1]
    assert shapely.get_coordinates(inside[0], include_z=True).tolist() == [[0, 0, 150], [50, 0, 200]]
    assert inside[1] is lines[1]
    index, outside = osm_trails.clip(lines, area, inside=False)
    assert index.tolist() == [0, 2]
    assert shapely.get_coordinates(outside[0], include_z=True).tolist() == [[-50, 0, 100], [0, 0, 150]]


def test_smooth_heights_keeps_ends_and_removes_noise():
    xy = np.column_stack([np.arange(0, 401, 20.0), np.zeros(21)])
    z = 1000 + xy[:, 0] * 0.2 + np.where(np.arange(21) % 2, 5.0, -5.0)  # 20 % slope with ±5 m noise
    out = osm_trails.smooth_heights(xy, z)
    assert out[0] == z[0] and out[-1] == z[-1]
    assert np.abs(out[3:-3] - (1000 + xy[3:-3, 0] * 0.2)).max() < 1.0


def test_extract_small_file(tmp_path, monkeypatch):
    """A route over a path and a village road, an unofficial path joining it mid-way, a private path, a track to a
    hut and one far from it;
    the part west of E 2 610 000 is "Switzerland" and left to swissTLM3D."""
    from pyproj import Transformer

    to_wgs84 = Transformer.from_crs("EPSG:2056", "EPSG:4326", always_xy=True)
    pts = {1: (2_609_000, 1_200_000), 2: (2_611_000, 1_200_000), 3: (2_612_000, 1_200_000), 4: (2_613_000, 1_200_000),
           5: (2_612_000, 1_201_000), 6: (2_614_000, 1_200_000), 7: (2_614_000, 1_201_000),
           8: (2_614_050, 1_201_050), 9: (2_620_000, 1_205_000), 10: (2_621_000, 1_205_000)}
    pbf = tmp_path / "x.osm.pbf"
    with osmium.SimpleWriter(str(pbf)) as w:
        for i, (e, n) in pts.items():
            tags = {"tourism": "alpine_hut"} if i == 8 else {}
            w.add_node(osmium.osm.mutable.Node(id=i, location=to_wgs84.transform(e, n), tags=tags))
        w.add_way(osmium.osm.mutable.Way(id=10, nodes=[1, 2, 3], tags={"highway": "path"}))
        w.add_way(osmium.osm.mutable.Way(id=11, nodes=[3, 4], tags={"highway": "residential"}))
        w.add_way(osmium.osm.mutable.Way(id=12, nodes=[5, 2], tags={"highway": "path"}))
        w.add_way(osmium.osm.mutable.Way(id=13, nodes=[4, 6], tags={"highway": "path", "access": "private"}))
        w.add_way(osmium.osm.mutable.Way(id=14, nodes=[6, 7], tags={"highway": "track"}))
        w.add_way(osmium.osm.mutable.Way(id=15, nodes=[9, 10], tags={"highway": "track"}))  # far from the hut
        w.add_relation(osmium.osm.mutable.Relation(id=20, members=[("w", 10, ""), ("w", 11, "")],
                                                   tags={"type": "route", "route": "hiking"}))
    monkeypatch.setattr(osm_trails, "pbf_path", lambda code: pbf)
    monkeypatch.setattr(osm_trails, "trails_path", lambda code: tmp_path / "trails.gpkg")
    monkeypatch.setattr(osm_trails.countries, "region_border_path", lambda code: tmp_path / "border.json")
    monkeypatch.setattr(osm_trails.regions, "area",
                        lambda code: shapely.box(2_600_000, 1_190_000, 2_630_000, 1_210_000))
    monkeypatch.setattr(osm_trails, "swiss_area", lambda: shapely.box(2_590_000, 1_190_000, 2_610_000, 1_210_000))
    monkeypatch.setattr(osm_trails.dem, "heights", lambda e, n: np.full(len(e), 1500.0))

    # km: road 3–4 and the hut's access track 6–7; path 2–3 and border–2; spur 5–2
    assert osm_trails.extract("AT") == {1: 2, 2: 2, 3: 0, 4: 1}
    _, _, geoms, (cats, ids) = pyogrio.raw.read(tmp_path / "trails.gpkg", columns=["category", "osm_id"])
    got = sorted((int(i), int(c), shapely.get_coordinates(g, include_z=True)[[0, -1]].round().tolist())
                 for i, c, g in zip(ids, cats, shapely.from_wkb(geoms)))
    assert got == [(10, 2, [[2_610_000, 1_200_000, 1500], [2_611_000, 1_200_000, 1500]]),  # clipped, split at 2
                   (10, 2, [[2_611_000, 1_200_000, 1500], [2_612_000, 1_200_000, 1500]]),
                   (11, 1, [[2_612_000, 1_200_000, 1500], [2_613_000, 1_200_000, 1500]]),
                   (12, 4, [[2_612_000, 1_201_000, 1500], [2_611_000, 1_200_000, 1500]]),
                   (14, 1, [[2_614_000, 1_200_000, 1500], [2_614_000, 1_201_000, 1500]])]
