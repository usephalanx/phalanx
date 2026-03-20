"""
Health check tests.

Mocks DB and Redis so these pass in any environment — real connectivity
is verified by the integration test suite against live services.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from phalanx.api.main import app


@pytest.mark.asyncio
async def test_health_check_ok():
    """Health check returns ok when DB and Redis are reachable (mocked)."""
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
    assert "status" in data
    assert "version" in data
    assert "service" in data


@pytest.mark.asyncio
async def test_health_check_degraded_when_db_down():
    """Health check returns 200 with degraded status when DB is unreachable."""
    mock_engine = MagicMock()
    mock_engine.connect.side_effect = Exception("Connection refused")

    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock()
    mock_redis.aclose = AsyncMock()

    with (
        patch("phalanx.db.session.engine", mock_engine),
        patch("redis.asyncio.from_url", return_value=mock_redis),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")

    # Still 200 — degraded but running
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "degraded"


@pytest.mark.asyncio
async def test_root():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/")

    assert response.status_code == 200
    data = response.json()
    assert "message" in data
