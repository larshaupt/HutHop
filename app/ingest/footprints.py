"""What the tour search needs from each route line, for edges stored without it (edges built before build_edges
computed it, and edges added by hand): the footprint (grid cells and shape samples, see planner/geometry.py) and the
highest point (from the height profile). Needs no trail data; refresh runs it every time.

    uv run python -m app.ingest.footprints
"""

import sqlite3

from app.db import connect
from app.planner import geometry, polyline


def fill(conn: sqlite3.Connection) -> int:
    """Store a footprint and highest point on every edge with a route line but without them. Returns the number of
    edges updated."""
    rows = conn.execute("SELECT id, geometry FROM edge WHERE footprint IS NULL AND geometry IS NOT NULL").fetchall()
    tops = conn.execute("SELECT id, elevation FROM edge WHERE max_altitude_m IS NULL AND elevation IS NOT NULL").fetchall()
    with conn:
        conn.executemany("UPDATE edge SET footprint = ?, samples = ? WHERE id = ?",
                         [(*geometry.pack(*geometry.footprint(r["geometry"])), r["id"]) for r in rows])
        conn.executemany("UPDATE edge SET max_altitude_m = ? WHERE id = ?",
                         [(max(polyline.decode_values(r["elevation"]), default=None), r["id"]) for r in tops])
    return len({r["id"] for r in rows} | {r["id"] for r in tops})


if __name__ == "__main__":
    print(f"updated {fill(connect())} edges")
