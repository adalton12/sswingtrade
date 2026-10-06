"""Paper-trading routes - FASE 8. Every order path goes through the Risk Engine."""

from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.capital import service as cap
from app.config import settings
from app.runtime.params import params
from app.database import get_db
from app.execution.pipeline import run_daily_cycle, run_decisions
from app.models import DecisionLog, Order, Position, PositionStatus

router = APIRouter()


class CycleRequest(BaseModel):
    tickers: Optional[List[str]] = None
    # PRE-COMPUTED devil's-advocate counter scores (0-100) from the batch; the engine never calls the LLM
    advocate_scores: Optional[Dict[str, float]] = None


def _f(v):
    return float(v) if v is not None else None


@router.get("/status")
async def status() -> dict:
    g = params.get
    return {"mode": "paper", "paper_enabled": g("risk.paper_enabled"), "real_enabled": settings.ENABLE_REAL_TRADING,
            "profile": g("risk.profile"), "min_score": g("risk.min_score"), "weights": {
                "technical": g("score.w_technical"), "news": g("score.w_news"), "ml": g("score.w_ml"),
                "volume_momentum": g("score.w_volume")},
            "risk": {"atr_stop_mult": g("risk.atr_stop_mult"), "min_rr": g("risk.min_rr"),
                     "max_open_positions": g("capital.max_open_positions")},
            "loss_limits": {"daily_pct": g("limits.daily_loss_pct"), "weekly_pct": g("limits.weekly_loss_pct"),
                            "monthly_pct": g("limits.monthly_loss_pct")}}


@router.post("/preview")
async def preview(req: CycleRequest, db: AsyncSession = Depends(get_db)) -> dict:
    """Dry run: scores + Risk Engine verdict for each ticker. Creates NO orders and writes NO log."""
    acc = await cap.get_or_create_account(db)
    return await run_decisions(db, acc, req.tickers, dry_run=True, advocate_scores=req.advocate_scores)


@router.post("/cycle")
async def cycle(req: CycleRequest, db: AsyncSession = Depends(get_db)) -> dict:
    """Run the daily cycle now: fills -> exits -> circuit breaker -> new decisions (pending orders)."""
    acc = await cap.get_or_create_account(db)
    return await run_daily_cycle(db, acc, req.tickers, req.advocate_scores)


@router.get("/positions")
async def positions(status: str = Query("open", pattern="^(open|closed|all)$"), limit: int = Query(100, ge=1, le=500),
                    db: AsyncSession = Depends(get_db)) -> dict:
    acc = await cap.get_or_create_account(db)
    q = select(Position).where(Position.account_id == acc.id)
    if status != "all":
        q = q.where(Position.status == (PositionStatus.OPEN if status == "open" else PositionStatus.CLOSED))
    rows = (await db.execute(q.order_by(Position.entry_date.desc()).limit(limit))).scalars().all()
    return {"count": len(rows), "positions": [{
        "id": p.id, "ticker": p.ticker, "qty": p.quantity, "entry_price": _f(p.entry_price),
        "entry_date": p.entry_date.isoformat(), "stop": _f(p.stop_loss_price), "take_profit": _f(p.take_profit_price),
        "exit_price": _f(p.exit_price), "exit_date": p.exit_date.isoformat() if p.exit_date else None,
        "net_pnl": _f(p.net_pnl), "pnl_percent": p.pnl_percent, "fees": _f(p.fees), "status": p.status.value,
        "composite_score": p.composite_score, "exit_reason": (p.extra_data or {}).get("exit_reason"),
        "last_close": (p.extra_data or {}).get("last_close"),
        "ml_model_id": (p.extra_data or {}).get("ml_model_id")} for p in rows]}


@router.get("/orders")
async def orders(limit: int = Query(50, ge=1, le=500), db: AsyncSession = Depends(get_db)) -> dict:
    acc = await cap.get_or_create_account(db)
    rows = (await db.execute(select(Order).where(Order.account_id == acc.id).order_by(Order.id.desc()).limit(limit))).scalars().all()
    return {"orders": [{"id": o.id, "ticker": o.ticker, "side": o.operation_type.value, "qty": o.quantity,
                        "status": o.status.value, "target_price": _f(o.target_price), "executed_price": _f(o.executed_price),
                        "signal_date": o.signal_date.isoformat() if o.signal_date else None,
                        "execution_date": o.execution_date.isoformat() if o.execution_date else None,
                        "fees": _f(o.fees)} for o in rows]}


@router.get("/decisions")
async def decisions(ticker: Optional[str] = None, approved: Optional[bool] = None,
                    limit: int = Query(50, ge=1, le=500), db: AsyncSession = Depends(get_db)) -> dict:
    """Audit trail: every decision with scores and each risk rule's pass/fail."""
    acc = await cap.get_or_create_account(db)
    q = select(DecisionLog).where(DecisionLog.account_id == acc.id)
    if ticker:
        q = q.where(DecisionLog.ticker == ticker.upper())
    if approved is not None:
        q = q.where(DecisionLog.approved == approved)
    rows = (await db.execute(q.order_by(DecisionLog.id.desc()).limit(limit))).scalars().all()
    return {"decisions": [{"id": d.id, "ticker": d.ticker, "signal_date": d.signal_date.isoformat(),
                           "composite_score": d.composite_score, "technical": d.technical_score, "news": d.news_score,
                           "ml_probability": d.ml_probability, "volume_momentum": d.volume_momentum_score,
                           "approved": d.approved, "reasons": d.reasons, "checks": d.checks, "order_id": d.order_id,
                           "ml_model_id": d.ml_model_id, "details": d.details} for d in rows]}


@router.post("/circuit-breaker/reset")
async def reset_breaker(period: str = "day", db: AsyncSession = Depends(get_db)) -> dict:
    """
    Operator override of the CURRENT day/week/month block (the scheduler only clears the day latch each morning).
    The loss realised so far stops counting: a fresh allowance of one full limit opens, and a ledger marker
    (`breaker_override`) records the override. Other periods that are still blocked stay blocked.
    """
    if period not in cap.PERIODS:
        raise HTTPException(400, f"period must be one of {cap.PERIODS}")
    acc = await cap.get_or_create_account(db)
    res = await cap.reset_circuit_breaker(db, acc, period, override=True)
    cb = await cap.refresh_circuit_breaker(db, acc)
    return {"circuit_breaker": cb["tripped"], "tripped_periods": cb["tripped_periods"],
            "overridden": res["overridden"], "remaining_allowance": cb["remaining_allowance"]}
