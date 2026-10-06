"""
Capital service - FASE 7 (DB layer). Every balance change is written to capital_history.

Account fields:
  available_balance = cash;  invested_capital = cost basis of open positions;
  current_balance   = available + invested  (equity at cost, realised P&L included)

All amounts/limits come from runtime parameters (app.runtime.params), never hard-coded.
Loss limits (day / week / month) are LATCHED: once hit, entries stay blocked until the period ends.
"""

from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (Account, CapitalHistory, LossLimitEvent, Order, OrderStatus, PlannedDeposit, Position,
                        PositionStatus)
from app.runtime.params import params

PNL_EVENTS = ("trade_pnl", "fees")
DEPOSIT_EVENTS = ("deposit", "weekly_deposit", "monthly_deposit", "planned_deposit")
OVERRIDE_EVENT = "breaker_override"      # ledger marker (amount 0) written by an operator override
PERIODS = ("day", "week", "month")


def _f(v) -> float:
    return float(v) if v is not None else 0.0


# ------------------------------------------------------------------------------------ account
async def get_or_create_account(db: AsyncSession, user_id: Optional[str] = None) -> Account:
    from app.config import settings
    user_id = user_id or settings.DEFAULT_USER_ID
    acc = (await db.execute(select(Account).where(Account.user_id == user_id))).scalar_one_or_none()
    if acc:
        return acc
    cap = float(params.get("capital.initial_capital"))
    acc = Account(user_id=user_id, initial_capital=cap, current_balance=cap, available_balance=cap,
                  invested_capital=0, weekly_base_allocation=params.get("capital.weekly_deposit"),
                  daily_limit_per_operation=round(cap * params.get("capital.per_op_pct") / 100, 2),
                  max_daily_loss_percent=params.get("limits.daily_loss_pct"))
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


