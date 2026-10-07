"""
Decision pipeline + daily cycle - FASE 8.

Flow (after market close, once candles/news/ML are fresh):
  1. PaperBroker fills yesterday's approved orders at today's open and manages stops/targets
  2. circuit breaker is refreshed from realised P&L
  3. every ticker gets a composite score -> Risk Engine -> (approved) PENDING order for the next open
  4. 100% of decisions (approved or not) are stored in decision_log

No LLM is called here: news/event scores come from the nightly batch already stored in the DB.
"""

import math
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd
from sqlalchemy import select

from app.broker.base import OrderRequest
from app.broker.paper import PaperBroker
from app.capital import service as cap
from app.capital.rules import CapitalRules
from app.runtime.params import params, universe
from app.ml.predictor import predict_latest
from app.models import DecisionLog, MLModel, TickerInfo
from app.risk.engine import AccountState, RiskEngine, TradeRequest
from app.risk.scoring import composite_score, ml_score, volume_momentum_score
from app.services.indicator_service import indicators_with_score, load_candles_df
from app.services.logger import logger
from app.services.market_clock import business_days_stale, market_now
from app.services.news_service import news_score

MIN_CANDLES = 70


def _today():
    """Market-local date (seam: tests replace it to run on fixed historical candles)."""
    return market_now().date()


