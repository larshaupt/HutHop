"""Refresh all source data and rebuild the route graph when something relevant changed.

    uv run python -m app.ingest.refresh               # huts, stops, timetable; trails only if swisstopo has a new one
    uv run python -m app.ingest.refresh --rebuild     # also rebuild the edges even if nothing moved
    uv run python -m app.ingest.refresh --skip-stops --skip-timetable --skip-trails

Suggested rhythm: weekly (SAC opening data changes during the season); swisstopo publishes the trail network
about once a year, the timetable weekly (downloaded only when a new edition is out). Each run is appended to
data/refresh_log.json. Manual exclusions and overrides survive.
"""

import argparse
import hashlib
import io
import json
import sqlite3
import time
import zipfile
from datetime import UTC, datetime

import httpx

from app.db import ROOT, connect
from app.ingest import build_edges, footprints, load_stops, load_timetable, sync_sac

TLM_STAC_ITEMS = "https://data.geo.admin.ch/api/stac/v1/collections/ch.swisstopo.swisstlm3d-wanderwege/items"
TLM_DIR = ROOT / "data/tlm"
TLM_EDITION = TLM_DIR / "edition.json"
LOG = ROOT / "data/refresh_log.json"
USER_AGENT = "HutHop/0.1 (private hut planner)"


def positions_fingerprint(conn: sqlite3.Connection, kind: str) -> str:
    """Hash of all place ids and positions (to the metre) of one kind; edges depend on nothing else."""
    rows = conn.execute("SELECT id, round(e_lv95), round(n_lv95) FROM place WHERE kind = ? ORDER BY id", (kind,))
    return hashlib.sha256(json.dumps([tuple(r) for r in rows]).encode()).hexdigest()


def refresh_trails(force: bool = False) -> bool:
    """Download the swissTLM3D hiking network if swisstopo published a new edition. Returns True if updated."""
    client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=120, follow_redirects=True)
    items = client.get(TLM_STAC_ITEMS).raise_for_status().json()["features"]
    href = next(a["href"] for item in items for a in item["assets"].values() if a["href"].endswith(".gpkg.zip"))
    head = client.head(href).raise_for_status().headers
    edition = {"href": href, "last_modified": head.get("last-modified"), "size": head.get("content-length")}
    known = json.loads(TLM_EDITION.read_text()) if TLM_EDITION.exists() else None
    has_file = any(TLM_DIR.glob("*.gpkg"))
    if known == edition and has_file and not force:
        print(f"trails: up to date ({edition['last_modified']})")
        return False
    print(f"trails: downloading {href} ({int(edition['size'] or 0) / 1e6:.0f} MB)")
    archive = client.get(href).raise_for_status().content
    TLM_DIR.mkdir(parents=True, exist_ok=True)
    zipfile.ZipFile(io.BytesIO(archive)).extractall(TLM_DIR)
    TLM_EDITION.write_text(json.dumps(edition, indent=1))
    return True


def run(skip_huts: bool = False, skip_stops: bool = False, skip_timetable: bool = False, skip_trails: bool = False,
        rebuild: bool = False) -> dict:
    started = time.time()
    conn = connect()
    report: dict = {"started_at": datetime.now(UTC).isoformat(timespec="seconds")}

    if not skip_huts:
        before = positions_fingerprint(conn, "hut")
        report["huts"] = sync_sac.sync(conn, sync_sac.fetch_and_save())
        report["huts_moved"] = positions_fingerprint(conn, "hut") != before
    if not skip_stops:
        before = positions_fingerprint(conn, "stop")
        load_stops.download()
        report["stops"] = load_stops.load(conn, load_stops.read_stops())
        report["stops_moved"] = positions_fingerprint(conn, "stop") != before
    if not skip_timetable:
        # Loaded every time (~30 s, from the local file) so new stops get their lines too.
        before = load_timetable.lines_fingerprint(conn)
        report["timetable_updated"] = load_timetable.download()
        report["stop_lines"] = load_timetable.load(conn, load_timetable.read_all())
        report["lines_changed"] = load_timetable.lines_fingerprint(conn) != before
    if not skip_trails:
        report["trails_updated"] = refresh_trails()

    needs_rebuild = rebuild or any(report.get(k) for k in ("huts_moved", "stops_moved", "lines_changed",
                                                          "trails_updated"))
    report["edges"] = build_edges.build(conn) if needs_rebuild else "unchanged"
    report["footprints_filled"] = footprints.fill(conn)  # edges from before footprints, and hand-made ones
    report["seconds"] = round(time.time() - started)

    log = json.loads(LOG.read_text()) if LOG.exists() else []
    LOG.write_text(json.dumps((log + [report])[-50:], indent=1))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-huts", action="store_true")
    parser.add_argument("--skip-stops", action="store_true")
    parser.add_argument("--skip-timetable", action="store_true")
    parser.add_argument("--skip-trails", action="store_true")
    parser.add_argument("--rebuild", action="store_true", help="rebuild the edges even if no position changed")
    args = parser.parse_args()
    report = run(args.skip_huts, args.skip_stops, args.skip_timetable, args.skip_trails, args.rebuild)
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
