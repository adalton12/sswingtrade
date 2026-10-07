"""
Market Data Collector Service - FASE 2
Fetches EOD and intraday data from yfinance for B3 stocks.

yfinance uses Yahoo Finance tickers: B3 stocks need .SA suffix
Example: PETR4 -> PETR4.SA, VALE3 -> VALE3.SA
"""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

import pandas as pd
import yfinance as yf
from sqlalchemy import select, and_, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.runtime.params import universe
from app.database import AsyncSessionLocal
from app.models import MarketCandle, IntradayCandle, TickerInfo, CollectionLog
from app.services.cache import cache
from app.services.logger import logger
from app.services.market_clock import drop_incomplete_today, market_now

# Yahoo rewrites the WHOLE history when a dividend/split happens (auto_adjust). The incremental refresh only
# rewrites the last days, so stored history would silently become a patchwork. When a re-fetched day differs from
# what is stored by more than this (and more than 1 cent of rounding), the ticker's history is re-synced in full.
ADJUSTMENT_TOLERANCE = 0.002


# ============================================================================
# Default B3 Tickers for Swing Trade
# ============================================================================

DEFAULT_TICKERS = [
    "PETR4", "VALE3", "ITUB4", "BBDC4", "ABEV3",
    "WEGE3", "RENT3", "EQTL3", "BBAS3", "B3SA3",
    "SUZB3", "JBSS3", "LREN3", "MGLU3", "RADL3",
    "HAPV3", "RAIL3", "VIVT3", "ELET3", "PRIO3",
]


def to_yahoo_ticker(ticker: str) -> str:
    """Convert B3 ticker to Yahoo Finance format."""
    ticker = ticker.upper().strip()
    if not ticker.endswith(".SA"):
        return f"{ticker}.SA"
    return ticker


def from_yahoo_ticker(yahoo_ticker: str) -> str:
    """Convert Yahoo Finance ticker back to B3 format."""
    return yahoo_ticker.replace(".SA", "").upper()


# ============================================================================
# Data Fetching (runs in thread pool - yfinance is sync)
# ============================================================================

def _fetch_daily_data(
    tickers: List[str],
    start_date: str,
    end_date: str,
) -> Dict[str, pd.DataFrame]:
    """
    Fetch daily OHLCV data from yfinance.
    Runs synchronously - call via asyncio.to_thread().

    Returns dict mapping B3 ticker -> DataFrame with OHLCV data.
    """
    results = {}
    yahoo_tickers = [to_yahoo_ticker(t) for t in tickers]

    for b3_ticker, yahoo_ticker in zip(tickers, yahoo_tickers):
        try:
            stock = yf.Ticker(yahoo_ticker)
            df = stock.history(start=start_date, end=end_date, interval="1d")

            if df.empty:
                logger.warning(f"No daily data returned for {b3_ticker}")
                continue

            # Standardize column names
            df = df.rename(columns={
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            })

            # Keep only OHLCV columns
            df = df[["open", "high", "low", "close", "volume"]].copy()
            df.index = pd.to_datetime(df.index)

            # Remove timezone info for PostgreSQL compatibility
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)

            # A bar dated today before the closing call is partial: never store it
            df = drop_incomplete_today(df, market_now())
            if df.empty:
                logger.warning(f"Only a partial candle available for {b3_ticker}; skipped")
                continue

            results[b3_ticker] = df
            logger.info(f"Fetched {len(df)} daily candles for {b3_ticker}")

        except Exception as e:
            logger.error(f"Error fetching daily data for {b3_ticker}: {e}")

    return results


