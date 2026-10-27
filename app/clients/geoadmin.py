"""Official warnings along a route from the federal geoportal (api3.geo.admin.ch identify, no key):
hiking trail closures (Swiss Hiking Federation, `ch.astra.wanderland-sperrungen_umleitungen`) and the army's
shooting notices with their danger zones (`ch.vbs.schiessanzeigen`)."""

import time
from datetime import date, datetime

import httpx

from app.clients.hut_reservation import USER_AGENT

URL = "https://api3.geo.admin.ch/rest/services/ech/MapServer/identify"
CLOSURES = "ch.astra.wanderland-sperrungen_umleitungen"
SHOOTING = "ch.vbs.schiessanzeigen"
TOLERANCE_M = 20  # how close to the route a closure or zone counts
CHUNK = 100  # route points per request (a whole day's line makes the URL too long)
TIMEOUT_S = 15
CACHE_TTL_S = 3600
# shortcut: an in-process cache per route line (lost on restart, one per worker); fine for a few users
_cache: dict[str, tuple[float, list[dict]]] = {}


def identify(points: list[tuple[float, float]], key: str) -> list[dict]:
    """Closure and shooting features within TOLERANCE_M of a line of (lat, lon) points, cached per `key`."""
    if (hit := _cache.get(key)) and time.time() - hit[0] < CACHE_TTL_S:
        return hit[1]
    results = []
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S) as client:
        for i in range(0, max(len(points) - 1, 1), CHUNK - 1):  # chunks share their end points
            results += _identify(points[i:i + CHUNK], client)
    _cache[key] = (time.time(), results)
    return results


def _identify(points: list[tuple[float, float]], client: httpx.Client) -> list[dict]:
    path = ",".join(f"[{lon:.5f},{lat:.5f}]" for lat, lon in points)
    lat, lon = points[0]
    # The tolerance is in pixels of a map image: 1000 pixels per 0.01° makes a pixel about a metre.
    params = {"geometryType": "esriGeometryPolyline", "geometry": f'{{"paths":[[{path}]]}}', "sr": 4326,
              "layers": f"all:{CLOSURES},{SHOOTING}", "tolerance": TOLERANCE_M, "returnGeometry": "false",
              "mapExtent": f"{lon},{lat},{lon + 0.01},{lat + 0.01}", "imageDisplay": "1000,1000,96", "lang": "en"}
    r = client.get(URL, params=params)
    r.raise_for_status()
    return r.json()["results"]


def closures(results: list[dict]) -> list[dict]:
    """Trail closures, once each."""
    out = {}
    for r in results:
        if r["layerBodId"] == CLOSURES:
            a = r["attributes"]
            out[r["id"]] = {"title": a.get("title_en"), "type": a.get("sperrungen_type_en"),
                            "reason": a.get("reason_en"), "duration": a.get("duration_en"),
                            "details": a.get("abstract_en"), "url": a.get("url1_link_en")}
    return list(out.values())


def shooting(results: list[dict], day: date) -> list[dict]:
    """Military danger zones, once per shooting plan, with the shooting times on `day` (empty: none published)."""
    out = {}
    for r in results:
        if r["layerBodId"] != SHOOTING:
            continue
        a = r["attributes"]
        times = []
        for i, d in enumerate(a.get("belegungsdatum") or []):
            if datetime.strptime(d, "%d.%m.%Y").date() != day or (a.get("kein_schiessen") or [False] * (i + 1))[i]:
                continue
            start, end = a["zeit_von"][i], a["zeit_bis"][i]
            times.append(f"{start[:2]}:{start[2:]}–{end[:2]}:{end[2:]}" if start and end else "all day")
        zone = out.setdefault(a.get("belplan_id") or r["id"], {"name": a.get("bezeichnung"), "url": a.get("url_en"),
                                                              "times": [], "places": []})
        zone["times"] = sorted(set(zone["times"] + times))
        if a.get("bezeichnung_ort") and a["bezeichnung_ort"] not in zone["places"]:
            zone["places"].append(a["bezeichnung_ort"])
    return list(out.values())
