"""Admin password protection of data-changing endpoints."""

import base64

import pytest
from fastapi.testclient import TestClient

from app import admin
from app.api.deps import get_db
from app.db import connect
from app.ingest.sync_sac import sync
from app.main import app
from tests.test_huts import HUTS


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "PASSWORD_FILE", tmp_path / "admin_password")
    monkeypatch.delenv("HUTHOP_REQUIRE_ADMIN", raising=False)
    conn = connect(tmp_path / "test.db")
    sync(conn, HUTS)
    app.dependency_overrides[get_db] = lambda: conn
    hut_id = conn.execute("SELECT place_id FROM hut WHERE sac_id = 1").fetchone()[0]
    yield TestClient(app), hut_id, monkeypatch
    app.dependency_overrides.clear()
    conn.close()


def auth(password: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"anyone:{password}".encode()).decode()}


def test_hash_roundtrip():
    stored = admin.hash_password("correct horse battery")
    assert admin.check_password("correct horse battery", stored)
    assert not admin.check_password("wrong", stored)
    assert not admin.check_password("x", "garbage")


def test_open_locally_without_password(client):
    c, hut_id, _ = client
    assert c.put(f"/api/huts/{hut_id}/overrides/beds", json={"value": 50}).status_code == 200


def test_refused_on_server_until_password_set(client):
    c, hut_id, monkeypatch = client
    monkeypatch.setenv("HUTHOP_REQUIRE_ADMIN", "1")
    res = c.put(f"/api/huts/{hut_id}/overrides/beds", json={"value": 50})
    assert res.status_code == 403 and "admin password" in res.json()["detail"]
    assert c.get("/api/huts").status_code == 200  # viewing stays open


def test_password_required_once_set(client):
    c, hut_id, _ = client
    admin.PASSWORD_FILE.write_text(admin.hash_password("correct horse battery"))
    url = f"/api/huts/{hut_id}/overrides/beds"
    res = c.put(url, json={"value": 50})
    assert res.status_code == 401 and res.headers["www-authenticate"].startswith("Basic")
    assert c.put(url, json={"value": 50}, headers=auth("wrong")).status_code == 401
    assert c.put(url, json={"value": 50}, headers=auth("correct horse battery")).status_code == 200
    for method, path in (("patch", "/api/edges/1"), ("delete", f"/api/huts/{hut_id}"),
                         ("post", "/api/huts"), ("delete", f"{url}")):
        assert getattr(c, method)(path).status_code == 401, (method, path)
    # Reading and planning need no password
    assert c.get(f"/api/huts/{hut_id}").status_code == 200
    body = {"start_date": "2027-07-10", "nights": 1, "check_availability": False}
    assert c.post("/api/tours/suggest", json=body).status_code == 200
