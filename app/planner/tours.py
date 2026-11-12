"""Tours: stop → hut (one per night) → … → stop over the place graph (graph.py). Search, scoring, rebuilding.

Beam search over hut sequences; public transport is attached to each sequence, not searched. The first hut needs a
stop to get there and the last hut a stop to get home: by default the closest one (walking time) that is served on
that day (timetable, transit.py), optionally without mountain lifts. Their days only have a maximum length, the
days between huts must lie in [min, max] hours. Each hut must be open on its night (SAC monthly codes, or the
booking calendar where known) in the requested way. With `transit`, a day between huts can also be a walk to a
stop, one ride and a walk to the next hut (ride_options). The beam keeps the partial tours with the lowest cost,
which is what each day loses of the "best" score (ranking.py, same weights), and a few trail-limit variants per hut
sequence; complete tours are ranked by "best" and diversified into a pool that the browser sorts. Other stops for a
tour: access_options / exit_options.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.planner import opening, ranking, transit
from app.planner.availability import NightAvailability
from app.planner.graph import Graph, Hut, Leg, RideLeg, Stop, ride_leg

Catering = opening.Wanted
BEAM_WIDTH = 1000
# Partial tours kept per hut sequence: variants differ only in a day's trail limit. Keeping few lets the beam hold
# many different hut sequences; the result list has at most one tour per set of huts anyway.
VARIANTS_PER_CHAIN = 3
LIFT_MODES = {"cableway", "chairlift", "funicular"}  # "avoid mountain lifts": stops served only by these
POOL_SIZE = 300  # varied tours returned per search (usually all there are); the browser sorts them
MAX_TOURS_PER_HUT = 3  # in one result list, so the list isn't the same few huts in different orders
MIN_RESULTS = 10
MAX_RIDES = 2  # days with a ride per tour (TourRequest.transit): mostly a hut tour, not a bus tour
LOOP_RADIUS_M = 3000
START_RADIUS_M = 1500  # a chosen start stop means "start around here" (nearby bus stops, stations)
HRS_STATUS = {"SERVICED": "catered", "UNSERVICED": "self_service"}
DEFAULT_HOURS = (3.0, 6.0)


@dataclass(frozen=True)
class TourRequest:
    start_date: date
    nights: int
    min_hours: float
    max_hours: float
    limit: int = 2  # hardest trail: 1 yellow, 2 red-white, 3 blue-white, 4 unofficial (OSM)
    catering: Catering = "any"
    bbox: tuple[float, float, float, float] | None = None  # west, south, east, north (WGS84) for huts
    start_near: tuple[float, float] | None = None  # LV95 point; tours start within START_RADIUS_M of it
    loop: bool = False
    include_unknown: bool = True
    avoid_lifts: bool = False  # no access or exit stop served only by cable cars, chairlifts or funiculars
    max_altitude: int | None = None  # no route whose highest point is above this (m)
    pace: float = 1.0  # walking time / Swiss standard time; the hours above are already in standard time (the API
    #                    divides them), this only judges the connection times at the start and end stop
    max_results: int = POOL_SIZE
    transit: bool = False  # a day between huts may include one public transport ride (ride_options)
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
        '2027-07-17_123-45-67-89_2-2-3_3-6.5', then the days with a ride, if any: day (0-based), the stop you walk
        to and that walk's limit, the stop you ride to and the next walk's limit, the departure (minutes), e.g.
        '_1.501.2.502.3.545'; several joined by '-'.

        Uses places, not edge ids, so the id survives rebuilds of the edge table; the hours make a shared tour
        score the same as in the search it came from."""
        places = [self.legs[0].frm, *(leg.to for leg in self.legs)]
        rides = [f"{i}.{leg.walk_to.to}.{leg.walk_to.category_limit}.{leg.walk_from.frm}."
                 f"{leg.walk_from.category_limit}.{leg.ride.departure}"
                 for i, leg in enumerate(self.legs) if isinstance(leg, RideLeg)]
        return (f"{self.start_date.isoformat()}_{'-'.join(map(str, places))}_"
                f"{'-'.join(str(leg.category_limit) for leg in self.legs)}_{self.hours[0]:g}-{self.hours[1]:g}"
                + (f"_{'-'.join(rides)}" if rides else ""))


