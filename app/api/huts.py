"""Hut endpoints: a compact list for the map and a detail view."""

import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_db
from app.clients import hut_reservation, sac, weather
from app.ingest import osm_access
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
def list_huts(conn: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    """Every hut, by name; the app filters them itself."""
    codes = opening.load_codes(conn)
    return [summary(r, codes.get(r["id"], (opening.NO_DATA, opening.NO_DATA)))
            for r in conn.execute("SELECT * FROM hut_effective ORDER BY name")]


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
    grades = osm_access.grades()
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
        "osm_access": hut_id in grades,  # an unofficial path to the signposted trails (trail limit 4)
        "osm_grade": grades.get(hut_id),
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


@router.get("/{hut_id}/weather")
def hut_weather(hut_id: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """MeteoSwiss forecast for the next days at the hut's position and altitude (clients/weather.py)."""
    row = conn.execute("SELECT lat, lon, altitude_m FROM hut_effective WHERE id = ?", (hut_id,)).fetchone()
    if not row:
        raise HTTPException(404, "hut not found")
    try:
        return {"days": weather.forecast(row["lat"], row["lon"], row["altitude_m"]), "error": False}
    except Exception:  # network and HTTP errors, unexpected JSON: the app just shows no forecast
        return {"days": [], "error": True}
