"""Trail closures and shooting notices from geo.admin identify results (no network)."""

from datetime import date

from app.clients.geoadmin import CLOSURES, SHOOTING, closures, shooting


def zone(fid, place, dates, start, end, no_shooting):
    return {"layerBodId": SHOOTING, "id": fid, "attributes": {
        "belplan_id": "1302.080", "bezeichnung": "Rouchgrat", "bezeichnung_ort": place,
        "url_en": "https://www.armee.ch/shootingrangebulletin/1302.080", "belegungsdatum": dates,
        "zeit_von": start, "zeit_bis": end, "kein_schiessen": no_shooting}}


RESULTS = [
    {"layerBodId": CLOSURES, "id": 7, "attributes": {"title_en": "Spiez - Thun", "sperrungen_type_en": "Closure",
                                                     "reason_en": "Path impassable", "duration_en": "Until further notice",
                                                     "abstract_en": "Bridge closed.", "url1_link_en": None}},
    {"layerBodId": CLOSURES, "id": 7, "attributes": {"title_en": "Spiez - Thun"}},  # same closure from another chunk
    zone("a", "Rouchgrat", ["27.10.2026", "28.10.2026", "02.11.2026"], ["0800", "0800", ""], ["2200", "1700", ""],
         [False, False, True]),
    zone("b", "Hinterer Rouchgrat", ["28.10.2026"], ["0800"], ["1700"], [False]),
]


def test_closures_once_each():
    assert [c["title"] for c in closures(RESULTS)] == ["Spiez - Thun"]


def test_shooting_times_on_the_day():
    [z] = shooting(RESULTS, date(2026, 10, 28))
    assert z["times"] == ["08:00–17:00"] and z["places"] == ["Rouchgrat", "Hinterer Rouchgrat"]
    assert shooting(RESULTS, date(2026, 11, 2))[0]["times"] == []  # "no shooting" that day
    assert shooting(RESULTS, date(2026, 12, 1))[0]["times"] == []  # not published: the zone is still listed
