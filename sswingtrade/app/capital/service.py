"""
Capital service - FASE 7 (DB layer). Every balance change is written to capital_history.

Account fields:
  available_balance = cash;  invested_capital = cost basis of open positions;
  current_balance   = available + invested  (equity at cost, realised P&L included)
"""

from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Account, CapitalHistory, Order, OrderStatus, Position, PositionStatus

DAILY_PNL_EVENTS = ("trade_pnl", "fees")


def _f(v) -> float:
    return float(v) if v is not None else 0.0


async def get_or_create_account(db: AsyncSession, user_id: Optional[str] = None) -> Account:
    user_id = user_id or settings.DEFAULT_USER_ID
    acc = (await db.execute(select(Account).where(Account.user_id == user_id))).scalar_one_or_none()
    if acc:
        return acc
    cap = settings.INITIAL_CAPITAL
    acc = Account(user_id=user_id, initial_capital=cap, current_balance=cap, available_balance=cap,
                  invested_capital=0, weekly_base_allocation=settings.MAX_WEEKLY_CAPITAL,
                  daily_limit_per_operation=settings.MAX_DAILY_ALLOCATION,
                  max_daily_loss_percent=settings.MAX_DAILY_LOSS_PERCENT)
    db.add(acc)
    await db.flush()
    db.add(CapitalHistory(account_id=acc.id, event_type="deposit", amount=cap, balance_before=0, balance_after=cap,
                          description="Initial capital"))
    await db.commit()
    await db.refresh(acc)
    return acc


def equity(acc: Account) -> float:
    return round(_f(acc.available_balance) + _f(acc.invested_capital), 2)


async def _log(db, acc, event_type, amount, before, after, description, meta=None, when=None):
    db.add(CapitalHistory(account_id=acc.id, event_type=event_type, amount=round(amount, 2),
                          balance_before=round(before, 2), balance_after=round(after, 2),
                          description=description, extra_data=meta, date=when or datetime.utcnow()))


def _sync_balance(acc: Account):
    acc.current_balance = round(_f(acc.available_balance) + _f(acc.invested_capital), 2)


async def deposit(db: AsyncSession, acc: Account, amount: float, event_type: str = "deposit",
                  description: str = "", when: Optional[datetime] = None) -> float:
    if amount <= 0:
        raise ValueError("deposit must be > 0")
    before = _f(acc.current_balance)
    acc.available_balance = round(_f(acc.available_balance) + amount, 2)
    if event_type == "monthly_deposit":
        acc.monthly_deposits = round(_f(acc.monthly_deposits) + amount, 2)
    _sync_balance(acc)
    await _log(db, acc, event_type, amount, before, _f(acc.current_balance), description or event_type, when=when)
    await db.commit()
    return _f(acc.current_balance)


async def _has_event(db, acc, types, start: datetime, end: datetime) -> bool:
    n = (await db.execute(select(func.count()).select_from(CapitalHistory).where(and_(
        CapitalHistory.account_id == acc.id, CapitalHistory.event_type.in_(types),
        CapitalHistory.date >= start, CapitalHistory.date < end)))).scalar_one()
    return n > 0


async def start_week(db: AsyncSession, acc: Account, today: Optional[date] = None) -> Optional[float]:
    """
    Weekly working-capital top-up (R$ WEEKLY_DEPOSIT). Idempotent: skipped if any deposit-type
    event already happened this ISO week (account creation counts as the first week's deposit).
    Profits are never withdrawn, so next week's capital = previous capital + profit + new deposit.
    """
    if not settings.WEEKLY_DEPOSIT_ENABLED or settings.WEEKLY_DEPOSIT <= 0:
        return None
    today = today or date.today()
    monday = datetime.combine(today - timedelta(days=today.weekday()), datetime.min.time())
    if await _has_event(db, acc, ("deposit", "weekly_deposit"), monday, monday + timedelta(days=7)):
        return None
    return await deposit(db, acc, settings.WEEKLY_DEPOSIT, "weekly_deposit",
                         f"Weekly working capital (week of {monday.date()})",
                         when=datetime.combine(today, datetime.utcnow().time()))


async def apply_monthly_deposit(db: AsyncSession, acc: Account, today: Optional[date] = None) -> Optional[float]:
    """MONTHLY_DEPOSIT on the first business day of the month (idempotent per month)."""
    if not settings.MONTHLY_DEPOSIT_ENABLED or settings.MONTHLY_DEPOSIT <= 0:
        return None
    today = today or date.today()
    first = datetime(today.year, today.month, 1)
    nxt = datetime(today.year + (today.month == 12), today.month % 12 + 1, 1)
    if await _has_event(db, acc, ("monthly_deposit",), first, nxt):
        return None
    return await deposit(db, acc, settings.MONTHLY_DEPOSIT, "monthly_deposit",
                         f"Monthly deposit {today:%Y-%m}",
                         when=datetime.combine(today, datetime.utcnow().time()))