def _fetch_intraday_data(
    tickers: List[str],
    period: str = "5d",
    interval: str = "1h",
) -> Dict[str, pd.DataFrame]:
    """
    Fetch intraday OHLCV data from yfinance.
    yfinance limits: 1h data available for last 730 days,
    15m/5m for last 60 days.

    Returns dict mapping B3 ticker -> DataFrame with OHLCV data.
    """
    results = {}
    yahoo_tickers = [to_yahoo_ticker(t) for t in tickers]

    for b3_ticker, yahoo_ticker in zip(tickers, yahoo_tickers):
        try:
            stock = yf.Ticker(yahoo_ticker)
            df = stock.history(period=period, interval=interval)

            if df.empty:
                logger.warning(f"No intraday data returned for {b3_ticker}")
                continue

            df = df.rename(columns={
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            })

            df = df[["open", "high", "low", "close", "volume"]].copy()
            df.index = pd.to_datetime(df.index)

            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)

            results[b3_ticker] = df
            logger.info(f"Fetched {len(df)} intraday ({interval}) candles for {b3_ticker}")

        except Exception as e:
            logger.error(f"Error fetching intraday data for {b3_ticker}: {e}")

    return results


def _fetch_ticker_info(ticker: str) -> Optional[dict]:
    """Fetch metadata for a B3 ticker from yfinance."""
    try:
        stock = yf.Ticker(to_yahoo_ticker(ticker))
        info = stock.info

        return {
            "ticker": ticker.upper(),
            "name": info.get("longName") or info.get("shortName"),
            "sector": info.get("sector"),
        }
    except Exception as e:
        logger.error(f"Error fetching info for {ticker}: {e}")
        return None


# ============================================================================
# Database Persistence
# ============================================================================

def _rows_from_df(df: pd.DataFrame, key: str, ticker: str, extra: Optional[dict] = None) -> List[dict]:
    """Convert OHLCV DataFrame into row dicts, skipping NaN/invalid rows."""
    rows = []
    for ts, r in df.dropna(subset=["open", "high", "low", "close"]).iterrows():
        row = {
            "ticker": ticker,
            key: ts.to_pydatetime(),
            "open_price": round(float(r["open"]), 2),
            "high_price": round(float(r["high"]), 2),
            "low_price": round(float(r["low"]), 2),
            "close_price": round(float(r["close"]), 2),
            "volume": int(r["volume"]) if pd.notna(r["volume"]) else 0,
        }
        if extra:
            row.update(extra)
        rows.append(row)
    return rows


async def _bulk_upsert(session: AsyncSession, model, rows: List[dict], index_elements: List[str]) -> Tuple[int, int]:
    """Single-statement bulk upsert. Returns (inserted, updated)."""
    if not rows:
        return 0, 0
    keys = [tuple(r[k] for k in index_elements) for r in rows]
    cols = [getattr(model, k) for k in index_elements]
    # Count pre-existing rows to split inserted vs updated
    existing = 0
    for i in range(0, len(rows), 500):
        chunk = rows[i:i + 500]
        stmt = select(func.count()).select_from(model).where(
            and_(cols[0] == chunk[0][index_elements[0]],
                 *[c.in_([r[k] for r in chunk]) for c, k in zip(cols[1:], index_elements[1:])])
        )
        existing += (await session.execute(stmt)).scalar_one()
    for i in range(0, len(rows), 500):
        chunk = rows[i:i + 500]
        ins = pg_insert(model).values(chunk)
        stmt = ins.on_conflict_do_update(
            index_elements=index_elements,
            set_={
                "open_price": ins.excluded.open_price,
                "high_price": ins.excluded.high_price,
                "low_price": ins.excluded.low_price,
                "close_price": ins.excluded.close_price,
                "volume": ins.excluded.volume,
            },
        )
        await session.execute(stmt)
    await session.commit()
    return len(rows) - existing, existing


async def _save_daily_candles(session: AsyncSession, ticker: str, df: pd.DataFrame) -> Tuple[int, int]:
    """Upsert daily candles (ON CONFLICT ticker,date). Returns (inserted, updated)."""
    rows = _rows_from_df(df, "date", ticker)
    return await _bulk_upsert(session, MarketCandle, rows, ["ticker", "date"])


