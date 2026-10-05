import numpy as np
import pandas as pd
import pytest

from app.quant.features import add_targets, build_features
from app.quant.indicators import atr, bollinger, compute_all, ema, macd, rsi, sma, technical_score


def make_df(n=120, seed=1, drift=0.001):
    rng = np.random.default_rng(seed)
    close = 20 * np.cumprod(1 + rng.normal(drift, 0.015, n))
    high = close * (1 + rng.uniform(0, 0.01, n))
    low = close * (1 - rng.uniform(0, 0.01, n))
    open_ = close * (1 + rng.normal(0, 0.003, n))
    vol = rng.integers(1_000_000, 5_000_000, n).astype(float)
    idx = pd.bdate_range("2026-01-01", periods=n)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx)


def test_sma_ema_basic():
    s = pd.Series([1.0, 2, 3, 4, 5])
    assert sma(s, 3).iloc[-1] == pytest.approx(4.0)
    assert np.isnan(sma(s, 3).iloc[1])
    assert ema(s, 3).iloc[-1] > sma(s, 3).iloc[-1] - 1


def test_rsi_bounds_and_extremes():
    up = pd.Series(np.arange(1, 60, dtype=float))
    assert rsi(up).iloc[-1] == pytest.approx(100.0)
    down = pd.Series(np.arange(60, 1, -1, dtype=float))
    assert rsi(down).iloc[-1] == pytest.approx(0.0, abs=1e-6)
    r = rsi(make_df()["close"]).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_macd_hist_identity():
    m = macd(make_df()["close"]).dropna()
    assert (m["macd_hist"] - (m["macd"] - m["macd_signal"])).abs().max() < 1e-9


def test_atr_positive_and_bollinger_order():
    df = make_df()
    assert (atr(df).dropna() > 0).all()
    b = bollinger(df["close"]).dropna()
    assert (b["bb_upper"] >= b["bb_middle"]).all() and (b["bb_middle"] >= b["bb_lower"]).all()


def test_no_lookahead():
    """Indicators at t must not change when future rows are appended."""
    df = make_df(150)
    full = compute_all(df)
    part = compute_all(df.iloc[:100])
    pd.testing.assert_frame_equal(full.iloc[:100], part, check_exact=False, atol=1e-9)


def test_score_range():
    ind = compute_all(make_df(150))
    scores = [technical_score(ind.iloc[i], ind.iloc[i - 1]) for i in range(1, len(ind))]
    valid = [s for s in scores if not np.isnan(s)]
    assert valid and all(0 <= s <= 100 for s in valid)
    assert np.isnan(technical_score(ind.iloc[0]))


def test_features_and_targets():
    df = make_df(150)
    f = add_targets(df, build_features(df))
    assert f[["y_3d_2pct", "y_5d_4pct"]].tail(5).isna().any().any()  # no future data at the end
    assert f["y_3d_2pct"].dropna().isin([0.0, 1.0]).all()
    # causal features unchanged by future rows
    f2 = build_features(df.iloc[:100])
    pd.testing.assert_frame_equal(f.drop(columns=["y_3d_2pct", "y_5d_4pct"]).iloc[:100], f2, check_exact=False, atol=1e-9)
