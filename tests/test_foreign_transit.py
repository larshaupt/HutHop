"""Foreign feeds together on tiny synthetic ones: an Austrian dataset with global ids and an open German feed with its
own ids meeting at Kufstein (one stop, counted by the Austrian feed, trips of both), the open feed cut to each
year's days with the rest unknown, and transit_feeds' download and cut (no network)."""

import zipfile
from datetime import date, timedelta

import httpx
import numpy as np
import pytest
import shapely

from app.db import connect
from app.ingest import countries, foreign_transit, load_stops, load_timetable, regions, transit_feeds
from app.ingest.foreign_transit import FeedFile, Source
from app.planner import transit
from tests.test_at_transit import write_zip
from tests.test_load_timetable import FEED

AT_FEED = {
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
                 "Pat:47:2184,Kufstein Bahnhof,47.5830,12.1660,1,\n"
                 "at:47:2184:0:1,Kufstein Bahnhof,47.5816,12.1656,0,Pat:47:2184\n"   # 160 m from the station
                 "at:47:2000:0:1,Wörgl Hbf,47.49,12.06,0,\n",
    "routes.txt": "route_id,route_short_name,route_long_name,route_type\nrex,REX,,2\n",
    "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                    "daily,1,1,1,1,1,1,1,20261213,20271211\n",
    "trips.txt": "route_id,service_id,trip_id,trip_headsign\nrex,daily,r1,Kufstein\n",
    "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                      "r1,07:00:00,07:00:00,at:47:2000:0:1,1\n"
                      "r1,07:20:00,07:20:00,at:47:2184:0:1,2\n",
}
# gtfs.de-like: numeric ids, 30 days. Kufstein (in Austria, 30 m from the Austrian station), Rosenheim and
# Kiefersfelden (in Germany, no German border known: the region's area alone, or the feed's country)
DE_FEED = {
    "stops.txt": "stop_name,parent_station,stop_id,stop_lat,stop_lon,location_type\n"
                 "Kufstein,,1,47.58308,12.16555,1\n"
                 "Kufstein,1,11,47.58308,12.16555,\n"
                 "Rosenheim,,2,47.85,12.12,1\n"
                 "Rosenheim,2,21,47.85,12.12,\n"
                 "Kiefersfelden Dorf,,3,47.61,12.19,\n",
    "routes.txt": "route_long_name,route_short_name,agency_id,route_type,route_id\n,RB54,1,2,rb\n,9494,2,3,bus\n",
    "calendar.txt": "monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date,service_id\n"
                    "1,1,1,1,1,1,1,20270105,20270204,wk\n",
    "trips.txt": "route_id,service_id,trip_id\nrb,wk,t1\nbus,wk,b1\n",
    "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                      "t1,06:00:00,06:00:00,21,0\n"
                      "t1,06:40:00,06:40:00,11,1\n"
                      "b1,08:00:00,08:00:00,3,0\n"
                      "b1,08:10:00,08:10:00,11,1\n",
}
SINCE = date(2026, 12, 1)


def box(south, west, north, east):
    """A box of latitudes and longitudes, in LV95."""
    degrees = shapely.segmentize(shapely.box(west, south, east, north), 0.01)
    return shapely.transform(degrees, lambda xy: np.column_stack(foreign_transit.WGS84_TO_LV95.transform(xy[:, 0], xy[:, 1])))