@dataclass(frozen=True)
class TourKey:
    """What a tour id names: enough to rebuild the tour from the graph."""

    start_date: date
    places: list[int]
    limits: list[int]
    hours: tuple[float, float] = DEFAULT_HOURS
    # Days with a ride: day → (stop walked to, its walk's limit, stop ridden to, the next walk's limit, departure)
    rides: dict[int, tuple[int, int, int, int, int]] = field(default_factory=dict)


def parse_tour_id(tour_id: str) -> TourKey:
    """Inverse of Tour.id. Ids without hours (older links) get DEFAULT_HOURS. Raises ValueError if malformed."""
    parts = tour_id.split("_")
    if len(parts) not in (3, 4, 5):
        raise ValueError("malformed tour id")
    place_ids, limit_ids = [int(p) for p in parts[1].split("-")], [int(x) for x in parts[2].split("-")]
    if len(place_ids) < 3 or len(limit_ids) != len(place_ids) - 1 or not set(limit_ids) <= {1, 2, 3, 4}:
        raise ValueError("malformed tour id")
    hours = DEFAULT_HOURS
    if len(parts) >= 4:
        lo, hi = (float(h) for h in parts[3].split("-"))
        if not (0 <= lo <= hi <= 24 and hi > 0):  # standard time: the API allows 12 h at pace 0.5
            raise ValueError("malformed tour id")
        hours = (lo, hi)
    rides = {}
    for token in parts[4].split("-") if len(parts) == 5 else ():
        day, a, la, b, lb, dep = (int(x) for x in token.split("."))
        if not (0 < day < len(limit_ids) - 1 and {la, lb} <= {1, 2, 3, 4} and 0 <= dep < 48 * 60):
            raise ValueError("malformed tour id")
        rides[day] = (a, la, b, lb, dep)
    return TourKey(date.fromisoformat(parts[0]), place_ids, limit_ids, hours, rides)


def night_status(hut: Hut, day: date, catering: Catering, include_unknown: bool) -> Night | None:
    """How the hut can be used on that night (SAC monthly codes and website seasons), or None if it doesn't fit
    the request."""
    result = opening.on_day(hut.catering, hut.self_service, hut.seasons, day, catering)
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


def below(leg: Leg, max_altitude: int | None) -> bool:
    """The route's highest point is within the altitude limit (None: no limit; routes without heights pass)."""
    return max_altitude is None or leg.max_altitude_m is None or leg.max_altitude_m <= max_altitude


def is_lift(mode: str | None) -> bool:
    """A stop served only by mountain lifts, from its mode list (e.g. "cableway,funicular")."""
    return set((mode or "").split(",")) <= LIFT_MODES


def access_options(graph: Graph, hut_id: int, day: date, max_minutes: float, avoid_lifts: bool = False,
                   near: tuple[float, float] | None = None, max_altitude: int | None = None,
                   pace: float = 1.0) -> list[Leg]:
    """Stop → hut connections usable on `day`: served that day (or no timetable for the date), at most
    `max_minutes` (standard time), within START_RADIUS_M of `near` if given, below `max_altitude`. Closest (walking
    time) first, but stops whose first connection gets you to the hut (at `pace`) only after
    transit.LATEST_HUT_ARRIVAL after all others."""
    legs = []
    for leg in graph.stop_in.get(hut_id, ()):
        stop = graph.stops[leg.frm]
        if leg.time_min > max_minutes or (avoid_lifts and is_lift(stop.mode)) or not below(leg, max_altitude) or \
                (near is not None and math.hypot(stop.e - near[0], stop.n - near[1]) > START_RADIUS_M) or \
                not graph.timetable.day(stop.id, day).served:
            continue
        legs.append(leg)

    def late(leg: Leg) -> bool:
        first = graph.timetable.day(leg.frm, day).first_min
        return first is not None and first + leg.time_min * pace > transit.LATEST_HUT_ARRIVAL
    return sorted(legs, key=lambda x: (late(x), x.time_min, x.max_category))


