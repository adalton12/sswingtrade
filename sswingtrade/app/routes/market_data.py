"""
Market data routes.
Handles candle fetching, ticker info, and technical analysis.
"""

from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import MarketCandle, TickerInfo

router = APIRouter()


# ============================================================================
# Schemas
# ============================================================================

class CandleResponse(BaseModel):
    """OHLCV candle response."""
    id: int
    ticker: str
    date: datetime
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: int


class TickerInfoResponse(BaseModel):
    """Ticker information response."""
    id: int
    ticker: str
    name: Optional[str]
    sector: Optional[str]
    is_active: bool
    min_price: Optional[float]
    max_price: Optional[float]


class CandleDataRequest(BaseModel):
    """Request to fetch/create candle data."""
    ticker: str = Field(..., min_length=1, max_length=10)
    start_date: datetime
    end_date: datetime


# ============================================================================
# Routes
# ============================================================================

@router.get("/candles/{ticker}", response_model=List[CandleResponse])
async def get_candles(
    ticker: str,
    start_date: datetime = Query(...),
    end_date: datetime = Query(...),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db)
) -> List[CandleResponse]:
    """
    Fetch candle data for a ticker within date range.

    Args:
        ticker: Stock ticker symbol (e.g., PETR4)
        start_date: Start of date range
        end_date: End of date range
        limit: Maximum number of candles to return
        db: Database session

    Returns:
        List of candles ordered by date
    """

    stmt = (
        select(MarketCandle)
        .where(
            and_(
                MarketCandle.ticker == ticker.upper(),
                MarketCandle.date >= start_date,
                MarketCandle.date <= end_date,
            )
        )
        .order_by(MarketCandle.date.desc())
        .limit(limit)
    )

    result = await db.execute(stmt)
    candles = result.scalars().all()

    if not candles:
        raise HTTPException(
            status_code=404,
            detail=f"No candle data found for {ticker} between {start_date} and {end_date}"
        )

    return [
        CandleResponse(
            id=c.id,
            ticker=c.ticker,
            date=c.date,
            open_price=float(c.open_price),
            high_price=float(c.high_price),
            low_price=float(c.low_price),
            close_price=float(c.close_price),
            volume=c.volume,
        )
        for c in candles
    ]


@router.get("/candles/{ticker}/latest")
async def get_latest_candle(
    ticker: str,
    db: AsyncSession = Depends(get_db)
) -> CandleResponse:
    """
    Get the most recent candle for a ticker.
    """

    stmt = (
        select(MarketCandle)
        .where(MarketCandle.ticker == ticker.upper())
        .order_by(MarketCandle.date.desc())
        .limit(1)
    )

    result = await db.execute(stmt)
    candle = result.scalar_one_or_none()

    if not candle:
        raise HTTPException(
            status_code=404,
            detail=f"No candle data found for {ticker}"
        )

    return CandleResponse(
        id=candle.id,
        ticker=candle.ticker,
        date=candle.date,
        open_price=float(candle.open_price),
        high_price=float(candle.high_price),
        low_price=float(candle.low_price),
        close_price=float(candle.close_price),
        volume=candle.volume,
    )


@router.get("/tickers/{ticker}", response_model=TickerInfoResponse)
async def get_ticker_info(
    ticker: str,
    db: AsyncSession = Depends(get_db)
) -> TickerInfoResponse:
    """
    Get ticker information and configuration.
    """

    stmt = select(TickerInfo).where(TickerInfo.ticker == ticker.upper())
    result = await db.execute(stmt)
    info = result.scalar_one_or_none()

    if not info:
        # Return default info if not found
        info = TickerInfo(
            ticker=ticker.upper(),
            name=None,
            sector=None,
            is_active=True,
        )

    return TickerInfoResponse(
        id=info.id if info.id else 0,
        ticker=info.ticker,
        name=info.name,
        sector=info.sector,
        is_active=info.is_active,
        min_price=float(info.min_price) if info.min_price else None,
        max_price=float(info.max_price) if info.max_price else None,
    )


@router.post("/tickers/{ticker}/register")
async def register_ticker(
    ticker: str,
    name: Optional[str] = None,
    sector: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    db: AsyncSession = Depends(get_db)
) -> TickerInfoResponse:
    """Register a new ticker or update existing configuration."""

    stmt = select(TickerInfo).where(TickerInfo.ticker == ticker.upper())
    result = await db.execute(stmt)
    ticker_info = result.scalar_one_or_none()

    if ticker_info:
        # Update existing
        if name:
            ticker_info.name = name
        if sector:
            ticker_info.sector = sector
        if min_price:
            ticker_info.min_price = min_price
        if max_price:
            ticker_info.max_price = max_price
    else:
        # Create new
        ticker_info = TickerInfo(
            ticker=ticker.upper(),
            name=name,
            sector=sector,
            min_price=min_price,
            max_price=max_price,
        )
        db.add(ticker_info)

    await db.commit()
    await db.refresh(ticker_info)

    return TickerInfoResponse(
        id=ticker_info.id,
        ticker=ticker_info.ticker,
        name=ticker_info.name,
        sector=ticker_info.sector,
        is_active=ticker_info.is_active,
        min_price=float(ticker_info.min_price) if ticker_info.min_price else None,
        max_price=float(ticker_info.max_price) if ticker_info.max_price else None,
    )


@router.post("/sync/yfinance")
async def sync_market_data_yfinance(
    tickers: List[str] = Query(...),
    days_back: int = Query(60, ge=1, le=365),
    db: AsyncSession = Depends(get_db)
) -> dict:
    """
    Sync market data from yfinance.

    FASE 2: Implement actual yfinance fetching
    For now, returns schema example.
    """

    return {
        "status": "pending",
        "message": "Market data sync not implemented in FASE 1",
        "tickers": tickers,
        "days": days_back,
        "note": "See FASE 2 for yfinance integration",
    }