async def _save_intraday_candles(session: AsyncSession, ticker: str, df: pd.DataFrame, interval: str = "1h") -> Tuple[int, int]:
    """Upsert intraday candles (ON CONFLICT ticker,datetime,interval)."""
    rows = _rows_from_df(df, "datetime", ticker, {"interval": interval})
    return await _bulk_upsert(session, IntradayCandle, rows, ["ticker", "datetime", "interval"])


async def _save_ticker_info(session: AsyncSession, info: dict):
    """Save or update ticker metadata."""
    stmt = select(TickerInfo).where(TickerInfo.ticker == info["ticker"])
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing:
        if info.get("name"):
            existing.name = info["name"]
        if info.get("sector"):
            existing.sector = info["sector"]
        existing.updated_at = datetime.utcnow()
    else:
        ticker_info = TickerInfo(
            ticker=info["ticker"],
            name=info.get("name"),
            sector=info.get("sector"),
            is_active=True,
        )
        session.add(ticker_info)

    await session.commit()


# ============================================================================
# History consistency (dividend / split adjustments)
# ============================================================================

async def _stored_closes(session: AsyncSession, ticker: str, dates: List[datetime]) -> Dict[datetime, float]:
    rows = (await session.execute(
        select(MarketCandle.date, MarketCandle.close_price).where(
            and_(MarketCandle.ticker == ticker, MarketCandle.date.in_(dates))))).all()
    return {d: float(c) for d, c in rows}


async def _needs_resync(session: AsyncSession, ticker: str, df: pd.DataFrame) -> bool:
    """True when a freshly fetched day disagrees with the stored one => the provider re-adjusted its history."""
    stored = await _stored_closes(session, ticker, [ts.to_pydatetime() for ts in df.index])
    for ts, close in df["close"].items():
        old = stored.get(ts.to_pydatetime())
        if not old or old <= 0:
            continue
        new = round(float(close), 2)
        if abs(new - old) / old > max(ADJUSTMENT_TOLERANCE, 0.012 / old):    # 0.012 = 1 cent rounding on both sides
            logger.warning(f"{ticker}: {ts.date()} stored {old:.2f} vs fetched {new:.2f} -> history was re-adjusted")
            return True
    return False


async def _oldest_stored(session: AsyncSession, ticker: str) -> Optional[datetime]:
    return (await session.execute(select(func.min(MarketCandle.date)).where(MarketCandle.ticker == ticker))).scalar_one_or_none()


# ============================================================================
# Public Collection API
# ============================================================================

