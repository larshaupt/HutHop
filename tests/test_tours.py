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
    leg = lambda i, a, b, minutes: Leg(i, a, b, minutes, minutes * 50, 500, 400, 2, None)
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
    blue = Leg(9, 1, 2, 120, 6000, 800, 700, 3, None)
    graph.hut_out[1].append(blue)
    tours = suggest(graph, TourRequest(date(2027, 7, 10), 2, 3, 6, limit=3))
    assert [[leg.edge_id for leg in t.legs] for t in tours][0] == [1, 2, 6]
