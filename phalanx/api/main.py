"""
FORGE FastAPI application.
Milestone 1: health check only.
Subsequent milestones add routes incrementally.
"""

import structlog
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from phalanx import __version__
from phalanx.api.routes.ci_fix_runs import router as ci_fix_runs_router
from phalanx.api.routes.ci_integrations import router as ci_integrations_router
from phalanx.api.routes.ci_webhooks import router as ci_webhooks_router
from phalanx.api.routes.demos import router as demos_router
from phalanx.api.routes.health import router as health_router
from phalanx.api.routes.runs import router as runs_router
from phalanx.api.routes.traces import portal_router as traces_portal_router
from phalanx.api.routes.traces import router as traces_router
from phalanx.api.routes.work_orders import router as work_orders_router
from phalanx.config.settings import get_settings
from phalanx.observability.logging import configure_logging

configure_logging()
log = structlog.get_logger(__name__)

settings = get_settings()

app = FastAPI(
    title="FORGE API",
    description="AI Team Operating System",
    version=__version__,
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
    if request.url.path in ("/health", "/healthz"):
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
app.include_router(traces_router, prefix="/v1")
app.include_router(traces_portal_router)
app.include_router(ci_webhooks_router, prefix="/webhook")
app.include_router(demos_router, prefix="/v1")
app.include_router(ci_integrations_router, prefix="/v1")
app.include_router(ci_fix_runs_router, prefix="/v1")
app.include_router(health_router)


@app.get("/")
async def root():
    return {"message": "FORGE is running. See /docs for API reference."}
