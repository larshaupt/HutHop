"""Refresh orchestration with all downloads replaced by fakes (no network)."""

import pytest

from app.db import connect
from app.ingest import refresh
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
    monkeypatch.setattr(refresh.sync_sac, "fetch_and_save", lambda: HUTS)
    monkeypatch.setattr(refresh.load_stops, "download", lambda: calls.append("stops-download"))
    monkeypatch.setattr(refresh.load_stops, "read_stops", lambda: [
        {"uic": 1, "name": "Valley", "mode": "bus", "e_lv95": 2620000.0, "n_lv95": 1150000.0, "lat": 46.5, "lon": 7.9}])
    monkeypatch.setattr(refresh.load_timetable, "download", lambda: False)
    monkeypatch.setattr(refresh.load_timetable, "read", lambda: list(lines))
    monkeypatch.setattr(refresh, "refresh_trails", lambda: False)
    monkeypatch.setattr(refresh.build_edges, "build", lambda conn: calls.append("build") or 42)
    return calls


def test_first_run_rebuilds_then_unchanged(fake):
    first = refresh.run()
    assert first["huts"] == 3 and first["stops"] == 1 and first["huts_moved"] and first["edges"] == 42
    assert first["stop_lines"] == 1 and first["lines_changed"]
    second = refresh.run()
    assert not second["huts_moved"] and not second["stops_moved"] and not second["lines_changed"]
    assert second["edges"] == "unchanged"
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
