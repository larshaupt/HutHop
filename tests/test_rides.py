"""Public transport within a tour (TourRequest.transit) on a small hand-made graph with one bus line.

    stop S1 ──4h── hut A ──2h── stop X ═══ bus ═══ stop Y ──2.5h── hut B ──4h── stop S2
                     └── also from stop S3 (5h)
No walking connection A → B: the only two-night tour rides the bus on day 2. The bus leaves X at 09:00 (before
you can be there: you leave the hut at 08:00), 10:30 (arrives 11:00) and 16:00 (you'd reach the hut after 18:00).
"""

from datetime import date

import pytest

from app.api.deps import get_db
from app.planner import editing, transit
from app.planner.graph import Graph, Hut, Leg, RideLeg, Stop
from app.planner.tours import TourKey, TourRequest, build_tour, parse_tour_id, ride_options, suggest
from tests.test_edges_api import client  # noqa: F401  (fixture)

S1, S2, S3, X, Y, A, B = 11, 12, 13, 21, 22, 1, 2
DAY = date(2027, 7, 10)


def bus(departure: int, lift: bool = False) -> transit.Pattern:
    return transit.Pattern("B 1", "Valley", lift, (0,), ((X, departure), (Y, departure + 30)))


def make_graph(patterns: list[transit.Pattern]) -> Graph:
    huts = {h: Hut(h, f"Hut {h}", 46.5, 7.8, 2_600_000, 1_150_000, "222221000122", "2" * 12, True) for h in (A, B)}
    stops = {s: Stop(s, f"Stop {s}", 46.5, 7.8, 2_600_000, 1_150_000, "bus") for s in (S1, S2, S3, X, Y)}
    served = {s: [(0, 360, 1200, 10)] for s in stops}
    timetable = transit.Timetable([(date(2027, 1, 1), date(2027, 12, 31), {0: (1 << 365) - 1}, served)], [patterns])
    g = Graph(huts, stops, timetable=timetable)

    def leg(i, a, b, minutes):
        return Leg(i, a, b, minutes, minutes * 50, 500, 400, 2, None, 2)
    g.stop_in = {A: [leg(1, S1, A, 240), leg(6, S3, A, 300)], B: [leg(3, Y, B, 150)]}
    g.stop_out = {A: [leg(2, A, X, 120)], B: [leg(4, B, S2, 240)]}
    g.legs = {(x.frm, x.to, x.category_limit): x for legs in (*g.stop_in.values(), *g.stop_out.values()) for x in legs}
    return g


@pytest.fixture
def graph():
    return make_graph([bus(9 * 60), bus(10 * 60 + 30), bus(16 * 60)])


def test_ride_only_when_asked(graph):
    assert suggest(graph, TourRequest(DAY, 2, 3, 6)) == []  # default: walking only, as before
    tours = suggest(graph, TourRequest(DAY, 2, 3, 6, transit=True))
    assert [[leg.frm for leg in t.legs] + [t.legs[-1].to] for t in tours] == [[S1, A, B, S2]]
    day2 = tours[0].legs[1]
    assert isinstance(day2, RideLeg) and day2.walks == (graph.legs[(A, X, 2)], graph.legs[(Y, B, 2)])
    assert (day2.ride.departure, day2.ride.arrival, day2.ride.label) == (630, 660, "B 1")  # the 10:30 bus
    assert day2.time_min == 270 and day2.distance_m == 270 * 50  # only the walks count


def test_ride_must_fit_the_day(graph):
    # 4–6 h a day: the walks (4.5 h) are too short
    assert suggest(graph, TourRequest(DAY, 2, 4.75, 6, transit=True)) == []
    # Slower: at the stop at 11:00 after the 10:30 bus, and from the 16:00 bus at the hut only at 20:15
    assert ride_options(graph, A, DAY, 180, 360, pace=1.5) == []
    # Without the 10:30 bus nothing fits
    assert ride_options(make_graph([bus(9 * 60), bus(16 * 60)]), A, DAY, 180, 360) == []
    # Only on days the bus runs: no timetable patterns on a day outside the feed
    assert ride_options(graph, A, date(2028, 7, 10), 180, 360) == []


def test_avoid_lifts_skips_lift_rides():
    lift_graph = make_graph([bus(10 * 60 + 30, lift=True)])
    assert len(ride_options(lift_graph, A, DAY, 180, 360)) == 1
    assert ride_options(lift_graph, A, DAY, 180, 360, avoid_lifts=True) == []


def test_ride_tour_id_and_rebuild(graph):
    tour = suggest(graph, TourRequest(DAY, 2, 3, 6, transit=True))[0]
    assert tour.id == f"2027-07-10_{S1}-{A}-{B}-{S2}_2-2-2_3-6_1.{X}.2.{Y}.2.630"
    key = parse_tour_id(tour.id)
    assert key.rides == {1: (X, 2, Y, 2, 630)} and key.hours == (3, 6)
    rebuilt = build_tour(graph, key)
    assert rebuilt.legs == tour.legs and rebuilt.id == tour.id and rebuilt.metrics == tour.metrics
    with pytest.raises(ValueError, match="no longer runs"):
        build_tour(graph, TourKey(DAY, key.places, key.limits, key.hours, {1: (X, 2, Y, 2, 631)}))
    for bad in ("_1.21.2.22.2", "_0.21.2.22.2.630", "_1.21.5.22.2.630", "_1.21.2.22.2.3000"):
        with pytest.raises(ValueError):
            parse_tour_id(f"2027-07-10_{S1}-{A}-{B}-{S2}_2-2-2_3-6{bad}")


