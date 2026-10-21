"""Route footprints (grid cells and shape samples, see planner/geometry.py) for edges stored without them: edges
built before build_edges computed them, and edges added by hand. Needs no trail data; refresh runs it every time.

    uv run python -m app.ingest.footprints
"""

import sqlite3

from app.db import connect
from app.planner import geometry


def fill(conn: sqlite3.Connection) -> int:
    """Store a footprint on every edge with a route line but none. Returns the number of edges updated."""
    rows = conn.execute("SELECT id, geometry FROM edge WHERE footprint IS NULL AND geometry IS NOT NULL").fetchall()
    with conn:
        conn.executemany("UPDATE edge SET footprint = ?, samples = ? WHERE id = ?",
                         [(*geometry.pack(*geometry.footprint(r["geometry"])), r["id"]) for r in rows])
    return len(rows)


if __name__ == "__main__":
    print(f"updated {fill(connect())} edges")
