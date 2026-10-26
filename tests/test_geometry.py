"""Tests for tour geometry (retracing, shape), the new sub-scores and result-list variety."""

from datetime import date

import pytest

from app.planner import polyline
from app.planner.geometry import M_PER_DEG_LAT, M_PER_DEG_LON, Footprints
from app.planner.graph import Graph, Hut, Leg, Stop
from app.planner.ranking import metrics
from app.planner.tours import MAX_TOURS_PER_HUT, Night, Tour, TourRequest, diversify, suggest

LAT0, LON0 = 46.6, 8.0


def line(*xy_km):
    """Encoded route line through points given in km east/north of a fixed origin."""
    return polyline.encode([(LAT0 + y * 1000 / M_PER_DEG_LAT, LON0 + x * 1000 / M_PER_DEG_LON) for x, y in xy_km])


def footprints(**lines):
    return Footprints({int(k[1:]): v for k, v in lines.items()})


def test_out_and_back_is_retraced_and_badly_shaped():
    fp = footprints(e1=line((0, 0), (3, 0)), e2=line((3, 0), (0, 0)))
    assert fp.repeated([1], 2) == pytest.approx(1.0, abs=0.05)
    assert fp.retraced([1, 2]) == pytest.approx(0.5, abs=0.03)  # half of all walked trail is walked twice
    assert fp.shape([1, 2]) < 0.1


def test_traverse_is_fresh_and_well_shaped():
    fp = footprints(e1=line((0, 0), (3, 0.5)), e2=line((3, 0.5), (6, 0)))
    assert fp.retraced([1, 2]) < 0.03  # only the shared hut point
    assert fp.shape([1, 2]) == 1.0


def test_loop_scores_by_roundness():
    square = footprints(e1=line((0, 0), (3, 0)), e2=line((3, 0), (3, 3)), e3=line((3, 3), (0, 3)), e4=line((0, 3), (0, 0)))
    assert square.retraced([1, 2, 3, 4]) < 0.03
    assert square.shape([1, 2, 3, 4]) == 1.0
    sliver = footprints(e1=line((0, 0), (6, 0.2)), e2=line((6, 0.2), (0, 0.4)))  # out and back on parallel trails
    assert sliver.retraced([1, 2]) < 0.03 and sliver.shape([1, 2]) < 0.3


def test_zigzag_scores_lower_than_traverse():
    zigzag = footprints(e1=line((0, 0), (3, 3)), e2=line((3, 3), (0, 3.5)), e3=line((0, 3.5), (3, 6)))
    straight = footprints(e1=line((0, 0), (2, 0)), e2=line((2, 0), (4, 0)), e3=line((4, 0), (6, 0)))
    assert zigzag.shape([1, 2, 3]) < straight.shape([1, 2, 3]) == 1.0


def test_edges_without_geometry_count_as_fresh():
    fp = Footprints({})
    assert fp.retraced([1, 2]) == 0.0 and fp.repeated([1], 2) == 0.0


def leg(edge_id, frm, to, minutes):
    return Leg(edge_id, frm, to, minutes, minutes * 50, 300, 300, 1, None, 1)


def test_metrics_fresh_and_even():
    nights = [Night(1, date(2027, 7, 10), "catered", False)]
    # Even days counts the days between huts; short days from and to public transport don't make a tour uneven
    even = metrics([leg(1, 0, 1, 60), leg(2, 1, 2, 270), leg(3, 2, 3, 270), leg(4, 3, 0, 90)], nights * 3, [2000] * 3,
                   180, 360, retraced=0.0, shape=1.0)
    uneven = metrics([leg(1, 0, 1, 60), leg(2, 1, 2, 180), leg(3, 2, 3, 360), leg(4, 3, 0, 60)], nights * 3,
                     [2000] * 3, 180, 360, retraced=0.3, shape=0.4)
    assert even.even == 1.0 and even.fresh == 1.0 and even.shape == 1.0
    assert uneven.even == pytest.approx(1 - 2 / 3) and uneven.fresh == pytest.approx(0.4) and uneven.shape == 0.4
    one_night = metrics([leg(1, 0, 1, 60), leg(2, 1, 0, 300)], nights, [2000], 180, 360)
    assert one_night.even == 1.0  # no days between huts


