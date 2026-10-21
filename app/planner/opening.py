"""Monthly opening of a hut from the SAC codes — the single place for this rule.

Each hut has two 12-character strings, one digit per month (January first):
  catering      catered (staffed)                     0 = whole month, 1 = part of the month, 2 = no
  self_service  winter room / self-service part open  same digits
All 24 digits 2 means the SAC has no data for the hut ("unknown"), not that it is closed.

Opening periods from the hut website (hut_season, app/ingest/scrape_huts.py) refine this to days:
- an exceptional closure ("closed" with a year) makes the hut unusable on those days;
- for each way of staying (catered, self-service) that the website gives dates for, those dates decide within
  months the SAC marks as "partly"; where the SAC says "whole month" but the website says closed, the sources
  disagree (or the model misread the site) and the night stays possible but uncertain; for a way of staying
  without website dates, the SAC monthly code applies as before;
- huts without SAC data get monthly codes derived from the website dates, so the map and filters show them.
A night is uncertain ("partial") within BOUNDARY_DAYS of a season start or end that is vague, not found word for
word on the website, or published for another year.
"""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

Status = Literal["catered", "self_service", "closed", "unknown"]
Wanted = Literal["any", "catered", "self_service"]
NO_DATA = "2" * 12
BOUNDARY_DAYS = 14


@dataclass(frozen=True)
class Season:
    kind: Literal["catered", "self_service", "closed"]
    start: tuple[int, int]  # (month, day), inclusive
    end: tuple[int, int]    # (month, day), inclusive; before start = over New Year
    year: int | None        # year the dates were published for; None = every year
    vague: bool             # approximate dates or quote not found on the website


def _span(season: Season, year: int) -> tuple[date, date]:
    """The season's days when it starts in `year` (29 February becomes the 28th in other years)."""
    def day(md: tuple[int, int], y: int) -> date:
        return date(y, md[0], min(md[1], 28) if md == (2, 29) else md[1])
    start = day(season.start, year)
    end = day(season.end, year + (season.end < season.start))
    return start, end


def _match(season: Season, day: date) -> tuple[date, date] | None:
    """(start, end) of the season's occurrence that contains the day, if any."""
    for year in (day.year - 1, day.year):
        start, end = _span(season, year)
        if start <= day <= end:
            return start, end
    return None


def _for_year(seasons: tuple[Season, ...], kind: str, year: int) -> tuple[tuple[Season, ...], bool]:
    """The seasons of one kind that apply to a year, and whether they were published for another year."""
    same = [s for s in seasons if s.kind == kind]
    for wanted in (lambda s: s.year == year, lambda s: s.year is None):
        if hits := [s for s in same if wanted(s)]:
            return tuple(hits), False
    latest = max((s.year for s in same), default=None)
    return tuple(s for s in same if s.year == latest), latest is not None


def on_day(catering: str, self_service: str, seasons: tuple[Season, ...], day: date,
           wanted: "Wanted") -> tuple["Status", bool] | None:
    """Like `usable`, for one day, with the website seasons applied (see the module docstring)."""
    if any(s.kind == "closed" and _span(s, s.year)[0] <= day <= _span(s, s.year)[1] for s in seasons):
        return None
    if not any(s.kind != "closed" for s in seasons):
        return usable(catering, self_service, day.month, wanted)
    for status, codes in (("catered", catering), ("self_service", self_service)):
        if wanted not in ("any", status):
            continue
        current, other_year = _for_year(seasons, status, day.year)
        sac = codes[day.month - 1] if has_data(catering, self_service) else "2"
        if current:
            for s in current:
                if span := _match(s, day):
                    near_edge = min(day - span[0], span[1] - day) < timedelta(days=BOUNDARY_DAYS)
                    return status, near_edge and (s.vague or other_year)
            if sac == "0":
                return status, True
        elif sac in "01":
            return status, sac == "1"
    return None


def codes_from_seasons(seasons: tuple[Season, ...], year: int | None = None) -> tuple[str, str]:
    """Monthly codes from website seasons (as they apply to `year`, default this year): 0 whole month, 1 part,
    2 none."""
    year = year or date.today().year

    def codes(kind: str) -> str:
        current, _ = _for_year(seasons, kind, year)
        out = ""
        for month in range(1, 13):
            days = [date(year, month, d) for d in range(1, calendar.monthrange(year, month)[1] + 1)]
            open_days = sum(any(_match(s, d) for s in current) for d in days)
            out += "0" if open_days == len(days) else "1" if open_days else "2"
        return out
    return codes("catered"), codes("self_service")


def has_data(catering: str, self_service: str) -> bool:
    return "0" in catering + self_service or "1" in catering + self_service


def month_status(catering: str, self_service: str, month: int) -> Status:
    """How the hut is open in a month (1–12); catered wins over self-service."""
    if not has_data(catering, self_service):
        return "unknown"
    if catering[month - 1] in "01":
        return "catered"
    if self_service[month - 1] in "01":
        return "self_service"
    return "closed"


def is_partial(catering: str, self_service: str, month: int) -> bool:
    """Open only part of the month in the way that `month_status` reports."""
    status = month_status(catering, self_service, month)
    code = {"catered": catering, "self_service": self_service}.get(status)
    return code is not None and code[month - 1] == "1"


def usable(catering: str, self_service: str, month: int, wanted: Wanted) -> tuple[Status, bool] | None:
    """(status, partial) if the hut can be used in that month the wanted way, else None. Unknown huts return
    ('unknown', False); callers decide whether to accept them."""
    if not has_data(catering, self_service):
        return "unknown", False
    for status, codes in (("catered", catering), ("self_service", self_service)):
        if wanted in ("any", status) and codes[month - 1] in "01":
            return status, codes[month - 1] == "1"
    return None


def load_codes(conn, hut_id: int | None = None) -> dict[int, tuple[str, str]]:
    """hut id → (catering, self_service) code strings, with manual overrides applied."""
    where, params = ("WHERE hut_id = ?", (hut_id,)) if hut_id is not None else ("", ())
    codes: dict[int, tuple[list[str], list[str]]] = {}
    for r in conn.execute(f"SELECT hut_id, month, catering, self_service FROM hut_month_effective {where} "
                          "ORDER BY hut_id, month", params):
        cat, ss = codes.setdefault(r["hut_id"], ([], []))
        cat.append(str(r["catering"] if r["catering"] is not None else 2))
        ss.append(str(r["self_service"] if r["self_service"] is not None else 2))
    result = {k: ("".join(c), "".join(s)) for k, (c, s) in codes.items()}
    # Huts without SAC opening data: monthly codes from the website seasons
    for hut, seasons in load_seasons(conn, hut_id).items():
        if hut in result and not has_data(*result[hut]) and any(s.kind != "closed" for s in seasons):
            result[hut] = codes_from_seasons(seasons)
    return result


def load_seasons(conn, hut_id: int | None = None) -> dict[int, tuple[Season, ...]]:
    """hut id → opening periods from the hut website."""
    where, params = ("WHERE hut_id = ?", (hut_id,)) if hut_id is not None else ("", ())
    seasons: dict[int, list[Season]] = {}
    for r in conn.execute(f"SELECT * FROM hut_season {where} ORDER BY hut_id, start_md", params):
        start, end = (tuple(int(x) for x in r[col].split("-")) for col in ("start_md", "end_md"))
        seasons.setdefault(r["hut_id"], []).append(
            Season(r["kind"], start, end, r["year"], bool(r["approximate"]) or r["verified"] == 0))
    return {k: tuple(v) for k, v in seasons.items()}
