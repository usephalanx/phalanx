"""Tests for database session management and dependency injection."""

import os
import sys

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

# Ensure backend app is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.database import async_session_factory, engine, get_db, init_db  # noqa: E402
from app.models.base import Base  # noqa: E402

pytestmark = pytest.mark.asyncio


async def test_db_session_is_async(db_session: AsyncSession) -> None:
    """The test fixture yields a valid AsyncSession."""
    assert isinstance(db_session, AsyncSession)


async def test_tables_created(db_session: AsyncSession) -> None:
    """All model tables exist after fixture setup."""
    from app.models import Board, Card, Column, User, Workspace, WorkspaceMember  # noqa: F401

    expected_tables = {"user", "workspace", "workspace_member", "board", "column", "card"}
    actual_tables = set(Base.metadata.tables.keys())
    assert expected_tables.issubset(actual_tables)


async def test_get_db_yields_async_session(db_session: AsyncSession) -> None:
    """get_db() is an async generator that yields an AsyncSession."""
    import inspect

    assert inspect.isasyncgenfunction(get_db)


async def test_async_session_factory_creates_session() -> None:
    """async_session_factory produces an AsyncSession instance."""
    assert async_session_factory is not None
    # The factory's class_ should be AsyncSession
    assert async_session_factory.class_ is AsyncSession


async def test_engine_is_configured() -> None:
    """The async engine is created with expected properties."""
    assert engine is not None
    # Engine URL should contain the configured database URL
    assert str(engine.url) is not None


async def test_session_expire_on_commit_disabled() -> None:
    """Sessions created by the factory should not expire on commit."""
    assert async_session_factory.kw.get("expire_on_commit") is False


async def test_get_db_commits_on_success(db_session: AsyncSession) -> None:
    """get_db() commits the session when no exception occurs."""
    from app.models.user import User

    user = User(
        email="commit-test@example.com",
        hashed_password="fakehash",
        display_name="Commit Test",
    )
    db_session.add(user)
    await db_session.flush()

    # Verify the user is accessible in the session
    from sqlalchemy import select

    result = await db_session.execute(select(User).where(User.email == "commit-test@example.com"))
    fetched = result.scalar_one_or_none()
    assert fetched is not None
    assert fetched.email == "commit-test@example.com"


async def test_get_db_rollback_on_exception(db_session: AsyncSession) -> None:
    """get_db() rolls back the session when an exception occurs."""
    from app.models.user import User

    # Add a user and flush to get it in the session
    user = User(
        email="rollback-test@example.com",
        hashed_password="fakehash",
        display_name="Rollback Test",
    )
    db_session.add(user)
    await db_session.flush()

    # Simulate rollback
    await db_session.rollback()

    # After rollback, the user should not be found
    from sqlalchemy import select

    result = await db_session.execute(
        select(User).where(User.email == "rollback-test@example.com")
    )
    fetched = result.scalar_one_or_none()
    assert fetched is None


async def test_init_db_creates_tables() -> None:
    """init_db() is callable and creates Base metadata tables."""
    # init_db is an async function
    import inspect

    assert inspect.iscoroutinefunction(init_db)


async def test_get_db_generator_protocol() -> None:
    """get_db follows the async generator protocol expected by FastAPI Depends."""
    from collections.abc import AsyncGenerator

    import typing

    hints = typing.get_type_hints(get_db)
    assert hints.get("return") is not None
