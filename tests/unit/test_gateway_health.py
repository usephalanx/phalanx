"""
Unit tests for the gateway health HTTP server.

All DB interactions are mocked — real connectivity is verified by integration tests.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import phalanx
from phalanx.gateway.health import GatewayHealthServer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def health_server() -> GatewayHealthServer:
    """Create a GatewayHealthServer with a mocked settings port."""
    with patch("phalanx.gateway.health.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(gateway_health_port=8100)
        server = GatewayHealthServer(port=8100)
    return server


@pytest.fixture
def aiohttp_app(health_server: GatewayHealthServer) -> web.Application:
    """Return the raw aiohttp Application for test-client use."""
    return health_server._app


def _mock_get_db_ok() -> MagicMock:
    """Create a mock get_db that returns a working async session context."""
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()

    @asynccontextmanager
    async def _fake_get_db() -> AsyncIterator[AsyncMock]:
        """Fake get_db context manager yielding a mock session."""
        yield mock_session

    return _fake_get_db


def _mock_get_db_fail(error: str = "Connection refused") -> MagicMock:
    """Create a mock get_db that raises on enter."""

    @asynccontextmanager
    async def _fake_get_db() -> AsyncIterator[AsyncMock]:
        """Fake get_db context manager that raises."""
        raise Exception(error)
        yield  # pragma: no cover — unreachable, needed for generator syntax

    return _fake_get_db


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_returns_200_when_db_reachable(aiohttp_app: web.Application) -> None:
    """GET /health returns 200 with correct schema when DB is reachable."""
    with patch("phalanx.db.session.get_db", _mock_get_db_ok()):
        async with TestClient(TestServer(aiohttp_app)) as client:
            resp = await client.get("/health")
            assert resp.status == 200
            data = await resp.json()

    assert data["status"] == "ok"
    assert data["version"] == phalanx.__version__


@pytest.mark.asyncio
async def test_health_returns_503_when_db_unreachable(aiohttp_app: web.Application) -> None:
    """GET /health returns 503 when DB connection fails."""
    with patch("phalanx.db.session.get_db", _mock_get_db_fail()):
        async with TestClient(TestServer(aiohttp_app)) as client:
            resp = await client.get("/health")
            assert resp.status == 503
            data = await resp.json()

    assert data["status"] == "unhealthy"
    assert data["error"] == "db_unreachable"


@pytest.mark.asyncio
async def test_healthz_always_returns_200(aiohttp_app: web.Application) -> None:
    """GET /healthz always returns 200 with no dependency checks."""
    async with TestClient(TestServer(aiohttp_app)) as client:
        resp = await client.get("/healthz")
        assert resp.status == 200
        data = await resp.json()

    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_health_version_matches_package(aiohttp_app: web.Application) -> None:
    """Health response version field matches phalanx.__version__."""
    with patch("phalanx.db.session.get_db", _mock_get_db_ok()):
        async with TestClient(TestServer(aiohttp_app)) as client:
            resp = await client.get("/health")
            data = await resp.json()

    assert data["version"] == phalanx.__version__


@pytest.mark.asyncio
async def test_health_ok_has_no_error_field(aiohttp_app: web.Application) -> None:
    """Successful health response does not include an 'error' key."""
    with patch("phalanx.db.session.get_db", _mock_get_db_ok()):
        async with TestClient(TestServer(aiohttp_app)) as client:
            resp = await client.get("/health")
            data = await resp.json()

    assert "error" not in data


@pytest.mark.asyncio
async def test_health_503_has_no_version_field(aiohttp_app: web.Application) -> None:
    """Unhealthy response does not include a 'version' key."""
    with patch("phalanx.db.session.get_db", _mock_get_db_fail()):
        async with TestClient(TestServer(aiohttp_app)) as client:
            resp = await client.get("/health")
            data = await resp.json()

    assert "version" not in data


@pytest.mark.asyncio
async def test_server_start_and_stop_lifecycle() -> None:
    """GatewayHealthServer.start() and stop() are safe to call."""
    with patch("phalanx.gateway.health.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(gateway_health_port=0)
        server = GatewayHealthServer(port=0)

    await server.start()
    assert server._runner is not None
    assert server._site is not None

    await server.stop()
    assert server._runner is None


@pytest.mark.asyncio
async def test_stop_is_idempotent() -> None:
    """Calling stop() multiple times does not raise."""
    with patch("phalanx.gateway.health.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(gateway_health_port=0)
        server = GatewayHealthServer(port=0)

    # stop() before start() — should not raise
    await server.stop()
    await server.stop()


@pytest.mark.asyncio
async def test_start_logs_error_on_port_conflict() -> None:
    """If the port is already in use, start() logs error but doesn't crash."""
    with patch("phalanx.gateway.health.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(gateway_health_port=0)
        server1 = GatewayHealthServer(port=0)

    await server1.start()

    # Get the actual port that was assigned
    assert server1._site is not None
    actual_port = server1._site._server.sockets[0].getsockname()[1]

    # Try to bind a second server to the same port
    with patch("phalanx.gateway.health.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(gateway_health_port=actual_port)
        server2 = GatewayHealthServer(port=actual_port)

    # Should not raise — logs error and continues
    await server2.start()
    # The second server should have cleaned up
    assert server2._runner is None

    await server1.stop()


@pytest.mark.asyncio
async def test_health_content_type_is_json(aiohttp_app: web.Application) -> None:
    """Edge case: GET /health response Content-Type is application/json."""
    with patch("phalanx.db.session.get_db", _mock_get_db_ok()):
        async with TestClient(TestServer(aiohttp_app)) as client:
            resp = await client.get("/health")
            assert resp.status == 200
            assert resp.content_type == "application/json"


@pytest.mark.asyncio
async def test_health_503_content_type_is_json(aiohttp_app: web.Application) -> None:
    """Edge case: GET /health 503 response Content-Type is application/json."""
    with patch("phalanx.db.session.get_db", _mock_get_db_fail()):
        async with TestClient(TestServer(aiohttp_app)) as client:
            resp = await client.get("/health")
            assert resp.status == 503
            assert resp.content_type == "application/json"


@pytest.mark.asyncio
async def test_healthz_content_type_is_json(aiohttp_app: web.Application) -> None:
    """Edge case: GET /healthz response Content-Type is application/json."""
    async with TestClient(TestServer(aiohttp_app)) as client:
        resp = await client.get("/healthz")
        assert resp.status == 200
        assert resp.content_type == "application/json"


@pytest.mark.asyncio
async def test_health_returns_503_on_db_timeout(aiohttp_app: web.Application) -> None:
    """GET /health returns 503 when DB probe exceeds _DB_PROBE_TIMEOUT_SECONDS."""

    @asynccontextmanager
    async def _hanging_get_db() -> AsyncIterator[AsyncMock]:
        """Fake get_db that simulates a hanging DB connection."""
        mock_session = AsyncMock()

        async def _hang(*args, **kwargs):
            await asyncio.sleep(60)

        mock_session.execute = _hang
        yield mock_session

    with (
        patch("phalanx.db.session.get_db", _hanging_get_db),
        patch("phalanx.gateway.health._DB_PROBE_TIMEOUT_SECONDS", 0.1),
    ):
        async with TestClient(TestServer(aiohttp_app)) as client:
            resp = await client.get("/health")
            assert resp.status == 503
            data = await resp.json()

    assert data["status"] == "unhealthy"
    assert data["error"] == "db_unreachable"


@pytest.mark.asyncio
async def test_cleanup_runner_swallows_exception() -> None:
    """_cleanup_runner does not raise even if runner.cleanup() errors."""
    with patch("phalanx.gateway.health.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(gateway_health_port=0)
        server = GatewayHealthServer(port=0)

    await server.start()
    # Patch AppRunner.cleanup at class level to raise
    from aiohttp.web_runner import AppRunner
    with patch.object(AppRunner, "cleanup", new=AsyncMock(side_effect=RuntimeError("cleanup failed"))):
        # Should not raise — exception is swallowed
        await server.stop()
    assert server._runner is None


@pytest.mark.asyncio
async def test_health_uses_fresh_get_db_context(aiohttp_app: web.Application) -> None:
    """Each /health request opens a fresh get_db() context (NullPool invariant)."""
    call_count = 0

    @asynccontextmanager
    async def _counting_get_db() -> AsyncIterator[AsyncMock]:
        """Fake get_db that counts invocations."""
        nonlocal call_count
        call_count += 1
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock()
        yield mock_session

    with patch("phalanx.db.session.get_db", _counting_get_db):
        async with TestClient(TestServer(aiohttp_app)) as client:
            await client.get("/health")
            await client.get("/health")

    assert call_count == 2, "get_db() must be called fresh on every request"
