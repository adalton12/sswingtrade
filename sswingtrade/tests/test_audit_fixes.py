"""Regression tests for the issues found in the system audit (R:R rounding, breaker override, brokerage fills)."""

import pandas as pd
import pytest
from sqlalchemy import select

from app.backtesting.costs import CostModel
from app.broker.base import OrderRequest
from app.broker.paper import PaperBroker
from app.capital import service as cap
from app.models import CapitalHistory, MarketCandle, Order, OrderStatus, Position
from app.risk.engine import AccountState, RiskConfig, RiskEngine, TradeRequest, issue_approval


# ----------------------------------------------------------------------------- R:R is judged on exact levels
def _state():
    return AccountState(equity=2000, cash=2000, exposure=0, open_positions=0, open_tickers=set(), daily_spent=0,
                        entries_today=0, circuit_breaker=False)


def test_risk_reward_not_vetoed_by_cent_rounding():
    eng = RiskEngine(RiskConfig(min_score=60, paper_enabled=True))      # target R:R == minimum R:R == 2.0
    # stop 9.16 / target 10.23 are cent-rounded: recomputing from them gives 1.97 and used to veto this trade
    d = eng.evaluate(TradeRequest("AAA", "buy", 9.52, 0.2368, composite_score=75.0), _state())
    assert d.approved, d.reasons
    assert d.stop_price == pytest.approx(9.16) and d.take_profit_price == pytest.approx(10.23)
    assert d.risk_reward == pytest.approx(2.0)


def test_risk_reward_never_rounding_vetoed_over_a_price_grid():
    eng = RiskEngine(RiskConfig(min_score=60, paper_enabled=True))
    bad = []
    for cents in range(500, 9000, 7):
        price = cents / 100
        for atr_pct in (0.012, 0.018, 0.024, 0.031, 0.035):
            d = eng.evaluate(TradeRequest("AAA", "buy", price, price * atr_pct, composite_score=75.0), _state())
            if any(c["rule"] == "risk_reward" and not c["passed"] for c in d.checks):
                bad.append((price, atr_pct))
    assert not bad, f"{len(bad)} trades vetoed on rounding noise, e.g. {bad[:3]}"


def test_a_genuinely_low_risk_reward_is_still_vetoed():
    eng = RiskEngine(RiskConfig(min_score=60, paper_enabled=True))
    # price 10, ATR 0.2 -> risk 0.3 (stop 9.70); a custom target of 10.57 is R:R 1.9 < 2.0
    low = eng.evaluate(TradeRequest("AAA", "buy", 10.0, 0.2, 75.0, custom_take_profit=10.57), _state())
    ok = eng.evaluate(TradeRequest("AAA", "buy", 10.0, 0.2, 75.0, custom_take_profit=10.60), _state())
    assert not low.approved and any(c["rule"] == "risk_reward" and not c["passed"] for c in low.checks)
    assert ok.approved


# ----------------------------------------------------------------------------- operator override of the loss limits
async def _realise_loss(db, acc, amount):
    await cap.reserve_for_buy(db, acc, 100.0, 0.0)
    await cap.release_on_sell(db, acc, 100.0, 100.0 - amount, -amount)
    await db.commit()


async def test_scheduler_reset_never_releases_a_real_loss(db):
    acc = await cap.get_or_create_account(db)                        # equity 500: day R$7.50, week R$20, month R$40
    await _realise_loss(db, acc, 30.0)
    assert set((await cap.refresh_circuit_breaker(db, acc))["tripped_periods"]) == {"day", "week"}
    res = await cap.reset_circuit_breaker(db, acc, "day")            # what the 07:00 job does
    assert res == {"period": "day", "overridden": False}
    cb = await cap.refresh_circuit_breaker(db, acc)
    assert "day" in cb["tripped_periods"] and cb["tripped"]          # still over the limit -> re-latched
    marks = (await db.execute(select(CapitalHistory).where(CapitalHistory.event_type == cap.OVERRIDE_EVENT))).scalars().all()
    assert marks == []


