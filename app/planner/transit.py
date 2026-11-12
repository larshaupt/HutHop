"""Public transport on a date: is a stop served, how often, from when to when, its departures, and rides between
two stops (from load_timetable's tables).

Loaded with the graph (graph.py) for the stops that have walking connections, and cached with it. The trips
(departures and rides) are only read from the database when first needed: a search without rides within the tour
uses the per-stop summary alone.
"""

import sqlite3
import threading
from dataclasses import dataclass
from datetime import date

# Flags shown with a tour's access and exit stop (minutes after midnight); also the window for a ride within a tour
LATEST_HUT_ARRIVAL = 18 * 60  # with the first connection, you'd reach the first hut only after this (dinner)
EARLIEST_HUT_DEPARTURE = 8 * 60  # to catch the last connection, you'd have to leave the last hut before this
DEPARTURE_CACHE = 20_000  # (stop, day) departure lists kept; cleared when full


@dataclass(frozen=True)
class StopDay:
    """A stop's service on one day. `known` is False when no loaded timetable covers the date."""

    known: bool
    calls: int = 0
    first_min: int | None = None
    last_min: int | None = None

    @property
    def served(self) -> bool:
        return not self.known or self.calls > 0


UNKNOWN = StopDay(False)


@dataclass(frozen=True)
class Pattern:
    """One run of a line through the stops with walking connections, shared by all the services it runs on."""

    label: str  # e.g. "B 121"
    headsign: str
    lift: bool  # cable car, chairlift, gondola or funicular
    services: tuple[int, ...]
    calls: tuple[tuple[int, int], ...]  # (stop place, minutes after midnight) in order


@dataclass(frozen=True)
class Ride:
    """One public transport ride from a stop to a later stop of the same trip."""

    frm: int
    to: int
    departure: int  # minutes after midnight
    arrival: int
    label: str
    headsign: str
    lift: bool = False


class Timetable:
    def __init__(self, feeds: list[tuple[date, date, dict[int, int], dict[int, list[tuple[int, int, int, int]]]]],
                 patterns: list[list[Pattern]] | None = None, db: str | None = None):
        """feeds: (start, end, service → day bits, stop → [(service, first, last, calls)]). Trips: `patterns` per
        feed, or read from the database file `db` when first needed (neither: no trips)."""
        self.feeds = feeds
        self.db = db
        self._patterns = patterns
        self._index: list[dict[int, list[tuple[int, Pattern, int]]]] | None = None
        self._lock = threading.Lock()
        self._days: dict[tuple[int, date], StopDay] = {}
        self._departures: dict[tuple[int, date], list[tuple[int, Pattern, int]]] = {}

    def day(self, stop_id: int, day: date) -> StopDay:
        key = (stop_id, day)
        if key not in self._days:
            self._days[key] = self._compute(stop_id, day)
        return self._days[key]

    def _feed(self, day: date) -> tuple[int, int] | None:
        """The feed covering the day, and the day's bit in its service masks."""
        for k, (start, end, _, _) in enumerate(self.feeds):
            if start <= day <= end:
                return k, (day - start).days
        return None

    def _compute(self, stop_id: int, day: date) -> StopDay:
        found = self._feed(day)
        if found is None:
            return UNKNOWN
        k, i = found
        services, stops = self.feeds[k][2], self.feeds[k][3]
        calls, first, last = 0, None, None
        for service, f, la, n in stops.get(stop_id, ()):
            if services.get(service, 0) >> i & 1:
                calls += n
                first = f if first is None or (f is not None and f < first) else first
                last = la if last is None or (la is not None and la > last) else last
        return StopDay(True, calls, first, last)

    def _calls(self) -> list[dict[int, list[tuple[int, Pattern, int]]]]:
        """Per feed: stop → (minutes, pattern, position in its calls), by time."""
        with self._lock:
            if self._index is None:
                if self._patterns is None:
                    self._patterns = load_patterns(self.db) if self.db else []
                index = []
                for patterns in self._patterns:
                    by_stop: dict[int, list[tuple[int, Pattern, int]]] = {}
                    for p in patterns:
                        for seq, (stop, t) in enumerate(p.calls):
                            by_stop.setdefault(stop, []).append((t, p, seq))
                    for calls in by_stop.values():
                        calls.sort(key=lambda c: c[0])
                    index.append(by_stop)
                self._index = index
            return self._index

    def departures(self, stop_id: int, day: date) -> list[tuple[int, Pattern, int]]:
        """Calls at a stop on a day: (minutes, pattern, position in its calls), by time. Empty without a timetable
        for the date."""
        key = (stop_id, day)
        if key not in self._departures:
            found, index = self._feed(day), self._calls()
            out = []
            if found is not None and found[0] < len(index):
                k, i = found
                services = self.feeds[k][2]
                out = [c for c in index[k].get(stop_id, ())
                       if any(services.get(s, 0) >> i & 1 for s in c[1].services)]
            if len(self._departures) >= DEPARTURE_CACHE:
                self._departures.clear()
            self._departures[key] = out
        return self._departures[key]

    def rides_from(self, stop_id: int, day: date, earliest: float, latest: float,
                   avoid_lifts: bool = False) -> dict[int, Ride]:
        """One ride from a stop leaving between `earliest` and `latest` (minutes): the earliest arrival at every
        later stop of the trips, by stop."""
        best: dict[int, Ride] = {}
        for t, p, seq in self.departures(stop_id, day):
            if t > latest:
                break
            if t < earliest or (avoid_lifts and p.lift):
                continue
            for stop, arrival in p.calls[seq + 1:]:
                if stop != stop_id and (stop not in best or arrival < best[stop].arrival):
                    best[stop] = Ride(stop_id, stop, t, arrival, p.label, p.headsign, p.lift)
        return best


