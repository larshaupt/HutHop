"""The hiking network outside Switzerland and Liechtenstein, from OpenStreetMap (regional extracts, regions.py).

    uv run python -m app.ingest.osm_trails            # download each region's extract if older than 30 days, extract
    uv run python -m app.ingest.osm_trails --force    # download and extract again

For each region in regions.REGIONS, the extract (data/osm/<name>-latest.osm.pbf, ~800 MB for Austria) is turned
into data/osm/trails_<code>.gpkg: 3D lines (LV95) with a trail category like swissTLM3D's, split at junctions,
clipped to the region's own area (regions.own_area) outside Switzerland and Liechtenstein (swissTLM3D covers those). build_edges joins
them to swissTLM3D at the border. Heights come from the Copernicus DEM (dem.py). The same pass saves the country's
border as data/osm/border_<code>.json, which countries.borders() reads: from its admin_level=2 boundary relation
where the extract holds the whole country (Austria), else the region's area outside the other known countries.

Selection, mirroring swissTLM3D's signposted network versus unofficial paths:
- signposted: ways in a route=hiking|foot relation (any highway: routes also follow village roads, which link the
  trails to the stops), and paths, tracks, footways and steps with a sac_scale. Only about a third of the paths
  around the Rätikon are in a route relation; many hut paths (Oberzalimhütte, Heinrich-Hueter-Hütte) are only graded.
  Category from sac_scale: hiking 1, mountain_hiking and demanding_mountain_hiking 2, alpine_hiking and harder 3;
  without one: paths 2, everything else 1. Via ferratas only within a route, as 3.
- unofficial (4): any other highway=path.
- hut access (1): tracks, footways and minor roads within 1 km of a hut (tourism=alpine_hut|wilderness_hut): see
  ACCESS.
Ways closed to walkers (foot=no, access=no|private without a foot permission) are left out.

Memory stays at about 1 GB for Austria without a node location index: relations, then the selected ways' node ids,
then only those nodes' locations, each a pass over the file.

Data © OpenStreetMap contributors (ODbL), extracts by Geofabrik.
"""

import argparse
import json
import time
from array import array
from datetime import UTC, datetime

import httpx
import numpy as np
import osmium
import pyogrio.raw
import shapely
from pyproj import Transformer
from scipy.spatial import cKDTree

from app.clients import USER_AGENT
from app.ingest import countries, dem, regions

MAX_AGE_DAYS = 30  # Geofabrik updates daily; trails change slowly
UNOFFICIAL = 4
SAC_CATEGORY = {"strolling": 1, "hiking": 1, "mountain_hiking": 2, "demanding_mountain_hiking": 2,
                "alpine_hiking": 3, "demanding_alpine_hiking": 3, "difficult_alpine_hiking": 3}
GRADED = {"path", "track", "footway", "steps", "bridleway"}  # signposted with a sac_scale even outside a route
NOT_WALKED = {"motorway", "motorway_link", "construction", "proposed", "abandoned", "disused", "razed", "platform",
              "raceway", "bus_stop", "rest_area", "services", "elevator", "corridor", "busway"}
FOOT_ALLOWED = {"yes", "designated", "permissive", "official"}
STEP_M = 20.0  # densify before sampling heights: hiking_time works in ~20 m pieces
SMOOTH_M = 40.0  # heights averaged over ±40 m along the line: see smooth_heights
SIMPLIFY_BORDER_M = 5.0
# The node locations are read in passes over slices of the id range: osmium's id filter takes a bit per id of the
# range it covers (in 4 MB chunks), ~1.6 GB for all of OpenStreetMap's node ids at once, ~0.15 GB per slice.
NODE_PASSES = 12
HUTS = {"alpine_hut", "wilderness_hut"}
# Roads and tracks in no route and without a sac_scale, kept within ACCESS_M of a hut as category 1: many huts are
# reached only by an unsigned access track (Heinrich-Hueter-Hütte, Kaltenberghütte), without which their graded
# paths would be islands. All of them would triple the trail file and the build's memory.
ACCESS = {"track", "service", "footway", "steps", "bridleway", "pedestrian", "unclassified", "residential",
          "living_street"}