def exit_options(graph: Graph, hut_id: int, day: date, max_minutes: float, avoid_lifts: bool = False,
                 loop_start: Stop | None = None, max_altitude: int | None = None, pace: float = 1.0) -> list[Leg]:
    """Hut → stop connections usable on `day`, closest first (but stops whose last connection means leaving the hut
    before transit.EARLIEST_HUT_DEPARTURE after all others); for a loop, within LOOP_RADIUS_M of its start."""
    legs = []
    for leg in graph.stop_out.get(hut_id, ()):
        stop = graph.stops[leg.to]
        if leg.time_min > max_minutes or (avoid_lifts and is_lift(stop.mode)) or not below(leg, max_altitude) or \
                (loop_start is not None and
                 math.hypot(stop.e - loop_start.e, stop.n - loop_start.n) > LOOP_RADIUS_M) or \
                not graph.timetable.day(stop.id, day).served:
            continue
        legs.append(leg)

    def early(leg: Leg) -> bool:
        last = graph.timetable.day(leg.to, day).last_min
        return last is not None and last - leg.time_min * pace < transit.EARLIEST_HUT_DEPARTURE
    return sorted(legs, key=lambda x: (early(x), x.time_min, x.max_category))


def ride_options(graph: Graph, hut_id: int, day: date, lo: float, hi: float, avoid_lifts: bool = False,
                 max_altitude: int | None = None, pace: float = 1.0) -> list[RideLeg]:
    """Days from a hut to other huts with one public transport ride on `day`: walk to a stop, ride, walk to the
    hut. The walks together take lo to hi minutes (standard time); you leave the hut at
    transit.EARLIEST_HUT_DEPARTURE and arrive by transit.LATEST_HUT_ARRIVAL (walks at `pace`). Per hut the one with
    the walking time closest to the middle of the hours, then the earliest arrival."""
    target = (lo + hi) / 2
    best: dict[int, tuple[tuple[float, float], RideLeg]] = {}
    for out in graph.stop_out.get(hut_id, ()):
        if out.time_min >= hi or not below(out, max_altitude):
            continue
        rides = graph.timetable.rides_from(out.to, day, transit.EARLIEST_HUT_DEPARTURE + out.time_min * pace,
                                           transit.LATEST_HUT_ARRIVAL, avoid_lifts)
        for ride in rides.values():
            for inn in graph.from_stop.get(ride.to, ()):
                walk = out.time_min + inn.time_min
                arrival = ride.arrival + inn.time_min * pace
                if inn.to == hut_id or not lo <= walk <= hi or arrival > transit.LATEST_HUT_ARRIVAL or \
                        not below(inn, max_altitude):
                    continue
                rank = (abs(walk - target), arrival)
                if inn.to not in best or rank < best[inn.to][0]:
                    best[inn.to] = (rank, ride_leg(out, ride, inn))
    return [leg for _, leg in best.values()]


