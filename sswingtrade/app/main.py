"""
SSWingTrade - Multi-Agent Swing Trading Platform
Main FastAPI Application Entry Point

FASE 1: Foundation + API + Database Connection
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import settings
from app.database import engine, get_db, init_db
from app.routes import health, capital, market_data, indicators
from app.services.logger import setup_logging
from app.services.cache import cache
from app.services.scheduler import start_scheduler, stop_scheduler

# Setup logging
logger = setup_logging(__name__)


class HealthResponse(BaseModel):
    """Health check response model."""
    status: str
    timestamp: datetime
    version: str
    database: str
    cache: str
    ollama: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifecycle management.

    Startup: Initialize database, migrate schema
    Shutdown: Close connections gracefully
    """
    logger.info("🚀 Starting SSWingTrade Application")

    try:
        # Initialize database schema
        await init_db()
        logger.info("✅ Database initialized")

        # Connect Redis (non-fatal: cache helpers degrade gracefully)
        try:
            await cache.connect()
        except Exception as e:
            logger.warning(f"Redis unavailable, continuing without cache: {e}")

        # Start market data scheduler (FASE 2)
        if settings.SCHEDULER_ENABLED:
            await start_scheduler()
            logger.info("✅ Market data scheduler started")

    except Exception as e:
        logger.error(f"❌ Failed to initialize: {e}")
        raise

    yield

    # Shutdown scheduler
    if settings.SCHEDULER_ENABLED:
        await stop_scheduler()
    await cache.disconnect()

    logger.info("🛑 Shutting down SSWingTrade Application")


# Create FastAPI app with lifespan
app = FastAPI(
    title="SSWingTrade API",
    description="Multi-Agent Platform for Swing Trading Analysis & Execution",
    version=settings.APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================================
# ROUTES
# ============================================================================

@app.get("/", tags=["Root"])
async def root():
    """Root endpoint - API information."""
    return {
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "environment": settings.APP_ENV,
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health", tags=["Health"])
async def health_check():
    """Health check (used by Docker HEALTHCHECK). Delegates to the router."""
    return await health.health_check()


# Include routers
app.include_router(health.router, prefix="/api/v1", tags=["Health"])
app.include_router(capital.router, prefix="/api/v1/capital", tags=["Capital Management"])
app.include_router(market_data.router, prefix="/api/v1/market", tags=["Market Data"])


app.include_router(indicators.router, prefix="/api/v1/indicators", tags=["Indicators"])


# ============================================================================
# ERROR HANDLERS
# ============================================================================

@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    """Global HTTP exception handler with logging."""
    logger.error(f"HTTP {exc.status_code}: {exc.detail}")
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail,
            "status_code": exc.status_code,
            "timestamp": datetime.utcnow().isoformat(),
        },
    )


@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    """Global exception handler for unexpected errors."""
    logger.exception(f"Unexpected error: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "status_code": 500,
            "timestamp": datetime.utcnow().isoformat(),
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.APP_ENV == "development",
        log_level=settings.LOG_LEVEL.lower(),
    )
