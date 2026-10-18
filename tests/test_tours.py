"""Tests for the tour search on a small hand-made place graph.

    stop S1 ──4h── hut A ──5h── hut B ──4h── stop S2
                     └───5h── hut C (self-service only, closes in August) ──4h── stop S3 (next to S1)
"""

from datetime import date

import pytest

from app.planner.tours import Graph, Hut, Leg, Stop, TourRequest, night_status, suggest

SUMMER = "222221000122"   # catered June (partly) to October (partly)
NONE = "2" * 12
SELF_JULY = "222222022222"  # self-service in July only


def hut(id, catering=SUMMER, self_service=NONE, lon=7.8):
    return Hut(id, f"Hut {id}", 46.5, lon, 2_600_000, 1_150_000, catering, self_service, True)


def stop(id, e):
    return Stop(id, f"Stop {id}", 46.5, 7.8, e, 1_150_000, "bus")


@pytest.fixture
def graph():
    huts = {1: hut(1), 2: hut(2), 3: hut(3, catering=NONE, self_service=SELF_JULY, lon=8.5)}
    stops = {11: stop(11, 2_600_000), 12: stop(12, 2_620_000), 13: stop(13, 2_601_000)}
    g = Graph(huts, stops)
    leg = lambda i, a, b, minutes: Leg(i, a, b, minutes, minutes * 50, 500, 400, 2, None, 2)
    g.stop_in = {1: [leg(1, 11, 1, 240)]}
    g.hut_out = {1: [leg(2, 1, 2, 300), leg(3, 1, 3, 300)], 2: [leg(4, 2, 1, 300)], 3: [leg(5, 3, 1, 300)]}
    g.stop_out = {2: [leg(6, 2, 12, 240)], 3: [leg(7, 3, 13, 240)], 1: [leg(8, 1, 11, 240)]}
    return g


def routes(tours):
    return [[leg.frm for leg in t.legs] + [t.legs[-1].to] for t in tours]


def test_two_nights_any_catering(graph):
    tours = suggest(graph, TourRequest(date(2027, 7, 10), 2, 3, 6))
    assert routes(tours) == [[11, 1, 2, 12], [11, 1, 3, 13]]


def test_day_length_limits(graph):
    assert suggest(graph, TourRequest(date(2027, 7, 10), 2, 4.5, 6)) == []   # first day is only 4 h
    assert suggest(graph, TourRequest(date(2027, 7, 10), 2, 3, 4.5)) == []   # hut-to-hut days are 5 h


def test_catering_and_month(graph):
    catered = suggest(graph, TourRequest(date(2027, 7, 10), 2, 3, 6, catering="catered"))
    assert routes(catered) == [[11, 1, 2, 12]]
    # Hut 3 is self-service in July only: a tour whose second night is 1 August can't use it.
    assert routes(suggest(graph, TourRequest(date(2027, 7, 31), 2, 3, 6))) == [[11, 1, 2, 12]]


def test_no_repeated_huts(graph):
    # A 3-night tour would need A → B → A, which is not allowed.
    assert suggest(graph, TourRequest(date(2027, 7, 10), 3, 3, 6)) == []


def test_start_near_and_loop(graph):
    near_s1 = (2_600_200, 1_150_000)
    assert len(suggest(graph, TourRequest(date(2027, 7, 10), 2, 3, 6, start_near=near_s1))) == 2
    assert suggest(graph, TourRequest(date(2027, 7, 10), 2, 3, 6, start_near=(2_700_000, 1_150_000))) == []
    loop = suggest(graph, TourRequest(date(2027, 7, 10), 2, 3, 6, loop=True))
    assert routes(loop) == [[11, 1, 3, 13]]  # S3 is 1 km from S1, S2 is 20 km away


def test_bbox_limits_huts(graph):
    tours = suggest(graph, TourRequest(date(2027, 7, 10), 2, 3, 6, bbox=(7.5, 46.0, 8.0, 47.0)))
    assert routes(tours) == [[11, 1, 2, 12]]  # hut 3 lies at 8.5° E


def test_night_status_unknown_and_partial():
    unknown = hut(9, catering=NONE, self_service=NONE)
    assert night_status(unknown, date(2027, 7, 1), "any", True).status == "unknown"
    assert night_status(unknown, date(2027, 7, 1), "any", False) is None
    june = night_status(hut(1), date(2027, 6, 20), "catered", True)
    assert june.status == "catered" and june.partial


def test_harder_limit_keeps_easier_alternative(graph):
    """With both a red (5 h) and a faster blue (2 h) edge A → B, a 3–6 h search must still find the red one."""
    blue = Leg(9, 1, 2, 120, 6000, 800, 700, 3, None, 3)
    graph.hut_out[1].append(blue)
    tours = suggest(graph, TourRequest(date(2027, 7, 10), 2, 3, 6, limit=3))
    assert [[leg.edge_id for leg in t.legs] for t in tours][0] == [1, 2, 6]


