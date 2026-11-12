"""Tour editor, planner side: every edit possible on a tour, and a one-night tour to start from at a hut.

A tour is its key (start date, places stop → hut … → stop, a trail limit per day, hours), so every edit is just
another key; the API builds them with build_tour. Edits keep the tour valid as a path through stored edges (each
day an edge) and within the general settings (Rules: hardest trail, highest point, no mountain-lift stops), but not
the search's hours: a tour you design yourself needn't follow them.

  start / end   another stop for the first / last day (stops with an edge to the first / from the last hut)
  swap N        another hut for night N (connected to both neighbours; at an end, the stop is kept if it connects,
                else the closest stop that runs that day)
  insert K      a new night before night K (K = number of nights: after the last); at an end the new hut gets the
                closest usable stop, and a night added at the start moves the start a day earlier (if not past)
  remove N      without night N, if its neighbours connect (removing the first night starts a day later)
  limit D       another trail-limit variant of day D's edge (on a day with a ride: walking the whole way instead)

Days with a public transport ride (TourKey.rides) are kept as they are wherever their huts stay.
"""

from dataclasses import dataclass
from datetime import date, timedelta

from app.planner.graph import Graph, Leg
from app.planner.tours import TourKey, access_options, below, exit_options, is_lift

MAX_STOP_OPTIONS = 12  # stops offered per end, closest first


@dataclass(frozen=True)
class Edit:
    kind: str  # 'start' | 'end' | 'swap' | 'insert' | 'remove' | 'limit'
    slot: int | None  # night (swap, remove), position (insert) or day (limit), 0-based; None for start / end
    place: int | None  # the stop or hut the edit brings in (None for remove and limit)
    key: TourKey


@dataclass(frozen=True)
class Rules:
    """The general settings every offered route must keep to."""

    cap: int = 3  # hardest trail: 1 yellow, 2 red-white, 3 blue-white, 4 unofficial (OSM)
    max_altitude: int | None = None  # highest point of a route (m)
    avoid_lifts: bool = False  # no start or end stop served only by mountain lifts
    pace: float = 1.0  # for the connection times at the start and end stop

    def allows(self, leg: Leg) -> bool:
        return leg.category_limit <= self.cap and below(leg, self.max_altitude)

    def stop_ok(self, graph: Graph, stop_id: int) -> bool:
        return not (self.avoid_lifts and is_lift(graph.stops[stop_id].mode))


def leg_limit(graph: Graph, frm: int, to: int, rules: Rules = Rules()) -> int | None:
    """The trail limit to name a day frm → to with: the hardest variant of that edge the rules allow (build_tour
    takes exactly it). None if no allowed edge connects the places."""
    limits = [lim for lim in (1, 2, 3, 4) if (frm, to, lim) in graph.legs and rules.allows(graph.legs[(frm, to, lim)])]
    return max(limits) if limits else None


