"""
Capital management routes.
Handles account setup, balance tracking, and allocation queries.
"""

from datetime import date, datetime
from typing import List, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.runtime.params import params
from app.database import get_db
from app.models import Account, CapitalHistory

router = APIRouter()


# ============================================================================
# Schemas (Request/Response models)
# ============================================================================

class AccountCreateRequest(BaseModel):
    """Create account request."""
    user_id: str = Field(..., min_length=1, max_length=100)
    initial_capital: Optional[float] = Field(default=None, gt=0)  # None = parameter capital.initial_capital


class AccountResponse(BaseModel):
    """Account details response."""
    id: int
    user_id: str
    current_balance: float
    available_balance: float
    invested_capital: float
    weekly_base_allocation: float
    daily_limit_per_operation: float
    total_gains: float
    total_losses: float
    daily_loss_triggered: bool
    created_at: datetime


class CapitalAllocationRequest(BaseModel):
    """Request for capital allocation calculation."""
    account_id: int
    operation_amount: float = Field(..., gt=0)
    operation_type: str = Field(..., pattern="^(buy|sell)$")


class CapitalAllocationResponse(BaseModel):
    """Capital allocation response."""
    account_id: int
    requested_amount: float
    approved_amount: float
    available_balance: float
    is_approved: bool
    reason: Optional[str] = None
    daily_remaining: float
    weekly_remaining: float


class CapitalHistoryResponse(BaseModel):
    """Capital history entry response."""
    id: int
    event_type: str
    amount: float
    balance_before: float
    balance_after: float
    description: Optional[str]
    date: datetime


# ============================================================================
# Routes
# ============================================================================

@router.post("/account", response_model=AccountResponse)
async def create_account(
    req: AccountCreateRequest,
    db: AsyncSession = Depends(get_db)
) -> AccountResponse:
    """
    Create a new trading account.

    Args:
        req: Account creation request
        db: Database session

    Returns:
        AccountResponse: Created account details
    """

    # Check if account already exists
    stmt = select(Account).where(Account.user_id == req.user_id)
    existing = await db.execute(stmt)

    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=400,
            detail=f"Account already exists for user {req.user_id}"
        )

    cap0 = req.initial_capital if req.initial_capital is not None else params.get("capital.initial_capital")
    # Create new account
    account = Account(
        user_id=req.user_id,
        initial_capital=cap0,
        current_balance=cap0,
        available_balance=cap0,
    )

    db.add(account)
    await db.commit()
    await db.refresh(account)

    # Log to capital history
    history = CapitalHistory(
        account_id=account.id,
        event_type="deposit",
        amount=cap0,
        balance_before=0,
        balance_after=cap0,
        description=f"Account creation with initial capital: R$ {cap0:.2f}"
    )
    db.add(history)
    await db.commit()

    return AccountResponse(
        id=account.id,
        user_id=account.user_id,
        current_balance=float(account.current_balance),
        available_balance=float(account.available_balance),
        invested_capital=float(account.invested_capital),
        weekly_base_allocation=float(account.weekly_base_allocation),
        daily_limit_per_operation=float(account.daily_limit_per_operation),
        total_gains=float(account.total_gains),
        total_losses=float(account.total_losses),
        daily_loss_triggered=account.daily_loss_triggered,
        created_at=account.created_at,
    )


@router.get("/account/{account_id}", response_model=AccountResponse)
async def get_account(
    account_id: int,
    db: AsyncSession = Depends(get_db)
) -> AccountResponse:
    """Get account details by ID."""

    stmt = select(Account).where(Account.id == account_id)
    account = await db.execute(stmt)
    account = account.scalar_one_or_none()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    return AccountResponse(
        id=account.id,
        user_id=account.user_id,
        current_balance=float(account.current_balance),
        available_balance=float(account.available_balance),
        invested_capital=float(account.invested_capital),
        weekly_base_allocation=float(account.weekly_base_allocation),
        daily_limit_per_operation=float(account.daily_limit_per_operation),
        total_gains=float(account.total_gains),
        total_losses=float(account.total_losses),
        daily_loss_triggered=account.daily_loss_triggered,
        created_at=account.created_at,
    )