ACCESS_M = 1000.0
ACCESS_CATEGORY = 1
CHUNK = 50_000  # trail pieces given heights and written at a time
WGS84_TO_LV95 = Transformer.from_crs("EPSG:4326", "EPSG:2056", always_xy=True)


def pbf_path(code: str):
    return regions.DIR / regions.REGIONS[code].pbf.rsplit("/", 1)[1]


def trails_path(code: str):
    return regions.DIR / f"trails_{code.lower()}.gpkg"


def edition_path(code: str):
    """The extract the trail file was made from (href, Last-Modified, size), written after each extraction."""
    return regions.DIR / f"edition_{code.lower()}.json"


def category(tags, in_route: bool) -> int | None:
    """Trail category (1–3 signposted, 4 unofficial) of a way with these tags, or None to leave it out."""
    highway = tags.get("highway")
    if highway is None or highway in NOT_WALKED or tags.get("foot") in ("no", "private"):
        return None
    if tags.get("access") in ("no", "private") and tags.get("foot") not in FOOT_ALLOWED:
        return None
    sac = SAC_CATEGORY.get(tags.get("sac_scale"))
    if highway == "via_ferrata":
        return 3 if in_route else None
    if in_route or (sac and highway in GRADED):
        return sac or (2 if highway == "path" else 1)
    return UNOFFICIAL if highway == "path" else None


def split_at_junctions(ways: list[np.ndarray]) -> list[tuple[int, int, int]]:
    """Pieces (way index, first, last vertex index) of ways given as node id arrays, split wherever a node is shared
    with another way (or visited twice by one), so that the trail graph has a node at every junction. A closed way
    is also split in the middle: a line from a node to itself would be dropped from the graph."""
    _, inverse, counts = np.unique(np.concatenate(ways), return_inverse=True, return_counts=True)
    is_shared = np.split(counts[inverse] > 1, np.cumsum([len(refs) for refs in ways])[:-1])
    pieces = []
    for w, refs in enumerate(ways):
        if len(refs) < 2:
            continue
        cuts = np.flatnonzero(is_shared[w][1:-1]) + 1
        if refs[0] == refs[-1] and not len(cuts):
            cuts = np.array([len(refs) // 2])
        bounds = [0, *cuts.tolist(), len(refs) - 1]
        pieces += [(w, a, b) for a, b in zip(bounds[:-1], bounds[1:]) if b > a]
    return pieces


def clip(lines: np.ndarray, area: shapely.Geometry, inside: bool) -> tuple[np.ndarray, np.ndarray]:
    """The parts of lines (shapely array) inside the area (or outside it, `inside=False`), as (source index, line).
    Lines wholly on the kept side are returned unchanged; only those crossing the area's edge are cut (heights are
    interpolated at the cut)."""
    shapely.prepare(area)
    covered = shapely.contains_properly(area, lines)
    crossing = ~covered & shapely.intersects(area, lines)
    whole = np.flatnonzero(covered if inside else ~covered & ~crossing)
    cut_idx = np.flatnonzero(crossing)
    cut = (shapely.intersection if inside else shapely.difference)(lines[cut_idx], area)
    parts, part_idx = shapely.get_parts(cut, return_index=True)
    keep = (shapely.get_type_id(parts) == 1) & (shapely.length(parts) > 0)  # LineStrings, no stray points
    index = np.concatenate([whole, cut_idx[part_idx[keep]]])
    geoms = np.concatenate([lines[whole], parts[keep]])
    order = np.argsort(index, kind="stable")
    return index[order], geoms[order]


def swiss_area() -> shapely.Geometry | None:
    """Switzerland and Liechtenstein (LV95), where swissTLM3D is used; None without data/countries.json."""
    shapes = countries.borders()
    if not all(c in shapes for c in countries.COUNTRIES):
        return None
    return shapely.union_all([shapes[c] for c in countries.COUNTRIES])


def read_lines(path, column: str, layer: str | None = None) -> tuple[list[np.ndarray], np.ndarray]:
    """The lines of a GeoPackage layer as N×3 arrays, and one column. Read in chunks: the WKB of all lines at once
    would take several hundred MB."""
    lines, values = [], []
    for skip in range(0, pyogrio.read_info(path, layer=layer)["features"], CHUNK):
        _, _, geoms, (vals,) = pyogrio.raw.read(path, layer=layer, columns=[column], skip_features=skip,
                                                max_features=CHUNK)
        lines += split_coordinates(shapely.from_wkb(geoms))
        values.append(vals)
    return lines, np.concatenate(values) if values else np.array([])


def split_coordinates(geoms: np.ndarray) -> list[np.ndarray]:
    """The N×3 coordinates of each line (views into one array; empty for a missing geometry)."""
    coords, index = shapely.get_coordinates(geoms, include_z=True, return_index=True)
    return np.split(coords, np.cumsum(np.bincount(index, minlength=len(geoms)))[:-1])


def smooth_heights(xy: np.ndarray, z: np.ndarray, half_m: float = SMOOTH_M) -> np.ndarray:
    """Heights averaged over ±half_m along the line, keeping both ends. The DEM is a surface model with ~5 m of noise
    (canopy, 30 m pixels on steep slopes) that adds false ascent: on the Prättigau trails, raw heights gave 14 % more
    ascent and 10 % more hiking time than swissTLM3D; averaged over ~80 m, the median line's time is within 1 %."""
    if len(z) < 3:
        return z
    s = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(xy, axis=0).T))])
    area = np.concatenate([[0.0], np.cumsum(np.diff(s) * (z[1:] + z[:-1]) / 2)])  # integral of the profile
    lo, hi = np.maximum(s - half_m, 0), np.minimum(s + half_m, s[-1])
    out = np.where(hi > lo, (np.interp(hi, s, area) - np.interp(lo, s, area)) / np.maximum(hi - lo, 1e-9), z)
    out[0], out[-1] = z[0], z[-1]
    return out