# ------------------------------------------------------------------------------------ deposits
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
    Weekly working-capital top-up (capital.weekly_deposit). Idempotent: skipped if any deposit-type
    event already happened this ISO week (account creation counts as the first week's deposit).
    Profits are never withdrawn, so next week's capital = previous capital + profit + new deposit.
    """
    amount = params.get("capital.weekly_deposit")
    if not params.get("capital.weekly_deposit_enabled") or amount <= 0:
        return None
    today = today or date.today()
    monday = datetime.combine(today - timedelta(days=today.weekday()), datetime.min.time())
    if await _has_event(db, acc, DEPOSIT_EVENTS, monday, monday + timedelta(days=7)):
        return None
    return await deposit(db, acc, amount, "weekly_deposit", f"Weekly working capital (week of {monday.date()})",
                         when=datetime.combine(today, datetime.utcnow().time()))


async def apply_monthly_deposit(db: AsyncSession, acc: Account, today: Optional[date] = None) -> Optional[float]:
    """capital.monthly_deposit on the first business day of the month (idempotent per month)."""
    amount = params.get("capital.monthly_deposit")
    if not params.get("capital.monthly_deposit_enabled") or amount <= 0:
        return None
    today = today or date.today()
    first = datetime(today.year, today.month, 1)
    nxt = datetime(today.year + (today.month == 12), today.month % 12 + 1, 1)
    if await _has_event(db, acc, ("monthly_deposit",), first, nxt):
        return None
    return await deposit(db, acc, amount, "monthly_deposit", f"Monthly deposit {today:%Y-%m}",
                         when=datetime.combine(today, datetime.utcnow().time()))


async def add_planned_deposit(db: AsyncSession, acc: Account, due: date, amount: float, note: str = "") -> PlannedDeposit:
    if amount <= 0:
        raise ValueError("amount must be > 0")
    pd_ = PlannedDeposit(account_id=acc.id, due_date=datetime.combine(due, datetime.min.time()), amount=amount, note=note[:200])
    db.add(pd_)
    await db.commit()
    await db.refresh(pd_)
    return pd_


async def apply_planned_deposits(db: AsyncSession, acc: Account, today: Optional[date] = None) -> List[dict]:
    """Apply every planned deposit that is due (any amount, any date) exactly once."""
    today = today or date.today()
    cutoff = datetime.combine(today, datetime.min.time()) + timedelta(days=1)
    due = (await db.execute(select(PlannedDeposit).where(
        PlannedDeposit.account_id == acc.id, PlannedDeposit.applied == False,  # noqa: E712
        PlannedDeposit.due_date < cutoff).order_by(PlannedDeposit.due_date))).scalars().all()
    out = []
    for p in due:
        await deposit(db, acc, _f(p.amount), "planned_deposit", p.note or f"Planned deposit {p.due_date.date()}",
                      when=datetime.combine(today, datetime.utcnow().time()))
        p.applied, p.applied_at = True, datetime.utcnow()
        out.append({"id": p.id, "amount": _f(p.amount), "due": p.due_date.date().isoformat()})
    await db.commit()
    return out


# ------------------------------------------------------------------------------------ trades
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


# ------------------------------------------------------------------------------------ loss limits
def period_start(kind: str, today: Optional[date] = None) -> datetime:
    today = today or date.today()
    if kind == "day":
        d = today
    elif kind == "week":
        d = today - timedelta(days=today.weekday())
    elif kind == "month":
        d = today.replace(day=1)
    else:
        raise ValueError(kind)
    return datetime.combine(d, datetime.min.time())


def period_end(kind: str, start: datetime) -> datetime:
    if kind == "day":
        return start + timedelta(days=1)
    if kind == "week":
        return start + timedelta(days=7)
    return datetime(start.year + (start.month == 12), start.month % 12 + 1, 1)


async def period_pnl(db, acc, start: datetime, end: datetime) -> float:
    return _f((await db.execute(select(func.coalesce(func.sum(CapitalHistory.amount), 0)).where(and_(
        CapitalHistory.account_id == acc.id, CapitalHistory.event_type.in_(PNL_EVENTS),
        CapitalHistory.date >= start, CapitalHistory.date < end)))).scalar_one())


async def period_start_equity(db, acc, start: datetime) -> float:
    """Equity right before the period started (from the ledger)."""
    prev = (await db.execute(select(CapitalHistory.balance_after).where(
        CapitalHistory.account_id == acc.id, CapitalHistory.date < start)
        .order_by(CapitalHistory.date.desc(), CapitalHistory.id.desc()).limit(1))).scalar_one_or_none()
    if prev is not None:
        return _f(prev)
    first = (await db.execute(select(CapitalHistory).where(
        CapitalHistory.account_id == acc.id, CapitalHistory.date >= start)
        .order_by(CapitalHistory.date.asc(), CapitalHistory.id.asc()).limit(1))).scalar_one_or_none()
    if first is not None:
        return _f(first.balance_before) or _f(first.balance_after)
    return equity(acc)


def _limit_value(kind: str, start_equity: float) -> Optional[float]:
    """Effective R$ loss allowed in the period = min(pct * start equity, absolute R$). None = disabled."""
    cands = []
    pct = params.get(f"limits.{ {'day': 'daily', 'week': 'weekly', 'month': 'monthly'}[kind] }_loss_pct")
    amt = params.get(f"limits.{ {'day': 'daily', 'week': 'weekly', 'month': 'monthly'}[kind] }_loss_amount")
    if pct and pct > 0:
        cands.append(start_equity * pct / 100)
    if amt and amt > 0:
        cands.append(amt)
    return min(cands) if cands else None


async def _override_baseline(db, acc: Account, kind: str, start: datetime) -> float:
    """Realised P&L of the period at the moment of the latest operator override (0 = never overridden)."""
    rows = (await db.execute(select(CapitalHistory).where(
        CapitalHistory.account_id == acc.id, CapitalHistory.event_type == OVERRIDE_EVENT)
        .order_by(CapitalHistory.id.desc()))).scalars().all()
    for r in rows:
        m = r.extra_data or {}
        if m.get("period") == kind and m.get("period_start") == start.isoformat():
            return _f(m.get("baseline_pnl"))
    return 0.0


async def loss_status(db: AsyncSession, acc: Account, today: Optional[date] = None) -> Dict[str, dict]:
    """Per-period realised P&L vs configured limit, with latch state. After an operator override only the
    P&L realised SINCE the override counts against the limit (`baseline` = P&L at override time)."""
    out = {}
    for kind in PERIODS:
        start = period_start(kind, today)
        end = period_end(kind, start)
        pnl = await period_pnl(db, acc, start, end)
        baseline = await _override_baseline(db, acc, kind, start)
        base = await period_start_equity(db, acc, start)
        limit = _limit_value(kind, base)
        latched = (await db.execute(select(LossLimitEvent.id).where(
            LossLimitEvent.account_id == acc.id, LossLimitEvent.period == kind,
            LossLimitEvent.period_start == start))).first() is not None
        loss = max(0.0, -(pnl - baseline))
        hit = limit is not None and loss >= limit - 1e-9
        out[kind] = {"start": start.isoformat(), "pnl": round(pnl, 2), "baseline": round(baseline, 2),
                     "start_equity": round(base, 2),
                     "loss": round(loss, 2), "loss_pct": round(loss / base * 100, 3) if base > 0 else 0.0,
                     "limit": round(limit, 2) if limit is not None else None,
                     "remaining": round(max(0.0, limit - loss), 2) if limit is not None else None,
                     "tripped": latched or hit, "newly_hit": hit and not latched}
    return out


async def refresh_circuit_breaker(db: AsyncSession, acc: Account, day: Optional[date] = None) -> dict:
    """
    Evaluate day/week/month loss limits, latch any newly hit one, and mirror the result on
    Account.daily_loss_triggered. `remaining_allowance` is the tightest R$ still allowed to lose.
    """
    st = await loss_status(db, acc, day)
    changed = False
    for kind, s in st.items():
        if s["newly_hit"]:
            db.add(LossLimitEvent(account_id=acc.id, period=kind, period_start=datetime.fromisoformat(s["start"]),
                                  pnl=s["pnl"], limit_value=s["limit"]))
            changed = True
    manual = bool(acc.daily_loss_triggered)          # operator/manual latch survives until reset
    tripped_periods = [k for k, s in st.items() if s["tripped"]]
    tripped = manual or bool(tripped_periods)
    if tripped and not acc.daily_loss_triggered:
        acc.daily_loss_triggered = True
        changed = True
    if changed:
        await db.commit()
    rem = [s["remaining"] for s in st.values() if s["remaining"] is not None]
    return {"daily_pnl": st["day"]["pnl"], "daily_loss_pct": st["day"]["loss_pct"], "tripped": tripped,
            "tripped_periods": tripped_periods, "periods": st,
            "remaining_allowance": min(rem) if rem else None}


async def reset_circuit_breaker(db: AsyncSession, acc: Account, period: str = "day", today: Optional[date] = None,
                                override: bool = False) -> dict:
    """
    Clear the latch of the CURRENT `period`.

    override=False (morning job): only drops a stale latch/flag. A loss that is still over the limit re-latches on
    the next evaluation, so the scheduler can never grant extra loss allowance by itself.
    override=True (operator): additionally records a ledger marker with the period's P&L at this moment, so only
    losses realised AFTER it count against the limit again (a fresh allowance of one full limit). Only written when
    the period is actually blocked; the marker is the audit trail of who lifted the block and when.
    """
    start = period_start(period, today)
    overridden = False
    if override:
        st = (await loss_status(db, acc, today))[period]
        if st["tripped"]:
            eq = equity(acc)
            await _log(db, acc, OVERRIDE_EVENT, 0.0, eq, eq, f"Operator override of {period} loss limit",
                       {"period": period, "period_start": start.isoformat(), "baseline_pnl": st["pnl"],
                        "limit": st["limit"], "loss_at_override": st["loss"]})
            overridden = True
    for ev in (await db.execute(select(LossLimitEvent).where(
            LossLimitEvent.account_id == acc.id, LossLimitEvent.period == period,
            LossLimitEvent.period_start == start))).scalars().all():
        await db.delete(ev)
    acc.daily_loss_triggered = False
    await db.commit()
    return {"period": period, "overridden": overridden}


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
