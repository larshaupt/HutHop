"""Load official public transport stops (BAV, GeoAdmin `ch.bav.haltestellen-oev`) into the database.

    uv run python -m app.ingest.load_stops             # download the current edition (~5 MB)
    uv run python -m app.ingest.load_stops --no-download

Keeps passenger stops only and upserts by the official stop number, so edges stay valid across reloads.
"""

import argparse
import io
import sqlite3
import zipfile

import httpx
import pyogrio.raw
from pyproj import Transformer
from shapely import from_wkb

from app.clients import USER_AGENT
from app.db import ROOT, connect

STAC_ITEMS = "https://data.geo.admin.ch/api/stac/v1/collections/ch.bav.haltestellen-oev/items"
ASSET_SUFFIX = "_2056_de.gpkg.zip"
STOPS_DIR = ROOT / "data/stops"
LV95_TO_WGS84 = Transformer.from_crs("EPSG:2056", "EPSG:4326", always_xy=True)
PASSENGER_TYPES = {"Haltestelle", "Haltestelle und Bedienpunkt"}
MODES = {"A": "bus", "B": "train", "C": "tram", "D": "metro", "E": "rack_railway", "F": "funicular",
         "G": "cableway", "H": "chairlift", "I": "boat", "J": "elevator"}


def download() -> str:
    """Find the current LV95 GeoPackage through the STAC catalogue and unpack it into data/stops/.
    Returns the asset URL (it changes with each edition)."""
    client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True)
    items = client.get(STAC_ITEMS).raise_for_status().json()["features"]
    href = next(a["href"] for item in items for a in item["assets"].values() if a["href"].endswith(ASSET_SUFFIX))
    archive = client.get(href).raise_for_status().content
    STOPS_DIR.mkdir(parents=True, exist_ok=True)
    zipfile.ZipFile(io.BytesIO(archive)).extractall(STOPS_DIR)
    print(f"downloaded {href} ({len(archive) / 1e6:.1f} MB)")
    return href


def read_stops() -> list[dict]:
    gpkg = next(STOPS_DIR.rglob("*.gpkg"))
    meta, _, geoms, fields = pyogrio.raw.read(gpkg, layer="Betriebspunkt")
    cols = dict(zip(meta["fields"], fields))
    stops = []
    for i, wkb in enumerate(geoms):
        modes = [MODES[c] for c in (cols["Verkehrsmittel_Code"][i] or "") if c in MODES]
        if cols["Betriebspunkttyp_Bezeichnung"][i] not in PASSENGER_TYPES or not modes or wkb is None:
            continue
        point = from_wkb(wkb)
        lon, lat = LV95_TO_WGS84.transform(point.x, point.y)
        stops.append({
            "uic": int(cols["Nummer"][i]),
            "name": cols["Name"][i],
            "mode": ",".join(modes),
            "e_lv95": point.x, "n_lv95": point.y, "lat": round(lat, 6), "lon": round(lon, 6),
        })
    return stops


def load(conn: sqlite3.Connection, stops: list[dict]) -> int:
    existing = dict(conn.execute("SELECT uic, place_id FROM stop").fetchall())
    with conn:
        for s in stops:
            place = {k: s[k] for k in ("name", "e_lv95", "n_lv95", "lat", "lon")}
            if s["uic"] in existing:
                conn.execute("UPDATE place SET name = :name, e_lv95 = :e_lv95, n_lv95 = :n_lv95, lat = :lat, lon = :lon "
                             "WHERE id = :id", place | {"id": existing[s["uic"]]})
                conn.execute("UPDATE stop SET mode = ? WHERE uic = ?", (s["mode"], s["uic"]))
            else:
                place_id = conn.execute("INSERT INTO place (kind, name, e_lv95, n_lv95, lat, lon) "
                                        "VALUES ('stop', :name, :e_lv95, :n_lv95, :lat, :lon)", place).lastrowid
                conn.execute("INSERT INTO stop (place_id, uic, mode) VALUES (?, ?, ?)", (place_id, s["uic"], s["mode"]))
    return len(stops)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-download", action="store_true", help="use the GeoPackage already in data/stops/")
    args = parser.parse_args()
    if not args.no_download:
        download()
    print(f"loaded {load(connect(), read_stops())} stops")


if __name__ == "__main__":
    main()
