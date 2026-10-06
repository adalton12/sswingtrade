"""Forecast service: gathers the evidence the system already has (ML, news, paper, backtests) and runs the provision."""

from dataclasses import replace
from datetime import date, datetime
from typing import Dict, List, Optional

import numpy as np
from sqlalchemy import select

from app.capital import service as cap
from app.forecast.provision import EdgeEvidence, SimConfig, build_forecast, estimate_edge
from app.models import BacktestRun, MLModel, PlannedDeposit, Position, PositionStatus
from app.runtime.params import PROFILES, params, universe
from app.services.logger import logger


async def gather_evidence(db, top_k: int = 5) -> dict:
    ev, info = EdgeEvidence(), {"atr_pct": None, "tickers_used": 0}
    # ---- ML: OOS quality + today's top candidates
    model = (await db.execute(select(MLModel).where(MLModel.target == params.get("ml.target"), MLModel.is_active == True)  # noqa: E712
                              .order_by(MLModel.id.desc()).limit(1))).scalar_one_or_none()
    from app.services.indicator_service import load_candles_df
    atrs, data = [], {}
    for t in universe():
        try:
            df = await load_candles_df(t, limit=400, session=db)
        except Exception:
            continue
        if len(df) >= 40:
            data[t] = df
            tr = (df["high"] - df["low"]).rolling(14).mean().iloc[-1]
            if np.isfinite(tr) and df["close"].iloc[-1] > 0:
                atrs.append(float(tr / df["close"].iloc[-1] * 100))
    if atrs:
        info["atr_pct"], info["tickers_used"] = float(np.median(atrs)), len(atrs)
    if model and model.metrics:
        m = (model.metrics.get("oos_calibrated") or {})
        ev.ml_auc, ev.ml_base_rate = m.get("auc"), m.get("base_rate")
        try:
            from app.ml.predictor import predict_latest
            preds = predict_latest(model.path, data) if data else {}
            probs = sorted((p["probability"] for p in preds.values()), reverse=True)[:top_k]
            ev.ml_top_prob = float(np.mean(probs)) if probs else None
            info["ml_model"] = model.model_id
        except Exception as e:
            logger.warning(f"forecast: ML predictions unavailable: {e}")
    # ---- news (mean of top candidates is overkill; use universe average, bounded later)
    try:
        from app.services.news_service import news_score
        sigs = []
        for t in list(data)[:20]:
            n = await news_score(t, session=db)
            if n.get("n_news"):
                sigs.append((n["news_score"] - 50) / 50.0)
        ev.news_signal = float(np.mean(sigs)) if sigs else None
    except Exception:
        pass
    # ---- paper track record + backtests
    closed = (await db.execute(select(Position).where(Position.status == PositionStatus.CLOSED))).scalars().all()
    ev.paper_trades, ev.paper_wins = len(closed), sum(1 for p in closed if float(p.net_pnl or 0) > 0)
    bts = (await db.execute(select(BacktestRun).where(BacktestRun.total_trades > 0).order_by(BacktestRun.id.desc()).limit(5))).scalars().all()
    tot = sum(b.total_trades or 0 for b in bts)
    if tot:
        ev.backtest_trades = tot
        ev.backtest_win_rate = sum((b.win_rate or 0) / 100 * (b.total_trades or 0) for b in bts) / tot
    return {"evidence": ev, "info": info}


