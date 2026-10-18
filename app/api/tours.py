"""Tour suggestions and the stop search used to pick a start point."""

import sqlite3
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from app.api.huts import get_db
from app.planner.tours import Catering, TourRequest, load_graph, suggest

router = APIRouter(prefix="/api", tags=["tours"])


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
    params = q.model_dump(exclude={"start_stop"})
    if q.start_stop:
        row = conn.execute("SELECT e_lv95, n_lv95 FROM place WHERE id = ? AND kind = 'stop'", (q.start_stop,)).fetchone()
        if not row:
            raise HTTPException(404, "start stop not found")
        params["start_near"] = (row["e_lv95"], row["n_lv95"])
    tours = suggest(graph, TourRequest(**params))
    edge_ids = sorted({leg.edge_id for t in tours for leg in t.legs})
    geometry = dict(conn.execute(f"SELECT id, geometry FROM edge WHERE id IN ({','.join('?' * len(edge_ids))})",
                                 edge_ids).fetchall()) if edge_ids else {}

    def place(pid: int) -> dict:
        if pid in graph.huts:
            h = graph.huts[pid]
            return {"id": pid, "kind": "hut", "name": h.name, "lat": h.lat, "lon": h.lon}
        s = graph.stops[pid]
        return {"id": pid, "kind": "stop", "name": s.name, "lat": s.lat, "lon": s.lon, "mode": s.mode}

    result = []
    for t in tours:
        days = []
        for i, leg in enumerate(t.legs):
            night = t.nights[i] if i < len(t.nights) else None
            days.append({
                "day": i + 1,
                "date": (q.start_date + timedelta(days=i)).isoformat(),
                "from": place(leg.frm),
                "to": place(leg.to),
                "edge_id": leg.edge_id,
                **{k: getattr(leg, k) for k in ("time_min", "distance_m", "ascent_m", "descent_m", "max_category",
                                                "sac_time_min")},
                "geometry": geometry.get(leg.edge_id),
                "night": {"status": night.status, "partial": night.partial} if night else None,
            })
        result.append({
            "score": round(t.score, 4),
            "nights": len(t.nights),
            "time_min": sum(leg.time_min for leg in t.legs),
            "distance_m": sum(leg.distance_m for leg in t.legs),
            "ascent_m": sum(leg.ascent_m for leg in t.legs),
            "descent_m": sum(leg.descent_m for leg in t.legs),
            "max_category": max(leg.max_category for leg in t.legs),
            "uncertain_nights": sum(1 for n in t.nights if n.status == "unknown" or n.partial),
            "days": days,
        })
    return {"query": q.model_dump(mode="json"), "tours": result}


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
