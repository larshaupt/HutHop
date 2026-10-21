"""Load the official Swiss timetable (GTFS, opentransportdata.swiss): which lines serve each stop, and how often.

    uv run python -m app.ingest.load_timetable                # download if a new edition is out (~90 MB), then load
    uv run python -m app.ingest.load_timetable --no-download

Uses next year's timetable as soon as it is published (in autumn), so the summer departures are for the coming
season. Reads the zip directly (stop_times.txt alone is 1.2 GB unpacked) in one streaming pass: ~30 s, ~250 MB RAM.
build_edges keeps only the closest stop of each line per hut; the connection list shows lines and departures.
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
from datetime import date, timedelta

import httpx

from app.db import ROOT, connect

PERMALINK = "https://data.opentransportdata.swiss/en/dataset/timetable-{year}-gtfs2020/permalink"
GTFS_DIR = ROOT / "data/gtfs"
GTFS_ZIP = GTFS_DIR / "gtfs.zip"
EDITION = GTFS_DIR / "edition.json"
USER_AGENT = "HutHop/0.1 (private hut planner)"
SUMMER = ((7, 1), (8, 31))  # departures are averaged over the hut season of the timetable year


def download(force: bool = False, today: date | None = None) -> bool:
    """Download the newest timetable (next year's if already published, else this year's) if its edition changed.
    Returns True if a new file was downloaded."""
    year = (today or date.today()).year
    client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=300)
    href = None
    for y in (year + 1, year):
        res = client.get(PERMALINK.format(year=y))  # redirects to the current dated file
        if res.is_redirect:
            href = res.headers["location"]
            break
    if href is None:
        raise RuntimeError(f"no GTFS timetable found for {year} or {year + 1}")
    known = json.loads(EDITION.read_text()) if EDITION.exists() else None
    if known == {"href": href} and GTFS_ZIP.exists() and not force:
        print(f"timetable: up to date ({href.rsplit('/', 1)[-1]})")
        return False
    print(f"timetable: downloading {href}")
    GTFS_DIR.mkdir(parents=True, exist_ok=True)
    partial = GTFS_ZIP.with_suffix(".part")
    with client.stream("GET", href, follow_redirects=True) as res, partial.open("wb") as f:
        res.raise_for_status()
        for chunk in res.iter_bytes(1 << 20):
            f.write(chunk)
    partial.replace(GTFS_ZIP)
    EDITION.write_text(json.dumps({"href": href}, indent=1))
    return True


def _rows(z: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    if name not in z.namelist():
        return iter(())
    return csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8-sig"))


def _seconds(hms: str) -> int:
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + s


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


def service_days(z: zipfile.ZipFile, first: date, last: date) -> dict[str, int]:
    """Days each service runs between first and last (calendar.txt weekdays plus calendar_dates.txt exceptions)."""
    window = [first + timedelta(d) for d in range((last - first).days + 1)]
    weekly = {r["service_id"]: (r, _day(r["start_date"]), _day(r["end_date"])) for r in _rows(z, "calendar.txt")}
    names = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

    def by_calendar(service: str, d: date) -> bool:
        row = weekly.get(service)
        return bool(row) and row[1] <= d <= row[2] and row[0][names[d.weekday()]] == "1"

    days = {s: sum(by_calendar(s, d) for d in window) for s in weekly}
    for r in _rows(z, "calendar_dates.txt"):
        d = _day(r["date"])
        if not first <= d <= last:
            continue
        s = r["service_id"]
        if r["exception_type"] == "1" and not by_calendar(s, d):
            days[s] = days.get(s, 0) + 1
        elif r["exception_type"] == "2" and by_calendar(s, d):
            days[s] -= 1
    return days


def read(path=GTFS_ZIP) -> list[dict]:
    """One row per official stop number and line: {uic, line, label, departures (calls per day in summer)}."""
    with zipfile.ZipFile(path) as z:
        feed = next(_rows(z, "feed_info.txt"))
        feed_start, feed_end = _day(feed["feed_start_date"]), _day(feed["feed_end_date"])
        year = feed_end.year
        first, last = max(feed_start, date(year, *SUMMER[0])), min(feed_end, date(year, *SUMMER[1]))
        if first > last:  # a feed that doesn't cover the summer: average over all of it
            first, last = feed_start, feed_end
        n_days = (last - first).days + 1
        days = service_days(z, first, last)

        uic = {r["stop_id"]: int(r["didok"]) for r in _rows(z, "stops.txt") if r.get("didok", "").isdigit()}
        lines, labels = {}, {}
        for r in _rows(z, "routes.txt"):
            lines[r["route_id"]] = key = line_key(r["route_id"])
            labels.setdefault(key, line_label(r["route_desc"], r["route_short_name"]))
        runs: dict[str, int] = defaultdict(int)  # trips given as "every N minutes from … to …"
        for r in _rows(z, "frequencies.txt"):
            span = _seconds(r["end_time"]) - _seconds(r["start_time"])
            runs[r["trip_id"]] += max(1, math.ceil(span / int(r["headway_secs"])))
        trips = {}
        for r in _rows(z, "trips.txt"):
            weight = days.get(r["service_id"], 0) * runs.get(r["trip_id"], 1)
            if weight:
                trips[r["trip_id"]] = (lines[r["route_id"]], weight)

        calls: dict[tuple[int, str], int] = defaultdict(int)
        reader = csv.reader(io.TextIOWrapper(z.open("stop_times.txt"), encoding="utf-8-sig"))
        header = next(reader)
        i_trip, i_stop = header.index("trip_id"), header.index("stop_id")
        for row in reader:
            trip, stop = trips.get(row[i_trip]), uic.get(row[i_stop])
            if trip and stop:
                calls[(stop, trip[0])] += trip[1]
    print(f"timetable {feed['feed_version']}: {len(calls):,} stop/line pairs, summer {first} to {last}")
    return [{"uic": s, "line": line, "label": labels[line], "departures": round(n / n_days, 2)}
            for (s, line), n in calls.items()]


def load(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Replace all stop lines. Rows for stop numbers not in the stop table (e.g. abroad) are skipped."""
    places = dict(conn.execute("SELECT uic, place_id FROM stop").fetchall())
    with conn:
        conn.execute("DELETE FROM stop_line")
        conn.executemany("INSERT INTO stop_line (stop_place, line, label, departures) VALUES (?, ?, ?, ?)",
                         [(places[r["uic"]], r["line"], r["label"], r["departures"]) for r in rows
                          if r["uic"] in places])
    return conn.execute("SELECT COUNT(*) FROM stop_line").fetchone()[0]


def lines_fingerprint(conn: sqlite3.Connection) -> str:
    """Hash of which stops serve which lines in summer; edges depend on that, not on the departure counts."""
    rows = conn.execute("SELECT stop_place, line FROM stop_line WHERE departures > 0 ORDER BY stop_place, line")
    return hashlib.sha256(json.dumps([tuple(r) for r in rows]).encode()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-download", action="store_true", help="use the timetable in data/gtfs/")
    args = parser.parse_args()
    if not args.no_download:
        download()
    print(f"loaded {load(connect(), read())} stop lines")


if __name__ == "__main__":
    main()
