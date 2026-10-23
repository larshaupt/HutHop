"""Planning steps that combine the graph, the tour search and live availability; the API only calls these.

  search_tours   a search, with free beds checked in rounds for the best candidates
  rebuild_tour   one tour from its id (sharing, GPX, checking beds later, and the tour editor)
"""

import sqlite3
from datetime import timedelta

from app.planner import availability
from app.planner.graph import Graph, cached_graph
from app.planner.tours import Tour, TourRequest, build_tour, parse_tour_id, suggest

AVAILABILITY_ROUNDS = 3  # search, fetch calendars of the huts in the best tours, search again
# Calendars are fetched for the huts of this many best candidates per round. Fewer than POOL_SIZE on purpose:
# fetches are capped (availability.MAX_FETCHES); the browser checks other tours when they are opened.
CANDIDATES_PER_ROUND = 25


def search_tours(conn: sqlite3.Connection, req: TourRequest, people: int,
                 check_availability: bool) -> tuple[Graph, list[Tour], int]:
    """The tour pool for a request, and the number of hut calendars fetched for it."""
    graph = cached_graph(conn, req.limit)
    if not check_availability:
        return graph, suggest(graph, req), 0
    end = req.start_date + timedelta(days=req.nights)
    fetched = 0
    for _ in range(AVAILABILITY_ROUNDS):
        snapshot = availability.Snapshot(conn, req.start_date, end, people)
        tours = suggest(graph, req, _lookup(snapshot))
        needed = {n.hut for t in tours[:CANDIDATES_PER_ROUND] for n in t.nights
                  if n.availability and n.availability.state == "not_checked"}
        if not needed or fetched >= availability.MAX_FETCHES:
            return graph, tours, fetched  # nothing new fetched: this search is already the final one
        fetched += availability.refresh(conn, needed, availability.MAX_FETCHES - fetched)
    snapshot = availability.Snapshot(conn, req.start_date, end, people)
    return graph, suggest(graph, req, _lookup(snapshot)), fetched


def rebuild_tour(conn: sqlite3.Connection, tour_id: str, people: int, check_availability: bool,
                 weights: dict[str, float], fresh: bool = False) -> tuple[Graph, Tour]:
    """Raises ValueError for a malformed id, LookupError if a connection of the tour no longer exists. `fresh`:
    free beds from calendars at most a minute old (right before booking), not the usual 10 minutes."""
    key = parse_tour_id(tour_id)
    graph = cached_graph(conn, 3)  # all trail categories; a tour id names its edges explicitly
    lookup = None
    if check_availability:
        availability.refresh(conn, set(key.places[1:-1]),
                             max_age_s=availability.FRESH_TTL_S if fresh else availability.CACHE_TTL_S)
        lookup = _lookup(availability.Snapshot(conn, key.start_date,
                                               key.start_date + timedelta(days=len(key.limits) - 1), people))
    try:
        return graph, build_tour(graph, key, availability=lookup, weights=weights)
    except ValueError as err:
        raise LookupError(str(err)) from None


def _lookup(snapshot: availability.Snapshot):
    return lambda hut, day: snapshot.night(hut.id, hut.hrs_id is not None, day)
