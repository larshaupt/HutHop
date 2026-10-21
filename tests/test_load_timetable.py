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


def test_lines_and_summer_departures(feed):
    rows = {(r["uic"], r["line"]): r for r in load_timetable.read(feed)}
    assert set(rows) == {(8500001, "92-121-A"), (8500001, "92-121-B"), (8500002, "92-121-A"),
                         (8500002, "93-244"), (8500002, "91-61")}  # winter-only train and stops abroad left out
    assert rows[(8500001, "92-121-A")]["departures"] == pytest.approx(61 / 62, abs=0.01)  # daily except 1 August
    assert rows[(8500001, "92-121-B")]["departures"] == pytest.approx(10 / 62, abs=0.01)  # 9 Saturdays + 1 Monday
    assert rows[(8500002, "93-244")]["departures"] == pytest.approx(61 * 18 / 62, abs=0.01)
    assert rows[(8500002, "91-61")]["label"] == "R61"
    assert rows[(8500002, "91-61")]["departures"] == pytest.approx(1 / 62, abs=0.01)  # one extra day


def test_load_and_fingerprint(feed, tmp_path):
    conn = connect(tmp_path / "test.db")
    for uic in (8500001, 8500002):
        place = conn.execute("INSERT INTO place (kind, name, e_lv95, n_lv95, lat, lon) "
                             "VALUES ('stop', 's', 2600000, 1200000, 46.5, 7.9)").lastrowid
        conn.execute("INSERT INTO stop (place_id, uic, mode) VALUES (?, ?, 'bus')", (place, uic))
    before = load_timetable.lines_fingerprint(conn)
    rows = load_timetable.read(feed)
    assert load_timetable.load(conn, rows) == 5
    after = load_timetable.lines_fingerprint(conn)
    assert after != before
    load_timetable.load(conn, [r | {"departures": r["departures"] * 2} for r in rows])
    assert load_timetable.lines_fingerprint(conn) == after  # only departures changed: no rebuild needed


def test_download_newest_edition_once(tmp_path, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request.url.path)
        if "timetable-2028" in request.url.path:
            return httpx.Response(404)
        if request.url.path.endswith("/permalink"):
            return httpx.Response(302, headers={"location": "https://example.org/download/gtfs_fp2027_20261017.zip"})
        return httpx.Response(200, content=b"zip bytes")

    real_client = httpx.Client
    monkeypatch.setattr(load_timetable.httpx, "Client",
                        lambda **kw: real_client(**kw, transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(load_timetable, "GTFS_DIR", tmp_path)
    monkeypatch.setattr(load_timetable, "GTFS_ZIP", tmp_path / "gtfs.zip")
    monkeypatch.setattr(load_timetable, "EDITION", tmp_path / "edition.json")

    assert load_timetable.download(today=date(2027, 8, 1))  # 2028 not out yet: this year's timetable
    assert (tmp_path / "gtfs.zip").read_bytes() == b"zip bytes"
    assert requests[-1] == "/download/gtfs_fp2027_20261017.zip"
    assert not load_timetable.download(today=date(2027, 8, 1))  # same edition: no second download
    assert requests.count("/download/gtfs_fp2027_20261017.zip") == 1
