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
from typing import Literal

from app.planner.availability import NightAvailability

Catering = Literal["any", "catered", "self_service"]
BEAM_WIDTH = 3000
END_LEGS_PER_STATE = 2
LOOP_RADIUS_M = 3000
START_RADIUS_M = 1500  # a chosen start stop means "start around here" (nearby bus stops, stations)
UNKNOWN_PENALTY = 0.15
PARTIAL_PENALTY = 0.05
CATEGORY_PENALTY = 0.01  # per day and trail category above yellow: easier wins between otherwise equal tours
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
    hut_out: dict[int, list[Leg]] = field(default_factory=dict)   # hut → hut
    stop_in: dict[int, list[Leg]] = field(default_factory=dict)   # stop → hut, keyed by hut
    stop_out: dict[int, list[Leg]] = field(default_factory=dict)  # hut → stop, keyed by hut


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
    max_results: int = 10


@dataclass(frozen=True)
class Night:
    hut: int
    date: date
    status: str  # 'catered' | 'self_service' | 'unknown'
    partial: bool
    availability: NightAvailability | None = None


@dataclass(frozen=True)
class Tour:
    score: float
    legs: tuple[Leg, ...]
    nights: tuple[Night, ...]


def load_graph(conn: sqlite3.Connection, limit: int) -> Graph:
    """Huts, stops and all non-excluded edges up to a trail category limit.

    A place pair can have one edge per category limit (only stored when the harder trail is faster). All of
    them are kept as alternatives, so allowing harder trails only ever adds options."""
    huts = {}
    months = {}
    for r in conn.execute("SELECT hut_id, month, catering, self_service FROM hut_month_effective ORDER BY hut_id, month"):
        c, s = months.setdefault(r["hut_id"], ([], []))
        c.append(str(r["catering"] if r["catering"] is not None else 2))
        s.append(str(r["self_service"] if r["self_service"] is not None else 2))
    for r in conn.execute("SELECT id, name, lat, lon, e_lv95, n_lv95, services, hrs_id FROM hut_effective"):
        c, s = months.get(r["id"], (["2"] * 12, ["2"] * 12))
        huts[r["id"]] = Hut(r["id"], r["name"], r["lat"], r["lon"], r["e_lv95"], r["n_lv95"], "".join(c), "".join(s),
                            json.loads(r["services"] or "{}").get("half_board", False), r["hrs_id"])
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
    graph = Graph(huts, stops)
    for r in rows:
        leg = Leg(r["id"], r["from_place"], r["to_place"], r["time_min"], r["distance_m"], r["ascent_m"],
                  r["descent_m"], r["max_category"], r["sac_time_min"])
        if leg.frm in huts and leg.to in huts:
            graph.hut_out.setdefault(leg.frm, []).append(leg)
        elif leg.to in huts and leg.frm in stops:
            graph.stop_in.setdefault(leg.to, []).append(leg)
        elif leg.frm in huts and leg.to in stops:
            graph.stop_out.setdefault(leg.frm, []).append(leg)
    return graph


def night_status(hut: Hut, day: date, catering: Catering, include_unknown: bool) -> Night | None:
    """How the hut can be used on that night, or None if it doesn't fit the request."""
    if set(hut.catering + hut.self_service) == {"2"}:
        return Night(hut.id, day, "unknown", False) if include_unknown else None
    c, s = hut.catering[day.month - 1], hut.self_service[day.month - 1]
    if catering in ("any", "catered") and c in "01":
        return Night(hut.id, day, "catered", c == "1")
    if catering in ("any", "self_service") and s in "01":
        return Night(hut.id, day, "self_service", s == "1")
    return None


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
                    nxt.append((cost + day_cost(leg) + penalty(n), legs + (leg,), nights + (n,)))
        states = heapq.nsmallest(BEAM_WIDTH, nxt, key=lambda s: s[0])

    # Last day: hut → stop
    tours = []
    for cost, legs, nights in states:
        start = graph.stops[legs[0].frm]
        ends = [leg for leg in graph.stop_out.get(legs[-1].to, ()) if lo <= leg.time_min <= hi and (
            not req.loop or math.hypot(graph.stops[leg.to].e - start.e, graph.stops[leg.to].n - start.n) <= LOOP_RADIUS_M)]
        for leg in heapq.nsmallest(END_LEGS_PER_STATE, ends, key=day_cost):
            all_legs = legs + (leg,)
            tours.append(Tour((cost + day_cost(leg)) / len(all_legs), all_legs, nights))
    tours.sort(key=lambda t: t.score)
    return diversify(tours, req.max_results)


def diversify(tours: list[Tour], k: int) -> list[Tour]:
    """Best tours first, skipping ones that share most of their huts with an already chosen tour."""
    chosen: list[Tour] = []
    for tour in tours:
        huts = {n.hut for n in tour.nights}
        if all(len(huts & {n.hut for n in c.nights}) / len(huts | {n.hut for n in c.nights}) < 0.6 for c in chosen):
            chosen.append(tour)
            if len(chosen) == k:
                break
    return chosen
