"""Parsing of the hut-reservation.org season calendar (no network)."""

from app.clients.hut_reservation import parse_calendar
from app.planner.availability import classify


def test_parse_calendar():
    rows = parse_calendar([
        {"freeBedsPerCategory": {"106": 120}, "freeBeds": 120, "hutStatus": "SERVICED", "date": "2027-07-01T00:00:00Z",
         "dateFormatted": "01.07.2027", "totalSleepingPlaces": 120, "percentage": "AVAILABLE"},
        {"freeBedsPerCategory": {}, "freeBeds": None, "hutStatus": "CLOSED", "date": "2029-07-13T00:00:00Z",
         "dateFormatted": "13.07.2029", "totalSleepingPlaces": 0, "percentage": "CLOSED"},
    ])
    assert rows == [
        {"date": "2027-07-01", "hut_status": "SERVICED", "free_beds": 120, "total_places": 120},
        {"date": "2029-07-13", "hut_status": "CLOSED", "free_beds": None, "total_places": 0},
    ]


def test_classify():
    assert classify("SERVICED", 5, 40, None, 4).state == "available"
    assert classify("SERVICED", 3, 40, None, 4).state == "full"
    assert classify("SERVICED", None, 0, None, 1).state == "not_released"
    assert classify("CLOSED", 0, 20, None, 1).state == "closed"
    # Closed 0-of-0 before a released season = really closed; with nothing released later = not released yet
    assert classify("CLOSED", None, 0, None, 1, released_later=True).state == "closed"
    assert classify("CLOSED", None, 0, None, 1, released_later=False).state == "not_released"
    assert classify("SERVICED", None, 0, None, 1, released_later=True).state == "not_released"


def test_hut_calendar_endpoint(tmp_path, monkeypatch):
    from datetime import date, timedelta

    from fastapi.testclient import TestClient

    from app.api.deps import get_db
    from app.db import connect
    from app.ingest.sync_sac import sync
    from app.main import app
    from tests.test_huts import HUTS

    conn = connect(tmp_path / "test.db")
    sync(conn, HUTS[:2])
    bookable, other = (r[0] for r in conn.execute("SELECT place_id FROM hut ORDER BY sac_id"))
    with conn:
        conn.execute("UPDATE hut SET hrs_id = 99 WHERE place_id = ?", (bookable,))
        conn.execute("UPDATE hut SET hrs_id = NULL WHERE place_id = ?", (other,))
    today = date.today()
    rows = [{"date": (today + timedelta(days=i)).isoformat(), "hut_status": s, "free_beds": f, "total_places": t}
            for i, (s, f, t) in enumerate([("SERVICED", 12, 40), ("SERVICED", 1, 40), ("CLOSED", 0, 0)])]
    calls = []
    monkeypatch.setattr("app.clients.hut_reservation.fetch_calendar", lambda hrs_id, client: calls.append(hrs_id) or rows)
    app.dependency_overrides[get_db] = lambda: conn
    try:
        c = TestClient(app)
        data = c.get(f"/api/huts/{bookable}/availability", params={"people": 2}).json()
        assert [d["state"] for d in data["days"]] == ["available", "full", "not_released"]
        assert data["days"][0]["free_beds"] == 12 and not data["error"]
        c.get(f"/api/huts/{bookable}/availability")
        assert calls == [99]  # second call served from the cache
        assert c.get(f"/api/huts/{other}/availability").json() == {"bookable": False, "days": []}
    finally:
        app.dependency_overrides.clear()