@pytest.fixture
def borders(monkeypatch):
    """Areas: Austria up to 47.65°N, Germany from 47.55°N; Austria's border at 47.598°N, Switzerland far away."""
    areas = {"AT": box(47.0, 11.5, 47.65, 12.8), "DE": box(47.55, 11.5, 48.0, 12.8)}
    monkeypatch.setattr(regions, "area", lambda code: areas.get(code))
    monkeypatch.setattr(countries, "borders", lambda: {"CH": box(47.1, 9.4, 47.2, 9.5),
                                                       "AT": box(47.0, 11.5, 47.598, 12.8)})


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(load_timetable, "GTFS_DIR", tmp_path)
    monkeypatch.setattr(foreign_transit, "GTFS_DIR", tmp_path)
    monkeypatch.setattr(foreign_transit, "AT_DIR", tmp_path / "at")
    (tmp_path / "at").mkdir()
    (tmp_path / "de").mkdir()
    write_zip(tmp_path / "gtfs_2027.zip", FEED)
    write_zip(tmp_path / "gtfs_2028.zip", {k: v.replace("2027", "2028").replace("2026", "2027") for k, v in FEED.items()})
    write_zip(tmp_path / "at" / "57_2027.zip", AT_FEED)
    write_zip(tmp_path / "de" / "gtfsde.zip", DE_FEED)
    return [FeedFile("at57", "AT", tmp_path / "at" / "57_2027.zip", 2027), FeedFile("gtfsde", "DE", tmp_path / "de" / "gtfsde.zip")]


def test_one_stop_per_station_across_feeds(files, borders, monkeypatch):
    assert foreign_transit.feed_files() == files
    net = foreign_transit.network()
    assert {k: (s["country"], s["owners"]) for k, s in net.stops.items()} == {
        "at:47:2184": ("AT", {"at57"}),  # also gtfs.de's Kufstein: counted by the Austrian feed only
        "at:47:2000": ("AT", {"at57"}),
        "gtfsde:2": ("DE", {"gtfsde"}),  # outside the borders known, in Germany's area only
        "gtfsde:3": ("DE", {"gtfsde"}),  # in both areas: the feed's country
    }
    assert net.keys["gtfsde"]["gtfsde:1"] == "at:47:2184"
    kufstein = net.stops["at:47:2184"]
    assert kufstein["modes"] == {"bus", "train"} and set(kufstein["sources"]) == {"at57", "gtfsde"}
    assert (kufstein["lat"], kufstein["lon"], kufstein["name"]) == (47.5830, 12.1660, "Kufstein Bahnhof")  # Pat:…

    stops = {s["ref"]: s for s in foreign_transit.read_stops()}
    assert stops["at:47:2184"]["mode"] == "bus,train" and stops["gtfsde:3"]["country"] == "DE"
    assert set(stops) == set(net.stops)
    # the keys saved for load_timetable: used until a feed changes
    monkeypatch.setattr(foreign_transit, "network", lambda files=None: pytest.fail("matched again"))
    saved = foreign_transit.stop_keys()
    assert saved.keys == net.keys and saved.owned("gtfsde") == {"gtfsde:2", "gtfsde:3"}
    write_zip(files[1].path, DE_FEED | {"routes.txt": DE_FEED["routes.txt"].replace("RB54", "RB")})
    with pytest.raises(pytest.fail.Exception, match="matched again"):
        foreign_transit.stop_keys()


def test_german_station_in_austria_kept_without_austrian_feeds(files, borders):
    stops = {s["ref"]: s["country"] for s in foreign_transit.read_stops(files[1:])}
    assert stops == {"gtfsde:1": "AT", "gtfsde:2": "DE", "gtfsde:3": "DE"}


