"""Technical indicator routes - FASE 3."""

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.indicator_service import compute_many, get_latest_indicators
from app.services.market_data_collector import DEFAULT_TICKERS

router = APIRouter()


class ComputeRequest(BaseModel):
    tickers: Optional[List[str]] = None
    days: int = Field(default=60, ge=1, le=400)


def _to_dict(i) -> dict:
    d = {c.name: getattr(i, c.name) for c in i.__table__.columns if c.name not in ("id", "created_at")}
    d["date"] = i.date.isoformat()
    return d


@router.post("/compute")
async def compute_indicators(request: ComputeRequest) -> dict:
    """Compute and store indicators (needs >= 60 daily candles per ticker)."""
    return await compute_many(request.tickers or DEFAULT_TICKERS, request.days)


@router.get("/ranking")
async def ranking(min_score: float = Query(0, ge=0, le=100), limit: int = Query(10, ge=1, le=50)) -> dict:
    """Tickers ranked by their latest technical score."""
    rows = []
    for t in DEFAULT_TICKERS:
        latest = await get_latest_indicators(t, 1)
        if latest and latest[0].technical_score is not None and latest[0].technical_score >= min_score:
            rows.append({"ticker": t, "date": latest[0].date.isoformat(),
                         "technical_score": round(latest[0].technical_score, 1),
                         "rsi_14": latest[0].rsi_14, "atr_14": latest[0].atr_14})
    rows.sort(key=lambda r: r["technical_score"], reverse=True)
    return {"ranking": rows[:limit]}


@router.get("/{ticker}")
async def get_indicators(ticker: str, limit: int = Query(30, ge=1, le=400)) -> dict:
    rows = await get_latest_indicators(ticker, limit)
    if not rows:
        raise HTTPException(404, f"No indicators for {ticker}. Run POST /api/v1/indicators/compute first.")
    return {"ticker": ticker.upper(), "count": len(rows), "indicators": [_to_dict(r) for r in rows]}


@router.get("/{ticker}/score")
async def get_score(ticker: str) -> dict:
    """Latest technical score (0-100) plus ATR-based stop/take-profit suggestion."""
    rows = await get_latest_indicators(ticker, 1)
    if not rows:
        raise HTTPException(404, f"No indicators for {ticker}")
    r = rows[0]
    out = {"ticker": ticker.upper(), "date": r.date.isoformat(),
           "technical_score": r.technical_score, "close": r.close, "rsi_14": r.rsi_14}
    if r.atr_14 and r.close:
        from app.config import settings
        stop = r.close - settings.STOP_LOSS_ATR_MULTIPLIER * r.atr_14
        risk = r.close - stop
        out["suggested"] = {"stop_loss": round(stop, 2),
                            "take_profit": round(r.close + settings.TAKE_PROFIT_RATIO * risk, 2),
                            "atr_14": round(r.atr_14, 4)}
    return out
