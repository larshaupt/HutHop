"""Public transport on a date: is a stop served, how often, and from when to when (from load_timetable's tables).

Loaded with the graph (graph.py) for the stops that have walking connections, and cached with it.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date

# Flags shown with a tour's access and exit stop (minutes after midnight)
LATEST_HUT_ARRIVAL = 18 * 60  # with the first connection, you'd reach the first hut only after this (dinner)
EARLIEST_HUT_DEPARTURE = 8 * 60  # to catch the last connection, you'd have to leave the last hut before this


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


class Timetable:
    def __init__(self, feeds: list[tuple[date, date, dict[int, int], dict[int, list[tuple[int, int, int, int]]]]]):
        """feeds: (start, end, service → day bits, stop → [(service, first, last, calls)])."""
        self.feeds = feeds
        self._days: dict[tuple[int, date], StopDay] = {}

    def day(self, stop_id: int, day: date) -> StopDay:
        key = (stop_id, day)
        if key not in self._days:
            self._days[key] = self._compute(stop_id, day)
        return self._days[key]

    def _compute(self, stop_id: int, day: date) -> StopDay:
        for start, end, services, stops in self.feeds:
            if start <= day <= end:
                i = (day - start).days
                calls, first, last = 0, None, None
                for service, f, la, n in stops.get(stop_id, ()):
                    if services.get(service, 0) >> i & 1:
                        calls += n
                        first = f if first is None or (f is not None and f < first) else first
                        last = la if last is None or (la is not None and la > last) else last
                return StopDay(True, calls, first, last)
        return UNKNOWN


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
    return Timetable(feeds)


def hhmm(minutes: int | None) -> str | None:
    """1510 → '25:10' is shown as '01:10' (+1 day is implied by the context)."""
    if minutes is None:
        return None
    return f"{minutes // 60 % 24:02d}:{minutes % 60:02d}"