async def reserve_for_buy(db: AsyncSession, acc: Account, notional: float, fees: float, meta=None):
    """Cash -> invested (cost basis); entry fees hit the balance immediately."""
    if notional + fees > _f(acc.available_balance) + 1e-9:
        raise ValueError("insufficient cash")
    before = _f(acc.current_balance)
    acc.available_balance = round(_f(acc.available_balance) - notional - fees, 2)
    acc.invested_capital = round(_f(acc.invested_capital) + notional, 2)
    _sync_balance(acc)
    await _log(db, acc, "fees", -fees, before, _f(acc.current_balance), "Entry fees", meta)


async def release_on_sell(db: AsyncSession, acc: Account, notional_cost: float, proceeds_net: float,
                          position_net_pnl: float, meta=None):
    """Invested -> cash. `proceeds_net` = sell notional - exit fees; P&L event = proceeds_net - cost basis."""
    before = _f(acc.current_balance)
    acc.available_balance = round(_f(acc.available_balance) + proceeds_net, 2)
    acc.invested_capital = round(max(0.0, _f(acc.invested_capital) - notional_cost), 2)
    if position_net_pnl >= 0:
        acc.total_gains = round(_f(acc.total_gains) + position_net_pnl, 2)
    else:
        acc.total_losses = round(_f(acc.total_losses) + abs(position_net_pnl), 2)
    _sync_balance(acc)
    await _log(db, acc, "trade_pnl", proceeds_net - notional_cost, before, _f(acc.current_balance),
               "Position closed", meta)


async def daily_realized_pnl(db: AsyncSession, acc: Account, day: Optional[date] = None) -> float:
    day = day or date.today()
    start = datetime.combine(day, datetime.min.time())
    total = (await db.execute(select(func.coalesce(func.sum(CapitalHistory.amount), 0)).where(and_(
        CapitalHistory.account_id == acc.id, CapitalHistory.event_type.in_(DAILY_PNL_EVENTS),
        CapitalHistory.date >= start, CapitalHistory.date < start + timedelta(days=1))))).scalar_one()
    return _f(total)


async def refresh_circuit_breaker(db: AsyncSession, acc: Account, day: Optional[date] = None) -> dict:
    """Trip when today's realised loss >= MAX_DAILY_LOSS_PERCENT of start-of-day equity."""
    pnl = await daily_realized_pnl(db, acc, day)
    start_equity = equity(acc) - pnl
    loss_pct = (-pnl / start_equity * 100) if start_equity > 0 and pnl < 0 else 0.0
    limit = _f(acc.max_daily_loss_percent or settings.MAX_DAILY_LOSS_PERCENT)
    # LATCHED for the rest of the day: a later win must not re-enable entries; only
    # reset_circuit_breaker (next-day job) or an operator can clear it.
    tripped = bool(acc.daily_loss_triggered) or loss_pct >= limit
    if tripped and not acc.daily_loss_triggered:
        acc.daily_loss_triggered = True
        await db.commit()
    return {"daily_pnl": round(pnl, 2), "daily_loss_pct": round(loss_pct, 3), "tripped": tripped}


async def reset_circuit_breaker(db: AsyncSession, acc: Account):
    if acc.daily_loss_triggered:
        acc.daily_loss_triggered = False
        await db.commit()


async def exposure_and_spent(db: AsyncSession, acc: Account, signal_date: Optional[datetime] = None) -> dict:
    """
    Exposure = cost basis of OPEN positions + notional of PENDING orders.
    daily_spent = notional of non-cancelled orders decided on `signal_date`.
    """
    pend = (await db.execute(select(Order).where(Order.account_id == acc.id,
                                                  Order.status == OrderStatus.PENDING))).scalars().all()
    pending_notional = sum(_f(o.target_price) * (o.quantity or 0) for o in pend)
    open_pos = (await db.execute(select(Position).where(Position.account_id == acc.id,
                                                         Position.status == PositionStatus.OPEN))).scalars().all()
    exposure = sum(_f(p.entry_price) * (p.quantity or 0) for p in open_pos) + pending_notional
    spent, entries = 0.0, 0
    if signal_date is not None:
        orders = (await db.execute(select(Order).where(
            Order.account_id == acc.id, Order.signal_date == signal_date,
            Order.status.notin_([OrderStatus.CANCELLED, OrderStatus.REJECTED])))).scalars().all()
        spent = sum(_f(o.target_price) * (o.quantity or 0) for o in orders)
        entries = len(orders)
    return {"exposure": round(exposure, 2), "pending_notional": round(pending_notional, 2),
            "open_positions": len(open_pos), "open_tickers": {p.ticker for p in open_pos} | {o.ticker for o in pend},
            "daily_spent": round(spent, 2), "entries_today": entries}
