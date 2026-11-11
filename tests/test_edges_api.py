"""Tests for the connections API: best edge per category limit, stop grouping and review."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_db
from app.db import connect
from app.ingest.sync_sac import sync
from app.main import app
from tests.test_huts import HUTS


@pytest.fixture
def client(tmp_path):
    conn = connect(tmp_path / "test.db")
    sync(conn, HUTS[:2])
    a, b = (r[0] for r in conn.execute("SELECT place_id FROM hut ORDER BY sac_id"))
    stop = conn.execute("INSERT INTO place (kind, name, e_lv95, n_lv95, lat, lon) "
                        "VALUES ('stop', 'Valley Station', 2620000, 1150000, 46.5, 7.9)").lastrowid
    conn.execute("INSERT INTO stop (place_id, uic, mode) VALUES (?, 8500001, 'cableway,bus')", (stop,))
    conn.executemany("INSERT INTO stop_line (stop_place, line, label, departures) VALUES (?, ?, ?, ?)",
                     [(stop, "92-121", "B 121", 20.4), (stop, "93-244", "GB 2440", 300.0), (stop, "92-9", "B 9", 0)])
    edges = [  # (from, to, limit, max_category, minutes)
        (a, b, 1, 1, 300), (a, b, 3, 3, 200),
        (stop, a, 1, 1, 90), (a, stop, 1, 1, 60),
    ]
    conn.executemany(
        "INSERT INTO edge (from_place, to_place, category_limit, max_category, time_min, distance_m, ascent_m, "
        "descent_m, source, geometry) VALUES (?, ?, ?, ?, ?, 1000, 100, 100, 'tlm_routed', '_p~iF~ps|U')", edges)
    conn.commit()
    app.dependency_overrides[get_db] = lambda: conn
    yield TestClient(app), {"a": a, "b": b, "stop": stop}
    app.dependency_overrides.clear()
    conn.close()


def test_best_edge_per_limit(client):
    c, ids = client
    for limit, minutes, category in ((1, 300, 1), (2, 300, 1), (3, 200, 3)):
        data = c.get(f"/api/huts/{ids['a']}/edges", params={"limit": limit}).json()
        assert [(e["place"]["id"], e["time_min"], e["max_category"]) for e in data["huts"]] == [(ids["b"], minutes, category)]


def test_stops_grouped_by_direction(client):
    c, ids = client
    stops = c.get(f"/api/huts/{ids['a']}/edges").json()["stops"]
    assert len(stops) == 1
    s = stops[0]
    assert s["place"]["name"] == "Valley Station" and s["place"]["mode"] == "cableway,bus"
    assert s["to_hut"]["time_min"] == 90 and s["from_hut"]["time_min"] == 60
    assert s["place"]["lines"] == ["GB 2440", "B 121"]  # most frequent first, no line without summer service
    assert s["place"]["departures"] == 320


def test_exclude_edge(client):
    c, ids = client
    edge = c.get(f"/api/huts/{ids['a']}/edges").json()["huts"][0]
    assert c.patch(f"/api/edges/{edge['id']}", json={"excluded": True, "note": "path closed"}).json() == {
        "id": edge["id"], "excluded": True, "note": "path closed"}
    assert c.get(f"/api/huts/{ids['a']}/edges").json()["huts"][0]["excluded"] is True
    assert c.get(f"/api/huts/{ids['a']}/edges", params={"limit": 1}).json()["huts"][0]["excluded"] is True
    assert c.patch("/api/edges/999999", json={"excluded": True}).status_code == 404
    assert c.patch(f"/api/edges/{edge['id']}", json={}).status_code == 400
    assert c.patch(f"/api/edges/{edge['id']}", json={"excluded": None}).status_code == 422


def test_unknown_hut(client):
    c, _ = client
    assert c.get("/api/huts/999999/edges").status_code == 404


def test_suggest_one_night_tour(client):
    c, ids = client
    body = {"start_date": "2027-07-10", "nights": 1, "min_hours": 1, "max_hours": 2, "limit": 2,
            "check_availability": False}
    tours = c.post("/api/tours/suggest", json=body).json()["tours"]
    assert len(tours) == 1
    days = tours[0]["days"]
    assert [d["from"]["id"] for d in days] == [ids["stop"], ids["a"]]
    assert days[0]["night"]["status"] == "catered" and not days[0]["night"]["partial"] and days[1]["night"] is None
    assert days[0]["night"]["booking_url"].endswith("/book-hut/7/wizard")
    assert days[0]["geometry"] and tours[0]["time_min"] == 150
    assert c.post("/api/tours/suggest", json=body | {"min_hours": 3, "max_hours": 2}).status_code == 422
    assert c.post("/api/tours/suggest", json=body | {"start_date": "2020-07-10"}).status_code == 422
    conn = app.dependency_overrides[get_db]()
    [log] = conn.execute("SELECT query, results, best_tour FROM search_log").fetchall()  # invalid ones aren't logged
    assert json.loads(log["query"])["nights"] == 1 and log["results"] == 1 and log["best_tour"] == tours[0]["id"]


def test_stop_search(client):
    c, ids = client
    assert [s["id"] for s in c.get("/api/stops", params={"q": "valley"}).json()] == [ids["stop"]]


def calendar(day: str, status: str, free: int | None, total: int) -> list[dict]:
    return [{"date": day, "hut_status": status, "free_beds": free, "total_places": total}]


@pytest.mark.parametrize(("status", "free", "total", "people", "expected"), [
    ("SERVICED", 10, 40, 2, "available"),
    ("SERVICED", 1, 40, 2, None),              # full for 2 people: the hut drops out
    ("SERVICED", None, 0, 2, "not_released"),  # 0 of 0: falls back to SAC opening codes
    ("CLOSED", None, 0, 2, "not_released"),
    ("UNSERVICED", 12, 12, 2, "available"),    # winter room counts as self-service
])
def test_tour_uses_availability(client, monkeypatch, status, free, total, people, expected):
    c, ids = client
    calls = []
    monkeypatch.setattr("app.clients.hut_reservation.fetch_calendar",
                        lambda hrs_id, _client: calls.append(hrs_id) or calendar("2027-07-10", status, free, total))
    body = {"start_date": "2027-07-10", "nights": 1, "min_hours": 1, "max_hours": 2, "people": people}
    tours = c.post("/api/tours/suggest", json=body).json()["tours"]
    assert calls == [7]  # hut A's hut-reservation.org id
    if expected is None:
        assert tours == []
        return
    night = tours[0]["days"][0]["night"]
    assert night["availability"]["state"] == expected
    if status == "UNSERVICED":
        assert night["status"] == "self_service"
    # Cached: a second search within the TTL doesn't fetch again
    c.post("/api/tours/suggest", json=body)
    assert calls == [7]


def test_closed_before_released_season(client, monkeypatch):
    """Like Chamanna Cluozza: closed (0 of 0) on 10 July, released from 20 July. The hut must drop out."""
    c, _ = client
    days = calendar("2027-07-10", "CLOSED", None, 0) + calendar("2027-07-20", "SERVICED", 50, 61)
    monkeypatch.setattr("app.clients.hut_reservation.fetch_calendar", lambda hrs_id, _client: days)
    body = {"start_date": "2027-07-10", "nights": 1, "min_hours": 1, "max_hours": 2}
    assert c.post("/api/tours/suggest", json=body).json()["tours"] == []


def test_fetch_error_keeps_tour_marked(client, monkeypatch):
    c, _ = client
    def boom(hrs_id, _client):
        raise RuntimeError("network down")
    monkeypatch.setattr("app.clients.hut_reservation.fetch_calendar", boom)
    body = {"start_date": "2027-07-10", "nights": 1, "min_hours": 1, "max_hours": 2}
    night = c.post("/api/tours/suggest", json=body).json()["tours"][0]["days"][0]["night"]
    assert night["availability"]["state"] == "error"


def test_tour_by_id_and_gpx(client):
    import xml.etree.ElementTree as ET
    c, ids = client
    body = {"start_date": "2027-07-10", "nights": 1, "min_hours": 1, "max_hours": 2, "check_availability": False}
    tour = c.post("/api/tours/suggest", json=body).json()["tours"][0]
    again = c.get(f"/api/tours/{tour['id']}").json()
    assert again["id"] == tour["id"] and again["days"][0]["to"]["id"] == ids["a"]

    res = c.get(f"/api/tours/{tour['id']}/gpx")
    assert res.status_code == 200 and res.headers["content-type"].startswith("application/gpx+xml")
    assert 'filename="huthop-2027-07-10-valley-station-1n.gpx"' in res.headers["content-disposition"]
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    root = ET.fromstring(res.text)
    assert len(root.findall("g:trk", ns)) == 2                       # one track per day
    assert [w.find("g:desc", ns).text for w in root.findall("g:wpt", ns)] == ["Start", "Night 1 (2027-07-10)", "End"]
    assert root.find("g:trk/g:trkseg/g:trkpt", ns) is not None

    assert c.get("/api/tours/not-a-tour").status_code == 400
    assert c.get(f"/api/tours/2027-07-10_{ids['stop']}-{ids['b']}-{ids['stop']}_1-1").status_code == 404


def test_share_page(client):
    c, ids = client
    c.app.dependency_overrides[get_db]().execute("UPDATE place SET name = '<b>\"Hut\"' WHERE id = ?", (ids["a"],))
    body = {"start_date": "2027-07-10", "nights": 1, "min_hours": 1, "max_hours": 2, "check_availability": False}
    tour = c.post("/api/tours/suggest", json=body).json()["tours"][0]
    res = c.get(f"/t/{tour['id']}")
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/html")
    page = res.text
    assert '<meta property="og:title" content="2-day hut tour: &lt;b&gt;&quot;Hut&quot;">' in page  # escaped
    assert "10 Jul 2027 – 11 Jul 2027 · Valley Station to Valley Station · 2.0 km · 200 m ascent" in page
    assert f'url=/#v=tours&amp;tid={tour["id"]}"' in page
    assert c.get("/t/not-a-tour").status_code == 400
    gone = c.get(f"/t/2027-07-10_{ids['stop']}-{ids['b']}-{ids['stop']}_1-1")
    assert gone.status_code == 200 and "HutHop hut tour" in gone.text and "url=/#v=tours" in gone.text


def test_editor_options_and_tour_from_hut(client):
    c, ids = client
    params = {"start_date": "2027-07-10", "min_hours": 1, "max_hours": 2, "limit": 2}
    tour_id = c.get(f"/api/huts/{ids['a']}/tour", params=params).json()["tour_id"]
    assert tour_id == f"2027-07-10_{ids['stop']}-{ids['a']}-{ids['stop']}_1-1_1-2"
    data = c.get(f"/api/tours/{tour_id}/options", params={"limit": 2}).json()
    assert data["tour"]["id"] == tour_id and "warnings" not in data["tour"] and data["tour"]["hours"] == [1, 2]
    placed = [o for o in data["options"] if o["place"]]
    assert all(o["distance_m"] > 0 and o["walk_min"] > 0 for o in placed)
    assert [o["distance_m"] for o in placed] == sorted(o["distance_m"] for o in placed)  # nearest first
    for o in data["options"]:  # every option names a tour that exists
        assert c.get(f"/api/tours/{o['tour_id']}").status_code == 200
    assert c.get(f"/api/huts/{ids['b']}/tour", params=params).status_code == 404  # no stop connects to B
    assert c.get("/api/huts/999999/tour", params=params).status_code == 404


def test_settings_pace_and_altitude(client):
    c, ids = client
    body = {"start_date": "2027-07-10", "nights": 1, "min_hours": 1, "max_hours": 2, "check_availability": False}
    tour = c.post("/api/tours/suggest", json=body).json()["tours"][0]
    assert tour["time_min"] == 150 and tour["outside_settings"] == []
    slow = c.get(f"/api/tours/{tour['id']}", params={"pace": 1.2}).json()
    assert slow["time_min"] == 180 and [d["time_min"] for d in slow["days"]] == [108, 72]  # 90 and 60 min at 120 %
    # At 150 % the 90-minute day takes 2:15 h, more than the 2 hours asked for: no tour
    assert c.post("/api/tours/suggest", json=body | {"pace": 1.5}).json()["tours"] == []
    edges = c.get(f"/api/huts/{ids['a']}/edges", params={"pace": 2}).json()
    assert edges["stops"][0]["to_hut"]["time_min"] == 180
    yellow = c.get(f"/api/tours/{tour['id']}", params={"limit": 1}).json()
    assert yellow["outside_settings"] == []  # its days are yellow


def test_booking_helper_fresh_beds_and_contact(client, monkeypatch):
    """The booking helper checks free beds again right before booking: calendars older than a minute are fetched
    again (not only after the usual 10 minutes). Each night carries the hut's contact for booking by hand."""
    c, ids = client
    calls = []
    monkeypatch.setattr("app.clients.hut_reservation.fetch_calendar",
                        lambda hrs_id, _client: calls.append(hrs_id) or calendar("2027-07-10", "SERVICED", 10, 40))
    tour_id = f"2027-07-10_{ids['stop']}-{ids['a']}-{ids['stop']}_1-1"
    params = {"check_availability": True}
    night = c.get(f"/api/tours/{tour_id}", params=params).json()["days"][0]["night"]
    assert calls == [7] and night["availability"]["state"] == "available"
    assert night["contact"]["booking_methods"] == [] and "phone" in night["contact"]
    conn = app.dependency_overrides[get_db]()
    two_min_ago = (datetime.now(UTC) - timedelta(minutes=2)).isoformat(timespec="seconds")
    conn.execute("UPDATE availability_fetch SET fetched_at = ?", (two_min_ago,))  # within the usual 10 minutes
    c.get(f"/api/tours/{tour_id}", params=params)
    assert calls == [7]
    c.get(f"/api/tours/{tour_id}", params=params | {"fresh": True})
    assert calls == [7, 7]
    c.get(f"/api/tours/{tour_id}", params=params | {"fresh": True})  # just fetched: not again
    assert calls == [7, 7]


