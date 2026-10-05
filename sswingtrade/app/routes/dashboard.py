"""Dashboard - FASE 9: one HTML page + one JSON summary endpoint."""

from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.capital import service as cap
from app.config import settings
from app.database import get_db
from app.models import CapitalHistory, DecisionLog, MarketCandle, Position, PositionStatus
from app.services.scheduler import get_scheduler_status

router = APIRouter()
INDEX = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"


def _f(v):
    return float(v) if v is not None else None


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def page() -> str:
    return INDEX.read_text(encoding="utf-8")


@router.get("/api/v1/dashboard/summary")
async def summary(db: AsyncSession = Depends(get_db)) -> dict:
    acc = await cap.get_or_create_account(db)
    eq = cap.equity(acc)

    hist = (await db.execute(select(CapitalHistory).where(CapitalHistory.account_id == acc.id)
                             .order_by(CapitalHistory.date.asc(), CapitalHistory.id.asc()))).scalars().all()
    curve = [{"date": h.date.isoformat(), "equity": _f(h.balance_after), "event": h.event_type} for h in hist]
    deposited = sum(_f(h.amount) for h in hist if h.event_type in ("deposit", "weekly_deposit", "monthly_deposit"))

    positions = (await db.execute(select(Position).where(Position.account_id == acc.id))).scalars().all()
    open_rows, unrealized = [], 0.0
    for p in positions:
        if p.status != PositionStatus.OPEN:
            continue
        last = (await db.execute(select(MarketCandle.close_price).where(MarketCandle.ticker == p.ticker)
                                 .order_by(MarketCandle.date.desc()).limit(1))).scalar_one_or_none()
        last = _f(last) or _f(p.entry_price)
        pnl = (last - _f(p.entry_price)) * p.quantity
        unrealized += pnl
        open_rows.append({"ticker": p.ticker, "qty": p.quantity, "entry": _f(p.entry_price), "last": last,
                          "stop": _f(p.stop_loss_price), "take_profit": _f(p.take_profit_price),
                          "unrealized_pnl": round(pnl, 2), "pct": round((last / _f(p.entry_price) - 1) * 100, 2),
                          "since": p.entry_date.date().isoformat(), "score": p.composite_score})

    closed = [p for p in positions if p.status == PositionStatus.CLOSED]
    wins = [p for p in closed if _f(p.net_pnl) > 0]
    realized = sum(_f(p.net_pnl) for p in closed)
    recent = sorted(closed, key=lambda p: p.exit_date or p.entry_date, reverse=True)[:15]

    top = (await db.execute(select(DecisionLog).where(DecisionLog.account_id == acc.id)
                            .order_by(DecisionLog.signal_date.desc(), DecisionLog.composite_score.desc()).limit(10))).scalars().all()
    cb = await cap.refresh_circuit_breaker(db, acc)
    return {
        "mode": "PAPER" if not settings.ENABLE_REAL_TRADING else "REAL",
        "equity": eq, "mark_to_market_equity": round(eq + unrealized, 2), "cash": _f(acc.available_balance),
        "invested": _f(acc.invested_capital), "total_deposited": round(deposited, 2),
        "net_profit": round(eq + unrealized - deposited, 2), "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "return_pct": round(((eq + unrealized) / deposited - 1) * 100, 2) if deposited else None,
        "trades_closed": len(closed), "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "avg_win": round(sum(_f(p.net_pnl) for p in wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(_f(p.net_pnl) for p in closed if _f(p.net_pnl) <= 0) / max(1, len(closed) - len(wins)), 2) if closed else None,
        "per_op_budget": round(eq * 0.2, 2), "circuit_breaker": cb,
        "equity_curve": curve, "open_positions": open_rows,
        "recent_trades": [{"ticker": p.ticker, "exit": (p.exit_date or p.entry_date).date().isoformat(),
                           "net_pnl": _f(p.net_pnl), "pct": p.pnl_percent,
                           "reason": (p.extra_data or {}).get("exit_reason")} for p in recent],
        "latest_decisions": [{"ticker": d.ticker, "date": d.signal_date.date().isoformat(), "score": d.composite_score,
                              "approved": d.approved, "reason": (d.reasons or [None])[0]} for d in top],
        "scheduler": get_scheduler_status() if settings.SCHEDULER_ENABLED else None,
    }