def suggest(graph: Graph, req: TourRequest, availability: AvailabilityLookup | None = None) -> list[Tour]:
    lo, hi = req.min_hours * 60, req.max_hours * 60
    target = (lo + hi) / 2

    # The beam cost is the "best" score's loss (ranking.metrics), split into what each day adds: weighted like the
    # score, so the beam keeps what the final order prefers. Shape needs the whole tour and only counts at the end.
    w = req.weights
    n_legs, n_nights = req.nights + 1, req.nights
    w_fit, w_ease, w_approach = w.get("fit", 0), w.get("ease", 0), w.get("approach", 0)
    w_fresh, w_even = w.get("fresh", 0), w.get("even", 0)
    w_certainty, w_beds, w_altitude = w.get("certainty", 0), w.get("beds", 0), w.get("altitude", 0)
    if not any(w.get(m, 0) for m in ranking.METRICS):
        w_fit = 1  # "best" is then the day-length fit alone

    def day_cost(leg: Leg, first_or_last: bool = False) -> float:
        c = (w_fit * ((leg.time_min - target) / target) ** 2 + w_ease * (min(leg.max_category, 3) - 1) / 2) / n_legs
        return c + w_approach * leg.time_min / (2 * hi) if first_or_last else c

    def even_cost(legs: tuple[Leg, ...], leg: Leg) -> float:
        """Difference from the earlier days between huts (legs[0] is the day from public transport)."""
        between = legs[1:]
        if not between:
            return 0.0
        mean = sum(x.time_min for x in between) / len(between)
        return w_even * 2 * abs(leg.time_min - mean) / mean / max(1, n_legs - 2) if mean else 0.0

    def retrace_cost(r: float) -> float:
        return w_fresh * 2 * r / n_legs

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

    def penalty(n: Night) -> float:
        uncertain = 1.0 if n.status == "unknown" or n.partial else 0.0
        beds = ranking.BED_VALUE.get(n.availability.state, 0.5) if n.availability else 0.5
        altitude = graph.huts[n.hut].altitude
        alt = ranking.clip((altitude - 1500) / 1500) if altitude else 0.5
        return (w_certainty * uncertain + w_beds * (1 - beds) + w_altitude * (1 - alt)) / n_nights

    fp = graph.footprints

    def cells(leg: Leg) -> int:
        return sum(len(fp.cells(w.edge_id)) for w in leg.walks)

    def repeated(legs: tuple[Leg, ...], leg: Leg) -> float:
        earlier = [w.edge_id for x in legs for w in x.walks]
        if len(leg.walks) == 1:
            return fp.repeated(earlier, leg.edge_id)
        r = 0.0  # a day with a ride: its walks weighted by length, the second also against the first
        for w in leg.walks:
            r += fp.repeated(earlier, w.edge_id) * len(fp.cells(w.edge_id))
            earlier.append(w.edge_id)
        return r / cells(leg) if cells(leg) else 0.0

    end_day = req.start_date + timedelta(days=req.nights)
    exits: dict[int, list[Leg]] = {}

    def exit_legs(hut_id: int) -> list[Leg]:
        if hut_id not in exits:
            exits[hut_id] = exit_options(graph, hut_id, end_day, hi, req.avoid_lifts, None, req.max_altitude, req.pace)
        return exits[hut_id]

    # Day 1: the closest usable stop → first hut. State: (cost, legs, nights, walked): walked = cells of the tour
    # so far that retrace earlier days (Footprints.retraced, summed day by day, so complete tours needn't compute it
    # again).
    states = []
    for hut_id in graph.stop_in:
        if not in_region(hut_id) or not (n := night(hut_id, 0)) or (req.nights == 1 and not exit_legs(hut_id)):
            continue
        access = access_options(graph, hut_id, req.start_date, hi, req.avoid_lifts, req.start_near, req.max_altitude,
                                req.pace)
        if access:
            states.append((day_cost(access[0], True) + penalty(n), (access[0],), (n,), 0.0))
    states = best_per_chain(states, 1)

    rides: dict[tuple[int, int], list[RideLeg]] = {}

    def day_legs(hut_id: int, i: int) -> list[Leg]:
        """The hut's walks to other huts, and with `transit` its days with a ride on day i."""
        if not req.transit:
            return graph.hut_out.get(hut_id, [])
        if (hut_id, i) not in rides:
            rides[(hut_id, i)] = ride_options(graph, hut_id, req.start_date + timedelta(days=i), lo, hi,
                                              req.avoid_lifts, req.max_altitude, req.pace)
        return [*graph.hut_out.get(hut_id, ()), *rides[(hut_id, i)]]

    # Days 2..nights: hut → hut; the last hut needs a stop to get home
    for i in range(1, req.nights):
        nxt = []
        for cost, legs, nights, walked in states:
            visited = {n.hut for n in nights}
            for leg in day_legs(legs[-1].to, i):
                if leg.to in visited or not (lo <= leg.time_min <= hi) or not in_region(leg.to) or \
                        not below(leg, req.max_altitude) or \
                        (i == req.nights - 1 and not exit_legs(leg.to)) or \
                        (isinstance(leg, RideLeg) and sum(isinstance(x, RideLeg) for x in legs) >= MAX_RIDES):
                    continue
                if n := night(leg.to, i):
                    r = repeated(legs, leg)
                    nxt.append((cost + day_cost(leg) + even_cost(legs, leg) + retrace_cost(r) + penalty(n),
                                legs + (leg,), nights + (n,), walked + r * cells(leg)))
        states = best_per_chain(nxt, VARIANTS_PER_CHAIN)

    # Last day: last hut → the closest usable stop (for a loop, near the first stop)
    tours = []
    for _, legs, nights, walked in states:
        start = graph.stops[legs[0].frm]
        ends = exit_options(graph, legs[-1].to, end_day, hi, req.avoid_lifts, start, req.max_altitude, req.pace) \
            if req.loop \
            else exit_legs(legs[-1].to)
        if not ends:
            continue
        # The first option (closest with a usable connection); between equally good ones, the one that walks less of
        # the tour's trail again
        r, leg = min(((repeated(legs, x), x) for x in ends if x.time_min == ends[0].time_min), key=lambda x: x[0])
        tour_legs = legs + (leg,)
        total = sum(cells(x) for x in tour_legs)
        retraced = min(1.0, (walked + r * len(fp.cells(leg.edge_id))) / total) if total else 0.0
        tours.append(scored(graph, tour_legs, nights, req.start_date, (req.min_hours, req.max_hours),
                            req.weights, retraced))
    tours.sort(key=lambda t: -t.score)
    return diversify(tours, req.max_results)


