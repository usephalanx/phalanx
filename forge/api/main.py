"""
FORGE FastAPI application.
Milestone 1: health check only.
Subsequent milestones add routes incrementally.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import structlog

log = structlog.get_logger()

app = FastAPI(
    title="FORGE API",
    description="AI Team Operating System",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    """Health check. Verified by Docker and load balancer."""
    # M1: basic health check
    # M3+: add db + redis connection checks
    return {
        "status": "ok",
        "version": "0.1.0",
        "service": "forge-api",
    }


@app.get("/")
async def root():
    return {"message": "FORGE is running. See /docs for API reference."}
