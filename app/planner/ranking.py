"""Tour quality: sub-scores (0–1, higher is better), the user-weighted "best" score, and sort orders.

The beam search finds valid tours; this module decides their order. Sub-scores:
  fit        days close to the middle of the requested hour range
  beds       free beds confirmed on hut-reservation.org (0.5 when unknown)
  certainty  nights with full-month SAC opening data (no unknown or partly-open months)
  ease       easier trails (yellow 1, red-white 0.5, blue-white 0)
  altitude   higher huts (1500 m → 0, 3000 m and above → 1), a simple proxy for scenery
  approach   short first and last day (getting to the first hut and back to public transport)
"""

from dataclasses import dataclass
from typing import Literal

Sort = Literal["best", "fit", "time", "ascent", "altitude", "beds"]
METRICS = ("fit", "beds", "certainty", "ease", "altitude", "approach")
DEFAULT_WEIGHTS = {"fit": 3, "beds": 2, "certainty": 2, "ease": 1, "altitude": 1, "approach": 1}
BED_VALUE = {"available": 1.0, "not_released": 0.5, "no_booking": 0.5, "not_checked": 0.3, "error": 0.3}


@dataclass(frozen=True)
class Metrics:
    fit: float
    beds: float
    certainty: float
    ease: float
    altitude: float
    approach: float
    time_min: int
    ascent_m: int
    mean_altitude: float

    def best(self, weights: dict[str, float]) -> float:
        total = sum(weights.get(m, 0) for m in METRICS)
        if not total:
            return self.fit
        return sum(weights.get(m, 0) * getattr(self, m) for m in METRICS) / total


def clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def metrics(legs, nights, altitudes: list[int | None], min_minutes: float, max_minutes: float) -> Metrics:
    """Sub-scores of one tour. `altitudes` are the night huts' altitudes in metres."""
    target = (min_minutes + max_minutes) / 2
    deviation = sum(((leg.time_min - target) / target) ** 2 for leg in legs) / len(legs)
    known = [a for a in altitudes if a]
    mean_alt = sum(known) / len(known) if known else 0.0
    return Metrics(
        fit=clip(1 - deviation ** 0.5 * 2),
        beds=sum(BED_VALUE.get(n.availability.state, 0.5) if n.availability else 0.5 for n in nights) / len(nights),
        certainty=sum(1.0 for n in nights if n.status != "unknown" and not n.partial) / len(nights),
        ease=1 - sum(leg.max_category - 1 for leg in legs) / (2 * len(legs)),
        altitude=clip((mean_alt - 1500) / 1500),
        approach=clip(1 - (legs[0].time_min + legs[-1].time_min) / (2 * max_minutes)),
        time_min=sum(leg.time_min for leg in legs),
        ascent_m=sum(leg.ascent_m for leg in legs),
        mean_altitude=mean_alt,
    )


def sort_key(sort: Sort, weights: dict[str, float]):
    """Key function on Metrics; smaller sorts first."""
    return {
        "best": lambda m: -m.best(weights),
        "fit": lambda m: -m.fit,
        "time": lambda m: m.time_min,
        "ascent": lambda m: m.ascent_m,
        "altitude": lambda m: -m.mean_altitude,
        "beds": lambda m: (-m.beds, -m.best(weights)),
    }[sort]