def with_heights(lines: np.ndarray) -> list[np.ndarray]:
    """2D lines densified to ≤ STEP_M, as N×3 arrays with smoothed DEM heights."""
    coords, index = shapely.get_coordinates(shapely.segmentize(lines, STEP_M), return_index=True)
    z = dem.heights(coords[:, 0], coords[:, 1])
    if np.isnan(z).any():  # outside the DEM (none in the Alps): the nearest known height along the line
        known = np.flatnonzero(~np.isnan(z))
        z = z[known[np.clip(np.searchsorted(known, np.arange(len(z))), 0, len(known) - 1)]]
    starts = np.flatnonzero(np.diff(index, prepend=-1))
    return [np.column_stack([xy, smooth_heights(xy, zz)])
            for xy, zz in zip(np.split(coords, starts[1:]), np.split(z, starts[1:]))]


def read_nodes(pbf, needed: np.ndarray) -> dict[str, np.ndarray]:
    """LV95 locations (sorted `ids` and `xy`) of the nodes with these sorted ids, in NODE_PASSES passes over slices
    of the id range."""
    ids, lon, lat = array("q"), array("d"), array("d")
    if len(needed):
        bounds = np.searchsorted(needed, np.linspace(0, needed[-1], NODE_PASSES + 1)[1:-1])
        for part in np.array_split(needed, bounds):
            if not len(part):
                continue
            for n in osmium.FileProcessor(pbf, osmium.osm.NODE).with_filter(osmium.filter.IdFilter(part)):
                ids.append(n.id)
                loc = n.location
                lon.append(loc.lon)
                lat.append(loc.lat)
    ids, lon, lat = np.frombuffer(ids, np.int64), np.frombuffer(lon), np.frombuffer(lat)
    order = np.argsort(ids)
    e, n = WGS84_TO_LV95.transform(lon[order], lat[order])
    return {"ids": ids[order], "xy": np.column_stack([e, n]).reshape(-1, 2)}