async def collect_daily_candles(
    tickers: Optional[List[str]] = None,
    days_back: int = 60,
    force_full: bool = False,
) -> dict:
    """
    Main entry point: collect daily candles for given tickers.
    Fetches from yfinance, saves to PostgreSQL, caches in Redis.

    Args:
        tickers: List of B3 tickers (default: DEFAULT_TICKERS)
        days_back: How many days of history to fetch
        force_full: re-download each ticker's WHOLE stored history (picks up dividend/split re-adjustments);
            without it a full re-sync still happens automatically when a re-fetched day disagrees with the stored one

    Returns:
        Collection summary with success/failure counts (`resynced` = tickers whose full history was rewritten)
    """
    tickers = tickers or universe()
    now = market_now()          # market time, not the server's: the container runs in UTC
    start_date = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")
    # yfinance's `end` is EXCLUSIVE: ending "today" would never return today's session (data one day late)
    end_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    logger.info(f"Starting daily collection for {len(tickers)} tickers, {days_back} days back")

    # Create collection log
    async with AsyncSessionLocal() as session:
        log = CollectionLog(
            collection_type="daily",
            tickers_requested=tickers,
            status="running",
            started_at=datetime.utcnow(),
        )
        session.add(log)
        await session.commit()
        await session.refresh(log)
        log_id = log.id

    # Fetch data in thread pool (yfinance is sync)
    data = await asyncio.to_thread(_fetch_daily_data, tickers, start_date, end_date)

    succeeded = []
    failed = []
    resynced = []
    total_inserted = 0
    total_updated = 0

    async with AsyncSessionLocal() as session:
        for ticker in tickers:
            if ticker in data and not data[ticker].empty:
                try:
                    if force_full or await _needs_resync(session, ticker, data[ticker]):
                        oldest = await _oldest_stored(session, ticker)
                        oldest_day = oldest.strftime("%Y-%m-%d") if oldest else None
                        if oldest_day and oldest_day < start_date:
                            # stored history is older than the window just downloaded: fetch all of it again
                            refetched = await asyncio.to_thread(_fetch_daily_data, [ticker], oldest_day, end_date)
                            if ticker in refetched and not refetched[ticker].empty:
                                data[ticker] = refetched[ticker]
                                resynced.append(ticker)
                                logger.info(f"{ticker}: full history re-synced from {oldest_day}")
                            else:
                                logger.error(f"{ticker}: history re-sync failed, stored history may be inconsistent")
                        else:
                            resynced.append(ticker)     # the window already covers everything stored

                    ins, upd = await _save_daily_candles(session, ticker, data[ticker])
                    total_inserted += ins
                    total_updated += upd
                    succeeded.append(ticker)

                    # Cache latest candle in Redis
                    latest = data[ticker].iloc[-1]
                    await cache.set(
                        f"candle:daily:{ticker}:latest",
                        {
                            "ticker": ticker,
                            "date": str(data[ticker].index[-1]),
                            "close": round(float(latest["close"]), 2),
                            "volume": int(latest["volume"]),
                        },
                        ttl=settings.REDIS_CACHE_TTL,
                    )

                    # Also fetch and save ticker info
                    info = await asyncio.to_thread(_fetch_ticker_info, ticker)
                    if info:
                        await _save_ticker_info(session, info)

                except Exception as e:
                    logger.error(f"Error saving daily candles for {ticker}: {e}")
                    failed.append(ticker)
            else:
                failed.append(ticker)

    # Update collection log
    finished_at = datetime.utcnow()
    async with AsyncSessionLocal() as session:
        log = await session.get(CollectionLog, log_id)
        if log:
            log.tickers_succeeded = succeeded
            log.tickers_failed = failed
            log.total_rows_inserted = total_inserted
            log.total_rows_updated = total_updated
            log.status = "completed" if not failed else ("completed" if succeeded else "failed")
            log.finished_at = finished_at
            log.duration_seconds = (finished_at - log.started_at).total_seconds()
            await session.commit()

    summary = {
        "collection_type": "daily",
        "tickers_total": len(tickers),
        "tickers_succeeded": len(succeeded),
        "tickers_failed": len(failed),
        "succeeded": succeeded,
        "failed": failed,
        "resynced": resynced,
        "total_inserted": total_inserted,
        "total_updated": total_updated,
        "days_back": days_back,
        "duration_seconds": (finished_at - log.started_at).total_seconds() if log else 0,
    }

    logger.info(f"Daily collection complete: {summary}")
    return summary


