"""Austrian public transport (GTFS, Mobilitätsverbünde Österreich): the timetables that load_timetable merges into
each year's Swiss one, and foreign_transit reads the stops from.

    uv run python -m app.ingest.at_transit                # download new editions (needs an account), load stops abroad
    uv run python -m app.ingest.at_transit --no-download

The data is free (Datenlizenz Mobilitätsverbünde Österreich), but downloads need a registered account at
https://user.mobilitaetsverbuende.at: set MVO_USERNAME and MVO_PASSWORD. Without them the download is skipped; the
Swiss data loads as before, and Austrian files already on disk are still used. One dataset per region (the regional
ones leave out the trains of the railway dataset), this year's and next year's where published, kept as
data/gtfs/at/{dataset}_{year}.zip and downloaded again only when the listing shows a new version.

Stops: foreign_transit.py reads them from these and the other feeds abroad (keyed by stop.ref, e.g. at:47:1234).
"""

import argparse
import json
import os
from datetime import date
from pathlib import Path

import httpx

from app.clients import USER_AGENT
from app.db import connect
from app.ingest import foreign_transit, load_stops, regions
from app.ingest.foreign_transit import AT_DIR

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
    updated = False
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
            token = _token(client, user, password)  # one per file: tokens expire after ~5 minutes
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
    for f in foreign_transit.feed_files():
        dataset = int(f.source[2:]) if f.country == "AT" else None
        if dataset is not None and (f.year < year or dataset not in DATASETS):
            f.path.unlink()
            _edition_path(dataset, f.year).unlink(missing_ok=True)
            f.path.with_suffix(".stations.json").unlink(missing_ok=True)
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-download", action="store_true", help="use the datasets in data/gtfs/at/")
    args = parser.parse_args()
    if not args.no_download:
        regions.download_polygons()
        download()
    print(f"loaded {load_stops.load(connect(), foreign_transit.read_stops())} stops abroad")


if __name__ == "__main__":
    main()
