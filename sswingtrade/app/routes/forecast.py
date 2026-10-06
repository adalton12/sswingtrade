"""Earnings provision (hypothetical scenarios based on AI evidence) - NOT a guarantee."""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.forecast.service import compare_profiles, run_forecast
from app.runtime.params import PROFILES

router = APIRouter()


class ForecastRequest(BaseModel):
    profile: Optional[str] = None                     # None = current settings
    overrides: Optional[Dict[str, Any]] = None        # any numeric param, e.g. {"capital.per_op_pct": 40}
    start_equity: Optional[float] = Field(None, gt=0)
    weeks: Optional[int] = Field(None, ge=1, le=260)


@router.post("")
async def forecast(req: ForecastRequest, db: AsyncSession = Depends(get_db)) -> dict:
    if req.profile and req.profile not in PROFILES:
        raise HTTPException(422, f"profile must be one of {list(PROFILES)}")
    return await run_forecast(db, req.profile, req.overrides, req.start_equity, req.weeks)


@router.get("/compare")
async def compare(start_equity: Optional[float] = Query(None, gt=0), weeks: Optional[int] = Query(None, ge=1, le=260),
                  db: AsyncSession = Depends(get_db)) -> dict:
    return await compare_profiles(db, start_equity, weeks)
