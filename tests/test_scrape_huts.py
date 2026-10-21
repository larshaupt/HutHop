"""Tests for reading hut websites (HTML to text, link choice, loading extractions) and for the website seasons
in the opening rule."""

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_db
from app.clients import web
from app.db import connect
from app.ingest import scrape_huts
from app.ingest.sync_sac import sync
from app.main import app
from app.planner.opening import Season, codes_from_seasons, on_day
from tests.test_huts import raw_hut

NONE = "2" * 12
SUMMER = "222221000122"  # catered June (partly) to October (partly)


def test_parse_html_text_and_links():
    page = web.parse_html(
        "<html><head><title>Hütte</title><style>p{}</style></head><body><nav><a href='/preise#x'>Preise</a>"
        "<a href='tel:+41 33 000 00 00'>Tel</a></nav><p>Bewartet vom<br>20. Juni&nbsp;bis 30. September</p>"
        "<table><tr><td>Übernachtung</td><td>35.-</td></tr></table><script>var x</script></body></html>",
        "https://www.huette.ch/")
    assert page["title"] == "Hütte"
    assert "Bewartet vom\n20. Juni bis 30. September" in page["text"]
    assert "Übernachtung | 35.-" in page["text"] and "var x" not in page["text"]
    assert page["links"] == [("https://www.huette.ch/preise", "Preise"), ("tel:+41 33 000 00 00", "Tel")]


def test_link_score_prefers_prices_on_the_same_site():
    base = "huette.ch"
    assert scrape_huts.link_score("https://www.huette.ch/preise", "Preise", base) > \
        scrape_huts.link_score("https://www.huette.ch/huette", "Die Hütte", base) > 0
    assert scrape_huts.link_score("https://www.huette.ch/galerie", "Galerie", base) == 0
    assert scrape_huts.link_score("https://other.ch/preise", "Preise", base) == 0
    assert scrape_huts.link_score("https://other.ch/tarife-2026.pdf", "Tarife", base) > 0  # PDFs elsewhere are ok
    assert scrape_huts.link_score("https://www.huette.ch/bild.jpg", "Preise", base) == 0


def season(kind, start, end, year=None, vague=False):
    return Season(kind, start, end, year, vague)


def test_website_season_decides_days_of_a_partial_month():
    seasons = (season("catered", (6, 20), (9, 30), 2027),)
    assert on_day(SUMMER, NONE, seasons, date(2027, 6, 19), "any") is None
    assert on_day(SUMMER, NONE, seasons, date(2027, 6, 20), "catered") == ("catered", False)
    # SAC says "partly" in October; the website says closed from October 1st
    assert on_day(SUMMER, NONE, seasons, date(2027, 10, 2), "any") is None


def test_website_conflicting_with_a_whole_sac_month_is_only_uncertain():
    seasons = (season("catered", (8, 1), (8, 31), 2027),)  # e.g. the model saw only part of the season
    assert on_day(SUMMER, NONE, seasons, date(2027, 7, 10), "any") == ("catered", True)


def test_website_season_from_another_year_is_uncertain_near_the_edges():
    seasons = (season("catered", (6, 20), (9, 30), 2026),)
    assert on_day(SUMMER, NONE, seasons, date(2027, 6, 25), "any") == ("catered", True)
    assert on_day(SUMMER, NONE, seasons, date(2027, 8, 1), "any") == ("catered", False)
    vague = (season("catered", (6, 15), (9, 30), None, vague=True),)
    assert on_day(SUMMER, NONE, vague, date(2027, 9, 25), "any") == ("catered", True)


def test_sac_codes_still_apply_to_the_other_way_of_staying():
    winter_room = "000000000000"
    seasons = (season("catered", (6, 20), (9, 30), 2027),)
    assert on_day(SUMMER, winter_room, seasons, date(2027, 3, 1), "any") == ("self_service", False)
    assert on_day(SUMMER, winter_room, seasons, date(2027, 3, 1), "catered") is None


def test_exceptional_closure_only_in_its_year():
    seasons = (season("closed", (6, 1), (10, 31), 2026),)
    assert on_day(SUMMER, NONE, seasons, date(2026, 7, 10), "any") is None
    assert on_day(SUMMER, NONE, seasons, date(2027, 7, 10), "any") == ("catered", False)


def test_season_over_new_year_and_codes_for_huts_without_sac_data():
    winter = (season("catered", (12, 20), (4, 10)),)
    assert on_day(NONE, NONE, winter, date(2028, 1, 5), "any") == ("catered", False)
    assert on_day(NONE, NONE, winter, date(2027, 7, 5), "any") is None
    assert codes_from_seasons(winter, 2027) == ("000122222221", NONE)
    assert on_day(NONE, NONE, (), date(2027, 7, 5), "any") == ("unknown", False)


