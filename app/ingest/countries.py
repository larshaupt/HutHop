"""Country borders: which country each hut and stop is in, and which parts of a route leave Switzerland.

    uv run python -m app.ingest.countries          # download swissBOUNDARIES3D if new, then tag all places

Switzerland and Liechtenstein come from swisstopo's swissBOUNDARIES3D (`tlm_landesgebiet`, open data, ~40 MB,
published about yearly). They are kept simplified in data/countries.json (LV95), which the app also reads: trail
closures and military shooting notices are only published for Switzerland (app/api/tours.py). The countries of the
regions (regions.py, e.g. Austria) come from their OpenStreetMap extract (osm_trails, data/osm/border_<code>.json).
Places elsewhere get the country their source gives, or none.
"""

import io
import json
import sqlite3
import zipfile
from functools import cache

import httpx
import numpy as np
import pyogrio.raw
import shapely
from pyproj import Transformer

from app.clients import USER_AGENT
from app.db import ROOT, connect
from app.ingest import regions

STAC_ITEMS = "https://data.geo.admin.ch/api/stac/v1/collections/ch.swisstopo.swissboundaries3d/items?limit=100"
FILE = ROOT / "data/countries.json"
COUNTRIES = ("CH", "LI")  # complete in swissBOUNDARIES3D; it also has the enclaves of Germany and Italy
SIMPLIFY_M = 5.0
ON_BORDER_M = 1.0
WGS84_TO_LV95 = Transformer.from_crs("EPSG:4326", "EPSG:2056", always_xy=True)


def download() -> bool:
    """Download the newest swissBOUNDARIES3D edition if it is not the one in data/countries.json. True if updated."""
    client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=120, follow_redirects=True)
    items = client.get(STAC_ITEMS).raise_for_status().json()["features"]
    href = max(a["href"] for item in items for a in item["assets"].values() if a["href"].endswith("_2056_5728.gpkg.zip"))
    if FILE.exists() and json.loads(FILE.read_text()).get("href") == href:
        print(f"countries: up to date ({href.rsplit('/', 1)[-1]})")
        return False
    print(f"countries: downloading {href}")
    archive = zipfile.ZipFile(io.BytesIO(client.get(href).raise_for_status().content))
    gpkg = next(n for n in archive.namelist() if n.endswith(".gpkg"))
    path = ROOT / "data/raw" / gpkg.rsplit("/", 1)[-1]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(archive.read(gpkg))
    _, _, geoms, (codes,) = pyogrio.raw.read(path, layer="tlm_landesgebiet", columns=["icc"])
    shapes = {}
    for code, wkb in zip(codes, geoms):
        if code in COUNTRIES:
            shape = shapely.force_2d(shapely.from_wkb(wkb))
            shapes[code] = shapely.to_geojson(shapely.simplify(shape, SIMPLIFY_M))
    FILE.write_text(json.dumps({"href": href, "crs": "EPSG:2056", "countries": shapes}))
    path.unlink()
    borders.cache_clear()
    return True


def region_border_path(code: str):
    """A region's country border (same format as countries.json, from its OpenStreetMap extract by osm_trails), in
    data/osm/ next to countries.json."""
    return FILE.parent / "osm" / f"border_{code.lower()}.json"


@cache
def borders() -> dict[str, shapely.Geometry]:
    """Country code → area (LV95), prepared for fast point tests: Switzerland and Liechtenstein, then the regions'
    countries (e.g. AT) whose border osm_trails has extracted; empty before the first download."""
    if not FILE.exists():
        return {}
    shapes = {code: shapely.from_geojson(g) for code, g in json.loads(FILE.read_text())["countries"].items()}
    for code in regions.REGIONS:
        if region_border_path(code).exists():
            shapes[code] = shapely.from_geojson(json.loads(region_border_path(code).read_text())["countries"][code])
    for shape in shapes.values():
        shapely.prepare(shape)
    return shapes


def country_at(e: np.ndarray, n: np.ndarray) -> list[str | None]:
    """The country of each LV95 point: CH or LI, or a region's country (AT), else None (elsewhere, or no border data
    yet). A point within a metre of the border counts as inside, Switzerland and Liechtenstein first (Pfälzerhütte
    stands on the Liechtenstein–Austria border)."""
    points = shapely.points(np.asarray(e, dtype=float), np.asarray(n, dtype=float))
    found: list[str | None] = [None] * len(points)
    for code, shape in borders().items():
        for i in np.flatnonzero(shapely.dwithin(shape, points, ON_BORDER_M)):
            if code in COUNTRIES or found[i] is None:
                found[i] = code
    return found


def tag_places(conn: sqlite3.Connection) -> dict[str, int]:
    """Set place.country from the borders for every place inside them; places outside keep their source's country.
    Returns the number of places per country."""
    rows = conn.execute("SELECT id, e_lv95, n_lv95 FROM place").fetchall()
    if not rows or not borders():
        return {}
    codes = country_at([r["e_lv95"] for r in rows], [r["n_lv95"] for r in rows])
    with conn:
        conn.executemany("UPDATE place SET country = ? WHERE id = ?",
                         [(code, r["id"]) for r, code in zip(rows, codes) if code])
    return {code: codes.count(code) for code in borders() if code in COUNTRIES or code in codes}


def outside(latlon: list[tuple[float, float]], code: str = "CH", tolerance_m: float = 50.0) -> list[str]:
    """Where a route line of (lat, lon) points leaves the country: the other countries it passes through (`LI`, `AT`
    once its border is extracted, or `abroad` for anywhere else), in order of first visit. Points within
    `tolerance_m` of the border count as inside. Empty without border data."""
    shapes = borders()
    if code not in shapes or not len(latlon):
        return []
    lat, lon = np.asarray(latlon, dtype=float).T
    points = shapely.points(*WGS84_TO_LV95.transform(lon, lat))
    away = ~shapely.dwithin(shapes[code], points, tolerance_m)
    found: list[str] = []
    for i in np.flatnonzero(away):
        other = next((c for c, s in shapes.items() if c != code and shapely.contains(s, points[i])), "abroad")
        if other not in found:
            found.append(other)
    return found


def main() -> None:
    download()
    print(f"places per country: {tag_places(connect())}")


if __name__ == "__main__":
    main()