def _nan_to_none(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


async def _active_ml(db) -> Optional[MLModel]:
    return (await db.execute(select(MLModel).where(MLModel.target == params.get("ml.target"), MLModel.is_active == True)
                             .order_by(MLModel.trained_at.desc()))).scalars().first()


async def gather_inputs(db, tickers: List[str]) -> Dict[str, dict]:
    """Technical row + volume/momentum per ticker from stored candles (causal, as of last candle)."""
    out = {}
    for t in tickers:
        df = await load_candles_df(t, 400, session=db)
        if len(df) < MIN_CANDLES:
            continue
        ind = indicators_with_score(df)
        last = ind.iloc[-1]
        ret5 = float(df["close"].iloc[-1] / df["close"].iloc[-6] - 1) if len(df) > 6 else None
        out[t] = {"df": df, "signal_date": df.index[-1].to_pydatetime(), "price": float(df["close"].iloc[-1]),
                  "atr": _nan_to_none(last.get("atr_14")), "technical": _nan_to_none(last.get("technical_score")),
                  "volume_momentum": volume_momentum_score(last, ret5), "rsi": _nan_to_none(last.get("rsi_14"))}
    return out


async def run_decisions(db, acc, tickers: Optional[List[str]] = None, dry_run: bool = False,
                        advocate_scores: Optional[Dict[str, float]] = None) -> dict:
    await params.refresh(db)
    tickers = [t.upper() for t in (tickers or universe())]
    advocate_scores = {k.upper(): v for k, v in (advocate_scores or {}).items()}
    inputs = await gather_inputs(db, tickers)
    if not inputs:
        return {"signal_date": None, "evaluated": 0, "approved": 0, "decisions": [],
                "note": "No ticker has enough candles. Run market sync first."}

    signal_date = max(v["signal_date"] for v in inputs.values())
    fresh = {t: v for t, v in inputs.items() if v["signal_date"] == signal_date}

    # ML probabilities (batch, one model load)
    ml_model, ml_preds = await _active_ml(db), {}
    if ml_model:
        try:
            ml_preds = predict_latest(ml_model.path, {t: v["df"] for t, v in fresh.items()})
        except Exception as e:  # missing artifact etc. -> neutral ML, logged as missing
            logger.warning(f"ML prediction unavailable: {e}")

    bands = {r.ticker: (r.min_price, r.max_price) for r in (await db.execute(select(TickerInfo))).scalars().all()}

    scored = []
    for t, v in fresh.items():
        news = await news_score(t, session=db)
        prob = ml_preds.get(t, {}).get("probability")
        bd = composite_score(v["technical"], news["news_score"] if news["n_news"] else None,
                             ml_score(prob), v["volume_momentum"])
        scored.append((t, v, bd, news, prob))
    scored.sort(key=lambda x: -x[2].composite)

    st0 = await cap.exposure_and_spent(db, acc, signal_date)
    cb = await cap.refresh_circuit_breaker(db, acc)
    state = AccountState(equity=cap.equity(acc), cash=float(acc.available_balance), exposure=st0["exposure"],
                         open_positions=st0["open_positions"], open_tickers=set(st0["open_tickers"]),
                         daily_spent=st0["daily_spent"], entries_today=st0["entries_today"],
                         circuit_breaker=cb["tripped"], daily_pnl=cb["daily_pnl"],
                         remaining_loss_allowance=cb["remaining_allowance"], tripped_periods=cb["tripped_periods"])

    engine = RiskEngine()
    min_score = engine.cfg.min_score
    data_age = business_days_stale(signal_date.date(), _today())
    if engine.cfg.max_data_age_bdays and data_age > engine.cfg.max_data_age_bdays:
        logger.warning(f"Stale market data: last candle {signal_date.date()} is {data_age} business days old "
                       f"(max {engine.cfg.max_data_age_bdays}); every entry will be refused")
    n_cands = sum(1 for s in scored if s[2].composite >= min_score)
    split = max(1, min(params.get("capital.max_entries_per_day"), n_cands))
    broker = PaperBroker(db, acc)

    decisions, approved_n = [], 0
    for t, v, bd, news, prob in scored:
        if not dry_run:
            exists = (await db.execute(select(DecisionLog.id).where(
                DecisionLog.account_id == acc.id, DecisionLog.ticker == t, DecisionLog.signal_date == signal_date))).first()
            if exists:
                decisions.append({"ticker": t, "skipped": "already decided for this signal_date"})
                continue
        lo, hi = bands.get(t, (None, None))
        dec = engine.evaluate(
            TradeRequest(ticker=t, side="buy", price=v["price"], atr=v["atr"], composite_score=bd.composite,
                         advocate_counter_score=advocate_scores.get(t), data_age_days=data_age,
                         min_price=float(lo) if lo else None, max_price=float(hi) if hi else None, split=split),
            state)
        order_id = None
        meta = {"technical_score": bd.technical, "news_score": bd.news, "ml_probability": prob,
                "composite_score": bd.composite, "volume_momentum_score": bd.volume_momentum,
                "ml_model_id": ml_model.model_id if ml_model else None, "missing_components": bd.missing}
        if dec.approved and not dry_run:
            order = await broker.submit_order(
                OrderRequest(t, "buy", dec.qty, v["price"], signal_date, dec.stop_price, dec.take_profit_price,
                             v["atr"], meta), dec.approval)
            order_id = order.id
            state.exposure += dec.allocation
            state.daily_spent += dec.allocation
            state.entries_today += 1
            state.open_positions += 1
            state.open_tickers.add(t)
            state.cash -= dec.allocation        # conservative: cash is reserved for the pending fill
        approved_n += int(dec.approved)
        if not dry_run:
            db.add(DecisionLog(account_id=acc.id, ticker=t, signal_date=signal_date, technical_score=bd.technical,
                               news_score=bd.news, ml_probability=prob, volume_momentum_score=bd.volume_momentum,
                               composite_score=bd.composite, approved=dec.approved, reasons=dec.reasons,
                               checks=dec.checks, order_id=order_id, ml_model_id=meta["ml_model_id"],
                               details={"qty": dec.qty, "stop": dec.stop_price, "take_profit": dec.take_profit_price,
                                        "rr": dec.risk_reward, "allocation": dec.allocation, "price": v["price"],
                                        "missing": bd.missing, "news": news, "split": split}))
        decisions.append({"ticker": t, "composite": bd.composite, "approved": dec.approved, "qty": dec.qty,
                          "price": v["price"], "stop": dec.stop_price, "take_profit": dec.take_profit_price,
                          "rr": dec.risk_reward, "allocation": dec.allocation, "reasons": dec.reasons,
                          "scores": {"technical": bd.technical, "news": bd.news, "ml": bd.ml,
                                     "volume_momentum": bd.volume_momentum}, "missing": bd.missing,
                          "order_id": order_id})
    if not dry_run:
        await db.commit()
    return {"signal_date": signal_date.date().isoformat(), "data_age_bdays": data_age, "dry_run": dry_run,
            "evaluated": len(scored), "approved": approved_n, "ml_model": ml_model.model_id if ml_model else None,
            "stale_skipped": sorted(set(inputs) - set(fresh)), "decisions": decisions}


async def run_daily_cycle(db, acc=None, tickers: Optional[List[str]] = None,
                          advocate_scores: Optional[Dict[str, float]] = None) -> dict:
    await params.refresh(db)
    acc = acc or await cap.get_or_create_account(db)
    broker = PaperBroker(db, acc)
    fills = await broker.process_pending()
    exits = await broker.manage_positions()
    cb = await cap.refresh_circuit_breaker(db, acc)
    decisions = await run_decisions(db, acc, tickers, advocate_scores=advocate_scores)
    logger.info(f"Daily cycle: {len(fills)} fill events, {len(exits)} exits, "
                f"{decisions['approved']}/{decisions['evaluated']} approved, circuit_breaker={cb['tripped']}")
    return {"fills": fills, "exits": exits, "circuit_breaker": cb, "decisions": decisions,
            "equity": cap.equity(acc)}
