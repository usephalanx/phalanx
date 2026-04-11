"""
Integration tests — health endpoint against a real aiosqlite-backed database.

Unlike unit tests (which mock DB/Redis), these tests verify the /health
endpoint actually issues ``SELECT 1`` against a live database engine.

We use aiosqlite (in-memory SQLite) so no external services are needed.
This validates the full code path: HTTP request → handler → SQLAlchemy
engine.connect() → execute(SELECT 1) → JSON response.

Run via:  pytest tests/integration/test_health_integration.py -v
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

import phalanx
from phalanx.api.main import app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def aiosqlite_engine():
    """Create a real async engine backed by aiosqlite (in-memory SQLite)."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    yield engine


# ---------------------------------------------------------------------------
# API /health integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_health_200_with_real_db(aiosqlite_engine) -> None:
    """GET /health returns 200 with {status: 'ok', version, service, db} against a real DB."""
    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock()
    mock_redis.aclose = AsyncMock()

    with (
        patch("phalanx.api.main.settings") as mock_settings,
        patch("phalanx.db.session.engine", aiosqlite_engine),
        patch("redis.asyncio.from_url", return_value=mock_redis),
    ):
        mock_settings.redis_url = "redis://localhost:6379/0"
        mock_settings.forge_api_key = None
        mock_settings.is_production = False
        mock_settings.forge_cors_origins = ""

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["version"] == phalanx.__version__
    assert data["service"] == "forge-api"
    assert data["db"] == "ok"


@pytest.mark.asyncio
async def test_api_health_includes_redis_ok(aiosqlite_engine) -> None:
    """GET /health reports redis as 'ok' when Redis is reachable (mocked)."""
    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock()
    mock_redis.aclose = AsyncMock()

    with (
        patch("phalanx.api.main.settings") as mock_settings,
        patch("phalanx.db.session.engine", aiosqlite_engine),
        patch("redis.asyncio.from_url", return_value=mock_redis),
    ):
        mock_settings.redis_url = "redis://localhost:6379/0"
        mock_settings.forge_api_key = None
        mock_settings.is_production = False
        mock_settings.forge_cors_origins = ""

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/health")

    data = response.json()
    assert data["redis"] == "ok"


@pytest.mark.asyncio
async def test_api_health_version_matches_package(aiosqlite_engine) -> None:
    """Health response version equals phalanx.__version__ with a real DB."""
    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock()
    mock_redis.aclose = AsyncMock()

    with (
        patch("phalanx.api.main.settings") as mock_settings,
        patch("phalanx.db.session.engine", aiosqlite_engine),
        patch("redis.asyncio.from_url", return_value=mock_redis),
    ):
        mock_settings.redis_url = "redis://localhost:6379/0"
        mock_settings.forge_api_key = None
        mock_settings.is_production = False
        mock_settings.forge_cors_origins = ""

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/health")

    data = response.json()
    assert data["version"] == phalanx.__version__


@pytest.mark.asyncio
async def test_api_healthz_returns_200(aiosqlite_engine) -> None:
    """GET /healthz returns 200 liveness probe (no DB or Redis needed)."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


# ---------------------------------------------------------------------------
# Gateway /health integration tests (aiohttp)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_health_200_with_real_db(aiosqlite_engine) -> None:
    """Gateway GET /health returns 200 with {status: 'ok', version} against a real DB."""
    from contextlib import asynccontextmanager
    from collections.abc import AsyncIterator
    from unittest.mock import MagicMock

    from aiohttp.test_utils import TestClient, TestServer
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from phalanx.gateway.health import GatewayHealthServer

    # Build a real async session factory backed by the aiosqlite engine.
    _SessionLocal = async_sessionmaker(
        aiosqlite_engine, class_=AsyncSession, expire_on_commit=False,
    )

    @asynccontextmanager
    async def _real_get_db() -> AsyncIterator[AsyncSession]:
        """Real session context manager using aiosqlite."""
        async with _SessionLocal() as session:
            yield session

    with patch("phalanx.gateway.health.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(gateway_health_port=0)
        server = GatewayHealthServer(port=0)

    with patch("phalanx.db.session.get_db", _real_get_db):
        async with TestClient(TestServer(server._app)) as client:
            resp = await client.get("/health")
            assert resp.status == 200
            data = await resp.json()

    assert data["status"] == "ok"
    assert data["version"] == phalanx.__version__


@pytest.mark.asyncio
async def test_api_health_content_type_is_json(aiosqlite_engine) -> None:
    """Edge case: GET /health Content-Type is application/json with a real DB."""
    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock()
    mock_redis.aclose = AsyncMock()

    with (
        patch("phalanx.api.main.settings") as mock_settings,
        patch("phalanx.db.session.engine", aiosqlite_engine),
        patch("redis.asyncio.from_url", return_value=mock_redis),
    ):
        mock_settings.redis_url = "redis://localhost:6379/0"
        mock_settings.forge_api_key = None
        mock_settings.is_production = False
        mock_settings.forge_cors_origins = ""

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/health")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"


@pytest.mark.asyncio
async def test_gateway_health_content_type_is_json(aiosqlite_engine) -> None:
    """Edge case: Gateway GET /health Content-Type is application/json with a real DB."""
    from contextlib import asynccontextmanager
    from collections.abc import AsyncIterator
    from unittest.mock import MagicMock

    from aiohttp.test_utils import TestClient, TestServer
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from phalanx.gateway.health import GatewayHealthServer

    _SessionLocal = async_sessionmaker(
        aiosqlite_engine, class_=AsyncSession, expire_on_commit=False,
    )

    @asynccontextmanager
    async def _real_get_db() -> AsyncIterator[AsyncSession]:
        """Real session context manager using aiosqlite."""
        async with _SessionLocal() as session:
            yield session

    with patch("phalanx.gateway.health.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(gateway_health_port=0)
        server = GatewayHealthServer(port=0)

    with patch("phalanx.db.session.get_db", _real_get_db):
        async with TestClient(TestServer(server._app)) as client:
            resp = await client.get("/health")
            assert resp.status == 200
            assert resp.content_type == "application/json"


@pytest.mark.asyncio
async def test_gateway_healthz_returns_200() -> None:
    """Gateway GET /healthz returns 200 liveness probe."""
    from unittest.mock import MagicMock

    from aiohttp.test_utils import TestClient, TestServer

    from phalanx.gateway.health import GatewayHealthServer

    with patch("phalanx.gateway.health.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(gateway_health_port=0)
        server = GatewayHealthServer(port=0)

    async with TestClient(TestServer(server._app)) as client:
        resp = await client.get("/healthz")
        assert resp.status == 200
        data = await resp.json()

    assert data["status"] == "ok"