async def collect_intraday_candles(
    tickers: Optional[List[str]] = None,
    period: str = "5d",
    interval: str = "1h",
) -> dict:
    """
    Collect intraday candles (hourly by default).

    Args:
        tickers: List of B3 tickers
        period: yfinance period string (1d, 5d, 1mo, etc.)
        interval: Candle interval (1h, 15m, 5m)
    """
    tickers = tickers or universe()

    logger.info(f"Starting intraday ({interval}) collection for {len(tickers)} tickers")

    async with AsyncSessionLocal() as session:
        log = CollectionLog(
            collection_type=f"intraday_{interval}",
            tickers_requested=tickers,
            status="running",
            started_at=datetime.utcnow(),
        )
        session.add(log)
        await session.commit()
        await session.refresh(log)
        log_id = log.id

    data = await asyncio.to_thread(_fetch_intraday_data, tickers, period, interval)

    succeeded = []
    failed = []
    total_inserted = 0
    total_updated = 0

    async with AsyncSessionLocal() as session:
        for ticker in tickers:
            if ticker in data and not data[ticker].empty:
                try:
                    ins, upd = await _save_intraday_candles(session, ticker, data[ticker], interval)
                    total_inserted += ins
                    total_updated += upd
                    succeeded.append(ticker)
                except Exception as e:
                    logger.error(f"Error saving intraday candles for {ticker}: {e}")
                    failed.append(ticker)
            else:
                failed.append(ticker)

    finished_at = datetime.utcnow()
    async with AsyncSessionLocal() as session:
        log = await session.get(CollectionLog, log_id)
        if log:
            log.tickers_succeeded = succeeded
            log.tickers_failed = failed
            log.total_rows_inserted = total_inserted
            log.total_rows_updated = total_updated
            log.status = "completed" if not failed else ("completed" if succeeded else "failed")
            log.finished_at = finished_at
            log.duration_seconds = (finished_at - log.started_at).total_seconds()
            await session.commit()

    summary = {
        "collection_type": f"intraday_{interval}",
        "tickers_total": len(tickers),
        "tickers_succeeded": len(succeeded),
        "tickers_failed": len(failed),
        "succeeded": succeeded,
        "failed": failed,
        "total_inserted": total_inserted,
        "total_updated": total_updated,
        "period": period,
        "interval": interval,
    }

    logger.info(f"Intraday collection complete: {summary}")
    return summary


async def get_collection_status() -> dict:
    """Get status of recent collection runs."""
    async with AsyncSessionLocal() as session:
        stmt = (
            select(CollectionLog)
            .order_by(CollectionLog.started_at.desc())
            .limit(10)
        )
        result = await session.execute(stmt)
        logs = result.scalars().all()

    return {
        "recent_collections": [
            {
                "id": log.id,
                "type": log.collection_type,
                "status": log.status,
                "tickers_requested": log.tickers_requested,
                "tickers_succeeded": log.tickers_succeeded,
                "tickers_failed": log.tickers_failed,
                "rows_inserted": log.total_rows_inserted,
                "rows_updated": log.total_rows_updated,
                "started_at": log.started_at.isoformat() if log.started_at else None,
                "finished_at": log.finished_at.isoformat() if log.finished_at else None,
                "duration_seconds": log.duration_seconds,
            }
            for log in logs
        ]
    }


async def get_data_coverage() -> dict:
    """Check data coverage: date ranges and gaps for each ticker."""
    async with AsyncSessionLocal() as session:
        # Daily coverage
        daily_stmt = text("""
            SELECT
                ticker,
                MIN(date) as first_date,
                MAX(date) as last_date,
                COUNT(*) as total_candles
            FROM market_candles
            GROUP BY ticker
            ORDER BY ticker
        """)
        daily_result = await session.execute(daily_stmt)
        daily_rows = daily_result.fetchall()

        # Intraday coverage
        intraday_stmt = text("""
            SELECT
                ticker,
                interval,
                MIN(datetime) as first_datetime,
                MAX(datetime) as last_datetime,
                COUNT(*) as total_candles
            FROM intraday_candles
            GROUP BY ticker, interval
            ORDER BY ticker, interval
        """)
        intraday_result = await session.execute(intraday_stmt)
        intraday_rows = intraday_result.fetchall()

    return {
        "daily_coverage": [
            {
                "ticker": row[0],
                "first_date": row[1].isoformat() if row[1] else None,
                "last_date": row[2].isoformat() if row[2] else None,
                "total_candles": row[3],
            }
            for row in daily_rows
        ],
        "intraday_coverage": [
            {
                "ticker": row[0],
                "interval": row[1],
                "first_datetime": row[2].isoformat() if row[2] else None,
                "last_datetime": row[3].isoformat() if row[3] else None,
                "total_candles": row[4],
            }
            for row in intraday_rows
        ],
    }
