"""Shared test fixtures for the Kanban Board test suite."""

from collections.abc import AsyncGenerator
from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import get_db
from app.main import app
from app.models.base import Base
from app.models.user import User
from app.models.workspace import Workspace

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(TEST_DATABASE_URL, echo=False, future=True)
test_session_factory = async_sessionmaker(
    test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@pytest_asyncio.fixture(autouse=True)
async def setup_database() -> AsyncGenerator[None, None]:
    """Create and tear down all tables for each test."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
    """Override the get_db dependency for testing."""
    async with test_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


app.dependency_overrides[get_db] = override_get_db


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Provide a raw async database session for direct ORM tests."""
    async with test_session_factory() as session:
        yield session
        # Always rollback to avoid commit failures from IntegrityError tests.
        await session.rollback()


@pytest_asyncio.fixture
async def sample_user(db_session: AsyncSession) -> User:
    """Create and return a sample User for model-level tests."""
    user = User(
        email="sample@example.com",
        hashed_password="hashed_sample",
        display_name="Sample User",
    )
    db_session.add(user)
    await db_session.flush()
    return user


@pytest_asyncio.fixture
async def sample_workspace(
    db_session: AsyncSession,
    sample_user: User,
) -> Workspace:
    """Create and return a sample Workspace for model-level tests."""
    ws = Workspace(
        name="Sample Workspace",
        slug="sample-workspace",
        owner_id=sample_user.id,
    )
    db_session.add(ws)
    await db_session.flush()
    return ws


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """Provide an async HTTP test client."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest_asyncio.fixture
async def registered_user(client: AsyncClient) -> dict[str, Any]:
    """Register a test user and return the response data including tokens."""
    response = await client.post(
        "/api/auth/register",
        json={
            "email": "owner@example.com",
            "password": "securepassword",
            "display_name": "Owner User",
        },
    )
    assert response.status_code == 201
    return response.json()


@pytest_asyncio.fixture
async def auth_headers(registered_user: dict[str, Any]) -> dict[str, str]:
    """Return authorization headers for the registered test user."""
    return {"Authorization": f"Bearer {registered_user['access_token']}"}


@pytest_asyncio.fixture
async def second_user(client: AsyncClient) -> dict[str, Any]:
    """Register a second test user and return the response data."""
    response = await client.post(
        "/api/auth/register",
        json={
            "email": "member@example.com",
            "password": "securepassword",
            "display_name": "Member User",
        },
    )
    assert response.status_code == 201
    return response.json()


@pytest_asyncio.fixture
async def second_auth_headers(second_user: dict[str, Any]) -> dict[str, str]:
    """Return authorization headers for the second test user."""
    return {"Authorization": f"Bearer {second_user['access_token']}"}


@pytest_asyncio.fixture
async def workspace(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> dict[str, Any]:
    """Create a test workspace and return the response data."""
    response = await client.post(
        "/api/workspaces",
        json={"name": "Test Workspace", "slug": "test-workspace"},
        headers=auth_headers,
    )
    assert response.status_code == 201
    return response.json()


@pytest_asyncio.fixture
async def board(
    client: AsyncClient,
    auth_headers: dict[str, str],
    workspace: dict[str, Any],
) -> dict[str, Any]:
    """Create a test board in the workspace and return the response data."""
    response = await client.post(
        f"/api/workspaces/{workspace['id']}/boards",
        json={"name": "Test Board", "description": "A test board"},
        headers=auth_headers,
    )
    assert response.status_code == 201
    return response.json()


@pytest_asyncio.fixture
async def column(
    client: AsyncClient,
    auth_headers: dict[str, str],
    board: dict[str, Any],
) -> dict[str, Any]:
    """Create a test column in the board and return the response data."""
    response = await client.post(
        f"/api/boards/{board['id']}/columns",
        json={"name": "To Do"},
        headers=auth_headers,
    )
    assert response.status_code == 201
    return response.json()


@pytest_asyncio.fixture
async def card(
    client: AsyncClient,
    auth_headers: dict[str, str],
    column: dict[str, Any],
) -> dict[str, Any]:
    """Create a test card in the column and return the response data."""
    response = await client.post(
        f"/api/columns/{column['id']}/cards",
        json={"title": "Test Card", "description": "A test card"},
        headers=auth_headers,
    )
    assert response.status_code == 201
    return response.json()
