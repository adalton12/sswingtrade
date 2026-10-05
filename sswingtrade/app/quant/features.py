"""
Feature engineering for ML (FASE 5) built on top of indicators.
Targets use FUTURE data and exist only for training; never use them at inference.
"""

import numpy as np
import pandas as pd

from app.quant.indicators import compute_all


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Causal features (safe for inference)."""
    ind = compute_all(df)
    if ind.empty:
        return ind
    close = ind["close"]
    f = pd.DataFrame(index=ind.index)
    for n in (1, 2, 3, 5, 10):
        f[f"ret_{n}d"] = close.pct_change(n)
    f["dist_sma_20"] = close / ind["sma_20"] - 1
    f["dist_sma_50"] = close / ind["sma_50"] - 1
    f["ema_spread"] = ind["ema_9"] / ind["ema_21"] - 1
    f["rsi_14"] = ind["rsi_14"]
    f["macd_hist"] = ind["macd_hist"]
    f["atr_pct"] = ind["atr_14"] / close
    f["bb_pctb"] = ind["bb_pctb"]
    f["dist_vwap_20"] = close / ind["vwap_20"] - 1
    f["volume_ratio"] = ind["volume_ratio"]
    f["volatility_10d"] = close.pct_change().rolling(10).std()
    return f.replace([np.inf, -np.inf], np.nan)


def add_targets(df: pd.DataFrame, feats: pd.DataFrame) -> pd.DataFrame:
    """
    Training targets (look-ahead by design):
      y_3d_2pct: max close in next 3 days >= +2%
      y_5d_4pct: max close in next 5 days >= +4%
    Last rows without enough future data are NaN (drop them before training).
    """
    close = df.sort_index()["close"].astype(float)
    out = feats.copy()

    def fwd_max_ret(h: int) -> pd.Series:
        fut = pd.concat([close.shift(-i) for i in range(1, h + 1)], axis=1).max(axis=1, skipna=False)
        return fut / close - 1

    r3, r5 = fwd_max_ret(3), fwd_max_ret(5)
    out["y_3d_2pct"] = (r3 >= 0.02).astype(float).where(r3.notna())
    out["y_5d_4pct"] = (r5 >= 0.04).astype(float).where(r5.notna())
    return out
