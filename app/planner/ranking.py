"""Tour quality: sub-scores (0–1, higher is better), the "best" score (weights from huthop.toml), and sort orders.

The beam search finds valid tours; this module decides their order. METRIC_INFO describes each sub-score; the
About dialog in the app shows it with the configured weights.
"""

from dataclasses import dataclass
from typing import Literal

Sort = Literal["best", "fit", "time", "ascent", "altitude", "beds"]
METRIC_INFO = {  # key: (label, what scores 1)
    "fit": ("Day length", "Every day is close to the middle of your hours per day."),
    "fresh": ("New trail", "No stretch of trail is walked twice (more than half walked twice scores 0)."),
    "shape": ("Shape", "The tour goes from A to B or makes a real loop, instead of out and back or zig-zag."),
    "beds": ("Free beds", "Free beds confirmed on hut-reservation.org for every night (unknown counts half)."),
    "certainty": ("Opening data", "The SAC lists every night's hut as open for the whole month."),
    "even": ("Even days", "All days take about the same time."),
    "ease": ("Easy trails", "Only yellow hiking trails (red-white counts half, blue-white zero)."),
    "altitude": ("Hut altitude", "Huts at 3000 m or higher on average (1500 m and lower scores 0); a proxy for views."),
    "approach": ("Short approach", "Short first and last day: quick to the first hut and back to public transport."),
}
METRICS = tuple(METRIC_INFO)
DEFAULT_WEIGHTS = {"fit": 3, "fresh": 3, "shape": 2, "beds": 2, "certainty": 2, "even": 1, "ease": 1, "altitude": 1,
                   "approach": 1}
BED_VALUE = {"available": 1.0, "not_released": 0.5, "no_booking": 0.5, "not_checked": 0.3, "error": 0.3}


@dataclass(frozen=True)
class Metrics:
    fit: float
    fresh: float
    shape: float
    even: float
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


def metrics(legs, nights, altitudes: list[int | None], min_minutes: float, max_minutes: float,
            retraced: float = 0.0, shape: float = 1.0) -> Metrics:
    """Sub-scores of one tour. `altitudes` are the night huts' altitudes in metres; `retraced` (share of the trail
    walked twice) and `shape` come from geometry.Footprints."""
    target = (min_minutes + max_minutes) / 2
    deviation = sum(((leg.time_min - target) / target) ** 2 for leg in legs) / len(legs)
    times = [leg.time_min for leg in legs]
    mean_time = sum(times) / len(times)
    spread = (sum((t - mean_time) ** 2 for t in times) / len(times)) ** 0.5 / mean_time if mean_time else 0.0
    known = [a for a in altitudes if a]
    mean_alt = sum(known) / len(known) if known else 0.0
    return Metrics(
        fit=clip(1 - deviation ** 0.5 * 2),
        fresh=clip(1 - 2 * retraced),
        shape=clip(shape),
        even=clip(1 - 2 * spread),
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
