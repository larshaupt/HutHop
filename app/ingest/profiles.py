"""Height profiles for edges stored before build_edges computed them (new builds include them).

    uv run python -m app.ingest.profiles     # needs the swissTLM3D files in data/tlm (refresh downloads them)
"""

import sqlite3

from app.db import connect
from app.ingest import build_edges


def fill(conn: sqlite3.Connection) -> int:
    """Store a profile on every edge without one. Returns the number of edges updated."""
    rows = conn.execute("SELECT id, geometry FROM edge WHERE elevation IS NULL AND geometry IS NOT NULL").fetchall()
    if not rows:
        return 0
    heights = build_edges.Heights(build_edges.load_network())
    with conn:
        conn.executemany("UPDATE edge SET elevation = ? WHERE id = ?",
                         [(heights.profile(r["geometry"]), r["id"]) for r in rows])
    return len(rows)


if __name__ == "__main__":
    print(f"updated {fill(connect())} edges")
