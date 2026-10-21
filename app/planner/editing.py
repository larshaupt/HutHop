"""Tour editor, planner side: every edit possible on a tour, and the warnings for a tour that was edited by hand.

A tour is its key (start date, places stop → hut … → stop, a trail limit per day, hours), so every edit is just
another key; the API scores them with build_tour. Edits keep the tour valid as a path through stored edges (each
day an edge) but not necessarily within the search rules: those become warnings, not refusals.

  start / end   another stop for the first / last day (stops with an edge to the first / from the last hut)
  swap N        another hut for night N (connected to both neighbours; at an end, the stop is kept if it connects,
                else the closest stop that runs that day)
  insert K      a new night before night K (K = number of nights: after the last); at an end the new hut gets the
                closest usable stop, and a night added at the start moves the start a day earlier (if not past)
  remove N      without night N, if its neighbours connect (removing the first night starts a day later)
  limit D       another trail-limit variant of day D's edge
"""

from dataclasses import dataclass
from datetime import date, timedelta

from app.planner import transit
from app.planner.graph import Graph
from app.planner.tours import Tour, TourKey, access_options, exit_options

MAX_STOP_OPTIONS = 12  # stops offered per end, closest first


@dataclass(frozen=True)
class Edit:
    kind: str  # 'start' | 'end' | 'swap' | 'insert' | 'remove' | 'limit'
    slot: int | None  # night (swap, remove), position (insert) or day (limit), 0-based; None for start / end
    place: int | None  # the stop or hut the edit brings in (None for remove and limit)
    key: TourKey


def leg_limit(graph: Graph, frm: int, to: int, cap: int) -> int | None:
    """The trail limit to name a day frm → to with: `cap` if an edge at or below it exists (build_tour then takes
    the hardest such), else the easiest harder one. None if the places aren't connected."""
    limits = [lim for lim in (1, 2, 3) if (frm, to, lim) in graph.legs]
    if not limits:
        return None
    return cap if min(limits) <= cap else min(limits)


def edits(graph: Graph, key: TourKey, cap: int, today: date | None = None) -> list[Edit]:
    """All single edits of a tour. `cap`: the planner's trail limit, used for days that change."""
    today = today or date.today()
    places, limits = key.places, key.limits
    huts = places[1:-1]
    n = len(huts)
    max_minutes = key.hours[1] * 60
    end_day = key.start_date + timedelta(days=n)
    hut_in: dict[int, set[int]] = {}
    for frm, legs in graph.hut_out.items():
        for leg in legs:
            hut_in.setdefault(leg.to, set()).add(frm)

    def out_huts(h: int) -> set[int]:
        return {leg.to for leg in graph.hut_out.get(h, ())}

    def make(kind: str, slot: int | None, place: int | None, new_places: list[int], start: date = key.start_date,
             keep: dict[int, int] | None = None) -> Edit | None:
        """An edit with these places; days whose ends are unchanged keep their limit (`keep`: new day → old day)."""
        new_limits = []
        for d in range(len(new_places) - 1):
            old = (keep or {}).get(d)
            lim = limits[old] if old is not None else leg_limit(graph, new_places[d], new_places[d + 1], cap)
            if lim is None:
                return None
            new_limits.append(lim)
        return Edit(kind, slot, place, TourKey(start, new_places, new_limits, key.hours))

    def first_stop(h: int, day: date) -> int | None:
        """The current start stop if it connects to `h`, else the closest usable one (else the closest at all)."""
        if leg_limit(graph, places[0], h, 3) is not None:
            return places[0]
        options = access_options(graph, h, day, max_minutes) or sorted(graph.stop_in.get(h, ()),
                                                                     key=lambda x: x.time_min)
        return options[0].frm if options else None

    def last_stop(h: int, day: date) -> int | None:
        if leg_limit(graph, h, places[-1], 3) is not None:
            return places[-1]
        options = exit_options(graph, h, day, max_minutes) or sorted(graph.stop_out.get(h, ()),
                                                                    key=lambda x: x.time_min)
        return options[0].to if options else None

    found: list[Edit | None] = []

    # Start / end stop
    for stop in dict.fromkeys(leg.frm for leg in sorted(graph.stop_in.get(huts[0], ()), key=lambda x: x.time_min)):
        if stop != places[0] and len(found) < MAX_STOP_OPTIONS:
            found.append(make("start", None, stop, [stop, *places[1:]], keep={d: d for d in range(1, n + 1)}))
    count = len(found)
    for stop in dict.fromkeys(leg.to for leg in sorted(graph.stop_out.get(huts[-1], ()), key=lambda x: x.time_min)):
        if stop != places[-1] and len(found) - count < MAX_STOP_OPTIONS:
            found.append(make("end", None, stop, [*places[:-1], stop], keep={d: d for d in range(n)}))

    # Swap a hut
    for i, current in enumerate(huts):
        others = set(huts) - {current}
        if n == 1:
            candidates = {leg.to for leg in graph.stop_out.get(places[0], ())} | \
                {h for h, legs in graph.stop_in.items() if any(leg.frm == places[0] for leg in legs)} | \
                {h for h, legs in graph.stop_out.items() if any(leg.to == places[-1] for leg in legs)}
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
                options = access_options(graph, h, start, max_minutes)
                if not options:
                    continue
                new = [options[0].frm, h, *places[1:]]
                found.append(make("insert", k, h, new, start, keep={d + 1: d for d in range(1, n + 1)}))
            elif k == n:
                options = exit_options(graph, h, end_day + timedelta(days=1), max_minutes)
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
            elif leg_limit(graph, huts[i - 1], huts[i + 1], cap) is not None:
                new = [*places[:i + 1], *places[i + 2:]]
                found.append(make("remove", i, None, new, keep={d: d for d in range(i)} |
                                  {d - 1: d for d in range(i + 2, n + 1)}))

    # Another trail-limit variant of a day
    for d in range(n + 1):
        frm, to = places[d], places[d + 1]
        used = next((lim for lim in range(limits[d], 0, -1) if (frm, to, lim) in graph.legs), None)
        for lim in (1, 2, 3):
            if lim != used and (frm, to, lim) in graph.legs:
                new_limits = list(limits)
                new_limits[d] = lim
                found.append(Edit("limit", d, None, TourKey(key.start_date, list(places), new_limits, key.hours)))
    return [e for e in found if e is not None]


