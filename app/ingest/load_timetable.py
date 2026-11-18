"""Load the official Swiss timetable (GTFS, opentransportdata.swiss), and the foreign ones on disk (foreign_transit.py):
which lines serve each stop, how often, and on which days from when to when.

    uv run python -m app.ingest.load_timetable                # download new editions (~90 MB each), then load
    uv run python -m app.ingest.load_timetable --no-download

Keeps this year's and (once published, in autumn) next year's timetable, so tours can be planned for any date
from now to the end of next season. Reads each zip directly (stop_times.txt alone is 1.2 GB unpacked) in one
streaming pass: ~25 s and ~600 MB RAM per year. Merged into the Swiss timetable of a year (merge, read_all): the
Austrian datasets of that year, and the days of the open feeds of Germany and Italy (transit_feeds.py) within it.
On the days a foreign stop's feeds don't cover (Germany's 30 days, a year not published yet), its timetable is
unknown (stop_service.calls UNKNOWN_CALLS), not "no service". Stores
  - stop_line: lines per stop with summer departures (newest timetable; for an open feed that doesn't reach it, e.g.
    Germany's, from the days it has), for build_edges (closest stop of each line per hut) and the connection list;
  - timetable, timetable_service, stop_service: per stop and service pattern the first and last time and number
    of calls, and the days each pattern runs, so the planner knows whether a stop is served on a date;
  - trip_pattern, trip_call: the trips through the stops that have walking connections (`ride_stops`, from the
    edges built so far), with their times there: stop timetables and rides within a tour.
"""

import argparse
import csv
import hashlib
import io
import json
import math
import re
import sqlite3
import sys
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import httpx

from app.clients import USER_AGENT
from app.db import connect
from app.ingest import foreign_transit
from app.ingest.foreign_transit import GTFS_DIR, Network
from app.ingest.foreign_transit import rows as _rows

PERMALINK = "https://data.opentransportdata.swiss/en/dataset/timetable-{year}-gtfs2020/permalink"
UNKNOWN_CALLS = -1  # stop_service.calls of the days a stop's timetable doesn't cover (transit.StopDay.known)
SUMMER = ((7, 1), (8, 31))  # summer departures per line are averaged over the hut season of the timetable year
LIFT_ROUTE_TYPES = {6, 7}  # aerial lift, funicular; extended types 1300–1499 (aerial lifts, funiculars) as well


def feed_path(year: int) -> Path:
    return GTFS_DIR / f"gtfs_{year}.zip"


def _edition_path(year: int) -> Path:
    return GTFS_DIR / f"edition_{year}.json"


def download(force: bool = False, today: date | None = None) -> bool:
    """Download this year's and next year's timetable (if published yet) where a new edition is out, and delete
    older years. Returns True if anything was downloaded."""
    year = (today or date.today()).year
    GTFS_DIR.mkdir(parents=True, exist_ok=True)
    _adopt_legacy_file()
    client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=300)
    updated, found = False, False
    for y in (year, year + 1):
        res = client.get(PERMALINK.format(year=y))  # redirects to the current dated file
        if not res.is_redirect:
            continue
        found = True
        href = res.headers["location"]
        known = json.loads(_edition_path(y).read_text()) if _edition_path(y).exists() else None
        if known == {"href": href} and feed_path(y).exists() and not force:
            print(f"timetable {y}: up to date ({href.rsplit('/', 1)[-1]})")
            continue
        print(f"timetable {y}: downloading {href}")
        partial = feed_path(y).with_suffix(".part")
        with client.stream("GET", href, follow_redirects=True) as res, partial.open("wb") as f:
            res.raise_for_status()
            for chunk in res.iter_bytes(1 << 20):
                f.write(chunk)
        partial.replace(feed_path(y))
        _edition_path(y).write_text(json.dumps({"href": href}, indent=1))
        updated = True
    if not found:
        raise RuntimeError(f"no GTFS timetable found for {year} or {year + 1}")
    for old in GTFS_DIR.glob("gtfs_*.zip"):
        if int(old.stem.split("_")[1]) < year:
            old.unlink()
            _edition_path(int(old.stem.split("_")[1])).unlink(missing_ok=True)
    return updated


