"""Tests for the tour search on a small hand-made place graph.

    stop S1 ──4h── hut A ──5h── hut B ──4h── stop S2
                     └───5h── hut C (self-service only, closes in August) ──4h── stop S3 (next to S1)
"""

from datetime import date

import pytest

from app.planner.graph import Graph, Hut, Leg, Stop
from app.planner.tours import TourRequest, night_status, suggest

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

    def leg(i, a, b, minutes):
        return Leg(i, a, b, minutes, minutes * 50, 500, 400, 2, None, 2)

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
    assert suggest(graph, TourRequest(date(2027, 7, 10), 2, 3, 4.5)) == []   # hut-to-hut days are 5 h
    assert suggest(graph, TourRequest(date(2027, 7, 10), 2, 5.5, 6)) == []   # … and need 5.5 h here
    # The days to the first and from the last hut (4 h) only have a maximum
    assert len(suggest(graph, TourRequest(date(2027, 7, 10), 2, 4.5, 6))) == 2
    assert suggest(graph, TourRequest(date(2027, 7, 10), 1, 1, 3.5)) == []


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
    from app.planner.tours import TourKey, build_tour, parse_tour_id
    graph.legs = {(leg.frm, leg.to, leg.category_limit): leg for legs in (
        *graph.stop_in.values(), *graph.hut_out.values(), *graph.stop_out.values()) for leg in legs}
    tour = suggest(graph, TourRequest(date(2027, 7, 10), 2, 3, 6.5))[0]
    assert tour.id == "2027-07-10_11-1-2-12_2-2-2_3-6.5"
    key = parse_tour_id(tour.id)
    assert key.hours == (3, 6.5)
    rebuilt = build_tour(graph, key)
    assert rebuilt.legs == tour.legs and [n.hut for n in rebuilt.nights] == [1, 2]
    assert rebuilt.metrics == tour.metrics  # scored for the same hours as in the search
    assert parse_tour_id("2027-07-10_11-1-2-12_2-2-2").hours == (3, 6)  # older links without hours
    assert parse_tour_id("2027-07-10_11-1-12_2-2_7-14.29").hours == (7, 14.29)  # 10 h at pace 0.7
    start = key.start_date
    assert build_tour(graph, TourKey(start, key.places, [3, 3, 3])).legs == tour.legs  # falls back to an easier variant
    with pytest.raises(ValueError, match="no longer available"):
        build_tour(graph, TourKey(start, [11, 1, 12], [2, 2]))   # there is no hut A → stop 12 edge
    with pytest.raises(ValueError, match="stop → hut"):
        build_tour(graph, TourKey(start, [1, 2, 12], [2, 2]))    # must start at a stop
    with pytest.raises(ValueError, match="no longer available"):
        build_tour(graph, TourKey(start, [11, 2, 12], [2, 2]))   # there is no stop 11 → hut B edge
    for bad in ("2027-07-10_11-1", "2027-07-10_11-1-12_2", "2027-07-10_11-1-12_2-9", "nonsense",
                "2027-07-10_11-1-12_2-2_6-3", "2027-07-10_11-1-12_2-2_3-6_x",
                "2027-07-10_11-1-12_2-2_0-0", "2027-07-10_11-1-12_2-2_3-25"):
        with pytest.raises(ValueError):
            parse_tour_id(bad)
    # A night that doesn't fit any more is kept and marked: hut 3 is closed in August
    late = build_tour(graph, TourKey(date(2027, 7, 31), [11, 1, 3, 13], [2, 2, 2]))
    assert [n.status for n in late.nights] == ["catered", "closed"]


def test_gpx_filename_is_ascii():
    from app.planner.gpx import filename
    tour = {"nights": 3, "days": [{"date": "2027-06-26", "from": {"name": "Elm, Büelstafel"}}]}
    assert filename(tour) == "huthop-2027-06-26-elm-buelstafel-3n.gpx"


