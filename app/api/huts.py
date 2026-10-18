"""Hut endpoints: a compact list for the map (with optional filters) and a detail view."""

import json
import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db import connect

router = APIRouter(prefix="/api/huts", tags=["huts"])
HRS_BOOKING_URL = "https://www.hut-reservation.org/reservation/book-hut/{}/wizard"


def get_db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def month_codes(conn: sqlite3.Connection) -> dict[int, tuple[str, str]]:
    """hut id → (catering, self_service) as 12-character code strings, e.g. '222221000122'."""
    rows = conn.execute("SELECT hut_id, month, catering, self_service FROM hut_month_effective ORDER BY hut_id, month")
    codes: dict[int, tuple[list[str], list[str]]] = {}
    for r in rows:
        cat, ss = codes.setdefault(r["hut_id"], ([], []))
        cat.append(str(r["catering"] if r["catering"] is not None else 2))
        ss.append(str(r["self_service"] if r["self_service"] is not None else 2))
    return {k: ("".join(c), "".join(s)) for k, (c, s) in codes.items()}


def month_status(catering: str, self_service: str, month: int) -> str:
    """'catered' | 'self_service' | 'closed' | 'unknown' for one month (1–12).

    SAC codes: 0 = whole month, 1 = part of the month, 2 = no. All 2 = no data."""
    if set(catering + self_service) == {"2"}:
        return "unknown"
    if catering[month - 1] in "01":
        return "catered"
    if self_service[month - 1] in "01":
        return "self_service"
    return "closed"


def summary(row: sqlite3.Row, codes: tuple[str, str]) -> dict:
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
        "catering": codes[0],
        "self_service": codes[1],
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
    codes = month_codes(conn)
    huts = [summary(r, codes.get(r["id"], ("2" * 12, "2" * 12)))
            for r in conn.execute("SELECT * FROM hut_effective ORDER BY name")]

    def keep(h: dict) -> bool:
        if month:
            status = month_status(h["catering"], h["self_service"], month)
            allowed = {"any": ("catered", "self_service"), "catered": ("catered",),
                       "self_service": ("self_service",)}[open_as]
            if status not in allowed:
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
    codes = month_codes(conn).get(hut_id, ("2" * 12, "2" * 12))
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
        "months": [month_status(*codes, m) for m in range(1, 13)],
        "partial_months": [codes[0][m] == "1" or (codes[0][m] == "2" and codes[1][m] == "1") for m in range(12)],
        "lv95": [row["e_lv95"], row["n_lv95"]],
        "snap_m": row["snap_m"],
        "source": row["source"],
        "synced_at": row["synced_at"],
    }
