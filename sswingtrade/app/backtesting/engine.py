"""
Backtest engine - FASE 4 (daily candles, long-only swing trade).

Anti look-ahead rules:
  * signal evaluated at close of day t-1 -> entry at OPEN of day t (+ slippage)
  * stop/take-profit derived from ATR known at day t-1
  * if a candle touches both stop and take-profit, STOP is assumed first (conservative)
  * gaps: if open is beyond stop/target the fill is at the open, not at the level
  * time exit at close after `max_hold_days`
Capital rules (spec): per-trade allocation = fraction of equity (R$100 of R$500 = 20%,
scales with compounding), exposure capped at equity, monthly deposit on first
trading day of month, daily-loss circuit breaker blocks new entries.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from app.backtesting.costs import CostModel
from app.backtesting.metrics import compute_metrics
from app.backtesting.strategies import Strategy, prepare_signals


@dataclass
class BacktestConfig:
    initial_capital: float = 500.0
    per_trade_allocation: float = 100.0        # R$ at initial capital
    dynamic_sizing: bool = True                # scale allocation with equity
    monthly_deposit: float = 0.0
    max_open_positions: int = 5
    max_hold_days: int = 5
    atr_stop_mult: float = 1.5
    risk_reward: float = 2.0
    max_daily_loss_pct: float = 1.5
    costs: CostModel = field(default_factory=CostModel)


def _exit_for_day(pos: dict, o: float, h: float, l: float, c: float, day_idx: int, costs: CostModel):
    """Return (raw_exit_price, reason) or None."""
    stop, tp = pos["stop"], pos["tp"]
    if o <= stop:
        return o, "stop_gap"
    if o >= tp:
        return o, "target_gap"
    if l <= stop:
        return stop, "stop"          # stop first when both touched
    if h >= tp:
        return tp, "target"
    if day_idx - pos["entry_idx"] >= pos["max_hold"]:
        return c, "time"
    return None


def run_backtest(data: Dict[str, pd.DataFrame], strategy: Strategy, cfg: Optional[BacktestConfig] = None) -> dict:
    cfg = cfg or BacktestConfig()
    costs = cfg.costs

    prepared = {t: prepare_signals(strategy, df.sort_index().astype(float)) for t, df in data.items() if not df.empty}
    ohlc = {t: df.sort_index().astype(float) for t, df in data.items() if t in prepared}
    # signals/ATR as known at the previous close
    sig_prev = {t: p["signal"].shift(1, fill_value=False) for t, p in prepared.items()}
    atr_prev = {t: p["atr_14"].shift(1) for t, p in prepared.items()}

    dates = sorted(set().union(*[set(df.index) for df in ohlc.values()])) if ohlc else []
    cash = cfg.initial_capital
    deposits = 0.0
    positions: Dict[str, dict] = {}
    trades: List[dict] = []
    equity_curve, equity_dates = [], []
    last_month = None
    day_counter = {t: {d: i for i, d in enumerate(df.index)} for t, df in ohlc.items()}

    def close_position(t, pos, raw_price, reason, d):
        nonlocal cash
        px = costs.sell_price(raw_price)
        notional = px * pos["qty"]
        fees = costs.fees(notional)
        cash += notional - fees
        gross = (px - pos["entry_price"]) * pos["qty"]
        total_fees = pos["entry_fees"] + fees
        trades.append({
            "ticker": t, "entry_date": pos["entry_date"].isoformat(), "exit_date": d.isoformat(),
            "entry_price": round(pos["entry_price"], 4), "exit_price": round(px, 4), "qty": pos["qty"],
            "gross_pnl": round(gross, 2), "fees": round(total_fees, 2), "net_pnl": round(gross - total_fees, 2),
            "return_pct": round((px / pos["entry_price"] - 1) * 100, 2),
            "holding_days": (d - pos["entry_date"]).days, "exit_reason": reason,
        })
        return gross - total_fees

    for d in dates:
        # monthly deposit on first trading day of the month
        if last_month is not None and (d.year, d.month) != last_month and cfg.monthly_deposit:
            cash += cfg.monthly_deposit
            deposits += cfg.monthly_deposit
        last_month = (d.year, d.month)

        equity_open = cash + sum(p["qty"] * p["last_close"] for p in positions.values())
        day_pnl = 0.0

        # 1) exits for positions opened on previous days
        for t in list(positions):
            df = ohlc[t]
            if d not in df.index:
                continue
            idx = day_counter[t][d]
            row = df.loc[d]
            res = _exit_for_day(positions[t], row["open"], row["high"], row["low"], row["close"], idx, costs)
            if res:
                day_pnl += close_position(t, positions.pop(t), res[0], res[1], d)
            else:
                positions[t]["last_close"] = row["close"]

        # 2) entries at today's open from yesterday's signal (best score first)
        breaker = day_pnl <= -(cfg.max_daily_loss_pct / 100) * equity_open if equity_open > 0 else True
        candidates = [t for t in ohlc if d in ohlc[t].index and t not in positions and bool(sig_prev[t].get(d, False))
                      and not np.isnan(atr_prev[t].get(d, np.nan))]
        candidates.sort(key=lambda t: -(prepared[t]["technical_score"].shift(1).get(d, 0) or 0))
        for t in candidates:
            if breaker or len(positions) >= cfg.max_open_positions:
                break
            row = ohlc[t].loc[d]
            entry = costs.buy_price(row["open"])
            equity_now = cash + sum(p["qty"] * p["last_close"] for p in positions.values())
            base = cfg.per_trade_allocation * (equity_now / cfg.initial_capital) if cfg.dynamic_sizing else cfg.per_trade_allocation
            alloc = min(base, cash)
            qty = int(alloc // (entry * (1 + costs.fee_rate)))
            if qty < 1:
                continue
            notional = entry * qty
            fees = costs.fees(notional)
            if notional + fees > cash:
                continue
            cash -= notional + fees
            atr = float(atr_prev[t].get(d))
            risk = cfg.atr_stop_mult * atr
            pos = {"qty": qty, "entry_price": entry, "entry_date": d, "entry_idx": day_counter[t][d],
                   "entry_fees": fees, "stop": entry - risk, "tp": entry + cfg.risk_reward * risk,
                   "max_hold": cfg.max_hold_days, "last_close": row["open"]}
            positions[t] = pos
            # 3) same-day exit check for the new position (stop first when both touched)
            if row["low"] <= pos["stop"] or row["high"] >= pos["tp"]:
                raw, why = (pos["stop"], "stop") if row["low"] <= pos["stop"] else (pos["tp"], "target")
                day_pnl += close_position(t, positions.pop(t), raw, why, d)
            else:
                pos["last_close"] = row["close"]

        # mark to market
        mtm = cash
        for t, p in positions.items():
            df = ohlc[t]
            if d in df.index:
                p["last_close"] = df.loc[d, "close"]
            mtm += p["qty"] * p["last_close"]
        equity_curve.append(mtm)
        equity_dates.append(d)

    # liquidate at the end
    if dates:
        for t in list(positions):
            p = positions.pop(t)
            close_position(t, p, p["last_close"], "end_of_data", dates[-1])
        if equity_curve:
            equity_curve[-1] = cash

    equity = pd.Series(equity_curve, index=pd.DatetimeIndex(equity_dates), dtype=float)
    metrics = compute_metrics(equity, trades, cfg.initial_capital, deposits)
    return {
        "strategy": strategy.name,
        "metrics": metrics,
        "trades": trades,
        "equity_curve": [{"date": d.isoformat(), "equity": round(float(v), 2)} for d, v in equity.items()],
    }
