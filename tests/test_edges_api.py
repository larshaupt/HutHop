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