async def test_operator_override_opens_a_fresh_allowance_and_is_audited(db):
    acc = await cap.get_or_create_account(db)
    eq_before = cap.equity(acc)
    await _realise_loss(db, acc, 30.0)
    assert (await cap.refresh_circuit_breaker(db, acc))["tripped"]

    assert (await cap.reset_circuit_breaker(db, acc, "day", override=True))["overridden"] is True
    cb = await cap.refresh_circuit_breaker(db, acc)
    assert "day" not in cb["tripped_periods"] and "week" in cb["tripped_periods"]   # week stays blocked
    assert cb["periods"]["day"]["baseline"] == -30.0 and cb["periods"]["day"]["loss"] == 0.0

    assert (await cap.reset_circuit_breaker(db, acc, "week", override=True))["overridden"] is True
    cb = await cap.refresh_circuit_breaker(db, acc)
    assert not cb["tripped"] and cb["remaining_allowance"] == pytest.approx(7.5, abs=0.01)   # a full day limit again

    marks = (await db.execute(select(CapitalHistory).where(CapitalHistory.event_type == cap.OVERRIDE_EVENT))).scalars().all()
    assert len(marks) == 2 and all(float(m.amount) == 0.0 for m in marks)
    assert {m.extra_data["period"] for m in marks} == {"day", "week"}
    assert cap.equity(acc) == pytest.approx(eq_before - 30.0)       # the marker moves no money

    # losses realised AFTER the override count again and re-latch (no unique-constraint clash with the old latch)
    await _realise_loss(db, acc, 8.0)
    cb = await cap.refresh_circuit_breaker(db, acc)
    assert "day" in cb["tripped_periods"] and cb["periods"]["day"]["loss"] == pytest.approx(8.0)


async def test_override_does_nothing_when_nothing_is_blocked(db):
    acc = await cap.get_or_create_account(db)
    res = await cap.reset_circuit_breaker(db, acc, "day", override=True)
    assert res["overridden"] is False
    assert (await db.execute(select(CapitalHistory).where(CapitalHistory.event_type == cap.OVERRIDE_EVENT))).first() is None


# ----------------------------------------------------------------------------- brokerage-aware fills
async def _pending_order(db, acc, qty=100):
    day = pd.Timestamp("2026-09-30").to_pydatetime()
    nxt = pd.Timestamp("2026-10-01").to_pydatetime()
    db.add_all([MarketCandle(ticker="AAA", date=d, open_price=10.0, high_price=10.1, low_price=9.95, close_price=10.0,
                             volume=1_000_000) for d in (day, nxt)])
    await db.commit()
    broker = PaperBroker(db, acc, costs=CostModel(fee_rate=0.000325, brokerage=5.0, slippage_bps=5.0))
    await broker.submit_order(OrderRequest("AAA", "buy", qty, 10.0, day, 9.7, 10.6, 0.2), issue_approval("AAA", "buy", qty, 10.0))
    return broker


async def test_fill_quantity_accounts_for_brokerage_when_cash_is_the_limit(db):
    acc = await cap.get_or_create_account(db)
    acc.available_balance = 100.14           # old sizing: 10 shares (R$100.10) + R$5 brokerage > cash -> ValueError
    cap._sync_balance(acc)
    await db.commit()
    broker = await _pending_order(db, acc)
    ev = await broker.process_pending()      # must not raise
    assert ev[0]["event"] == "filled" and ev[0]["qty"] == 9
    assert float(acc.available_balance) >= 0
    pos = (await db.execute(select(Position))).scalars().one()
    assert pos.quantity == 9


def test_affordable_qty_is_the_largest_that_fits():
    class _Acc:  # only the cost model is used by the helper
        id = 1
    b = PaperBroker(None, _Acc(), costs=CostModel(fee_rate=0.000325, brokerage=2.5, slippage_bps=5.0))
    for cash in (13.0, 57.31, 100.14, 333.33, 1234.56):
        for entry in (7.77, 10.01, 33.33, 91.49):
            q = b._affordable_qty(10_000, cash, entry)
            cost = lambda n: round(entry * n, 2) + round(b.costs.fees(round(entry * n, 2)), 2)
            assert q == 0 or cost(q) <= cash + 1e-9
            assert cost(q + 1) > cash + 1e-9           # one more share would not fit
    assert b._affordable_qty(3, 10_000, 10.0) == 3     # never more than the order asked for


async def test_unaffordable_order_is_rejected_without_aborting_the_cycle(db, monkeypatch):
    acc = await cap.get_or_create_account(db)
    broker = await _pending_order(db, acc, qty=5)

    async def boom(*a, **k):
        raise ValueError("insufficient cash")
    monkeypatch.setattr(cap, "reserve_for_buy", boom)
    ev = await broker.process_pending()      # must not raise
    assert ev == [{"order_id": 1, "ticker": "AAA", "event": "rejected_no_cash"}]
    order = (await db.execute(select(Order))).scalars().one()
    assert order.status == OrderStatus.REJECTED
