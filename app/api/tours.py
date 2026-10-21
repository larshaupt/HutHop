"""Tour suggestions, single tours by id (JSON and GPX), the tour editor's options, a tour from a hut, and the
stop search used to pick a start point."""

import sqlite3
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator

from app import config
from app.api.deps import get_db
from app.clients import hut_reservation
from app.planner import costs, editing, gpx, planning, ranking, transit
from app.planner.graph import Graph, cached_graph
from app.planner.tours import Catering, Night, Tour, TourRequest, build_tour, parse_tour_id

router = APIRouter(prefix="/api", tags=["tours"])
AVAILABILITY_STATES = ("available", "not_released", "no_booking", "not_checked", "error")


class TourQuery(BaseModel):
    start_date: date
    nights: int = Field(2, ge=1, le=10)
    min_hours: float = Field(3, ge=0, le=12)
    max_hours: float = Field(6, gt=0, le=12)
    limit: int = Field(2, ge=1, le=3, description="hardest trail: 1 yellow, 2 red-white, 3 blue-white")
    catering: Catering = "any"
    bbox: tuple[float, float, float, float] | None = Field(None, description="west, south, east, north (WGS84)")
    start_stop: int | None = None
    loop: bool = False
    include_unknown: bool = True
    avoid_lifts: bool = Field(False, description="no access or exit stop served only by cable cars, chairlifts or "
                                                 "funiculars")
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
    if q.start_stop:
        row = conn.execute("SELECT e_lv95, n_lv95 FROM place WHERE id = ? AND kind = 'stop'", (q.start_stop,)).fetchone()
        if not row:
            raise HTTPException(404, "start stop not found")
        params["start_near"] = (row["e_lv95"], row["n_lv95"])
    req = TourRequest(**params, weights=config.ranking_weights())
    graph, tours, fetched = planning.search_tours(conn, req, q.people, q.check_availability)
    return {"query": q.model_dump(mode="json"), "tours": tour_json(conn, graph, tours), "calendars_fetched": fetched}


