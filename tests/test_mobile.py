"""Browser tests in mobile emulation (Playwright): the real app on a small seeded database, on phone viewports.

    stop Valley ── A ── B ── C ── D ── stop Pass   (plus A–C, B–D, B–Valley, C–Pass: several tours)

Needs a browser: `uv run playwright install chromium`. Map tiles come from the internet."""

import re
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import Page, expect

from app.api.deps import get_db
from app.db import connect
from app.ingest.sync_sac import sync
from app.main import app
from tests.test_huts import raw_hut

DEVICES = ["iPhone 13", "Pixel 7"]
SEARCH = "#v=tours&tb=7.6,46.4,8.2,46.6"  # the seeded huts' area


def seed(path):
    conn = connect(path)
    sync(conn, [raw_hut(i, f"Hut {c}", "111111111111", "111111111111") for i, c in enumerate("ABCD", 1)])
    huts = [r[0] for r in conn.execute("SELECT place_id FROM hut ORDER BY sac_id")]
    for i, h in enumerate(huts):
        conn.execute("UPDATE place SET e_lv95 = ?, n_lv95 = 1150000, lat = 46.5, lon = ? WHERE id = ?",
                     (2_600_000 + i * 5000, 7.8 + i * 0.065, h))
    stops = []
    for name, lon, e in (("Valley", 7.75, 2_596_000), ("Pass", 8.05, 2_619_000)):
        s = conn.execute("INSERT INTO place (kind, name, e_lv95, n_lv95, lat, lon) VALUES ('stop', ?, ?, 1150000, 46.5, ?)",
                         (name, e, lon)).lastrowid
        conn.execute("INSERT INTO stop (place_id, uic, mode) VALUES (?, ?, 'bus')", (s, 8_500_000 + s))
        stops.append(s)
    a, b, c, d = huts
    pairs = [(stops[0], a), (a, b), (b, c), (c, d), (a, c), (b, d), (d, stops[1]), (c, stops[1]), (b, stops[0])]
    conn.executemany(
        "INSERT INTO edge (from_place, to_place, category_limit, max_category, time_min, distance_m, ascent_m, "
        "descent_m, source, geometry) VALUES (?, ?, 1, 1, 240, 10000, 600, 400, 'tlm_routed', '_p~iF~ps|U')",
        [e for x, y in pairs for e in ((x, y), (y, x))])
    conn.commit()
    conn.close()


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    path = tmp_path_factory.mktemp("mobile") / "huthop.db"
    seed(path)

    def db():
        conn = connect(path, init=False)
        try:
            yield conn
        finally:
            conn.close()

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app.dependency_overrides[get_db] = db
    srv = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/"
    srv.should_exit = True
    thread.join()
    app.dependency_overrides.clear()


@pytest.fixture(params=DEVICES)
def browser_context_args(browser_context_args, playwright, request):
    device = {k: v for k, v in playwright.devices[request.param].items() if k != "default_browser_type"}
    return {**browser_context_args, **device}


@pytest.fixture
def phone(page: Page, server):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    yield page
    assert not errors, errors  # uncaught script errors


def no_sideways_scroll(page: Page):
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def swipe(page: Page, dx: int):
    """A one-finger swipe across the open tour (Playwright has no swipe; touch events from script)."""
    page.evaluate("""dx => {
        const el = document.querySelector('#tourDetail');
        const r = el.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + 200;
        const touch = (cx) => new Touch({ identifier: 1, target: el, clientX: cx, clientY: y });
        el.dispatchEvent(new TouchEvent('touchstart', { touches: [touch(x)], changedTouches: [touch(x)], bubbles: true }));
        el.dispatchEvent(new TouchEvent('touchend', { touches: [], changedTouches: [touch(x + dx)], bubbles: true }));
    }""", dx)


def test_select_hut(phone: Page, server):
    phone.goto(server)
    phone.locator("#sheetHandle").click()  # the sheet starts collapsed
    phone.get_by_placeholder("Search huts").fill("Hut B")
    expect(phone.locator("#list .hut")).to_have_count(1)
    no_sideways_scroll(phone)
    phone.locator("#list .hut").click()
    expect(phone.locator("#detail h2")).to_have_text("Hut B")
    no_sideways_scroll(phone)