def _adopt_legacy_file() -> None:
    """Before both years were kept, one timetable was stored as gtfs.zip: keep it under its year's name."""
    legacy, edition = GTFS_DIR / "gtfs.zip", GTFS_DIR / "edition.json"
    if legacy.exists() and edition.exists():
        match = re.search(r"gtfs_fp(\d{4})", json.loads(edition.read_text())["href"])
        if match and not feed_path(int(match[1])).exists():
            legacy.replace(feed_path(int(match[1])))
            edition.replace(_edition_path(int(match[1])))


def feed_files() -> list[Path]:
    """The timetables on disk, oldest first."""
    return sorted(GTFS_DIR.glob("gtfs_*.zip"))


def _minutes(hms: str) -> int:
    """'25:10:00' → 1510 (times after midnight belong to the service day before)."""
    return int(hms[:-6]) * 60 + int(hms[-5:-3])


def _day(yyyymmdd: str) -> date:
    return date(int(yyyymmdd[:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:]))


def line_key(route_id: str) -> str:
    """The route id without the timetable year (`92-121-A-j27-1` → `92-121-A`)."""
    return re.sub(r"-j\d{2}-\d+$", "", route_id)


def line_label(category: str, short_name: str) -> str:
    """`B` + `121` → `B 121`; `R` + `R61` → `R61`."""
    if not category or short_name.startswith(category):
        return short_name or category
    return f"{category} {short_name}".strip()


def service_days(z: zipfile.ZipFile, start: date, end: date) -> dict[str, int]:
    """The days each service runs, as a bit mask: bit i = start + i days (calendar.txt weekdays within its date
    range, plus and minus the exceptions in calendar_dates.txt)."""
    n = (end - start).days + 1
    by_weekday = [0] * 7
    for i in range(n):
        by_weekday[(start + timedelta(i)).weekday()] |= 1 << i
    names = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    days: dict[str, int] = {}
    for r in _rows(z, "calendar.txt"):
        first = max(0, (_day(r["start_date"]) - start).days)
        last = min(n - 1, (_day(r["end_date"]) - start).days)
        in_range = ((1 << (last + 1)) - 1) ^ ((1 << first) - 1) if first <= last else 0
        weekly = 0
        for k, name in enumerate(names):
            if r[name] == "1":
                weekly |= by_weekday[k]
        days[r["service_id"]] = weekly & in_range
    for r in _rows(z, "calendar_dates.txt"):
        i = (_day(r["date"]) - start).days
        if 0 <= i < n:
            bits = days.get(r["service_id"], 0)
            days[r["service_id"]] = bits | (1 << i) if r["exception_type"] == "1" else bits & ~(1 << i)
    return days


def _span(z: zipfile.ZipFile) -> tuple[str, date, date]:
    """Version, first and last day of a feed: from feed_info.txt, else (optional in GTFS) from its calendars."""
    info = next(_rows(z, "feed_info.txt"), {})
    if info.get("feed_start_date") and info.get("feed_end_date"):
        return info.get("feed_version", ""), _day(info["feed_start_date"]), _day(info["feed_end_date"])
    days = [d for r in _rows(z, "calendar.txt") for d in (r["start_date"], r["end_date"])] + \
        [r["date"] for r in _rows(z, "calendar_dates.txt")]
    return info.get("feed_version") or Path(z.filename or "").stem, _day(min(days)), _day(max(days))


@dataclass
class Feed:
    """One year's timetable, as read from its zip, or several merged (merge). Stops are keyed by their uic, or by
    stop.ref for stops without one (abroad)."""

    year: int
    version: str
    start: date
    end: date
    lines: list[dict] = field(default_factory=list)  # {uic (the stop key), line, label, departures (summer calls/day)}
    services: dict[str, int] = field(default_factory=dict)  # service id → day bits (bit i = start + i days)
    stop_services: list[tuple] = field(default_factory=list)  # (stop key, service id, first min, last min, calls)
    patterns: list[tuple] = field(default_factory=list)  # (label, headsign, lift, service ids, ((stop key, min), …))
    source: str = ""  # "" for the Swiss timetable, else the foreign feed's source ("at57": Austrian dataset 57)


