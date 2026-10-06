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
from app.runtime.params import params, universe
from app.services.market_data_collector import (
    collect_daily_candles,
    collect_intraday_candles,
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
            tickers=universe(),
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
        result = await compute_many(universe(), days=5)
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
        result = await run_news_batch(universe())
        logger.info(f"⏰ Scheduler: News batch done - {result}")
    except Exception as e:
        logger.error(f"⏰ Scheduler: News batch FAILED - {e}")


async def job_morning_capital():
    """07:00 BRT: reset circuit breaker, weekly top-up (Mondays) and monthly deposit (first business day)."""
    try:
        from app.capital import service as cap
        from app.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            acc = await cap.get_or_create_account(db)
            await params.refresh(db)
            await cap.reset_circuit_breaker(db, acc, "day")   # week/month latches persist until their period ends
            wk = await cap.start_week(db, acc)
            mo = await cap.apply_monthly_deposit(db, acc)
            pl = await cap.apply_planned_deposits(db, acc)
            logger.info(f"⏰ Scheduler: morning capital - weekly={wk}, monthly={mo}, planned={pl}, equity={cap.equity(acc)}")
    except Exception as e:
        logger.error(f"⏰ Scheduler: morning capital FAILED - {e}")


async def job_daily_cycle():
    """20:00 BRT: paper-broker fills/exits + new Risk-Engine-approved orders for tomorrow's open."""
    try:
        from app.database import AsyncSessionLocal
        from app.execution.pipeline import run_daily_cycle
        async with AsyncSessionLocal() as db:
            out = await run_daily_cycle(db)
            logger.info(f"⏰ Scheduler: daily cycle done - equity={out['equity']}, "
                        f"approved={out['decisions']['approved']}/{out['decisions']['evaluated']}")
    except Exception as e:
        logger.error(f"⏰ Scheduler: daily cycle FAILED - {e}")


async def job_collect_intraday():
    """Scheduled job: collect intraday (1h) candles after market close."""
    logger.info("⏰ Scheduler: Starting intraday candle collection")
    try:
        result = await collect_intraday_candles(
            tickers=universe(),
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
            tickers=universe(),
            days_back=30,  # Wider window on weekends
        )
        logger.info(f"⏰ Scheduler: Weekend catch-up done - {result['total_inserted']} new rows")
    except Exception as e:
        logger.error(f"⏰ Scheduler: Weekend catch-up FAILED - {e}")


# ============================================================================
# Scheduler Lifecycle
# ============================================================================

def _hm(key: str):
    h, m = params.get(key).split(":")
    return int(h), int(m)


def setup_scheduler():
    """(Re)configure all jobs from the runtime parameters. Safe to call again after a settings change."""
    tz = settings.MARKET_TIMEZONE
    weekdays = [
        (job_morning_capital, "schedule.morning_time", "morning_capital", "Capital: deposits + breaker reset"),
        (job_collect_daily, "schedule.collect_daily_time", "collect_daily_candles", "Collect Daily Candles (EOD)"),
        (job_collect_intraday, "schedule.collect_intraday_time", "collect_intraday_candles", "Collect Intraday Candles (1h)"),
        (job_compute_indicators, "schedule.indicators_time", "compute_indicators", "Compute Technical Indicators"),
        (job_news_batch, "schedule.news_time", "news_batch", "News + LLM Batch Analysis"),
        (job_daily_cycle, "schedule.cycle_time", "daily_cycle", "Paper trading daily cycle"),
    ]
    for fn, key, jid, name in weekdays:
        h, m = _hm(key)
        scheduler.add_job(fn, CronTrigger(day_of_week="mon-fri", hour=h, minute=m, timezone=tz), id=jid, name=name,
                          replace_existing=True, misfire_grace_time=3600)
    scheduler.add_job(job_weekend_catchup, CronTrigger(day_of_week="sat", hour=10, minute=0, timezone=tz),
                      id="weekend_catchup", name="Weekend Data Catch-up", replace_existing=True, misfire_grace_time=7200)
    logger.info("⏰ Scheduler configured: " + ", ".join(f"{jid}@{params.get(k)}" for _, k, jid, _ in weekdays))


def reschedule():
    """Apply new schedule.* parameters to a running scheduler."""
    setup_scheduler()


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
