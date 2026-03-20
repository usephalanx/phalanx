"""
FORGE FastAPI application.
Milestone 1: health check only.
Subsequent milestones add routes incrementally.
"""

import structlog
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from phalanx.api.routes.runs import router as runs_router
from phalanx.api.routes.work_orders import router as work_orders_router
from phalanx.config.settings import get_settings
from phalanx.observability.logging import configure_logging

configure_logging()
log = structlog.get_logger(__name__)

settings = get_settings()

app = FastAPI(
    title="FORGE API",
    description="AI Team Operating System",
    version="0.1.0",
    docs_url="/docs" if not settings.is_production else None,
    redoc_url="/redoc" if not settings.is_production else None,
)

# ── CORS ─────────────────────────────────────────────────────────────────────
# Dev: allow all origins. Production: restrict to explicitly configured origins.
# The FORGE API has no browser frontend for MVP — only internal service calls.
if settings.forge_cors_origins:
    _origins = [o.strip() for o in settings.forge_cors_origins.split(",") if o.strip()]
elif settings.is_production:
    _origins = []  # no browser access in production by default
else:
    _origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)


# ── API Key Middleware ────────────────────────────────────────────────────────
# When FORGE_API_KEY is set, all routes except /health require the header.
# This prevents accidental exposure if the server port becomes public.
@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    if not settings.forge_api_key:
        # Auth disabled (dev/test mode)
        return await call_next(request)

    # Health check is always public — used by Docker healthcheck
    if request.url.path == "/health":
        return await call_next(request)

    api_key = request.headers.get("X-API-Key", "")
    if api_key != settings.forge_api_key:
        log.warning(
            "api.unauthorized",
            path=request.url.path,
            method=request.method,
            client=request.client.host if request.client else "unknown",
        )
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Missing or invalid X-API-Key header"},
        )

    return await call_next(request)


# ── Routes ───────────────────────────────────────────────────────────────────
app.include_router(work_orders_router, prefix="/v1")
app.include_router(runs_router, prefix="/v1")


@app.get("/health")
async def health():
    """
    Health check with optional DB and Redis connectivity.
    Returns degraded status (200) if dependencies are unavailable,
    so Docker doesn't restart the container for transient failures.
    """
    checks: dict = {"status": "ok", "version": "0.1.0", "service": "forge-api"}

    # DB check
    try:
        from sqlalchemy import text  # noqa: PLC0415

        from phalanx.db.session import engine as db_engine  # noqa: PLC0415

        async with db_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception as exc:
        checks["db"] = f"degraded: {exc}"
        checks["status"] = "degraded"

    # Redis check
    try:
        import redis.asyncio as aioredis  # noqa: PLC0415

        r = aioredis.from_url(settings.redis_url, socket_connect_timeout=2)
        await r.ping()
        await r.aclose()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"degraded: {exc}"
        checks["status"] = "degraded"

    return checks


@app.get("/")
async def root():
    return {"message": "FORGE is running. See /docs for API reference."}