def read_pbf(pbf, code: str) -> tuple[list[tuple[int, int, bool, np.ndarray]], dict[str, np.ndarray],
                                      list[tuple[str, np.ndarray]]]:
    """Selected ways (OSM id, category, in a route, node ids), the LV95 locations of their nodes (sorted `ids` and
    `xy`), and the member ways (role, node ids) of the country's boundary relation. Passes over the file for
    relations, ways, huts and nodes, holding only ids and the nodes needed.

    Access ways (ACCESS) are kept when their first, middle or last node is within ACCESS_M of a hut: their middle
    and end nodes are read with the others, the rest of their nodes in a second round."""
    started = time.time()
    route_ways, boundary = set(), []
    for r in osmium.FileProcessor(pbf, osmium.osm.RELATION).with_filter(osmium.filter.KeyFilter("route", "boundary")):
        if r.tags.get("route") in ("hiking", "foot"):
            route_ways.update(m.ref for m in r.members if m.type == "w")
        elif (r.tags.get("boundary") == "administrative" and r.tags.get("admin_level") == "2"
              and r.tags.get("ISO3166-1") == code):
            boundary = [(m.ref, m.role) for m in r.members if m.type == "w"]
    print(f"osm_trails {code}: {len(route_ways):,} ways in hiking routes, boundary of {len(boundary)} ways "
          f"({time.time() - started:.0f} s)", flush=True)

    ways, hut_nodes = [], array("q")
    access_ids, access_refs, access_sizes = array("q"), array("q"), array("q")
    for w in osmium.FileProcessor(pbf, osmium.osm.WAY).with_filter(osmium.filter.KeyFilter("highway", "tourism")):
        tags = w.tags
        if tags.get("tourism") in HUTS and len(w.nodes):
            hut_nodes.append(w.nodes[0].ref)  # a hut drawn as a building: one of its corners is close enough
        if "highway" not in tags:
            continue
        in_route = w.id in route_ways
        cat = category(tags, in_route)
        if cat is not None:
            ways.append((w.id, cat, in_route, np.array([n.ref for n in w.nodes], dtype=np.int64)))
        elif tags.get("highway") in ACCESS and category(tags, True) is not None:  # walkable
            access_ids.append(w.id)
            access_refs.extend(n.ref for n in w.nodes)
            access_sizes.append(len(w.nodes))
    roles = dict(boundary)
    border_ways = [(roles[w.id], np.array([n.ref for n in w.nodes], dtype=np.int64)) for w in
                   osmium.FileProcessor(pbf, osmium.osm.WAY).with_filter(osmium.filter.IdFilter(list(roles)))]
    hut_lonlat = [(n.location.lon, n.location.lat) for n in
                  osmium.FileProcessor(pbf, osmium.osm.NODE).with_filter(osmium.filter.KeyFilter("tourism"))
                  if n.tags.get("tourism") in HUTS]
    print(f"osm_trails {code}: {len(ways):,} ways selected, {len(access_ids):,} access ways to check, "
          f"{len(hut_nodes) + len(hut_lonlat):,} huts ({time.time() - started:.0f} s)", flush=True)

    access_refs, access_sizes = np.frombuffer(access_refs, np.int64), np.frombuffer(access_sizes, np.int64)
    offsets = np.concatenate([[0], np.cumsum(access_sizes)]).astype(np.int64)
    samples = access_refs[np.column_stack([offsets[:-1], offsets[:-1] + access_sizes // 2, offsets[1:] - 1])]
    hut_nodes = np.frombuffer(hut_nodes, np.int64)
    nodes = read_nodes(pbf, np.unique(np.concatenate([w[3] for w in ways] + [refs for _, refs in border_ways]
                                                     + [samples.ravel(), hut_nodes])))
    huts = np.concatenate([locate(nodes, hut_nodes),
                           np.column_stack(WGS84_TO_LV95.transform(*np.array(hut_lonlat).reshape(-1, 2).T))])
    if len(huts) and len(samples):
        dist, _ = cKDTree(huts).query(locate(nodes, samples.ravel()), distance_upper_bound=ACCESS_M)
        near = np.flatnonzero(np.isfinite(dist).reshape(-1, 3).any(axis=1))
        ways += [(access_ids[k], ACCESS_CATEGORY, False, access_refs[offsets[k]:offsets[k + 1]].copy()) for k in near]
        del access_refs
        more = np.setdiff1d(np.concatenate([w[3] for w in ways[-len(near):]]), nodes["ids"]) if len(near) else []
        extra = read_nodes(pbf, np.asarray(more, dtype=np.int64))
        order = np.argsort(np.concatenate([nodes["ids"], extra["ids"]]))
        nodes = {"ids": np.concatenate([nodes["ids"], extra["ids"]])[order],
                 "xy": np.concatenate([nodes["xy"], extra["xy"]])[order]}
        print(f"osm_trails {code}: {len(near):,} access ways near huts", flush=True)
    print(f"osm_trails {code}: {len(nodes['ids']):,} nodes ({time.time() - started:.0f} s)", flush=True)
    return ways, nodes, border_ways


def locate(nodes: dict, refs: np.ndarray) -> np.ndarray:
    """LV95 points of node ids (each must be in `nodes`)."""
    return nodes["xy"][np.searchsorted(nodes["ids"], refs)]


def border_polygon(border_ways: list[tuple[str, np.ndarray]], nodes: dict) -> shapely.Geometry:
    """The country's area from its boundary relation: outer rings minus inner rings, simplified like countries.json."""
    def rings(role):
        lines = [shapely.linestrings(locate(nodes, refs)) for r, refs in border_ways if r == role and len(refs) > 1]
        return shapely.union_all(shapely.get_parts(shapely.polygonize(lines))) if lines else shapely.Polygon()

    return shapely.simplify(shapely.difference(rings("outer"), rings("inner")), SIMPLIFY_BORDER_M)


def region_border(code: str) -> shapely.Geometry:
    """The country's part of a region whose extract holds only part of it (so not the whole boundary relation): the
    region's area outside Switzerland, Liechtenstein and the countries whose border is known (Austria)."""
    known = [shape for c, shape in countries.borders().items() if c != code]
    return shapely.simplify(shapely.difference(regions.area(code), shapely.union_all(known)), SIMPLIFY_BORDER_M)


def extract(code: str) -> dict[int, float]:
    """Trail file and border of one region from its extract. Returns km of trail per category.

    The trails are clipped, given heights and written in chunks of CHUNK pieces, sorted by DEM tile so that each
    chunk reads few tiles: all of Austria at once would take over 1.5 GB."""
    started = time.time()
    area, swiss = regions.own_area(code), swiss_area()
    if area is None or swiss is None:
        raise RuntimeError("osm_trails: needs the region polygon (regions.download_polygons) and data/countries.json")
    ways, nodes, border_ways = read_pbf(pbf_path(code), code)
    border = border_polygon(border_ways, nodes) if regions.REGIONS[code].whole_country else region_border(code)
    refs = [w[3] for w in ways]
    osm_ids, cats, in_route = (np.array(col) for col in list(zip(*[w[:3] for w in ways])))
    del ways, border_ways

    pieces = np.array(split_at_junctions(refs), dtype=np.int64).reshape(-1, 3)  # way, first, last vertex
    sizes = pieces[:, 2] - pieces[:, 1] + 1
    offsets = np.concatenate([[0], np.cumsum(sizes)])  # piece i's points: xy[offsets[i]:offsets[i + 1]]
    xy = locate(nodes, np.concatenate([refs[w][a:b + 1] for w, a, b in pieces]))
    del refs, nodes
    lon, lat = dem.LV95_TO_WGS84.transform(*xy[offsets[:-1]].T)
    order = np.lexsort((np.floor(lon), np.floor(lat)))
    replaced = shapely.difference(area, swiss)
    print(f"osm_trails {code}: {len(pieces):,} pieces ({time.time() - started:.0f} s)", flush=True)

    path = trails_path(code)
    part = path.with_suffix(".part.gpkg")
    part.unlink(missing_ok=True)
    km, n_lines = np.zeros(UNOFFICIAL + 1), 0
    for chunk in np.array_split(order, max(1, len(order) // CHUNK)):
        point = np.repeat(offsets[chunk] - np.concatenate([[0], np.cumsum(sizes[chunk])[:-1]]), sizes[chunk])
        lines = shapely.linestrings(xy[point + np.arange(len(point))], indices=np.repeat(np.arange(len(chunk)),
                                                                                           sizes[chunk]))
        index, lines = clip(lines, replaced, inside=True)
        if not len(lines):
            continue
        way = pieces[chunk[index], 0]
        coords = with_heights(lines)
        geoms = shapely.linestrings(np.concatenate(coords), indices=np.repeat(np.arange(len(coords)),
                                                                              [len(c) for c in coords]))
        pyogrio.raw.write(part, shapely.to_wkb(geoms), field_data=[cats[way].astype(np.int8), osm_ids[way],
                                                                     in_route[way].astype(np.int8)],
                          fields=["category", "osm_id", "route"], layer="trails", driver="GPKG",
                          geometry_type="LineString Z", crs="EPSG:2056", append=part.exists())
        np.add.at(km, cats[way], shapely.length(lines) / 1000)
        n_lines += len(lines)
    part.rename(path)

    countries.region_border_path(code).write_text(json.dumps(
        {"source": pbf_path(code).name, "crs": "EPSG:2056", "countries": {code: shapely.to_geojson(border)}}))
    km = {c: round(float(km[c])) for c in range(1, UNOFFICIAL + 1)}
    print(f"osm_trails {code}: {n_lines:,} lines outside CH/LI, km per category {km}, border "
          f"{border.area / 1e6:,.0f} km² ({time.time() - started:.0f} s)", flush=True)
    return km


def download(code: str) -> dict:
    """Download the region's extract (to a temporary file, then renamed). Returns its edition."""
    href = regions.REGIONS[code].pbf
    path, part = pbf_path(code), pbf_path(code).with_suffix(".part")
    print(f"osm_trails {code}: downloading {href}", flush=True)
    with httpx.stream("GET", href, headers={"User-Agent": USER_AGENT}, timeout=300, follow_redirects=True) as r:
        r.raise_for_status()
        with part.open("wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
        edition = {"href": href, "last_modified": r.headers.get("last-modified"),
                   "size": r.headers.get("content-length")}
    part.rename(path)
    return edition


def refresh(force: bool = False) -> bool:
    """Download each region's extract when missing or older than MAX_AGE_DAYS, and extract trails and border from
    it when it is not the one the trail file was made from. True if a trail file changed."""
    regions.download_polygons()
    changed = False
    for code in regions.REGIONS:
        path = pbf_path(code)
        known = json.loads(edition_path(code).read_text()) if edition_path(code).exists() else None
        age_days = (time.time() - path.stat().st_mtime) / 86400 if path.exists() else None
        if force or age_days is None or age_days > MAX_AGE_DAYS:
            edition = download(code)
        elif known and known["size"] == str(path.stat().st_size):
            edition = known
        else:  # downloaded by hand: identified by its file date and size
            modified = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(timespec="seconds")
            edition = {"href": regions.REGIONS[code].pbf, "last_modified": modified,
                       "size": str(path.stat().st_size)}
        missing = not trails_path(code).exists() or not countries.region_border_path(code).exists()
        if force or edition != known or missing:
            extract(code)
            edition_path(code).write_text(json.dumps(edition, indent=1))
            changed = True
        else:
            print(f"osm_trails {code}: up to date ({edition['last_modified']})")
    return changed


def editions() -> dict[str, dict]:
    """The extract each trail file was made from: what the route graph depends on."""
    return {code: json.loads(edition_path(code).read_text()) for code in regions.REGIONS if edition_path(code).exists()}


def load() -> tuple[list[np.ndarray], list[int], shapely.Geometry | None]:
    """The trail lines (N×3, LV95) and categories of all regions with a trail file, and the area they replace
    swissTLM3D in (the regions' areas outside Switzerland and Liechtenstein); ([], [], None) without any."""
    swiss = swiss_area()
    found = [code for code in regions.REGIONS if trails_path(code).exists() and regions.area(code) is not None]
    if not found or swiss is None:
        return [], [], None
    lines, categories = [], []
    for code in found:
        more, cats = read_lines(trails_path(code), "category")
        lines += more
        categories += cats.astype(int).tolist()
    return lines, categories, shapely.difference(shapely.union_all([regions.area(c) for c in found]), swiss)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="download and extract again")
    refresh(parser.parse_args().force)


if __name__ == "__main__":
    main()
