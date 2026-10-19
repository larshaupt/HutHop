"""Hut endpoints: a compact list for the map (with optional filters) and a detail view."""

import json
import sqlite3
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_db
from app.planner import availability, opening

router = APIRouter(prefix="/api/huts", tags=["huts"])
HRS_BOOKING_URL = "https://www.hut-reservation.org/reservation/book-hut/{}/wizard"


def summary(row: sqlite3.Row, codes: tuple[str, str]) -> dict:
    """Compact hut record for the map and list. `months` holds the opening status per month (see opening.py)."""
    services = json.loads(row["services"] or "{}")
    photos = json.loads(row["photos"] or "[]")
    return {
        "id": row["id"],
        "name": row["name"],
        "lat": row["lat"],
        "lon": row["lon"],
        "altitude": row["altitude_m"],
        "beds": row["beds"],
        "shelter_beds": row["shelter_beds"],
        "operator": row["operator"],
        "bookable": bool(row["hrs_id"]),
        "months": [opening.month_status(*codes, m) for m in range(1, 13)],
        "partial_months": [opening.is_partial(*codes, m) for m in range(1, 13)],
        "half_board": services.get("half_board", False),
        "self_catering": services.get("cooking_non_catered", False),
        "photo": photos[0]["small"] if photos else None,
    }


@router.get("")
def list_huts(
    month: int | None = Query(None, ge=1, le=12, description="only huts open in this month"),
    open_as: Literal["any", "catered", "self_service"] = Query("any", description="how the hut must be open"),
    half_board: bool = False,
    self_catering: bool = False,
    min_beds: int | None = None,
    alt_min: int | None = None,
    alt_max: int | None = None,
    operator: Literal["sac", "private"] | None = None,
    q: str | None = Query(None, description="name contains"),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    codes = opening.load_codes(conn)
    huts = [summary(r, codes.get(r["id"], (opening.NO_DATA, opening.NO_DATA)))
            for r in conn.execute("SELECT * FROM hut_effective ORDER BY name")]

    def keep(h: dict) -> bool:
        if month:
            allowed = {"any": ("catered", "self_service"), "catered": ("catered",),
                       "self_service": ("self_service",)}[open_as]
            if h["months"][month - 1] not in allowed:
                return False
        return not (
            (half_board and not h["half_board"])
            or (self_catering and not h["self_catering"])
            or (min_beds and (h["beds"] or 0) < min_beds)
            or (alt_min and (h["altitude"] or 0) < alt_min)
            or (alt_max and (h["altitude"] or 0) > alt_max)
            or (operator and h["operator"] != operator)
            or (q and q.casefold() not in h["name"].casefold())
        )

    return [h for h in huts if keep(h)]


@router.get("/{hut_id}")
def hut_detail(hut_id: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    row = conn.execute("SELECT * FROM hut_effective WHERE id = ?", (hut_id,)).fetchone()
    if not row:
        raise HTTPException(404, "hut not found")
    codes = opening.load_codes(conn, hut_id).get(hut_id, (opening.NO_DATA, opening.NO_DATA))
    routes = [r for r in json.loads(row["routes"] or "[]") if r["discipline"] == "mountain_hiking"]
    routes.sort(key=lambda r: (not r["normal_route"], r["ascent_time_max"] or 9999))
    return summary(row, codes) | {
        "owner": row["owner"],
        "description": row["description"],
        "website": row["website"],
        "booking_url": HRS_BOOKING_URL.format(row["hrs_id"]) if row["hrs_id"] else None,
        "services": json.loads(row["services"] or "{}"),
        "photos": json.loads(row["photos"] or "[]"),
        "routes": routes,
        "lv95": [row["e_lv95"], row["n_lv95"]],
        "snap_m": row["snap_m"],
        "source": row["source"],
        "synced_at": row["synced_at"],
    }


@router.get("/{hut_id}/availability")
def hut_availability(hut_id: int, people: int = Query(2, ge=1, le=30), conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """The hut's booking calendar (hut-reservation.org, cached like tour checks), one entry per day."""
    row = conn.execute("SELECT hrs_id FROM hut_effective WHERE id = ?", (hut_id,)).fetchone()
    if not row:
        raise HTTPException(404, "hut not found")
    if not row["hrs_id"]:
        return {"bookable": False, "days": []}
    availability.refresh(conn, {hut_id}, max_fetches=1)
    fetch = conn.execute("SELECT fetched_at, ok FROM availability_fetch WHERE hut_id = ?", (hut_id,)).fetchone()
    result = {"bookable": True, "booking_url": HRS_BOOKING_URL.format(row["hrs_id"]), "people": people,
              "checked_at": fetch["fetched_at"] if fetch else None, "error": not (fetch and fetch["ok"]), "days": []}
    dates = [date.fromisoformat(r[0]) for r in conn.execute(
        "SELECT date FROM availability WHERE hut_id = ? AND date >= ? ORDER BY date", (hut_id, date.today().isoformat()))]
    if result["error"] or not dates:
        return result
    snapshot = availability.Snapshot(conn, dates[0], dates[-1], people)
    for day in dates:
        a = snapshot.night(hut_id, True, day)
        result["days"].append({"date": day.isoformat(), "state": a.state, "free_beds": a.free_beds,
                               "total_places": a.total_places, "hut_status": a.hut_status})
    return result
