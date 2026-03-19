# ─────────────────────────────────────────────────────────────────────────────
# FORGE — Multi-stage Dockerfile
# Stage 1: base       — shared deps, non-root user, system packages
# Stage 2: dev        — dev tools, hot reload, debugger
# Stage 3: builder    — compile dependencies for production
# Stage 4: production — minimal, no dev tools, hardened
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1: Base ────────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Non-root user for security
RUN groupadd --gid 1001 forge && \
    useradd --uid 1001 --gid forge --shell /bin/bash --create-home forge

WORKDIR /app

# Python env config
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ── Stage 2: Development ─────────────────────────────────────────────────────
FROM base AS development

# Install all deps including dev tools
COPY pyproject.toml ./
RUN pip install -e ".[dev]"

# Install additional dev tools
RUN pip install \
    watchfiles \
    debugpy \
    ipython

USER forge
COPY --chown=forge:forge . .

# Default: run API with hot reload
CMD ["uvicorn", "forge.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ── Stage 3: Builder (compile wheels) ────────────────────────────────────────
FROM base AS builder

COPY pyproject.toml ./
COPY forge/ ./forge/
RUN pip install setuptools wheel && \
    pip wheel --no-deps --wheel-dir /wheels .

# ── Stage 4: Production ──────────────────────────────────────────────────────
FROM python:3.12-slim AS production

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1001 forge && \
    useradd --uid 1001 --gid forge --shell /bin/bash --create-home forge

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1

# Install runtime + qa extras (pytest/ruff needed by QA agent in workers)
COPY pyproject.toml ./
RUN pip install "setuptools>=61" wheel && pip install ".[qa]"

USER forge
COPY --chown=forge:forge . .

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "forge.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