def is_lift(route_type: str) -> bool:
    t = int(route_type or 0)
    return t in LIFT_ROUTE_TYPES or 1300 <= t < 1500


def read(path: Path, since: date | None = None, keep: set[int | str] | None = None, source: str = "",
         year: int | None = None, network: Network | None = None, within: tuple[date, date] | None = None) -> Feed:
    """One year's timetable from its zip. Days before `since` (default today) are dropped: tours are never planned
    in the past, and the current year's file holds many past service patterns (e.g. for construction work).
    Trips are kept with their calls at the stops `keep` (stop keys), if given. `source`: "" for the Swiss timetable
    (stops keyed by uic), else a foreign feed (`at57`, `gtfsde`; of timetable `year` if it has one), its stops
    keyed by foreign_transit: as in `network` (its stops' keys; lines and calls counted only at the stops the feed
    owns there), else by station_keys. `within`: the first and last day to read (an open feed cut to a year)."""
    since = since or date.today()
    with zipfile.ZipFile(path) as z:
        version, start, end = _span(z)
        if within:
            start, end = max(start, within[0]), min(end, within[1])
        feed = Feed(year or end.year, version, start, end, source=source)
        all_days = service_days(z, start, end)
        # Summer departures count the whole summer, even once it is past; only the served days stored drop the past
        past = (1 << (since - start).days) - 1 if since > start else 0
        days = {s: bits & ~past for s, bits in all_days.items()}
        first, last = max(start, date(feed.year, *SUMMER[0])), min(end, date(feed.year, *SUMMER[1]))
        if first > last:  # a feed that doesn't cover the summer: average over all of it
            first, last = start, end
        summer_mask = ((1 << ((last - start).days + 1)) - 1) ^ ((1 << (first - start).days) - 1)
        summer_days = {s: (bits & summer_mask).bit_count() for s, bits in all_days.items()}
        n_summer = (last - first).days + 1

        owned = None
        if source:
            stop_keys = foreign_transit.station_keys(z, source)
            if network is not None:
                to_stop = network.keys.get(source, {})
                stop_keys = {stop_id: to_stop[key] for stop_id, key in stop_keys.items() if key in to_stop}
                owned = network.owned(source)
        else:
            stop_keys = {r["stop_id"]: int(r["didok"]) for r in _rows(z, "stops.txt") if r.get("didok", "").isdigit()}
        lines, labels, lifts = {}, {}, set()
        for r in _rows(z, "routes.txt"):
            lines[r["route_id"]] = key = line_key(r["route_id"]) if not source else r["route_id"]
            if source:  # route_desc abroad is free text (if any), not a category like B or IR
                labels.setdefault(key, r.get("route_short_name") or r.get("route_long_name", ""))
            else:
                labels.setdefault(key, line_label(r["route_desc"], r["route_short_name"]))
            if is_lift(r.get("route_type", "")):
                lifts.add(key)
        # Trips given as "every N minutes from … to …": runs, first and last start (minutes)
        frequency: dict[str, list[int]] = {}
        for r in _rows(z, "frequencies.txt"):
            begin, stop = _minutes(r["start_time"]), _minutes(r["end_time"])
            headway = int(r["headway_secs"]) / 60
            runs = max(1, math.ceil((stop - begin) / headway))
            f = frequency.setdefault(r["trip_id"], [0, begin, begin])
            f[0] += runs
            f[1] = min(f[1], begin)
            f[2] = max(f[2], round(begin + (runs - 1) * headway))
        trips = {}  # trip → (line, service, frequency, headsign): one dict, Switzerland has a million trips
        for r in _rows(z, "trips.txt"):
            if all_days.get(r["service_id"]):
                trips[r["trip_id"]] = (lines[r["route_id"]], sys.intern(r["service_id"]), frequency.get(r["trip_id"]),
                                       sys.intern(r.get("trip_headsign", "")))

        patterns: dict[tuple, set[str]] = {}  # (line, headsign, calls) → services
        kept: list[tuple[int, int]] = []  # the current trip's calls at `keep` stops

        def add_trip(trip_id: str, start: int) -> None:
            """The current trip's calls as a pattern; a trip given by frequency as one per run."""
            line, service, freq, headsign = trips[trip_id]
            # shortcut: runs are spread evenly from the first to the last start, exact for one frequency row per trip
            shifts = [0] if freq is None else \
                [freq[1] - start + (round(k * (freq[2] - freq[1]) / (freq[0] - 1)) if freq[0] > 1 else 0)
                 for k in range(freq[0])]
            for shift in shifts:
                key = (line, headsign, tuple((s, t + shift) for s, t in kept))
                patterns.setdefault(key, set()).add(service)

        line_calls: dict[tuple[int, str], int] = defaultdict(int)
        service_calls: dict[tuple[int, str], list[int]] = {}
        reader = csv.reader(io.TextIOWrapper(z.open("stop_times.txt"), encoding="utf-8-sig"))
        header = next(reader)
        i_trip, i_stop, i_time = header.index("trip_id"), header.index("stop_id"), header.index("departure_time")
        current, trip_start = None, 0
        for row in reader:
            trip_id = row[i_trip]
            trip = trips.get(trip_id)
            if trip is None:
                continue
            t = _minutes(row[i_time]) if row[i_time] else None
            if trip_id != current:  # rows are grouped by trip, in stop order
                if kept:
                    add_trip(current, trip_start)
                    kept.clear()
                current, trip_start = trip_id, t or 0
            stop = stop_keys.get(row[i_stop])
            if stop is None:
                continue
            line, service, freq, _ = trip
            runs, t_first, t_last = (1, t, t) if freq is None else \
                (freq[0], freq[1] + (t or trip_start) - trip_start, freq[2] + (t or trip_start) - trip_start)
            own = owned is None or stop in owned  # else another feed counts the stop's calls (foreign_transit)
            if own:
                line_calls[(stop, line)] += summer_days[service] * runs
            if not days[service]:  # no future days: counts towards the lines only
                continue
            if keep is not None and stop in keep and t is not None:
                kept.append((stop, t))
            if not own:
                continue
            agg = service_calls.get((stop, service))
            if agg is None:
                service_calls[(stop, service)] = [t_first, t_last, runs]
            else:
                if t_first is not None and (agg[0] is None or t_first < agg[0]):
                    agg[0] = t_first
                if t_last is not None and (agg[1] is None or t_last > agg[1]):
                    agg[1] = t_last
                agg[2] += runs
        if kept:
            add_trip(current, trip_start)
    feed.lines = [{"uic": s, "line": line, "label": labels[line], "departures": round(n / n_summer, 2)}
                  for (s, line), n in line_calls.items() if n]
    used = {service for _, service in service_calls}
    feed.services = {s: bits for s, bits in days.items() if s in used}
    feed.stop_services = [(s, service, *agg) for (s, service), agg in service_calls.items()]
    feed.patterns = [(labels[line], headsign, line in lifts, services, calls)
                     for (line, headsign, calls), services in patterns.items()]
    print(f"timetable {feed.year} ({feed.version}, {start} to {end}): {len(feed.lines):,} stop/line pairs, "
          f"{len(feed.stop_services):,} stop/service pairs, {len(feed.services):,} services, "
          f"{len(feed.patterns):,} trip patterns")
    return feed


