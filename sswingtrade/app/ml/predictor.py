"""Inference: calibrated probabilities from a saved model - FASE 5."""

from functools import lru_cache
from typing import Dict

import joblib
import pandas as pd

from app.ml.dataset import FEATURE_COLS, build_panel


@lru_cache(maxsize=8)
def load_bundle(path: str) -> dict:
    return joblib.load(path)


def predict_latest(path: str, data: Dict[str, pd.DataFrame]) -> Dict[str, dict]:
    """Probability for the LAST available candle of each ticker (features are causal)."""
    bundle = load_bundle(path)
    panel = build_panel(data, with_targets=False)
    out = {}
    if panel.empty:
        return out
    for ticker, g in panel.groupby("ticker"):
        g = g.dropna(subset=FEATURE_COLS)
        if g.empty:
            continue
        row = g.iloc[[-1]]
        raw = float(bundle["model"].predict_proba(row[FEATURE_COLS])[:, 1][0])
        cal = float(bundle["calibrator"].predict([raw])[0])
        out[ticker] = {"date": str(row.index[0].date()), "target": bundle["target"],
                       "probability": round(cal, 4), "raw_probability": round(raw, 4)}
    return out
