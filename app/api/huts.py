"""Hut endpoints: a compact list for the map (with optional filters) and a detail view."""

import json
import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_db
from app.clients import hut_reservation, sac
from app.planner import availability, opening

router = APIRouter(prefix="/api/huts", tags=["huts"])


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


def website_info(conn: sqlite3.Connection, hut_id: int) -> dict | None:
    """What was read from the hut website (app/ingest/scrape_huts.py): booking, notes, seasons and prices."""
    web = conn.execute("SELECT * FROM hut_web WHERE hut_id = ?", (hut_id,)).fetchone()
    if not web:
        return None
    seasons = conn.execute("SELECT kind, start_md, end_md, year, approximate, note, source_url, quote, verified "
                           "FROM hut_season WHERE hut_id = ? ORDER BY kind = 'closed', year, start_md", (hut_id,))
    prices = conn.execute("SELECT item, label, includes_overnight, member, non_member, youth, child, currency, year, "
                          "source, source_url, quote, verified FROM hut_price WHERE hut_id = ? ORDER BY rowid",
                          (hut_id,))
    return {
        "url": web["url"],
        "status": web["status"],
        "booking_methods": json.loads(web["booking_methods"] or "[]"),
        "booking_url": web["booking_url"],
        "winter_room": None if web["winter_room"] is None else bool(web["winter_room"]),
        "notes": web["notes"],
        "checked_at": web["extracted_at"],
        "seasons": [dict(r) for r in seasons],
        "prices": [dict(r) for r in prices],
    }


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
        "sac_url": sac.page_url(row["sac_id"], row["source_name"]) if row["sac_id"] else None,
        "phone": row["phone"],
        "email": row["email"],
        "web": website_info(conn, hut_id),
        "booking_url": hut_reservation.BOOKING_URL.format(row["hrs_id"]) if row["hrs_id"] else None,
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
    return availability.calendar(conn, hut_id, row["hrs_id"], people)
