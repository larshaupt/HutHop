"""Tour search: stop → hut (one per night) → … → stop, over the precomputed place graph.

Beam search over hut sequences. Each day is one edge; its hiking time must lie in [min, max]. Each hut must be
open on its night (SAC monthly codes) in the requested way. Tours are ranked by how close the days are to the
middle of the requested range, with penalties for uncertain opening data.
"""

import heapq
import json
import math
import sqlite3
from dataclasses import dataclass, field
from collections.abc import Callable
from datetime import date, timedelta

from app.planner import opening, ranking
from app.planner.geometry import Footprints
from app.planner.availability import NightAvailability

Catering = opening.Wanted
BEAM_WIDTH = 3000
END_LEGS_PER_STATE = 2
POOL_SIZE = 40  # varied tours returned per search; the browser sorts them
MAX_TOURS_PER_HUT = 3  # in one result list, so the list isn't the same few huts in different orders
MIN_RESULTS = 10
LOOP_RADIUS_M = 3000
START_RADIUS_M = 1500  # a chosen start stop means "start around here" (nearby bus stops, stations)
UNKNOWN_PENALTY = 0.15
PARTIAL_PENALTY = 0.05
CATEGORY_PENALTY = 0.01  # per day and trail category above yellow: easier wins between otherwise equal tours
RETRACE_PENALTY = 0.5  # per day, times the share of that day on trail already walked (a full day ≈ 3 h off target)
# Nights without confirmed free beds rank lower (not bookable yet, no online booking, not checked).
AVAILABILITY_PENALTY = {"available": 0.0, "not_released": 0.03, "no_booking": 0.03, "not_checked": 0.05, "error": 0.05}
HRS_STATUS = {"SERVICED": "catered", "UNSERVICED": "self_service"}


@dataclass(frozen=True)
class Leg:
    edge_id: int
    frm: int
    to: int
    time_min: int
    distance_m: int
    ascent_m: int
    descent_m: int
    max_category: int
    sac_time_min: int | None
    category_limit: int = 1  # search limit the edge was stored for; with frm/to it names the edge stably


@dataclass(frozen=True)
class Hut:
    id: int
    name: str
    lat: float
    lon: float
    e: float
    n: float
    catering: str  # 12 SAC codes, one per month: 0 yes, 1 partly, 2 no
    self_service: str
    half_board: bool
    hrs_id: int | None = None
    altitude: int | None = None


@dataclass(frozen=True)
class Stop:
    id: int
    name: str
    lat: float
    lon: float
    e: float
    n: float
    mode: str


@dataclass
class Graph:
    huts: dict[int, Hut]
    stops: dict[int, Stop]
    legs: dict[tuple[int, int, int], Leg] = field(default_factory=dict)  # by (from, to, category limit)
    hut_out: dict[int, list[Leg]] = field(default_factory=dict)   # hut → hut
    stop_in: dict[int, list[Leg]] = field(default_factory=dict)   # stop → hut, keyed by hut
    stop_out: dict[int, list[Leg]] = field(default_factory=dict)  # hut → stop, keyed by hut
    footprints: Footprints = field(default_factory=lambda: Footprints({}))  # route lines, for retracing and shape


@dataclass(frozen=True)
class TourRequest:
    start_date: date
    nights: int
    min_hours: float
    max_hours: float
    limit: int = 2  # hardest trail: 1 yellow, 2 red-white, 3 blue-white
    catering: Catering = "any"
    bbox: tuple[float, float, float, float] | None = None  # west, south, east, north (WGS84) for huts
    start_near: tuple[float, float] | None = None  # LV95 point; tours start within START_RADIUS_M of it
    loop: bool = False
    include_unknown: bool = True
    max_results: int = POOL_SIZE
    sort: ranking.Sort = "best"
    weights: dict[str, float] = field(default_factory=lambda: dict(ranking.DEFAULT_WEIGHTS))


@dataclass(frozen=True)
class Night:
    hut: int
    date: date
    status: str  # 'catered' | 'self_service' | 'unknown' | 'closed' (only in tours built by hand)
    partial: bool
    availability: NightAvailability | None = None


