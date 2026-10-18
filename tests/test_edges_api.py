"""Tests for the connections API: best edge per category limit, stop grouping and review."""

import pytest
from fastapi.testclient import TestClient

from app.api.huts import get_db
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


def test_exclude_edge(client):
    c, ids = client
    edge = c.get(f"/api/huts/{ids['a']}/edges").json()["huts"][0]
    assert c.patch(f"/api/edges/{edge['id']}", json={"excluded": True, "note": "path closed"}).json() == {
        "id": edge["id"], "excluded": True, "note": "path closed"}
    assert c.get(f"/api/huts/{ids['a']}/edges").json()["huts"][0]["excluded"] is True
    assert c.patch("/api/edges/999999", json={"excluded": True}).status_code == 404
    assert c.patch(f"/api/edges/{edge['id']}", json={}).status_code == 400


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


def test_fetch_error_keeps_tour_marked(client, monkeypatch):
    c, _ = client
    def boom(hrs_id, _client):
        raise RuntimeError("network down")
    monkeypatch.setattr("app.clients.hut_reservation.fetch_calendar", boom)
    body = {"start_date": "2027-07-10", "nights": 1, "min_hours": 1, "max_hours": 2}
    night = c.post("/api/tours/suggest", json=body).json()["tours"][0]["days"][0]["night"]
    assert night["availability"]["state"] == "error"