def warnings(graph: Graph, tour: Tour, limit: int | None = None) -> list[dict]:
    """What doesn't fit the usual search rules, for tours edited by hand: [{code, day} or {code, night}], 1-based.
    Codes: too_short / too_long (day outside the hours; the first and last day only have a maximum),
    harder_trail (above `limit`), closed / full (night), no_service / late / early (start or end stop)."""
    lo, hi = tour.hours[0] * 60, tour.hours[1] * 60
    out = []
    last = len(tour.legs) - 1
    for d, leg in enumerate(tour.legs):
        if leg.time_min > hi:
            out.append({"code": "too_long", "day": d + 1})
        elif 0 < d < last and leg.time_min < lo:
            out.append({"code": "too_short", "day": d + 1})
        if limit is not None and leg.max_category > limit:
            out.append({"code": "harder_trail", "day": d + 1})
    for i, night in enumerate(tour.nights):
        state = night.availability.state if night.availability else None
        if night.status == "closed" or state == "closed":
            out.append({"code": "closed", "night": i + 1})
        elif state == "full":
            out.append({"code": "full", "night": i + 1})
    end_day = tour.start_date + timedelta(days=last)
    access = graph.timetable.day(tour.legs[0].frm, tour.start_date)
    exit_ = graph.timetable.day(tour.legs[-1].to, end_day)
    if not access.served:
        out.append({"code": "no_service", "day": 1})
    elif access.first_min is not None and access.first_min + tour.legs[0].time_min > transit.LATEST_HUT_ARRIVAL:
        out.append({"code": "late", "day": 1})
    if not exit_.served:
        out.append({"code": "no_service", "day": last + 1})
    elif exit_.last_min is not None and exit_.last_min - tour.legs[-1].time_min < transit.EARLIEST_HUT_DEPARTURE:
        out.append({"code": "early", "day": last + 1})
    return out


def tour_from_hut(graph: Graph, hut_id: int, start: date, hours: tuple[float, float], cap: int) -> TourKey | None:
    """One night at a hut, with the closest stops that run on those days (else the closest at all)."""
    max_minutes = hours[1] * 60
    access = access_options(graph, hut_id, start, max_minutes) or \
        sorted(graph.stop_in.get(hut_id, ()), key=lambda x: x.time_min)
    exits = exit_options(graph, hut_id, start + timedelta(days=1), max_minutes) or \
        sorted(graph.stop_out.get(hut_id, ()), key=lambda x: x.time_min)
    if not access or not exits:
        return None
    places = [access[0].frm, hut_id, exits[0].to]
    limits = [leg_limit(graph, places[0], hut_id, cap), leg_limit(graph, hut_id, places[2], cap)]
    return TourKey(start, places, limits, hours)
