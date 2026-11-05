"""Access paths from OpenStreetMap for huts far from the signposted trails (glacier and high-alpine huts, huts abroad).

    uv run python -m app.ingest.osm_access            # fetch if the set of far huts changed
    uv run python -m app.ingest.osm_access --force

For each hut more than build_edges.HUT_MAX_SNAP_M from a signposted swissTLM3D trail (`place.snap_m`, from the last
build), the shortest OSM path from the hut to the end of a signposted trail line. build_edges adds these as trail
lines of category 4 ("unofficial path"), offered only with that trail limit. OSM has no heights: they come from
the Open-Meteo elevation API (Copernicus DEM, ~90 m). Saved to data/osm/access.json.

Data © OpenStreetMap contributors (ODbL), fetched with the Overpass API.
"""

import argparse
import json
import sqlite3
import time
from datetime import UTC, datetime

import httpx
import numpy as np
import pyogrio.raw
import shapely
from pyproj import Transformer
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

from app.db import ROOT, connect

OVERPASS = "https://overpass-api.de/api/interpreter"
ELEVATION = "https://api.open-meteo.com/v1/elevation"
USER_AGENT = "HutHop/0.1 (private hut planner)"
FILE = ROOT / "data/osm/access.json"
SEARCH_RADIUS_M = 7000  # OSM paths fetched around each hut
HUT_SNAP_M = 150.0  # hut to the nearest OSM path node
TRAIL_END_M = 30.0  # OSM node to the end of a signposted trail line: where the access path joins
MAX_PATH_M = 15_000.0
HEIGHT_STEP_M = 90.0  # the DEM's resolution; finer points add nothing
HEIGHTS_PER_REQUEST = 100  # Open-Meteo's limit
GRADE = {"hiking": 1, "mountain_hiking": 2, "demanding_mountain_hiking": 3, "alpine_hiking": 4,
         "demanding_alpine_hiking": 5, "difficult_alpine_hiking": 6}
WGS84_TO_LV95 = Transformer.from_crs("EPSG:4326", "EPSG:2056", always_xy=True)
LV95_TO_WGS84 = Transformer.from_crs("EPSG:2056", "EPSG:4326", always_xy=True)


def far_huts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    from app.ingest.build_edges import HUT_MAX_SNAP_M  # build_edges imports this module

    return conn.execute("SELECT id, name, lat, lon, e_lv95, n_lv95 FROM place WHERE kind = 'hut' AND snap_m > ? "
                        "ORDER BY id", (HUT_MAX_SNAP_M,)).fetchall()


def fetch_paths(huts: list[sqlite3.Row]) -> list[dict]:
    """OSM footpaths around the huts: ways (with tags and node ids) and their nodes."""
    around = "".join(f'way["highway"~"^(path|footway|track|steps)$"](around:{SEARCH_RADIUS_M},{h["lat"]},{h["lon"]});'
                     for h in huts)
    query = f"[out:json][timeout:300];({around});out body;>;out skel qt;"
    r = httpx.post(OVERPASS, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=330)
    return r.raise_for_status().json()["elements"]


def trail_ends(bounds: tuple[float, float, float, float]) -> np.ndarray:
    """End points (LV95) of the signposted trail lines in the bounds: the trail network's nodes."""
    from app.ingest.build_edges import GPKG

    _, _, geoms, _ = pyogrio.raw.read(GPKG, layer="tlm_strassen_strasse", columns=[], bbox=bounds)
    lines = shapely.from_wkb(geoms)
    return np.concatenate([shapely.get_coordinates(shapely.get_point(lines, i)) for i in (0, -1)])


