"""Edges of the place graph: connections of a hut, and manual review (exclude / note)."""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.huts import get_db

router = APIRouter(prefix="/api", tags=["edges"])

# One edge per (from, to) for the requested category limit: the most permissive stored limit ≤ the
# request wins (it is only stored when it differs), and manual edges win over computed ones.
BEST_EDGES = """
SELECT * FROM (
  SELECT e.*, ROW_NUMBER() OVER (
    PARTITION BY e.from_place, e.to_place
    ORDER BY e.source = 'manual' DESC, e.category_limit DESC) AS rn
  FROM edge e
  WHERE e.category_limit <= :limit AND (e.from_place = :hut OR e.to_place = :hut)
) WHERE rn = 1
"""


@router.get("/huts/{hut_id}/edges")
def hut_edges(hut_id: int, limit: int = Query(2, ge=1, le=3, description="max trail category: 1 yellow, "
              "2 red-white, 3 blue-white"), conn: sqlite3.Connection = Depends(get_db)) -> dict:
    if not conn.execute("SELECT 1 FROM hut WHERE place_id = ?", (hut_id,)).fetchone():
        raise HTTPException(404, "hut not found")
    rows = conn.execute(
        f"""SELECT b.*, p.name, p.kind, p.lat, p.lon, p.altitude_m, s.mode
            FROM ({BEST_EDGES}) b
            JOIN place p ON p.id = CASE WHEN b.from_place = :hut THEN b.to_place ELSE b.from_place END
            LEFT JOIN stop s ON s.place_id = p.id
            ORDER BY b.time_min""",
        {"hut": hut_id, "limit": limit},
    ).fetchall()

    def item(r: sqlite3.Row) -> dict:
        return {
            "id": r["id"],
            "place": {"id": r["from_place"] if r["to_place"] == hut_id else r["to_place"], "name": r["name"],
                      "kind": r["kind"], "mode": r["mode"], "altitude": r["altitude_m"], "lat": r["lat"],
                      "lon": r["lon"]},
            **{k: r[k] for k in ("time_min", "distance_m", "ascent_m", "descent_m", "max_category", "category_limit",
                                 "sac_route_id", "sac_time_min", "sac_grade", "geometry", "note", "source")},
            "excluded": bool(r["excluded"]),
        }

    huts = [item(r) for r in rows if r["from_place"] == hut_id and r["kind"] == "hut"]
    stops: dict[int, dict] = {}
    for r in rows:
        if r["kind"] != "stop":
            continue
        direction = "to_hut" if r["to_place"] == hut_id else "from_hut"
        e = item(r)
        entry = stops.setdefault(e["place"]["id"], {"place": e["place"], "to_hut": None, "from_hut": None})
        entry[direction] = {k: v for k, v in e.items() if k != "place"}
    stop_list = sorted(stops.values(), key=lambda s: min(
        (s[d]["time_min"] for d in ("to_hut", "from_hut") if s[d]), default=9999))
    return {"limit": limit, "huts": huts, "stops": stop_list}


class EdgeReview(BaseModel):
    excluded: bool | None = None
    note: str | None = None


@router.patch("/edges/{edge_id}")
def review_edge(edge_id: int, review: EdgeReview, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    changes = review.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(400, "nothing to change")
    with conn:
        updated = conn.execute(
            f"UPDATE edge SET {', '.join(f'{k} = :{k}' for k in changes)} WHERE id = :id",
            {k: (int(v) if k == "excluded" else v) for k, v in changes.items()} | {"id": edge_id},
        ).rowcount
    if not updated:
        raise HTTPException(404, "edge not found")
    row = conn.execute("SELECT id, excluded, note FROM edge WHERE id = ?", (edge_id,)).fetchone()
    return {"id": row["id"], "excluded": bool(row["excluded"]), "note": row["note"]}
