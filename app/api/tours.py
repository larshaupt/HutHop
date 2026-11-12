"""Tour suggestions, single tours by id (JSON and GPX), the tour editor's options, a tour from a hut, the stop
search used to pick a start point, and a stop's departures on a day."""

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator

from app import config
from app.api.deps import get_db
from app.clients import geoadmin, hut_reservation
from app.ingest import osm_access
from app.planner import costs, editing, gpx, planning, polyline, ranking, transit
from app.planner.graph import Graph, Leg, RideLeg, cached_graph
from app.planner.tours import Catering, Night, Tour, TourRequest, build_tour, is_lift, parse_tour_id

router = APIRouter(prefix="/api", tags=["tours"])
AVAILABILITY_STATES = ("available", "not_released", "no_booking", "not_checked", "error")


def settings(limit: int = Query(3, ge=1, le=4, description="hardest trail: 1 yellow, 2 red-white, 3 blue-white, "
                                                   "4 also unofficial OSM paths"),
             max_altitude: int | None = Query(None, ge=500, le=5000, description="highest point of a route (m)"),
             avoid_lifts: bool = Query(False, description="no start or end stop served only by mountain lifts"),
             pace: float = Query(1.0, ge=0.5, le=2.0, description="walking time / Swiss standard time")) -> editing.Rules:
    """The app's general settings (kept in the browser), sent with every request that offers or shows routes."""
    return editing.Rules(limit, max_altitude, avoid_lifts, pace)


class TourQuery(BaseModel):
    start_date: date
    nights: int = Field(2, ge=1, le=10)
    min_hours: float = Field(3, ge=0, le=12)
    max_hours: float = Field(6, gt=0, le=12)
    limit: int = Field(3, ge=1, le=4, description="hardest trail: 1 yellow, 2 red-white, 3 blue-white, "
                                                         "4 also unofficial OSM paths")
    catering: Catering = "any"
    bbox: tuple[float, float, float, float] | None = Field(None, description="west, south, east, north (WGS84)")
    start_stop: int | None = None
    loop: bool = False
    include_unknown: bool = True
    avoid_lifts: bool = Field(False, description="no access or exit stop served only by cable cars, chairlifts or "
                                                 "funiculars")
    max_altitude: int | None = Field(None, ge=500, le=5000, description="highest point of a route (m)")
    pace: float = Field(1.0, ge=0.5, le=2.0, description="walking time / Swiss standard time")
    transit: bool = Field(False, description="a day between huts may include one bus, train or cable car ride")
    people: int = Field(2, ge=1, le=30)
    check_availability: bool = True

    @model_validator(mode="after")
    def check_hours(self):
        if self.min_hours > self.max_hours:
            raise ValueError("min_hours must not exceed max_hours")
        if self.start_date < date.today():
            raise ValueError("start_date must not be in the past")
        return self


