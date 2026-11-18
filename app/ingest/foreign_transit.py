"""Public transport abroad: the GTFS feeds of the regions (regions.py), their stops, and how the stops of different
feeds fit together.

    uv run python -m app.ingest.foreign_transit     # load the stops abroad from the feeds on disk

Feeds: Austria (at_transit.py: one dataset per region and timetable year, data/gtfs/at/{dataset}_{year}.zip), and
the open feeds of Germany and Italy (transit_feeds.py: data/gtfs/{de,it}/{source}.zip). load_timetable merges them
into each year's Swiss timetable.

Stops are stations (parent stations where a feed has them) keyed by stop.ref: the station's global id where the feed
gives one (`at:47:2184`, `it:22021:1389`; the Austrian and South Tyrolean feeds use them), else the feed's own station
id after the source (`gtfsde:194752`, `tte:12`). Stations are kept inside a region's area and outside Switzerland and
Liechtenstein (those come from the Swiss sources), and matched in three passes:
  1. global ids in their feed's country (the Austrian datasets' at:…, South Tyrol's it:…): stops;
  2. a feed's own ids in its country (gtfs.de in Germany, Trentino), source by source: the stop of a source before
     within MATCH_M that shares a mode, else a new stop;
  3. all others, i.e. stations in another feed's country (gtfs.de's Kufstein, the Austrian feeds' Garmisch or
     Brennero, South Tyrol's Matrei): the stop with the same global id, else as in 2, else a new stop only in a
     country without feeds of its own (the feeds of the country are taken to list all its stations).
So each feed brings the stops of its own country, and a border station listed by two feeds is one stop (same key),
where the trips of both meet.

Each stop is owned by the feeds of its country that serve it (or, if none, by all feeds that serve it): only they
count its lines and daily calls (stop_line, stop_service), so a border station listed by two feeds doesn't count its
trains twice. Trips keep their calls at every stop, so rides across the border connect. On days none of its owners'
timetables covers (gtfs.de's 30 days, a season not published yet), a stop's timetable is unknown, not "no service".
Limitations: a train in two feeds (e.g. a Brenner train in the Austrian railway dataset and South Tyrol's) is listed
twice in a border station's departures; stops a feed no longer lists (or that now match another feed's stop) stay
in the stop table without timetable, as load_stops never deletes stops (edges and overrides refer to them).
"""

import csv
import io
import json
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import shapely
from pyproj import Transformer

from app.db import ROOT, connect
from app.ingest import countries, load_stops, regions

GTFS_DIR = ROOT / "data/gtfs"
AT_DIR = GTFS_DIR / "at"  # Austrian datasets, {dataset}_{year}.zip (at_transit.py downloads them)
GLOBAL_ID = re.compile(r"[a-z]{2}:\d+:\d+")  # a station's global id: at:47:1234 (quays: at:47:1234:1:2)
PARENT_ID = re.compile(r"(?:P|Parent)?([a-z]{2}:\d+:\d+)")  # parent stations: Pat:47:1234 (Austria), Parentat:… (STA)
STATIONS_VERSION = 3  # of the cached stations (stations()): bump when station_keys or route_mode change
# South Tyrol names its stops after a quay: "Merano, Stazione di Merano Linee 3 - N13, dir. Funivia Merano 2000"
PLATFORM_NOTE = re.compile(r"\s+(Linee?|Linea|Linie|Linien)\s.*$")
MATCH_M = 150.0  # stations of different feeds this close, with a mode in common, are the same stop
WGS84_TO_LV95 = Transformer.from_crs("EPSG:4326", "EPSG:2056", always_xy=True)


@dataclass(frozen=True)
class Source:
    """An open feed downloaded by transit_feeds.py, cut to the stops in bbox (WGS84 west, south, east, north)."""

    country: str
    url: str
    bbox: tuple[float, float, float, float]
    route_types: dict[str, str] = field(default_factory=dict)  # fixes applied when cut, e.g. a cableway given as 5


