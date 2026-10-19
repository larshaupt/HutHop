"""What the About dialog shows: how "best" is scored (with the configured weights) and the impressum."""

from fastapi import APIRouter

from app import config
from app.planner import ranking

router = APIRouter(prefix="/api", tags=["info"])


@router.get("/info")
def info() -> dict:
    weights = config.ranking_weights()
    total = sum(weights.values()) or 1
    return {
        "metrics": [{"key": k, "label": label, "description": text, "weight": weights[k],
                     "share": round(weights[k] / total, 3)} for k, (label, text) in ranking.METRIC_INFO.items()],
        "impressum": config.impressum(),
    }
