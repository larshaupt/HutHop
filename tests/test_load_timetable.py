"""Timetable import on a tiny GTFS feed: lines per stop and summer departures (calendar, exceptions, frequencies)."""

import zipfile
from datetime import date

import httpx
import pytest

from app.db import connect
from app.ingest import load_timetable

# Timetable year 2027: summer window 1 July to 31 August = 62 days, of which 9 Saturdays.
FEED = {
    "feed_info.txt": "feed_publisher_name,feed_start_date,feed_end_date,feed_version\nSBB,20261213,20271211,20261017\n",
    "stops.txt": "stop_id,stop_name,location_type,parent_station,didok\n"
                 "ch:1:sloid:1:1:1,Valley,,Parentch:1:sloid:1,8500001\n"
                 "ch:1:sloid:1:1:2,Valley,,Parentch:1:sloid:1,8500001\n"
                 "ch:1:sloid:2,Pass,,Parentch:1:sloid:2,8500002\n"
                 "8000001,Abroad,,Parent8000001,8000001\n",
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_desc,route_type\n"
                  "92-121-A-j27-1,1,121,,B,700\n"
                  "92-121-B-j27-1,2,121,,B,700\n"
                  "93-244-j27-1,3,2440,,GB,1300\n"
                  "91-61-j27-1,4,R61,,R,106\n",
    "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                    "daily,1,1,1,1,1,1,1,20261213,20271211\n"
                    "saturdays,0,0,0,0,0,1,0,20261213,20271211\n"
                    "winter,1,1,1,1,1,1,1,20261213,20270331\n",
    "calendar_dates.txt": "service_id,date,exception_type\n"
                          "daily,20270801,2\n"         # no service on 1 August
                          "extra,20270715,1\n"         # one extra day, only in calendar_dates
                          "saturdays,20270705,1\n",    # a Monday added to the Saturday service
    "trips.txt": "route_id,service_id,trip_id\n"
                 "92-121-A-j27-1,daily,bus1\n"
                 "92-121-B-j27-1,saturdays,bus2\n"
                 "93-244-j27-1,daily,gondola\n"
                 "91-61-j27-1,winter,train\n"
                 "91-61-j27-1,extra,train2\n",
    "frequencies.txt": "trip_id,start_time,end_time,headway_secs,exact_times\n"
                       "gondola,08:00:00,17:00:00,1800,0\n",   # every 30 minutes: 18 runs a day
    "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                      "bus1,08:00:00,08:00:00,ch:1:sloid:1:1:1,1\n"
                      "bus1,08:30:00,08:30:00,ch:1:sloid:2,2\n"
                      "bus2,09:00:00,09:00:00,ch:1:sloid:1:1:2,1\n"
                      "gondola,08:00:00,08:00:00,ch:1:sloid:2,1\n"
                      "train,07:00:00,07:00:00,ch:1:sloid:1:1:1,1\n"
                      "train,07:00:00,07:00:00,8000001,2\n"
                      "train2,07:00:00,07:00:00,ch:1:sloid:2,1\n",
}


@pytest.fixture
def feed(tmp_path):
    path = tmp_path / "gtfs.zip"
    with zipfile.ZipFile(path, "w") as z:
        for name, text in FEED.items():
            z.writestr(name, "﻿" + text)  # the real files start with a byte order mark
    return path


def test_line_key_and_label():
    assert load_timetable.line_key("92-121-A-j27-1") == "92-121-A"
    assert load_timetable.line_label("B", "121") == "B 121"
    assert load_timetable.line_label("R", "R61") == "R61"


SINCE = date(2026, 12, 1)  # read() drops past days; the fixture timetable is for 2027


def test_lines_and_summer_departures(feed):
    rows = {(r["uic"], r["line"]): r for r in load_timetable.read(feed, SINCE).lines}
    assert set(rows) == {(8500001, "92-121-A"), (8500001, "92-121-B"), (8500002, "92-121-A"),
                         (8500002, "93-244"), (8500002, "91-61")}  # winter-only train and stops abroad left out
    assert rows[(8500001, "92-121-A")]["departures"] == pytest.approx(61 / 62, abs=0.01)  # daily except 1 August
    assert rows[(8500001, "92-121-B")]["departures"] == pytest.approx(10 / 62, abs=0.01)  # 9 Saturdays + 1 Monday
    assert rows[(8500002, "93-244")]["departures"] == pytest.approx(61 * 18 / 62, abs=0.01)
    assert rows[(8500002, "91-61")]["label"] == "R61"
    assert rows[(8500002, "91-61")]["departures"] == pytest.approx(1 / 62, abs=0.01)  # one extra day


