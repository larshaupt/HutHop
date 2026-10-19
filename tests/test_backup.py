"""Tests for the database backup and the cached route graph."""

from datetime import date

from app.backup import backup
from app.db import connect
from app.ingest.sync_sac import sync
from app.planner.graph import cached_graph
from tests.test_huts import HUTS


def test_backup_copies_and_keeps_newest(tmp_path):
    db = tmp_path / "huthop.db"
    conn = connect(db)
    sync(conn, HUTS[:2])
    for day in range(1, 5):
        backup(db, keep=3, day=date(2026, 10, day))
    files = sorted(p.name for p in (tmp_path / "backups").iterdir())
    assert files == ["huthop-2026-10-02.db", "huthop-2026-10-03.db", "huthop-2026-10-04.db"]
    copy = connect(tmp_path / "backups" / files[-1], init=False)
    assert copy.execute("SELECT count(*) FROM hut").fetchone()[0] == 2


def test_graph_cache_follows_changes(tmp_path):
    conn = connect(tmp_path / "huthop.db")
    sync(conn, HUTS[:2])
    first = cached_graph(conn, 2)
    assert cached_graph(conn, 2) is first  # nothing changed
    a = min(first.huts)
    with conn:
        conn.execute("INSERT INTO hut_override (hut_id, field, value, updated_at) VALUES (?, 'name', '\"New\"', 'now')", (a,))
    second = cached_graph(conn, 2)
    assert second is not first and second.huts[a].name == "New"
    assert cached_graph(conn, 3).footprints is second.footprints  # one geometry cache for all trail limits


def test_health(tmp_path):
    from fastapi.testclient import TestClient

    from app.api.deps import get_db
    from app.main import app

    conn = connect(tmp_path / "huthop.db")
    sync(conn, HUTS[:2])
    app.dependency_overrides[get_db] = lambda: conn
    try:
        assert TestClient(app).get("/api/health").json() == {"status": "ok", "huts": 2, "edges": 0}
    finally:
        app.dependency_overrides.clear()
