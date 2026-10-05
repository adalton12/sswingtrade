"""
Indicator service - FASE 3.
Loads candles from PostgreSQL, computes indicators (app.quant) and persists
them in technical_indicators (upsert).
"""

from typing import List, Optional

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import AsyncSessionLocal
from app.models import MarketCandle, TechnicalIndicator
from app.quant.indicators import compute_all, technical_score
from app.services.logger import logger

# Warm-up: SMA50 needs 50 candles; keep extra history so recent rows are valid
MIN_CANDLES = 60
INDICATOR_COLS = [
    "close", "sma_5", "sma_10", "sma_20", "sma_50", "ema_9", "ema_21", "rsi_14",
    "macd", "macd_signal", "macd_hist", "atr_14", "bb_upper", "bb_middle",
    "bb_lower", "bb_pctb", "vwap_20", "volume_sma_20", "volume_change", "volume_ratio",
]


async def load_candles_df(ticker: str, limit: int = 400) -> pd.DataFrame:
    """Last `limit` daily candles as OHLCV DataFrame (ascending dates)."""
    async with AsyncSessionLocal() as session:
        stmt = (
            select(MarketCandle)
            .where(MarketCandle.ticker == ticker.upper())
            .order_by(MarketCandle.date.desc())
            .limit(limit)
        )
        candles = (await session.execute(stmt)).scalars().all()

    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(
        {
            "open": [float(c.open_price) for c in candles],
            "high": [float(c.high_price) for c in candles],
            "low": [float(c.low_price) for c in candles],
            "close": [float(c.close_price) for c in candles],
            "volume": [float(c.volume) for c in candles],
        },
        index=pd.DatetimeIndex([c.date for c in candles], name="date"),
    )
    return df.sort_index()


def indicators_with_score(df: pd.DataFrame) -> pd.DataFrame:
    """compute_all + technical_score column."""
    ind = compute_all(df)
    if ind.empty:
        return ind
    scores = []
    prev = None
    for _, row in ind.iterrows():
        scores.append(technical_score(row, prev))
        prev = row
    ind["technical_score"] = scores
    return ind


def _clean(v):
    return None if v is None or (isinstance(v, float) and not np.isfinite(v)) else float(v)


async def compute_and_store(ticker: str, days: int = 60) -> dict:
    """Compute indicators for a ticker and upsert the last `days` rows."""
    ticker = ticker.upper()
    df = await load_candles_df(ticker)
    if len(df) < MIN_CANDLES:
        return {"ticker": ticker, "status": "insufficient_data", "candles": len(df), "required": MIN_CANDLES}

    ind = indicators_with_score(df).tail(days)
    rows = []
    for ts, r in ind.iterrows():
        row = {"ticker": ticker, "date": ts.to_pydatetime(), "technical_score": _clean(r["technical_score"])}
        for c in INDICATOR_COLS:
            row[c] = _clean(r[c])
        rows.append(row)

    async with AsyncSessionLocal() as session:
        for i in range(0, len(rows), 200):
            ins = pg_insert(TechnicalIndicator).values(rows[i:i + 200])
            stmt = ins.on_conflict_do_update(
                index_elements=["ticker", "date"],
                set_={c: getattr(ins.excluded, c) for c in INDICATOR_COLS + ["technical_score"]},
            )
            await session.execute(stmt)
        await session.commit()

    logger.info(f"Indicators stored for {ticker}: {len(rows)} rows")
    return {"ticker": ticker, "status": "ok", "rows": len(rows), "latest_score": rows[-1]["technical_score"]}


async def compute_many(tickers: List[str], days: int = 60) -> dict:
    results, failed = [], []
    for t in tickers:
        try:
            results.append(await compute_and_store(t, days))
        except Exception as e:
            logger.error(f"Indicator computation failed for {t}: {e}")
            failed.append(t)
    return {"computed": results, "failed": failed}


async def get_latest_indicators(ticker: str, limit: int = 30) -> List[TechnicalIndicator]:
    async with AsyncSessionLocal() as session:
        stmt = (
            select(TechnicalIndicator)
            .where(TechnicalIndicator.ticker == ticker.upper())
            .order_by(TechnicalIndicator.date.desc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())
