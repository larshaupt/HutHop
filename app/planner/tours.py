"""Tours: stop → hut (one per night) → … → stop over the place graph (graph.py). Search, scoring, rebuilding.

Beam search over hut sequences. Each day is one edge; its hiking time must lie in [min, max]. Each hut must be
open on its night (SAC monthly codes, or the booking calendar where known) in the requested way. The beam keeps
partial tours with days near the middle of the range and little retraced trail; complete tours are ranked by the
"best" score (ranking.py) and diversified into a pool that the browser sorts.
"""

import heapq
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.planner import opening, ranking
from app.planner.availability import NightAvailability
from app.planner.graph import Graph, Hut, Leg, Stop

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
DEFAULT_HOURS = (3.0, 6.0)


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
    hours: tuple[float, float] = DEFAULT_HOURS  # the requested hours per day the tour was scored for

    @property
    def id(self) -> str:
        """Start date, place ids, each day's category limit and the hours per day, e.g.
        '2027-07-17_123-45-67-89_2-2-3_3-6.5'.

        Uses places, not edge ids, so the id survives rebuilds of the edge table; the hours make a shared tour
        score the same as in the search it came from."""
        places = [self.legs[0].frm, *(leg.to for leg in self.legs)]
        return (f"{self.start_date.isoformat()}_{'-'.join(map(str, places))}_"
                f"{'-'.join(str(leg.category_limit) for leg in self.legs)}_{self.hours[0]:g}-{self.hours[1]:g}")


@dataclass(frozen=True)
class TourKey:
    """What a tour id names: enough to rebuild the tour from the graph."""

    start_date: date
    places: list[int]
    limits: list[int]
    hours: tuple[float, float] = DEFAULT_HOURS


def parse_tour_id(tour_id: str) -> TourKey:
    """Inverse of Tour.id. Ids without hours (older links) get DEFAULT_HOURS. Raises ValueError if malformed."""
    parts = tour_id.split("_")
    if len(parts) not in (3, 4):
        raise ValueError("malformed tour id")
    place_ids, limit_ids = [int(p) for p in parts[1].split("-")], [int(x) for x in parts[2].split("-")]
    if len(place_ids) < 3 or len(limit_ids) != len(place_ids) - 1 or not set(limit_ids) <= {1, 2, 3}:
        raise ValueError("malformed tour id")
    hours = DEFAULT_HOURS
    if len(parts) == 4:
        lo, hi = (float(h) for h in parts[3].split("-"))
        if not 0 <= lo <= hi <= 12:
            raise ValueError("malformed tour id")
        hours = (lo, hi)
    return TourKey(date.fromisoformat(parts[0]), place_ids, limit_ids, hours)


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

    # Last day: hut → stop. Then order all complete tours by "best" (other orders are applied in the browser).
    tours = []
    for cost, legs, nights in states:
        start = graph.stops[legs[0].frm]
        ends = [leg for leg in graph.stop_out.get(legs[-1].to, ()) if lo <= leg.time_min <= hi and (
            not req.loop or math.hypot(graph.stops[leg.to].e - start.e, graph.stops[leg.to].n - start.n) <= LOOP_RADIUS_M)]
        for leg in heapq.nsmallest(END_LEGS_PER_STATE, ends, key=lambda x: day_cost(x) + retrace_cost(legs, x)):
            tours.append(scored(graph, legs + (leg,), nights, req.start_date, (req.min_hours, req.max_hours),
                                req.weights))
    tours.sort(key=lambda t: -t.score)
    return diversify(tours, req.max_results)


def scored(graph: Graph, legs: tuple[Leg, ...], nights: tuple[Night, ...], start_date: date,
           hours: tuple[float, float], weights: dict[str, float]) -> Tour:
    edge_ids = [leg.edge_id for leg in legs]
    m = ranking.metrics(legs, nights, [graph.huts[n.hut].altitude for n in nights], hours[0] * 60, hours[1] * 60,
                        retraced=graph.footprints.retraced(edge_ids), shape=graph.footprints.shape(edge_ids))
    return Tour(m.best(weights), legs, nights, start_date, m, hours)


def build_tour(graph: Graph, key: TourKey, catering: Catering = "any", availability: AvailabilityLookup | None = None,
               weights: dict[str, float] | None = None) -> Tour:
    """Rebuild a tour from its key (places, per-day category limits, hours) for export, sharing and editing.

    Raises ValueError if the tour isn't stop → hut → … → stop or a connection no longer exists (e.g. excluded).
    Nights that no longer fit are kept with what we know (status 'closed' or availability 'full'), so callers
    can warn."""
    start_date, places, limits = key.start_date, key.places, key.limits
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
    return scored(graph, legs, tuple(nights), start_date, key.hours, weights or ranking.DEFAULT_WEIGHTS)


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