GERMAN_ALPS = (9.5, 47.2, 13.2, 48.0)  # around the region DE: Bavaria south of 47.85°N
TRENTINO_SOUTH_TYROL = (10.3, 45.6, 12.6, 47.15)  # around the region IT
# Source → feed, in the order their stations are matched (see above). Attribution in web/js/info.js.
SOURCES = {
    "gtfsde": Source("DE", "https://download.gtfs.de/germany/free/latest.zip", GERMAN_ALPS),
    "sta": Source("IT", "https://gtfs.api.opendatahub.com/v1/dataset/sta-time-tables/raw", TRENTINO_SOUTH_TYROL),
    "tte": Source("IT", "https://www.trentinotrasporti.it/opendata/google_transit_extraurbano_tte.zip",
                  TRENTINO_SOUTH_TYROL),
    "ttu": Source("IT", "https://www.trentinotrasporti.it/opendata/google_transit_urbano_tte.zip",
                  TRENTINO_SOUTH_TYROL, {"5": "6"}),  # the Trento–Sardagna cableway as cable tram
}
# GTFS route type → stop mode (the words of load_stops.MODES): basic types, extended ones by exact type, then by
# hundreds (None: not a mode HutHop knows, e.g. air)
ROUTE_MODES = {0: "tram", 1: "metro", 2: "train", 3: "bus", 4: "boat", 5: "tram", 6: "cableway", 7: "funicular",
               11: "bus", 12: "train", 116: "rack_railway", 1303: "elevator", 1304: "chairlift", 1305: "chairlift"}
ROUTE_MODES_BY_HUNDRED = {1: "train", 2: "bus", 4: "metro", 5: "metro", 6: "metro", 7: "bus", 8: "bus", 9: "tram",
                          10: "boat", 12: "boat", 13: "cableway", 14: "funicular", 15: "bus"}
MODE_ORDER = list(load_stops.MODES.values())


def path(source: str) -> Path:
    return GTFS_DIR / SOURCES[source].country.lower() / f"{source}.zip"


@dataclass(frozen=True)
class FeedFile:
    source: str  # "at57" (Austrian dataset 57), "gtfsde", "sta": the prefix of the feed's own stop ids
    country: str
    path: Path
    year: int | None = None  # timetable year of an Austrian dataset; the other feeds are cut by date (load_timetable)


def feed_files() -> list[FeedFile]:
    """The foreign feeds on disk: the Austrian datasets (by year, then dataset), then the open feeds."""
    files = []
    for p in AT_DIR.glob("*_*.zip"):
        dataset, year = map(int, p.stem.split("_"))
        files.append(FeedFile(f"at{dataset}", "AT", p, year))
    files.sort(key=lambda f: (f.year, f.path.name))
    return files + [FeedFile(name, s.country, path(name)) for name, s in SOURCES.items() if path(name).exists()]


