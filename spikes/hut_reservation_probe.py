"""Phase 0 spike: probe hut-reservation.org public (unauthenticated) read endpoints.

Endpoints learned from domwoe/hut-reservation-mcp (MIT), src/upstream.ts.
Read-only, low rate (>=1.5 s between requests), no login, no booking endpoints.
Raw responses are written to data/raw/hrs/ (gitignored).

    uv run python spikes/hut_reservation_probe.py              # everything
    uv run python spikes/hut_reservation_probe.py calendar     # season calendar only
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date
from pathlib import Path

import httpx

BASE = "https://www.hut-reservation.org/api/v1"
UA = "HutHop/0.1 (private hut planner)"
OUT = Path(__file__).resolve().parents[1] / "data" / "raw" / "hrs"
DELAY_S = 1.5

# hutId values from the MCP repo's bundled catalog (data/catalog.json)
HUTS = {6: "Monte Rosa Hütte", 558: "Lauteraarhütte", 1: "Blüemlisalphütte"}
SUMMER = (date(2027, 7, 15), date(2027, 7, 18))  # 3 nights, 2027 season
NOW = (date(2026, 10, 24), date(2026, 10, 26))  # current dates (likely closed)


def swiss(d: date) -> str:
    return d.strftime("%d.%m.%Y")


class Probe:
    def __init__(self) -> None:
        self.client = httpx.Client(
            headers={"User-Agent": UA, "Accept": "application/json"}, timeout=20
        )
        self.n = 0

    def call(self, method: str, path: str, name: str, **kw) -> httpx.Response:
        if self.n:
            time.sleep(DELAY_S)
        self.n += 1
        r = self.client.request(method, f"{BASE}{path}", **kw)
        print(f"[{self.n:02d}] {method} {path} -> {r.status_code} "
              f"({len(r.content)} B, {r.headers.get('content-type')}, "
              f"cache-control={r.headers.get('cache-control')})")
        try:
            body = r.json()
        except ValueError:
            body = r.text[:2000]
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"{name}.json").write_text(json.dumps(
            {"request": {"method": method, "path": path, "json": kw.get("json")},
             "status": r.status_code, "headers": dict(r.headers), "body": body},
            ensure_ascii=False, indent=2))
        return r

    def availability(self, hut_id: int, arr: date, dep: date, tag: str) -> None:
        dates = {"arrivalDate": swiss(arr), "departureDate": swiss(dep)}
        st = self.call("POST", f"/reservation/hutStatus/{hut_id}",
                       f"hutStatus_{hut_id}_{tag}", json=dates)
        status = st.json() if st.is_success else {}
        payload = {
            **dates,
            "numberOfPeople": 0,
            "nextPossibleReservations": False,
            "peoplePerCategory": [
                {"categoryId": c.get("categoryID") or c.get("categoryId"), "people": 0}
                for c in status.get("categories") or []
            ],
            "hutId": hut_id,
            "serviced": status.get("hutStatus"),
            "isWaitingListEnabled": status.get("isWaitingListEnabled", False),
            "isWaitingListAccepted": status.get("isWaitingListAccepted", False),
            "allowAnyBedCategory": False,
        }
        r = self.call("POST", f"/reservation/checkAvailability/{hut_id}",
                      f"checkAvailability_{hut_id}_{tag}", json=payload)
        if r.is_success:
            for d in r.json().get("availabilityPerDayDTOs") or []:
                print(f"     {d.get('day')}: freePlaces={d.get('freePlaces')} "
                      f"status={d.get('hutStatus')} bookable={d.get('availableForReservation')}")


    def calendar(self, hut_id: int) -> None:
        """Season calendar: one GET -> ~1000 days from today (not used by the MCP repo;
        this is the call the hut-reservation.org SPA's calendar view makes)."""
        r = self.call("GET", "/reservation/getHutAvailability", f"getHutAvailability_{hut_id}",
                      params={"hutId": hut_id, "step": "WIZARD"})
        if r.is_success:
            days = r.json()
            open_days = [d for d in days if d.get("hutStatus") != "CLOSED"]
            print(f"     {len(days)} days, {len(open_days)} not CLOSED; first: {days[0]}")


def main() -> None:
    p = Probe()
    if sys.argv[1:] == ["calendar"]:
        for hut_id in HUTS:
            if hut_id != 6:  # already fetched for 6 during the spike
                p.calendar(hut_id)
        return
    for hut_id in HUTS:
        p.calendar(hut_id)
    for hut_id in HUTS:
        p.call("GET", f"/reservation/hutInfo/{hut_id}", f"hutInfo_{hut_id}")
    for hut_id in HUTS:
        p.availability(hut_id, *SUMMER, tag="2027-07")
    p.availability(6, *NOW, tag="now")
    print(f"done: {p.n} requests, raw responses in {OUT}")


if __name__ == "__main__":
    main()