def test_best_follows_weights(graph):
    from app.planner import ranking
    base = dict(start_date=date(2027, 7, 10), nights=2, min_hours=3, max_hours=6)
    # Hut 2 (via B) is catered: certain opening data; hut 3 is self-service in July only (also certain).
    # Make hut 3 higher so a weight on altitude alone prefers the C tour.
    graph.huts[3] = Hut(3, "Hut 3", 46.5, 8.5, 2_600_000, 1_150_000, NONE, SELF_JULY, False, None, 3000)
    graph.huts[2] = Hut(2, "Hut 2", 46.5, 7.8, 2_600_000, 1_150_000, SUMMER, NONE, True, None, 1600)
    only_altitude = {m: 0 for m in ranking.METRICS} | {"altitude": 3}
    best_alt = suggest(graph, TourRequest(**base, weights=only_altitude))
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
    assert config.ranking_weights()["fresh"] == 3  # defaults without a file
    (tmp_path / "huthop.toml").write_text("[ranking.weights]\naltitude = 5\n")
    assert config.ranking_weights() | {} == {**config.ranking.DEFAULT_WEIGHTS, "altitude": 5.0}
    (tmp_path / "huthop.toml").write_text("[ranking.weights]\naltitdue = 5\n")
    with pytest.raises(ValueError, match="altitdue"):
        config.ranking_weights()


def test_gpx_has_heights():
    from app.planner import polyline
    from app.planner.gpx import to_gpx
    stop = {"name": "S", "lat": 46.5, "lon": 7.8}
    day = {"day": 1, "date": "2027-07-17", "from": stop, "to": stop, "time_min": 60, "distance_m": 1000,
           "ascent_m": 10, "descent_m": 0, "max_category": 1,
           "geometry": polyline.encode([(46.5, 7.8), (46.501, 7.801)]), "elevation": polyline.encode_values([1500, 1510])}
    xml = to_gpx({"nights": 0, "days": [day]})
    assert "<ele>1500</ele>" in xml and "<ele>1510</ele>" in xml
    assert "<ele>" not in to_gpx({"nights": 0, "days": [day | {"elevation": None}]})


def test_best_per_chain_keeps_few_variants(monkeypatch):
    from types import SimpleNamespace

    from app.planner import tours

    def state(cost, *huts):
        return (cost, (), tuple(SimpleNamespace(hut=h) for h in huts), 0.0)

    states = [state(0.1, 1, 2), state(0.2, 1, 2), state(0.3, 1, 2), state(0.15, 1, 3), state(0.05, 2, 1)]
    kept = tours.best_per_chain(states, 2)
    assert [s[0] for s in kept] == [0.05, 0.1, 0.15, 0.2]  # cheapest first, third (1, 2) variant dropped
    monkeypatch.setattr(tours, "BEAM_WIDTH", 2)
    assert [s[0] for s in tours.best_per_chain(states, 2)] == [0.05, 0.1]


def test_one_tour_per_hut_sequence_best_start(graph):
    """Two start stops for the same huts: the result has the tour once, with the better start (easier trail)."""
    graph.stop_in[1] = graph.stop_in[1] + [Leg(9, 13, 1, 240, 12_000, 500, 400, 1, None, 2)]
    tours = suggest(graph, TourRequest(date(2027, 7, 10), 2, 3, 6))
    sequences = [tuple(n.hut for n in t.nights) for t in tours]
    assert len(sequences) == len(set(sequences))
    first = next(t for t in tours if sequences[tours.index(t)] == (1, 2))
    assert first.legs[0].frm == 13 and first.legs[0].max_category == 1


def test_free_bed_search_reuses_last_pass(monkeypatch):
    """Rounds: search, fetch calendars, search again; once nothing is left to fetch, that search is the result."""
    from types import SimpleNamespace

    from app.planner import planning

    pending = {5}
    calls = []

    def fake_suggest(graph, req, lookup=None):
        calls.append("suggest")
        state = "not_checked" if pending else "available"
        return [SimpleNamespace(nights=[SimpleNamespace(hut=5, availability=SimpleNamespace(state=state))])]

    def fake_refresh(conn, huts, limit):
        calls.append("fetch")
        pending.clear()
        return len(huts)

    monkeypatch.setattr(planning, "cached_graph", lambda conn, limit: "graph")
    monkeypatch.setattr(planning, "suggest", fake_suggest)
    monkeypatch.setattr(planning.availability, "Snapshot", lambda *a: None)
    monkeypatch.setattr(planning.availability, "refresh", fake_refresh)
    req = TourRequest(date(2027, 7, 10), 2, 3, 6)
    _, found, fetched = planning.search_tours(None, req, 2, True)
    assert calls == ["suggest", "fetch", "suggest"] and fetched == 1 and len(found) == 1
    calls.clear()
    planning.search_tours(None, req, 2, True)
    assert calls == ["suggest"]  # calendars already known: one search pass