def test_search_avoids_retracing():
    """Stop S → hut A → back to S on the same trail, or on to stop T on a new trail: same time, the new trail wins."""
    huts = {1: Hut(1, "A", LAT0, LON0, 2_600_000, 1_200_000, "0" * 12, "2" * 12, True)}
    stops = {10: Stop(10, "S", LAT0, LON0, 2_600_000, 1_200_000, "bus"),
             11: Stop(11, "T", LAT0, LON0, 2_606_000, 1_200_000, "bus")}
    g = Graph(huts, stops, footprints=footprints(e1=line((0, 0), (3, 0)), e2=line((3, 0), (0, 0)),
                                                 e3=line((3, 0), (6, 0))))
    g.stop_in = {1: [leg(1, 10, 1, 240)]}
    g.stop_out = {1: [leg(2, 1, 10, 240), leg(3, 1, 11, 240)]}
    tours = suggest(g, TourRequest(date(2027, 7, 10), 1, 3, 6))
    assert tours[0].legs[-1].to == 11 and tours[0].metrics.fresh > 0.95  # only the hut point is shared
    assert all(t.metrics.fresh < 0.1 for t in tours[1:])  # the retracing tour ranks last


def tour(huts, score):
    nights = tuple(Night(h, date(2027, 7, 10), "catered", False) for h in huts)
    return Tour(score, (), nights, date(2027, 7, 10))


def test_diversify_limits_tours_per_hut():
    # Hut 1 is in every candidate; only MAX_TOURS_PER_HUT of them make the list while enough others exist.
    with_1 = [tour((1, 100 + i), 1 - i / 100) for i in range(6)]
    others = [tour((200 + 2 * i, 201 + 2 * i), 0.5 - i / 100) for i in range(10)]
    chosen = diversify(with_1 + others, 12)
    assert sum(1 for t in chosen if 1 in {n.hut for n in t.nights}) == MAX_TOURS_PER_HUT
    assert len(chosen) == MAX_TOURS_PER_HUT + 9


def test_diversify_lifts_hut_cap_in_small_areas():
    with_1 = [tour((1, 100 + i), 1 - i / 100) for i in range(6)]
    assert len(diversify(with_1, 10)) == 6  # fewer than MIN_RESULTS otherwise


def test_info_endpoint_lists_weights_and_impressum(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import config
    from app.main import app

    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "huthop.toml")
    (tmp_path / "huthop.toml").write_text('[ranking.weights]\nfresh = 5\n[impressum]\nname = "Someone"\ncontact = ""\n')
    data = TestClient(app).get("/api/info").json()
    fresh = next(m for m in data["metrics"] if m["key"] == "fresh")
    assert fresh["weight"] == 5 and 0 < fresh["share"] < 1 and fresh["label"]
    assert data["impressum"] == {"name": "Someone"}  # empty fields are dropped


def test_overlap_cache_is_bounded(monkeypatch):
    monkeypatch.setattr("app.planner.geometry.MAX_CACHED_PAIRS", 3)
    fp = footprints(**{f"e{i}": line((i, 0), (i + 1, 0)) for i in range(1, 7)})
    for b in range(2, 7):
        fp.overlap(1, b)
    assert len(fp._overlap) <= 3
    assert fp.overlap(1, 2) == fp.overlap(2, 1)  # recomputed after the cache started over


