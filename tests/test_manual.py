"""Manual corrections and hand-added huts."""

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_db
from app.db import connect
from app.ingest.sync_sac import sync
from app.main import app
from tests.test_huts import HUTS


@pytest.fixture
def setup(tmp_path):
    conn = connect(tmp_path / "test.db")
    sync(conn, HUTS)
    app.dependency_overrides[get_db] = lambda: conn
    hut_id = conn.execute("SELECT place_id FROM hut WHERE sac_id = 1").fetchone()[0]
    yield TestClient(app), conn, hut_id
    app.dependency_overrides.clear()
    conn.close()


def test_override_survives_sync_and_resets(setup):
    c, conn, hut_id = setup
    assert c.put(f"/api/huts/{hut_id}/overrides/beds", json={"value": 55, "note": "phoned the warden"}).status_code == 200
    assert c.put(f"/api/huts/{hut_id}/overrides/catering.7", json={"value": 2}).status_code == 200
    assert c.put(f"/api/huts/{hut_id}/overrides/altitude", json={"value": 2900}).status_code == 200
    sync(conn, HUTS)  # a re-sync must not undo manual values
    detail = c.get(f"/api/huts/{hut_id}").json()
    assert detail["beds"] == 55 and detail["altitude"] == 2900 and detail["months"][6] == "self_service"
    ov = c.get(f"/api/huts/{hut_id}/overrides").json()
    assert ov["fields"]["beds"] == {"value": 55, "source": 40, "overridden": True, "note": "phoned the warden"}
    assert ov["months"]["catering"][6]["overridden"] and ov["months"]["catering"][6]["source"] == 0
    assert c.delete(f"/api/huts/{hut_id}/overrides/beds").status_code == 200
    assert c.get(f"/api/huts/{hut_id}").json()["beds"] == 40
    assert c.delete(f"/api/huts/{hut_id}/overrides/beds").status_code == 404


@pytest.mark.parametrize(("field", "value"), [("beds", -1), ("beds", "many"), ("website", "ftp://x"),
                                               ("catering.13", 0), ("catering.7", 5), ("colour", "red")])
def test_override_validation(setup, field, value):
    c, _, hut_id = setup
    assert c.put(f"/api/huts/{hut_id}/overrides/{field}", json={"value": value}).status_code == 422


def test_add_and_delete_manual_hut(setup):
    c, _, sac_hut = setup
    body = {"name": "Alp Hidden", "lat": 46.6, "lon": 8.0, "altitude": 1900, "beds": 8,
            "catering": "222222002222", "self_service": "222222222222"}
    res = c.post("/api/huts", json=body)
    assert res.status_code == 201
    hut_id = res.json()["id"]
    hut = next(h for h in c.get("/api/huts").json() if h["id"] == hut_id)
    assert hut["name"] == "Alp Hidden" and hut["months"][6] == "catered" and hut["operator"] == "private"
    assert c.get(f"/api/huts/{hut_id}").json()["source"] == "manual"
    assert c.delete(f"/api/huts/{sac_hut}").status_code == 409   # SAC huts can only be corrected
    assert c.delete(f"/api/huts/{hut_id}").status_code == 200
    assert c.get(f"/api/huts/{hut_id}").status_code == 404
    assert c.post("/api/huts", json=body | {"catering": "22"}).status_code == 422
    assert c.post("/api/huts", json=body | {"lat": 52.0}).status_code == 422