def test_flexible_start_date(graph, monkeypatch):
    """A date window: each tour once, on its best start date."""
    from app.planner import planning

    monkeypatch.setattr(planning, "cached_graph", lambda conn, limit: graph)
    # Hut 3 is self-service in July only: from 28 June, its tour first works on 1 July. Huts 1 and 2 are open only
    # part of June, so their tour scores best from 1 July too.
    _, tours, _ = planning.search_tours(None, TourRequest(date(2027, 6, 28), 2, 3, 6), 2, False, flex_days=4)
    assert sorted((routes([t])[0], t.start_date) for t in tours) == [
        ([11, 1, 2, 12], date(2027, 7, 1)), ([11, 1, 3, 13], date(2027, 7, 1))]
    _, exact, _ = planning.search_tours(None, TourRequest(date(2027, 6, 28), 2, 3, 6), 2, False)
    assert routes(exact) == [[11, 1, 2, 12]]


def test_warm_up_loads_everything_once(tmp_path):
    from app.db import connect
    from app.planner import graph as graph_mod
    from app.planner.geometry import footprint, pack

    conn = connect(tmp_path / "test.db")
    for kind in ("hut", "stop"):
        conn.execute("INSERT INTO place (kind, name, e_lv95, n_lv95, lat, lon) VALUES (?, 'p', 0, 0, 46.5, 8)", (kind,))
    conn.execute("INSERT INTO hut (place_id, sac_id, source) VALUES (1, 1, 'manual')")
    conn.execute("INSERT INTO stop (place_id, uic, mode) VALUES (2, 1, 'bus')")
    line = "_p~iF~ps|U_ulLnnqC"
    conn.execute("INSERT INTO edge (from_place, to_place, category_limit, max_category, time_min, source, geometry, "
                 "footprint, samples) VALUES (2, 1, 1, 1, 60, 'tlm_routed', ?, ?, ?)", (line, *pack(*footprint(line))))
    conn.commit()
    assert graph_mod.warm_up(conn) and not graph_mod.warm_up(conn)
    fp = graph_mod.cached_graph(conn, 2).footprints
    assert set(fp._cells) == {1}  # unpacked ahead of any search
    conn.execute("UPDATE edge SET time_min = 70")
    conn.commit()
    assert graph_mod.warm_up(conn)  # data changed: loaded again


def test_stops_served_on_the_day_and_connections_that_fit(graph):
    """Access stops: S1 (4 h walk) only gets you to the hut at 19:00, S3 (4 h 10) has a 07:00 connection; S4 is a
    cable car station with no service on the start date; S5 is a cable car that runs."""
    from app.planner import transit
    from app.planner.tours import access_options

    graph.stops[14] = Stop(14, "Stop 14", 46.5, 7.8, 2_600_500, 1_150_000, "cableway")
    graph.stops[15] = Stop(15, "Stop 15", 46.5, 7.8, 2_600_600, 1_150_000, "cableway")
    graph.stop_in[1] = [Leg(1, 11, 1, 240, 12_000, 500, 400, 2, None, 2), Leg(9, 13, 1, 250, 12_000, 500, 400, 2, None, 2),
                        Leg(10, 14, 1, 100, 5_000, 500, 400, 2, None, 2), Leg(11, 15, 1, 120, 5_000, 500, 400, 2, None, 2)]
    start = date(2027, 7, 10)
    day = (start - date(2026, 12, 13)).days
    every_day, never = (1 << 400) - 1, 0
    graph.timetable = transit.Timetable([(date(2026, 12, 13), date(2027, 12, 11), {0: every_day, 1: never}, {
        11: [(0, 15 * 60, 18 * 60, 5)], 13: [(0, 7 * 60, 18 * 60, 5)], 14: [(1, 8 * 60, 17 * 60, 50)],
        15: [(0, 8 * 60, 17 * 60, 50)], 12: [(0, 6 * 60, 20 * 60, 9)]})])
    assert graph.timetable.day(14, start).calls == 0 and day > 0
    options = [leg.frm for leg in access_options(graph, 1, start, 360)]
    assert options == [15, 13, 11]  # S4 not running; S1 last (too late for the hut); S5 closest that works
    assert [leg.frm for leg in access_options(graph, 1, start, 360, avoid_lifts=True)] == [13, 11]
    tours = suggest(graph, TourRequest(start, 2, 3, 6, avoid_lifts=True))
    assert tours and all(t.legs[0].frm == 13 for t in tours)