def test_service_days_and_times(feed):
    f = load_timetable.read(feed, SINCE)
    day = lambda d: (d - f.start).days  # noqa: E731
    assert f.services["daily"] >> day(date(2027, 7, 31)) & 1 and not f.services["daily"] >> day(date(2027, 8, 1)) & 1
    assert f.services["extra"] == 1 << day(date(2027, 7, 15))
    assert f.services["saturdays"] >> day(date(2027, 7, 5)) & 1  # a Monday added by an exception
    times = {(uic, s): (first, last, calls) for uic, s, first, last, calls in f.stop_services}
    assert times[(8500002, "daily")] == (8 * 60, 16 * 60 + 30, 19)  # bus 08:30 + gondola every 30 min 08:00–16:30
    assert times[(8500001, "saturdays")] == (9 * 60, 9 * 60, 1)
    later = load_timetable.read(feed, date(2027, 8, 1))  # days before are dropped, and services only running then
    assert "extra" not in later.services and not any(s == "extra" for _, s, *_ in later.stop_services)


def test_summer_departures_once_summer_is_past(feed):
    for since in (date(2027, 8, 1), date(2027, 9, 6)):  # the newest feed's summer partly or fully past
        assert load_timetable.read(feed, since).lines == load_timetable.read(feed, SINCE).lines


def stops_and_hut(conn):
    """Two stops with the fixture's numbers, each connected to a hut (the planner loads stops with edges only)."""
    hut = conn.execute("INSERT INTO place (kind, name, e_lv95, n_lv95, lat, lon) "
                       "VALUES ('hut', 'h', 2600000, 1200000, 46.5, 7.9)").lastrowid
    ids = []
    for uic in (8500001, 8500002):
        place = conn.execute("INSERT INTO place (kind, name, e_lv95, n_lv95, lat, lon) "
                             "VALUES ('stop', 's', 2600000, 1200000, 46.5, 7.9)").lastrowid
        conn.execute("INSERT INTO stop (place_id, uic, mode) VALUES (?, ?, 'bus')", (place, uic))
        conn.execute("INSERT INTO edge (from_place, to_place, category_limit, max_category, time_min, source) "
                     "VALUES (?, ?, 1, 1, 60, 'tlm_routed')", (place, hut))
        ids.append(place)
    return ids


def test_load_and_fingerprint(feed, tmp_path):
    conn = connect(tmp_path / "test.db")
    stops_and_hut(conn)
    before = load_timetable.lines_fingerprint(conn)
    f = load_timetable.read(feed, SINCE)
    assert load_timetable.load(conn, [f]) == 5
    after = load_timetable.lines_fingerprint(conn)
    assert after != before
    f.lines = [r | {"departures": r["departures"] * 2} for r in f.lines]
    load_timetable.load(conn, [f])
    assert load_timetable.lines_fingerprint(conn) == after  # only departures changed: no rebuild needed


def test_stop_served_on_a_date(feed, tmp_path):
    from app.planner import transit

    conn = connect(tmp_path / "test.db")
    valley, pass_ = stops_and_hut(conn)
    load_timetable.load(conn, [load_timetable.read(feed, SINCE)])
    timetable = transit.load(conn)
    saturday = timetable.day(valley, date(2027, 7, 31))  # daily bus 08:00 and the Saturday bus 09:00
    assert saturday.served and (saturday.calls, saturday.first_min, saturday.last_min) == (2, 480, 540)
    assert not timetable.day(valley, date(2027, 8, 1)).served  # a Sunday without the daily bus (1 August)
    assert timetable.day(pass_, date(2027, 7, 17)).calls == 19
    assert not timetable.day(valley, date(2028, 1, 5)).known  # no timetable loaded for that date: counts as served
    assert timetable.day(valley, date(2028, 1, 5)).served
    assert transit.hhmm(1510) == "01:10"


def test_download_this_and_next_year(tmp_path, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request.url.path)
        if "timetable-2028" in request.url.path:
            return httpx.Response(404)
        if request.url.path.endswith("/permalink"):
            year = request.url.path.split("timetable-")[1][:4]
            return httpx.Response(302, headers={"location": f"https://example.org/download/gtfs_fp{year}_1017.zip"})
        return httpx.Response(200, content=b"zip bytes")

    real_client = httpx.Client
    monkeypatch.setattr(load_timetable.httpx, "Client",
                        lambda **kw: real_client(**kw, transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(load_timetable, "GTFS_DIR", tmp_path)
    (tmp_path / "gtfs_2026.zip").write_bytes(b"last year")

    assert load_timetable.download(today=date(2027, 8, 1))  # 2028 not out yet: this year's timetable only
    assert (tmp_path / "gtfs_2027.zip").read_bytes() == b"zip bytes"
    assert not (tmp_path / "gtfs_2026.zip").exists()  # past years are deleted
    assert not load_timetable.download(today=date(2027, 8, 1))  # same edition: no second download
    assert requests.count("/download/gtfs_fp2027_1017.zip") == 1


def test_adopts_file_from_before_years(tmp_path, monkeypatch):
    monkeypatch.setattr(load_timetable, "GTFS_DIR", tmp_path)
    (tmp_path / "gtfs.zip").write_bytes(b"old layout")
    (tmp_path / "edition.json").write_text('{"href": "https://example.org/x/gtfs_fp2027_20261017.zip"}')
    load_timetable._adopt_legacy_file()
    assert (tmp_path / "gtfs_2027.zip").read_bytes() == b"old layout"
    assert load_timetable.feed_files() == [tmp_path / "gtfs_2027.zip"]