def test_open_feed_cut_to_each_year_and_unknown_elsewhere(files, borders, tmp_path):
    keep = {"at:47:2184", "at:47:2000", "gtfsde:2", "gtfsde:3"}
    feeds = list(load_timetable.read_all(keep, SINCE))
    assert [(f.year, f.start, f.end) for f in feeds] == [(2027, date(2026, 12, 13), date(2027, 12, 11)),
                                                        (2028, date(2027, 12, 13), date(2028, 12, 11))]
    first, newest = feeds
    lines = {(r["uic"], r["line"]) for r in first.lines if isinstance(r["uic"], str)}
    # Kufstein's lines and calls from the Austrian feed only; the German trips still call there
    assert lines == {("at:47:2000", "at57:rex"), ("at:47:2184", "at57:rex"), ("gtfsde:2", "gtfsde:rb"),
                     ("gtfsde:3", "gtfsde:bus")}
    assert not any(stop == "at:47:2184" and s.startswith("gtfsde:") for stop, s, *_ in first.stop_services)
    assert ("RB54", "", False, {"gtfsde:wk"}, (("gtfsde:2", 360), ("at:47:2184", 400))) in first.patterns
    assert ("9494", "", False, {"gtfsde:wk"}, (("gtfsde:3", 480), ("at:47:2184", 490))) in first.patterns
    # the German feed's days only; the rest of the year unknown at its stops, not at the Austrian ones
    covered = first.services["gtfsde:wk"]
    assert covered == ((1 << 31) - 1) << (date(2027, 1, 5) - first.start).days
    assert first.services["unknown:gtfsde"] == ((1 << ((first.end - first.start).days + 1)) - 1) & ~covered
    unknown = {stop for stop, s, *_ in first.stop_services if s.startswith("unknown:")}
    assert unknown == {"gtfsde:2", "gtfsde:3"}
    # next year: the German lines from its 30 days (stop_line, for the edges); every foreign stop unknown
    assert {(r["uic"], r["line"]) for r in newest.lines if isinstance(r["uic"], str)} == \
        {("gtfsde:2", "gtfsde:rb"), ("gtfsde:3", "gtfsde:bus")}
    assert {stop for stop, s, *_ in newest.stop_services if s.startswith("unknown:")} == set(keep)

    conn = connect(tmp_path / "test.db")
    load_stops.load(conn, foreign_transit.read_stops())
    places = dict(conn.execute("SELECT ref, place_id FROM stop").fetchall())
    hut = conn.execute("INSERT INTO place (kind, name, e_lv95, n_lv95, lat, lon) "
                       "VALUES ('hut', 'h', 2600000, 1200000, 46.5, 7.9)").lastrowid
    for place in places.values():
        conn.execute("INSERT INTO edge (from_place, to_place, category_limit, max_category, time_min, source) "
                     "VALUES (?, ?, 1, 1, 60, 'tlm_routed')", (place, hut))
    conn.commit()
    load_timetable.load(conn, feeds)
    timetable = transit.load(conn)
    rosenheim, kufstein = places["gtfsde:2"], places["at:47:2184"]
    assert timetable.day(rosenheim, date(2027, 1, 10)) == transit.StopDay(True, 1, 360, 360)
    assert timetable.day(rosenheim, date(2027, 3, 1)) == transit.UNKNOWN  # after the German feed's 30 days
    assert timetable.day(rosenheim, date(2027, 3, 1)).served
    assert timetable.day(kufstein, date(2027, 3, 1)) == transit.StopDay(True, 1, 440, 440)  # the Austrian train
    assert timetable.day(kufstein, date(2028, 3, 1)) == transit.UNKNOWN  # no Austrian dataset for 2028
    ride = timetable.rides_from(rosenheim, date(2027, 1, 10), 0, 1440)[kufstein]  # across the border
    assert (ride.departure, ride.arrival, ride.label) == (360, 400, "RB54")


RAW = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\n1,TT,https://tt.it,Europe/Rome\n",
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nin1,Trento,46.07,11.12\nin2,Sardagna,46.07,11.10\n"
                 "out,Verona,45.44,10.99\n",
    "routes.txt": "route_id,route_short_name,route_type\nfu,Fu,5\nbus,B,3\nfar,X,3\n",
    "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                    "a,1,1,1,1,1,1,1,20270101,20270630\nb,1,1,1,1,1,1,1,20270101,20270630\n",
    "trips.txt": "route_id,service_id,trip_id\nfu,a,f1\nbus,a,b1\nfar,b,x1\n",
    "shapes.txt": "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\ns,46,11,1\n",
    "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                      "f1,08:00:00,08:00:00,in1,1\nf1,08:05:00,08:05:00,in2,2\n"
                      "b1,07:00:00,07:00:00,out,1\nb1,08:30:00,08:30:00,in1,2\n"
                      "x1,09:00:00,09:00:00,out,1\n",
}


