"""Tests for the tour cost estimate from hut prices."""

from datetime import date

from app.planner.costs import night_cost, tour_cost
from app.planner.tours import Night


def price(item, member, non_member=None, includes_overnight=False, label="", currency="CHF", year=2027):
    return {"item": item, "label": label, "includes_overnight": includes_overnight, "member": member,
            "non_member": non_member if non_member is not None else member, "currency": currency, "year": year}


SAC_HUT = [price("overnight", 35, 45, label="Massenlager"), price("overnight", 60, 70, label="Zimmer"),
           price("half_board", 40, 40, label="Halbpension"), price("tax", 3), price("tax", 1.5)]


def test_catered_night_is_cheapest_bed_plus_half_board_plus_adult_tax():
    c = night_cost(SAC_HUT, "catered")
    assert (c["member"], c["non_member"], c["basis"], c["label"]) == (78, 88, "half board", "Halbpension")


def test_half_board_including_the_bed_is_not_added_twice():
    prices = [price("overnight", 50), price("half_board", 90, includes_overnight=True, label="Lager mit HP"),
              price("half_board", 120, includes_overnight=True, label="Zimmer mit HP")]
    assert night_cost(prices, "unknown")["non_member"] == 90


def test_self_service_night_uses_winter_room_and_kitchen_fee():
    prices = SAC_HUT + [price("winter_room", 20, 30), price("self_catering_fee", 5)]
    c = night_cost(prices, "self_service")
    assert (c["member"], c["basis"]) == (28, "winter room")
    assert night_cost(SAC_HUT, "self_service")["basis"] == "bed, self-catering"


def test_bed_only_without_half_board_and_none_without_prices():
    assert night_cost([price("overnight", 40)], "catered")["basis"] == "bed only"
    assert night_cost([price("breakfast", 15)], "catered") is None
    assert night_cost([], "catered") is None


def test_tour_totals_per_currency_and_unpriced_nights():
    nights = tuple(Night(hut, date(2027, 7, 10 + i), "catered", False) for i, hut in enumerate((1, 2, 3)))
    prices = {1: SAC_HUT, 2: [price("half_board", 80, includes_overnight=True, currency="EUR", year=2026)]}
    cost = tour_cost(nights, prices)
    assert cost["totals"] == {"CHF": {"member": 78, "non_member": 88}, "EUR": {"member": 80, "non_member": 80}}
    assert cost["priced"] == 2 and cost["nights"][2] is None and cost["years"] == [2026, 2027]
