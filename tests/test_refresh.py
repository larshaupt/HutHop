"""Refresh orchestration with all downloads replaced by fakes (no network)."""

from datetime import date

import pytest

from app.db import connect
from app.ingest import refresh
from app.ingest.load_timetable import Feed
from tests.test_huts import HUTS


@pytest.fixture
def lines():
    """What the fake timetable returns; tests change it between runs."""
    return [{"uic": 1, "line": "92-121", "label": "B 121", "departures": 30.0}]


@pytest.fixture
def fake(tmp_path, monkeypatch, lines):
    calls = []
    monkeypatch.setattr(refresh, "connect", lambda: connect(tmp_path / "test.db"))
    monkeypatch.setattr(refresh, "LOG", tmp_path / "log.json")
    monkeypatch.setattr(refresh, "BUILT", tmp_path / "built_from.json")
    monkeypatch.setattr(refresh, "TLM_EDITION", tmp_path / "edition.json")
    monkeypatch.setattr(refresh.sync_sac, "fetch_and_save", lambda: HUTS)
    monkeypatch.setattr(refresh.load_stops, "download", lambda: calls.append("stops-download"))
    monkeypatch.setattr(refresh.load_stops, "read_stops", lambda: [
        {"uic": 1, "name": "Valley", "mode": "bus", "e_lv95": 2620000.0, "n_lv95": 1150000.0, "lat": 46.5, "lon": 7.9}])
    monkeypatch.setattr(refresh.load_timetable, "download", lambda: False)
    monkeypatch.setattr(refresh.load_timetable, "read_all", lambda: [
        Feed(2027, "v1", date(2026, 12, 13), date(2027, 12, 11), lines=list(lines))])
    monkeypatch.setattr(refresh, "refresh_trails", lambda: False)
    monkeypatch.setattr(refresh.osm_access, "FILE", tmp_path / "access.json")
    monkeypatch.setattr(refresh.osm_access, "refresh", lambda conn: "unchanged")
    monkeypatch.setattr(refresh.build_edges, "build", lambda conn: calls.append("build") or 42)
    return calls


def test_first_run_rebuilds_then_unchanged(fake):
    first = refresh.run()
    assert first["huts"] == 3 and first["stops"] == 1 and first["edges"] == 42
    assert first["stop_lines"] == 1 and first["changed"] == ["huts", "stops", "lines"]
    second = refresh.run()
    assert second["changed"] == [] and second["edges"] == "unchanged"
    assert fake.count("build") == 1
    assert len(__import__("json").loads(refresh.LOG.read_text())) == 2


def test_forced_rebuild(fake):
    refresh.run()
    assert refresh.run(rebuild=True)["edges"] == 42


def test_rebuild_when_lines_change(fake, lines):
    refresh.run()
    lines.append({"uic": 1, "line": "92-122", "label": "B 122", "departures": 10.0})
    assert refresh.run()["edges"] == 42
    lines[:] = [r | {"departures": 5.0} for r in lines]  # only departure counts changed
    assert refresh.run()["edges"] == "unchanged"


def test_failed_run_rebuilds_next_time(fake, monkeypatch):
    refresh.run()
    moved = [h | {"geom": {"type": "Point", "coordinates": [2621000.0, 1151000.0]}} for h in HUTS]
    monkeypatch.setattr(refresh.sync_sac, "fetch_and_save", lambda: moved)

    def fail():
        raise RuntimeError("timetable server down")
    monkeypatch.setattr(refresh.load_timetable, "download", fail)
    with pytest.raises(RuntimeError):
        refresh.run()  # the huts moved and were saved, then the run failed before the build
    monkeypatch.setattr(refresh.load_timetable, "download", lambda: False)
    assert refresh.run()["changed"] == ["huts"]
    assert fake.count("build") == 2