def access_paths(elements: list[dict], huts: list[tuple[int, float, float]], ends: np.ndarray) -> dict[int, dict]:
    """Shortest OSM path (LV95 points, hut first) from each hut (id, e, n) to a trail end, with its hardest
    `sac_scale` as T1–T6 (None when no way along it is graded). Huts without one are left out."""
    nodes = {e["id"]: (e["lon"], e["lat"]) for e in elements if e["type"] == "node"}
    ids = list(nodes)
    if not ids:
        return {}
    pos = {k: i for i, k in enumerate(ids)}
    xy = np.column_stack(WGS84_TO_LV95.transform(*np.array([nodes[k] for k in ids]).T))
    src, dst, length, grade = [], [], [], []
    for e in elements:
        tags = e.get("tags", {})
        if e["type"] != "way" or tags.get("access") in ("no", "private"):
            continue
        nd = [pos[n] for n in e["nodes"] if n in pos]
        a, b = np.array(nd[:-1]), np.array(nd[1:])
        d = np.hypot(*(xy[a] - xy[b]).T) + 1e-6  # csgraph drops zero weights
        g = GRADE.get(tags.get("sac_scale"), 0)
        src += [a, b]
        dst += [b, a]
        length += [d, d]
        grade += [np.full(len(a), g)] * 2
    if not src:  # every way closed
        return {}
    src, dst, length, grade = map(np.concatenate, (src, dst, length, grade))
    n = len(ids)
    # two ways over the same node pair: csr_matrix would add them up, so keep one
    _, first = np.unique(src.astype(np.int64) * n + dst, return_index=True)
    src, dst, length, grade = src[first], dst[first], length[first], grade[first]
    graph = csr_matrix((length, (src, dst)), shape=(n, n))
    grade_of = dict(zip(zip(src.tolist(), dst.tolist()), grade.tolist()))

    tree = shapely.STRtree(shapely.points(ends))
    joins = np.zeros(n, bool)
    joins[np.unique(tree.query(shapely.points(xy), predicate="dwithin", distance=TRAIL_END_M)[0])] = True
    hut_xy = np.array([(e, n_) for _, e, n_ in huts])
    (_, start), snap_d = shapely.STRtree(shapely.points(xy)).query_nearest(
        shapely.points(hut_xy), return_distance=True, all_matches=False)
    paths = {}
    for (hut_id, *_), s, d, h_xy in zip(huts, start, snap_d, hut_xy):
        if d > HUT_SNAP_M:
            continue
        dist, pred = dijkstra(graph, indices=int(s), return_predecessors=True, limit=MAX_PATH_M)
        reach = np.flatnonzero(joins & np.isfinite(dist))
        if not len(reach):
            continue
        path = [int(reach[np.argmin(dist[reach])])]
        while path[-1] != s:
            path.append(int(pred[path[-1]]))
        path.reverse()
        hardest = max((grade_of[(a, b)] for a, b in zip(path, path[1:])), default=0)
        paths[hut_id] = {"xy": np.vstack([h_xy, xy[path]]), "grade": f"T{hardest}" if hardest else None}
    return paths


def with_heights(xy: np.ndarray) -> np.ndarray:
    """The line resampled to ~HEIGHT_STEP_M with terrain heights (N×3, LV95 metres)."""
    line = shapely.segmentize(shapely.simplify(shapely.linestrings(xy), 5.0), HEIGHT_STEP_M)
    pts = shapely.get_coordinates(line)
    lon, lat = LV95_TO_WGS84.transform(*pts.T)
    z = []
    for i in range(0, len(pts), HEIGHTS_PER_REQUEST):
        params = {"latitude": ",".join(f"{v:.5f}" for v in lat[i:i + HEIGHTS_PER_REQUEST]),
                  "longitude": ",".join(f"{v:.5f}" for v in lon[i:i + HEIGHTS_PER_REQUEST])}
        for attempt in range(6):  # the free API limits requests per minute
            r = httpx.get(ELEVATION, params=params, headers={"User-Agent": USER_AGENT}, timeout=30)
            if r.status_code != 429 or attempt == 5:
                break
            time.sleep(15 * (attempt + 1))
        z += r.raise_for_status().json()["elevation"]
    return np.column_stack([pts, z])


def refresh(conn: sqlite3.Connection, force: bool = False) -> int | str:
    """Fetch access paths when the set of far huts changed (or forced). Returns the number of paths saved."""
    # shortcut: OSM edits are only picked up when the set of far huts changes or with --force; refetch on a
    # schedule if the paths turn out to change often
    huts = far_huts(conn)
    known = json.loads(FILE.read_text()) if FILE.exists() else None
    if known and known["huts"] == [h["id"] for h in huts] and not force:
        return "unchanged"
    paths = {}
    if huts:
        elements = fetch_paths(huts)
        xy = np.array([(h["e_lv95"], h["n_lv95"]) for h in huts])
        bounds = (*(xy.min(0) - SEARCH_RADIUS_M), *(xy.max(0) + SEARCH_RADIUS_M))
        found = access_paths(elements, [(h["id"], h["e_lv95"], h["n_lv95"]) for h in huts], trail_ends(bounds))
        paths = {str(k): {"grade": p["grade"], "coords": np.round(with_heights(p["xy"]), 1).tolist()}
                 for k, p in found.items()}
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps({"fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
                                "huts": [h["id"] for h in huts], "paths": paths}))
    print(f"osm access: {len(paths)} of {len(huts)} far huts have a path to a signposted trail")
    return len(paths)


def load() -> dict[int, dict]:
    """Saved access paths per hut id: {"coords": N×3 LV95 array, "grade": "T5" or None}."""
    if not FILE.exists():
        return {}
    return {int(k): {"coords": np.array(p["coords"]), "grade": p["grade"]}
            for k, p in json.loads(FILE.read_text())["paths"].items()}


def grades() -> dict[int, str | None]:
    """T-grade of each hut's access path, by hut id (None: no way along it has a sac_scale)."""
    if not FILE.exists():
        return {}
    return {int(k): p["grade"] for k, p in json.loads(FILE.read_text())["paths"].items()}


def edge_grade(grades: dict[int, str | None], max_category: int, a: int, b: int) -> str | None:
    """Hardest grade on an unofficial connection: an access path is a dead end at its hut, so it is the end huts'."""
    if max_category < 4:
        return None
    return max((g for g in (grades.get(a), grades.get(b)) if g), default=None)  # "T1" < … < "T6"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="fetch even if the set of far huts is unchanged")
    print(refresh(connect(), parser.parse_args().force))


if __name__ == "__main__":
    main()
