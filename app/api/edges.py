"""Edges of the place graph: connections of a hut, and manual review (exclude / note)."""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.admin import require_admin
from app.api.deps import get_db
from app.ingest import osm_access
from app.planner.tours import is_lift

router = APIRouter(prefix="/api", tags=["edges"])

# One edge per (from, to) for the requested category limit and altitude: the most permissive stored limit ≤ the
# request wins (it is only stored when it differs), and manual edges win over computed ones.
BEST_EDGES = """
SELECT * FROM (
  SELECT e.*, ROW_NUMBER() OVER (
    PARTITION BY e.from_place, e.to_place
    ORDER BY e.source = 'manual' DESC, e.category_limit DESC) AS rn
  FROM edge e
  WHERE e.category_limit <= :limit AND (e.from_place = :hut OR e.to_place = :hut)
    AND (:max_altitude IS NULL OR e.max_altitude_m IS NULL OR e.max_altitude_m <= :max_altitude)
) WHERE rn = 1
"""


@router.get("/huts/{hut_id}/edges")
def hut_edges(hut_id: int, limit: int = Query(3, ge=1, le=4, description="max trail category: 1 yellow, "
              "2 red-white, 3 blue-white, 4 also unofficial OSM paths"),
              max_altitude: int | None = Query(None, ge=500, le=5000, description="highest point of a route (m)"),
              avoid_lifts: bool = Query(False, description="no stops served only by mountain lifts"),
              pace: float = Query(1.0, ge=0.5, le=2.0, description="walking time / Swiss standard time"),
              conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """A hut's connections within the general settings (trail level, altitude, lifts), times at your pace."""
    if not conn.execute("SELECT 1 FROM hut WHERE place_id = ?", (hut_id,)).fetchone():
        raise HTTPException(404, "hut not found")
    rows = conn.execute(
        f"""SELECT b.*, p.name, p.kind, p.lat, p.lon, p.altitude_m, s.mode
            FROM ({BEST_EDGES}) b
            JOIN place p ON p.id = CASE WHEN b.from_place = :hut THEN b.to_place ELSE b.from_place END
            LEFT JOIN stop s ON s.place_id = p.id
            ORDER BY b.time_min""",
        {"hut": hut_id, "limit": limit, "max_altitude": max_altitude},
    ).fetchall()

    grades = osm_access.grades()

    def item(r: sqlite3.Row) -> dict:
        return {
            "id": r["id"],
            "place": {"id": r["from_place"] if r["to_place"] == hut_id else r["to_place"], "name": r["name"],
                      "kind": r["kind"], "mode": r["mode"], "altitude": r["altitude_m"], "lat": r["lat"],
                      "lon": r["lon"]},
            **{k: r[k] for k in ("distance_m", "ascent_m", "descent_m", "max_category", "category_limit",
                                 "max_altitude_m", "sac_route_id", "sac_time_min", "sac_grade", "geometry", "note",
                                 "source")},
            "osm_grade": osm_access.edge_grade(grades, r["max_category"], r["from_place"], r["to_place"]),
            "time_min": round(r["time_min"] * pace),
            "excluded": bool(r["excluded"]),
        }

    huts = [item(r) for r in rows if r["from_place"] == hut_id and r["kind"] == "hut"]
    stops: dict[int, dict] = {}
    for r in rows:
        if r["kind"] != "stop" or (avoid_lifts and is_lift(r["mode"])):
            continue
        direction = "to_hut" if r["to_place"] == hut_id else "from_hut"
        e = item(r)
        entry = stops.setdefault(e["place"]["id"], {"place": e["place"], "to_hut": None, "from_hut": None})
        entry[direction] = {k: v for k, v in e.items() if k != "place"}
    if stops:
        ids = list(stops)
        for r in conn.execute(f"SELECT stop_place, label, departures FROM stop_line WHERE departures > 0 AND "
                              f"stop_place IN ({','.join('?' * len(ids))}) ORDER BY departures DESC", ids):
            place = stops[r["stop_place"]]["place"]
            place.setdefault("lines", []).append(r["label"])
            place["departures"] = round(place.get("departures", 0) + r["departures"])
    stop_list = sorted(stops.values(), key=lambda s: min(
        (s[d]["time_min"] for d in ("to_hut", "from_hut") if s[d]), default=9999))
    return {"limit": limit, "huts": huts, "stops": stop_list}


class EdgeReview(BaseModel):
    excluded: bool = False  # only applied when sent (exclude_unset); null is refused
    note: str | None = None


@router.patch("/edges/{edge_id}", dependencies=[Depends(require_admin)])
def review_edge(edge_id: int, review: EdgeReview, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    changes = review.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(400, "nothing to change")
    with conn:  # every trail-limit variant of the connection, or the planner keeps using the easier one
        updated = conn.execute(
            f"UPDATE edge SET {', '.join(f'{k} = :{k}' for k in changes)} "
            "WHERE (from_place, to_place) = (SELECT from_place, to_place FROM edge WHERE id = :id)",
            {k: (int(v) if k == "excluded" else v) for k, v in changes.items()} | {"id": edge_id},
        ).rowcount
    if not updated:
        raise HTTPException(404, "edge not found")
    row = conn.execute("SELECT id, excluded, note FROM edge WHERE id = ?", (edge_id,)).fetchone()
    return {"id": row["id"], "excluded": bool(row["excluded"]), "note": row["note"]}


@router.get("/osm-paths")
def osm_paths() -> dict:
    """The unofficial access paths (OpenStreetMap, see osm_access) as GeoJSON lines, for the map's trail overlay."""
    features = []
    for hut, path in osm_access.load().items():
        lon, lat = osm_access.LV95_TO_WGS84.transform(path["coords"][:, 0], path["coords"][:, 1])
        features.append({"type": "Feature", "properties": {"hut": hut, "grade": path["grade"]},
                         "geometry": {"type": "LineString", "coordinates": [[round(x, 5), round(y, 5)]
                                                                            for x, y in zip(lon, lat)]}})
    return {"type": "FeatureCollection", "features": features}
