"""Austrian public transport (GTFS, Mobilitätsverbünde Österreich): the stops in the Austrian region, and the
timetables that load_timetable merges into each year's Swiss one.

    uv run python -m app.ingest.at_transit                # download new editions (needs an account), load stops
    uv run python -m app.ingest.at_transit --no-download

The data is free (Datenlizenz Mobilitätsverbünde Österreich), but downloads need a registered account at
https://user.mobilitaetsverbuende.at: set MVO_USERNAME and MVO_PASSWORD. Without them the download is skipped; the
Swiss data loads as before, and Austrian files already on disk are still used. One dataset per region (the regional
ones leave out the trains of the railway dataset), this year's and next year's where published, kept as
data/gtfs/at/{dataset}_{year}.zip and downloaded again only when the listing shows a new version.

Stops: the stations (parent stations where the feed has them) inside the region's area and outside Switzerland and
Liechtenstein (stops there come from the Swiss sources), keyed by stop.ref (e.g. at:47:1234), with the modes of
the routes calling there.
"""

import argparse
import csv
import io
import json
import os
import zipfile
from datetime import date
from pathlib import Path

import httpx
import numpy as np
import shapely
from pyproj import Transformer

from app.clients import USER_AGENT
from app.db import connect
from app.ingest import countries, load_stops, regions
from app.ingest.load_timetable import AT_DIR, GLOBAL_ID, _rows, at_feed_files, at_stop_keys

TOKEN_URL = "https://user.mobilitaetsverbuende.at/auth/realms/dbp-public/protocol/openid-connect/token"
# Public, no login. Without tagFilterModeInclusive the API answers 400 ("boolean string is expected").
LISTING = "https://data.mobilitaetsverbuende.at/api/public/v1/data-sets?tagFilterModeInclusive=false"
FILE_URL = "https://data.mobilitaetsverbuende.at/api/public/v1/data-sets/{dataset}/{year}/file"
# Data set id → what it covers, from the public listing (Nov 2026). The Flex datasets hold a region's whole timetable
# plus its on-demand services (GTFS-Flex; each is slightly larger than the plain GTFS one, same description), so
# they replace the plain ones (58 Vorarlberg, 57 Tyrol, 54 Salzburg, 55 Carinthia, 53 Styria, 56 Upper Austria,
# 52 Eastern region). Not used: Linz AG (69, 74: city traffic), 72 (Styria in nine parts, as in 79), NeTEx/DINO.
DATASETS = {66: "railway (ÖBB, Montafonerbahn, WESTbahn, …)", 75: "Vorarlberg", 76: "Tyrol", 73: "Salzburg",
            78: "Carinthia", 79: "Styria", 77: "Upper Austria",
            80: "Eastern region (Lower Austria, Vienna, Burgenland)"}
WGS84_TO_LV95 = Transformer.from_crs("EPSG:4326", "EPSG:2056", always_xy=True)
# GTFS route type → stop mode (the words of load_stops.MODES): basic types, extended ones by exact type, then by
# hundreds (None: not a mode HutHop knows, e.g. air)
ROUTE_MODES = {0: "tram", 1: "metro", 2: "train", 3: "bus", 4: "boat", 5: "tram", 6: "cableway", 7: "funicular",
               11: "bus", 12: "train", 116: "rack_railway", 1303: "elevator", 1304: "chairlift", 1305: "chairlift"}
ROUTE_MODES_BY_HUNDRED = {1: "train", 2: "bus", 4: "metro", 5: "metro", 6: "metro", 7: "bus", 8: "bus", 9: "tram",
                          10: "boat", 12: "boat", 13: "cableway", 14: "funicular", 15: "bus"}
MODE_ORDER = list(load_stops.MODES.values())


def path(dataset: int, year: int) -> Path:
    return AT_DIR / f"{dataset}_{year}.zip"


def _edition_path(dataset: int, year: int) -> Path:
    return AT_DIR / f"{dataset}_{year}.json"


def _token(client: httpx.Client, user: str, password: str) -> str:
    res = client.post(TOKEN_URL, data={"grant_type": "password", "client_id": "dbp-public-ui", "scope": "openid",
                                       "username": user, "password": password})
    if res.status_code in (400, 401):
        raise RuntimeError(f"austria: login refused ({res.json().get('error_description', res.status_code)}), "
                           "check MVO_USERNAME and MVO_PASSWORD")
    return res.raise_for_status().json()["access_token"]


