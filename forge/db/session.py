"""
SQLAlchemy async session factory.
Use get_db() as a FastAPI dependency or async context manager.
"""
from contextlib import asynccontextmanager
from typing import AsyncIterator
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from forge.config.settings import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    echo=settings.is_development,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,       # reconnect on stale connections
    pool_recycle=3600,        # recycle connections every hour
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,   # objects usable after commit
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
