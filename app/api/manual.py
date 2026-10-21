"""Manual corrections (overrides that survive SAC re-syncs) and huts added by hand.

All changes need the admin password once one is set (see app/admin.py).
"""

import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from pyproj import Transformer

from app.admin import require_admin
from app.api.deps import get_db
from app.planner import opening

router = APIRouter(prefix="/api/huts", tags=["manual"])
WGS84_TO_LV95 = Transformer.from_crs("EPSG:4326", "EPSG:2056", always_xy=True)

# Overridable scalar fields: name → (source column in hut/place, parser that validates and normalises a value)
FIELDS: dict[str, tuple[str, Any]] = {
    "name": ("p.name", lambda v: _text(v, 120)),
    "altitude": ("p.altitude_m", lambda v: _int(v, 0, 5000)),
    "beds": ("h.beds", lambda v: _int(v, 0, 1000)),
    "shelter_beds": ("h.shelter_beds", lambda v: _int(v, 0, 200)),
    "website": ("h.website", lambda v: _url(v)),
    "phone": ("h.phone", lambda v: _phone(v)),
    "email": ("h.email", lambda v: _email(v)),
    "hrs_id": ("h.hrs_id", lambda v: _int(v, 1, 100_000)),
}
MONTH_FIELDS = ("catering", "self_service")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _text(v: Any, max_len: int) -> str:
    if not isinstance(v, str) or not v.strip() or len(v) > max_len:
        raise ValueError(f"expected text up to {max_len} characters")
    return v.strip()


def _int(v: Any, lo: int, hi: int) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi or int(v) != v:
        raise ValueError(f"expected a whole number from {lo} to {hi}")
    return int(v)


def _url(v: Any) -> str | None:
    if v is None or v == "":
        return None
    if not isinstance(v, str) or not v.startswith(("http://", "https://")) or len(v) > 300:
        raise ValueError("expected an http(s) URL")
    return v


def _phone(v: Any) -> str | None:
    if v is None or v == "":
        return None
    if not isinstance(v, str) or not re.fullmatch(r"\+?[\d ()/.-]{6,30}", v.strip()):
        raise ValueError("expected a phone number")
    return v.strip()


def _email(v: Any) -> str | None:
    if v is None or v == "":
        return None
    if not isinstance(v, str) or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v.strip()) or len(v) > 120:
        raise ValueError("expected an email address")
    return v.strip()


def parse_field(field: str, value: Any) -> Any:
    """Validate an override; month fields are 'catering.<1-12>' / 'self_service.<1-12>' with codes 0, 1 or 2."""
    if field in FIELDS:
        return FIELDS[field][1](value)
    kind, _, month = field.partition(".")
    if kind in MONTH_FIELDS and month.isdigit() and 1 <= int(month) <= 12:
        if value not in (0, 1, 2):
            raise ValueError("expected 0 (whole month), 1 (part of the month) or 2 (no)")
        return value
    raise ValueError(f"unknown field {field!r}")


def require_hut(conn: sqlite3.Connection, hut_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT h.*, p.name, p.altitude_m FROM hut h JOIN place p ON p.id = h.place_id "
                       "WHERE h.place_id = ?", (hut_id,)).fetchone()
    if not row:
        raise HTTPException(404, "hut not found")
    return row


