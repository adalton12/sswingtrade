"""Backtesting routes - FASE 4."""

import csv
import io
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.backtesting.costs import CostModel
from app.backtesting.engine import BacktestConfig, run_backtest
from app.backtesting.strategies import STRATEGIES, build_strategy
from app.database import get_db
from app.models import BacktestRun
from app.services.indicator_service import load_candles_df
from app.services.market_data_collector import DEFAULT_TICKERS

router = APIRouter()


class BacktestRequest(BaseModel):
    name: Optional[str] = None
    strategy: str = "score"
    strategy_params: Dict[str, float] = Field(default_factory=dict)
    tickers: Optional[List[str]] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    initial_capital: float = Field(500.0, gt=0)
    per_trade_allocation: float = Field(100.0, gt=0)
    dynamic_sizing: bool = True
    monthly_deposit: float = Field(0.0, ge=0)
    max_open_positions: int = Field(5, ge=1, le=20)
    max_hold_days: int = Field(5, ge=1, le=30)
    atr_stop_mult: float = Field(1.5, gt=0)
    risk_reward: float = Field(2.0, gt=0)
    slippage_bps: float = Field(5.0, ge=0)
    fee_rate: float = Field(0.000325, ge=0)
    brokerage: float = Field(0.0, ge=0)


@router.get("/strategies")
async def list_strategies() -> dict:
    return {"strategies": list(STRATEGIES)}


@router.post("/run")
async def run(req: BacktestRequest, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        strat = build_strategy(req.strategy, **req.strategy_params)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))

    tickers = [t.upper() for t in (req.tickers or DEFAULT_TICKERS)]
    data = {}
    for t in tickers:
        df = await load_candles_df(t, limit=2000)
        if req.start_date is not None and not df.empty:
            # keep 60 candles of warm-up before start so indicators are valid at start
            pos = df.index.searchsorted(req.start_date)
            df = df.iloc[max(0, pos - 60):]
        if req.end_date is not None and not df.empty:
            df = df[df.index <= req.end_date]
        if len(df) >= 70:
            data[t] = df
    if not data:
        raise HTTPException(404, "Not enough candles (need >= 70 per ticker). Run POST /api/v1/market/sync/daily first.")

    cfg = BacktestConfig(
        initial_capital=req.initial_capital, per_trade_allocation=req.per_trade_allocation,
        dynamic_sizing=req.dynamic_sizing, monthly_deposit=req.monthly_deposit,
        max_open_positions=req.max_open_positions, max_hold_days=req.max_hold_days,
        atr_stop_mult=req.atr_stop_mult, risk_reward=req.risk_reward,
        costs=CostModel(fee_rate=req.fee_rate, brokerage=req.brokerage, slippage_bps=req.slippage_bps),
    )
    result = run_backtest(data, strat, cfg)
    m = result["metrics"]
    all_dates = [d for df in data.values() for d in df.index]

    run_row = BacktestRun(
        name=req.name or f"{req.strategy} {datetime.utcnow():%Y-%m-%d %H:%M}",
        strategy_id=req.strategy,
        start_date=req.start_date or min(all_dates), end_date=req.end_date or max(all_dates),
        total_trades=m.get("trades"), winning_trades=m.get("winning_trades"), losing_trades=m.get("losing_trades"),
        win_rate=m.get("win_rate_pct"), initial_capital=req.initial_capital, final_capital=m.get("final_equity"),
        total_return=m.get("total_return_pct"), max_drawdown=m.get("max_drawdown_pct"), sharpe_ratio=m.get("sharpe"),
        parameters=req.model_dump(mode="json"),
        results={"metrics": m, "trades": result["trades"], "equity_curve": result["equity_curve"],
                 "tickers": sorted(data)},
    )
    db.add(run_row)
    await db.commit()
    await db.refresh(run_row)
    return {"id": run_row.id, "metrics": m, "tickers": sorted(data), "trades_count": len(result["trades"]),
            "note": "Past performance does not guarantee future results; validate with paper trading (FASE 8)."}


@router.get("/runs")
async def list_runs(limit: int = Query(20, ge=1, le=100), db: AsyncSession = Depends(get_db)) -> dict:
    rows = (await db.execute(select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(limit))).scalars().all()
    return {"runs": [{"id": r.id, "name": r.name, "strategy": r.strategy_id, "trades": r.total_trades,
                      "win_rate": r.win_rate, "total_return_pct": r.total_return, "max_drawdown_pct": r.max_drawdown,
                      "sharpe": r.sharpe_ratio, "created_at": r.created_at.isoformat()} for r in rows]}


async def _get_run(db, run_id: int) -> BacktestRun:
    r = await db.get(BacktestRun, run_id)
    if not r:
        raise HTTPException(404, f"Backtest run {run_id} not found")
    return r


@router.get("/runs/{run_id}")
async def get_run(run_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    r = await _get_run(db, run_id)
    return {"id": r.id, "name": r.name, "strategy": r.strategy_id, "parameters": r.parameters, "results": r.results}


@router.get("/runs/{run_id}/export")
async def export_run(run_id: int, format: str = Query("csv", pattern="^(csv|json)$"),
                     db: AsyncSession = Depends(get_db)):
    r = await _get_run(db, run_id)
    trades = (r.results or {}).get("trades", [])
    if format == "json":
        return r.results
    buf = io.StringIO()
    if trades:
        w = csv.DictWriter(buf, fieldnames=list(trades[0].keys()))
        w.writeheader()
        w.writerows(trades)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=backtest_{run_id}_trades.csv"})