def download(force: bool = False, today: date | None = None) -> bool:
    """Download this year's and next year's dataset (if published yet) of each region where a new version is out,
    and delete older years and datasets no longer used. Skipped without credentials. True if anything was
    downloaded."""
    user, password = os.environ.get("MVO_USERNAME"), os.environ.get("MVO_PASSWORD")
    if not user or not password:
        print("austria: MVO_USERNAME and MVO_PASSWORD are not set, Austrian timetables not downloaded "
              "(free account at https://user.mobilitaetsverbuende.at)")
        return False
    year = (today or date.today()).year
    AT_DIR.mkdir(parents=True, exist_ok=True)
    client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=300, follow_redirects=True)
    listing = {int(d["id"]): d for d in client.get(LISTING).raise_for_status().json()}
    token, updated = None, False
    for dataset, name in DATASETS.items():
        for v in listing.get(dataset, {}).get("activeVersions", []):
            y, version = int(v["year"]), v["dataSetVersion"]["id"]
            if y not in (year, year + 1):
                continue
            edition = _edition_path(dataset, y)
            known = json.loads(edition.read_text()) if edition.exists() else None
            if known == {"version": version} and path(dataset, y).exists() and not force:
                print(f"austria {dataset} {name} {y}: up to date ({v['dataSetVersion']['file']['originalName']})")
                continue
            token = token or _token(client, user, password)
            print(f"austria {dataset} {name} {y}: downloading {v['dataSetVersion']['file']['originalName']}")
            partial = path(dataset, y).with_suffix(".part")
            with client.stream("GET", FILE_URL.format(dataset=dataset, year=y),
                               headers={"Authorization": f"Bearer {token}"}) as res, partial.open("wb") as f:
                if res.status_code == 451:  # the account has not accepted the data licence yet
                    raise RuntimeError("the account may not download Austrian timetables yet: sign in at "
                                       "https://data.mobilitaetsverbuende.at and accept the data licence "
                                       "(Datenlizenz Mobilitätsverbünde Österreich)")
                res.raise_for_status()
                for chunk in res.iter_bytes(1 << 20):
                    f.write(chunk)
            partial.replace(path(dataset, y))
            edition.write_text(json.dumps({"version": version}, indent=1))
            updated = True
    for y, files in at_feed_files().items():
        for dataset, p in files:
            if y < year or dataset not in DATASETS:
                p.unlink()
                _edition_path(dataset, y).unlink(missing_ok=True)
    return updated


def route_mode(route_type: str) -> str | None:
    t = int(route_type or 3)
    return ROUTE_MODES.get(t) or ROUTE_MODES_BY_HUNDRED.get(t // 100)


def _read_file(p: Path, source: str, stations: dict[str, dict], modes: dict[str, set[str]]) -> None:
    """Add a feed's stations (by stop key: position and name, a parent station's before a quay's) and the modes of
    the routes calling at them."""
    with zipfile.ZipFile(p) as z:
        keys = at_stop_keys(z, source)
        for r in _rows(z, "stops.txt"):
            if not r.get("stop_lat") or not r.get("stop_lon"):
                continue
            key, is_station = keys[r["stop_id"]], r.get("location_type") == "1"
            if key not in stations or (is_station and not stations[key]["station"]):
                stations[key] = {"name": r["stop_name"], "lat": float(r["stop_lat"]), "lon": float(r["stop_lon"]),
                                 "station": is_station}
        route = {r["route_id"]: route_mode(r.get("route_type", "")) for r in _rows(z, "routes.txt")}
        trip = {r["trip_id"]: route.get(r["route_id"]) for r in _rows(z, "trips.txt")}
        reader = csv.reader(io.TextIOWrapper(z.open("stop_times.txt"), encoding="utf-8-sig"))
        header = next(reader)
        i_trip, i_stop = header.index("trip_id"), header.index("stop_id")
        for row in reader:
            mode, key = trip.get(row[i_trip]), keys.get(row[i_stop])
            if mode and key:
                modes.setdefault(key, set()).add(mode)


def read_stops(paths: dict[int, Path] | None = None) -> list[dict]:
    """The stations of the Austrian datasets (default: the newest year of each on disk) with their modes, inside the
    region's area and outside Switzerland and Liechtenstein, as load_stops.load takes them (with `ref`)."""
    area, borders = regions.area("AT"), countries.borders()
    if area is None or not borders:
        print("austria: region polygon (regions) or borders (countries) missing, no Austrian stops loaded")
        return []
    if paths is None:
        paths = {dataset: p for _, files in sorted(at_feed_files().items()) for dataset, p in files}
    stations: dict[str, dict] = {}
    modes: dict[str, set[str]] = {}
    for dataset, p in paths.items():
        _read_file(p, f"at{dataset}", stations, modes)
    refs = [k for k in stations if modes.get(k)]
    if not refs:
        return []
    lat = np.array([stations[k]["lat"] for k in refs])
    lon = np.array([stations[k]["lon"] for k in refs])
    e, n = WGS84_TO_LV95.transform(lon, lat)
    inside = shapely.contains_xy(area, e, n)
    country = countries.country_at(e, n)
    stops = []
    for i, ref in enumerate(refs):
        if inside[i] and country[i] not in countries.COUNTRIES:  # None, or AT once its border is known
            stops.append({
                "ref": ref, "name": stations[ref]["name"],
                "mode": ",".join(sorted(modes[ref], key=MODE_ORDER.index)),
                "e_lv95": float(e[i]), "n_lv95": float(n[i]), "lat": round(lat[i], 6), "lon": round(lon[i], 6),
                "country": country[i] or (ref[:2].upper() if GLOBAL_ID.fullmatch(ref) else None),
            })
    print(f"austria: {len(stops):,} stops in the region of {len(refs):,} served in {len(paths)} datasets")
    return stops


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-download", action="store_true", help="use the datasets in data/gtfs/at/")
    args = parser.parse_args()
    if not args.no_download:
        regions.download_polygons()
        download()
    print(f"loaded {load_stops.load(connect(), read_stops())} Austrian stops")


if __name__ == "__main__":
    main()
