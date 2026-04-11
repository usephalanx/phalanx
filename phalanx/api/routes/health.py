"""
Health check router — GET /health.

Returns structured health status with DB and Redis connectivity checks,
dynamic version from phalanx.__version__, and configurable timeouts.

HTTP 200: status is 'ok' (all checks pass) or 'degraded' (Redis only down).
HTTP 503: status is 'unhealthy' (DB is unreachable — critical dependency).
"""

from __future__ import annotations

import asyncio
import time
from typing import Literal

import structlog
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from phalanx import __version__
from phalanx.config.settings import get_settings

log = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])

# Module-level process start time for uptime calculation.
_PROCESS_START_TIME: float = time.monotonic()

# Timeout in seconds for dependency checks.
_CHECK_TIMEOUT_SECONDS: float = 3.0


class HealthResponse(BaseModel):
    """Pydantic schema for the /health endpoint response."""

    status: Literal["ok", "degraded", "unhealthy"]
    version: str
    service: str
    db: str
    redis: str
    uptime_seconds: float


async def _check_db() -> str:
    """Attempt a lightweight DB connectivity check (SELECT 1)."""
    from sqlalchemy import text  # noqa: PLC0415

    from phalanx.db.session import engine as db_engine  # noqa: PLC0415

    async with db_engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return "ok"


async def _check_redis(redis_url: str) -> str:
    """Attempt a Redis PING."""
    import redis.asyncio as aioredis  # noqa: PLC0415

    r = aioredis.from_url(redis_url, socket_connect_timeout=2)
    try:
        await r.ping()
    finally:
        await r.aclose()
    return "ok"


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={
        200: {"description": "Healthy or degraded (Redis-only failure)"},
        503: {"description": "Unhealthy — critical dependency (DB) is down"},
    },
)
async def health() -> JSONResponse:
    """
    Health check with DB and Redis connectivity.

    Returns 503 when the database is unreachable (critical dependency).
    Returns 200 with 'degraded' status when only Redis is down.
    """
    settings = get_settings()

    db_status = "ok"
    redis_status = "ok"
    overall: Literal["ok", "degraded", "unhealthy"] = "ok"

    # ── DB check (critical) ──────────────────────────────────────────────
    try:
        db_status = await asyncio.wait_for(
            _check_db(),
            timeout=_CHECK_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        db_status = "timeout"
        overall = "unhealthy"
        log.warning("health.db_timeout", timeout=_CHECK_TIMEOUT_SECONDS)
    except Exception as exc:
        db_status = f"error: {exc}"
        overall = "unhealthy"
        log.warning("health.db_error", error=str(exc))

    # ── Redis check (non-critical) ───────────────────────────────────────
    try:
        redis_status = await asyncio.wait_for(
            _check_redis(settings.redis_url),
            timeout=_CHECK_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        redis_status = "timeout"
        if overall != "unhealthy":
            overall = "degraded"
        log.warning("health.redis_timeout", timeout=_CHECK_TIMEOUT_SECONDS)
    except Exception as exc:
        redis_status = f"error: {exc}"
        if overall != "unhealthy":
            overall = "degraded"
        log.warning("health.redis_error", error=str(exc))

    body = HealthResponse(
        status=overall,
        version=__version__,
        service="forge-api",
        db=db_status,
        redis=redis_status,
        uptime_seconds=round(time.monotonic() - _PROCESS_START_TIME, 2),
    )

    http_status = (
        status.HTTP_503_SERVICE_UNAVAILABLE
        if overall == "unhealthy"
        else status.HTTP_200_OK
    )

    return JSONResponse(
        status_code=http_status,
        content=body.model_dump(),
    )
