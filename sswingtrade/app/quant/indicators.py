"""
Quantitative indicators - FASE 3.
Pure pandas/numpy implementations (no DB, no I/O) so they are easy to test and
reuse in backtesting (FASE 4) and ML (FASE 5).

Input DataFrame columns: open, high, low, close, volume (index = date, ascending).
All functions are causal: value at t only uses data up to t (no look-ahead).
"""

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    # avg_loss == 0 and avg_gain > 0 -> RSI 100
    out = out.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    return out


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig})


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder's Average True Range (volatility, used for stop-loss sizing)."""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    upper, lower = mid + k * sd, mid - k * sd
    width = (upper - lower).replace(0, np.nan)
    return pd.DataFrame(
        {
            "bb_upper": upper,
            "bb_middle": mid,
            "bb_lower": lower,
            "bb_pctb": (close - lower) / width,
        }
    )


def vwap(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """
    Rolling VWAP over n daily candles using typical price (H+L+C)/3.
    (True intraday VWAP needs tick/intraday data; this is the EOD approximation.)
    """
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = (tp * df["volume"]).rolling(n, min_periods=n).sum()
    v = df["volume"].rolling(n, min_periods=n).sum().replace(0, np.nan)
    return pv / v


def volume_features(volume: pd.Series, n: int = 20) -> pd.DataFrame:
    vsma = sma(volume.astype(float), n)
    return pd.DataFrame(
        {
            "volume_sma_20": vsma,
            "volume_change": volume.astype(float).pct_change(),
            "volume_ratio": volume / vsma.replace(0, np.nan),
        }
    )


def compute_all(df: pd.DataFrame) -> pd.DataFrame:
    """Compute every indicator. Returns a DataFrame aligned with df.index."""
    if df.empty:
        return pd.DataFrame(index=df.index)
    df = df.sort_index().astype(float)
    close = df["close"]
    out = pd.DataFrame(index=df.index)
    for n in (5, 10, 20, 50):
        out[f"sma_{n}"] = sma(close, n)
    out["ema_9"] = ema(close, 9)
    out["ema_21"] = ema(close, 21)
    out["rsi_14"] = rsi(close, 14)
    out = out.join(macd(close))
    out["atr_14"] = atr(df, 14)
    out = out.join(bollinger(close))
    out["vwap_20"] = vwap(df, 20)
    out = out.join(volume_features(df["volume"]))
    out["close"] = close
    return out.replace([np.inf, -np.inf], np.nan)


def technical_score(row: pd.Series, prev: pd.Series = None) -> float:
    """
    Technical score 0-100 for swing trade LONG bias (1-5 days).
    Weights: trend 30, momentum(RSI) 20, MACD 20, Bollinger 15, volume 15.
    Returns NaN if the key indicators are not available yet (warm-up).
    """
    need = ["close", "sma_20", "ema_9", "ema_21", "rsi_14", "macd_hist", "bb_pctb"]
    if any(pd.isna(row.get(k)) for k in need):
        return float("nan")

    # Trend (30): price vs SMA20/SMA50 and EMA9 vs EMA21
    trend = 0.0
    trend += 10 if row["close"] > row["sma_20"] else 0
    if not pd.isna(row.get("sma_50")):
        trend += 10 if row["sma_20"] > row["sma_50"] else 0
    else:
        trend += 5
    trend += 10 if row["ema_9"] > row["ema_21"] else 0

    # Momentum (20): best zone 45-65 (healthy), oversold bounce <30 gets partial, overbought >75 penalised
    r = row["rsi_14"]
    if 45 <= r <= 65:
        mom = 20
    elif 30 <= r < 45 or 65 < r <= 75:
        mom = 12
    elif r < 30:
        mom = 10
    else:
        mom = 4

    # MACD (20): histogram positive (10) and rising (10)
    m = 10 if row["macd_hist"] > 0 else 0
    if prev is not None and not pd.isna(prev.get("macd_hist")):
        m += 10 if row["macd_hist"] > prev["macd_hist"] else 0
    else:
        m += 5

    # Bollinger %B (15): 0.4-0.8 constructive; <0.1 possible reversal; >1 stretched
    b = row["bb_pctb"]
    if 0.4 <= b <= 0.8:
        bb = 15
    elif b < 0.1:
        bb = 9
    elif 0.8 < b <= 1.0 or 0.1 <= b < 0.4:
        bb = 8
    else:
        bb = 3

    # Volume (15): above 20d average confirms moves
    vr = row.get("volume_ratio")
    if pd.isna(vr):
        vol = 7
    elif vr >= 1.5:
        vol = 15
    elif vr >= 1.0:
        vol = 11
    elif vr >= 0.7:
        vol = 6
    else:
        vol = 2

    return float(max(0, min(100, trend + mom + m + bb + vol)))