def span(path: Path) -> tuple[date, date]:
    """A feed's first and last day."""
    with zipfile.ZipFile(path) as z:
        return _span(z)[1:]


def merge(feeds: list[Feed]) -> Feed:
    """One year's timetables as one (the year is the timetable's key): it runs from the earliest start to the latest
    end, day bits are shifted to that start, and the service and line ids of each foreign feed get its source as
    prefix (`at57:…`, `gtfsde:…`) so feeds don't collide. A Swiss timetable that starts first is extended (a lone one
    returned as it is)."""
    if len(feeds) == 1 and not feeds[0].source:
        return feeds[0]
    start, end = min(f.start for f in feeds), max(f.end for f in feeds)
    version = " ".join(f"{f.source}:{f.version}" if f.source else f.version for f in feeds)
    if not feeds[0].source and feeds[0].start == start:  # extended in place: the Swiss timetable is large
        merged = feeds[0]
        merged.version, merged.end = version, end
    else:
        merged = Feed(feeds[0].year, version, start, end)
    for f in feeds:
        if f is merged:
            continue
        prefix, shift = f"{f.source}:" if f.source else "", (f.start - start).days
        merged.lines += [r | {"line": prefix + r["line"]} for r in f.lines]
        merged.services |= {prefix + s: bits << shift for s, bits in f.services.items()}
        merged.stop_services += [(stop, prefix + s, *agg) for stop, s, *agg in f.stop_services]
        merged.patterns += [(label, headsign, lift, {prefix + s for s in services}, calls)
                            for label, headsign, lift, services, calls in f.patterns]
    return merged


