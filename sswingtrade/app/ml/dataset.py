"""Panel dataset (all tickers stacked) for supervised learning - FASE 5."""

from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from app.quant.features import add_targets, build_features

TARGETS = {"y_3d_2pct": "P(alta >= 2% em ate 3 dias)", "y_5d_4pct": "P(alta >= 4% em ate 5 dias)"}
FEATURE_COLS = [
    "ret_1d", "ret_2d", "ret_3d", "ret_5d", "ret_10d", "dist_sma_20", "dist_sma_50",
    "ema_spread", "rsi_14", "macd_hist", "atr_pct", "bb_pctb", "dist_vwap_20",
    "volume_ratio", "volatility_10d",
]


def build_panel(data: Dict[str, pd.DataFrame], with_targets: bool = True) -> pd.DataFrame:
    """Rows indexed by (date) with columns: ticker, features, [targets]. Sorted by date."""
    parts: List[pd.DataFrame] = []
    for ticker, df in data.items():
        if df is None or len(df) < 80:
            continue
        df = df.sort_index().astype(float)
        f = build_features(df)
        if with_targets:
            f = add_targets(df, f)
        f["ticker"] = ticker
        parts.append(f)
    if not parts:
        return pd.DataFrame()
    panel = pd.concat(parts).sort_index(kind="stable")
    panel.index.name = "date"
    return panel.replace([np.inf, -np.inf], np.nan)


def clean_for_training(panel: pd.DataFrame, target: str) -> pd.DataFrame:
    cols = FEATURE_COLS + [target]
    return panel.dropna(subset=cols)


def split_xy(panel: pd.DataFrame, target: str) -> Tuple[pd.DataFrame, pd.Series]:
    return panel[FEATURE_COLS], panel[target].astype(int)
