import shapely

from app.ingest import regions


def poly(lon0, lat0, lon1, lat1):
    return (f"region\n1\n   {lon0} {lat0}\n   {lon1} {lat0}\n   {lon1} {lat1}\n   {lon0} {lat1}\n   {lon0} {lat0}\n"
            "END\nEND\n")


def test_overlapping_regions_and_mountain_cut(tmp_path, monkeypatch):
    monkeypatch.setattr(regions, "DIR", tmp_path)
    monkeypatch.setattr(regions, "REGIONS", {
        "AT": regions.Region("at.pbf", "at.poly", whole_country=True),
        "DE": regions.Region("de.pbf", "de.poly", south_of=47.5)})
    (tmp_path / "region_at.poly").write_text(poly(10.0, 47.0, 11.0, 47.6))
    (tmp_path / "region_de.poly").write_text(poly(10.5, 47.2, 11.5, 48.5))
    regions.area.cache_clear()
    try:
        point = lambda lon, lat: shapely.Point(*regions.WGS84_TO_LV95.transform(lon, lat))  # noqa: E731
        de, de_own = regions.area("DE"), regions.own_area("DE")
        assert de.contains(point(11.2, 47.4)) and not de.contains(point(11.2, 47.7))  # only south of 47.5°
        # where both extracts reach, the first region's trails are used: DE's own part leaves out Austria's area
        assert de.contains(point(10.8, 47.4)) and not de_own.contains(point(10.8, 47.4))
        assert de_own.contains(point(11.2, 47.4))
        assert regions.own_area("AT").equals(regions.area("AT"))
    finally:
        regions.area.cache_clear()
