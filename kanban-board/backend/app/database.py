"""Database session and engine configuration for async SQLAlchemy + asyncpg.

Provides:
  - ``engine`` — async engine created via ``create_async_engine``
  - ``async_session_factory`` — bound ``async_sessionmaker`` producing ``AsyncSession``
  - ``get_db()`` — async generator suitable for ``FastAPI.Depends`` injection
  - ``init_db()`` — DDL helper for local dev / tests (production uses Alembic)
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models.base import Base

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    future=True,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async database session for FastAPI dependency injection.

    Usage in a route::

        @router.get("/items")
        async def list_items(db: AsyncSession = Depends(get_db)):
            ...

    The session auto-commits on success and rolls back on unhandled
    exceptions.  It is always closed when the generator exits.
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Create all tables if they don't already exist.

    This is primarily used for local development and testing.
    Production deployments should use Alembic migrations.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