@router.post("/tours/suggest")
def suggest_tours(q: TourQuery, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """A pool of up to tours.POOL_SIZE varied tours, ordered by "best"; the app sorts them otherwise itself."""
    params = q.model_dump(exclude={"start_stop", "people", "check_availability"})
    # Hours per day are your walking time; the routes are in standard time
    params["min_hours"], params["max_hours"] = round(q.min_hours / q.pace, 2), round(q.max_hours / q.pace, 2)
    if q.start_stop:
        row = conn.execute("SELECT e_lv95, n_lv95 FROM place WHERE id = ? AND kind = 'stop'", (q.start_stop,)).fetchone()
        if not row:
            raise HTTPException(404, "start stop not found")
        params["start_near"] = (row["e_lv95"], row["n_lv95"])
    req = TourRequest(**params, weights=config.ranking_weights())
    started = time.monotonic()
    graph, tours, fetched = planning.search_tours(conn, req, q.people, q.check_availability)
    with conn:
        conn.execute("INSERT INTO search_log (created_at, query, results, best_tour, duration_ms) VALUES (?, ?, ?, ?, ?)",
                     (datetime.now(UTC).isoformat(timespec="seconds"), q.model_dump_json(), len(tours),
                      tours[0].id if tours else None, round((time.monotonic() - started) * 1000)))
    rules = editing.Rules(q.limit, q.max_altitude, q.avoid_lifts, q.pace)
    return {"query": q.model_dump(mode="json"), "tours": tour_json(conn, graph, tours, rules),
            "calendars_fetched": fetched}


@router.get("/tours/{tour_id}")
def get_tour(tour_id: str, people: int = Query(2, ge=1, le=30), check_availability: bool = False,
             fresh: bool = Query(False, description="free beds at most a minute old (the booking helper)"),
             rules: editing.Rules = Depends(settings), conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """A tour rebuilt from its id (start date, places, per-day trail limits, hours), e.g. for sharing or export."""
    graph, tour = rebuild(conn, tour_id, people, check_availability, fresh)
    return tour_json(conn, graph, [tour], rules)[0]


@router.get("/tours/{tour_id}/options")
def tour_options(tour_id: str, rules: editing.Rules = Depends(settings),
                 conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """The tour editor: the tour, and every single edit of it (planner/editing.py) with the tour it gives. Edits
    that bring in a hut or stop come nearest first (`distance_m`: the walk between it and its neighbouring huts in
    the tour, or to / from a stop), then by "best". Availability isn't fetched here; the app checks free beds of the
    tour it switches to. Only edits within the general settings are offered; times are at your pace."""
    graph, tour = rebuild(conn, tour_id, people=1, check_availability=False)
    pace = rules.pace
    weights = config.ranking_weights()
    options = []
    for edit in editing.edits(graph, parse_tour_id(tour_id), rules):
        try:
            variant = build_tour(graph, edit.key, weights=weights)
        except ValueError:
            continue
        option = {"kind": edit.kind, "slot": edit.slot, "tour_id": variant.id, "score": round(variant.score, 3),
                  "place": place_json(graph, edit.place) if edit.place is not None else None,
                  "time_min": round(sum(leg.time_min for leg in variant.legs) * pace),
                  "ascent_m": sum(leg.ascent_m for leg in variant.legs),
                  "days": [round(leg.time_min * pace) for leg in variant.legs]}
        if edit.place is not None:
            near = [leg for leg in variant.legs if edit.place in (leg.frm, leg.to)]
            between_huts = [leg for leg in near if leg.frm in graph.huts and leg.to in graph.huts]
            near = between_huts or near  # a hut's walk to its neighbouring huts, else (one night) to the stops
            option["distance_m"] = sum(leg.distance_m for leg in near)
            option["walk_min"] = round(sum(leg.time_min for leg in near) * pace)
        if edit.kind == "start":
            option["service"] = service_json(graph, edit.place, variant.start_date,
                                             round(variant.legs[0].time_min * pace), "access")
        elif edit.kind == "end":
            option["service"] = service_json(graph, edit.place, variant.start_date + timedelta(
                days=len(variant.legs) - 1), round(variant.legs[-1].time_min * pace), "exit")
        elif edit.kind in ("swap", "insert"):
            night = next(n for n in variant.nights if n.hut == edit.place)
            option["night"] = {"status": night.status, "partial": night.partial}
        elif edit.kind == "limit":
            option["max_category"] = variant.legs[edit.slot].max_category
        options.append(option)
    options.sort(key=lambda o: (o.get("distance_m", 0), -o["score"]))
    return {"tour": tour_json(conn, graph, [tour], rules)[0], "options": options}


@router.get("/huts/{hut_id}/tour")
def tour_from_hut(hut_id: int, start_date: date, min_hours: float = Query(3, ge=0, le=12),
                  max_hours: float = Query(6, gt=0, le=12), rules: editing.Rules = Depends(settings),
                  conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """A one-night tour at a hut, to start the tour editor with: the closest stops that run on those days."""
    graph = cached_graph(conn, 4)  # all trail categories; the settings limit which ones are offered
    if hut_id not in graph.huts:
        raise HTTPException(404, "hut not found")
    hours = (round(min_hours / rules.pace, 2), round(max_hours / rules.pace, 2))  # standard time, like the routes
    key = editing.tour_from_hut(graph, hut_id, start_date, hours, rules)
    if key is None:
        raise HTTPException(404, "no public transport within walking distance of this hut")
    return {"tour_id": build_tour(graph, key, weights=config.ranking_weights()).id}


@router.get("/tours/{tour_id}/gpx")
def get_tour_gpx(tour_id: str, day: int | None = Query(None, ge=1, description="only this day (1-based)"),
                 conn: sqlite3.Connection = Depends(get_db)) -> Response:
    graph, tour = rebuild(conn, tour_id, people=1, check_availability=False)
    data = tour_json(conn, graph, [tour])[0]
    if day is not None and day > len(data["days"]):
        raise HTTPException(404, "no such day")
    return Response(gpx.to_gpx(data, day), media_type="application/gpx+xml",
                    headers={"Content-Disposition": f'attachment; filename="{gpx.filename(data, day)}"'})


@router.get("/tours/{tour_id}/warnings")
def tour_warnings(tour_id: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """Official trail closures and military danger zones along each day's route, with shooting times on that day
    (clients/geoadmin.py). Separate from the tour, so a slow or failing geoportal never holds the tour up."""
    _, tour = rebuild(conn, tour_id, people=1, check_availability=False)
    edge_ids = [w.edge_id for leg in tour.legs for w in leg.walks]
    lines = {r["id"]: r["geometry"] for r in conn.execute(
        f"SELECT id, geometry FROM edge WHERE id IN ({','.join('?' * len(edge_ids))})", edge_ids)}

    def day(i: int) -> dict:
        found = [f for w in tour.legs[i].walks if (line := lines.get(w.edge_id))
                 for f in geoadmin.identify(polyline.decode(line), line)]
        return {"day": i + 1, "closures": geoadmin.closures(found),
                "shooting": geoadmin.shooting(found, tour.start_date + timedelta(days=i))}

    try:
        with ThreadPoolExecutor(4) as pool:
            return {"days": list(pool.map(day, range(len(tour.legs)))), "error": False}
    except Exception:  # network and HTTP errors, unexpected JSON
        return {"days": [], "error": True}


def rebuild(conn: sqlite3.Connection, tour_id: str, people: int, check_availability: bool,
            fresh: bool = False) -> tuple[Graph, Tour]:
    try:
        return planning.rebuild_tour(conn, tour_id, people, check_availability, config.ranking_weights(), fresh)
    except LookupError as err:
        raise HTTPException(404, str(err)) from None
    except ValueError:
        raise HTTPException(400, "malformed tour id") from None


# ---------- JSON ----------

def tour_json(conn: sqlite3.Connection, graph: Graph, tours: list[Tour],
              rules: editing.Rules = editing.Rules()) -> list[dict]:
    """The one serialisation of tours for the frontend (and for GPX export). Walking times are at the settings'
    pace; `outside_settings` lists what of a tour (made earlier, or shared) the settings wouldn't offer."""
    pace = rules.pace
    edge_ids = sorted({w.edge_id for t in tours for leg in t.legs for w in leg.walks})
    lines = {r["id"]: r for r in conn.execute(
        f"SELECT id, geometry, elevation FROM edge WHERE id IN ({','.join('?' * len(edge_ids))})", edge_ids)}
    prices = costs.load_prices(conn, {n.hut for t in tours for n in t.nights})
    contacts = hut_contacts(conn, {n.hut for t in tours for n in t.nights})
    estimates = [costs.tour_cost(t.nights, prices) for t in tours]
    grades = osm_access.grades()
    return [{
        "id": t.id,
        "score": round(t.score, 3),
        "metrics": {m: round(getattr(t.metrics, m), 3) for m in ranking.METRICS} if t.metrics else None,
        "hut_altitude": round(t.metrics.mean_altitude) if t.metrics else None,  # for sorting in the browser
        "nights": len(t.nights),
        "time_min": round(sum(leg.time_min for leg in t.legs) * pace),
        "distance_m": sum(leg.distance_m for leg in t.legs),
        "ascent_m": sum(leg.ascent_m for leg in t.legs),
        "descent_m": sum(leg.descent_m for leg in t.legs),
        "max_category": max(leg.max_category for leg in t.legs),
        "uncertain_nights": sum(1 for n in t.nights if n.status == "unknown" or n.partial),
        "cost": {k: v for k, v in cost.items() if k != "nights"},
        "access": service_json(graph, t.legs[0].frm, t.start_date, round(t.legs[0].time_min * pace), "access"),
        "exit": service_json(graph, t.legs[-1].to, t.start_date + timedelta(days=len(t.legs) - 1),
                             round(t.legs[-1].time_min * pace), "exit"),
        "outside_settings": outside_settings(graph, t, rules),
        "availability": {s: sum(1 for n in t.nights if n.availability and n.availability.state == s)
                         for s in AVAILABILITY_STATES},
        "hours": list(t.hours),
        "days": [{
            "day": i + 1,
            "date": (t.start_date + timedelta(days=i)).isoformat(),
            "from": place_json(graph, leg.frm),
            "to": place_json(graph, leg.to),
            "edge_id": leg.edge_id,
            **{k: getattr(leg, k) for k in ("distance_m", "ascent_m", "descent_m", "max_category", "sac_time_min",
                                            "max_altitude_m")},
            "osm_grade": osm_access.edge_grade(grades, leg.max_category, leg.frm, leg.to),
            "time_min": round(leg.time_min * pace),
            "geometry": lines[leg.edge_id]["geometry"] if leg.edge_id in lines else None,
            "elevation": lines[leg.edge_id]["elevation"] if leg.edge_id in lines else None,  # heights per point
            **(ride_json(graph, leg, lines, pace) if isinstance(leg, RideLeg) else {}),
            "night": night_json(t.nights[i], graph.huts[leg.to].hrs_id)
            | {"cost": cost["nights"][i], "contact": contacts.get(leg.to)}
            if i < len(t.nights) else None,
        } for i, leg in enumerate(t.legs)],
    } for t, cost in zip(tours, estimates)]


def hut_contacts(conn: sqlite3.Connection, hut_ids: set[int]) -> dict[int, dict]:
    """How to book each hut without hut-reservation.org: website, phone, email, and what its website says
    (booking methods and page, see app/ingest/scrape_huts.py)."""
    if not hut_ids:
        return {}
    ids = sorted(hut_ids)
    rows = conn.execute(
        f"""SELECT h.id, h.website, h.phone, h.email, w.booking_methods, w.booking_url FROM hut_effective h
            LEFT JOIN hut_web w ON w.hut_id = h.id WHERE h.id IN ({','.join('?' * len(ids))})""", ids)
    return {r["id"]: {"website": r["website"], "phone": r["phone"], "email": r["email"],
                      "booking_methods": json.loads(r["booking_methods"] or "[]"), "booking_url": r["booking_url"]}
            for r in rows}


def outside_settings(graph: Graph, t: Tour, rules: editing.Rules) -> list[str]:
    """What of a tour the settings wouldn't offer: 'trail' (harder), 'altitude' (higher), 'lift' (a mountain-lift
    stop although avoided)."""
    out = []
    if any(leg.max_category > rules.cap for leg in t.legs):
        out.append("trail")
    if rules.max_altitude is not None and any((leg.max_altitude_m or 0) > rules.max_altitude for leg in t.legs):
        out.append("altitude")
    if rules.avoid_lifts and (is_lift(graph.stops[t.legs[0].frm].mode) or is_lift(graph.stops[t.legs[-1].to].mode)
                              or any(isinstance(leg, RideLeg) and leg.ride.lift for leg in t.legs)):
        out.append("lift")
    return out


def ride_json(graph: Graph, leg: RideLeg, lines: dict, pace: float) -> dict:
    """A day with a ride: the ride (line, stops, times) and its two walks, each with its own route line (the day
    itself has none)."""
    r = leg.ride

    def walk(w: Leg) -> dict:
        line = lines.get(w.edge_id)
        return {"from": place_json(graph, w.frm), "to": place_json(graph, w.to), "time_min": round(w.time_min * pace),
                **{k: getattr(w, k) for k in ("distance_m", "ascent_m", "descent_m", "max_category")},
                "geometry": line["geometry"] if line else None, "elevation": line["elevation"] if line else None}
    return {"ride": {"from": place_json(graph, r.frm), "to": place_json(graph, r.to), "line": r.label,
                     "headsign": r.headsign, "departure": transit.hhmm(r.departure),
                     "arrival": transit.hhmm(r.arrival), "lift": r.lift},
            "walks": [walk(leg.walk_to), walk(leg.walk_from)], "edge_id": None}


def service_json(graph: Graph, stop_id: int, day: date, walk_min: int, role: str) -> dict:
    """A tour's access or exit stop on its day: departures, first and last call, and when you'd reach the first hut
    (access) or have to leave the last hut (exit) with them; `late` / `early` flag tight connections."""
    d = graph.timetable.day(stop_id, day)
    out = {"known": d.known, "calls": d.calls, "first": transit.hhmm(d.first_min), "last": transit.hhmm(d.last_min)}
    if role == "access" and d.first_min is not None:
        out["hut_from"] = transit.hhmm(d.first_min + walk_min)
        out["late"] = d.first_min + walk_min > transit.LATEST_HUT_ARRIVAL
    if role == "exit" and d.last_min is not None:
        out["leave_by"] = transit.hhmm(d.last_min - walk_min)
        out["early"] = d.last_min - walk_min < transit.EARLIEST_HUT_DEPARTURE
    return out


def place_json(graph: Graph, pid: int) -> dict:
    if pid in graph.huts:
        h = graph.huts[pid]
        return {"id": pid, "kind": "hut", "name": h.name, "lat": h.lat, "lon": h.lon}
    s = graph.stops[pid]
    return {"id": pid, "kind": "stop", "name": s.name, "lat": s.lat, "lon": s.lon, "mode": s.mode}


def night_json(night: Night, hrs_id: int | None) -> dict:
    a = night.availability
    return {
        "status": night.status,
        "partial": night.partial,
        "availability": {"state": a.state, "free_beds": a.free_beds, "total_places": a.total_places,
                         "checked_at": a.checked_at} if a else None,
        "booking_url": hut_reservation.BOOKING_URL.format(hrs_id) if hrs_id else None,
    }


# ---------- Stops ----------

@router.get("/stops")
def search_stops(q: str = Query(..., min_length=2), conn: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    """Stops matched by name: prefix matches first, then stations and mountain transport before bus stops."""
    rows = conn.execute(
        """SELECT p.id, p.name, p.lat, p.lon, s.mode FROM place p JOIN stop s ON s.place_id = p.id
           WHERE p.name LIKE :any
           ORDER BY p.name NOT LIKE :prefix,
                    NOT (s.mode LIKE '%train%' OR s.mode LIKE '%cableway%' OR s.mode LIKE '%funicular%'
                         OR s.mode LIKE '%rack_railway%' OR s.mode LIKE '%chairlift%'),
                    length(p.name), p.name
           LIMIT 12""",
        {"any": f"%{q}%", "prefix": f"{q}%"},
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/stops/{stop_id}/departures")
def stop_departures(stop_id: int, day: date = Query(..., alias="date"),
                    conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """A stop's timetable on a day: the times per line and direction (headsign). Only stops with walking
    connections have their trips loaded; `known` is False when no loaded timetable covers the date."""
    graph = cached_graph(conn, 4)
    if stop_id not in graph.stops:
        raise HTTPException(404, "stop not found")
    lines: dict[tuple[str, str], list[str]] = {}
    # shortcut: lists every call, also where a trip ends; GTFS pickup types aren't loaded
    for t, p, _ in graph.timetable.departures(stop_id, day):
        lines.setdefault((p.label, p.headsign), []).append(transit.hhmm(t))
    return {"stop": place_json(graph, stop_id), "date": day.isoformat(),
            "known": graph.timetable.day(stop_id, day).known,
            "lines": [{"line": label, "headsign": headsign, "times": times}
                      for (label, headsign), times in sorted(lines.items())]}
