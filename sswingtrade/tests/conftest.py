import os

os.environ.setdefault("LOG_FILE_PATH", "/tmp/sswingtrade_test.log")

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import pytest

from app.models import Base
from app.runtime.params import params


@pytest.fixture(autouse=True)
def _reset_params():
    params.reset_cache()
    yield
    params.reset_cache()


@pytest_asyncio.fixture
async def db():
    """Real SQLAlchemy async session on in-memory SQLite (no Postgres needed)."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
                              connect_args={"check_same_thread": False})
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(eng, expire_on_commit=False)() as session:
        yield session
    await eng.dispose()
