"""Load the official Swiss timetable (GTFS, opentransportdata.swiss): which lines serve each stop, how often, and on
which days from when to when.

    uv run python -m app.ingest.load_timetable                # download new editions (~90 MB each), then load
    uv run python -m app.ingest.load_timetable --no-download

Keeps this year's and (once published, in autumn) next year's timetable, so tours can be planned for any date
from now to the end of next season. Reads each zip directly (stop_times.txt alone is 1.2 GB unpacked) in one
streaming pass: ~25 s and ~600 MB RAM per year. Stores
  - stop_line: lines per stop with summer departures (newest timetable), for build_edges (closest stop of each
    line per hut) and the connection list;
  - timetable, timetable_service, stop_service: per stop and service pattern the first and last time and number
    of calls, and the days each pattern runs, so the planner knows whether a stop is served on a date.
"""

import argparse
import csv
import hashlib
import io
import json
import math
import re
import sqlite3
import zipfile
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import httpx

from app.db import ROOT, connect

PERMALINK = "https://data.opentransportdata.swiss/en/dataset/timetable-{year}-gtfs2020/permalink"
GTFS_DIR = ROOT / "data/gtfs"
USER_AGENT = "HutHop/0.1 (private hut planner)"
SUMMER = ((7, 1), (8, 31))  # summer departures per line are averaged over the hut season of the timetable year


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


def _rows(z: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    if name not in z.namelist():
        return iter(())
    return csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8-sig"))


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


@dataclass
class Feed:
    """One year's timetable, as read from its zip."""

    year: int
    version: str
    start: date
    end: date
    lines: list[dict] = field(default_factory=list)  # {uic, line, label, departures (summer calls per day)}
    services: dict[str, int] = field(default_factory=dict)  # service id → day bits (bit i = start + i days)
    stop_services: list[tuple] = field(default_factory=list)  # (uic, service id, first min, last min, calls)


def read(path: Path, since: date | None = None) -> Feed:
    """One year's timetable from its zip. Days before `since` (default today) are dropped: tours are never planned
    in the past, and the current year's file holds many past service patterns (e.g. for construction work)."""
    since = since or date.today()
    with zipfile.ZipFile(path) as z:
        info = next(_rows(z, "feed_info.txt"))
        start, end = _day(info["feed_start_date"]), _day(info["feed_end_date"])
        feed = Feed(end.year, info["feed_version"], start, end)
        all_days = service_days(z, start, end)
        # Summer departures count the whole summer, even once it is past; only the served days stored drop the past
        past = (1 << (since - start).days) - 1 if since > start else 0
        days = {s: bits & ~past for s, bits in all_days.items()}
        first, last = max(start, date(end.year, *SUMMER[0])), min(end, date(end.year, *SUMMER[1]))
        if first > last:  # a feed that doesn't cover the summer: average over all of it
            first, last = start, end
        summer_mask = ((1 << ((last - start).days + 1)) - 1) ^ ((1 << (first - start).days) - 1)
        summer_days = {s: (bits & summer_mask).bit_count() for s, bits in all_days.items()}
        n_summer = (last - first).days + 1

        uic = {r["stop_id"]: int(r["didok"]) for r in _rows(z, "stops.txt") if r.get("didok", "").isdigit()}
        lines, labels = {}, {}
        for r in _rows(z, "routes.txt"):
            lines[r["route_id"]] = key = line_key(r["route_id"])
            labels.setdefault(key, line_label(r["route_desc"], r["route_short_name"]))
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
        trips = {}
        for r in _rows(z, "trips.txt"):
            if all_days.get(r["service_id"]):
                trips[r["trip_id"]] = (lines[r["route_id"]], r["service_id"], frequency.get(r["trip_id"]))

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
                current, trip_start = trip_id, t or 0
            stop = uic.get(row[i_stop])
            if stop is None:
                continue
            line, service, freq = trip
            runs, t_first, t_last = (1, t, t) if freq is None else \
                (freq[0], freq[1] + (t or trip_start) - trip_start, freq[2] + (t or trip_start) - trip_start)
            line_calls[(stop, line)] += summer_days[service] * runs
            if not days[service]:  # no future days: counts towards the lines only
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
    feed.lines = [{"uic": s, "line": line, "label": labels[line], "departures": round(n / n_summer, 2)}
                  for (s, line), n in line_calls.items() if n]
    used = {service for _, service in service_calls}
    feed.services = {s: bits for s, bits in days.items() if s in used}
    feed.stop_services = [(s, service, *agg) for (s, service), agg in service_calls.items()]
    print(f"timetable {feed.year} ({feed.version}, {start} to {end}): {len(feed.lines):,} stop/line pairs, "
          f"{len(feed.stop_services):,} stop/service pairs, {len(feed.services):,} services")
    return feed


def read_all() -> list[Feed]:
    return [read(path) for path in feed_files()]


def load(conn: sqlite3.Connection, feeds: list[Feed]) -> int:
    """Replace the timetable data with these feeds (stop lines from the newest). Rows for stop numbers not in the
    stop table (e.g. abroad) are skipped. Returns the number of stop lines."""
    places = dict(conn.execute("SELECT uic, place_id FROM stop").fetchall())
    with conn:
        conn.execute("DELETE FROM stop_line")
        conn.execute("DELETE FROM stop_service")
        conn.execute("DELETE FROM timetable_service")
        conn.execute("DELETE FROM timetable")
        if feeds:
            newest = max(feeds, key=lambda f: f.year)
            conn.executemany("INSERT INTO stop_line (stop_place, line, label, departures) VALUES (?, ?, ?, ?)",
                             [(places[r["uic"]], r["line"], r["label"], r["departures"]) for r in newest.lines
                              if r["uic"] in places])
        for f in feeds:
            conn.execute("INSERT INTO timetable (feed, version, start_date, end_date) VALUES (?, ?, ?, ?)",
                         (f.year, f.version, f.start.isoformat(), f.end.isoformat()))
            number = {s: i for i, s in enumerate(sorted(f.services))}
            n_bytes = ((f.end - f.start).days + 8) // 8
            conn.executemany("INSERT INTO timetable_service (feed, service, days) VALUES (?, ?, ?)",
                             [(f.year, number[s], bits.to_bytes(n_bytes, "little")) for s, bits in f.services.items()])
            conn.executemany(
                "INSERT INTO stop_service (feed, stop_place, service, first_min, last_min, calls) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(f.year, places[uic], number[s], first, last, calls) for uic, s, first, last, calls in f.stop_services
                 if uic in places and s in number])
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
    print(f"loaded {load(connect(), read_all())} stop lines")


if __name__ == "__main__":
    main()
