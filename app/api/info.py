"""What the About dialog shows (how "best" is scored, with the configured weights, and the impressum), and for the map
the border of Switzerland and Liechtenstein and the trails outside it."""

import json
from functools import lru_cache

import numpy as np
import shapely
from fastapi import APIRouter, Response
from pyproj import Transformer

from app import config
from app.ingest import countries, trail_map
from app.planner import ranking

router = APIRouter(prefix="/api", tags=["info"])
BORDER_SIMPLIFY_M = 100
LV95_TO_WGS84 = Transformer.from_crs("EPSG:2056", "EPSG:4326", always_xy=True)


@router.get("/info")
def info() -> dict:
    weights = config.ranking_weights()
    total = sum(weights.values()) or 1
    return {
        "metrics": [{"key": k, "label": label, "description": text, "weight": weights[k],
                     "share": round(weights[k] / total, 3)} for k, (label, text) in ranking.METRIC_INFO.items()],
        "impressum": config.impressum(),
    }


@router.get("/border")
def border() -> dict:
    """Switzerland and Liechtenstein as one GeoJSON area (WGS84, simplified to 100 m), or null before
    swissBOUNDARIES3D is downloaded. Inside it the map shows swisstopo, outside the maps of other sources (js/map.js)."""
    return {"type": "Feature", "properties": {}, "geometry": _border(countries.edition())}


@lru_cache(maxsize=1)
def _border(stamp: tuple) -> dict | None:
    """Per edition of the border files (countries.edition): refresh writes them while the app runs."""
    shapes = countries.borders()
    if "CH" not in shapes:
        return None
    area = shapely.simplify(shapely.union_all([shapes[c] for c in countries.COUNTRIES if c in shapes]), BORDER_SIMPLIFY_M)
    area = shapely.transform(area, lambda xy: np.round(np.column_stack(LV95_TO_WGS84.transform(*xy.T)), 5))
    return json.loads(shapely.to_geojson(area))


@router.get("/trail-tiles/{z}/{x}/{y}")
def trail_tile(z: int, x: int, y: int, response: Response) -> dict:
    """One map tile of the trails outside Switzerland and Liechtenstein (trail_map.tile), drawn by js/map.js."""
    response.headers["Cache-Control"] = "max-age=86400"  # the trail files change about monthly
    return trail_map.tile(z, x, y)
