"""Terrain heights from the Copernicus DEM GLO-30 (~30 m), for trails without heights of their own (OpenStreetMap).

Tiles of 1°×1° (Cloud Optimized GeoTIFF, WGS84, heights in metres) from the AWS open data bucket, downloaded once
into data/dem/ (~20–40 MB each) as they are needed. The DEM is a surface model: in forest it gives the canopy
height, so a trail through woods gets a few metres of noise; hiking_time smooths it by merging vertices into ~20 m
pieces.

© DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union
and ESA; all rights reserved (free licence, attribution required).
"""

import httpx
import numpy as np
import rasterio
from pyproj import Transformer

from app.clients import USER_AGENT
from app.db import ROOT

DIR = ROOT / "data/dem"
URL = "https://copernicus-dem-30m.s3.amazonaws.com/{name}/{name}.tif"
LV95_TO_WGS84 = Transformer.from_crs("EPSG:2056", "EPSG:4326", always_xy=True)


def tile_name(lat: int, lon: int) -> str:
    """Name of the tile whose south-west corner is at (lat, lon), e.g. Copernicus_DSM_COG_10_N47_00_E009_00_DEM."""
    return (f"Copernicus_DSM_COG_10_{'N' if lat >= 0 else 'S'}{abs(lat):02d}_00_"
            f"{'E' if lon >= 0 else 'W'}{abs(lon):03d}_00_DEM")


def tile_path(lat: int, lon: int):
    """The tile's local copy, downloaded first if needed; None where the DEM has no tile (open sea)."""
    name = tile_name(lat, lon)
    path = DIR / f"{name}.tif"
    if not path.exists():
        DIR.mkdir(parents=True, exist_ok=True)
        with httpx.stream("GET", URL.format(name=name), headers={"User-Agent": USER_AGENT}, timeout=300) as r:
            if r.status_code in (403, 404):  # the bucket answers 403 for tiles that don't exist
                return None
            r.raise_for_status()
            part = path.with_suffix(".part")
            with part.open("wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
            part.rename(path)
        print(f"dem: downloaded {name}")
    return path


def bilinear(grid: np.ndarray, col: np.ndarray, row: np.ndarray) -> np.ndarray:
    """Values of a grid at fractional pixel-centre positions (column, row), interpolated between the four nearest
    pixels; positions within half a pixel of the edge use the edge pixels."""
    h, w = grid.shape
    col, row = np.clip(col, 0, w - 1), np.clip(row, 0, h - 1)
    c0, r0 = np.minimum(col.astype(int), w - 2), np.minimum(row.astype(int), h - 2)
    fc, fr = col - c0, row - r0
    top = grid[r0, c0] * (1 - fc) + grid[r0, c0 + 1] * fc
    bottom = grid[r0 + 1, c0] * (1 - fc) + grid[r0 + 1, c0 + 1] * fc
    return top * (1 - fr) + bottom * fr


def heights(e: np.ndarray, n: np.ndarray) -> np.ndarray:
    """Height (m) at LV95 points, interpolated bilinearly; NaN where there is no tile. One tile in memory at a time
    (~50 MB)."""
    lon, lat = LV95_TO_WGS84.transform(np.asarray(e, dtype=float), np.asarray(n, dtype=float))
    z = np.full(len(lon), np.nan)
    tile = np.floor(lat).astype(int) * 1000 + np.floor(lon).astype(int)
    for key in np.unique(tile):
        sel = np.flatnonzero(tile == key)
        path = tile_path(int(np.floor(lat[sel[0]])), int(np.floor(lon[sel[0]])))
        if path is None:
            continue
        with rasterio.open(path) as src:
            grid = src.read(1).astype(np.float32)
            col, row = ~src.transform * (lon[sel], lat[sel])
        z[sel] = bilinear(grid, np.asarray(col) - 0.5, np.asarray(row) - 0.5)
    return z