def test_edits_keep_rides(graph):
    tour = suggest(graph, TourRequest(DAY, 2, 3, 6, transit=True))[0]
    start = [e for e in editing.edits(graph, parse_tour_id(tour.id), today=DAY) if e.kind == "start"]
    assert [e.place for e in start] == [S3] and start[0].key.rides == {1: (X, 2, Y, 2, 630)}
    assert build_tour(graph, start[0].key).legs[1] == tour.legs[1]


def test_rides_per_tour_capped(graph, monkeypatch):
    from app.planner import tours
    monkeypatch.setattr(tours, "MAX_RIDES", 0)
    assert suggest(graph, TourRequest(DAY, 2, 3, 6, transit=True)) == []


def test_departures_and_rides_from(graph):
    tt = graph.timetable
    assert [t for t, _, _ in tt.departures(X, DAY)] == [540, 630, 960]
    assert tt.rides_from(X, DAY, 600, 18 * 60) == {Y: transit.Ride(X, Y, 630, 660, "B 1", "Valley")}
    assert tt.rides_from(Y, DAY, 0, 24 * 60) == {}  # the bus ends there


def test_gpx_of_a_ride_day_has_the_walks_only():
    from app.planner import polyline
    from app.planner.gpx import to_gpx
    stop = {"name": "S", "lat": 46.5, "lon": 7.8}
    walk = {"geometry": polyline.encode([(46.5, 7.8), (46.501, 7.801)]), "elevation": None}
    day = {"day": 1, "date": "2027-07-17", "from": stop, "to": stop, "time_min": 60, "distance_m": 1000,
           "ascent_m": 10, "descent_m": 0, "max_category": 1, "geometry": None, "elevation": None,
           "walks": [walk, walk], "ride": {"line": "B 1", "departure": "10:30", "arrival": "11:00",
                                           "from": stop, "to": stop}}
    xml = to_gpx({"nights": 0, "days": [day]})
    assert xml.count("<trkseg>") == 2 and xml.count("<trk>") == 1 and "B 1 10:30" in xml


def test_api_ride_day_and_stop_timetable(client):  # noqa: F811
    c, ids = client
    conn = c.app.dependency_overrides[get_db]()
    stop, a, b = ids["stop"], ids["a"], ids["b"]
    other = conn.execute("INSERT INTO place (kind, name, e_lv95, n_lv95, lat, lon) "
                         "VALUES ('stop', 'Pass', 2630000, 1150000, 46.5, 8.0)").lastrowid
    conn.execute("INSERT INTO stop (place_id, uic, mode) VALUES (?, 8500002, 'bus')", (other,))
    conn.executemany("INSERT INTO edge (from_place, to_place, category_limit, max_category, time_min, distance_m, "
                     "ascent_m, descent_m, source, geometry) VALUES (?, ?, 1, 1, ?, 1000, 100, 100, 'tlm_routed', "
                     "'_p~iF~ps|U')", [(other, b, 120), (b, other, 120)])
    conn.execute("INSERT INTO timetable (feed, start_date, end_date) VALUES (2027, '2026-12-13', '2027-12-11')")
    conn.execute("INSERT INTO timetable_service (feed, service, days) VALUES (2027, 0, ?)", (b"\xff" * 46,))
    conn.execute("INSERT INTO trip_pattern VALUES (2027, 0, 'B 121', 'Pass', 0, '0')")
    conn.executemany("INSERT INTO trip_call VALUES (2027, 0, ?, ?, ?)", [(0, stop, 630), (1, other, 660)])
    conn.execute("UPDATE data_version SET version = version + 1")
    conn.commit()

    tour_id = f"2027-07-10_{stop}-{a}-{b}-{other}_1-1-1_3-6_1.{stop}.1.{other}.1.630"
    day = c.get(f"/api/tours/{tour_id}").json()["days"][1]
    assert day["ride"]["line"] == "B 121" and (day["ride"]["departure"], day["ride"]["arrival"]) == ("10:30", "11:00")
    assert [(w["from"]["id"], w["to"]["id"], w["time_min"]) for w in day["walks"]] == [(a, stop, 60), (other, b, 120)]
    assert day["time_min"] == 180 and day["geometry"] is None
    assert c.get(f"/api/tours/{tour_id}/gpx").text.count("<trkseg>") == 4  # days 1 and 3, and day 2's two walks

    timetable = c.get(f"/api/stops/{stop}/departures", params={"date": "2027-07-11"}).json()
    assert timetable["known"] and timetable["lines"] == [{"line": "B 121", "headsign": "Pass", "times": ["10:30"]}]
    assert not c.get(f"/api/stops/{stop}/departures", params={"date": "2028-07-11"}).json()["known"]
    assert c.get("/api/stops/999999/departures", params={"date": "2027-07-11"}).status_code == 404
