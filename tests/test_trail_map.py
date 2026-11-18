import math

import numpy as np
import pyogrio.raw
import shapely
from pyproj import Transformer

from app.ingest import trail_map

LV95_TO_WGS84 = Transformer.from_crs("EPSG:2056", "EPSG:4326", always_xy=True)


def slippy(z, e, n):
    """The tile of an LV95 point at zoom z, and its position in that tile (0–EXTENT), by the usual tile formula."""
    lon, lat = LV95_TO_WGS84.transform(e, n)
    fx = (lon + 180) / 360 * 2 ** z
    fy = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * 2 ** z
    return (int(fx), int(fy)), ((fx % 1) * trail_map.EXTENT, (fy % 1) * trail_map.EXTENT)


def test_tiles_of_the_trail_file(tmp_path, monkeypatch):
    lines = [[(2_800_000, 1_230_000, 1500), (2_806_000, 1_234_000, 1800)],  # a mountain trail
             [(2_800_000, 1_234_000, 1500), (2_806_000, 1_230_000, 1500)]]  # an unofficial path, crossing it
    pyogrio.raw.write(tmp_path / "trails.gpkg", shapely.to_wkb(shapely.linestrings(lines)),
                      field_data=[np.array([2, 4], np.int8)], fields=["category"], layer="trails", driver="GPKG",
                      geometry_type="LineString Z", crs="EPSG:2056")
    monkeypatch.setattr(trail_map.osm_trails, "trails_path", lambda code: tmp_path / "trails.gpkg")
    monkeypatch.setattr(trail_map, "map_path", lambda code: tmp_path / f"map_{code}.npz")
    monkeypatch.setattr(trail_map.regions, "REGIONS", {"AT": trail_map.regions.REGIONS["AT"]})
    monkeypatch.setattr(trail_map.regions, "area", lambda code: shapely.box(2_790_000, 1_220_000, 2_810_000, 1_240_000))
    assert trail_map.refresh()
    assert not trail_map.refresh()  # up to date

    # the mountain trail passes through its midpoint, where the tile formula puts it; the unofficial path isn't drawn
    tile, (px, py) = slippy(14, 2_803_000, 1_232_000)
    out = trail_map.tile(14, *tile)
    assert out["area"] == "all"
    assert [line[0] for line in out["lines"]] == [2]
    drawn = shapely.LineString(np.reshape(out["lines"][0][1:], (-1, 2)))
    assert drawn.distance(shapely.Point(px, py)) < 8  # half a pixel (a straight line in LV95 bends a little)

    # at the area's edge: the trails replace other sources only inside it
    tile, (px, py) = slippy(12, 2_810_000, 1_232_000)
    edge = trail_map.tile(12, *tile)
    area = shapely.Polygon(np.reshape(edge["area"][0], (-1, 2)))
    assert area.contains(shapely.Point(px - 20, py)) and not area.contains(shapely.Point(px + 20, py))
    assert trail_map.tile(14, *slippy(14, 2_830_000, 1_232_000)[0]) == {"area": None, "lines": []}
