"""
Strategies produce a boolean entry signal per candle computed ONLY from data up to
that candle's close. The engine executes it on the NEXT candle's open (no look-ahead).
"""

from abc import ABC, abstractmethod

import pandas as pd

from app.services.indicator_service import indicators_with_score


class Strategy(ABC):
    name = "base"

    @abstractmethod
    def signals(self, ind: pd.DataFrame) -> pd.Series:
        """ind = indicators DataFrame (compute_all + technical_score)."""


class ScoreStrategy(Strategy):
    """Enter long when composite technical score is high and RSI is not overbought."""
    name = "score"

    def __init__(self, min_score: float = 70, max_rsi: float = 75):
        self.min_score, self.max_rsi = min_score, max_rsi

    def signals(self, ind):
        return (ind["technical_score"] >= self.min_score) & (ind["rsi_14"] <= self.max_rsi)


class OversoldBounceStrategy(Strategy):
    """Mean reversion inside an uptrend: RSI oversold while above SMA50."""
    name = "oversold_bounce"

    def __init__(self, rsi_below: float = 35):
        self.rsi_below = rsi_below

    def signals(self, ind):
        return (ind["rsi_14"] < self.rsi_below) & (ind["close"] > ind["sma_50"])


class TrendBreakoutStrategy(Strategy):
    """MACD histogram turns positive with EMA9>EMA21 and above-average volume."""
    name = "trend_breakout"

    def signals(self, ind):
        turn = (ind["macd_hist"] > 0) & (ind["macd_hist"].shift(1) <= 0)
        return turn & (ind["ema_9"] > ind["ema_21"]) & (ind["volume_ratio"] >= 1.0)


STRATEGIES = {
    "score": ScoreStrategy,
    "oversold_bounce": OversoldBounceStrategy,
    "trend_breakout": TrendBreakoutStrategy,
}


def build_strategy(name: str, **params) -> Strategy:
    if name not in STRATEGIES:
        raise ValueError(f"Unknown strategy '{name}'. Available: {list(STRATEGIES)}")
    return STRATEGIES[name](**params)


def prepare_signals(strategy: Strategy, df: pd.DataFrame) -> pd.DataFrame:
    """Returns df with columns: indicators + 'signal' (bool, evaluated at close)."""
    ind = indicators_with_score(df)
    ind["signal"] = strategy.signals(ind).fillna(False).astype(bool)
    return ind
