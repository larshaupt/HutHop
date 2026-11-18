"""Open timetables of Germany and Italy (GTFS, no account needed), cut to the surroundings of their region.

    uv run python -m app.ingest.transit_feeds                # download what changed, then load the stops abroad
    uv run python -m app.ingest.transit_feeds --no-download

Sources (foreign_transit.SOURCES):
  - gtfsde: gtfs.de "Germany Full", free feed (CC BY 4.0, data DELFI e.V.), ~280 MB, updated weekly. The free feed
    only holds the next ~30 days: German stops count as "timetable unknown" on later dates (foreign_transit).
  - sta: South Tyrol (STA – Südtiroler Transportstrukturen, CC0, through the Open Data Hub), ~150 MB, rebuilt
    nightly: buses, regional trains (Brenner, Vinschgau, Pustertal) and STA's lifts; one timetable year at a time.
  - tte, ttu: Trentino Trasporti, extra-urban (buses, Trento–Malè railway) and urban (Trento, Rovereto, Alto Garda,
    Pergine; the Trento–Sardagna cableway) networks (CC BY 4.0), a few MB each, one season at a time.
Each is downloaded only when its ETag, Last-Modified or size changed (South Tyrol has neither ETag nor
Last-Modified: also after MAX_AGE_DAYS), and cut to its bounding box (stops, the trips
calling there with only those calls, and their routes and calendars; no shapes): data/gtfs/{de,it}/{source}.zip,
a few MB, read quickly by load_timetable. Files of sources no longer listed are deleted.
"""

import argparse
import csv
import io
import json
import zipfile
from datetime import date
from pathlib import Path

import httpx

from app.clients import USER_AGENT
from app.db import connect
from app.ingest import foreign_transit, load_stops, regions
from app.ingest.foreign_transit import SOURCES, path

KEEP_FILES = ("agency.txt", "feed_info.txt", "attributions.txt")  # copied as they are
EXTRACT_VERSION = 1  # bump when extract() changes, to cut the feeds again
MAX_AGE_DAYS = 6  # a feed served without ETag or Last-Modified (South Tyrol) is fetched again after this, or if its
# size changed


def _edition_path(source: str) -> Path:
    return path(source).with_suffix(".json")


def _edition(source: str, headers: httpx.Headers) -> dict:
    s = SOURCES[source]
    return {"url": s.url, "etag": headers.get("etag"), "last_modified": headers.get("last-modified"),
            "size": headers.get("content-length"), "bbox": list(s.bbox), "extract": EXTRACT_VERSION}


def download(source: str, force: bool = False, today: date | None = None) -> bool:
    """Download a source if it changed since the last time, and cut it to its bounding box. True if updated; False
    if up to date or offline."""
    today = today or date.today()
    s, target = SOURCES[source], path(source)
    client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=600, follow_redirects=True)
    try:
        head = client.head(s.url).raise_for_status().headers
    except httpx.TransportError as e:
        print(f"{source}: offline ({e}), kept what is on disk")
        return False
    known = json.loads(_edition_path(source).read_text()) if _edition_path(source).exists() else {}
    downloaded = date.fromisoformat(known.pop("downloaded", "2000-01-01"))
    recent = head.get("etag") or head.get("last-modified") or (today - downloaded).days < MAX_AGE_DAYS
    if known == _edition(source, head) and recent and target.exists() and not force:
        print(f"{source}: up to date ({head.get('last-modified') or head.get('content-length')})")
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = target.with_suffix(".download")
    print(f"{source}: downloading {s.url} ({int(head.get('content-length') or 0) / 1e6:.0f} MB)")
    try:
        with client.stream("GET", s.url) as res, raw.open("wb") as f:
            res.raise_for_status()
            edition = _edition(source, res.headers)
            for chunk in res.iter_bytes(1 << 20):
                f.write(chunk)
        partial = target.with_suffix(".part")
        print(f"{source}: {extract(raw, partial, s.bbox, s.route_types)}")
        partial.replace(target)
        _edition_path(source).write_text(json.dumps(edition | {"downloaded": today.isoformat()}, indent=1))
    finally:
        raw.unlink(missing_ok=True)
    return True


def remove_stale() -> None:
    """Delete the files of sources no longer listed (and leftovers of interrupted downloads)."""
    for country in {s.country for s in SOURCES.values()}:
        for p in (foreign_transit.GTFS_DIR / country.lower()).glob("*"):
            name = p.name.split(".")[0]
            if name not in SOURCES or SOURCES[name].country != country or p.suffix in (".download", ".part"):
                p.unlink()