def rows(z: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    if name not in z.namelist():
        return iter(())
    return csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8-sig"))


def station_keys(z: zipfile.ZipFile, source: str) -> dict[str, str]:
    """Stop id → station key in a feed: the station's global id (`at:47:1234` for the quay `at:47:1234:1:2` and the
    parent station `Pat:47:1234`), else its parent station's or its own id after the source (`at57:…`, `gtfsde:…`)."""
    keys = {}
    for r in rows(z, "stops.txt"):
        root = r.get("parent_station") or r["stop_id"]
        match = PARENT_ID.fullmatch(root) or GLOBAL_ID.match(r["stop_id"]) or PARENT_ID.match(r["stop_id"])
        keys[r["stop_id"]] = (match[1] if match.re is PARENT_ID else match[0]) if match else f"{source}:{root}"
    return keys


def route_mode(route_type: str) -> str | None:
    t = int(route_type or 3)
    return ROUTE_MODES.get(t) or ROUTE_MODES_BY_HUNDRED.get(t // 100)


def stations(f: FeedFile) -> dict[str, list]:
    """A feed's stations, by key: [name, lat, lon, is a parent station, modes of the routes calling there]; a parent
    station's position and name before a quay's. Kept next to the feed (…stations.json) until the feed changes:
    reading the modes takes a pass over its stop_times."""
    cache = f.path.with_suffix(".stations.json")
    stamp = [f.path.stat().st_size, f.path.stat().st_mtime_ns, STATIONS_VERSION]
    if cache.exists():
        cached = json.loads(cache.read_text())
        if cached["stamp"] == stamp:
            return cached["stations"]
    out: dict[str, list] = {}
    modes: dict[str, set[str]] = {}
    with zipfile.ZipFile(f.path) as z:
        keys = station_keys(z, f.source)
        for r in rows(z, "stops.txt"):
            if not r.get("stop_lat") or not r.get("stop_lon"):
                continue
            key, is_station = keys[r["stop_id"]], r.get("location_type") == "1"
            if key not in out or (is_station and not out[key][3]):
                name = PLATFORM_NOTE.sub("", r["stop_name"])
                out[key] = [name, float(r["stop_lat"]), float(r["stop_lon"]), is_station, []]
        route = {r["route_id"]: route_mode(r.get("route_type", "")) for r in rows(z, "routes.txt")}
        trip = {r["trip_id"]: route.get(r["route_id"]) for r in rows(z, "trips.txt")}
        reader = csv.reader(io.TextIOWrapper(z.open("stop_times.txt"), encoding="utf-8-sig"))
        header = next(reader)
        i_trip, i_stop = header.index("trip_id"), header.index("stop_id")
        for row in reader:
            mode, key = trip.get(row[i_trip]), keys.get(row[i_stop])
            if mode and key:
                modes.setdefault(key, set()).add(mode)
    for key, station in out.items():
        station[4] = sorted(modes.get(key, ()), key=MODE_ORDER.index)
    cache.write_text(json.dumps({"stamp": stamp, "stations": out}))
    return out


@dataclass
class Network:
    """The stops of all foreign feeds: by stop key, {name, lat, lon, e_lv95, n_lv95, country, modes, sources, owners}
    (sources: the feeds serving it); and per source, its station keys → stop keys."""

    stops: dict[str, dict] = field(default_factory=dict)
    keys: dict[str, dict[str, str]] = field(default_factory=dict)

    def owned(self, source: str) -> set[str]:
        return {key for key, s in self.stops.items() if source in s["owners"]}


def network(files: list[FeedFile] | None = None) -> Network:
    """Match the stations of the feeds (default: all on disk) into stops (see the module docstring). Empty without
    a region area or the borders."""
    files = feed_files() if files is None else files
    feed_countries = {f.country for f in files}
    if not files or not any(regions.area(c) is not None for c in feed_countries) or not countries.borders():
        if files:
            print("foreign transit: region polygons (regions) or borders (countries) missing, no stops abroad")
        return Network()
    # Every served station of every feed: (source, country of its feed, key, station), newer files of a source first
    records = []
    for f in sorted(files, key=lambda f: -(f.year or 0)):
        records += [(f.source, f.country, key, s) for key, s in stations(f).items() if s[4]]
    lat = np.array([s[1] for *_, s in records])
    lon = np.array([s[2] for *_, s in records])
    e, n = WGS84_TO_LV95.transform(lon, lat)
    in_area = {c: shapely.contains_xy(a, e, n) for c in sorted(feed_countries) if (a := regions.area(c)) is not None}
    inside = np.logical_or.reduce(list(in_area.values()))
    # the country: from the borders, else the region's if in one region's area only (e.g. before its border is known)
    country = [c or (next(iter(r)) if len(r := [code for code, a in in_area.items() if a[i]]) == 1 else None)
               for i, c in enumerate(countries.country_at(e, n))]
    order: dict[str, int] = {}
    for i, f in enumerate(files):
        order.setdefault(f.source, i)

    # In three passes: global ids in their feed's country, a feed's own ids in its country, all others
    passes: tuple[list, list, list] = ([], [], [])
    for i in sorted(range(len(records)), key=lambda i: order[records[i][0]]):
        source, home, key, s = records[i]
        if not inside[i] or country[i] in countries.COUNTRIES:
            continue
        is_global = GLOBAL_ID.fullmatch(key) is not None
        c = country[i] or (key[:2].upper() if is_global else home)
        passes[0 if c == home and is_global else 1 if c == home else 2].append((i, c))
    net = Network()
    for i, c in passes[0]:
        source, home, key, s = records[i]
        stop = net.stops.get(key)
        if stop is None or (s[3] and not stop["station"]):  # a parent station's name and position before a quay's
            net.stops[key] = _stop(s, e[i], n[i], c) | ({} if stop is None else
                                                        {"modes": stop["modes"], "sources": stop["sources"]})
        _add(net, key, source, home, key, s)
    for items in passes[1:]:
        for source in dict.fromkeys(records[i][0] for i, _ in items):
            mine = [(i, c) for i, c in items if records[i][0] == source]
            keys = list(net.stops)  # the stops of the sources before
            tree = shapely.STRtree(shapely.points([net.stops[k]["e_lv95"] for k in keys],
                                                  [net.stops[k]["n_lv95"] for k in keys]))
            points = shapely.points(e[[i for i, _ in mine]], n[[i for i, _ in mine]])
            near: dict[int, list[int]] = {}
            for j, k in zip(*tree.query(points, predicate="dwithin", distance=MATCH_M)):
                near.setdefault(int(j), []).append(int(k))
            for j, (i, c) in enumerate(mine):
                _, home, key, s = records[i]
                if key in net.stops:  # the same global id, or an older file of the source
                    _add(net, key, source, home, key, s)
                    continue
                match = min((k for k in near.get(j, ()) if net.stops[keys[k]]["modes"] & set(s[4])),
                            key=lambda k: shapely.distance(points[j], tree.geometries[k]), default=None)
                if match is not None:
                    _add(net, keys[match], source, home, key, s)
                elif c == home or c not in feed_countries:
                    net.stops[key] = _stop(s, e[i], n[i], c)
                    _add(net, key, source, home, key, s)

    for stop in net.stops.values():
        stop["owners"] = {src for src, c in stop["sources"].items() if c == stop["country"]} or set(stop["sources"])
    return net


def _stop(station: list, e: float, n: float, country: str) -> dict:
    return {"name": station[0], "lat": station[1], "lon": station[2], "e_lv95": float(e), "n_lv95": float(n),
            "country": country, "station": station[3], "modes": set(), "sources": {}}


def _add(net: Network, stop_key: str, source: str, home: str, key: str, station: list) -> None:
    """A feed's station `key` is the stop `stop_key`."""
    stop = net.stops[stop_key]
    stop["modes"] |= set(station[4])
    stop["sources"][source] = home
    net.keys.setdefault(source, {}).setdefault(key, stop_key)


def _keys_path() -> Path:
    return GTFS_DIR / "stop_keys.json"


def _stamp(files: list[FeedFile]) -> list:
    return [[str(f.path), f.path.stat().st_size, f.path.stat().st_mtime_ns] for f in files]


def _save_keys(net: Network, files: list[FeedFile]) -> None:
    _keys_path().write_text(json.dumps({"files": _stamp(files), "keys": net.keys,
                                        "owners": {k: sorted(s["owners"]) for k, s in net.stops.items()}}))


def stop_keys() -> Network:
    """The stop keys and owners of the feeds on disk (a Network without the stops' names and positions), as
    read_stops last matched them (saved in data/gtfs/stop_keys.json), so load_timetable keys the timetables like the
    stops loaded, without matching them again (~100 MB). Matched again if the feeds changed since."""
    files = feed_files()
    saved = json.loads(_keys_path().read_text()) if _keys_path().exists() else {}
    if saved.get("files") != _stamp(files):
        net = network(files)
        if files:
            _save_keys(net, files)
        saved = {"keys": net.keys, "owners": {k: sorted(s["owners"]) for k, s in net.stops.items()}}
        del net
    return Network({k: {"owners": set(o)} for k, o in saved["owners"].items()}, saved["keys"])


def read_stops(files: list[FeedFile] | None = None) -> list[dict]:
    """The stops abroad (default: from all feeds on disk, and their keys saved for stop_keys) as load_stops.load takes
    them (with `ref`)."""
    net = network(files)
    if files is None and net.stops:
        _save_keys(net, feed_files())
    stops = [{"ref": key, "name": s["name"], "mode": ",".join(sorted(s["modes"], key=MODE_ORDER.index)),
              "e_lv95": s["e_lv95"], "n_lv95": s["n_lv95"], "lat": round(s["lat"], 6), "lon": round(s["lon"], 6),
              "country": s["country"]} for key, s in net.stops.items()]
    by_country: dict[str, int] = {}
    for s in stops:
        by_country[s["country"]] = by_country.get(s["country"], 0) + 1
    print(f"foreign transit: {len(stops):,} stops abroad {by_country}")
    return stops


def main() -> None:
    print(f"loaded {load_stops.load(connect(), read_stops())} stops abroad")


if __name__ == "__main__":
    main()
