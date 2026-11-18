"""Import of huts abroad from hut-reservation.org (app/ingest/sync_hrs.py), from saved responses (no network)."""

import json

import pytest
import shapely
from fastapi.testclient import TestClient

from app.api.deps import get_db
from app.db import connect
from app.ingest import countries, regions, sync_hrs
from app.ingest.sync_sac import sync as sync_sac
from app.main import app
from tests.test_huts import raw_hut


def hut_info(hrs_id: int, name: str, coordinates: str, tenant: str = "OEAV", **extra) -> dict:
    """A trimmed hutInfo response."""
    return {
        "hutId": hrs_id, "hutName": name, "tenantCode": tenant, "tenantCountry": "AT", "coordinates": coordinates,
        "altitude": "2.211 m", "totalBedsInfo": "80", "hutWebsite": "https://www.alpenverein.at/test",
        "hutWarden": "Someone", "phone": "+43 660 1234567",
        "picture": {"blobPath": f"https://www.hut-reservation.org/data/files/hut_picture_{hrs_id}.jpg"},
        "hutBedCategories": [{"categoryID": 1, "totalSleepingPlaces": 70, "reservationMode": "ROOM", "isVisible": True},
                             {"categoryID": 2, "totalSleepingPlaces": 10, "reservationMode": "UNSERVICED",
                              "isVisible": True}],
        "hutGeneralDescriptions": [{"language": "DE_DE", "description": "<p>Sch&ouml;ne H&uuml;tte</p>"}],
        **extra,
    }


def day(iso: str, status: str, total: int, free: int | None = None) -> dict:
    """One day of the season calendar (getHutAvailability)."""
    y, m, d = iso.split("-")
    return {"dateFormatted": f"{d}.{m}.{y}", "hutStatus": status, "freeBeds": free, "totalSleepingPlaces": total}


def days(start: str, end: str, status: str, total: int) -> list[dict]:
    from datetime import date, timedelta
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    return [day((a + timedelta(i)).isoformat(), status, total, total) for i in range((b - a).days + 1)]


# Winter room in December, closed January to mid-June, catered from 15 June to 20 September, then not released
CALENDAR = (days("2026-12-01", "2026-12-31", "UNSERVICED", 10) + days("2027-01-01", "2027-06-14", "CLOSED", 0)
            + days("2027-06-15", "2027-09-20", "SERVICED", 70) + days("2027-09-21", "2027-11-30", "CLOSED", 0))


def test_parse_hut_maps_fields():
    parsed = sync_hrs.parse_hut(hut_info(300, " Oberwalder-Hütte ", "47.105367/12.715533", altitude="2.973 m",
                                         hutGeneralDescriptions=[
                                             {"language": "DE_DE", "description": "Deutsch"},
                                             {"language": "EN", "description": "Nice <b>hut</b><br>Book&nbsp;early"}]))
    place, hut = parsed["place"], parsed["hut"]
    assert place["name"] == "Oberwalder-Hütte" and place["altitude_m"] == 2973
    assert (place["lat"], place["lon"]) == (47.105367, 12.715533)
    assert hut["operator"] == "club" and hut["owner"] == "ÖAV" and hut["sac_id"] is None and hut["hrs_id"] == 300
    assert hut["beds"] == 80 and hut["shelter_beds"] == 10 and hut["source"] == "hrs_api"
    assert hut["description"] == "Nice hut\nBook early"
    assert json.loads(hut["photos"])[0]["large"].endswith("hut_picture_300.jpg")
    assert "hutWarden" not in json.loads(hut["raw"])  # no personal names stored
    assert sync_hrs.parse_hut(hut_info(1, "German", "47.1, 12.7"))["hut"]["description"] == "Schöne Hütte"
    assert sync_hrs.parse_hut(hut_info(2, "Private", "47.1, 12.7", tenant="HOBEX"))["hut"]["operator"] == "private"


@pytest.mark.parametrize(("text", "expected"), [
    ("47.105367, 12.715533", (47.105367, 12.715533)),
    ("12.24388, 47.70463", (47.70463, 12.24388)),  # lon, lat
    ("615056 / 104542 ; N 46.09224 / E 7.63336", (46.09224, 7.63336)),
    ("5280260 / 445766", (47.673455, 14.277505)),  # UTM zone 33 (Dümlerhütte)
    ("UTM Y (Nord) 5.217.848 / UTM X (Ost) 602.866", (47.106114, 10.35574)),  # zone 32 (Edmund-Graf-Hütte)
    ("629110 / 138260", None),  # Swiss grid
    ("46,944 / 13,339", (46.944, 13.339)),  # decimal commas
    ("46.916885,8.875034", (46.916885, 8.875034)),
    ("XXX.XXX / YYY.YYY", None),
    ("66.4688/15.9878", None),  # not in the Alps
    ("", None),
])
def test_coordinates(text, expected):
    assert sync_hrs._latlon(text) == expected