def reference_shape(fp, edge_ids):
    """shape() as computed before the per-edge parts: on all samples joined together (numpy)."""
    import math

    import numpy as np

    from app.planner import geometry as g

    pts = np.concatenate([g.footprint(fp.geometry[e])[1] for e in edge_ids])
    length = float(np.hypot(*np.diff(pts, axis=0).T).sum())
    progress = float(np.hypot(*(pts[-1] - pts[0]))) / length
    closed = np.vstack((pts, pts[:1]))
    area = 0.5 * abs(float(np.dot(closed[:-1, 0], closed[1:, 1]) - np.dot(closed[1:, 0], closed[:-1, 1])))
    roundness = 4 * math.pi * area / (length + float(np.hypot(*(pts[-1] - pts[0])))) ** 2
    loop = max(0.0, 1 - progress / g.LOOP_PROGRESS)
    return max(0.0, min(1.0, max(progress / g.PROGRESS_GOOD, loop * roundness / g.ROUNDNESS_GOOD)))


def test_shape_from_parts_matches_joined_samples():
    fp = footprints(e1=line((0, 0), (2, 1), (3, 3)), e2=line((3, 3), (1, 4)), e3=line((1, 4), (0.5, 1.5)),
                    e4=line((0.5, 1.5), (4, 0.2)))
    for ids in ([1, 2], [1, 2, 3], [2, 3, 4], [1, 2, 3, 4]):
        # Same formula, other summation order: rounding differs at ~1e-10 (coordinates are ~5000 km)
        assert fp.shape(ids) == pytest.approx(reference_shape(fp, ids), abs=1e-8)


def test_packed_footprint_roundtrip_and_same_overlaps():
    from app.planner.geometry import footprint, pack, unpack

    lines = {1: line((0, 0), (3, 0), (3, 2)), 2: line((3, 2), (3, 0), (5, -1)), 3: line((5, -1), (8, 0))}
    decoded = Footprints(lines)
    packed = Footprints({}, {e: pack(*footprint(g)) for e, g in lines.items()})
    for e, g in lines.items():
        cells, samples = unpack(*pack(*footprint(g)))
        assert (cells == decoded.cells(e)).all() and (samples == footprint(g)[1]).all()
        assert cells.dtype.name == "int32" and (cells[1:] > cells[:-1]).all()  # sorted, unique
    for a, b in ((1, 2), (1, 3), (2, 3)):
        assert packed.overlap(a, b) == decoded.overlap(a, b)
    assert packed.overlap(1, 2) > 0 and packed.overlap(1, 3) == 0  # 1 and 3 are far apart: coarse check


def test_search_retraced_matches_rebuilt_tour():
    """The search sums retracing day by day; a tour rebuilt from its id computes it from scratch."""
    from app.planner.tours import build_tour, parse_tour_id

    huts = {1: Hut(1, "A", LAT0, LON0, 2_600_000, 1_200_000, "0" * 12, "2" * 12, True),
            2: Hut(2, "B", LAT0, LON0, 2_603_000, 1_200_000, "0" * 12, "2" * 12, True)}
    stops = {10: Stop(10, "S", LAT0, LON0, 2_600_000, 1_200_000, "bus"),
             11: Stop(11, "T", LAT0, LON0, 2_606_000, 1_200_000, "bus")}
    # Day 2 walks back over a stretch of day 1, day 3 back over part of day 2: some retracing, not clipped to 0.
    g = Graph(huts, stops, footprints=footprints(e1=line((0, 0), (3, 0)), e2=line((3, 0), (2.5, 0), (2.5, 2)),
                                                 e3=line((2.5, 2), (2.5, 1.5), (6, 1.5))))
    g.stop_in = {1: [leg(1, 10, 1, 240)]}
    g.hut_out = {1: [leg(2, 1, 2, 240)]}
    g.stop_out = {2: [leg(3, 2, 10, 240)]}
    g.legs = {(x.frm, x.to, x.category_limit): x for x in (*g.stop_in[1], *g.hut_out[1], *g.stop_out[2])}
    [t] = suggest(g, TourRequest(date(2027, 7, 10), 2, 3, 6))
    assert 0 < t.metrics.fresh < 1
    rebuilt = build_tour(g, parse_tour_id(t.id))
    assert rebuilt.metrics.fresh == t.metrics.fresh and rebuilt.score == t.score
