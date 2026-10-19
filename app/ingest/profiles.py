"""Elevation profiles for edges stored before build_edges computed them (new builds include them).

    uv run python -m app.ingest.profiles                                  # fill the local database (needs data/tlm)
    uv run python -m app.ingest.profiles --export data/edge_profiles.json # the same, for another copy of the database
    python -m app.ingest.profiles --import data/edge_profiles.json        # on server2, which has no swissTLM3D

The export is keyed by a hash of each route line, so it applies to any database whose edges come from the same
build, whatever their ids.
"""

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from app.db import connect
from app.ingest import build_edges


def key(geometry: str) -> str:
    return hashlib.sha1(geometry.encode()).hexdigest()[:16]


def compute(conn: sqlite3.Connection) -> dict[str, str]:
    """Profile of every edge without one, by route line hash."""
    rows = conn.execute("SELECT DISTINCT geometry FROM edge WHERE elevation IS NULL AND geometry IS NOT NULL").fetchall()
    if not rows:
        return {}
    heights = build_edges.Heights(build_edges.load_network())
    return {key(r["geometry"]): heights.profile(r["geometry"]) for r in rows}


def apply(conn: sqlite3.Connection, profiles: dict[str, str]) -> int:
    """Store profiles on the edges whose route line they belong to. Returns the number of edges updated."""
    rows = conn.execute("SELECT id, geometry FROM edge WHERE elevation IS NULL AND geometry IS NOT NULL").fetchall()
    updates = [(profiles[k], r["id"]) for r in rows if (k := key(r["geometry"])) in profiles]
    with conn:
        conn.executemany("UPDATE edge SET elevation = ? WHERE id = ?", updates)
    return len(updates)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--export", type=Path, help="write the profiles to this file instead of the database")
    group.add_argument("--import", dest="import_", type=Path, help="read profiles from an export")
    args = parser.parse_args()
    conn = connect()
    if args.import_:
        print(f"updated {apply(conn, json.loads(args.import_.read_text()))} edges")
        return
    profiles = compute(conn)
    if args.export:
        args.export.write_text(json.dumps(profiles))
        print(f"wrote {len(profiles)} profiles to {args.export}")
    else:
        print(f"updated {apply(conn, profiles)} edges")


if __name__ == "__main__":
    main()
