import numpy as np
import pandas as pd
import pytest

from app.backtesting.costs import CostModel
from app.backtesting.engine import BacktestConfig, run_backtest
from app.backtesting.strategies import Strategy, ScoreStrategy
from tests.test_indicators import make_df


class FixedDaySignal(Strategy):
    """Signal at close of a given row position (to test execution timing)."""
    name = "fixed"

    def __init__(self, pos):
        self.pos = pos

    def signals(self, ind):
        s = pd.Series(False, index=ind.index)
        s.iloc[self.pos] = True
        return s


def flat_df(n=80, price=10.0, atr_range=0.2):
    idx = pd.bdate_range("2026-01-01", periods=n)
    return pd.DataFrame({"open": price, "high": price + atr_range / 2, "low": price - atr_range / 2,
                         "close": price, "volume": 1e6}, index=idx)


ZERO = CostModel(fee_rate=0.0, brokerage=0.0, slippage_bps=0.0)


def cfg(**kw):
    base = dict(initial_capital=1000, per_trade_allocation=500, dynamic_sizing=False, costs=ZERO)
    base.update(kw)
    return BacktestConfig(**base)


def test_entry_next_open_not_signal_close():
    df = flat_df()
    df.iloc[61, df.columns.get_loc("open")] = 10.05   # day after signal (pos 60)
    res = run_backtest({"AAA": df}, FixedDaySignal(60), cfg())
    t = res["trades"][0]
    assert t["entry_date"].startswith(str(df.index[61].date()))
    assert t["entry_price"] == pytest.approx(10.05)


def test_stop_first_when_both_hit():
    df = flat_df()
    i = 61
    df.iloc[i, df.columns.get_loc("high")] = 20.0   # touches target
    df.iloc[i, df.columns.get_loc("low")] = 1.0     # and stop
    res = run_backtest({"AAA": df}, FixedDaySignal(60), cfg())
    assert res["trades"][0]["exit_reason"] == "stop"
    assert res["trades"][0]["net_pnl"] < 0


def test_target_and_gap_fill():
    df = flat_df()
    df.iloc[62, df.columns.get_loc("high")] = 11.0
    res = run_backtest({"AAA": df}, FixedDaySignal(60), cfg())
    assert res["trades"][0]["exit_reason"] == "target" and res["trades"][0]["net_pnl"] > 0
    df2 = flat_df()
    for c in ("open", "high", "low", "close"):
        df2.iloc[62, df2.columns.get_loc(c)] = 5.0   # gap down below stop
    r2 = run_backtest({"AAA": df2}, FixedDaySignal(60), cfg())
    assert r2["trades"][0]["exit_reason"] == "stop_gap"
    assert r2["trades"][0]["exit_price"] == pytest.approx(5.0)


def test_time_exit_and_costs_reduce_pnl():
    df = flat_df()
    free = run_backtest({"AAA": df}, FixedDaySignal(60), cfg())
    costly = run_backtest({"AAA": df}, FixedDaySignal(60), cfg(costs=CostModel(slippage_bps=20, fee_rate=0.001)))
    assert free["trades"][0]["exit_reason"] == "time"
    assert free["trades"][0]["net_pnl"] == pytest.approx(0.0)
    assert costly["trades"][0]["net_pnl"] < free["trades"][0]["net_pnl"]
    assert costly["trades"][0]["fees"] > 0


def test_cash_never_negative_and_qty_integer():
    df = flat_df(price=137.0, atr_range=2.0)
    res = run_backtest({"AAA": df}, FixedDaySignal(60), cfg(per_trade_allocation=500))
    assert res["trades"][0]["qty"] == 3          # floor(500/137)
    assert min(p["equity"] for p in res["equity_curve"]) > 0


def test_dynamic_sizing_scales():
    df = flat_df(price=10.0)
    small = run_backtest({"A": df}, FixedDaySignal(60), cfg(dynamic_sizing=True, initial_capital=500, per_trade_allocation=100))
    big = run_backtest({"A": df}, FixedDaySignal(60), cfg(dynamic_sizing=True, initial_capital=500, per_trade_allocation=100, monthly_deposit=0))
    assert small["trades"][0]["qty"] == 10  # 100/10
    assert big["trades"][0]["qty"] == 10


def test_full_run_with_real_strategy_has_metrics():
    data = {"AAA": make_df(300, seed=3), "BBB": make_df(300, seed=4)}
    res = run_backtest(data, ScoreStrategy(min_score=60), BacktestConfig())
    m = res["metrics"]
    assert "max_drawdown_pct" in m and len(res["equity_curve"]) == 300
    assert m["max_drawdown_pct"] <= 0