def read_all(keep: set[int | str] | None = None, since: date | None = None) -> Iterator[Feed]:
    """Each year's timetable, oldest first: the Swiss one merged with the foreign feeds on disk (foreign_transit): the
    Austrian datasets of that year, and the days of the open feeds within the Swiss timetable's (else the Austrian
    ones'). One year at a time (~600 MB each): the next is read once the caller is done with the last."""
    files = foreign_transit.feed_files()
    net = foreign_transit.stop_keys()
    swiss = {int(path.stem.split("_")[1]): path for path in feed_files()}
    open_feeds = [(f, span(f.path)) for f in files if f.year is None]
    open_lines: dict[str, list[dict]] = {}
    years = sorted(swiss.keys() | {f.year for f in files if f.year})
    for year in years:
        parts = [read(swiss[year], since, keep)] if year in swiss else []
        parts += [read(f.path, since, keep, f.source, year, net) for f in files if f.year == year]
        days = (parts[0].start, parts[0].end) if year in swiss else \
            (min(p.start for p in parts), max(p.end for p in parts))
        for f, (start, end) in open_feeds:
            if start <= days[1] and end >= days[0]:
                parts.append(read(f.path, since, keep, f.source, year, net, days))
                open_lines[f.source] = parts[-1].lines
        merged = merge(parts)
        mark_unknown(merged, parts, net)
        if year == years[-1]:  # the lines of open feeds that end before the newest year starts (Germany's 30 days)
            merged.lines += [r | {"line": f"{source}:{r['line']}"} for source, rows in open_lines.items()
                             if source not in {p.source for p in parts} for r in rows]
        yield merged
        del parts, merged


def mark_unknown(feed: Feed, parts: list[Feed], net: Network) -> None:
    """Add to a merged timetable, for each foreign stop, the days none of the feeds that own it (foreign_transit)
    covers: a service with UNKNOWN_CALLS calls there, so the planner doesn't take these days for "no service"."""
    covered = {p.source: ((1 << ((p.end - p.start).days + 1)) - 1) << (p.start - feed.start).days
               for p in parts if p.source}
    every_day = (1 << ((feed.end - feed.start).days + 1)) - 1
    by_owners: dict[frozenset[str], list[str]] = {}
    for key, stop in net.stops.items():
        by_owners.setdefault(frozenset(stop["owners"]), []).append(key)
    for owners, keys in by_owners.items():
        days = every_day
        for source in owners:
            days &= ~covered.get(source, 0)
        if days:
            service = "unknown:" + ",".join(sorted(owners))
            feed.services[service] = days
            feed.stop_services += [(key, service, None, None, UNKNOWN_CALLS) for key in keys]


