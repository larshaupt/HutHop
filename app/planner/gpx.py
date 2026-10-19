"""GPX 1.1 export of a tour: one track per day, waypoints for start, huts and end.

Works on the tour JSON produced by the API (`tour_json`), so the file matches what the user sees. Route lines
are the simplified edge geometries (10 m tolerance) with swissTLM3D heights, so Komoot and the swisstopo app show
the right ascent.
"""

import re
import unicodedata
from datetime import date
from xml.sax.saxutils import escape

from app.planner import polyline

CATEGORY_NAME = {1: "hiking trail (yellow)", 2: "mountain trail (red-white)", 3: "alpine trail (blue-white)"}


def _minutes(m: int) -> str:
    return f"{m // 60}:{m % 60:02d} h"


def _wpt(lat: float, lon: float, name: str, desc: str, sym: str) -> str:
    return (f'  <wpt lat="{lat:.6f}" lon="{lon:.6f}"><name>{escape(name)}</name>'
            f"<desc>{escape(desc)}</desc><sym>{sym}</sym></wpt>")


def to_gpx(tour: dict) -> str:
    days = tour["days"]
    start, end = days[0]["from"], days[-1]["to"]
    title = f"{start['name']} → {end['name']} ({tour['nights']} nights)"
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="HutHop" xmlns="http://www.topografix.com/GPX/1/1">',
        f"  <metadata><name>{escape(title)}</name>",
        "    <desc>Planned with HutHop. Trails © swisstopo, hut data © SAC-CAS. A planning aid: check conditions, "
        "weather and official sources before you go.</desc>",
        f"    <time>{date.fromisoformat(days[0]['date']).isoformat()}T00:00:00Z</time></metadata>",
        _wpt(start["lat"], start["lon"], start["name"], "Start", "Flag, Green"),
    ]
    for d in days[:-1]:
        hut = d["to"]
        out.append(_wpt(hut["lat"], hut["lon"], hut["name"], f"Night {d['day']} ({d['date']})", "Lodging"))
    out.append(_wpt(end["lat"], end["lon"], end["name"], "End", "Flag, Red"))
    for d in days:
        desc = (f"{_minutes(d['time_min'])}, {d['distance_m'] / 1000:.1f} km, ↑ {d['ascent_m']} m, ↓ {d['descent_m']} m, "
                f"hardest: {CATEGORY_NAME[d['max_category']]}")
        name = f"Day {d['day']}: {d['from']['name']} → {d['to']['name']}"
        points = polyline.decode(d["geometry"] or "")
        heights = polyline.decode_values(d.get("elevation") or "")
        if len(heights) != len(points):  # no profile (edges built before profiles, or drawn by hand)
            heights = [None] * len(points)
        out.append(f"  <trk><name>{escape(name)}</name><desc>{escape(desc)}</desc><type>hiking</type><trkseg>")
        out.extend(f'    <trkpt lat="{lat:.5f}" lon="{lon:.5f}"><ele>{z}</ele></trkpt>' if z is not None
                   else f'    <trkpt lat="{lat:.5f}" lon="{lon:.5f}"/>' for (lat, lon), z in zip(points, heights))
        out.append("  </trkseg></trk>")
    out.append("</gpx>")
    return "\n".join(out) + "\n"


def filename(tour: dict) -> str:
    """Safe file name like 'huthop-2027-07-17-kandersteg-3n.gpx'."""
    ascii_name = unicodedata.normalize("NFKD", tour["days"][0]["from"]["name"]).encode("ascii", "ignore").decode()
    start = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return f"huthop-{tour['days'][0]['date']}-{start[:30].rstrip('-')}-{tour['nights']}n.gpx"

