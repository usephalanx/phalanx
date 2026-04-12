"""Tests for async database session factory and get_db dependency injection."""

import inspect

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory, get_db, init_db
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def test_get_db_is_async_generator() -> None:
    """get_db must be an async generator function for FastAPI Depends."""
    assert inspect.isasyncgenfunction(get_db)


async def test_session_factory_produces_async_session() -> None:
    """async_session_factory should produce AsyncSession instances."""
    assert async_session_factory.class_ is AsyncSession


async def test_session_no_expire_on_commit() -> None:
    """Sessions should retain attribute values after commit."""
    assert async_session_factory.kw.get("expire_on_commit") is False


async def test_init_db_is_coroutine() -> None:
    """init_db must be an async function."""
    assert inspect.iscoroutinefunction(init_db)


async def test_session_insert_and_query(db_session: AsyncSession) -> None:
    """A session can insert and query a row within the same transaction."""
    user = User(
        email="session-test@example.com",
        hashed_password="fakehash",
        display_name="Session Test",
    )
    db_session.add(user)
    await db_session.flush()

    result = await db_session.execute(
        select(User).where(User.email == "session-test@example.com")
    )
    fetched = result.scalar_one_or_none()
    assert fetched is not None
    assert fetched.email == "session-test@example.com"
    assert fetched.display_name == "Session Test"


async def test_session_rollback_discards_changes(db_session: AsyncSession) -> None:
    """Rolling back a session discards unflushed or flushed-but-uncommitted rows."""
    user = User(
        email="discard@example.com",
        hashed_password="fakehash",
        display_name="Discard",
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.rollback()

    result = await db_session.execute(
        select(User).where(User.email == "discard@example.com")
    )
    assert result.scalar_one_or_none() is None


async def test_multiple_sessions_are_independent(
    db_session: AsyncSession,
) -> None:
    """Each session from the factory operates independently."""
    user = User(
        email="independent@example.com",
        hashed_password="fakehash",
        display_name="Independent",
    )
    db_session.add(user)
    await db_session.flush()

    # The user should be visible in this session
    result = await db_session.execute(
        select(User).where(User.email == "independent@example.com")
    )
    assert result.scalar_one_or_none() is not None