def load(conn: sqlite3.Connection) -> Timetable:
    """The timetables of all stops that have a walking connection."""
    feeds = []
    for f in conn.execute("SELECT feed, start_date, end_date FROM timetable ORDER BY feed").fetchall():
        services = {r["service"]: int.from_bytes(r["days"], "little") for r in conn.execute(
            "SELECT service, days FROM timetable_service WHERE feed = ?", (f["feed"],))}
        stops: dict[int, list[tuple[int, int, int, int]]] = {}
        for r in conn.execute(
                "SELECT stop_place, service, first_min, last_min, calls FROM stop_service WHERE feed = ? AND stop_place "
                "IN (SELECT from_place FROM edge UNION SELECT to_place FROM edge)", (f["feed"],)):
            stops.setdefault(r["stop_place"], []).append((r["service"], r["first_min"], r["last_min"], r["calls"]))
        feeds.append((date.fromisoformat(f["start_date"]), date.fromisoformat(f["end_date"]), services, stops))
    return Timetable(feeds, db=conn.execute("PRAGMA database_list").fetchone()[2] or None)


def load_patterns(db: str) -> list[list[Pattern]]:
    """The trips of each feed (in feed order, like load), from load_timetable's tables."""
    conn = sqlite3.connect(db)
    try:
        calls: dict[tuple[int, int], list[tuple[int, int]]] = {}
        for feed, pattern, stop, t in conn.execute(
                "SELECT feed, pattern, stop_place, time_min FROM trip_call ORDER BY feed, pattern, seq"):
            calls.setdefault((feed, pattern), []).append((stop, t))
        out: dict[int, list[Pattern]] = {r[0]: [] for r in conn.execute("SELECT feed FROM timetable")}
        for feed, pattern, label, headsign, lift, services in conn.execute(
                "SELECT feed, pattern, label, headsign, lift, services FROM trip_pattern"):
            if feed in out:
                out[feed].append(Pattern(label, headsign or "", bool(lift), tuple(map(int, services.split(","))),
                                         tuple(calls.get((feed, pattern), ()))))
        return [out[f] for f in sorted(out)]
    finally:
        conn.close()


def hhmm(minutes: int | None) -> str | None:
    """1510 → '25:10' is shown as '01:10' (+1 day is implied by the context)."""
    if minutes is None:
        return None
    return f"{minutes // 60 % 24:02d}:{minutes % 60:02d}"
