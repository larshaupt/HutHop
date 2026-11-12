"""Read-only client for hut-reservation.org availability. See spikes/hut_reservation_findings.md.

Only the public season calendar is used (no login, no booking endpoints).
"""

from datetime import datetime

import httpx

from app.clients import USER_AGENT

BASE = "https://www.hut-reservation.org/api/v1"
BOOKING_URL = "https://www.hut-reservation.org/reservation/book-hut/{}/wizard"  # page for users, by hrs id
TIMEOUT_S = 20


def make_client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=TIMEOUT_S)


def parse_calendar(days: list[dict]) -> list[dict]:
    """One row per day: ISO date, hut status, free beds and total places (0 total = not released)."""
    rows = []
    for d in days:
        day = datetime.strptime(d["dateFormatted"], "%d.%m.%Y").date().isoformat()
        rows.append({
            "date": day,
            "hut_status": d.get("hutStatus"),
            "free_beds": d.get("freeBeds"),
            "total_places": d.get("totalSleepingPlaces") or 0,
        })
    return rows


def fetch_calendar(hrs_id: int, client: httpx.Client) -> list[dict]:
    """The hut's whole booking window, from today (one request, 30–200 KB)."""
    r = client.get(f"{BASE}/reservation/getHutAvailability", params={"hutId": hrs_id, "step": "WIZARD"})
    r.raise_for_status()
    return parse_calendar(r.json())
