"""The trail overlay of the map outside Switzerland and Liechtenstein, drawn from our own trail files (osm_trails).

    uv run python -m app.ingest.trail_map    # (re)write the map files of trail files that changed

swisstopo's trail map ends at the border, and Waymarked Trails only shows ways in a hiking route relation: about a
third of the paths around the Rätikon. So the map draws the network the planner walks on instead, categories 1–3
(the unofficial paths come from /api/osm-paths, with the trail limit). Each trail file gets a map file,
data/osm/trails_map_<code>.npz: its lines joined per category and simplified for a few zoom ranges, in Web Mercator
metres. The app reads it once per edition and cuts tiles from it (/api/trail-tiles), which js/map.js draws in
swisstopo's colours. About 50 MB in memory for Austria.
"""

from functools import lru_cache

import numpy as np
import pyogrio.raw
import shapely
from pyproj import Transformer

from app.ingest import osm_trails, regions

# (lowest zoom level, simplification in metres on the ground): about half a pixel at that zoom level. Lines shorter
# than MIN_PIXELS there are left out (bits of trail between junctions that don't show at that scale).
LEVELS = ((0, 400.0), (9, 100.0), (11, 25.0), (13, 6.0), (15, 1.5))
MIN_PIXELS = 4
CATEGORIES = (1, 2, 3)
EXTENT = 4096  # tile coordinates: 0–4096 across a tile (16 per pixel)
MARGIN = 64  # lines are clipped this far past the tile's edge, so that their ends don't show
WORLD = 2 * np.pi * 6378137.0  # Web Mercator's width in metres
LV95_TO_MERCATOR = Transformer.from_crs("EPSG:2056", "EPSG:3857", always_xy=True)


def map_path(code: str):
    return regions.DIR / f"trails_map_{code.lower()}.npz"


def to_mercator(geom: shapely.Geometry) -> shapely.Geometry:
    return shapely.transform(geom, lambda xy: np.column_stack(LV95_TO_MERCATOR.transform(xy[:, 0], xy[:, 1])))


def write(code: str) -> None:
    """The map file of one region's trail file."""
    _, _, wkb, (cats,) = pyogrio.raw.read(osm_trails.trails_path(code), layer="trails", columns=["category"],
                                          where=f"category <= {max(CATEGORIES)}")
    lines = shapely.force_2d(shapely.from_wkb(wkb))
    del wkb
    arrays = {"area": np.frombuffer(shapely.to_wkb(to_mercator(regions.area(code))), dtype=np.uint8)}
    joined = {c: shapely.get_parts(shapely.line_merge(shapely.multilinestrings(lines[cats == c]))) for c in CATEGORIES}
    for level, (_, tolerance) in enumerate(LEVELS):
        parts = [(c, shapely.simplify(g[shapely.length(g) >= MIN_PIXELS * 2 * tolerance], tolerance))
                 for c, g in joined.items()]
        geoms = to_mercator(np.concatenate([g for _, g in parts]))
        xy, index = shapely.get_coordinates(geoms, return_index=True)
        arrays |= {f"xy{level}": xy.astype(np.float32),  # metres: ~0.06 m steps at Austria's 1–2,000 km
                   f"start{level}": np.searchsorted(index, np.arange(len(geoms) + 1)).astype(np.int32),
                   f"category{level}": np.concatenate([np.full(len(g), c, np.int8) for c, g in parts]),
                   f"bounds{level}": shapely.bounds(geoms).astype(np.float32)}
    part = map_path(code).with_suffix(".part.npz")
    np.savez(part, **arrays)
    part.rename(map_path(code))
    print(f"trail_map {code}: " + ", ".join(f"z{z}+ {len(arrays[f'xy{i}']):,} points"
                                             for i, (z, _) in enumerate(LEVELS)), flush=True)


def refresh() -> bool:
    """Write the map file of each trail file newer than its map file. True if one was written."""
    changed = False
    for code in regions.REGIONS:
        trails, out = osm_trails.trails_path(code), map_path(code)
        if trails.exists() and regions.area(code) is not None and (
                not out.exists() or out.stat().st_mtime < trails.stat().st_mtime):
            write(code)
            changed = True
    return changed


def edition() -> tuple:
    """Which map files there are, and when they were written: refresh writes them while the app runs."""
    return tuple((code, map_path(code).stat().st_mtime) for code in regions.REGIONS if map_path(code).exists())


@lru_cache(maxsize=1)
def _load(stamp: tuple) -> list[dict]:
    maps = []
    for code, _ in stamp:
        with np.load(map_path(code)) as f:
            area = shapely.from_wkb(f["area"].tobytes())
            shapely.prepare(area)
            maps.append({"area": area, "levels": [{k: f[f"{k}{i}"] for k in ("xy", "start", "category", "bounds")}
                                                  for i in range(len(LEVELS))]})
    return maps


def tile(z: int, x: int, y: int) -> dict:
    """The trails of one map tile, in tile coordinates (0–EXTENT, y down):
    {"area": where they replace other trail maps ("all", None, or polygon rings), "lines": [[category, x, y, x, y, …]]}.
    Outside every region's area (or without map files) {"area": None, "lines": []}."""
    size = WORLD / 2 ** z
    x0, y1 = -WORLD / 2 + x * size, WORLD / 2 - y * size  # west, north
    box = shapely.box(x0, y1 - size, x0 + size, y1)
    pad = size * MARGIN / EXTENT
    level = max(i for i, (zoom, _) in enumerate(LEVELS) if zoom <= z)
    to_tile = lambda xy: np.column_stack([(xy[:, 0] - x0) / size * EXTENT, (y1 - xy[:, 1]) / size * EXTENT])  # noqa: E731
    covered, lines = [], []
    for m in _load(edition()):
        if not m["area"].intersects(box):
            continue
        covered.append(m["area"])
        d = m["levels"][level]
        b = d["bounds"]
        hit = np.flatnonzero((b[:, 0] <= x0 + size + pad) & (b[:, 2] >= x0 - pad) & (b[:, 1] <= y1 + pad)
                             & (b[:, 3] >= y1 - size - pad))
        if not len(hit):
            continue
        counts = d["start"][hit + 1] - d["start"][hit]
        rows = np.repeat(d["start"][hit] - np.cumsum(counts) + counts, counts) + np.arange(counts.sum())
        geoms = shapely.linestrings(d["xy"][rows].astype(np.float64), indices=np.repeat(np.arange(len(hit)), counts))
        clipped = shapely.clip_by_rect(geoms, x0 - pad, y1 - size - pad, x0 + size + pad, y1 + pad)
        for category, geom in zip(d["category"][hit], clipped):
            for part in shapely.get_parts(geom):
                if isinstance(part, shapely.LineString) and not part.is_empty:
                    xy = np.round(to_tile(shapely.get_coordinates(part))).astype(int)
                    lines.append([int(category), *xy.ravel().tolist()])
    if not covered:
        return {"area": None, "lines": []}
    area = shapely.union_all(covered)
    if area.contains(box):
        return {"area": "all", "lines": lines}
    inside = shapely.clip_by_rect(area, x0 - pad, y1 - size - pad, x0 + size + pad, y1 + pad)
    rings = [np.round(to_tile(shapely.get_coordinates(r))).astype(int).ravel().tolist()
             for p in shapely.get_parts(inside) if isinstance(p, shapely.Polygon)
             for r in (p.exterior, *p.interiors)]
    return {"area": rings, "lines": lines}


if __name__ == "__main__":
    for code in regions.REGIONS:
        if osm_trails.trails_path(code).exists():
            write(code)