def best_per_chain(states: list[tuple], variants: int) -> list[tuple]:
    """The BEAM_WIDTH cheapest states (cost first), at most `variants` per hut sequence."""
    states.sort(key=lambda s: s[0])
    kept, count = [], {}
    for state in states:
        chain = tuple(n.hut for n in state[2])
        if count.get(chain, 0) < variants:
            count[chain] = count.get(chain, 0) + 1
            kept.append(state)
            if len(kept) == BEAM_WIDTH:
                break
    return kept


def scored(graph: Graph, legs: tuple[Leg, ...], nights: tuple[Night, ...], start_date: date,
           hours: tuple[float, float], weights: dict[str, float], retraced: float | None = None) -> Tour:
    """A tour with its metrics and "best" score. `retraced` if the caller already knows it (the search does)."""
    edge_ids = [w.edge_id for leg in legs for w in leg.walks]
    if retraced is None:
        retraced = graph.footprints.retraced(edge_ids)
    m = ranking.metrics(legs, nights, [graph.huts[n.hut].altitude for n in nights], hours[0] * 60, hours[1] * 60,
                        retraced=retraced, shape=graph.footprints.shape(edge_ids))
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

    def edge(frm: int, to: int, limit: int) -> Leg:
        # The edge for exactly this limit, or (if that variant was dropped) the best one at an easier limit
        leg = next((graph.legs[(frm, to, lim)] for lim in range(limit, 0, -1) if (frm, to, lim) in graph.legs), None)
        if leg is None:
            raise ValueError(f"the connection {frm} → {to} is no longer available")
        return leg

    legs = []
    for i, (frm, to, limit) in enumerate(zip(places, places[1:], limits)):
        if i not in key.rides:
            legs.append(edge(frm, to, limit))
            continue
        a, la, b, lb, departure = key.rides[i]
        ride = graph.timetable.rides_from(a, start_date + timedelta(days=i), departure, departure).get(b)
        if ride is None:
            raise ValueError(f"the ride {a} → {b} no longer runs")
        legs.append(ride_leg(edge(frm, a, la), ride, edge(b, to, lb)))
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
