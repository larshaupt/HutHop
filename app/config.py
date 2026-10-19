"""Project settings from huthop.toml (optional; defaults apply when the file or a key is missing)."""

import tomllib

from app.db import ROOT
from app.planner import ranking

CONFIG_PATH = ROOT / "huthop.toml"


def _read() -> dict:
    return tomllib.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}


def impressum() -> dict[str, str]:
    """Operator details for the About dialog; empty fields are dropped."""
    return {k: str(v).strip() for k, v in _read().get("impressum", {}).items() if str(v).strip()}


def ranking_weights() -> dict[str, float]:
    """Weights for the "best" tour order. Unknown keys raise, so typos don't silently do nothing."""
    weights = dict(ranking.DEFAULT_WEIGHTS)
    if configured := _read().get("ranking", {}).get("weights", {}):
        unknown = set(configured) - set(ranking.METRICS)
        if unknown:
            raise ValueError(f"{CONFIG_PATH.name}: unknown ranking weights {sorted(unknown)}")
        weights.update({k: float(v) for k, v in configured.items()})
    return weights
