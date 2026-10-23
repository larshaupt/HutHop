"""Bed availability per hut and night, fetched on demand from hut-reservation.org and cached briefly.

Availability changes quickly, so a hut's calendar is only trusted for CACHE_TTL_S. Fetches run with at most
MAX_PARALLEL requests at a time and at most MAX_FETCHES per call, to stay a polite API consumer.
"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from app.clients import hut_reservation

CACHE_TTL_S = 600
FRESH_TTL_S = 60  # "check again" right before booking: calendars at most a minute old
MAX_PARALLEL = 2
MAX_FETCHES = 40

State = Literal["available", "full", "closed", "not_released", "no_booking", "not_checked", "error"]


@dataclass(frozen=True)
class NightAvailability:
    state: State
    hut_status: str | None = None  # SERVICED (catered) | UNSERVICED (winter room / self-service) | CLOSED
    free_beds: int | None = None
    total_places: int | None = None
    checked_at: str | None = None


def _cutoff(max_age_s: int = CACHE_TTL_S) -> str:
    return (datetime.now(UTC) - timedelta(seconds=max_age_s)).isoformat(timespec="seconds")


def stale_huts(conn: sqlite3.Connection, hut_ids: set[int], max_age_s: int = CACHE_TTL_S) -> list[tuple[int, int]]:
    """(hut id, hrs id) of bookable huts whose cached calendar is missing or older than `max_age_s`."""
    if not hut_ids:
        return []
    ids = list(hut_ids)
    rows = conn.execute(
        f"""SELECT h.id, h.hrs_id FROM hut_effective h
            LEFT JOIN availability_fetch f ON f.hut_id = h.id
            WHERE h.id IN ({','.join('?' * len(ids))}) AND h.hrs_id IS NOT NULL
              AND (f.fetched_at IS NULL OR f.fetched_at < ?)""", ids + [_cutoff(max_age_s)]).fetchall()
    return [(r["id"], r["hrs_id"]) for r in rows]


def refresh(conn: sqlite3.Connection, hut_ids: set[int], max_fetches: int = MAX_FETCHES,
            max_age_s: int = CACHE_TTL_S) -> int:
    """Fetch calendars for huts whose cache is older than `max_age_s`. Returns the number of huts fetched."""
    todo = stale_huts(conn, hut_ids, max_age_s)[:max_fetches]
    if not todo:
        return 0
    client = hut_reservation.make_client()

    def fetch(item: tuple[int, int]) -> tuple[int, list[dict] | None, str | None]:
        hut_id, hrs_id = item
        try:
            return hut_id, hut_reservation.fetch_calendar(hrs_id, client), None
        except Exception as err:  # network and HTTP errors, unexpected JSON
            return hut_id, None, f"{type(err).__name__}: {err}"[:200]

    with ThreadPoolExecutor(MAX_PARALLEL) as pool:
        results = list(pool.map(fetch, todo))
    fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
    with conn:
        for hut_id, rows, error in results:
            if rows is not None:
                conn.execute("DELETE FROM availability WHERE hut_id = ?", (hut_id,))
                conn.executemany(
                    "INSERT INTO availability (hut_id, date, hut_status, free_beds, total_places, fetched_at) "
                    "VALUES (:hut_id, :date, :hut_status, :free_beds, :total_places, :fetched_at)",
                    [r | {"hut_id": hut_id, "fetched_at": fetched_at} for r in rows])
            conn.execute("INSERT OR REPLACE INTO availability_fetch (hut_id, fetched_at, ok, error) "
                         "VALUES (?, ?, ?, ?)", (hut_id, fetched_at, int(rows is not None), error))
    return len(todo)


class Snapshot:
    """Fresh cached availability for a date range, classified per hut night and group size."""

    def __init__(self, conn: sqlite3.Connection, start: date, end: date, people: int):
        self.people = people
        cutoff = _cutoff()
        self.fetch = {r["hut_id"]: bool(r["ok"]) for r in conn.execute(
            "SELECT hut_id, ok FROM availability_fetch WHERE fetched_at >= ?", (cutoff,))}
        self.days = {(r["hut_id"], date.fromisoformat(r["date"])): r for r in conn.execute(
            """SELECT a.* FROM availability a JOIN availability_fetch f ON f.hut_id = a.hut_id
               WHERE f.fetched_at >= ? AND f.ok = 1 AND a.date BETWEEN ? AND ?""",
            (cutoff, start.isoformat(), end.isoformat()))}
        # Last date with released places per hut: a CLOSED 0-of-0 night before it is a real closure
        # (the season is out, the hut just isn't open then), not a contingent that is still to come.
        self.last_released = {r["hut_id"]: date.fromisoformat(r["last"]) for r in conn.execute(
            """SELECT a.hut_id, max(a.date) AS last FROM availability a JOIN availability_fetch f ON f.hut_id = a.hut_id
               WHERE f.fetched_at >= ? AND f.ok = 1 AND a.total_places > 0 GROUP BY a.hut_id""", (cutoff,))}

    def night(self, hut_id: int, bookable: bool, day: date) -> NightAvailability:
        if not bookable:
            return NightAvailability("no_booking")
        if hut_id not in self.fetch:
            return NightAvailability("not_checked")
        if not self.fetch[hut_id]:
            return NightAvailability("error")
        row = self.days.get((hut_id, day))
        if row is None:  # beyond the hut's booking window
            return NightAvailability("not_released")
        released_later = (last := self.last_released.get(hut_id)) is not None and day < last
        return classify(row["hut_status"], row["free_beds"], row["total_places"], row["fetched_at"], self.people,
                        released_later)


def classify(hut_status: str | None, free_beds: int | None, total_places: int, checked_at: str | None,
             people: int, released_later: bool = False) -> NightAvailability:
    """`released_later`: the hut's calendar has bookable places after this date."""
    base = dict(hut_status=hut_status, free_beds=free_beds, total_places=total_places, checked_at=checked_at)
    if hut_status == "CLOSED" and not total_places and released_later:
        return NightAvailability("closed", **base)
    if not total_places:
        # 0 of 0: the contingent is not open for booking yet (or nothing is released for a closed period)
        return NightAvailability("not_released", **base)
    if hut_status == "CLOSED":
        return NightAvailability("closed", **base)
    if (free_beds or 0) >= people:
        return NightAvailability("available", **base)
    return NightAvailability("full", **base)


def calendar(conn: sqlite3.Connection, hut_id: int, hrs_id: int, people: int) -> dict:
    """A bookable hut's calendar from today, one entry per day classified for the group size (fetched if stale)."""
    refresh(conn, {hut_id}, max_fetches=1)
    fetch = conn.execute("SELECT fetched_at, ok FROM availability_fetch WHERE hut_id = ?", (hut_id,)).fetchone()
    result = {"bookable": True, "booking_url": hut_reservation.BOOKING_URL.format(hrs_id), "people": people,
              "checked_at": fetch["fetched_at"] if fetch else None, "error": not (fetch and fetch["ok"]), "days": []}
    dates = [date.fromisoformat(r[0]) for r in conn.execute(
        "SELECT date FROM availability WHERE hut_id = ? AND date >= ? ORDER BY date", (hut_id, date.today().isoformat()))]
    if result["error"] or not dates:
        return result
    snapshot = Snapshot(conn, dates[0], dates[-1], people)
    for day in dates:
        a = snapshot.night(hut_id, True, day)
        result["days"].append({"date": day.isoformat(), "state": a.state, "free_beds": a.free_beds,
                               "total_places": a.total_places, "hut_status": a.hut_status})
    return result
