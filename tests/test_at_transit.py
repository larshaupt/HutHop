"""Austrian stops and timetables on a tiny synthetic feed: stop keys, modes, the merge with the Swiss timetable of the
same year, and the download (skipped without credentials; no network)."""

import json
import zipfile
from datetime import date

import httpx
import pytest
import shapely

from app.db import connect
from app.ingest import at_transit, countries, load_stops, load_timetable, regions
from app.planner import transit
from tests.test_load_timetable import FEED, SINCE

# Timetable year 2027, starting six weeks before the Swiss one (13 December 2026), with a service id ("daily") that
# the Swiss fixture uses too. Kleinwalsertal-like: a station with two quays, a gondola, a stop in Switzerland (left
# to the Swiss sources), one far outside the region, and one without a station global id.
AT_FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\n1,VVT,https://vvt.at,Europe/Vienna\n",
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
                 "at:47:100,Tal Bahnhof,47.2,10.0,1,\n"
                 "at:47:100:0:1,Tal Bahnhof,47.2001,10.0001,0,at:47:100\n"
                 "at:47:100:0:2,Tal Bahnhof,47.2002,10.0002,0,at:47:100\n"
                 "at:47:200:0:1,Bergstation,47.21,10.02,0,\n"
                 "x17,Dorfplatz,47.22,10.03,0,\n"
                 "at:43:300:0:1,Buchs SG,47.17,9.48,0,\n"
                 "at:49:900:0:1,Wien Mitte,48.2,16.38,0,\n"
                 "at:47:999,Unused,47.23,10.04,0,\n",
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_desc,route_type\n"
                  "bus-1,1,4100,,Regionalbus,3\n"
                  "gondola,1,,Bergbahn Tal,,6\n"
                  "train,1,REX,,,2\n",
    "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                    "daily,1,1,1,1,1,1,1,20261101,20271211\n"
                    "summer,1,1,1,1,1,1,1,20270601,20270930\n",
    "trips.txt": "route_id,service_id,trip_id,trip_headsign\n"
                 "bus-1,daily,b1,Dorf\n"
                 "gondola,summer,g1,Berg\n"
                 "train,daily,t1,Wien\n",
    "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                      "b1,07:00:00,07:00:00,at:47:100:0:1,1\n"
                      "b1,07:10:00,07:10:00,x17,2\n"
                      "g1,09:00:00,09:00:00,at:47:100:0:2,1\n"
                      "g1,09:15:00,09:15:00,at:47:200:0:1,2\n"
                      "t1,06:00:00,06:00:00,at:43:300:0:1,1\n"
                      "t1,08:00:00,08:00:00,at:47:100:0:1,2\n"
                      "t1,14:00:00,14:00:00,at:49:900:0:1,3\n",
}


