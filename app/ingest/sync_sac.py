"""Sync SAC huts into the database.

    uv run python -m app.ingest.sync_sac                       # fetch from the SAC API
    uv run python -m app.ingest.sync_sac --from-file data/raw/sac/huts.json

Upserts by SAC id, so manual overrides (hut_override) survive re-syncs.
"""

import argparse
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pyproj import Transformer

from app.clients import sac
from app.db import ROOT, connect

RAW_DIR = ROOT / "data/raw/sac"
LV95_TO_WGS84 = Transformer.from_crs("EPSG:2056", "EPSG:4326", always_xy=True)
TAG_RE = re.compile(r"<[^>]+>")
PHOTO_SIZES ={"small": "320x200", "medium": "500x313", "large": "1200x750"}
ROUTE_FIELDS = ("id", "title", "discipline", "main_difficulty", "normal_route", "ascent_altitude",
                "ascent_time_min", "ascent_time_max", "descent_altitude", "descent_time_min", "descent_time_max")


def parse_hut(raw: dict) -> dict:
    """Map one SAC portal record to place, hut and hut_month rows."""
    e, n = raw["geom"]["coordinates"]
    lon, lat = LV95_TO_WGS84.transform(e, n)
    photos = [
        {
            "caption": p.get("caption"),
            "copyright": (p["photo"].get("copyright") or "SAC-CAS"),
            **{key: p["photo"]["thumbnails"].get(size) for key, size in PHOTO_SIZES.items()},
        }
        for p in raw.get("photos") or []
        if p.get("photo", {}).get("thumbnails")
    ]
    # Paid route portal content ("limited") must not be used, see brief §6.
    routes = [{k: r.get(k) for k in ROUTE_FIELDS} for r in raw.get("routes") or [] if r.get("availability") == "free"]
    months = [
        {"month": m, "self_service": (raw.get("opening") or {}).get(f"month_{m:02d}"),
         "catering": (raw.get("catering") or {}).get(f"month_{m:02d}")}
        for m in range(1, 13)
    ]
    return {
        "place": {"kind": "hut", "name": raw["display_name"].strip(), "e_lv95": e, "n_lv95": n,
                  "lat": round(lat, 6), "lon": round(lon, 6),
                  "altitude_m": round(raw["altitude"]) if raw.get("altitude") else None},
        "hut": {
            "sac_id": raw["id"],
            "hrs_id": raw.get("hrs_id") or None,
            "operator": "private" if raw.get("is_private") else "sac",
            "owner": raw.get("owner"),
            "beds": raw.get("sleeps"),
            "shelter_beds": raw.get("emergency_shelter"),
            "website": raw.get("url") or None,
            "description": TAG_RE.sub("", raw.get("description") or "").strip() or None,
            "services": json.dumps({k: bool(v) for k, v in (raw.get("services") or {}).items()}),
            "photos": json.dumps(photos, ensure_ascii=False),
            "routes": json.dumps(routes, ensure_ascii=False),
            "source": "sac_api",
            "raw": json.dumps(raw, ensure_ascii=False),
        },
        "months": months,
    }


def upsert(conn: sqlite3.Connection, parsed: dict, synced_at: str) -> None:
    place, hut = parsed["place"], parsed["hut"] | {"synced_at": synced_at}
    row = conn.execute("SELECT place_id FROM hut WHERE sac_id = ?", (hut["sac_id"],)).fetchone()
    if row:
        place_id = row["place_id"]
        conn.execute(f"UPDATE place SET {', '.join(f'{k} = :{k}' for k in place)} WHERE id = :id",
                     place | {"id": place_id})
        conn.execute(f"UPDATE hut SET {', '.join(f'{k} = :{k}' for k in hut)} WHERE place_id = :place_id",
                     hut | {"place_id": place_id})
    else:
        place_id = conn.execute(f"INSERT INTO place ({', '.join(place)}) VALUES ({', '.join(f':{k}' for k in place)})",
                                place).lastrowid
        conn.execute(f"INSERT INTO hut (place_id, {', '.join(hut)}) VALUES (:place_id, {', '.join(f':{k}' for k in hut)})",
                     hut | {"place_id": place_id})
    conn.executemany(
        "INSERT OR REPLACE INTO hut_month (hut_id, month, self_service, catering) "
        "VALUES (:hut_id, :month, :self_service, :catering)",
        [m | {"hut_id": place_id} for m in parsed["months"]],
    )


def sync(conn: sqlite3.Connection, raw_huts: list[dict]) -> int:
    synced_at = datetime.now(UTC).isoformat(timespec="seconds")
    with conn:
        for raw in raw_huts:
            upsert(conn, parse_hut(raw), synced_at)
    return len(raw_huts)


def fetch_and_save() -> list[dict]:
    """Fetch all huts from the SAC API and keep the raw response in data/raw/sac/huts.json."""
    raw_huts = sac.fetch_huts()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / "huts.json").write_text(json.dumps(raw_huts, ensure_ascii=False))
    return raw_huts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from-file", type=Path, help="use a saved API response instead of fetching")
    args = parser.parse_args()
    raw_huts = json.loads(args.from_file.read_text()) if args.from_file else fetch_and_save()
    count = sync(connect(), raw_huts)
    print(f"synced {count} huts")


if __name__ == "__main__":
    main()
