"""Runtime settings API: every tunable parameter, aggressiveness profiles, reset."""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings as env
from app.database import get_db
from app.runtime.params import params

router = APIRouter()


class UpdateRequest(BaseModel):
    values: Dict[str, Any]


class ProfileRequest(BaseModel):
    profile: str


class ResetRequest(BaseModel):
    keys: Optional[List[str]] = None


def _after_change(changed: Dict[str, Any]):
    if any(k.startswith("schedule.") for k in changed):
        from app.services.scheduler import reschedule, scheduler
        if scheduler.running:
            reschedule()


@router.get("")
async def get_settings(db: AsyncSession = Depends(get_db)) -> dict:
    await params.refresh(db)
    snap = params.snapshot()
    snap["real_trading_enabled"] = env.ENABLE_REAL_TRADING
    snap["note"] = "ENABLE_REAL_TRADING só pode ser alterado por variável de ambiente (não por esta API)."
    return snap


@router.put("")
async def update_settings(req: UpdateRequest, db: AsyncSession = Depends(get_db)) -> dict:
    await params.refresh(db)
    try:
        applied = await params.update(db, req.values)
    except ValueError as e:
        raise HTTPException(422, str(e))
    _after_change(applied)
    return {"applied": applied, "profile": params.get("risk.profile")}


@router.post("/profile")
async def set_profile(req: ProfileRequest, db: AsyncSession = Depends(get_db)) -> dict:
    await params.refresh(db)
    try:
        applied = await params.apply_profile(db, req.profile)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"profile": req.profile, "applied": applied}


@router.post("/reset")
async def reset_settings(req: ResetRequest, db: AsyncSession = Depends(get_db)) -> dict:
    await params.reset(db, req.keys)
    _after_change({"schedule.x": 1})
    return {"reset": req.keys or "all"}