def test_tour_id_roundtrip_and_rebuild(graph):
    from app.planner.tours import build_tour, parse_tour_id
    graph.legs = {(leg.frm, leg.to, leg.category_limit): leg for legs in (
        *graph.stop_in.values(), *graph.hut_out.values(), *graph.stop_out.values()) for leg in legs}
    tour = suggest(graph, TourRequest(date(2027, 7, 10), 2, 3, 6))[0]
    assert tour.id == "2027-07-10_11-1-2-12_2-2-2"
    start, places, limits = parse_tour_id(tour.id)
    rebuilt = build_tour(graph, start, places, limits)
    assert rebuilt.legs == tour.legs and [n.hut for n in rebuilt.nights] == [1, 2]
    assert build_tour(graph, start, places, [3, 3, 3]).legs == tour.legs  # falls back to the stored easier variant
    with pytest.raises(ValueError, match="no longer available"):
        build_tour(graph, start, [11, 1, 12], [2, 2])   # there is no hut A → stop 12 edge
    with pytest.raises(ValueError, match="stop → hut"):
        build_tour(graph, start, [1, 2, 12], [2, 2])    # must start at a stop
    with pytest.raises(ValueError, match="no longer available"):
        build_tour(graph, start, [11, 2, 12], [2, 2])   # there is no stop 11 → hut B edge
    for bad in ("2027-07-10_11-1", "2027-07-10_11-1-12_2", "2027-07-10_11-1-12_2-9", "nonsense"):
        with pytest.raises(ValueError):
            parse_tour_id(bad)
    # A night that doesn't fit any more is kept and marked: hut 3 is closed in August
    late = build_tour(graph, date(2027, 7, 31), [11, 1, 3, 13], [2, 2, 2])
    assert [n.status for n in late.nights] == ["catered", "closed"]


def test_gpx_filename_is_ascii():
    from app.planner.gpx import filename
    tour = {"nights": 3, "days": [{"date": "2027-06-26", "from": {"name": "Elm, Büelstafel"}}]}
    assert filename(tour) == "huthop-2027-06-26-elm-buelstafel-3n.gpx"


def test_sort_orders_and_weights(graph):
    from app.planner import ranking
    base = dict(start_date=date(2027, 7, 10), nights=2, min_hours=3, max_hours=6)
    # Hut 2 (via B) is catered: certain opening data; hut 3 is self-service in July only (also certain).
    # Make hut 3 higher so "altitude" prefers the C tour while "fit" ties.
    graph.huts[3] = Hut(3, "Hut 3", 46.5, 8.5, 2_600_000, 1_150_000, NONE, SELF_JULY, False, None, 3000)
    graph.huts[2] = Hut(2, "Hut 2", 46.5, 7.8, 2_600_000, 1_150_000, SUMMER, NONE, True, None, 1600)
    by_alt = suggest(graph, TourRequest(**base, sort="altitude"))
    assert [t.nights[-1].hut for t in by_alt] == [3, 2]
    only_altitude = {m: 0 for m in ranking.METRICS} | {"altitude": 3}
    best_alt = suggest(graph, TourRequest(**base, sort="best", weights=only_altitude))
    assert [t.nights[-1].hut for t in best_alt] == [3, 2]
    assert 0 <= best_alt[0].score <= 1 and best_alt[0].metrics.altitude > best_alt[1].metrics.altitude


def test_metrics_values():
    from app.planner.ranking import metrics
    from app.planner.tours import Night
    legs = [Leg(1, 0, 1, 270, 9000, 800, 300, 1, None), Leg(2, 1, 0, 270, 9000, 300, 800, 3, None)]
    nights = [Night(1, date(2027, 7, 10), "catered", False)]
    m = metrics(legs, nights, [2250], 180, 360)
    assert m.fit == 1.0 and m.ease == 0.5 and m.altitude == 0.5 and m.certainty == 1.0 and m.beds == 0.5
    assert m.approach == 0.25  # 540 of 720 possible minutes on the first and last day


def test_config_weights(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "huthop.toml")
    assert config.ranking_weights()["fit"] == 3  # defaults without a file
    (tmp_path / "huthop.toml").write_text("[ranking.weights]\naltitude = 5\n")
    assert config.ranking_weights() | {} == {**config.ranking.DEFAULT_WEIGHTS, "altitude": 5.0}
    (tmp_path / "huthop.toml").write_text("[ranking.weights]\naltitdue = 5\n")
    with pytest.raises(ValueError, match="altitdue"):
        config.ranking_weights()