def config_from_params(start_equity: float, values: Optional[Dict] = None, atr_pct: Optional[float] = None,
                       planned: Optional[List] = None) -> SimConfig:
    v = {k: params.get(k) for k in ("capital.per_op_pct", "capital.max_exposure_pct", "risk.max_risk_per_trade_pct",
                                    "risk.atr_stop_mult", "risk.risk_reward", "risk.max_hold_days", "limits.daily_loss_pct",
                                    "limits.weekly_loss_pct", "limits.monthly_loss_pct", "limits.daily_loss_amount",
                                    "limits.weekly_loss_amount", "limits.monthly_loss_amount", "forecast.trades_per_week")}
    v.update(values or {})
    return SimConfig(
        start_equity=start_equity, weeks=params.get("forecast.horizon_weeks"),
        trades_per_week=v["forecast.trades_per_week"], per_op_pct=v["capital.per_op_pct"],
        max_exposure_pct=v["capital.max_exposure_pct"], risk_per_trade_pct=v["risk.max_risk_per_trade_pct"],
        atr_stop_mult=v["risk.atr_stop_mult"], risk_reward=v["risk.risk_reward"],
        atr_pct=atr_pct or params.get("forecast.default_atr_pct"), hold_days=min(5, v["risk.max_hold_days"]) * 0.6,
        fee_rate=params.get("costs.fee_rate_pct") / 100, slippage_bps=params.get("costs.slippage_bps"),
        brokerage=params.get("costs.brokerage"),
        weekly_deposit=params.get("capital.weekly_deposit") if params.get("capital.weekly_deposit_enabled") else 0.0,
        monthly_deposit=params.get("capital.monthly_deposit") if params.get("capital.monthly_deposit_enabled") else 0.0,
        planned_deposits=planned or [], daily_loss_pct=v["limits.daily_loss_pct"], weekly_loss_pct=v["limits.weekly_loss_pct"],
        monthly_loss_pct=v["limits.monthly_loss_pct"], daily_loss_amount=v["limits.daily_loss_amount"],
        weekly_loss_amount=v["limits.weekly_loss_amount"], monthly_loss_amount=v["limits.monthly_loss_amount"],
        simulations=params.get("forecast.simulations"))


async def _planned(db, acc) -> List:
    rows = (await db.execute(select(PlannedDeposit).where(PlannedDeposit.account_id == acc.id,
                                                          PlannedDeposit.applied == False))).scalars().all()  # noqa: E712
    today, out = date.today(), []
    for r in rows:
        w = (r.due_date.date() - today).days // 7 + 1
        if 1 <= w <= 260:
            out.append((w, float(r.amount)))
    return out


async def run_forecast(db, profile: Optional[str] = None, overrides: Optional[Dict] = None,
                       start_equity: Optional[float] = None, weeks: Optional[int] = None) -> dict:
    await params.refresh(db)
    acc = await cap.get_or_create_account(db)
    g = await gather_evidence(db)
    start = start_equity if start_equity is not None else cap.equity(acc)
    vals = dict(PROFILES[profile]["values"]) if profile else {}
    vals.update(overrides or {})
    c = config_from_params(start, vals, g["info"]["atr_pct"], await _planned(db, acc))
    if weeks:
        c = replace(c, weeks=weeks)
    edge = estimate_edge(g["evidence"], c.risk_reward, params.get("forecast.ml_shrink_max"), params.get("forecast.evidence_k"))
    out = build_forecast(c, edge)
    out["profile"] = profile or params.get("risk.profile")
    out["data"] = g["info"] | {"paper_trades": g["evidence"].paper_trades, "backtest_trades": g["evidence"].backtest_trades}
    return out


async def compare_profiles(db, start_equity: Optional[float] = None, weeks: Optional[int] = None) -> dict:
    out = {}
    for name in PROFILES:
        r = await run_forecast(db, profile=name, start_equity=start_equity, weeks=weeks)
        mc = r["monte_carlo"]
        out[name] = {"label": PROFILES[name]["label"], "scenarios": r["scenarios"], "final": mc["final"],
                     "expected_profit": mc["expected_profit"], "prob_loss": mc["prob_loss"],
                     "prob_drawdown_20": mc["prob_drawdown_20"], "max_drawdown_p90": mc["max_drawdown_p90"],
                     "limit_hit_paths_pct": mc["limit_hit_paths_pct"], "total_deposited": mc["total_deposited"],
                     "params": PROFILES[name]["values"]}
    return {"profiles": out, "weeks": weeks or params.get("forecast.horizon_weeks"), "disclaimer": r["disclaimer"]}
