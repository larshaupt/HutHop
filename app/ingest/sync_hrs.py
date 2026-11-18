"""Sync huts outside Switzerland and Liechtenstein from hut-reservation.org (HRS) into the database.

    uv run python -m app.ingest.sync_hrs              # probe for new huts, refetch old responses, import
    uv run python -m app.ingest.sync_hrs --offline    # re-import the responses saved in data/raw/hrs/, no requests

HRS takes the bookings of the alpine clubs' huts in Austria (ÖAV), Germany (DAV), South Tyrol (AVS) and
Switzerland (SAC), and of some private huts. Its hut list needs a login, so hut ids are found by probing the public
hut info: ids 1, 2, … up to MISSES_TO_STOP ids in a row without a hut past the highest known one. Responses are kept
in data/raw/hrs/ (info/<id>.json, calendar/<id>.json; ids without a hut in missing.json) and fetched again only when
old: hut info after INFO_MAX_AGE_DAYS, calendars after CALENDAR_MAX_AGE_DAYS, gaps below the highest known id after
MISSING_MAX_AGE_DAYS (ids past it on every run: new huts get the next ids). Requests are DELAY_S apart.

Imported are the huts within a region's area (app/ingest/regions.py) that are not in Switzerland or Liechtenstein
(the SAC covers those) and whose hrs id no other hut has (the SAC lists some huts abroad with it). Upserts by hrs id
(source 'hrs_api', no sac id), so manual overrides (hut_override) survive re-syncs. Opening: see month_codes.
"""

import argparse
import html
import json
import re
import sqlite3
import time
from datetime import UTC, date, datetime

import httpx
import shapely
from pyproj import Transformer

from app.clients import hut_reservation
from app.db import ROOT, connect
from app.ingest import countries, regions

RAW_DIR = ROOT / "data/raw/hrs"
INFO_DIR = RAW_DIR / "info"
CALENDAR_DIR = RAW_DIR / "calendar"
MISSING_FILE = RAW_DIR / "missing.json"  # hrs id → date it was last probed without a hut
DELAY_S = 1.5
MISSES_TO_STOP = 50
INFO_MAX_AGE_DAYS = 30
CALENDAR_MAX_AGE_DAYS = 6
MISSING_MAX_AGE_DAYS = 90
WGS84_TO_LV95 = Transformer.from_crs("EPSG:4326", "EPSG:2056", always_xy=True)
UTM_TO_WGS84 = {zone: Transformer.from_crs(f"EPSG:326{zone}", "EPSG:4326", always_xy=True) for zone in (32, 33)}
TAG_RE = re.compile(r"<[^>]+>")
# HRS tenant code → the alpine club that owns the hut (operator 'club'); other tenants are private huts.
CLUBS = {"OEAV": "ÖAV", "DAV": "DAV", "AVS": "AVS", "SAC": "SAC", "SAT": "SAT", "CAI-AA": "CAI",
         "Naturfreunde_Oesterreich": "Naturfreunde"}
TEST_HUT = re.compile(r"\btest", re.I)  # HRS's own test entries ("Testhütte Carolin")
MIN_DAYS = 10  # a month is judged from the calendar only with at least this many days known
PLACEHOLDER_DAYS = 300  # a calendar SERVICED on every day for this long has no season in it (see month_codes)
# Months without calendar data and without codes from an earlier sync: catered "part of the month" from June to
# September (the usual season of the Eastern Alps' huts, uncertain), closed otherwise.
DEFAULT_CATERING = "222221111222"
DEFAULT_SELF_SERVICE = "2" * 12


def _age_days(path) -> float:
    return (time.time() - path.stat().st_mtime) / 86400