@dataclass(frozen=True)
class Tour:
    """stop → hut → … → hut → stop: one leg (stored edge) per day, one night per hut."""

    score: float  # the "best" score (0–1) for the request's weights
    legs: tuple[Leg, ...]
    nights: tuple[Night, ...]
    start_date: date
    metrics: ranking.Metrics | None = None

    @property
    def id(self) -> str:
        """Start date, place ids and each day's category limit, e.g. '2027-07-17_123-45-67-89_2-2-3'.

        Uses places, not edge ids, so the id survives rebuilds of the edge table."""
        places = [self.legs[0].frm, *(leg.to for leg in self.legs)]
        return (f"{self.start_date.isoformat()}_{'-'.join(map(str, places))}_"
                f"{'-'.join(str(leg.category_limit) for leg in self.legs)}")


def parse_tour_id(tour_id: str) -> tuple[date, list[int], list[int]]:
    """Inverse of Tour.id: (start date, place ids, category limit per day). Raises ValueError if malformed."""
    day, places, limits = tour_id.split("_")
    place_ids, limit_ids = [int(p) for p in places.split("-")], [int(x) for x in limits.split("-")]
    if len(place_ids) < 3 or len(limit_ids) != len(place_ids) - 1 or not set(limit_ids) <= {1, 2, 3}:
        raise ValueError("malformed tour id")
    return date.fromisoformat(day), place_ids, limit_ids


def load_graph(conn: sqlite3.Connection, limit: int) -> Graph:
    """Huts, stops and all non-excluded edges up to a trail category limit.

    A place pair can have one edge per category limit (only stored when the harder trail is faster). All of
    them are kept as alternatives, so allowing harder trails only ever adds options."""
    huts = {}
    codes = opening.load_codes(conn)
    for r in conn.execute("SELECT id, name, lat, lon, e_lv95, n_lv95, services, hrs_id, altitude_m FROM hut_effective"):
        c, s = codes.get(r["id"], (opening.NO_DATA, opening.NO_DATA))
        huts[r["id"]] = Hut(r["id"], r["name"], r["lat"], r["lon"], r["e_lv95"], r["n_lv95"], c, s,
                            json.loads(r["services"] or "{}").get("half_board", False), r["hrs_id"], r["altitude_m"])
    rows = conn.execute(
        "SELECT * FROM edge WHERE category_limit <= ? AND excluded = 0", (limit,)).fetchall()
    stop_ids = {r["from_place"] for r in rows if r["from_place"] not in huts} | \
               {r["to_place"] for r in rows if r["to_place"] not in huts}
    stops = {}
    for chunk in range(0, len(stop_ids), 900):
        ids = list(stop_ids)[chunk:chunk + 900]
        for r in conn.execute(f"SELECT p.id, p.name, p.lat, p.lon, p.e_lv95, p.n_lv95, s.mode FROM place p "
                              f"JOIN stop s ON s.place_id = p.id WHERE p.id IN ({','.join('?' * len(ids))})", ids):
            stops[r["id"]] = Stop(r["id"], r["name"], r["lat"], r["lon"], r["e_lv95"], r["n_lv95"], r["mode"])
    graph = Graph(huts, stops, footprints=Footprints({r["id"]: r["geometry"] for r in rows}))
    for r in rows:
        leg = Leg(r["id"], r["from_place"], r["to_place"], r["time_min"], r["distance_m"], r["ascent_m"],
                  r["descent_m"], r["max_category"], r["sac_time_min"], r["category_limit"])
        graph.legs[(leg.frm, leg.to, leg.category_limit)] = leg
        if leg.frm in huts and leg.to in huts:
            graph.hut_out.setdefault(leg.frm, []).append(leg)
        elif leg.to in huts and leg.frm in stops:
            graph.stop_in.setdefault(leg.to, []).append(leg)
        elif leg.frm in huts and leg.to in stops:
            graph.stop_out.setdefault(leg.frm, []).append(leg)
    return graph


_graph_cache: dict[tuple[str, int], tuple[int, Graph]] = {}  # (database file, limit) → (data version, graph)


