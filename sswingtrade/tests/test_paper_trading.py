from datetime import datetime

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from app.broker.base import OrderRequest
from app.broker.paper import PaperBroker
from app.capital import service as cap
from app.execution import pipeline
from app.models import (Base, DecisionLog, MarketCandle, Order, OrderStatus, Position, PositionStatus)
from app.risk.engine import RiskConfig, RiskEngine, RiskViolation, issue_approval


@pytest.fixture(autouse=True)
def permissive_engine(monkeypatch):
    """Score gate off so the synthetic flat series can trade; all other rules stay active."""
    monkeypatch.setattr(pipeline, "RiskEngine", lambda: RiskEngine(RiskConfig(min_score=0)))


def add_candles(db, ticker, dates, o, h, l, c, v=1_000_000):
    db.add_all([MarketCandle(ticker=ticker, date=d.to_pydatetime(), open_price=o, high_price=h, low_price=l,
                             close_price=c, volume=v) for d in dates])


def history(n=100):
    return pd.bdate_range(end="2026-09-30", periods=n)


async def setup_flat(db, n=100):
    days = history(n)
    add_candles(db, "AAA", days, 10.0, 10.1, 9.9, 10.0)
    await db.commit()
    return days


async def next_day(db, days, o, h, l, c, k=1):
    d = pd.bdate_range(start=days[-1] + pd.offsets.BDay(k), periods=1)
    add_candles(db, "AAA", d, o, h, l, c)
    await db.commit()
    return d[0]


async def test_decision_creates_pending_order_and_logs_everything(db):
    acc = await cap.get_or_create_account(db)
    await setup_flat(db)
    r = await pipeline.run_decisions(db, acc, ["AAA"])
    assert r["approved"] == 1 and r["evaluated"] == 1
    d = r["decisions"][0]
    assert d["approved"] and d["qty"] >= 1 and d["stop"] < d["price"] < d["take_profit"]
    order = (await db.execute(select(Order))).scalars().one()
    assert order.status == OrderStatus.PENDING and order.signal_date.date().isoformat() == "2026-09-30"
    logs = (await db.execute(select(DecisionLog))).scalars().all()
    assert len(logs) == 1 and logs[0].approved and logs[0].checks and logs[0].order_id == order.id
    assert float(acc.available_balance) == 500.0            # nothing is spent before the fill

    again = await pipeline.run_decisions(db, acc, ["AAA"])  # idempotent per signal_date
    assert again["decisions"][0].get("skipped")
    assert len((await db.execute(select(Order))).scalars().all()) == 1


async def test_no_fill_without_a_later_candle_and_fill_at_next_open(db):
    acc = await cap.get_or_create_account(db)
    days = await setup_flat(db)
    await pipeline.run_decisions(db, acc, ["AAA"])
    broker = PaperBroker(db, acc)
    assert await broker.process_pending() == []             # decision day's close is NOT an execution price
    order = (await db.execute(select(Order))).scalars().one()
    assert order.status == OrderStatus.PENDING

    await next_day(db, days, o=10.5, h=10.6, l=10.4, c=10.5)
    ev = await broker.process_pending()
    assert ev[0]["event"] == "filled"
    pos = (await db.execute(select(Position))).scalars().one()
    assert pos.status == PositionStatus.OPEN
    assert float(pos.entry_price) >= 10.5                   # next OPEN + slippage, not yesterday's 10.0
    assert float(pos.stop_loss_price) < float(pos.entry_price) < float(pos.take_profit_price)
    assert float(acc.invested_capital) == pytest.approx(float(pos.entry_price) * pos.quantity, abs=0.01)
    assert cap.equity(acc) < 500.0                          # entry fees already deducted


async def test_target_exit_books_profit_and_grows_budget(db):
    acc = await cap.get_or_create_account(db)
    days = await setup_flat(db)
    await pipeline.run_decisions(db, acc, ["AAA"])
    broker = PaperBroker(db, acc)
    d1 = await next_day(db, days, 10.0, 10.1, 9.95, 10.05)
    await broker.process_pending()
    pos = (await db.execute(select(Position))).scalars().one()
    tp = float(pos.take_profit_price)
    await next_day(db, days, 10.1, tp + 0.5, 10.0, tp, k=2)  # touches the target
    ev = await broker.manage_positions()
    assert ev and ev[0]["reason"] == "target" and ev[0]["net_pnl"] > 0
    await db.refresh(pos)
    assert pos.status == PositionStatus.CLOSED and float(pos.net_pnl) > 0
    assert float(acc.invested_capital) == 0.0
    assert cap.equity(acc) > 500.0 and float(acc.total_gains) > 0
    assert cap.equity(acc) == pytest.approx(500 + float(pos.net_pnl), abs=0.02)   # ledger reconciles with P&L
    from app.capital.rules import daily_budget
    assert daily_budget(cap.equity(acc)) > 100.0            # profit is reinvested -> bigger next budget


async def test_stop_wins_when_both_levels_touched(db):
    acc = await cap.get_or_create_account(db)
    days = await setup_flat(db)
    await pipeline.run_decisions(db, acc, ["AAA"])
    broker = PaperBroker(db, acc)
    await next_day(db, days, 10.0, 10.1, 9.95, 10.05)
    await broker.process_pending()
    await next_day(db, days, 10.0, 99.0, 1.0, 10.0, k=2)
    ev = await broker.manage_positions()
    assert ev[0]["reason"] in ("stop", "stop_gap") and ev[0]["net_pnl"] < 0
    assert cap.equity(acc) < 500.0 and float(acc.total_losses) > 0


async def test_time_exit_after_max_hold(db):
    acc = await cap.get_or_create_account(db)
    days = await setup_flat(db)
    await pipeline.run_decisions(db, acc, ["AAA"])
    broker = PaperBroker(db, acc)
    await next_day(db, days, 10.0, 10.1, 9.95, 10.0)
    await broker.process_pending()
    last = None
    for k in range(2, 8):
        last = await next_day(db, days, 10.0, 10.1, 9.95, 10.0, k=k)
    ev = await broker.manage_positions()
    assert ev and ev[0]["reason"] == "time"


async def test_circuit_breaker_blocks_new_entries(db):
    acc = await cap.get_or_create_account(db)
    await setup_flat(db)
    acc.daily_loss_triggered = True
    await db.commit()
    r = await pipeline.run_decisions(db, acc, ["AAA"])
    assert r["approved"] == 0
    assert any("circuit_breaker" in x for x in r["decisions"][0]["reasons"])


async def test_broker_refuses_orders_without_valid_approval(db):
    acc = await cap.get_or_create_account(db)
    broker = PaperBroker(db, acc)
    req = OrderRequest("AAA", "buy", 5, 10.0, datetime(2026, 9, 30), 9.7, 10.6, 0.2)
    with pytest.raises(RiskViolation):
        await broker.submit_order(req, None)
    with pytest.raises(RiskViolation):                       # approval for 5 shares, order tries 50
        await broker.submit_order(OrderRequest("AAA", "buy", 50, 10.0, datetime(2026, 9, 30)), issue_approval("AAA", "buy", 5, 10.0))
    assert (await db.execute(select(Order))).scalars().all() == []
    order = await broker.submit_order(req, issue_approval("AAA", "buy", 5, 10.0))
    assert order.status == OrderStatus.PENDING


async def test_full_daily_cycle_runs(db):
    acc = await cap.get_or_create_account(db)
    await setup_flat(db)
    out = await pipeline.run_daily_cycle(db, acc, ["AAA"])
    assert out["decisions"]["approved"] == 1 and out["equity"] == 500.0