def test_search_open_and_swipe_between_tours(phone: Page, server):
    phone.goto(server + SEARCH + "&tour=1")
    detail = phone.locator("#tourDetail")
    expect(detail).to_be_visible()
    total = int(detail.locator(".tour-step span").inner_text().split("/")[1])
    assert total >= 3
    expect(detail.locator('[data-step-tour="-1"]')).to_be_disabled()
    no_sideways_scroll(phone)

    swipe(phone, -120)  # left: next tour
    expect(detail.locator(".tour-step span")).to_have_text(f"2 / {total}")
    assert "tour=2" in phone.url
    swipe(phone, 120)  # right: previous
    expect(detail.locator(".tour-step span")).to_have_text(f"1 / {total}")
    swipe(phone, 120)  # nothing before the first
    expect(detail.locator(".tour-step span")).to_have_text(f"1 / {total}")

    detail.get_by_label("Next tour").click()
    expect(detail.locator(".tour-step span")).to_have_text(f"2 / {total}")
    assert "tour=2" in phone.url

    detail.locator("#tourBack").click()
    expect(phone.locator(".tour-card")).to_have_count(total)
    no_sideways_scroll(phone)


def test_mostly_vertical_swipe_keeps_the_tour(phone: Page, server):
    phone.goto(server + SEARCH + "&tour=1")
    expect(phone.locator(".tour-step span")).to_contain_text("1 /")
    phone.evaluate("""() => {
        const el = document.querySelector('#tourDetail');
        const t = (x, y) => new Touch({ identifier: 1, target: el, clientX: x, clientY: y });
        el.dispatchEvent(new TouchEvent('touchstart', { touches: [t(200, 400)], changedTouches: [t(200, 400)] }));
        el.dispatchEvent(new TouchEvent('touchend', { touches: [], changedTouches: [t(120, 200)] }));
    }""")
    expect(phone.locator(".tour-step span")).to_contain_text("1 /")


def test_search_everywhere_unless_limited_to_the_map(phone: Page, server):
    phone.goto(server + "#v=tours")
    phone.locator("#sheetHandle").click()
    phone.locator("#tFind").click()
    expect(phone.locator(".tour-card").first).to_be_visible()
    assert "tb=" not in phone.url  # all huts, whatever the map shows
    phone.get_by_label("Only in the area visible on the map").check()
    phone.locator("#tFind").click()
    expect(phone).to_have_url(re.compile(r"tb=[-\d.]+%2C"))


def test_saved_tour_works_offline(phone: Page, server):
    phone.context.route(re.compile(r"^https://wmts\.geo\.admin\.ch/"), lambda route: route.abort())  # no map downloads
    phone.goto(server + SEARCH + "&tour=1")
    phone.wait_for_function("navigator.serviceWorker.controller")  # sw.js installed and in charge
    phone.locator("#tourKeep").click()
    expect(phone.locator("#offlineStatus")).to_contain_text("Saved for offline use", timeout=15000)
    expect(phone.locator("#offlineNote")).to_be_hidden()

    phone.context.set_offline(True)
    phone.goto(server + "#v=mine")
    phone.reload()  # a new page load, from the service worker's cache
    phone.locator("#sheetHandle").click()
    phone.locator("#yoursList .your-tour").click()
    expect(phone.locator("#tourDetail h2")).to_be_visible()
    expect(phone.locator("#offlineNote")).to_contain_text("Offline — showing saved data from")
    phone.locator("#tourDetail [data-hut]").first.click()  # the huts' details are saved too
    expect(phone.locator("#detail h2")).to_have_text(re.compile("Hut"))
    no_sideways_scroll(phone)
    phone.context.set_offline(False)


def test_offline_map_tiles_are_capped(phone: Page, server):
    phone.goto(server)
    # one straight day of 115 km: the gap between its two points is filled, and the cap keeps high zoom levels out
    tiles = phone.evaluate("""async () => {
        const { tourTiles, MAX_TILES } = await import('/js/offline.js');
        const { urls, zoom } = tourTiles({ days: [{ geometry: '_`yzG_ptf@?_~cH' }] });
        return { count: urls.length, unique: new Set(urls).size, zoom, max: MAX_TILES };
    }""")
    assert 0 < tiles["count"] <= tiles["max"] and tiles["unique"] == tiles["count"]
    assert 10 <= tiles["zoom"] < 15


def test_flexible_search_from_link(phone: Page, server):
    """A shared flexible search: the window comes from the link, and each card names its start date."""
    phone.goto(server + "#v=tours&tf=3")
    phone.locator("#sheetHandle").click()
    expect(phone.locator("#tFlex")).to_have_value("3")
    phone.locator("#tFind").click()
    expect(phone.locator(".tour-card").first).to_be_visible()
    expect(phone.locator(".tour-head").first).to_contain_text(re.compile(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d+ \w+ · "))
    no_sideways_scroll(phone)
