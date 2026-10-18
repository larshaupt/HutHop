"""Monthly opening of a hut from the SAC codes — the single place for this rule.

Each hut has two 12-character strings, one digit per month (January first):
  catering      catered (staffed)                     0 = whole month, 1 = part of the month, 2 = no
  self_service  winter room / self-service part open  same digits
All 24 digits 2 means the SAC has no data for the hut ("unknown"), not that it is closed.
"""

from typing import Literal

Status = Literal["catered", "self_service", "closed", "unknown"]
Wanted = Literal["any", "catered", "self_service"]
NO_DATA = "2" * 12


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
    return {k: ("".join(c), "".join(s)) for k, (c, s) in codes.items()}
