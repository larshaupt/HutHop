"""Tests for the SAC import and the huts API, using a small temporary database."""

import json

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_db
from app.planner.opening import month_status
from app.db import connect
from app.ingest.sync_sac import parse_hut, sync
from app.main import app


def raw_hut(sac_id: int, name: str, catering: str, opening: str, **extra) -> dict:
    """A minimal SAC portal record. Codes are 12-character strings, one digit per month."""
    return {
        "id": sac_id,
        "display_name": name,
        "geom": {"type": "Point", "coordinates": [2620000.0, 1150000.0]},
        "altitude": extra.pop("altitude", 2500.0),
        "sleeps": extra.pop("sleeps", 40),
        "hrs_id": extra.pop("hrs_id", 0),
        "is_private": extra.pop("is_private", False),
        "catering": {f"month_{m:02d}": int(c) for m, c in enumerate(catering, 1)},
        "opening": {f"month_{m:02d}": int(c) for m, c in enumerate(opening, 1)},
        "services": extra.pop("services", {}),
        "routes": extra.pop("routes", []),
        "photos": [],
        **extra,
    }


HUTS = [
    raw_hut(1, "Summer Hut", "222221000122", "000000000000", altitude=2840.0, hrs_id=7,
            services={"half_board": True, "cooking_non_catered": True}),
    raw_hut(2, "Winter Room Only", "222222222222", "000000000000", altitude=3200.0, sleeps=12,
            services={"cooking_non_catered": True}),
    raw_hut(3, "Mystery Hut", "222222222222", "222222222222", altitude=1500.0, is_private=True),
]


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "test.db")
    sync(conn, HUTS)
    yield conn
    conn.close()


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_parse_hut_converts_coordinates_and_drops_paid_routes():
    raw = raw_hut(9, " Test <b>Hut</b> ", "2" * 12, "0" * 12, description="Nice <b>view</b>", routes=[
        {"id": 1, "title": "Free", "discipline": "mountain_hiking", "availability": "free"},
        {"id": 2, "title": "Paid", "discipline": "mountain_hiking", "availability": "limited"},
    ])
    parsed = parse_hut(raw)
    assert parsed["place"]["name"] == "Test <b>Hut</b>"
    assert 46 < parsed["place"]["lat"] < 47 and 7 < parsed["place"]["lon"] < 9
    assert [r["title"] for r in json.loads(parsed["hut"]["routes"])] == ["Free"]
    assert parsed["hut"]["description"] == "Nice view"
    assert parsed["hut"]["hrs_id"] is None


def test_sync_is_idempotent_and_keeps_overrides(db):
    hut_id = db.execute("SELECT place_id FROM hut WHERE sac_id = 1").fetchone()[0]
    db.execute("INSERT INTO hut_override (hut_id, field, value) VALUES (?, 'beds', '55')", (hut_id,))
    sync(db, HUTS)
    assert db.execute("SELECT count(*) FROM hut").fetchone()[0] == 3
    assert db.execute("SELECT beds FROM hut_effective WHERE id = ?", (hut_id,)).fetchone()[0] == 55


@pytest.mark.parametrize(("catering", "self_service", "month", "expected"), [
    ("222221000122", "000000000000", 7, "catered"),
    ("222221000122", "000000000000", 6, "catered"),       # 1 = part of the month counts
    ("222221000122", "000000000000", 1, "self_service"),
    ("222221000122", "222222222222", 1, "closed"),
    ("222222222222", "222222222222", 7, "unknown"),       # all 2 = no data
])
def test_month_status(catering, self_service, month, expected):
    assert month_status(catering, self_service, month) == expected


def test_list_filters(client):
    names = lambda **params: [h["name"] for h in client.get("/api/huts", params=params).json()]
    assert names() == ["Mystery Hut", "Summer Hut", "Winter Room Only"]
    assert names(month=7) == ["Summer Hut", "Winter Room Only"]
    assert names(month=7, open_as="catered") == ["Summer Hut"]
    assert names(month=1, open_as="self_service") == ["Summer Hut", "Winter Room Only"]
    assert names(half_board=True) == ["Summer Hut"]
    assert names(alt_min=3000) == ["Winter Room Only"]
    assert names(operator="private") == ["Mystery Hut"]
    assert names(q="winter") == ["Winter Room Only"]


def test_detail(client):
    hut = next(h for h in client.get("/api/huts").json() if h["name"] == "Summer Hut")
    detail = client.get(f"/api/huts/{hut['id']}").json()
    assert detail["months"][6] == "catered" and detail["months"][0] == "self_service"
    assert detail["partial_months"][5] is True
    assert detail["booking_url"].endswith("/book-hut/7/wizard")
    assert client.get("/api/huts/999999").status_code == 404


def test_frontend_is_revalidated():
    from fastapi.testclient import TestClient

    from app.main import app

    for path in ("/", "/js/main.js", "/style.css"):
        assert TestClient(app).get(path).headers["cache-control"] == "no-cache"