def probe(client: httpx.Client) -> int:
    """Fetch the hut info of new ids and refetch old responses (see the module docstring). Returns the number of
    requests. Stops early, keeping what it has, on a network or HTTP error."""
    INFO_DIR.mkdir(parents=True, exist_ok=True)
    missing: dict[str, str] = json.loads(MISSING_FILE.read_text()) if MISSING_FILE.exists() else {}
    known = {int(p.stem) for p in INFO_DIR.glob("*.json")}
    today = date.today()
    highest = max(known, default=0)
    requests, misses, hrs_id = 0, 0, 0
    try:
        while hrs_id < highest or misses < MISSES_TO_STOP:
            hrs_id += 1
            path = INFO_DIR / f"{hrs_id}.json"
            if hrs_id in known:
                due = _age_days(path) >= INFO_MAX_AGE_DAYS
            else:
                checked = missing.get(str(hrs_id))
                due = (hrs_id > highest or not checked
                       or (today - date.fromisoformat(checked)).days >= MISSING_MAX_AGE_DAYS)
            if due:
                if requests:
                    time.sleep(DELAY_S)
                try:
                    info = hut_reservation.fetch_hut_info(hrs_id, client)
                except httpx.HTTPError as err:
                    print(f"hrs: probing stopped at id {hrs_id}: {err}")
                    break
                requests += 1
                if info:
                    path.write_text(json.dumps(info, ensure_ascii=False))
                    known.add(hrs_id)
                    missing.pop(str(hrs_id), None)
                else:  # no hut (any more)
                    path.unlink(missing_ok=True)
                    known.discard(hrs_id)
                    missing[str(hrs_id)] = today.isoformat()
            misses = 0 if hrs_id in known else misses + 1
    finally:
        MISSING_FILE.write_text(json.dumps(missing, indent=0, sort_keys=True))
    return requests


def load_calendar(hrs_id: int, client: httpx.Client | None) -> list[dict] | None:
    """The hut's season calendar (parse_calendar rows), refetched when old unless `client` is None; None if never
    fetched."""
    path = CALENDAR_DIR / f"{hrs_id}.json"
    if client and (not path.exists() or _age_days(path) >= CALENDAR_MAX_AGE_DAYS):
        CALENDAR_DIR.mkdir(parents=True, exist_ok=True)
        time.sleep(DELAY_S)
        try:
            path.write_text(json.dumps(hut_reservation.fetch_calendar_days(hrs_id, client)))
        except (httpx.HTTPError, ValueError) as err:
            print(f"hrs: calendar of {hrs_id} not fetched: {err}")
    return hut_reservation.parse_calendar(json.loads(path.read_text())) if path.exists() else None


def month_codes(days: list[dict]) -> dict[int, tuple[int, int]]:
    """month → (catering, self_service) codes (0 whole month, 1 part, 2 no; see opening.py) for the months the season
    calendar tells: SERVICED days are catered, UNSERVICED ones self-service (winter room). A CLOSED day only counts
    up to the end of the month of the last day with released places (as in availability.Snapshot, there the season
    ends); after it, the next season may just not be released yet. Each month is judged by its occurrence with the
    most known days, if at least MIN_DAYS. A calendar that is SERVICED on every day of a year or more tells nothing:
    many huts leave it like that, high huts that close in winter too (Edmund-Graf-Hütte, 2408 m)."""
    if len(days) >= PLACEHOLDER_DAYS and all(d["hut_status"] == "SERVICED" for d in days):
        return {}
    last_released = max((d["date"] for d in days if d["total_places"]), default="")
    known: dict[tuple[str, int], list[str]] = {}
    for d in days:
        if d["hut_status"] in ("SERVICED", "UNSERVICED") or d["date"][:7] <= last_released[:7]:
            known.setdefault((d["date"][:4], int(d["date"][5:7])), []).append(d["hut_status"])
    best: dict[int, list[str]] = {}
    for (_, month), statuses in sorted(known.items()):
        if len(statuses) >= MIN_DAYS and len(statuses) > len(best.get(month, [])):
            best[month] = statuses

    def code(statuses: list[str], status: str) -> int:
        days_open = statuses.count(status)
        return 0 if days_open == len(statuses) else 1 if days_open else 2
    return {m: (code(s, "SERVICED"), code(s, "UNSERVICED")) for m, s in best.items()}