def cached_graph(conn: sqlite3.Connection, limit: int) -> Graph:
    """load_graph, reused until anything it reads changes (data_version is bumped by triggers, see schema.sql).

    The graph is read-only after loading, so requests can share it."""
    version = conn.execute("SELECT version FROM data_version").fetchone()[0]
    key = (conn.execute("PRAGMA database_list").fetchone()["file"], limit)
    hit = _graph_cache.get(key)
    if hit and hit[0] == version:
        return hit[1]
    graph = load_graph(conn, limit)
    _graph_cache[key] = (version, graph)
    return graph


def night_status(hut: Hut, day: date, catering: Catering, include_unknown: bool) -> Night | None:
    """How the hut can be used on that night (SAC monthly codes), or None if it doesn't fit the request."""
    result = opening.usable(hut.catering, hut.self_service, day.month, catering)
    if result is None or (result[0] == "unknown" and not include_unknown):
        return None
    return Night(hut.id, day, *result)


AvailabilityLookup = Callable[[Hut, date], NightAvailability]


def with_availability(night: Night | None, hut: Hut, day: date, catering: Catering,
                      avail: NightAvailability) -> Night | None:
    """Combine the SAC monthly status with the hut's daily booking calendar (which wins where it is known)."""
    if avail.state in ("full", "closed"):
        return None
    if avail.state == "available":
        status = HRS_STATUS.get(avail.hut_status or "")
        if status is None or (catering != "any" and status != catering):
            return None
        return Night(hut.id, day, status, False, avail)
    return Night(night.hut, night.date, night.status, night.partial, avail) if night else None


def suggest(graph: Graph, req: TourRequest, availability: AvailabilityLookup | None = None) -> list[Tour]:
    lo, hi = req.min_hours * 60, req.max_hours * 60
    target = (lo + hi) / 2

    def day_cost(leg: Leg) -> float:
        return ((leg.time_min - target) / target) ** 2 + CATEGORY_PENALTY * (leg.max_category - 1)

    def in_region(hut_id: int) -> bool:
        if req.bbox is None:
            return True
        h = graph.huts[hut_id]
        west, south, east, north = req.bbox
        return west <= h.lon <= east and south <= h.lat <= north

    def night(hut_id: int, i: int) -> Night | None:
        hut, day = graph.huts[hut_id], req.start_date + timedelta(days=i)
        n = night_status(hut, day, req.catering, req.include_unknown)
        return with_availability(n, hut, day, req.catering, availability(hut, day)) if availability else n

    def near(stop: Stop, point: tuple[float, float]) -> bool:
        return math.hypot(stop.e - point[0], stop.n - point[1]) <= START_RADIUS_M

    def penalty(n: Night) -> float:
        p = UNKNOWN_PENALTY if n.status == "unknown" else PARTIAL_PENALTY if n.partial else 0.0
        return p + (AVAILABILITY_PENALTY[n.availability.state] if n.availability else 0.0)

    def retrace_cost(legs: tuple[Leg, ...], leg: Leg) -> float:
        return RETRACE_PENALTY * graph.footprints.repeated([x.edge_id for x in legs], leg.edge_id)

    # Day 1: stop → first hut. State: (cost, legs, nights)
    states = []
    for hut_id, legs in graph.stop_in.items():
        if not in_region(hut_id) or not (n := night(hut_id, 0)):
            continue
        for leg in legs:
            if lo <= leg.time_min <= hi and (req.start_near is None or near(graph.stops[leg.frm], req.start_near)):
                states.append((day_cost(leg) + penalty(n), (leg,), (n,)))
    states = heapq.nsmallest(BEAM_WIDTH, states, key=lambda s: s[0])

    # Days 2..nights: hut → hut
    for i in range(1, req.nights):
        nxt = []
        for cost, legs, nights in states:
            visited = {n.hut for n in nights}
            for leg in graph.hut_out.get(legs[-1].to, ()):
                if leg.to in visited or not (lo <= leg.time_min <= hi) or not in_region(leg.to):
                    continue
                if n := night(leg.to, i):
                    nxt.append((cost + day_cost(leg) + retrace_cost(legs, leg) + penalty(n), legs + (leg,), nights + (n,)))
        states = heapq.nsmallest(BEAM_WIDTH, nxt, key=lambda s: s[0])

    # Last day: hut → stop. Then order all complete tours by the requested sort (see ranking.py).
    tours = []
    for cost, legs, nights in states:
        start = graph.stops[legs[0].frm]
        ends = [leg for leg in graph.stop_out.get(legs[-1].to, ()) if lo <= leg.time_min <= hi and (
            not req.loop or math.hypot(graph.stops[leg.to].e - start.e, graph.stops[leg.to].n - start.n) <= LOOP_RADIUS_M)]
        for leg in heapq.nsmallest(END_LEGS_PER_STATE, ends, key=lambda x: day_cost(x) + retrace_cost(legs, x)):
            tours.append(scored(graph, legs + (leg,), nights, req.start_date, lo, hi, req.weights))
    key = ranking.sort_key(req.sort, req.weights)
    tours.sort(key=lambda t: key(t.metrics))
    return diversify(tours, req.max_results)


