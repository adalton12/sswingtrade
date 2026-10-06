"""
Health check routes and system diagnostics.
"""

from datetime import datetime
from typing import Dict

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.runtime.params import params
from app.database import get_db, check_database_health
from app.services.cache import check_redis_health

router = APIRouter()


@router.get("/health")
async def health_check(db: AsyncSession = Depends(get_db)) -> Dict:
    """
    System health check endpoint.

    Returns:
        dict: System health status with all dependencies
    """

    # Check database
    db_health = await check_database_health()

    # Check Redis
    redis_health = await check_redis_health()

    # Ollama check (simple HTTP request)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.OLLAMA_BASE_URL}/api/tags")
            ollama_health = {
                "status": "healthy" if resp.status_code == 200 else "unhealthy",
                "message": "Ollama responding"
            }
    except Exception as e:
        ollama_health = {
            "status": "unhealthy",
            "message": f"Ollama unreachable: {str(e)}"
        }

    # Aggregate status
    all_healthy = all([
        db_health["status"] == "healthy",
        redis_health["status"] == "healthy",
        ollama_health["status"] == "healthy",
    ])

    return {
        "status": "healthy" if all_healthy else "degraded",
        "timestamp": datetime.utcnow().isoformat(),
        "version": settings.APP_VERSION,
        "components": {
            "database": db_health,
            "cache": redis_health,
            "ollama": ollama_health,
        }
    }


@router.get("/health/detailed")
async def health_check_detailed(db: AsyncSession = Depends(get_db)) -> Dict:
    """Detailed health check with configuration."""
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "version": settings.APP_VERSION,
        "environment": settings.APP_ENV,
        "configuration": {
            "parameters": "see /api/v1/settings (runtime-editable)",
            "initial_capital": params.get("capital.initial_capital"),
            "per_op_pct": params.get("capital.per_op_pct"),
            "profile": params.get("risk.profile"),
            "loss_limits_pct": {"day": params.get("limits.daily_loss_pct"), "week": params.get("limits.weekly_loss_pct"),
                                "month": params.get("limits.monthly_loss_pct")},
            "market_data_source": settings.MARKET_DATA_SOURCE,
            "ollama_model": settings.OLLAMA_MODEL,
        }
    }