def opening_months(derived: dict[int, tuple[int, int]], previous: dict[int, tuple[int, int]]) -> list[dict]:
    """hut_month rows: from the calendar where it tells, else as at the last sync (last year's calendar), else the
    default season."""
    rows = []
    for m in range(1, 13):
        catering, self_service = derived.get(m) or previous.get(m) or (int(DEFAULT_CATERING[m - 1]),
                                                                        int(DEFAULT_SELF_SERVICE[m - 1]))
        rows.append({"month": m, "catering": catering, "self_service": self_service})
    return rows


def _number(text: str | None) -> int | None:
    """'2.211 m' → 2211 (dot or comma as thousands separator), '120' → 120."""
    digits = re.match(r"[\d.,' ]*", (text or "").strip()).group().replace(" ", "")
    return int(re.sub(r"\D", "", digits)) if re.search(r"\d", digits) else None


def _description(info: dict) -> str | None:
    """The general description in English, else German, as plain text."""
    texts = {d.get("language"): d.get("description") for d in info.get("hutGeneralDescriptions") or []}
    text = next((texts[lang] for lang in ("EN", "DE_AT", "DE_DE", "DE_CH") if texts.get(lang)), None)
    if not text:
        return None
    text = re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.I)
    text = html.unescape(TAG_RE.sub("", text))
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t\xa0]+", " ", text)).strip() or None


def _latlon(text: str | None) -> tuple[float, float] | None:
    """WGS84 (lat, lon) from HRS's free-text coordinates ("47.1, 12.7", "47.1/12.7", "N 46.09 / E 7.63", …): the first
    two numbers in a row that fit the Alps (in either order: there, latitude > longitude), else UTM. None for other
    systems (Swiss grid) and impossible values."""
    for decimals in (text or "", re.sub(r"(\d),(\d)", r"\1.\2", text or "")):  # then with decimal commas
        numbers = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", decimals)]
        for a, b in zip(numbers, numbers[1:]):
            lat, lon = max(a, b), min(a, b)
            if 43 <= lat <= 49.5 and 4 <= lon <= 18:
                return lat, lon
    return _utm(text or "")


def _utm(text: str) -> tuple[float, float] | None:
    """(lat, lon) from UTM metres without a zone ("UTM Y (Nord) 5.307.786 / UTM X (Ost) 446.557", "5280260 / 445766"),
    as many Austrian huts give them: zone 32 where that lands in its own band west of 12° E (Vorarlberg, Tyrol),
    else zone 33. Eastings of 530–620 km fit both (western Tyrol or Styria); zone 32 is assumed for them."""
    numbers = [int(re.sub(r"\D", "", x)) for x in re.findall(r"\d{1,3}(?:[.' ]\d{3})+|\d{6,7}", text)]
    north = next((x for x in numbers if 4_900_000 <= x <= 5_500_000), None)
    east = next((x for x in numbers if 160_000 <= x <= 840_000), None)
    if not north or not east:
        return None
    for zone, west, east_edge in ((32, 9.4, 12.0), (33, 12.0, 17.2)):
        lon, lat = UTM_TO_WGS84[zone].transform(east, north)
        if west <= lon <= east_edge and 45 <= lat <= 49.5:
            return round(lat, 6), round(lon, 6)
    return None


def parse_hut(info: dict) -> dict | None:
    """Map one HRS hut info to place and hut rows (None without usable coordinates, and for test entries). Opening
    comes separately."""
    if TEST_HUT.search(info["hutName"]) or not (latlon := _latlon(info.get("coordinates"))):
        return None
    lat, lon = latlon
    e, n = WGS84_TO_LV95.transform(lon, lat)
    altitude = _number(info.get("altitude"))
    categories = [c for c in info.get("hutBedCategories") or [] if c.get("isVisible", True)]
    shelter = sum(c.get("totalSleepingPlaces") or 0 for c in categories if c.get("reservationMode") == "UNSERVICED")
    beds = _number(info.get("totalBedsInfo")) or sum(c.get("totalSleepingPlaces") or 0 for c in categories
                                                     if c.get("reservationMode") != "UNSERVICED") or None
    picture = (info.get("picture") or {}).get("blobPath")
    club = CLUBS.get(info.get("tenantCode") or "")
    raw = {k: v for k, v in info.items() if k != "hutWarden"}  # no personal names (see the HRS findings)
    return {
        "place": {"kind": "hut", "name": info["hutName"].strip(), "e_lv95": e, "n_lv95": n,
                  "lat": round(lat, 6), "lon": round(lon, 6),
                  "altitude_m": altitude if altitude and 200 <= altitude <= 5000 else None},
        "hut": {
            "sac_id": None,
            "hrs_id": info["hutId"],
            "operator": "club" if club else "private",
            "owner": club,
            "beds": beds,
            "shelter_beds": shelter or None,
            "website": (info.get("hutWebsite") or "").strip() or None,
            "phone": (info.get("phone") or "").strip() or None,
            "email": None,
            "description": _description(info),
            "photos": json.dumps([{"caption": None, "copyright": "hut-reservation.org", "small": picture,
                                   "medium": picture, "large": picture}] if picture else []),
            "routes": "[]",
            "source": "hrs_api",
            "raw": json.dumps(raw, ensure_ascii=False),
        },
    }


