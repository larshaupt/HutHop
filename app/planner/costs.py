"""Cost estimate per person for a tour's nights, from the price components in hut_price (hut websites).

Per night, the cheapest bed (usually the dormitory):
  catered or unknown  half board (plus the bed if half board is listed without it); bed only if no half board
  self_service        winter room (or bed) plus the kitchen fee
plus the tourist tax. Member and regular prices are both computed, so the app can switch without a new search.
Nights at huts without prices are left out and counted, so the total is a lower bound then.
"""

import sqlite3

from app.planner.tours import Night


def load_prices(conn: sqlite3.Connection, hut_ids: set[int]) -> dict[int, list[dict]]:
    ids = sorted(hut_ids)
    prices: dict[int, list[dict]] = {}
    for r in conn.execute(f"SELECT * FROM hut_price WHERE hut_id IN ({','.join('?' * len(ids))}) ORDER BY rowid", ids):
        prices.setdefault(r["hut_id"], []).append(dict(r))
    return prices


def _amount(p: dict | None, member: bool) -> float:
    if p is None:
        return 0.0
    first, second = ("member", "non_member") if member else ("non_member", "member")
    return p[first] if p[first] is not None else p[second] or 0.0


def _cheapest(prices: list[dict], item: str) -> dict | None:
    return min((p for p in prices if p["item"] == item and _amount(p, False)), key=lambda p: _amount(p, False),
               default=None)


def night_cost(prices: list[dict], status: str) -> dict | None:
    """{member, non_member, currency, basis, label, year} per person, or None without usable prices."""
    if not prices:
        return None
    bed = _cheapest(prices, "overnight")
    extra = None
    if status == "self_service":
        main = _cheapest(prices, "winter_room") or bed
        basis = "winter room" if main and main["item"] == "winter_room" else "bed, self-catering"
        extra = _cheapest(prices, "self_catering_fee")
    else:
        boards = [p for p in prices if p["item"] == "half_board" and _amount(p, False)]
        main = min(boards, key=lambda p: _amount(p, False) + (0 if p["includes_overnight"] else _amount(bed, False)),
                   default=None)
        if main and not main["includes_overnight"]:
            extra = bed
        basis = "half board" if main else "bed only"
        main = main or bed
    if not main:
        return None
    taxes = [p for p in prices if p["item"] == "tax"]
    tax = max(taxes, key=lambda p: _amount(p, False), default=None)  # the adult rate
    parts = [p for p in (main, extra, tax) if p and p["currency"] == main["currency"]]
    return {
        "member": round(sum(_amount(p, True) for p in parts), 2),
        "non_member": round(sum(_amount(p, False) for p in parts), 2),
        "currency": main["currency"],
        "basis": basis,
        "label": main["label"],
        "year": main["year"],
        "tax": tax is not None,
    }


def tour_cost(nights: tuple[Night, ...], prices: dict[int, list[dict]]) -> dict:
    """Per-night costs and per-person totals by currency (huts in Italy publish EUR)."""
    per_night = [night_cost(prices.get(n.hut, []), n.status) for n in nights]
    totals: dict[str, dict[str, float]] = {}
    for c in filter(None, per_night):
        t = totals.setdefault(c["currency"], {"member": 0.0, "non_member": 0.0})
        t["member"] = round(t["member"] + c["member"], 2)
        t["non_member"] = round(t["non_member"] + c["non_member"], 2)
    return {"nights": per_night, "totals": totals, "priced": sum(c is not None for c in per_night),
            "years": sorted({c["year"] for c in per_night if c and c["year"]})}