@router.post("/allocation/check", response_model=CapitalAllocationResponse)
async def check_capital_allocation(
    req: CapitalAllocationRequest,
    db: AsyncSession = Depends(get_db)
) -> CapitalAllocationResponse:
    """
    Check if capital allocation is approved by Risk Engine.

    Validates against:
    - MAX_DAILY_ALLOCATION (R$ 100/day)
    - MAX_WEEKLY_CAPITAL (R$ 500/week)
    - Account available balance
    - Circuit breaker (max daily loss)
    """

    stmt = select(Account).where(Account.id == req.account_id)
    account = await db.execute(stmt)
    account = account.scalar_one_or_none()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    from app.capital.rules import daily_budget
    from app.capital.service import equity as _equity, exposure_and_spent

    eq = _equity(account)
    budget = daily_budget(eq)
    st = await exposure_and_spent(db, account, datetime.combine(datetime.utcnow().date(), datetime.min.time()))
    available = float(account.available_balance)
    daily_remaining = max(0.0, budget - st["daily_spent"])
    weekly_remaining = max(0.0, min(available, eq - st["exposure"]))

    def resp(approved_amount, ok, reason=None):
        return CapitalAllocationResponse(
            account_id=req.account_id, requested_amount=req.operation_amount, approved_amount=round(approved_amount, 2),
            available_balance=available, is_approved=ok, reason=reason,
            daily_remaining=round(daily_remaining, 2), weekly_remaining=round(weekly_remaining, 2))

    if account.daily_loss_triggered:
        return resp(0, False, "Daily loss limit exceeded - circuit breaker active")

    approved = min(req.operation_amount, daily_remaining, weekly_remaining, available)
    ok = approved >= req.operation_amount - 1e-9
    reason = None
    if not ok:
        if available < req.operation_amount:
            reason = f"Insufficient balance. Available: R$ {available:.2f}"
        elif daily_remaining < req.operation_amount:
            reason = f"Daily budget exceeded. Remaining: R$ {daily_remaining:.2f} (budget R$ {budget:.2f})"
        else:
            reason = f"Exposure limit exceeded. Room: R$ {weekly_remaining:.2f}"
    return resp(approved, ok, reason)


# ============================================================================
# FASE 7 - working capital, compounding, sizing
# ============================================================================

class DepositRequest(BaseModel):
    amount: float = Field(..., gt=0)
    description: str = ""


class SizingRequest(BaseModel):
    price: float = Field(..., gt=0)
    stop_price: Optional[float] = Field(None, gt=0)
    split: int = Field(1, ge=1, le=10)


@router.get("/summary")
async def capital_summary(db: AsyncSession = Depends(get_db)) -> dict:
    """Equity, cash, exposure, today's budget and circuit-breaker state of the default account."""
    from app.capital import service as cap
    from app.capital.rules import CapitalRules, daily_budget

    acc = await cap.get_or_create_account(db)
    eq = cap.equity(acc)
    today = datetime.combine(datetime.utcnow().date(), datetime.min.time())
    st = await cap.exposure_and_spent(db, acc, today)
    cb = await cap.refresh_circuit_breaker(db, acc)
    deposited = float((await db.execute(
        select(func.coalesce(func.sum(CapitalHistory.amount), 0)).where(
            CapitalHistory.account_id == acc.id,
            CapitalHistory.event_type.in_(["deposit", "weekly_deposit", "monthly_deposit", "planned_deposit"])))).scalar_one())
    return {
        "account_id": acc.id, "equity": eq, "cash": float(acc.available_balance), "invested_cost": float(acc.invested_capital),
        "total_deposited": round(deposited, 2), "net_profit": round(eq - deposited, 2),
        "return_on_deposits_pct": round((eq / deposited - 1) * 100, 2) if deposited else None,
        "total_gains": float(acc.total_gains), "total_losses": float(acc.total_losses),
        "per_operation_budget_today": daily_budget(eq), "per_op_pct": CapitalRules.from_params().per_op_pct,
        "daily_spent": st["daily_spent"], "exposure": st["exposure"], "open_positions": st["open_positions"],
        "circuit_breaker": cb,
    }


@router.post("/deposit")
async def manual_deposit(req: DepositRequest, db: AsyncSession = Depends(get_db)) -> dict:
    from app.capital import service as cap
    acc = await cap.get_or_create_account(db)
    bal = await cap.deposit(db, acc, req.amount, "deposit", req.description or "Manual deposit")
    return {"equity": bal}


@router.post("/week/start")
async def start_week(db: AsyncSession = Depends(get_db)) -> dict:
    """Add this week's working capital (idempotent per ISO week)."""
    from app.capital import service as cap
    acc = await cap.get_or_create_account(db)
    bal = await cap.start_week(db, acc)
    return {"applied": bal is not None, "equity": cap.equity(acc)}


