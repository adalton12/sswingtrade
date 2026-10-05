"""
Database initialization, connection management, and session handling.
FASE 1: PostgreSQL + SQLAlchemy ORM + Alembic migrations
"""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.config import settings
from app.models import Base

# ============================================================================
# Database Engine Configuration
# ============================================================================

# Async engine for FastAPI
async_engine = create_async_engine(
    settings.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://"),
    echo=settings.DATABASE_ECHO,
    pool_size=settings.DATABASE_POOL_SIZE,
    pool_recycle=settings.DATABASE_POOL_RECYCLE,
    pool_pre_ping=True,  # Test connections before using
    max_overflow=10,
)

# Async session factory
AsyncSessionLocal = async_sessionmaker(
    async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)

# Sync engine for Alembic migrations
engine = create_engine(
    settings.DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://"),
    echo=settings.DATABASE_ECHO,
    pool_size=settings.DATABASE_POOL_SIZE,
    pool_recycle=settings.DATABASE_POOL_RECYCLE,
)


# ============================================================================
# Database Initialization
# ============================================================================

async def init_db():
    """
    Initialize database schema.
    Creates all tables defined in ORM models.
    """
    async with async_engine.begin() as conn:
        # Create all tables
        await conn.run_sync(Base.metadata.create_all)


async def drop_db():
    """
    Drop all tables (CAUTION: Development only!).
    Used for testing and schema resets.
    """
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def close_db():
    """Close database connections gracefully."""
    await async_engine.dispose()


# ============================================================================
# Session Management
# ============================================================================

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency for FastAPI to provide database sessions.

    Usage:
        @app.get("/items")
        async def get_items(db: AsyncSession = Depends(get_db)):
            ...
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


@asynccontextmanager
async def session_scope(session: Optional[AsyncSession] = None):
    """Use the given session (caller owns it) or open a fresh one."""
    if session is not None:
        yield session
    else:
        async with AsyncSessionLocal() as s:
            yield s


# ============================================================================
# Health Check
# ============================================================================

async def check_database_health() -> dict:
    """
    Check database connectivity and readiness.

    Returns:
        dict: Health status with connection info
    """
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))

        return {
            "status": "healthy",
            "database": "connected",
            "message": "PostgreSQL connection successful"
        }

    except Exception as e:
        return {
            "status": "unhealthy",
            "database": "disconnected",
            "message": f"Database connection failed: {str(e)}"
        }


# ============================================================================
# Schema Management (Alembic Integration)
# ============================================================================

def init_alembic():
    """
    Initialize Alembic for schema migrations.
    Run after initial setup: `alembic init -t async alembic`
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    alembic_cfg = Config("alembic.ini")
    script_dir = ScriptDirectory.from_config(alembic_cfg)

    return alembic_cfg, script_dir


async def run_migrations():
    """
    Run pending database migrations.
    Called during application startup.
    """
    from alembic import command
    from alembic.config import Config

    try:
        alembic_cfg = Config("alembic.ini")
        command.upgrade(alembic_cfg, "head")
        print("✅ Database migrations completed successfully")

    except Exception as e:
        print(f"❌ Migration error: {e}")
        raise


# ============================================================================
# Test Database
# ============================================================================

async def setup_test_db():
    """Set up in-memory test database."""
    test_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    return test_engine


if __name__ == "__main__":
    # Test database connection
    print("Testing database connection...")

    try:
        result = asyncio.run(check_database_health())
        print(f"Database: {result['database']}")
        print(f"Status: {result['status']}")
        print(f"Message: {result['message']}")

    except Exception as e:
        print(f"Error: {e}")
