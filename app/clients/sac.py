"""Client for the (undocumented) SAC route portal API. See spikes/sac_findings.md."""

import time

import httpx

BASE = "https://www.suissealpine.sac-cas.ch/api/1/"
USER_AGENT = "HutHop/0.1 (private hut planner)"
SEARCH_PARAMS = {
    "lang": "en",
    "output_lang": "en",
    "order_by": "-first_time_published",
    "type": "hut",
    "disciplines": "mountain_hiking,alpine_tour,climbing,via_ferrata,ski_tour,snowshoe_tour,alpine_climbing",
    "hut_type": "all",
    "mode": "per_discipline",
    "limit": 100,
}
REQUEST_DELAY_S = 2.0


def fetch_huts(client: httpx.Client | None = None) -> list[dict]:
    """All huts from the portal search, paging with `cursor` (4 requests for ~400 huts)."""
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30)
    total = client.get(BASE + "poi/search_count", params=SEARCH_PARAMS).raise_for_status().json()["count"]
    huts: list[dict] = []
    cursor = None
    while len(huts) < total:
        time.sleep(REQUEST_DELAY_S)
        params = SEARCH_PARAMS | ({"cursor": cursor} if cursor else {})
        page = client.get(BASE + "poi/search", params=params).raise_for_status().json()
        if not page["results"]:
            break
        huts += page["results"]
        cursor = page.get("cursor")
        if not cursor:
            break
    return huts
