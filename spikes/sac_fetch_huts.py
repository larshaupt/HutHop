"""Spike: download all SAC huts from the route portal API into data/raw/sac/huts.json."""

import json
import time
from pathlib import Path

import httpx

BASE = "https://www.suissealpine.sac-cas.ch/api/1/"
PARAMS = {
    "lang": "en",
    "output_lang": "en",
    "order_by": "-first_time_published",
    "type": "hut",
    "disciplines": "mountain_hiking,alpine_tour,climbing,via_ferrata,ski_tour,snowshoe_tour,alpine_climbing",
    "hut_type": "all",
    "mode": "per_discipline",
    "limit": 100,
}
OUT = Path(__file__).parent.parent / "data/raw/sac/huts.json"


def main() -> None:
    client = httpx.Client(headers={"User-Agent": "HutHop/0.1 (private hut planner)"}, timeout=30)
    total = client.get(BASE + "poi/search_count", params=PARAMS).json()["count"]
    huts, cursor = [], None
    while True:
        time.sleep(2)
        params = PARAMS | ({"cursor": cursor} if cursor else {})
        page = client.get(BASE + "poi/search", params=params).raise_for_status().json()
        huts += page["results"]
        print(f"{len(huts)}/{total}")
        cursor = page.get("cursor")
        if not page["results"] or not cursor or len(huts) >= total:
            break
    OUT.write_text(json.dumps(huts, ensure_ascii=False))
    print(f"saved {len(huts)} huts ({len({h['id'] for h in huts})} unique) to {OUT}")


if __name__ == "__main__":
    main()
