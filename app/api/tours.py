"""Tour suggestions, single tours by id (JSON and GPX), and the stop search used to pick a start point."""

import sqlite3
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator

from app.api.deps import get_db
from app.api.huts import HRS_BOOKING_URL
from app.planner import availability, gpx
from app.planner.tours import (Catering, Graph, Night, Tour, TourRequest, build_tour, load_graph, parse_tour_id,
                               suggest)

router = APIRouter(prefix="/api", tags=["tours"])
AVAILABILITY_ROUNDS = 3  # search, fetch calendars of the huts in the best tours, search again
CANDIDATES_PER_ROUND = 25
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
    graph = load_graph(conn, q.limit)
    params = q.model_dump(exclude={"start_stop", "people", "check_availability"})
    if q.start_stop:
        row = conn.execute("SELECT e_lv95, n_lv95 FROM place WHERE id = ? AND kind = 'stop'", (q.start_stop,)).fetchone()
        if not row:
            raise HTTPException(404, "start stop not found")
        params["start_near"] = (row["e_lv95"], row["n_lv95"])
    req = TourRequest(**params)
    fetched = 0
    if q.check_availability:
        tours, fetched = search_with_availability(conn, graph, req, q.people)
    else:
        tours = suggest(graph, req)
    return {"query": q.model_dump(mode="json"), "tours": tour_json(conn, graph, tours), "calendars_fetched": fetched}


@router.get("/tours/{tour_id}")
def get_tour(tour_id: str, people: int = Query(2, ge=1, le=30), check_availability: bool = False,
             conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """A tour rebuilt from its id (start date, places, per-day trail limits), e.g. for sharing or export."""
    graph, tour = rebuild(conn, tour_id, people, check_availability)
    return tour_json(conn, graph, [tour])[0]


@router.get("/tours/{tour_id}/gpx")
def get_tour_gpx(tour_id: str, conn: sqlite3.Connection = Depends(get_db)) -> Response:
    graph, tour = rebuild(conn, tour_id, people=1, check_availability=False)
    data = tour_json(conn, graph, [tour])[0]
    return Response(gpx.to_gpx(data), media_type="application/gpx+xml",
                    headers={"Content-Disposition": f'attachment; filename="{gpx.filename(data)}"'})


def rebuild(conn: sqlite3.Connection, tour_id: str, people: int, check_availability: bool) -> tuple[Graph, Tour]:
    try:
        start, places, limits = parse_tour_id(tour_id)
    except ValueError:
        raise HTTPException(400, "malformed tour id") from None
    graph = load_graph(conn, 3)  # all trail categories; a tour id names its edges explicitly
    lookup = None
    if check_availability:
        availability.refresh(conn, set(places[1:-1]))
        snapshot = availability.Snapshot(conn, start, start + timedelta(days=len(limits) - 1), people)
        lookup = lambda hut, day: snapshot.night(hut.id, hut.hrs_id is not None, day)  # noqa: E731
    try:
        return graph, build_tour(graph, start, places, limits, availability=lookup)
    except ValueError as err:
        raise HTTPException(404, str(err)) from None


def search_with_availability(conn: sqlite3.Connection, graph: Graph, req: TourRequest,
                             people: int) -> tuple[list[Tour], int]:
    """Search, fetch the booking calendars of huts in the best candidates, and search again with them, so full
    huts drop out. Calendars are cached for availability.CACHE_TTL_S; fetches per request are capped."""
    end = req.start_date + timedelta(days=req.nights)
    wide = TourRequest(**{**req.__dict__, "max_results": CANDIDATES_PER_ROUND})
    fetched = 0
    for _ in range(AVAILABILITY_ROUNDS):
        snapshot = availability.Snapshot(conn, req.start_date, end, people)
        tours = suggest(graph, wide, lambda hut, day: snapshot.night(hut.id, hut.hrs_id is not None, day))
        needed = {n.hut for t in tours for n in t.nights if n.availability and n.availability.state == "not_checked"}
        if not needed or fetched >= availability.MAX_FETCHES:
            break
        fetched += availability.refresh(conn, needed, availability.MAX_FETCHES - fetched)
    snapshot = availability.Snapshot(conn, req.start_date, end, people)
    return suggest(graph, req, lambda hut, day: snapshot.night(hut.id, hut.hrs_id is not None, day)), fetched


# ---------- JSON ----------

def tour_json(conn: sqlite3.Connection, graph: Graph, tours: list[Tour]) -> list[dict]:
    """The one serialisation of tours for the frontend (and for GPX export)."""
    edge_ids = sorted({leg.edge_id for t in tours for leg in t.legs})
    geometry = dict(conn.execute(f"SELECT id, geometry FROM edge WHERE id IN ({','.join('?' * len(edge_ids))})",
                                 edge_ids).fetchall()) if edge_ids else {}
    return [{
        "id": t.id,
        "score": round(t.score, 4),
        "nights": len(t.nights),
        "time_min": sum(leg.time_min for leg in t.legs),
        "distance_m": sum(leg.distance_m for leg in t.legs),
        "ascent_m": sum(leg.ascent_m for leg in t.legs),
        "descent_m": sum(leg.descent_m for leg in t.legs),
        "max_category": max(leg.max_category for leg in t.legs),
        "uncertain_nights": sum(1 for n in t.nights if n.status == "unknown" or n.partial),
        "availability": {s: sum(1 for n in t.nights if n.availability and n.availability.state == s)
                         for s in AVAILABILITY_STATES},
        "days": [{
            "day": i + 1,
            "date": (t.start_date + timedelta(days=i)).isoformat(),
            "from": place_json(graph, leg.frm),
            "to": place_json(graph, leg.to),
            "edge_id": leg.edge_id,
            **{k: getattr(leg, k) for k in ("time_min", "distance_m", "ascent_m", "descent_m", "max_category",
                                            "sac_time_min")},
            "geometry": geometry.get(leg.edge_id),
            "night": night_json(t.nights[i], graph.huts[leg.to].hrs_id) if i < len(t.nights) else None,
        } for i, leg in enumerate(t.legs)],
    } for t in tours]


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
        "booking_url": HRS_BOOKING_URL.format(hrs_id) if hrs_id else None,
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
