"""The place graph the planner searches: huts and stops as nodes, stored walking connections (edges) as legs.

Loaded from SQLite and cached per trail-category limit until the data changes (data_version, bumped by triggers
in schema.sql). One Footprints cache (route footprints, for retracing and shape) is shared by the graphs of all
limits.
"""

import json
import sqlite3
from dataclasses import dataclass, field

from app.planner import opening, transit
from app.planner.geometry import Footprints


@dataclass(frozen=True)
class Leg:
    edge_id: int
    frm: int
    to: int
    time_min: int
    distance_m: int
    ascent_m: int
    descent_m: int
    max_category: int
    sac_time_min: int | None
    category_limit: int = 1  # search limit the edge was stored for; with frm/to it names the edge stably


@dataclass(frozen=True)
class Hut:
    id: int
    name: str
    lat: float
    lon: float
    e: float
    n: float
    catering: str  # 12 SAC codes, one per month: 0 yes, 1 partly, 2 no
    self_service: str
    half_board: bool
    hrs_id: int | None = None
    altitude: int | None = None
    seasons: tuple[opening.Season, ...] = ()  # opening periods from the hut website


@dataclass(frozen=True)
class Stop:
    id: int
    name: str
    lat: float
    lon: float
    e: float
    n: float
    mode: str


@dataclass
class Graph:
    huts: dict[int, Hut]
    stops: dict[int, Stop]
    legs: dict[tuple[int, int, int], Leg] = field(default_factory=dict)  # by (from, to, category limit)
    hut_out: dict[int, list[Leg]] = field(default_factory=dict)   # hut → hut
    stop_in: dict[int, list[Leg]] = field(default_factory=dict)   # stop → hut, keyed by hut
    stop_out: dict[int, list[Leg]] = field(default_factory=dict)  # hut → stop, keyed by hut
    footprints: Footprints = field(default_factory=lambda: Footprints({}))  # route lines, for retracing and shape
    timetable: transit.Timetable = field(default_factory=lambda: transit.Timetable([]))  # empty: every stop served


def load_graph(conn: sqlite3.Connection, limit: int, footprints: Footprints | None = None,
               timetable: transit.Timetable | None = None) -> Graph:
    """Huts, stops and all non-excluded edges up to a trail category limit.

    A place pair can have one edge per category limit (only stored when the harder trail is faster). All of
    them are kept as alternatives, so allowing harder trails only ever adds options."""
    huts = {}
    codes, seasons = opening.load_codes(conn), opening.load_seasons(conn)
    for r in conn.execute("SELECT id, name, lat, lon, e_lv95, n_lv95, services, hrs_id, altitude_m FROM hut_effective"):
        c, s = codes.get(r["id"], (opening.NO_DATA, opening.NO_DATA))
        huts[r["id"]] = Hut(r["id"], r["name"], r["lat"], r["lon"], r["e_lv95"], r["n_lv95"], c, s,
                            json.loads(r["services"] or "{}").get("half_board", False), r["hrs_id"], r["altitude_m"],
                            seasons.get(r["id"], ()))
    rows = conn.execute(
        "SELECT id, from_place, to_place, time_min, distance_m, ascent_m, descent_m, max_category, sac_time_min, "
        "category_limit, footprint, samples, CASE WHEN footprint IS NULL THEN geometry END AS geometry "
        "FROM edge WHERE category_limit <= ? AND excluded = 0", (limit,)).fetchall()
    stop_ids = {r["from_place"] for r in rows if r["from_place"] not in huts} | \
               {r["to_place"] for r in rows if r["to_place"] not in huts}
    stops = {}
    for chunk in range(0, len(stop_ids), 900):
        ids = list(stop_ids)[chunk:chunk + 900]
        for r in conn.execute(f"SELECT p.id, p.name, p.lat, p.lon, p.e_lv95, p.n_lv95, s.mode FROM place p "
                              f"JOIN stop s ON s.place_id = p.id WHERE p.id IN ({','.join('?' * len(ids))})", ids):
            stops[r["id"]] = Stop(r["id"], r["name"], r["lat"], r["lon"], r["e_lv95"], r["n_lv95"], r["mode"])
    footprints = footprints or Footprints({})
    footprints.packed.update({r["id"]: (r["footprint"], r["samples"]) for r in rows if r["footprint"]})
    footprints.geometry.update({r["id"]: r["geometry"] for r in rows if not r["footprint"]})
    graph = Graph(huts, stops, footprints=footprints, timetable=timetable or transit.load(conn))
    for r in rows:
        leg = Leg(r["id"], r["from_place"], r["to_place"], r["time_min"], r["distance_m"], r["ascent_m"],
                  r["descent_m"], r["max_category"], r["sac_time_min"], r["category_limit"])
        graph.legs[(leg.frm, leg.to, leg.category_limit)] = leg
        if leg.frm in huts and leg.to in huts:
            graph.hut_out.setdefault(leg.frm, []).append(leg)
        elif leg.to in huts and leg.frm in stops:
            graph.stop_in.setdefault(leg.to, []).append(leg)
        elif leg.frm in huts and leg.to in stops:
            graph.stop_out.setdefault(leg.frm, []).append(leg)
    return graph


# database file → (data version, shared footprints, {limit: graph}, shared timetable)
_cache: dict[str, tuple[int, Footprints, dict[int, Graph], transit.Timetable]] = {}


def cached_graph(conn: sqlite3.Connection, limit: int) -> Graph:
    """load_graph, reused until anything it reads changes. Graphs are read-only after loading, so requests can
    share them."""
    version = conn.execute("SELECT version FROM data_version").fetchone()[0]
    db = conn.execute("PRAGMA database_list").fetchone()["file"]
    if db not in _cache or _cache[db][0] != version:
        _cache[db] = (version, Footprints({}), {}, transit.load(conn))
    _, footprints, graphs, timetable = _cache[db]
    if limit not in graphs:
        graphs[limit] = load_graph(conn, limit, footprints, timetable)
    return graphs[limit]


def warm_up(conn: sqlite3.Connection) -> bool:
    """Load the graphs of all trail limits and unpack every footprint, unless already done for the current data.
    Returns True if it loaded anything."""
    db = conn.execute("PRAGMA database_list").fetchone()["file"]
    version = conn.execute("SELECT version FROM data_version").fetchone()[0]
    if db in _cache and _cache[db][0] == version and len(_cache[db][2]) == 3:
        return False
    for limit in (3, 2, 1):
        cached_graph(conn, limit)
    _cache[db][1].unpack_all()
    return True
