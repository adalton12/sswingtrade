"""
Market data routes - FASE 2.
Handles candle fetching, ticker info, data sync, and collection management.
"""

from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel, Field
from sqlalchemy import select, and_, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import MarketCandle, IntradayCandle, TickerInfo, CollectionLog
from app.services.cache import cache
from app.services.market_data_collector import (
    collect_daily_candles,
    collect_intraday_candles,
    get_collection_status,
    get_data_coverage,
    DEFAULT_TICKERS,
)
from app.services.scheduler import get_scheduler_status

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


class IntradayCandleResponse(BaseModel):
    """Intraday OHLCV candle response."""
    id: int
    ticker: str
    datetime: datetime
    interval: str
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


class SyncRequest(BaseModel):
    """Request to sync market data."""
    tickers: Optional[List[str]] = None
    days_back: int = Field(default=60, ge=1, le=730)


class SyncIntradayRequest(BaseModel):
    """Request to sync intraday data."""
    tickers: Optional[List[str]] = None
    period: str = Field(default="5d", pattern="^(1d|5d|1mo|3mo|6mo|1y|2y)$")
    interval: str = Field(default="1h", pattern="^(5m|15m|30m|1h)$")


# ============================================================================
# Daily Candle Routes
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
    Fetch daily candle data for a ticker within date range.
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
    """Get the most recent daily candle for a ticker."""

    # Try cache first
    cached = await cache.get(f"candle:daily:{ticker.upper()}:latest")
    if cached:
        # Still need the full record from DB for complete response
        pass

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


# ============================================================================
# Intraday Candle Routes
# ============================================================================

@router.get("/candles/{ticker}/intraday", response_model=List[IntradayCandleResponse])
async def get_intraday_candles(
    ticker: str,
    start_date: datetime = Query(...),
    end_date: datetime = Query(...),
    interval: str = Query("1h", pattern="^(5m|15m|30m|1h)$"),
    limit: int = Query(200, ge=1, le=2000),
    db: AsyncSession = Depends(get_db)
) -> List[IntradayCandleResponse]:
    """Fetch intraday candles for a ticker within date range."""

    stmt = (
        select(IntradayCandle)
        .where(
            and_(
                IntradayCandle.ticker == ticker.upper(),
                IntradayCandle.datetime >= start_date,
                IntradayCandle.datetime <= end_date,
                IntradayCandle.interval == interval,
            )
        )
        .order_by(IntradayCandle.datetime.desc())
        .limit(limit)
    )

    result = await db.execute(stmt)
    candles = result.scalars().all()

    if not candles:
        raise HTTPException(
            status_code=404,
            detail=f"No intraday data found for {ticker} ({interval})"
        )

    return [
        IntradayCandleResponse(
            id=c.id,
            ticker=c.ticker,
            datetime=c.datetime,
            interval=c.interval,
            open_price=float(c.open_price),
            high_price=float(c.high_price),
            low_price=float(c.low_price),
            close_price=float(c.close_price),
            volume=c.volume,
        )
        for c in candles
    ]


# ============================================================================
# Ticker Info Routes
# ============================================================================

@router.get("/tickers", response_model=List[TickerInfoResponse])
async def list_tickers(
    active_only: bool = Query(True),
    db: AsyncSession = Depends(get_db)
) -> List[TickerInfoResponse]:
    """List all registered tickers."""
    stmt = select(TickerInfo)
    if active_only:
        stmt = stmt.where(TickerInfo.is_active == True)
    stmt = stmt.order_by(TickerInfo.ticker)

    result = await db.execute(stmt)
    tickers = result.scalars().all()

    return [
        TickerInfoResponse(
            id=t.id,
            ticker=t.ticker,
            name=t.name,
            sector=t.sector,
            is_active=t.is_active,
            min_price=float(t.min_price) if t.min_price else None,
            max_price=float(t.max_price) if t.max_price else None,
        )
        for t in tickers
    ]


@router.get("/tickers/{ticker}", response_model=TickerInfoResponse)
async def get_ticker_info(
    ticker: str,
    db: AsyncSession = Depends(get_db)
) -> TickerInfoResponse:
    """Get ticker information and configuration."""
    stmt = select(TickerInfo).where(TickerInfo.ticker == ticker.upper())
    result = await db.execute(stmt)
    info = result.scalar_one_or_none()

    if not info:
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
        if name:
            ticker_info.name = name
        if sector:
            ticker_info.sector = sector
        if min_price:
            ticker_info.min_price = min_price
        if max_price:
            ticker_info.max_price = max_price
    else:
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