@router.get("/tours/{tour_id}")
def get_tour(tour_id: str, people: int = Query(2, ge=1, le=30), check_availability: bool = False,
             conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """A tour rebuilt from its id (start date, places, per-day trail limits, hours), e.g. for sharing or export."""
    graph, tour = rebuild(conn, tour_id, people, check_availability)
    return tour_json(conn, graph, [tour])[0]


@router.get("/tours/{tour_id}/options")
def tour_options(tour_id: str, limit: int = Query(2, ge=1, le=3, description="trail limit for days that change"),
                 conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """The tour editor: the tour, and every single edit of it (planner/editing.py) with the tour it gives. Edits
    that bring in a hut or stop come nearest first (`distance_m`: the walk between it and its neighbouring huts in
    the tour, or to / from a stop), then by "best". Availability isn't fetched here; the app checks free beds of the
    tour it switches to."""
    graph, tour = rebuild(conn, tour_id, people=1, check_availability=False)
    weights = config.ranking_weights()
    options = []
    for edit in editing.edits(graph, parse_tour_id(tour_id), limit):
        try:
            variant = build_tour(graph, edit.key, weights=weights)
        except ValueError:
            continue
        option = {"kind": edit.kind, "slot": edit.slot, "tour_id": variant.id, "score": round(variant.score, 3),
                  "place": place_json(graph, edit.place) if edit.place is not None else None,
                  "time_min": sum(leg.time_min for leg in variant.legs),
                  "ascent_m": sum(leg.ascent_m for leg in variant.legs),
                  "days": [leg.time_min for leg in variant.legs]}
        if edit.place is not None:
            near = [leg for leg in variant.legs if edit.place in (leg.frm, leg.to)]
            between_huts = [leg for leg in near if leg.frm in graph.huts and leg.to in graph.huts]
            near = between_huts or near  # a hut's walk to its neighbouring huts, else (one night) to the stops
            option["distance_m"] = sum(leg.distance_m for leg in near)
            option["walk_min"] = sum(leg.time_min for leg in near)
        if edit.kind == "start":
            option["service"] = service_json(graph, edit.place, variant.start_date, variant.legs[0].time_min,
                                             "access")
        elif edit.kind == "end":
            option["service"] = service_json(graph, edit.place, variant.start_date + timedelta(
                days=len(variant.legs) - 1), variant.legs[-1].time_min, "exit")
        elif edit.kind in ("swap", "insert"):
            night = next(n for n in variant.nights if n.hut == edit.place)
            option["night"] = {"status": night.status, "partial": night.partial}
        elif edit.kind == "limit":
            option["max_category"] = variant.legs[edit.slot].max_category
        options.append(option)
    options.sort(key=lambda o: (o.get("distance_m", 0), -o["score"]))
    return {"tour": tour_json(conn, graph, [tour])[0], "options": options}


@router.get("/huts/{hut_id}/tour")
def tour_from_hut(hut_id: int, start_date: date, min_hours: float = Query(3, ge=0, le=12),
                  max_hours: float = Query(6, gt=0, le=12), limit: int = Query(2, ge=1, le=3),
                  conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """A one-night tour at a hut, to start the tour editor with: the closest stops that run on those days."""
    graph = cached_graph(conn, 3)
    if hut_id not in graph.huts:
        raise HTTPException(404, "hut not found")
    key = editing.tour_from_hut(graph, hut_id, start_date, (min_hours, max_hours), limit)
    if key is None:
        raise HTTPException(404, "no public transport within walking distance of this hut")
    return {"tour_id": build_tour(graph, key, weights=config.ranking_weights()).id}


@router.get("/tours/{tour_id}/gpx")
def get_tour_gpx(tour_id: str, conn: sqlite3.Connection = Depends(get_db)) -> Response:
    graph, tour = rebuild(conn, tour_id, people=1, check_availability=False)
    data = tour_json(conn, graph, [tour])[0]
    return Response(gpx.to_gpx(data), media_type="application/gpx+xml",
                    headers={"Content-Disposition": f'attachment; filename="{gpx.filename(data)}"'})


def rebuild(conn: sqlite3.Connection, tour_id: str, people: int, check_availability: bool) -> tuple[Graph, Tour]:
    try:
        return planning.rebuild_tour(conn, tour_id, people, check_availability, config.ranking_weights())
    except LookupError as err:
        raise HTTPException(404, str(err)) from None
    except ValueError:
        raise HTTPException(400, "malformed tour id") from None


# ---------- JSON ----------

def tour_json(conn: sqlite3.Connection, graph: Graph, tours: list[Tour]) -> list[dict]:
    """The one serialisation of tours for the frontend (and for GPX export)."""
    edge_ids = sorted({leg.edge_id for t in tours for leg in t.legs})
    lines = {r["id"]: r for r in conn.execute(
        f"SELECT id, geometry, elevation FROM edge WHERE id IN ({','.join('?' * len(edge_ids))})", edge_ids)}
    prices = costs.load_prices(conn, {n.hut for t in tours for n in t.nights})
    estimates = [costs.tour_cost(t.nights, prices) for t in tours]
    return [{
        "id": t.id,
        "score": round(t.score, 3),
        "metrics": {m: round(getattr(t.metrics, m), 3) for m in ranking.METRICS} if t.metrics else None,
        "hut_altitude": round(t.metrics.mean_altitude) if t.metrics else None,  # for sorting in the browser
        "nights": len(t.nights),
        "time_min": sum(leg.time_min for leg in t.legs),
        "distance_m": sum(leg.distance_m for leg in t.legs),
        "ascent_m": sum(leg.ascent_m for leg in t.legs),
        "descent_m": sum(leg.descent_m for leg in t.legs),
        "max_category": max(leg.max_category for leg in t.legs),
        "uncertain_nights": sum(1 for n in t.nights if n.status == "unknown" or n.partial),
        "cost": {k: v for k, v in cost.items() if k != "nights"},
        "access": service_json(graph, t.legs[0].frm, t.start_date, t.legs[0].time_min, "access"),
        "exit": service_json(graph, t.legs[-1].to, t.start_date + timedelta(days=len(t.legs) - 1),
                             t.legs[-1].time_min, "exit"),
        "availability": {s: sum(1 for n in t.nights if n.availability and n.availability.state == s)
                         for s in AVAILABILITY_STATES},
        "hours": list(t.hours),
        "days": [{
            "day": i + 1,
            "date": (t.start_date + timedelta(days=i)).isoformat(),
            "from": place_json(graph, leg.frm),
            "to": place_json(graph, leg.to),
            "edge_id": leg.edge_id,
            **{k: getattr(leg, k) for k in ("time_min", "distance_m", "ascent_m", "descent_m", "max_category",
                                            "sac_time_min")},
            "geometry": lines[leg.edge_id]["geometry"] if leg.edge_id in lines else None,
            "elevation": lines[leg.edge_id]["elevation"] if leg.edge_id in lines else None,  # heights per point
            "night": night_json(t.nights[i], graph.huts[leg.to].hrs_id) | {"cost": cost["nights"][i]}
            if i < len(t.nights) else None,
        } for i, leg in enumerate(t.legs)],
    } for t, cost in zip(tours, estimates)]


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