@router.get("/{hut_id}/overrides")
def get_overrides(hut_id: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """Effective value, source value and override note for every editable field."""
    require_hut(conn, hut_id)
    overrides = {r["field"]: r for r in conn.execute("SELECT * FROM hut_override WHERE hut_id = ?", (hut_id,))}
    source = conn.execute(f"SELECT {', '.join(f'{col} AS {name}' for name, (col, _) in FIELDS.items())} "
                          "FROM hut h JOIN place p ON p.id = h.place_id WHERE h.place_id = ?", (hut_id,)).fetchone()

    def entry(field: str, source_value: Any) -> dict:
        o = overrides.get(field)
        return {"value": json.loads(o["value"]) if o else source_value, "source": source_value,
                "overridden": o is not None, "note": o["note"] if o else None}

    months = {r["month"]: r for r in conn.execute("SELECT * FROM hut_month WHERE hut_id = ? ORDER BY month", (hut_id,))}
    return {
        "fields": {name: entry(name, source[name]) for name in FIELDS},
        "months": {kind: [entry(f"{kind}.{m}", months[m][kind] if m in months else 2) for m in range(1, 13)]
                   for kind in MONTH_FIELDS},
        "source": conn.execute("SELECT source FROM hut WHERE place_id = ?", (hut_id,)).fetchone()["source"],
    }


class OverrideBody(BaseModel):
    value: Any
    note: str | None = Field(None, max_length=500)


@router.put("/{hut_id}/overrides/{field}", dependencies=[Depends(require_admin)])
def set_override(hut_id: int, field: str, body: OverrideBody, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    require_hut(conn, hut_id)
    try:
        value = parse_field(field, body.value)
    except ValueError as err:
        raise HTTPException(422, str(err)) from None
    with conn:
        conn.execute("INSERT OR REPLACE INTO hut_override (hut_id, field, value, note, updated_at) VALUES (?, ?, ?, ?, ?)",
                     (hut_id, field, json.dumps(value), body.note, _now()))
    return {"field": field, "value": value, "note": body.note}


@router.delete("/{hut_id}/overrides/{field}", dependencies=[Depends(require_admin)])
def delete_override(hut_id: int, field: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    require_hut(conn, hut_id)
    with conn:
        deleted = conn.execute("DELETE FROM hut_override WHERE hut_id = ? AND field = ?", (hut_id, field)).rowcount
    if not deleted:
        raise HTTPException(404, "no override for this field")
    return {"field": field, "reset": True}


class ManualHut(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    lat: float = Field(ge=45.7, le=47.9)
    lon: float = Field(ge=5.8, le=10.6)
    altitude: int | None = Field(None, ge=0, le=5000)
    beds: int | None = Field(None, ge=0, le=1000)
    shelter_beds: int | None = Field(None, ge=0, le=200)
    website: str | None = None
    hrs_id: int | None = Field(None, ge=1)
    description: str | None = Field(None, max_length=4000)
    catering: str = Field(opening.NO_DATA, description="12 codes, one per month: 0 yes, 1 partly, 2 no")
    self_service: str = Field(opening.NO_DATA)

    @field_validator("catering", "self_service")
    @classmethod
    def twelve_codes(cls, v: str) -> str:
        if len(v) != 12 or set(v) - set("012"):
            raise ValueError("expected 12 digits 0/1/2, one per month")
        return v

    @field_validator("website")
    @classmethod
    def website_url(cls, v: str | None) -> str | None:
        return _url(v)


@router.post("", status_code=201, dependencies=[Depends(require_admin)])
def add_hut(hut: ManualHut, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """Add a hut that isn't in the SAC portal. Run `refresh --rebuild` (or build_edges) to connect it to trails."""
    e, n = WGS84_TO_LV95.transform(hut.lon, hut.lat)
    with conn:
        place_id = conn.execute(
            "INSERT INTO place (kind, name, e_lv95, n_lv95, lat, lon, altitude_m) VALUES ('hut', ?, ?, ?, ?, ?, ?)",
            (hut.name.strip(), e, n, round(hut.lat, 6), round(hut.lon, 6), hut.altitude)).lastrowid
        conn.execute(
            "INSERT INTO hut (place_id, hrs_id, operator, beds, shelter_beds, website, description, services, photos, "
            "routes, source, synced_at) VALUES (?, ?, 'private', ?, ?, ?, ?, '{}', '[]', '[]', 'manual', ?)",
            (place_id, hut.hrs_id, hut.beds, hut.shelter_beds, hut.website, hut.description, _now()))
        conn.executemany("INSERT INTO hut_month (hut_id, month, catering, self_service) VALUES (?, ?, ?, ?)",
                         [(place_id, m, int(hut.catering[m - 1]), int(hut.self_service[m - 1])) for m in range(1, 13)])
    return {"id": place_id, "note": "Not connected to trails yet: run `uv run python -m app.ingest.refresh --rebuild`."}


@router.delete("/{hut_id}", dependencies=[Depends(require_admin)])
def delete_manual_hut(hut_id: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """Delete a hut that was added by hand (SAC huts can't be deleted, only corrected)."""
    if require_hut(conn, hut_id)["source"] != "manual":
        raise HTTPException(409, "only huts added by hand can be deleted")
    with conn:
        for table, col in (("edge", "from_place"), ("edge", "to_place"), ("hut_override", "hut_id"),
                           ("hut_month", "hut_id"), ("availability", "hut_id"), ("availability_fetch", "hut_id"),
                           ("hut", "place_id"), ("place", "id")):
            conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (hut_id,))
    return {"id": hut_id, "deleted": True}
