"""
SQLAlchemy async session factory.
Use get_db() as a FastAPI dependency or async context manager.

NullPool is used for Celery workers: each asyncio.run() call creates a fresh
event loop, so a persistent connection pool would produce "Future attached to
a different loop" errors. NullPool opens/closes connections per-request which
is correct for short-lived Celery tasks.
FastAPI uses a real pool (pool_size=10) because it runs a single long-lived
event loop.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from forge.config.settings import get_settings

settings = get_settings()

# Celery workers set FORGE_WORKER=1; they need NullPool to avoid event-loop
# conflicts across asyncio.run() calls. FastAPI uses a persistent pool.
_pool_kwargs: dict = (
    {"poolclass": NullPool}
    if os.environ.get("FORGE_WORKER")
    else {
        "pool_size": 10,
        "max_overflow": 20,
        "pool_pre_ping": True,
        "pool_recycle": 3600,
    }
)

engine = create_async_engine(
    settings.database_url,
    echo=settings.is_development,
    **_pool_kwargs,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,  # objects usable after commit
    autocommit=False,
    autoflush=False,
)


@asynccontextmanager
async def get_db() -> AsyncIterator[AsyncSession]:
    """
    Async context manager for database sessions.
    Usage:
        async with get_db() as db:
            result = await db.execute(...)
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_db_dependency() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency version."""
    async with get_db() as db:
        yield db
