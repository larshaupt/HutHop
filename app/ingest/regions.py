"""Regions outside Switzerland and Liechtenstein that HutHop covers, and the area each covers.

A region brings its own sources: trails (OpenStreetMap, osm_trails), huts (hut-reservation.org, sync_hrs) and
timetables (load_timetable). Its area is the polygon of its OpenStreetMap extract (Geofabrik, openstreetmap.fr),
which reaches a few kilometres past its border, cut to the mountains where the extract is larger (Bavaria: its
Alpine south); within it, OpenStreetMap trails replace swissTLM3D outside Switzerland and Liechtenstein, and huts
are imported. Where two regions' areas overlap, the first in REGIONS has it (see own_area).
"""

from dataclasses import dataclass
from functools import cache

import httpx
import numpy as np
import shapely
from pyproj import Transformer

from app.clients import USER_AGENT
from app.db import ROOT


@dataclass(frozen=True)
class Region:
    pbf: str  # the OpenStreetMap extract
    poly: str  # its polygon (Osmosis .poly)
    whole_country: bool = False  # the extract holds the whole country, so its border comes from OpenStreetMap
    south_of: float | None = None  # only the part south of this latitude (WGS84): the mountains of a larger extract


GEOFABRIK, OSM_FR = "https://download.geofabrik.de/europe", "https://download.openstreetmap.fr"
# Region code (the country's ISO code) → extract
REGIONS = {
    "AT": Region(f"{GEOFABRIK}/austria-latest.osm.pbf", f"{GEOFABRIK}/austria.poly", whole_country=True),
    # the Bavarian Alps and their foothills down to Rosenheim, Traunstein and Kempten
    "DE": Region(f"{GEOFABRIK}/germany/bayern-latest.osm.pbf", f"{GEOFABRIK}/germany/bayern.poly", south_of=47.9),
    # Trentino-South Tyrol (Geofabrik's Italian extracts are larger: Nord-Est)
    "IT": Region(f"{OSM_FR}/extracts/europe/italy/trentino_alto_adige-latest.osm.pbf",
                 f"{OSM_FR}/polygons/europe/italy/trentino_alto_adige.poly"),
}
DIR = ROOT / "data/osm"
WGS84_TO_LV95 = Transformer.from_crs("EPSG:4326", "EPSG:2056", always_xy=True)


def poly_path(code: str):
    return DIR / f"region_{code.lower()}.poly"


def download_polygons() -> None:
    """Fetch each region's extract polygon (a few KB) when it is missing."""
    DIR.mkdir(parents=True, exist_ok=True)
    for code, region in REGIONS.items():
        if not poly_path(code).exists():
            text = httpx.get(region.poly, headers={"User-Agent": USER_AGENT}, timeout=60).raise_for_status().text
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
    shape = parse_poly(poly_path(code).read_text())
    if (south := REGIONS[code].south_of) is not None:
        shape = shapely.clip_by_rect(shape, -180, -90, 180, south)
    shape = shapely.transform(shape, lambda xy: np.column_stack(WGS84_TO_LV95.transform(xy[:, 0], xy[:, 1])))
    shapely.prepare(shape)
    return shape


def areas() -> dict[str, shapely.Geometry]:
    return {code: a for code in REGIONS if (a := area(code)) is not None}



def own_area(code: str) -> shapely.Geometry | None:
    """The part of the region's area no region before it in REGIONS has: where its trails are used. The extracts'
    polygons overlap by a few kilometres at the borders between them."""
    if (shape := area(code)) is None:
        return None
    before = [a for c in REGIONS if c != code and list(REGIONS).index(c) < list(REGIONS).index(code)
              and (a := area(c)) is not None]
    return shapely.difference(shape, shapely.union_all(before)) if before else shape
