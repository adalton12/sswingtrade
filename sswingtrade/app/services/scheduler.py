"""
Market Data Scheduler Service - FASE 2
Automated collection of market data using APScheduler.

Schedule:
- Daily candles: Weekdays at 18:30 BRT (after B3 closes at 18:00)
- Intraday candles: Weekdays at 18:45 BRT
- Weekend catch-up: Saturday at 10:00 BRT (fill any gaps)
"""

from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.services.logger import logger
from app.services.market_data_collector import (
    collect_daily_candles,
    collect_intraday_candles,
    DEFAULT_TICKERS,
)


# Global scheduler instance
scheduler = AsyncIOScheduler(timezone=settings.MARKET_TIMEZONE)


# ============================================================================
# Scheduled Jobs
# ============================================================================

async def job_collect_daily():
    """Scheduled job: collect daily candles after market close."""
    logger.info("⏰ Scheduler: Starting daily candle collection")
    try:
        result = await collect_daily_candles(
            tickers=DEFAULT_TICKERS,
            days_back=5,  # Last 5 days to catch any gaps
        )
        logger.info(
            f"⏰ Scheduler: Daily collection done - "
            f"{result['tickers_succeeded']} succeeded, "
            f"{result['tickers_failed']} failed, "
            f"{result['total_inserted']} inserted, "
            f"{result['total_updated']} updated"
        )
    except Exception as e:
        logger.error(f"⏰ Scheduler: Daily collection FAILED - {e}")


async def job_compute_indicators():
    """Scheduled job: recompute indicators after daily candles are stored."""
    logger.info("⏰ Scheduler: Computing indicators")
    try:
        from app.services.indicator_service import compute_many
        result = await compute_many(DEFAULT_TICKERS, days=5)
        logger.info(f"⏰ Scheduler: Indicators done - {len(result['computed'])} ok, {len(result['failed'])} failed")
    except Exception as e:
        logger.error(f"⏰ Scheduler: Indicator computation FAILED - {e}")


async def job_news_batch():
    """Scheduled job: RSS fetch + batch LLM analysis (after market close, never per tick)."""
    if not settings.ENABLE_LLM_ANALYSIS:
        return
    logger.info("⏰ Scheduler: Starting news batch")
    try:
        from app.services.news_service import run_news_batch
        result = await run_news_batch(DEFAULT_TICKERS)
        logger.info(f"⏰ Scheduler: News batch done - {result}")
    except Exception as e:
        logger.error(f"⏰ Scheduler: News batch FAILED - {e}")


async def job_collect_intraday():
    """Scheduled job: collect intraday (1h) candles after market close."""
    logger.info("⏰ Scheduler: Starting intraday candle collection")
    try:
        result = await collect_intraday_candles(
            tickers=DEFAULT_TICKERS,
            period="5d",
            interval="1h",
        )
        logger.info(
            f"⏰ Scheduler: Intraday collection done - "
            f"{result['tickers_succeeded']} succeeded, "
            f"{result['tickers_failed']} failed"
        )
    except Exception as e:
        logger.error(f"⏰ Scheduler: Intraday collection FAILED - {e}")


async def job_weekend_catchup():
    """Scheduled job: weekend catch-up for any missing data."""
    logger.info("⏰ Scheduler: Starting weekend catch-up collection")
    try:
        result = await collect_daily_candles(
            tickers=DEFAULT_TICKERS,
            days_back=30,  # Wider window on weekends
        )
        logger.info(f"⏰ Scheduler: Weekend catch-up done - {result['total_inserted']} new rows")
    except Exception as e:
        logger.error(f"⏰ Scheduler: Weekend catch-up FAILED - {e}")


# ============================================================================
# Scheduler Lifecycle
# ============================================================================

def setup_scheduler():
    """Configure and add all scheduled jobs."""

    # Daily candles - weekdays at 18:30 BRT (after B3 closes at ~18:00)
    scheduler.add_job(
        job_collect_daily,
        CronTrigger(
            day_of_week="mon-fri",
            hour=18,
            minute=30,
            timezone=settings.MARKET_TIMEZONE,
        ),
        id="collect_daily_candles",
        name="Collect Daily Candles (EOD)",
        replace_existing=True,
        misfire_grace_time=3600,  # 1 hour grace period
    )

    # Intraday candles - weekdays at 18:45 BRT
    scheduler.add_job(
        job_collect_intraday,
        CronTrigger(
            day_of_week="mon-fri",
            hour=18,
            minute=45,
            timezone=settings.MARKET_TIMEZONE,
        ),
        id="collect_intraday_candles",
        name="Collect Intraday Candles (1h)",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # Indicators - weekdays at 19:00 BRT (after daily candles)
    scheduler.add_job(
        job_compute_indicators,
        CronTrigger(day_of_week="mon-fri", hour=19, minute=0, timezone=settings.MARKET_TIMEZONE),
        id="compute_indicators",
        name="Compute Technical Indicators",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # News + LLM batch - weekdays at 19:30 BRT
    scheduler.add_job(
        job_news_batch,
        CronTrigger(day_of_week="mon-fri", hour=19, minute=30, timezone=settings.MARKET_TIMEZONE),
        id="news_batch",
        name="News + LLM Batch Analysis",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # Weekend catch-up - Saturday at 10:00 BRT
    scheduler.add_job(
        job_weekend_catchup,
        CronTrigger(
            day_of_week="sat",
            hour=10,
            minute=0,
            timezone=settings.MARKET_TIMEZONE,
        ),
        id="weekend_catchup",
        name="Weekend Data Catch-up",
        replace_existing=True,
        misfire_grace_time=7200,
    )

    logger.info("⏰ Scheduler configured with 5 jobs:")
    logger.info("  - Daily candles: Mon-Fri 18:30 BRT")
    logger.info("  - Intraday candles: Mon-Fri 18:45 BRT")
    logger.info("  - Weekend catch-up: Sat 10:00 BRT")


async def start_scheduler():
    """Start the scheduler."""
    setup_scheduler()
    scheduler.start()
    logger.info("⏰ Scheduler started")


async def stop_scheduler():
    """Stop the scheduler gracefully."""
    if scheduler.running:
        scheduler.shutdown(wait=True)
        logger.info("⏰ Scheduler stopped")


def get_scheduler_status() -> dict:
    """Get current scheduler status and next run times."""
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            "trigger": str(job.trigger),
        })

    return {
        "running": scheduler.running,
        "timezone": str(settings.MARKET_TIMEZONE),
        "jobs": jobs,
    }
