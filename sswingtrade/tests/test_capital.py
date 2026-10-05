from datetime import date

import pytest

from app.capital import service as cap
from app.capital.rules import CapitalRules, compound_projection, daily_budget, size_position
from app.config import settings


def test_budget_scales_with_equity():
    assert daily_budget(500) == 100.0      # spec: R$100 of R$500
    assert daily_budget(1000) == 200.0     # equity doubles -> R$200
    assert daily_budget(0) == 0.0


def test_size_respects_every_limit():
    r = size_position(equity=500, cash=500, exposure=0, daily_spent=0, price=10.0)
    assert r.qty == 9 and r.binding == "daily_budget"           # floor(100 / (10*(1+fee)))
    r = size_position(500, 500, 0, daily_spent=60, price=10.0)
    assert r.qty == 3                                           # only R$40 of budget left
    r = size_position(500, 500, exposure=480, daily_spent=0, price=10.0)
    assert r.qty == 1 and r.binding in ("exposure", "daily_budget")
    r = size_position(500, cash=5, exposure=0, daily_spent=0, price=10.0)
    assert r.qty == 0 and r.reasons and r.binding == "cash"
    r = size_position(500, 500, 0, 0, price=10.0, split=5)
    assert r.qty == 1 and r.budget_cap == 20.0                  # budget split in up to 5 entries


def test_risk_per_trade_cap():
    # equity 500 -> max loss 2% = R$10; stop distance 5 -> max 2 shares even though budget allows 9
    r = size_position(500, 500, 0, 0, price=10.0, stop_price=5.0)
    assert r.qty == 2 and r.binding == "risk_per_trade" and r.risk_amount == 10.0


def test_compound_projection():
    rows = compound_projection(500, 4, 2.0, weekly_deposit=500)
    assert rows[0]["equity"] == pytest.approx((500 + 500) * 1.02, abs=0.01)
    assert rows[-1]["equity"] > rows[-1]["deposited"] > 500
    assert rows[-1]["per_op_budget"] == pytest.approx(rows[-1]["equity"] * 0.2, abs=0.01)
    flat = compound_projection(500, 10, 0.0)
    assert flat[-1]["equity"] == 500


async def test_account_and_weekly_deposit_idempotent(db):
    acc = await cap.get_or_create_account(db)
    assert cap.equity(acc) == settings.INITIAL_CAPITAL
    again = await cap.get_or_create_account(db)
    assert again.id == acc.id
    # creation counted as this week's deposit -> no double top-up
    assert await cap.start_week(db, acc, date.today()) is None
    # a later week tops up
    later = date.fromordinal(date.today().toordinal() + 14)
    # events are dated "now", so emulate next-week by checking a different week window
    assert await cap.start_week(db, acc, later) == pytest.approx(cap.equity(acc))
    assert cap.equity(acc) == settings.INITIAL_CAPITAL + settings.WEEKLY_DEPOSIT
    assert await cap.start_week(db, acc, later) is None


async def test_monthly_deposit_once_per_month(db):
    acc = await cap.get_or_create_account(db)
    before = cap.equity(acc)
    assert await cap.apply_monthly_deposit(db, acc) is not None
    assert cap.equity(acc) == before + settings.MONTHLY_DEPOSIT
    assert await cap.apply_monthly_deposit(db, acc) is None


async def test_buy_sell_compounding_and_history(db):
    acc = await cap.get_or_create_account(db)
    eq0 = cap.equity(acc)
    await cap.reserve_for_buy(db, acc, notional=100.0, fees=0.5)
    assert float(acc.available_balance) == pytest.approx(eq0 - 100.5)
    assert float(acc.invested_capital) == 100.0
    assert cap.equity(acc) == pytest.approx(eq0 - 0.5)           # entry fee already felt
    # sell for 110 net of exit fees 0.5 -> proceeds_net 109.5
    await cap.release_on_sell(db, acc, notional_cost=100.0, proceeds_net=109.5, position_net_pnl=9.0)
    assert float(acc.invested_capital) == 0.0
    assert cap.equity(acc) == pytest.approx(eq0 + 9.0)           # profit stays and is reinvested
    assert float(acc.total_gains) == 9.0
    assert daily_budget(cap.equity(acc)) > daily_budget(eq0)     # bigger equity -> bigger next budget
    with pytest.raises(ValueError):
        await cap.reserve_for_buy(db, acc, 10_000, 1)


async def test_circuit_breaker_trips_at_limit(db):
    acc = await cap.get_or_create_account(db)               # equity 500, limit 1.5% = R$7.5
    await cap.reserve_for_buy(db, acc, 100.0, 0.0)
    await cap.release_on_sell(db, acc, 100.0, 95.0, -5.0)
    cb = await cap.refresh_circuit_breaker(db, acc)
    assert not cb["tripped"] and cb["daily_pnl"] == -5.0
    await cap.reserve_for_buy(db, acc, 100.0, 0.0)
    await cap.release_on_sell(db, acc, 100.0, 96.0, -4.0)
    cb = await cap.refresh_circuit_breaker(db, acc)
    assert cb["tripped"] and acc.daily_loss_triggered
    await cap.reset_circuit_breaker(db, acc)
    assert not acc.daily_loss_triggered


async def test_circuit_breaker_is_latched_even_if_pnl_recovers(db):
    acc = await cap.get_or_create_account(db)
    await cap.reserve_for_buy(db, acc, 100.0, 0.0)
    await cap.release_on_sell(db, acc, 100.0, 90.0, -10.0)          # -2% > 1.5% limit
    assert (await cap.refresh_circuit_breaker(db, acc))["tripped"]
    await cap.reserve_for_buy(db, acc, 100.0, 0.0)
    await cap.release_on_sell(db, acc, 100.0, 125.0, 25.0)          # big win afterwards
    assert (await cap.refresh_circuit_breaker(db, acc))["tripped"]  # still blocked today