def test_osm_paths_as_geojson(client, tmp_path, monkeypatch):
    from app.ingest import osm_access

    client, _ = client
    monkeypatch.setattr(osm_access, "FILE", tmp_path / "access.json")
    assert client.get("/api/osm-paths").json() == {"type": "FeatureCollection", "features": []}  # none fetched yet
    osm_access.FILE.write_text(json.dumps({"huts": [5], "paths": {"5": {
        "grade": "T5", "coords": [[2600000, 1200000, 3000], [2600100, 1200000, 2950]]}}}))
    (feature,) = client.get("/api/osm-paths").json()["features"]
    assert feature["properties"] == {"hut": 5, "grade": "T5"}
    (lon, lat), _ = feature["geometry"]["coordinates"]
    assert lon == pytest.approx(7.4386, abs=1e-3) and lat == pytest.approx(46.9511, abs=1e-3)  # LV95 origin, Bern


def test_unofficial_connection_has_the_access_path_grade(client, tmp_path, monkeypatch):
    from app.ingest import osm_access

    client, ids = client
    monkeypatch.setattr(osm_access, "FILE", tmp_path / "access.json")
    osm_access.FILE.write_text(json.dumps({"huts": [ids["b"]], "paths": {str(ids["b"]): {"grade": "T4", "coords": []}}}))
    conn = app.dependency_overrides[get_db]()
    conn.execute("INSERT INTO edge (from_place, to_place, category_limit, max_category, time_min, distance_m, ascent_m, "
                 "descent_m, source, geometry) VALUES (?, ?, 4, 4, 100, 1000, 100, 100, 'tlm_routed', '_p~iF~ps|U')",
                 (ids["a"], ids["b"]))
    (edge,) = client.get(f"/api/huts/{ids['a']}/edges", params={"limit": 4}).json()["huts"]
    assert edge["max_category"] == 4 and edge["osm_grade"] == "T4"
    (edge,) = client.get(f"/api/huts/{ids['a']}/edges", params={"limit": 3}).json()["huts"]
    assert edge["osm_grade"] is None  # the alpine trail, not the unofficial path
    assert client.get(f"/api/huts/{ids['b']}").json()["osm_grade"] == "T4"
    assert osm_access.edge_grade({1: "T3", 2: "T5"}, 4, 1, 2) == "T5"


def test_tour_over_an_unofficial_path_can_be_reopened(client):
    client, ids = client
    conn = app.dependency_overrides[get_db]()
    conn.executemany("INSERT INTO edge (from_place, to_place, category_limit, max_category, time_min, distance_m, "
                     "ascent_m, descent_m, source, geometry) VALUES (?, ?, 4, 4, 100, 1000, 100, 100, 'tlm_routed', "
                     "'_p~iF~ps|U')", [(ids["stop"], ids["b"]), (ids["b"], ids["stop"])])
    conn.commit()
    tour_id = f"2027-07-10_{ids['stop']}-{ids['b']}-{ids['stop']}_4-4"
    tour = client.get(f"/api/tours/{tour_id}", params={"limit": 4})
    assert tour.status_code == 200, tour.text  # the graph of all trail limits, not only up to blue-white
    assert [d["max_category"] for d in tour.json()["days"]] == [4, 4]