def edits(graph: Graph, key: TourKey, rules: Rules = Rules(), today: date | None = None) -> list[Edit]:
    """All single edits of a tour that keep to the rules (for the days they change)."""
    today = today or date.today()
    places, limits = key.places, key.limits
    huts = places[1:-1]
    n = len(huts)
    max_minutes = key.hours[1] * 60
    end_day = key.start_date + timedelta(days=n)
    hut_in: dict[int, set[int]] = {}
    for frm, legs in graph.hut_out.items():
        for leg in legs:
            if rules.allows(leg):
                hut_in.setdefault(leg.to, set()).add(frm)

    def out_huts(h: int) -> set[int]:
        return {leg.to for leg in graph.hut_out.get(h, ()) if rules.allows(leg)}

    def stops_in(h: int) -> list[Leg]:
        """Allowed stop → hut legs, closest first."""
        return sorted((x for x in graph.stop_in.get(h, ()) if rules.allows(x) and rules.stop_ok(graph, x.frm)),
                      key=lambda x: x.time_min)

    def stops_out(h: int) -> list[Leg]:
        return sorted((x for x in graph.stop_out.get(h, ()) if rules.allows(x) and rules.stop_ok(graph, x.to)),
                      key=lambda x: x.time_min)

    def access(h: int, day: date) -> list[Leg]:
        return access_options(graph, h, day, max_minutes, rules.avoid_lifts, None, rules.max_altitude, rules.pace)

    def exits(h: int, day: date) -> list[Leg]:
        return exit_options(graph, h, day, max_minutes, rules.avoid_lifts, None, rules.max_altitude, rules.pace)

    def make(kind: str, slot: int | None, place: int | None, new_places: list[int], start: date = key.start_date,
             keep: dict[int, int] | None = None) -> Edit | None:
        """An edit with these places; days whose ends are unchanged keep their limit (`keep`: new day → old day)."""
        new_limits = []
        for d in range(len(new_places) - 1):
            old = (keep or {}).get(d)
            lim = limits[old] if old is not None else leg_limit(graph, new_places[d], new_places[d + 1], rules)
            if lim is None:
                return None
            new_limits.append(lim)
        rides = {d: key.rides[old] for d, old in (keep or {}).items() if old in key.rides}  # unchanged days keep theirs
        return Edit(kind, slot, place, TourKey(start, new_places, new_limits, key.hours, rides))

    def first_stop(h: int, day: date) -> int | None:
        """The current start stop if it connects to `h`, else the closest usable one (else the closest at all)."""
        if leg_limit(graph, places[0], h, rules) is not None:
            return places[0]
        options = [x for x in access(h, day) if rules.allows(x)] or stops_in(h)
        return options[0].frm if options else None

    def last_stop(h: int, day: date) -> int | None:
        if leg_limit(graph, h, places[-1], rules) is not None:
            return places[-1]
        options = [x for x in exits(h, day) if rules.allows(x)] or stops_out(h)
        return options[0].to if options else None

    found: list[Edit | None] = []

    # Start / end stop
    for stop in dict.fromkeys(leg.frm for leg in stops_in(huts[0])):
        if stop != places[0] and len(found) < MAX_STOP_OPTIONS:
            found.append(make("start", None, stop, [stop, *places[1:]], keep={d: d for d in range(1, n + 1)}))
    count = len(found)
    for stop in dict.fromkeys(leg.to for leg in stops_out(huts[-1])):
        if stop != places[-1] and len(found) - count < MAX_STOP_OPTIONS:
            found.append(make("end", None, stop, [*places[:-1], stop], keep={d: d for d in range(n)}))

    # Swap a hut
    for i, current in enumerate(huts):
        others = set(huts) - {current}
        if n == 1:
            candidates = {x.to for legs in graph.stop_in.values() for x in legs
                          if x.frm == places[0] and rules.allows(x)} | \
                {x.frm for legs in graph.stop_out.values() for x in legs if x.to == places[-1] and rules.allows(x)}
        elif i == 0:
            candidates = hut_in.get(huts[1], set())
        elif i == n - 1:
            candidates = out_huts(huts[n - 2])
        else:
            candidates = out_huts(huts[i - 1]) & hut_in.get(huts[i + 1], set())
        for h in candidates - others - {current}:
            if h not in graph.huts:
                continue
            new = list(places)
            new[i + 1] = h
            if i == 0 and (s := first_stop(h, key.start_date)) is not None:
                new[0] = s
            if i == n - 1 and (s := last_stop(h, end_day)) is not None:
                new[-1] = s
            keep = {d: d for d in range(n + 1) if d not in (i, i + 1)}
            found.append(make("swap", i, h, new, keep=keep))

    # Insert a night before night k (k == n: after the last)
    for k in range(n + 1):
        if 0 < k < n:
            candidates = out_huts(huts[k - 1]) & hut_in.get(huts[k], set())
        elif k == 0:
            candidates = hut_in.get(huts[0], set())
        else:
            candidates = out_huts(huts[-1])
        for h in candidates - set(huts):
            if h not in graph.huts:
                continue
            if k == 0:
                start = key.start_date - timedelta(days=1)
                start = start if start >= today else key.start_date
                options = [x for x in access(h, start) if rules.allows(x)]
                if not options:
                    continue
                new = [options[0].frm, h, *places[1:]]
                found.append(make("insert", k, h, new, start, keep={d + 1: d for d in range(1, n + 1)}))
            elif k == n:
                options = [x for x in exits(h, end_day + timedelta(days=1)) if rules.allows(x)]
                if not options:
                    continue
                new = [*places[:-1], h, options[0].to]
                found.append(make("insert", k, h, new, keep={d: d for d in range(n)}))
            else:
                new = [*places[:k + 1], h, *places[k + 1:]]
                keep = {d: d for d in range(k)} | {d + 1: d for d in range(k + 1, n + 1)}
                found.append(make("insert", k, h, new, keep=keep))

    # Remove a night
    if n >= 2:
        for i in range(n):
            if i == 0:
                stop = first_stop(huts[1], key.start_date + timedelta(days=1))
                if stop is not None:
                    found.append(make("remove", 0, None, [stop, *places[2:]], key.start_date + timedelta(days=1),
                                      keep={d - 1: d for d in range(2, n + 1)}))
            elif i == n - 1:
                stop = last_stop(huts[-2], end_day - timedelta(days=1))
                if stop is not None:
                    found.append(make("remove", i, None, [*places[:-2], stop], keep={d: d for d in range(n - 1)}))
            elif leg_limit(graph, huts[i - 1], huts[i + 1], rules) is not None:
                new = [*places[:i + 1], *places[i + 2:]]
                found.append(make("remove", i, None, new, keep={d: d for d in range(i)} |
                                  {d - 1: d for d in range(i + 2, n + 1)}))

    # Another trail-limit variant of a day
    for d in range(n + 1):
        frm, to = places[d], places[d + 1]
        used = next((lim for lim in range(limits[d], 0, -1) if (frm, to, lim) in graph.legs), None)
        for lim in (1, 2, 3, 4):
            if lim != used and (frm, to, lim) in graph.legs and rules.allows(graph.legs[(frm, to, lim)]):
                new_limits = list(limits)
                new_limits[d] = lim
                rides = {k: v for k, v in key.rides.items() if k != d}
                found.append(Edit("limit", d, None, TourKey(key.start_date, list(places), new_limits, key.hours, rides)))
    return [e for e in found if e is not None]