def write_zip(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for name, text in files.items():
            z.writestr(name, "\ufeff" + text)
    return path


def lv95(lat, lon):
    e, n = at_transit.WGS84_TO_LV95.transform(lon, lat)
    return float(e), float(n)


@pytest.fixture
def at_feed(tmp_path):
    return write_zip(tmp_path / "57_2027.zip", AT_FEED)


@pytest.fixture
def area(monkeypatch):
    """The region: a box around the fixture's Tyrolean stops and Buchs SG; Switzerland: a box around Buchs SG."""
    (w, s), (e, n) = lv95(47.0, 9.3), lv95(47.5, 10.3)
    buchs = shapely.Point(lv95(47.17, 9.48)).buffer(2000)
    monkeypatch.setattr(regions, "area", lambda code: shapely.box(w, s, e, n) if code == "AT" else None)
    monkeypatch.setattr(countries, "borders", lambda: {"CH": buchs})


def test_route_modes():
    assert [at_transit.route_mode(t) for t in ("3", "6", "7", "2", "1304", "116", "715", "1100", "")] == \
        ["bus", "cableway", "funicular", "train", "chairlift", "rack_railway", "bus", None, "bus"]


def test_stops_in_the_region_keyed_by_station(at_feed, area, tmp_path):
    stops = {s["ref"]: s for s in at_transit.read_stops({57: at_feed})}
    # quays merged into their station; Switzerland, outside the region and unserved stops left out
    assert set(stops) == {"at:47:100", "at:47:200", "at57:x17"}
    assert stops["at:47:100"]["mode"] == "bus,train,cableway" and stops["at:47:100"]["lat"] == 47.2  # the station's
    assert stops["at:47:200"]["mode"] == "cableway"  # a lift station: "avoid lifts" leaves it out
    assert stops["at:47:100"]["country"] == "AT" and stops["at57:x17"]["country"] is None

    conn = connect(tmp_path / "test.db")
    swiss = {"uic": 8500001, "name": "Valley", "mode": "bus", "e_lv95": 2.6e6, "n_lv95": 1.2e6, "lat": 46.5, "lon": 7.9}
    assert load_stops.load(conn, [swiss, *stops.values()]) == 4
    load_stops.load(conn, [stops["at:47:200"] | {"name": "Bergstation Gipfel"}])  # reload: updated by ref
    rows = conn.execute("SELECT s.uic, s.ref, s.mode, p.name, p.country FROM stop s JOIN place p ON p.id = s.place_id "
                        "ORDER BY s.place_id").fetchall()
    assert [tuple(r) for r in rows] == [(8500001, None, "bus", "Valley", None),
                                        (None, "at:47:100", "bus,train,cableway", "Tal Bahnhof", "AT"),
                                        (None, "at:47:200", "cableway", "Bergstation Gipfel", "AT"),
                                        (None, "at57:x17", "bus", "Dorfplatz", None)]


def test_stops_with_the_austrian_border_known(at_feed, area, monkeypatch):
    swiss = countries.borders()
    tyrol = shapely.Point(lv95(47.22, 10.03)).buffer(500)  # around Dorfplatz only
    monkeypatch.setattr(countries, "borders", lambda: swiss | {"AT": tyrol})
    stops = {s["ref"]: s["country"] for s in at_transit.read_stops({57: at_feed})}
    assert stops == {"at:47:100": "AT", "at:47:200": "AT", "at57:x17": "AT"}  # in Austria's border, not dropped


def test_no_stops_without_region_or_borders(at_feed, monkeypatch):
    monkeypatch.setattr(regions, "area", lambda code: None)
    assert at_transit.read_stops({57: at_feed}) == []


def test_merge_with_the_swiss_timetable_of_the_year(at_feed, area, tmp_path, monkeypatch):
    swiss_zip = write_zip(tmp_path / "gtfs_2027.zip", FEED)
    swiss = load_timetable.read(swiss_zip, SINCE)
    austrian = load_timetable.read(at_feed, SINCE, source="at57", year=2027)
    merged = load_timetable.merge([swiss, austrian])
    assert (merged.year, merged.start, merged.end) == (2027, date(2026, 11, 1), date(2027, 12, 11))
    shift = (date(2026, 12, 13) - date(2026, 11, 1)).days
    assert merged.services["daily"] == swiss.services["daily"] << shift  # Swiss bits re-based to the merged start
    assert merged.services["at57:daily"] == austrian.services["daily"]  # namespaced, no collision
    assert {r["line"] for r in merged.lines if isinstance(r["uic"], str)} == {"at57:bus-1", "at57:gondola", "at57:train"}
    assert load_timetable.merge([swiss]) is swiss  # Swiss alone: unchanged

    # read_all finds both on disk and merges them into one timetable for the year
    monkeypatch.setattr(load_timetable, "GTFS_DIR", tmp_path)
    monkeypatch.setattr(load_timetable, "AT_DIR", tmp_path / "at")
    (tmp_path / "at").mkdir()
    at_feed.rename(tmp_path / "at" / "57_2027.zip")
    conn = connect(tmp_path / "test.db")
    load_stops.load(conn, at_transit.read_stops())
    hut = conn.execute("INSERT INTO place (kind, name, e_lv95, n_lv95, lat, lon) "
                       "VALUES ('hut', 'h', 2600000, 1200000, 46.5, 7.9)").lastrowid
    for uic in (8500001, 8500002):
        place = conn.execute("INSERT INTO place (kind, name, e_lv95, n_lv95, lat, lon) "
                             "VALUES ('stop', 's', 2600000, 1200000, 46.5, 7.9)").lastrowid
        conn.execute("INSERT INTO stop (place_id, uic, mode) VALUES (?, ?, 'bus')", (place, uic))
    places = dict(conn.execute("SELECT coalesce(uic, ref), place_id FROM stop").fetchall())
    for key in (8500001, 8500002, "at:47:100", "at:47:200"):
        conn.execute("INSERT INTO edge (from_place, to_place, category_limit, max_category, time_min, source) "
                     "VALUES (?, ?, 1, 1, 60, 'tlm_routed')", (places[key], hut))
    conn.commit()
    keep = load_timetable.ride_stops(conn)
    assert keep == {8500001, 8500002, "at:47:100", "at:47:200"}
    feeds = [load_timetable.merge([load_timetable.read(swiss_zip, SINCE, keep=keep),
                                   load_timetable.read(tmp_path / "at" / "57_2027.zip", SINCE, keep=keep,
                                                       source="at57", year=2027)])]
    assert [f.year for f in load_timetable.read_all(keep)] == [2027]
    load_timetable.load(conn, feeds)
    assert conn.execute("SELECT start_date FROM timetable").fetchone()[0] == "2026-11-01"

    timetable = transit.load(conn)
    saturday = date(2027, 7, 31)
    swiss_day = timetable.day(places[8500001], saturday)  # as without Austria: daily bus 08:00, Saturday bus 09:00
    assert (swiss_day.calls, swiss_day.first_min, swiss_day.last_min) == (2, 480, 540)
    assert not timetable.day(places[8500001], date(2027, 8, 1)).served
    tal = timetable.day(places["at:47:100"], saturday)  # bus 07:00, train 08:00, gondola 09:00
    assert (tal.calls, tal.first_min, tal.last_min) == (3, 420, 540)
    assert timetable.day(places["at:47:200"], date(2027, 1, 10)) == transit.StopDay(True)  # gondola: summer only
    ride = timetable.rides_from(places["at:47:100"], saturday, 0, 1440)[places["at:47:200"]]
    assert (ride.departure, ride.arrival, ride.label, ride.lift) == (540, 555, "Bergbahn Tal", True)


def test_download_skipped_without_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MVO_USERNAME", raising=False)
    monkeypatch.delenv("MVO_PASSWORD", raising=False)
    monkeypatch.setattr(at_transit.httpx, "Client", lambda **kw: pytest.fail("no request without credentials"))
    assert not at_transit.download()
    assert "MVO_USERNAME and MVO_PASSWORD are not set" in capsys.readouterr().out


def test_download_new_versions_only(tmp_path, monkeypatch):
    requests = []
    version = {"id": "1"}

    def handler(request):
        requests.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path.endswith("/token"):
            ok = b"password=secret" in request.content
            return httpx.Response(200 if ok else 401, json={"access_token": "tok"} if ok else
                                  {"error": "invalid_grant", "error_description": "Invalid user credentials"})
        if request.url.path.endswith("/data-sets"):
            versions = [{"year": str(y), "dataSetVersion": {"id": version["id"], "file": {"originalName": "f.zip"}}}
                        for y in (2027, 2028)]
            return httpx.Response(200, json=[{"id": "57", "activeVersions": versions},
                                             {"id": "999", "activeVersions": versions}])
        return httpx.Response(200, content=b"zip " + request.url.path.encode())

    real_client = httpx.Client
    monkeypatch.setattr(at_transit.httpx, "Client", lambda **kw: real_client(**kw, transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(at_transit, "AT_DIR", tmp_path)
    monkeypatch.setattr(load_timetable, "AT_DIR", tmp_path)
    monkeypatch.setattr(at_transit, "DATASETS", {57: "Tyrol"})
    monkeypatch.setenv("MVO_USERNAME", "me@example.org")
    monkeypatch.setenv("MVO_PASSWORD", "secret")
    (tmp_path / "57_2026.zip").write_bytes(b"last year")
    (tmp_path / "58_2027.zip").write_bytes(b"dataset no longer used")

    assert at_transit.download(today=date(2027, 8, 1))
    assert (tmp_path / "57_2027.zip").read_bytes() == b"zip /api/public/v1/data-sets/57/2027/file"
    assert (tmp_path / "57_2028.zip").exists() and not (tmp_path / "57_2026.zip").exists()
    assert not (tmp_path / "58_2027.zip").exists()
    assert ("GET", "/api/public/v1/data-sets/57/2027/file", "Bearer tok") in requests
    assert json.loads((tmp_path / "57_2027.json").read_text()) == {"version": "1"}

    requests.clear()
    assert not at_transit.download(today=date(2027, 8, 1))  # same versions: no login, no download
    assert [p for _, p, _ in requests] == ["/api/public/v1/data-sets"]
    version["id"] = "2"
    assert at_transit.download(today=date(2027, 8, 1))  # a new version: downloaded again

    monkeypatch.setenv("MVO_PASSWORD", "wrong")
    version["id"] = "3"
    with pytest.raises(RuntimeError, match="Invalid user credentials"):
        at_transit.download(today=date(2027, 8, 1))
