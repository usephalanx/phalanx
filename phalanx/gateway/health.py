"""
Gateway health server — lightweight HTTP sidecar for Docker health probes.

The Slack gateway process uses Socket Mode (persistent WebSocket), which has
no HTTP surface for Docker/orchestrator to probe. This module spins up a
minimal aiohttp server on a configurable port (default 8100) that exposes:

  GET /health   — readiness probe (DB connectivity via SELECT 1)
  GET /healthz  — liveness probe (always 200, no dependency checks)

The server runs as a background asyncio task alongside the Socket Mode handler.
If the port fails to bind, it logs an error but does NOT crash the gateway —
the Slack bot continues to work even without the health HTTP surface.

The /health handler uses a fresh ``get_db()`` context per request to respect
the NullPool invariant — sessions are never reused across calls.
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog
from aiohttp import web
from sqlalchemy import text

from phalanx import __version__
from phalanx.config.settings import get_settings

log = structlog.get_logger(__name__)

# Timeout for the SELECT 1 probe — avoids request pile-up on slow DB.
_DB_PROBE_TIMEOUT_SECONDS = 3


class GatewayHealthServer:
    """Lightweight HTTP health server for the Slack gateway process."""

    def __init__(self, port: int | None = None) -> None:
        """Initialise the health server.

        Args:
            port: TCP port to listen on.  Falls back to the
                  ``gateway_health_port`` setting (default 8100).
        """
        settings = get_settings()
        self._port: int = port if port is not None else settings.gateway_health_port
        self._app: web.Application = web.Application()
        self._app.router.add_get("/health", self._health_handler)
        self._app.router.add_get("/healthz", self._healthz_handler)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the HTTP server as a background task.

        Catches ``OSError`` (port in use) and logs rather than crashing
        so the Slack bot remains operational.
        """
        try:
            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, "0.0.0.0", self._port)
            await self._site.start()
            log.info(
                "gateway_health.started",
                port=self._port,
                endpoints=["/health", "/healthz"],
            )
        except OSError as exc:
            log.error(
                "gateway_health.bind_failed",
                port=self._port,
                error=str(exc),
            )
            # Clean up partial state — do not propagate.
            await self._cleanup_runner()

    async def stop(self) -> None:
        """Gracefully shut down the HTTP server.  Idempotent."""
        await self._cleanup_runner()
        log.info("gateway_health.stopped")

    async def _cleanup_runner(self) -> None:
        """Internal helper: tear down runner if it exists."""
        if self._runner is not None:
            with contextlib.suppress(Exception):  # noqa: BLE001
                await self._runner.cleanup()
            self._runner = None
            self._site = None

    # ── Handlers ──────────────────────────────────────────────────────────

    async def _health_handler(self, _request: web.Request) -> web.Response:
        """Readiness probe: verify DB connectivity via ``SELECT 1``.

        Opens a fresh ``get_db()`` async context on every call — never reuses
        sessions across requests.  This respects the NullPool invariant
        required by Celery fork-workers.

        Returns:
            200 with ``{"status": "ok", "version": "<version>"}`` when DB is reachable.
            503 with ``{"status": "unhealthy", "error": "db_unreachable"}`` on failure.
        """
        import json  # noqa: PLC0415

        try:
            from phalanx.db.session import get_db  # noqa: PLC0415

            async with asyncio.timeout(_DB_PROBE_TIMEOUT_SECONDS):
                async with get_db() as session:
                    await session.execute(text("SELECT 1"))
        except Exception as exc:
            log.error("gateway_health.db_unreachable", error=str(exc))
            payload: dict[str, str] = {
                "status": "unhealthy",
                "error": "db_unreachable",
            }
            return web.Response(
                text=json.dumps(payload),
                status=503,
                content_type="application/json",
            )

        payload = {
            "status": "ok",
            "version": __version__,
        }
        return web.Response(
            text=json.dumps(payload),
            status=200,
            content_type="application/json",
        )

    async def _healthz_handler(self, _request: web.Request) -> web.Response:
        """Liveness probe — always returns 200.  No dependency checks."""
        import json  # noqa: PLC0415

        return web.Response(
            text=json.dumps({"status": "ok"}),
            status=200,
            content_type="application/json",
        )
