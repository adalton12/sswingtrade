"""
Composite score 0-100 - FASE 8 (pure functions).
Weights (configurable): technical 25%, news/event 25%, ML probability 30%, volume/momentum 20%.
Missing components are treated as NEUTRAL (50) and reported in `missing` for the audit trail.
"""

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd

from app.config import settings


def ml_score(probability: Optional[float], full_scale: Optional[float] = None) -> Optional[float]:
    """Calibrated probability -> 0..100 (probability == full_scale maps to 100)."""
    if probability is None or np.isnan(probability):
        return None
    full = full_scale or settings.ML_PROB_FULL_SCALE
    return float(max(0.0, min(100.0, probability / full * 100)))


def volume_momentum_score(row: pd.Series, ret_5d: Optional[float]) -> Optional[float]:
    """0-50 from volume vs 20d average + 0-50 from 5-day return (needs both to be known)."""
    vr = row.get("volume_ratio")
    if vr is None or pd.isna(vr) or ret_5d is None or pd.isna(ret_5d):
        return None
    vol = 50 if vr >= 1.5 else 38 if vr >= 1.0 else 20 if vr >= 0.7 else 5
    mom = 50 if ret_5d > 0.04 else 45 if ret_5d > 0.02 else 35 if ret_5d > 0 else 20 if ret_5d > -0.02 else 5
    return float(vol + mom)


@dataclass
class ScoreBreakdown:
    technical: float
    news: float
    ml: float
    volume_momentum: float
    composite: float
    missing: List[str] = field(default_factory=list)


def composite_score(technical: Optional[float], news: Optional[float], ml: Optional[float],
                    volume_momentum: Optional[float]) -> ScoreBreakdown:
    missing, vals = [], {}
    for name, v in (("technical", technical), ("news", news), ("ml", ml), ("volume_momentum", volume_momentum)):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            missing.append(name)
            vals[name] = 50.0
        else:
            vals[name] = float(max(0.0, min(100.0, v)))
    w = {"technical": settings.W_TECHNICAL, "news": settings.W_NEWS, "ml": settings.W_ML,
         "volume_momentum": settings.W_VOLUME_MOMENTUM}
    total_w = sum(w.values()) or 1.0
    comp = sum(vals[k] * w[k] for k in w) / total_w
    return ScoreBreakdown(vals["technical"], vals["news"], vals["ml"], vals["volume_momentum"],
                          round(comp, 2), missing)
