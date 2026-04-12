"""
Health check tests.

Mocks DB and Redis so these pass in any environment — real connectivity
is verified by the integration test suite against live services.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from phalanx import __version__
from phalanx.api.main import app


@pytest.mark.asyncio
async def test_health_check_ok():
    """Health check returns 200 with {status: 'ok', version: <version>} when DB is reachable."""
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)

    mock_engine = MagicMock()
    mock_engine.connect = MagicMock(return_value=mock_conn)

    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock()
    mock_redis.aclose = AsyncMock()

    with (
        patch("phalanx.api.main.settings") as mock_settings,
        patch("phalanx.db.session.engine", mock_engine),
        patch("redis.asyncio.from_url", return_value=mock_redis),
    ):
        mock_settings.redis_url = "redis://localhost:6379/0"
        mock_settings.forge_api_key = None
        mock_settings.is_production = False
        mock_settings.forge_cors_origins = ""

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["version"] == __version__
    assert data["service"] == "forge-api"
    assert data["db"] == "ok"
    assert data["redis"] == "ok"
    assert "uptime_seconds" in data


@pytest.mark.asyncio
async def test_health_check_ok_content_type():
    """GET /health response Content-Type is application/json."""
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)

    mock_engine = MagicMock()
    mock_engine.connect = MagicMock(return_value=mock_conn)

    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock()
    mock_redis.aclose = AsyncMock()

    with (
        patch("phalanx.api.main.settings") as mock_settings,
        patch("phalanx.db.session.engine", mock_engine),
        patch("redis.asyncio.from_url", return_value=mock_redis),
    ):
        mock_settings.redis_url = "redis://localhost:6379/0"
        mock_settings.forge_api_key = None
        mock_settings.is_production = False
        mock_settings.forge_cors_origins = ""

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")

    assert response.headers["content-type"] == "application/json"


@pytest.mark.asyncio
async def test_health_check_version_matches_module():
    """Version field in /health response matches phalanx.__version__."""
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)

    mock_engine = MagicMock()
    mock_engine.connect = MagicMock(return_value=mock_conn)

    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock()
    mock_redis.aclose = AsyncMock()

    with (
        patch("phalanx.api.main.settings") as mock_settings,
        patch("phalanx.db.session.engine", mock_engine),
        patch("redis.asyncio.from_url", return_value=mock_redis),
    ):
        mock_settings.redis_url = "redis://localhost:6379/0"
        mock_settings.forge_api_key = None
        mock_settings.is_production = False
        mock_settings.forge_cors_origins = ""

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")

    data = response.json()
    assert data["version"] == __version__
    # Ensure it's a non-empty string
    assert isinstance(data["version"], str)
    assert len(data["version"]) > 0


@pytest.mark.asyncio
async def test_health_check_unhealthy_when_db_down():
    """GET /health returns 503 with {status: 'unhealthy'} when DB check raises an exception."""
    mock_engine = MagicMock()
    mock_engine.connect.side_effect = Exception("Connection refused")

    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock()
    mock_redis.aclose = AsyncMock()

    with (
        patch("phalanx.api.main.settings") as mock_settings,
        patch("phalanx.db.session.engine", mock_engine),
        patch("redis.asyncio.from_url", return_value=mock_redis),
    ):
        mock_settings.redis_url = "redis://localhost:6379/0"
        mock_settings.forge_api_key = None
        mock_settings.is_production = False
        mock_settings.forge_cors_origins = ""

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")

    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "unhealthy"


@pytest.mark.asyncio
async def test_health_check_unhealthy_content_type():
    """GET /health returns application/json even on 503 unhealthy responses."""
    mock_engine = MagicMock()
    mock_engine.connect.side_effect = Exception("Connection refused")

    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock()
    mock_redis.aclose = AsyncMock()

    with (
        patch("phalanx.api.main.settings") as mock_settings,
        patch("phalanx.db.session.engine", mock_engine),
        patch("redis.asyncio.from_url", return_value=mock_redis),
    ):
        mock_settings.redis_url = "redis://localhost:6379/0"
        mock_settings.forge_api_key = None
        mock_settings.is_production = False
        mock_settings.forge_cors_origins = ""

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")

    assert response.headers["content-type"] == "application/json"


@pytest.mark.asyncio
async def test_healthz_always_200():
    """GET /healthz returns 200 with {'status': 'ok'} regardless of dependencies."""
    with (
        patch("phalanx.api.main.settings") as mock_settings,
    ):
        mock_settings.forge_api_key = None
        mock_settings.is_production = False
        mock_settings.forge_cors_origins = ""

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/healthz")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_healthz_content_type():
    """GET /healthz response Content-Type is application/json."""
    with (
        patch("phalanx.api.main.settings") as mock_settings,
    ):
        mock_settings.forge_api_key = None
        mock_settings.is_production = False
        mock_settings.forge_cors_origins = ""

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/healthz")

    assert response.headers["content-type"] == "application/json"


@pytest.mark.asyncio
async def test_root():
    """GET / returns 200 with a message field."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/")

    assert response.status_code == 200
    data = response.json()
    assert "message" in data