def tour_from_hut(graph: Graph, hut_id: int, start: date, hours: tuple[float, float],
                  rules: Rules = Rules()) -> TourKey | None:
    """One night at a hut, with the closest stops (within the rules) that run on those days, else the closest."""
    max_minutes = hours[1] * 60

    def fits(legs, stop_of):
        return [x for x in legs if rules.allows(x) and rules.stop_ok(graph, stop_of(x))]
    access = fits(access_options(graph, hut_id, start, max_minutes, rules.avoid_lifts, None, rules.max_altitude,
                                 rules.pace), lambda x: x.frm) or \
        sorted(fits(graph.stop_in.get(hut_id, ()), lambda x: x.frm), key=lambda x: x.time_min)
    exits = fits(exit_options(graph, hut_id, start + timedelta(days=1), max_minutes, rules.avoid_lifts, None,
                              rules.max_altitude, rules.pace), lambda x: x.to) or \
        sorted(fits(graph.stop_out.get(hut_id, ()), lambda x: x.to), key=lambda x: x.time_min)
    if not access or not exits:
        return None
    places = [access[0].frm, hut_id, exits[0].to]
    limits = [leg_limit(graph, places[0], hut_id, rules), leg_limit(graph, hut_id, places[2], rules)]
    return TourKey(start, places, limits, hours)
