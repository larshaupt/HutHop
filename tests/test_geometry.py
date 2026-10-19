"""Tests for tour geometry (retracing, shape), the new sub-scores and result-list variety."""

from datetime import date

import pytest

from app.planner import polyline
from app.planner.geometry import M_PER_DEG_LAT, M_PER_DEG_LON, Footprints
from app.planner.ranking import metrics
from app.planner.graph import Graph, Hut, Leg, Stop
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
    even = metrics([leg(1, 0, 1, 270), leg(2, 1, 0, 270)], nights, [2000], 180, 360, retraced=0.0, shape=1.0)
    uneven = metrics([leg(1, 0, 1, 180), leg(2, 1, 0, 360)], nights, [2000], 180, 360, retraced=0.3, shape=0.4)
    assert even.even == 1.0 and even.fresh == 1.0 and even.shape == 1.0
    assert uneven.even == pytest.approx(1 - 2 / 3) and uneven.fresh == pytest.approx(0.4) and uneven.shape == 0.4


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
