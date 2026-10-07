"""
Market clock helpers (B3): market-local "now", data-freshness in business days, partial-candle guard.
B3 holidays are NOT modelled (a holiday simply counts as a business day of staleness, which the
freshness tolerance absorbs).
"""

from datetime import date, datetime, time, timedelta, timezone

import numpy as np
import pandas as pd

from app.config import settings

# Yahoo's daily bar is only final after the closing call; before this (market-local) time a bar dated
# "today" is still partial and must not be stored (it would be wrong and never refreshed).
MARKET_FINAL_CANDLE_AFTER = time(18, 10)

_BRT_FALLBACK = timezone(timedelta(hours=-3))      # Brazil has no DST since 2019


def market_now() -> datetime:
    """Naive datetime in the market time zone (settings.MARKET_TIMEZONE), independent of the server's TZ."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(settings.MARKET_TIMEZONE)).replace(tzinfo=None)
    except Exception:
        return datetime.now(_BRT_FALLBACK).replace(tzinfo=None)


def business_days_stale(last_candle: date, today: date) -> int:
    """
    Business days between the last stored candle and the most recent session that should exist
    (today, or the previous business day on weekends). 0 = up to date.
    """
    last_session = np.busday_offset(np.datetime64(today), 0, roll="backward")
    return max(0, int(np.busday_count(np.datetime64(last_candle), last_session)))


def drop_incomplete_today(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Remove a candle dated today when the session's final bar is not available yet."""
    if df.empty or now.time() >= MARKET_FINAL_CANDLE_AFTER:
        return df
    return df[df.index.normalize() != pd.Timestamp(now.date())]
