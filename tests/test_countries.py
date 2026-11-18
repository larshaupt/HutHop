"""Country borders: tagging places and finding where a route leaves Switzerland (no download)."""

import json

import pytest
import shapely

from app.db import connect
from app.ingest import countries

# Two squares side by side in LV95 metres: "CH" west of E 2 610 000, "LI" east of it up to E 2 620 000.
CH = shapely.box(2_600_000, 1_200_000, 2_610_000, 1_210_000)
LI = shapely.box(2_610_000, 1_200_000, 2_620_000, 1_210_000)


@pytest.fixture(autouse=True)
def borders(tmp_path, monkeypatch):
    path = tmp_path / "countries.json"
    path.write_text(json.dumps({"href": "x", "countries": {"CH": shapely.to_geojson(CH), "LI": shapely.to_geojson(LI)}}))
    monkeypatch.setattr(countries, "FILE", path)
    countries.borders.cache_clear()
    yield
    countries.borders.cache_clear()


def latlon(e, n):
    lon, lat = countries.WGS84_TO_LV95.transform(e, n, direction="INVERSE")
    return lat, lon


def test_places_inside_get_their_country_others_keep_theirs(tmp_path):
    conn = connect(tmp_path / "t.db")
    rows = [("in CH", 2_605_000, 1_205_000, None), ("in LI", 2_615_000, 1_205_000, None),
            ("on the border", 2_620_000, 1_205_000, None), ("abroad", 2_630_000, 1_205_000, "AT")]
    conn.executemany("INSERT INTO place (kind, name, e_lv95, n_lv95, lat, lon, country) VALUES ('hut', ?, ?, ?, 0, 0, ?)",
                     rows)
    assert countries.tag_places(conn) == {"CH": 1, "LI": 2}
    assert dict(conn.execute("SELECT name, country FROM place").fetchall()) == {
        "in CH": "CH", "in LI": "LI", "on the border": "LI", "abroad": "AT"}


def test_route_leaving_switzerland():
    swiss = [latlon(2_602_000, 1_205_000), latlon(2_608_000, 1_205_000)]
    assert countries.outside(swiss) == []
    # 30 m past the border is within the tolerance, 2 km is not; then on into a country without border data
    assert countries.outside(swiss + [latlon(2_610_030, 1_205_000)]) == []
    assert countries.outside(swiss + [latlon(2_612_000, 1_205_000), latlon(2_630_000, 1_205_000)]) == ["LI", "abroad"]