def select(conn: sqlite3.Connection, infos: list[dict]) -> list[dict]:
    """The parsed huts to import: in a region's area, not in Switzerland or Liechtenstein, hrs id on no other hut.
    Sets place.country: from the borders (countries.py), else the region's code where the borders don't cover it."""
    areas = regions.areas()
    taken = {r[0] for r in conn.execute("SELECT hrs_id FROM hut_effective WHERE source != 'hrs_api' "
                                        "AND hrs_id IS NOT NULL")}
    parsed = [p for info in infos if info["hutId"] not in taken and (p := parse_hut(info))]
    if not parsed or not areas:
        return []
    codes = countries.country_at([p["place"]["e_lv95"] for p in parsed], [p["place"]["n_lv95"] for p in parsed])
    out = []
    for p, code in zip(parsed, codes):
        e, n = p["place"]["e_lv95"], p["place"]["n_lv95"]
        region = next((r for r, a in areas.items() if shapely.contains_xy(a, e, n)), None)
        if region and code not in ("CH", "LI"):
            p["place"]["country"] = code or (None if region in countries.borders() else region)
            out.append(p)
    return out


def upsert(conn: sqlite3.Connection, parsed: dict, days: list[dict] | None, synced_at: str) -> None:
    place, hut = parsed["place"], parsed["hut"]
    row = conn.execute("SELECT place_id FROM hut WHERE hrs_id = ? AND source = 'hrs_api'", (hut["hrs_id"],)).fetchone()
    previous = {}
    if row:
        previous = {r["month"]: (r["catering"], r["self_service"]) for r in conn.execute(
            "SELECT * FROM hut_month WHERE hut_id = ? AND catering IS NOT NULL AND self_service IS NOT NULL",
            (row["place_id"],))}
    months = opening_months(month_codes(days or []), previous)
    hut = hut | {"synced_at": synced_at, "services": json.dumps({
        "half_board": any(m["catering"] < 2 for m in months),  # the clubs' catered huts serve half board
        "cooking_non_catered": any(m["self_service"] < 2 for m in months),  # winter room: cook yourself
    })}
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
        [m | {"hut_id": place_id} for m in months],
    )


def load_infos() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(INFO_DIR.glob("*.json"), key=lambda p: int(p.stem))]


def sync(conn: sqlite3.Connection, offline: bool = False) -> int:
    """Probe and refetch (unless `offline`), then import the huts from the saved responses. Returns the number of
    huts imported or updated."""
    client = None if offline else hut_reservation.make_client()
    if client:
        probe(client)
    huts = select(conn, load_infos())
    calendars = {p["hut"]["hrs_id"]: load_calendar(p["hut"]["hrs_id"], client) for p in huts}
    synced_at = datetime.now(UTC).isoformat(timespec="seconds")
    with conn:
        for parsed in huts:
            upsert(conn, parsed, calendars[parsed["hut"]["hrs_id"]], synced_at)
    return len(huts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="only use the responses saved in data/raw/hrs/")
    args = parser.parse_args()
    regions.download_polygons()
    count = sync(connect(), args.offline)
    print(f"synced {count} huts from hut-reservation.org")


if __name__ == "__main__":
    main()
