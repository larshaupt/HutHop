"""Client for the (undocumented) SAC route portal API. See spikes/sac_findings.md."""

import re
import time
import unicodedata

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
# A hut's page in the route portal, as linked from hut websites: name slug and POI id
PAGE_URL = "https://www.sac-cas.ch/en/huts-and-tours/sac-route-portal/{}-{}/"


def page_url(sac_id: int, name: str) -> str:
    """The hut's route portal page, e.g. "Refuge de Chalin CAS" → …/refuge-de-chalin-cas-2147000060/."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return PAGE_URL.format(re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-"), sac_id)


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