def _reader(z: zipfile.ZipFile, name: str):
    return csv.reader(io.TextIOWrapper(z.open(name), encoding="utf-8-sig", newline=""))


def _filter(z: zipfile.ZipFile, out: zipfile.ZipFile, name: str, column: str, keep: set[str],
            collect: dict[str, set[str]] | None = None, route_types: dict[str, str] | None = None) -> int:
    """Copy the rows of a file whose `column` is in `keep`; collect the values of other columns from them."""
    if name not in z.namelist():
        return 0
    reader = _reader(z, name)
    header = next(reader)
    i = header.index(column)
    collected = [(header.index(c), values) for c, values in (collect or {}).items() if c in header]
    i_type = header.index("route_type") if route_types and "route_type" in header else None
    n = 0
    with io.TextIOWrapper(out.open(name, "w", force_zip64=True), encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        for row in reader:
            if row[i] in keep:
                if i_type is not None:
                    row[i_type] = route_types.get(row[i_type], row[i_type])
                writer.writerow(row)
                n += 1
                for j, values in collected:
                    values.add(row[j])
    return n


def extract(raw: Path, out: Path, bbox: tuple[float, float, float, float],
            route_types: dict[str, str] | None = None) -> str:
    """Cut a GTFS feed to the stops in `bbox` (west, south, east, north; with their parent stations), the trips calling
    there (only those calls, plus each trip's first one, which frequencies.txt counts from), and the routes, calendars
    and frequencies of those trips; route types changed as in `route_types`. Streams stop_times.txt (2 GB for
    Germany) once; rows must be grouped by trip, as load_timetable reads them. Returns a summary."""
    west, south, east, north = bbox
    with zipfile.ZipFile(raw) as z, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as o:
        reader = _reader(z, "stops.txt")
        header = next(reader)
        i_id, i_lat, i_lon = header.index("stop_id"), header.index("stop_lat"), header.index("stop_lon")
        i_parent = header.index("parent_station") if "parent_station" in header else None
        stops: set[str] = set()
        for row in reader:
            try:
                inside = west <= float(row[i_lon]) <= east and south <= float(row[i_lat]) <= north
            except ValueError:  # e.g. generic nodes without position
                inside = False
            if inside:
                stops.add(row[i_id])
                if i_parent is not None and row[i_parent]:
                    stops.add(row[i_parent])
        _filter(z, o, "stops.txt", "stop_id", stops)

        trips: set[str] = set()
        reader = _reader(z, "stop_times.txt")
        header = next(reader)
        i_trip, i_stop = header.index("trip_id"), header.index("stop_id")
        regrouped = 0
        with io.TextIOWrapper(o.open("stop_times.txt", "w", force_zip64=True), encoding="utf-8", newline="") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(header)
            current, first, calls = None, None, []

            def flush() -> None:
                nonlocal regrouped
                if calls:
                    if current in trips:
                        regrouped += 1
                    trips.add(current)
                    writer.writerows([first] + calls if calls[0] is not first else calls)

            for row in reader:
                if row[i_trip] != current:
                    flush()
                    current, first, calls = row[i_trip], row, []
                if row[i_stop] in stops:
                    calls.append(row)
            flush()
        if regrouped:
            print(f"warning: stop_times.txt of {raw.name} is not grouped by trip ({regrouped} trips split)")

        routes, services = set(), set()
        n_trips = _filter(z, o, "trips.txt", "trip_id", trips, {"route_id": routes, "service_id": services})
        _filter(z, o, "routes.txt", "route_id", routes, route_types=route_types)
        _filter(z, o, "calendar.txt", "service_id", services)
        _filter(z, o, "calendar_dates.txt", "service_id", services)
        _filter(z, o, "frequencies.txt", "trip_id", trips)
        for name in KEEP_FILES:
            if name in z.namelist():
                o.writestr(name, z.read(name).decode("utf-8-sig"))
    return f"{len(stops):,} stops, {n_trips:,} trips, {len(routes):,} routes in the region's surroundings"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-download", action="store_true", help="use the feeds in data/gtfs/{de,it}/")
    parser.add_argument("--force", action="store_true", help="download and cut the feeds again")
    args = parser.parse_args()
    if not args.no_download:
        regions.download_polygons()
        remove_stale()
        for source in SOURCES:
            download(source, args.force)
    print(f"loaded {load_stops.load(connect(), foreign_transit.read_stops())} stops abroad")


if __name__ == "__main__":
    main()
