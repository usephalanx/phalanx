"""Unit tests for the FastAPI /health route in api/routes/health.py."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_health_route_ok():
    """GET /health returns 200 when DB and Redis both ok."""
    from phalanx.api.main import app

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
        mock_settings.redis_url = "redis://localhost/0"
        mock_settings.forge_api_key = ""
        mock_settings.is_production = False
        mock_settings.forge_cors_origins = ""

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] in ("ok", "degraded")


@pytest.mark.asyncio
async def test_api_health_route_db_error():
    """GET /health returns degraded/unhealthy when DB fails."""
    from phalanx.api.main import app

    with (
        patch("phalanx.api.main.settings") as mock_settings,
        patch("phalanx.db.session.engine") as mock_engine,
        patch("redis.asyncio.from_url", side_effect=Exception("redis down")),
    ):
        mock_settings.redis_url = "redis://localhost/0"
        mock_settings.forge_api_key = ""
        mock_settings.is_production = False
        mock_settings.forge_cors_origins = ""
        mock_engine.connect.side_effect = Exception("DB unavailable")

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/health")

    # DB failure → 503 or 200 degraded
    assert response.status_code in (200, 503)


@pytest.mark.asyncio
async def test_api_routes_health_check_db_and_redis():
    """Test the api/routes/health.py _check_db and _check_redis functions."""
    from phalanx.api.routes.health import _check_db, _check_redis

    # _check_db with mocked engine
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_engine = MagicMock()
    mock_engine.connect = MagicMock(return_value=mock_conn)

    with patch("phalanx.db.session.engine", mock_engine):
        result = await _check_db()
    assert result == "ok"

    # _check_redis with mocked aioredis
    mock_r = AsyncMock()
    mock_r.ping = AsyncMock()
    mock_r.aclose = AsyncMock()
    with patch("redis.asyncio.from_url", return_value=mock_r):
        result = await _check_redis("redis://localhost/0")
    assert result == "ok"


@pytest.mark.asyncio
async def test_api_routes_health_endpoint_full():
    """Test the health endpoint handler from api/routes/health.py directly."""
    from phalanx.api.routes.health import health as health_handler, _CHECK_TIMEOUT_SECONDS

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_engine = MagicMock()
    mock_engine.connect = MagicMock(return_value=mock_conn)

    mock_r = AsyncMock()
    mock_r.ping = AsyncMock()
    mock_r.aclose = AsyncMock()

    with (
        patch("phalanx.db.session.engine", mock_engine),
        patch("redis.asyncio.from_url", return_value=mock_r),
    ):
        response = await health_handler()

    assert response.status_code == 200
    import json as _json
    data = _json.loads(response.body)
    assert data["status"] == "ok"
    assert data["db"] == "ok"
    assert data["redis"] == "ok"
    assert "version" in data


@pytest.mark.asyncio
async def test_api_routes_health_unhealthy_on_db_error():
    """Health endpoint returns 503 when DB is down."""
    from phalanx.api.routes.health import health as health_handler

    with (
        patch("phalanx.db.session.engine") as mock_engine,
        patch("redis.asyncio.from_url", side_effect=Exception("redis down")),
    ):
        mock_engine.connect.side_effect = Exception("DB down")
        response = await health_handler()

    import json as _json
    data = _json.loads(response.body)
    assert data["status"] == "unhealthy"
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_api_routes_health_degraded_on_redis_error():
    """Health endpoint returns 200 degraded when only Redis is down."""
    from phalanx.api.routes.health import health as health_handler

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_engine = MagicMock()
    mock_engine.connect = MagicMock(return_value=mock_conn)

    with (
        patch("phalanx.db.session.engine", mock_engine),
        patch("redis.asyncio.from_url", side_effect=Exception("redis down")),
    ):
        response = await health_handler()

    import json as _json
    data = _json.loads(response.body)
    assert data["status"] == "degraded"
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_api_routes_health_db_timeout():
    """Health endpoint marks DB as unhealthy on timeout."""
    import asyncio
    from phalanx.api.routes.health import health as health_handler

    async def slow_db():
        await asyncio.sleep(100)

    with (
        patch("phalanx.api.routes.health._check_db", side_effect=asyncio.TimeoutError()),
        patch("redis.asyncio.from_url", side_effect=Exception("redis down")),
    ):
        response = await health_handler()

    import json as _json
    data = _json.loads(response.body)
    assert data["db"] == "timeout"
    assert data["status"] == "unhealthy"


@pytest.mark.asyncio
async def test_api_routes_health_redis_timeout():
    """Health endpoint marks Redis as degraded on timeout."""
    import asyncio
    from phalanx.api.routes.health import health as health_handler

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_engine = MagicMock()
    mock_engine.connect = MagicMock(return_value=mock_conn)

    with (
        patch("phalanx.db.session.engine", mock_engine),
        patch("phalanx.api.routes.health._check_redis", side_effect=asyncio.TimeoutError()),
    ):
        response = await health_handler()

    import json as _json
    data = _json.loads(response.body)
    assert data["redis"] == "timeout"
    assert data["status"] == "degraded"
