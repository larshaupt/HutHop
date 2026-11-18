"""Regions outside Switzerland and Liechtenstein that HutHop covers, and the area each covers.

A region brings its own sources: trails (OpenStreetMap, osm_trails), huts (hut-reservation.org, sync_hrs) and
timetables (load_timetable). Its area is the polygon of its OpenStreetMap extract (Geofabrik), which reaches a few
kilometres past the country's border; within it, OpenStreetMap trails replace swissTLM3D outside Switzerland and
Liechtenstein, and huts are imported.
"""

from functools import cache

import httpx
import numpy as np
import shapely
from pyproj import Transformer

from app.clients import USER_AGENT
from app.db import ROOT

# Region code → Geofabrik extract (the .osm.pbf and .poly share the name).
REGIONS = {"AT": "https://download.geofabrik.de/europe/austria"}
DIR = ROOT / "data/osm"
WGS84_TO_LV95 = Transformer.from_crs("EPSG:4326", "EPSG:2056", always_xy=True)


def poly_path(code: str):
    return DIR / f"region_{code.lower()}.poly"


def download_polygons() -> None:
    """Fetch each region's extract polygon (a few KB) when it is missing."""
    DIR.mkdir(parents=True, exist_ok=True)
    for code, base in REGIONS.items():
        if not poly_path(code).exists():
            text = httpx.get(f"{base}.poly", headers={"User-Agent": USER_AGENT}, timeout=60).raise_for_status().text
            poly_path(code).write_text(text)
            area.cache_clear()


def parse_poly(text: str) -> shapely.Geometry:
    """An Osmosis .poly file (rings of "lon lat" lines; a ring name starting with ! is a hole) as WGS84 polygon."""
    lines = [line.strip() for line in text.splitlines()]
    outer, holes, ring, name = [], [], None, None
    for line in lines[1:]:
        if ring is None:
            if line == "END":
                break
            ring, name = [], line
        elif line == "END":
            (holes if name.startswith("!") else outer).append(shapely.Polygon(ring))
            ring = None
        else:
            lon, lat = map(float, line.split())
            ring.append((lon, lat))
    return shapely.difference(shapely.union_all(outer), shapely.union_all(holes)) if holes else shapely.union_all(outer)


@cache
def area(code: str) -> shapely.Geometry | None:
    """The region's area in LV95, prepared; None before its polygon is downloaded."""
    if not poly_path(code).exists():
        return None
    shape = shapely.transform(parse_poly(poly_path(code).read_text()),
                              lambda xy: np.column_stack(WGS84_TO_LV95.transform(xy[:, 0], xy[:, 1])))
    shapely.prepare(shape)
    return shape


def areas() -> dict[str, shapely.Geometry]:
    return {code: a for code in REGIONS if (a := area(code)) is not None}