@pytest.fixture
def scraped(tmp_path, monkeypatch):
    conn = connect(tmp_path / "test.db")
    sync(conn, [raw_hut(1, "Summer Hut", SUMMER, NONE, tel="+41 33 111 11 11", email="hut@sac.ch"),
                raw_hut(2, "Mystery Hut", NONE, NONE), raw_hut(3, "Dead Link Hut", NONE, NONE)])
    monkeypatch.setattr(scrape_huts, "RAW_DIR", tmp_path)
    text = "Bewartet vom 20. Juni bis 30. September 2027.\nÜbernachtung | Mitglieder 35.- | Nichtmitglieder 45.-"
    raw = {"website": "https://huette.ch", "fetched_at": "2026-10-21T10:00:00+00:00",
           "pages": [{"url": "https://huette.ch", "title": "", "text": text}], "pdfs": []}
    (tmp_path / "2.json").write_text(json.dumps(raw))
    (tmp_path / "3.json").write_text(json.dumps(raw | {"pages": []}))
    result = {
        "about_this_hut": True, "phone": "+41 33 222 22 22", "email": "mailto:info@huette.ch", "booking_methods": ["phone"], "booking_url": None,
        "closed_otherwise": None, "winter_room": False, "winter_room_beds": None, "beds": 30,
        "notes": "Cash only.\x00",
        "seasons": [{"kind": "catered", "start": "06-20", "end": "09-30", "year": 2027, "approximate": False,
                     "quote": "Bewartet vom 20. Juni bis 30. September 2027.", "source_url": "https://huette.ch"},
                    {"kind": "catered", "start": "13-01", "end": "09-30", "year": None, "approximate": False,
                     "quote": "?", "source_url": ""}],
        "prices": [{"item": "overnight", "label": "Übernachtung", "includes_overnight": True, "member": 35,
                    "non_member": 45, "currency": "CHF", "year": 2027, "quote": "Mitglieder 35.-",
                    "source_url": "https://huette.ch"},
                   {"item": "breakfast", "label": "Frühstück", "includes_overnight": False, "member": None,
                    "non_member": None, "currency": "CHF", "year": 2027, "quote": "", "source_url": ""}],
    }
    out = {"result": result, "info": {"model": "test"}, "extracted_at": "2026-10-21T11:00:00+00:00"}
    (tmp_path / "2.extract.json").write_text(json.dumps(out))
    (tmp_path / "3.extract.json").write_text(json.dumps(out | {"result": None}))
    return conn


def test_load_extractions(scraped):
    counts = scrape_huts.load(scraped)
    assert counts == {"huts": 2, "ok": 1, "seasons": 1, "prices": 1, "skipped_values": 2}
    status = dict(scraped.execute("SELECT h.sac_id, w.status FROM hut_web w JOIN hut h ON h.place_id = w.hut_id"))
    assert status == {2: "ok", 3: "unreachable"}
    price = scraped.execute("SELECT * FROM hut_price").fetchone()
    assert (price["member"], price["non_member"], price["includes_overnight"], price["verified"]) == (35, 45, 0, 1)
    assert scraped.execute("SELECT notes FROM hut_web WHERE notes IS NOT NULL").fetchone()[0] == "Cash only."
    # Loading again replaces the scraped rows instead of adding to them
    assert scrape_huts.load(scraped)["prices"] == 1
    assert scraped.execute("SELECT count(*) FROM hut_price").fetchone()[0] == 1


def test_hut_detail_shows_phone_seasons_and_prices(scraped):
    scrape_huts.load(scraped)
    app.dependency_overrides[get_db] = lambda: scraped
    try:
        client = TestClient(app)
        ids = dict(scraped.execute("SELECT sac_id, place_id FROM hut"))
        sac_phone = client.get(f"/api/huts/{ids[1]}").json()
        assert sac_phone["phone"] == "+41 33 111 11 11" and sac_phone["web"] is None
        assert sac_phone["email"] == "hut@sac.ch"
        mystery = client.get(f"/api/huts/{ids[2]}").json()
        assert mystery["phone"] == "+41 33 222 22 22"  # from the website when SAC has none
        assert mystery["email"] == "info@huette.ch"
        assert mystery["web"]["booking_methods"] == ["phone"]
        assert mystery["web"]["prices"][0]["label"] == "Übernachtung"
        # The website season turns "no data" into a real season on the map
        assert mystery["months"][6] == "catered" and mystery["months"][0] == "closed"
    finally:
        app.dependency_overrides.clear()
