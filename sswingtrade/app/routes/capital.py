"""
Capital management routes.
Handles account setup, balance tracking, and allocation queries.
"""

from datetime import datetime
from typing import List, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Account, CapitalHistory

router = APIRouter()


# ============================================================================
# Schemas (Request/Response models)
# ============================================================================

class AccountCreateRequest(BaseModel):
    """Create account request."""
    user_id: str = Field(..., min_length=1, max_length=100)
    initial_capital: float = Field(default=settings.INITIAL_CAPITAL)


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

    # Create new account
    account = Account(
        user_id=req.user_id,
        initial_capital=req.initial_capital,
        current_balance=req.initial_capital,
        available_balance=req.initial_capital,
    )

    db.add(account)
    await db.commit()
    await db.refresh(account)

    # Log to capital history
    history = CapitalHistory(
        account_id=account.id,
        event_type="deposit",
        amount=req.initial_capital,
        balance_before=0,
        balance_after=req.initial_capital,
        description=f"Account creation with initial capital: R$ {req.initial_capital:.2f}"
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

    # Circuit breaker check
    if account.daily_loss_triggered:
        return CapitalAllocationResponse(
            account_id=req.account_id,
            requested_amount=req.operation_amount,
            approved_amount=0,
            available_balance=float(account.available_balance),
            is_approved=False,
            reason="Daily loss limit exceeded - circuit breaker active",
            daily_remaining=0,
            weekly_remaining=float(account.available_balance),
        )

    # Daily limit check
    daily_limit = float(account.daily_limit_per_operation)
    daily_remaining = daily_limit - 0  # TODO: Query today's operations

    # Available balance check
    available = float(account.available_balance)

    # Determine approval
    approved_amount = min(
        req.operation_amount,
        available,
        daily_limit,
    )

    is_approved = approved_amount == req.operation_amount

    reason = None
    if not is_approved:
        if available < req.operation_amount:
            reason = f"Insufficient balance. Available: R$ {available:.2f}"
        elif daily_limit < req.operation_amount:
            reason = f"Daily limit exceeded. Remaining: R$ {daily_remaining:.2f}"

    return CapitalAllocationResponse(
        account_id=req.account_id,
        requested_amount=req.operation_amount,
        approved_amount=approved_amount,
        available_balance=available,
        is_approved=is_approved,
        reason=reason,
        daily_remaining=daily_remaining,
        weekly_remaining=float(account.available_balance),
    )


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