# ============================================================================
# Data Sync Routes (FASE 2)
# ============================================================================

@router.post("/sync/daily")
async def sync_daily_data(
    request: SyncRequest,
    background_tasks: BackgroundTasks,
) -> dict:
    """
    Trigger daily candle collection from yfinance.
    Runs in background for large ticker lists.
    """
    tickers = request.tickers or DEFAULT_TICKERS

    if len(tickers) <= 5:
        # Small batch: run synchronously for immediate feedback
        result = await collect_daily_candles(
            tickers=tickers,
            days_back=request.days_back,
        )
        return {"status": "completed", "result": result}
    else:
        # Large batch: run in background
        background_tasks.add_task(
            collect_daily_candles,
            tickers=tickers,
            days_back=request.days_back,
        )
        return {
            "status": "started",
            "message": f"Daily collection started for {len(tickers)} tickers in background",
            "tickers": tickers,
            "days_back": request.days_back,
            "check_status_at": "/api/v1/market/sync/status",
        }


@router.post("/sync/intraday")
async def sync_intraday_data(
    request: SyncIntradayRequest,
    background_tasks: BackgroundTasks,
) -> dict:
    """
    Trigger intraday candle collection from yfinance.
    """
    tickers = request.tickers or DEFAULT_TICKERS

    if len(tickers) <= 5:
        result = await collect_intraday_candles(
            tickers=tickers,
            period=request.period,
            interval=request.interval,
        )
        return {"status": "completed", "result": result}
    else:
        background_tasks.add_task(
            collect_intraday_candles,
            tickers=tickers,
            period=request.period,
            interval=request.interval,
        )
        return {
            "status": "started",
            "message": f"Intraday collection started for {len(tickers)} tickers in background",
            "tickers": tickers,
            "period": request.period,
            "interval": request.interval,
            "check_status_at": "/api/v1/market/sync/status",
        }


@router.get("/sync/status")
async def get_sync_status() -> dict:
    """Get status of recent data collection runs."""
    return await get_collection_status()


@router.get("/sync/coverage")
async def get_coverage() -> dict:
    """
    Check data coverage: date ranges and row counts for each ticker.
    Useful to identify gaps in historical data.
    """
    return await get_data_coverage()


@router.get("/sync/scheduler")
async def get_scheduler_info() -> dict:
    """Get scheduler status and next scheduled run times."""
    return get_scheduler_status()


# ============================================================================
# Statistics Routes
# ============================================================================

@router.get("/stats/{ticker}")
async def get_ticker_stats(
    ticker: str,
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db)
) -> dict:
    """
    Get basic statistics for a ticker over a period.
    Useful for quick overview before deeper analysis (FASE 3).
    """
    cutoff = datetime.utcnow() - timedelta(days=days)

    stmt = (
        select(
            func.count(MarketCandle.id).label("total_candles"),
            func.min(MarketCandle.close_price).label("min_close"),
            func.max(MarketCandle.close_price).label("max_close"),
            func.avg(MarketCandle.close_price).label("avg_close"),
            func.avg(MarketCandle.volume).label("avg_volume"),
            func.min(MarketCandle.date).label("first_date"),
            func.max(MarketCandle.date).label("last_date"),
        )
        .where(
            and_(
                MarketCandle.ticker == ticker.upper(),
                MarketCandle.date >= cutoff,
            )
        )
    )

    result = await db.execute(stmt)
    row = result.one_or_none()

    if not row or row[0] == 0:
        raise HTTPException(
            status_code=404,
            detail=f"No data found for {ticker} in the last {days} days"
        )

    return {
        "ticker": ticker.upper(),
        "period_days": days,
        "total_candles": row[0],
        "price_min": round(float(row[1]), 2) if row[1] else None,
        "price_max": round(float(row[2]), 2) if row[2] else None,
        "price_avg": round(float(row[3]), 2) if row[3] else None,
        "volume_avg": int(row[4]) if row[4] else None,
        "first_date": row[5].isoformat() if row[5] else None,
        "last_date": row[6].isoformat() if row[6] else None,
    }
