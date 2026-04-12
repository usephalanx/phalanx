"""FastAPI application entry point for the Kanban Board backend."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.database import init_db
from app.routes.auth import router as auth_router
from app.routes.boards import router as boards_router
from app.routes.cards import router as cards_router
from app.routes.columns import router as columns_router
from app.routes.websocket import router as websocket_router
from app.routes.workspaces import router as workspaces_router


def create_app() -> FastAPI:
    """Application factory that creates and configures a FastAPI instance.

    Wires up CORS middleware, lifespan hooks, and all API routers.
    Returns a fully configured FastAPI application ready to serve.
    """
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Initialize the database on startup."""
        await init_db()
        yield

    application = FastAPI(
        title="Kanban Board API",
        description="REST API for a collaborative Kanban board SaaS application.",
        version="0.1.0",
        lifespan=lifespan,
    )

    origins = (
        settings.cors_origins.split(",") if settings.cors_origins != "*" else ["*"]
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.include_router(auth_router)
    application.include_router(boards_router)
    application.include_router(cards_router)
    application.include_router(columns_router)
    application.include_router(websocket_router)
    application.include_router(workspaces_router)

    @application.get("/api/health")
    async def health_check() -> dict[str, str]:
        """Health check endpoint."""
        return {"status": "ok"}

    return application


# Module-level app instance used by uvicorn and test fixtures.
app = create_app()
