"""Tour editor (planner/editing.py) on a small hand-made graph.

    S1 ── A ══ B ── S2        tour: S1 → A → B → S2 (A ══ B has a yellow and a harder, faster variant)
     │    │╲   │╲             S1 → B, A → S1, B → S3, S3 → A, S3 → D
     S3   C  D  C             A → C, A → D, D → B, B → C, C → S2
"""

from datetime import date, timedelta

import pytest

from app.planner.editing import edits, leg_limit, tour_from_hut, warnings
from app.planner.graph import Graph, Hut, Leg, Stop
from app.planner.tours import TourKey, build_tour

OPEN = "222221000122"  # catered June to October
A, B, C, D = 1, 2, 3, 4
S1, S2, S3 = 11, 12, 13
START = date(2027, 7, 10)


@pytest.fixture
def graph():
    huts = {h: Hut(h, f"Hut {h}", 46.5, 7.8, 2_600_000 + h * 1000, 1_150_000, OPEN, "2" * 12, True) for h in (A, B, C, D)}
    stops = {s: Stop(s, f"Stop {s}", 46.5, 7.8, 2_600_000 + s * 1000, 1_150_000, "bus") for s in (S1, S2, S3)}
    g = Graph(huts, stops)
    legs = [(S1, A, 120), (A, B, 300), (B, S2, 150), (S1, B, 200), (A, S1, 120), (B, S3, 100), (S3, A, 90),
            (S3, D, 60), (A, C, 280), (A, D, 240), (D, B, 250), (B, C, 260), (C, S2, 110)]
    for i, (frm, to, minutes) in enumerate(legs, 1):
        add(g, Leg(i, frm, to, minutes, minutes * 50, 300, 300, 1, None, 1))
    add(g, Leg(99, A, B, 270, 14_000, 400, 300, 2, None, 2))  # the harder, faster variant of A → B
    return g


def add(g: Graph, leg: Leg) -> None:
    g.legs[(leg.frm, leg.to, leg.category_limit)] = leg
    if leg.frm in g.huts and leg.to in g.huts:
        g.hut_out.setdefault(leg.frm, []).append(leg)
    elif leg.frm in g.stops:
        g.stop_in.setdefault(leg.to, []).append(leg)
    else:
        g.stop_out.setdefault(leg.frm, []).append(leg)


TOUR = TourKey(START, [S1, A, B, S2], [1, 1, 1], (3.0, 6.0))


def by_kind(graph, key=TOUR, cap=1):
    out = {}
    for e in edits(graph, key, cap, today=date(2027, 1, 1)):
        out.setdefault((e.kind, e.slot), []).append(e)
    return out


def test_leg_limit(graph):
    assert leg_limit(graph, A, B, 1) == 1 and leg_limit(graph, A, B, 2) == 2
    assert leg_limit(graph, S1, A, 3) == 3  # build_tour takes the hardest variant at or below the cap
    assert leg_limit(graph, C, A, 1) is None


def test_start_and_end_stops(graph):
    e = by_kind(graph)
    assert [x.key.places for x in e[("start", None)]] == [[S3, A, B, S2]]
    assert [x.key.places for x in e[("end", None)]] == [[S1, A, B, S3]]


def test_swap_keeps_or_repicks_the_stop(graph):
    e = by_kind(graph)
    # Night 1: D instead of A; S1 doesn't connect to D, so the start becomes S3 (the only stop that does)
    assert [x.key.places for x in e[("swap", 0)]] == [[S3, D, B, S2]]
    # Night 2: C instead of B (C → S2 exists, so S2 stays); D has no stop to get home and isn't offered
    assert [x.key.places for x in e[("swap", 1)]] == [[S1, A, C, S2]]


def test_insert_between_and_at_the_end(graph):
    e = by_kind(graph)
    assert ("insert", 0) not in e  # no hut leads to A
    middle = e[("insert", 1)]
    assert [x.key.places for x in middle] == [[S1, A, D, B, S2]]
    assert middle[0].key.limits == [1, 1, 1, 1]
    end = e[("insert", 2)]
    assert [x.key.places for x in end] == [[S1, A, B, C, S2]] and end[0].key.start_date == START


def test_insert_at_the_start_moves_the_start_a_day_earlier(graph):
    add(graph, Leg(50, C, A, 200, 10_000, 300, 300, 1, None, 1))
    add(graph, Leg(51, S2, C, 100, 5_000, 300, 300, 1, None, 1))
    first = by_kind(graph)[("insert", 0)][0]
    assert first.key.places == [S2, C, A, B, S2] and first.key.start_date == START - timedelta(days=1)
    soon = TourKey(date(2027, 1, 1), TOUR.places, TOUR.limits, TOUR.hours)  # a day earlier would be in the past
    assert by_kind(graph, soon)[("insert", 0)][0].key.start_date == date(2027, 1, 1)


def test_remove_a_night(graph):
    e = by_kind(graph)
    first = e[("remove", 0)][0].key
    assert first.places == [S1, B, S2] and first.start_date == START + timedelta(days=1)  # S1 → B exists
    last = e[("remove", 1)][0].key
    assert last.places == [S1, A, S1] and last.start_date == START  # A → S2 doesn't: the closest stop, S1


def test_other_trail_limit(graph):
    variants = by_kind(graph)[("limit", 1)]
    assert [x.key.limits for x in variants] == [[1, 2, 1]]
    tour = build_tour(graph, variants[0].key)
    assert tour.legs[1].edge_id == 99


def test_every_edit_builds(graph):
    for e in edits(graph, TOUR, 1, today=date(2027, 1, 1)):
        build_tour(graph, e.key)  # raises if a day has no edge


def test_warnings(graph):
    long_day = build_tour(graph, TourKey(START, [S1, A, B, S2], [1, 1, 1], (3.0, 4.5)))
    assert {"code": "too_long", "day": 2} in warnings(graph, long_day)  # 5 h between huts, at most 4.5
    short = build_tour(graph, TourKey(START, [S1, A, B, S2], [1, 1, 1], (5.5, 8.0)))
    assert warnings(graph, short) == [{"code": "too_short", "day": 2}]  # the first and last day have no minimum
    harder = build_tour(graph, TourKey(START, [S1, A, B, S2], [1, 2, 1], (3.0, 6.0)))
    assert warnings(graph, harder, limit=1) == [{"code": "harder_trail", "day": 2}]
    november = build_tour(graph, TourKey(date(2027, 11, 10), [S1, A, B, S2], [1, 1, 1], (3.0, 6.0)))
    assert {"code": "closed", "night": 1} in warnings(graph, november)


def test_tour_from_hut(graph):
    key = tour_from_hut(graph, A, START, (3.0, 6.0), 1)
    assert key.places == [S3, A, S1] and key.limits == [1, 1]  # S3 → A (1.5 h) is closer than S1 → A (2 h)
    assert tour_from_hut(graph, C, START, (3.0, 6.0), 1) is None  # no stop leads to C