def ride_stops(conn: sqlite3.Connection) -> set[int | str]:
    """Stop keys (uic, else ref) of the stops with walking connections: the ones whose trips are kept. Edges are
    built after the timetable, so stops new to them get their trips with the next timetable load."""
    return {r[0] for r in conn.execute(
        "SELECT coalesce(uic, ref) FROM stop "
        "WHERE place_id IN (SELECT from_place FROM edge UNION SELECT to_place FROM edge)")}


def load(conn: sqlite3.Connection, feeds: Iterable[Feed]) -> int:
    """Replace the timetable data with these feeds (stop lines from the newest), taken one at a time: read_all
    reads the next year once the last one is stored. Rows for stop keys not in the stop table are skipped. Returns
    the number of stop lines."""
    places = dict(conn.execute("SELECT coalesce(uic, ref), place_id FROM stop").fetchall())
    with conn:
        conn.execute("DELETE FROM stop_line")
        conn.execute("DELETE FROM stop_service")
        conn.execute("DELETE FROM timetable_service")
        conn.execute("DELETE FROM trip_pattern")
        conn.execute("DELETE FROM trip_call")
        conn.execute("DELETE FROM timetable")
        newest_year, newest_lines = None, []
        for f in feeds:
            if newest_year is None or f.year > newest_year:
                newest_year, newest_lines = f.year, f.lines
            conn.execute("INSERT INTO timetable (feed, version, start_date, end_date) VALUES (?, ?, ?, ?)",
                         (f.year, f.version, f.start.isoformat(), f.end.isoformat()))
            number = {s: i for i, s in enumerate(sorted(f.services))}
            n_bytes = ((f.end - f.start).days + 8) // 8
            conn.executemany("INSERT INTO timetable_service (feed, service, days) VALUES (?, ?, ?)",
                             [(f.year, number[s], bits.to_bytes(n_bytes, "little")) for s, bits in f.services.items()])
            conn.executemany(
                "INSERT INTO stop_service (feed, stop_place, service, first_min, last_min, calls) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ((f.year, places[uic], number[s], first, last, calls) for uic, s, first, last, calls in f.stop_services
                 if uic in places and s in number))
            patterns = ((label, headsign, lift, [number[s] for s in services if s in number],
                         [(places[uic], t) for uic, t in calls if uic in places])
                        for label, headsign, lift, services, calls in f.patterns)
            patterns = [p for p in patterns if p[3] and p[4]]
            conn.executemany("INSERT INTO trip_pattern (feed, pattern, label, headsign, lift, services) "
                             "VALUES (?, ?, ?, ?, ?, ?)",
                             ((f.year, i, label, headsign, int(lift), ",".join(map(str, sorted(services))))
                              for i, (label, headsign, lift, services, _) in enumerate(patterns)))
            conn.executemany("INSERT INTO trip_call (feed, pattern, seq, stop_place, time_min) VALUES (?, ?, ?, ?, ?)",
                             ((f.year, i, seq, place, t) for i, p in enumerate(patterns)  # rows not kept: millions
                              for seq, (place, t) in enumerate(p[4])))
            del f, patterns  # before the next year is read
        conn.executemany("INSERT INTO stop_line (stop_place, line, label, departures) VALUES (?, ?, ?, ?)",
                         [(places[r["uic"]], r["line"], r["label"], r["departures"]) for r in newest_lines
                          if r["uic"] in places])
        conn.execute("UPDATE data_version SET version = version + 1")  # the planner caches the timetable
    return conn.execute("SELECT COUNT(*) FROM stop_line").fetchone()[0]


def lines_fingerprint(conn: sqlite3.Connection) -> str:
    """Hash of which stops serve which lines in summer; edges depend on that, not on the departure counts."""
    rows = conn.execute("SELECT stop_place, line FROM stop_line WHERE departures > 0 ORDER BY stop_place, line")
    return hashlib.sha256(json.dumps([tuple(r) for r in rows]).encode()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-download", action="store_true", help="use the timetables in data/gtfs/")
    args = parser.parse_args()
    if not args.no_download:
        download()
    conn = connect()
    print(f"loaded {load(conn, read_all(ride_stops(conn)))} stop lines")


if __name__ == "__main__":
    main()