@router.post("/sizing")
async def sizing_preview(req: SizingRequest, db: AsyncSession = Depends(get_db)) -> dict:
    """How many shares the capital rules allow right now (before the Risk Engine)."""
    from app.capital import service as cap
    from app.capital.rules import size_position
    acc = await cap.get_or_create_account(db)
    today = datetime.combine(datetime.utcnow().date(), datetime.min.time())
    st = await cap.exposure_and_spent(db, acc, today)
    r = size_position(cap.equity(acc), float(acc.available_balance), st["exposure"], st["daily_spent"],
                      req.price, req.stop_price, req.split,
                      max_loss_amount=(await cap.refresh_circuit_breaker(db, acc))["remaining_allowance"])
    return r.__dict__


@router.get("/projection")
async def projection(weeks: int = Query(26, ge=1, le=260), weekly_return_pct: float = Query(1.0, ge=-20, le=20),
                     start_equity: Optional[float] = Query(None, gt=0),
                     db: AsyncSession = Depends(get_db)) -> dict:
    """What-if compound simulation (NOT a forecast). Uses configured weekly/monthly deposits."""
    from app.capital import service as cap
    from app.capital.rules import compound_projection
    start = start_equity
    if start is None:
        start = cap.equity(await cap.get_or_create_account(db))
    wd = params.get("capital.weekly_deposit") if params.get("capital.weekly_deposit_enabled") else 0.0
    md = params.get("capital.monthly_deposit") if params.get("capital.monthly_deposit_enabled") else 0.0
    rows = compound_projection(start, weeks, weekly_return_pct, wd, md)
    return {"assumptions": {"start_equity": start, "weekly_return_pct": weekly_return_pct,
                            "weekly_deposit": wd, "monthly_deposit": md},
            "disclaimer": "Hypothetical compounding, not a prediction of results.", "projection": rows}


@router.get("/account/{account_id}/history", response_model=List[CapitalHistoryResponse])
async def get_capital_history(
    account_id: int,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db)
) -> List[CapitalHistoryResponse]:
    """Get capital movement history for an account."""

    stmt = (
        select(CapitalHistory)
        .where(CapitalHistory.account_id == account_id)
        .order_by(CapitalHistory.date.desc())
        .limit(limit)
        .offset(offset)
    )

    result = await db.execute(stmt)
    history_items = result.scalars().all()

    return [
        CapitalHistoryResponse(
            id=h.id,
            event_type=h.event_type,
            amount=float(h.amount),
            balance_before=float(h.balance_before),
            balance_after=float(h.balance_after),
            description=h.description,
            date=h.date,
        )
        for h in history_items
    ]


# ============================================================================
# Variable deposits + loss limits
# ============================================================================
class PlannedDepositRequest(BaseModel):
    due_date: date
    amount: float = Field(..., gt=0)
    note: str = ""


@router.get("/planned-deposits")
async def list_planned_deposits(db: AsyncSession = Depends(get_db)) -> dict:
    from app.capital import service as cap
    from app.models import PlannedDeposit
    acc = await cap.get_or_create_account(db)
    rows = (await db.execute(select(PlannedDeposit).where(PlannedDeposit.account_id == acc.id)
                             .order_by(PlannedDeposit.due_date))).scalars().all()
    return {"planned_deposits": [{"id": r.id, "due_date": r.due_date.date().isoformat(), "amount": float(r.amount),
                                  "note": r.note, "applied": r.applied} for r in rows]}


@router.post("/planned-deposits")
async def create_planned_deposit(req: PlannedDepositRequest, db: AsyncSession = Depends(get_db)) -> dict:
    """Schedule a deposit of ANY amount on ANY date (applied by the morning job, or immediately if already due)."""
    from app.capital import service as cap
    acc = await cap.get_or_create_account(db)
    pd_ = await cap.add_planned_deposit(db, acc, req.due_date, req.amount, req.note)
    applied = await cap.apply_planned_deposits(db, acc)
    return {"id": pd_.id, "applied_now": [a for a in applied if a["id"] == pd_.id]}


@router.delete("/planned-deposits/{pid}")
async def delete_planned_deposit(pid: int, db: AsyncSession = Depends(get_db)) -> dict:
    from app.models import PlannedDeposit
    row = await db.get(PlannedDeposit, pid)
    if not row or row.applied:
        raise HTTPException(404, "pending planned deposit not found")
    await db.delete(row)
    await db.commit()
    return {"deleted": pid}


@router.get("/loss-limits")
async def loss_limits(db: AsyncSession = Depends(get_db)) -> dict:
    """Day / week / month realised P&L vs configured limits (percent and/or fixed R$)."""
    from app.capital import service as cap
    acc = await cap.get_or_create_account(db)
    return await cap.refresh_circuit_breaker(db, acc)
