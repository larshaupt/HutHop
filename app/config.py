"""Project settings from huthop.toml (optional; defaults apply when the file or a key is missing)."""

import tomllib

from app.db import ROOT
from app.planner import ranking

CONFIG_PATH = ROOT / "huthop.toml"


def ranking_weights() -> dict[str, float]:
    """Weights for the "best" tour order. Unknown keys raise, so typos don't silently do nothing."""
    weights = dict(ranking.DEFAULT_WEIGHTS)
    if CONFIG_PATH.exists():
        configured = tomllib.loads(CONFIG_PATH.read_text()).get("ranking", {}).get("weights", {})
        unknown = set(configured) - set(ranking.METRICS)
        if unknown:
            raise ValueError(f"{CONFIG_PATH.name}: unknown ranking weights {sorted(unknown)}")
        weights.update({k: float(v) for k, v in configured.items()})
    return weights