def test_month_codes_from_the_calendar():
    codes = sync_hrs.month_codes(sync_hrs.hut_reservation.parse_calendar(CALENDAR))
    assert codes[12] == (2, 0)  # winter room only
    assert codes[3] == (2, 2)  # closed before the released season
    assert codes[6] == (1, 2) and codes[7] == (0, 2) and codes[9] == (1, 2)
    assert 10 not in codes and 11 not in codes  # closed after the last released day: not known yet
    rows = sync_hrs.opening_months(codes, previous={10: (0, 2)})
    assert rows[9] == {"month": 10, "catering": 0, "self_service": 2}  # last sync's code
    assert rows[10] == {"month": 11, "catering": 2, "self_service": 2}  # default season: closed in November
    assert sync_hrs.opening_months({}, {})[6] == {"month": 7, "catering": 1, "self_service": 2}  # default: partly
    # Serviced on every day for two years: a placeholder, not a season
    assert sync_hrs.month_codes(sync_hrs.hut_reservation.parse_calendar(
        days("2026-11-17", "2028-11-16", "SERVICED", 50))) == {}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_hrs, "INFO_DIR", tmp_path / "info")
    monkeypatch.setattr(sync_hrs, "CALENDAR_DIR", tmp_path / "calendar")
    monkeypatch.setattr(regions, "areas", lambda: {"AT": shapely.box(2_400_000, 1_000_000, 3_200_000, 1_400_000)})
    monkeypatch.setattr(countries, "country_at", lambda e, n: ["CH" if x < 2_700_000 else None for x in e])
    monkeypatch.setattr(countries, "borders", lambda: {})
    (tmp_path / "info").mkdir()
    (tmp_path / "calendar").mkdir()
    infos = [hut_info(240, "Lindauer Hütte", "47.07, 9.83", tenant="DAV"),
             hut_info(2, "Glecksteinhütte SAC", "46.625111, 8.096502", tenant="SAC"),  # in Switzerland
             hut_info(7, "Listed by the SAC", "47.0, 10.5"),  # an SAC hut has this hrs id
             hut_info(9, "Far away", "48.9, 16.9")]  # outside the region's area
    for info in infos:
        (tmp_path / "info" / f"{info['hutId']}.json").write_text(json.dumps(info))
    (tmp_path / "calendar" / "240.json").write_text(json.dumps(CALENDAR))
    conn = connect(tmp_path / "test.db")
    sync_sac(conn, [raw_hut(1, "SAC hut abroad", "2" * 12, "2" * 12, hrs_id=7)])
    yield conn
    conn.close()


def test_sync_imports_huts_abroad_and_keeps_overrides(db):
    assert sync_hrs.sync(db, offline=True) == 1
    hut = db.execute("SELECT * FROM hut_effective WHERE source = 'hrs_api'").fetchone()
    assert (hut["name"], hut["owner"], hut["operator"], hut["country"]) == ("Lindauer Hütte", "DAV", "club", "AT")
    assert json.loads(hut["services"]) == {"half_board": True, "cooking_non_catered": True}
    months = {r["month"]: (r["catering"], r["self_service"]) for r in db.execute(
        "SELECT * FROM hut_month WHERE hut_id = ?", (hut["id"],))}
    assert months[7] == (0, 2) and months[12] == (2, 0)
    db.execute("INSERT INTO hut_override (hut_id, field, value) VALUES (?, 'beds', '55')", (hut["id"],))
    db.execute("INSERT INTO hut_override (hut_id, field, value) VALUES (?, 'catering.10', '0')", (hut["id"],))
    assert sync_hrs.sync(db, offline=True) == 1
    assert db.execute("SELECT count(*) FROM hut").fetchone()[0] == 2
    assert db.execute("SELECT beds FROM hut_effective WHERE id = ?", (hut["id"],)).fetchone()[0] == 55
    assert db.execute("SELECT catering FROM hut_month_effective WHERE hut_id = ? AND month = 10",
                      (hut["id"],)).fetchone()[0] == 0


def test_hut_websites_are_scraped_by_hrs_key(db):
    from app.ingest import scrape_huts
    sync_hrs.sync(db, offline=True)
    db.execute("UPDATE hut SET website = 'https://sac.example' WHERE sac_id = 1")
    keys = {h["key"]: h["name"] for h in scrape_huts.huts_to_scrape(db, None, None)}
    assert keys == {"1": "SAC hut abroad", "hrs-240": "Lindauer Hütte"}


def test_api_shows_hrs_huts(db):
    sync_hrs.sync(db, offline=True)
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        hut = next(h for h in client.get("/api/huts").json() if h["name"] == "Lindauer Hütte")
        assert hut["operator"] == "club" and hut["bookable"] and hut["half_board"] and hut["self_catering"]
        assert hut["months"][6] == "catered" and hut["months"][11] == "self_service"
        detail = client.get(f"/api/huts/{hut['id']}").json()
        assert detail["sac_url"] is None and detail["routes"] == [] and detail["source"] == "hrs_api"
        assert detail["booking_url"].endswith("/240/wizard")
        assert client.get(f"/api/huts/{hut['id']}/overrides").json()["source"] == "hrs_api"
    finally:
        app.dependency_overrides.clear()