def test_extract_cuts_to_the_box(tmp_path):
    raw = write_zip(tmp_path / "raw.zip", RAW)
    summary = transit_feeds.extract(raw, tmp_path / "cut.zip", (10.3, 45.6, 12.6, 47.15), {"5": "6"})
    assert summary.startswith("2 stops, 2 trips, 2 routes")
    with zipfile.ZipFile(tmp_path / "cut.zip") as z:
        text = {n: z.read(n).decode() for n in z.namelist()}
    assert "shapes.txt" not in text and "agency.txt" in text
    assert "Verona" not in text["stops.txt"] and "x1" not in text["trips.txt"] and "\nb," not in text["calendar.txt"]
    assert text["routes.txt"].splitlines()[1:] == ["fu,Fu,6", "bus,B,3"]  # the cableway as aerial lift
    # each trip's first call stays (frequencies count from it), the others outside the box go
    assert text["stop_times.txt"].splitlines()[1:] == [
        "f1,08:00:00,08:00:00,in1,1", "f1,08:05:00,08:05:00,in2,2",
        "b1,07:00:00,07:00:00,out,1", "b1,08:30:00,08:30:00,in1,2"]
    feed = load_timetable.read(tmp_path / "cut.zip", date(2026, 12, 1), source="ttu", year=2027)
    assert {r["line"] for r in feed.lines} == {"fu", "bus"}


@pytest.fixture
def source(tmp_path, monkeypatch):
    sources = {"ttu": Source("IT", "https://example.org/feed.zip", (10.3, 45.6, 12.6, 47.15))}
    monkeypatch.setattr(foreign_transit, "GTFS_DIR", tmp_path)
    monkeypatch.setattr(foreign_transit, "SOURCES", sources)
    monkeypatch.setattr(transit_feeds, "SOURCES", sources)
    return sources


def test_download_only_when_changed(tmp_path, monkeypatch, source):
    requests, headers = [], {"etag": '"1"'}
    raw = write_zip(tmp_path / "raw.zip", RAW).read_bytes()

    def handler(request):
        requests.append(request.method)
        return httpx.Response(200, headers=headers | {"content-length": str(len(raw))},
                              content=b"" if request.method == "HEAD" else raw)

    real_client = httpx.Client
    monkeypatch.setattr(transit_feeds.httpx, "Client",
                        lambda **kw: real_client(**kw, transport=httpx.MockTransport(handler)))
    today = date(2027, 3, 1)
    assert transit_feeds.download("ttu", today=today)
    assert foreign_transit.path("ttu") == tmp_path / "it" / "ttu.zip"
    assert "Verona" not in zipfile.ZipFile(tmp_path / "it" / "ttu.zip").read("stops.txt").decode()
    assert not transit_feeds.download("ttu", today=today) and requests == ["HEAD", "GET", "HEAD"]
    headers["etag"] = '"2"'
    assert transit_feeds.download("ttu", today=today)
    # without ETag and Last-Modified: by size, and again after a few days
    headers.clear()
    assert transit_feeds.download("ttu", today=today)
    assert not transit_feeds.download("ttu", today=today + timedelta(days=1))
    assert transit_feeds.download("ttu", today=today + timedelta(days=transit_feeds.MAX_AGE_DAYS))
    assert not list((tmp_path / "it").glob("*.download")) and not list((tmp_path / "it").glob("*.part"))

    def offline(request):
        raise httpx.ConnectError("no network")
    monkeypatch.setattr(transit_feeds.httpx, "Client",
                        lambda **kw: real_client(**kw, transport=httpx.MockTransport(offline)))
    assert not transit_feeds.download("ttu", today=today + timedelta(days=30))  # skipped, the file kept
    assert (tmp_path / "it" / "ttu.zip").exists()


def test_stale_files_removed(tmp_path, source):
    (tmp_path / "it").mkdir()
    for name in ("ttu.zip", "ttu.json", "ttu.stations.json", "ttu.part", "old.zip", "old.json"):
        (tmp_path / "it" / name).write_text("x")
    transit_feeds.remove_stale()
    assert sorted(p.name for p in (tmp_path / "it").iterdir()) == ["ttu.json", "ttu.stations.json", "ttu.zip"]
