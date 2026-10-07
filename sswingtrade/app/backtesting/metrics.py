"""Performance metrics for backtests."""

from typing import Optional

import numpy as np
import pandas as pd


def time_weighted_returns(equity: pd.Series, flows: Optional[pd.Series] = None) -> pd.Series:
    """
    Daily returns of the STRATEGY, free of the effect of deposits: a deposit is not performance.
    `flows[t]` = cash added at the start of day t (before trading), so r_t = E_t / (E_{t-1} + flow_t) - 1.
    Without flows this is the plain pct_change of the equity curve.
    """
    if flows is None:
        return equity.pct_change().dropna()
    f = flows.reindex(equity.index).fillna(0.0)
    base = equity.shift(1) + f
    return (equity / base - 1).iloc[1:].replace([np.inf, -np.inf], np.nan).dropna()


def compute_metrics(equity: pd.Series, trades: list, initial_capital: float, deposits: float = 0.0,
                    flows: Optional[pd.Series] = None) -> dict:
    out = {"trades": len(trades)}
    if equity.empty:
        return out
    invested = initial_capital + deposits
    final = float(equity.iloc[-1])
    out["initial_capital"] = round(initial_capital, 2)
    out["total_deposits"] = round(deposits, 2)
    out["final_equity"] = round(final, 2)
    out["net_profit"] = round(final - invested, 2)
    out["total_return_pct"] = round((final / invested - 1) * 100, 2) if invested else None

    # Risk metrics use time-weighted returns so deposits do not look like profit nor hide drawdowns
    rets = time_weighted_returns(equity, flows)
    out["twr_return_pct"] = round(float(((1 + rets).prod() - 1) * 100), 2) if len(rets) else 0.0
    if len(rets) > 1 and rets.std() > 0:
        out["sharpe"] = round(float(rets.mean() / rets.std() * np.sqrt(252)), 2)
        down = rets[rets < 0]
        out["sortino"] = round(float(rets.mean() / down.std() * np.sqrt(252)), 2) if len(down) > 1 and down.std() > 0 else None
    else:
        out["sharpe"] = out["sortino"] = None
    index = pd.concat([pd.Series([1.0]), (1 + rets).cumprod().reset_index(drop=True)], ignore_index=True)   # value of R$1
    out["max_drawdown_pct"] = round(float(((index / index.cummax()) - 1).min() * 100), 2)

    if trades:
        pnl = np.array([t["net_pnl"] for t in trades])
        wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
        out["winning_trades"] = int((pnl > 0).sum())
        out["losing_trades"] = int((pnl <= 0).sum())
        out["win_rate_pct"] = round(float((pnl > 0).mean() * 100), 2)
        out["avg_trade"] = round(float(pnl.mean()), 2)
        out["avg_win"] = round(float(wins.mean()), 2) if len(wins) else 0.0
        out["avg_loss"] = round(float(losses.mean()), 2) if len(losses) else 0.0
        gl = abs(losses.sum())
        out["profit_factor"] = round(float(wins.sum() / gl), 2) if gl > 0 else None
        out["total_fees"] = round(float(sum(t["fees"] for t in trades)), 2)
        out["avg_holding_days"] = round(float(np.mean([t["holding_days"] for t in trades])), 2)
    return out