def scored(graph: Graph, legs: tuple[Leg, ...], nights: tuple[Night, ...], start_date: date, lo: float, hi: float,
           weights: dict[str, float]) -> Tour:
    edge_ids = [leg.edge_id for leg in legs]
    m = ranking.metrics(legs, nights, [graph.huts[n.hut].altitude for n in nights], lo, hi,
                        retraced=graph.footprints.retraced(edge_ids), shape=graph.footprints.shape(edge_ids))
    return Tour(m.best(weights), legs, nights, start_date, m)


def build_tour(graph: Graph, start_date: date, places: list[int], limits: list[int], catering: Catering = "any",
               availability: AvailabilityLookup | None = None, min_hours: float = 3, max_hours: float = 6,
               weights: dict[str, float] | None = None) -> Tour:
    """Rebuild a tour from its places and per-day category limits (for export, sharing and, later, editing).

    Raises ValueError if the tour isn't stop → hut → … → stop or a connection no longer exists (e.g. excluded).
    Nights that no longer fit are kept with what we know (status 'closed' or availability 'full'), so callers
    can warn."""
    if places[0] not in graph.stops or places[-1] not in graph.stops or any(p not in graph.huts for p in places[1:-1]):
        raise ValueError("a tour must go stop → hut → … → hut → stop")
    legs = []
    for frm, to, limit in zip(places, places[1:], limits):
        # The edge for exactly this limit, or (if that variant was dropped) the best one at an easier limit
        leg = next((graph.legs[(frm, to, lim)] for lim in range(limit, 0, -1) if (frm, to, lim) in graph.legs), None)
        if leg is None:
            raise ValueError(f"the connection {frm} → {to} is no longer available")
        legs.append(leg)
    legs = tuple(legs)
    nights = []
    for i, leg in enumerate(legs[:-1]):
        hut, day = graph.huts[leg.to], start_date + timedelta(days=i)
        n = night_status(hut, day, catering, include_unknown=True)
        avail = availability(hut, day) if availability else None
        combined = with_availability(n, hut, day, catering, avail) if avail else n
        # Keep nights that no longer fit, with what we know (e.g. availability 'full'), so callers can warn.
        nights.append(combined or Night(hut.id, day, n.status if n else "closed", n.partial if n else False, avail))
    return scored(graph, legs, tuple(nights), start_date, min_hours * 60, max_hours * 60, weights or ranking.DEFAULT_WEIGHTS)


def diversify(tours: list[Tour], k: int) -> list[Tour]:
    """Best tours first, skipping ones that share most of their huts with an already chosen tour, or that use a
    hut already in MAX_TOURS_PER_HUT chosen tours. The hut cap is lifted when it leaves fewer than MIN_RESULTS
    (small areas with few huts)."""
    chosen: list[Tour] = []
    uses: dict[int, int] = {}
    for capped in (True, False):
        for tour in tours:
            huts = {n.hut for n in tour.nights}
            if tour in chosen or (capped and any(uses.get(h, 0) >= MAX_TOURS_PER_HUT for h in huts)):
                continue
            if all(len(huts & {n.hut for n in c.nights}) / len(huts | {n.hut for n in c.nights}) < 0.6 for c in chosen):
                chosen.append(tour)
                for h in huts:
                    uses[h] = uses.get(h, 0) + 1
                if len(chosen) == k:
                    return chosen
        if len(chosen) >= MIN_RESULTS:
            break
    return chosen
