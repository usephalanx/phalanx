"""
CI Integrations admin API.

Endpoints:
  POST   /v1/ci-integrations          — register a repo for CI auto-fix
  GET    /v1/ci-integrations          — list all integrations
  GET    /v1/ci-integrations/{id}     — get one integration
  PATCH  /v1/ci-integrations/{id}     — update (enable/disable, rotate token)
  DELETE /v1/ci-integrations/{id}     — remove

These are internal admin endpoints — protected by X-API-Key like everything else.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from phalanx.db.models import CIIntegration
from phalanx.db.session import get_db

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/ci-integrations", tags=["ci-integrations"])


# ── Request / Response schemas ────────────────────────────────────────────────

class CIIntegrationCreate(BaseModel):
    repo_full_name: str = Field(..., description="owner/repo, e.g. acme/backend")
    ci_provider: str = Field(default="github_actions", description="github_actions | buildkite")
    github_token: str | None = Field(default=None, description="GitHub PAT or App token")
    ci_api_key: str | None = Field(default=None, description="CI provider API key (stored as-is for now)")
    webhook_secret: str | None = Field(default=None, description="HMAC secret for webhook verification")
    max_attempts: int = Field(default=2, ge=1, le=5)
    auto_commit: bool = Field(default=True, description="Auto-commit fixes to the branch")
    allowed_authors: list[str] = Field(default_factory=list, description="Only fix PRs by these GitHub logins. Empty = fix all.")


class CIIntegrationUpdate(BaseModel):
    github_token: str | None = None
    ci_api_key: str | None = None
    webhook_secret: str | None = None
    max_attempts: int | None = Field(default=None, ge=1, le=5)
    auto_commit: bool | None = None
    allowed_authors: list[str] | None = None
    enabled: bool | None = None


class CIIntegrationResponse(BaseModel):
    id: str
    repo_full_name: str
    ci_provider: str
    max_attempts: int
    auto_commit: bool
    allowed_authors: list[str]
    enabled: bool
    has_github_token: bool
    has_ci_api_key: bool
    has_webhook_secret: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm(cls, obj: CIIntegration) -> "CIIntegrationResponse":
        return cls(
            id=obj.id,
            repo_full_name=obj.repo_full_name,
            ci_provider=obj.ci_provider,
            max_attempts=obj.max_attempts,
            auto_commit=obj.auto_commit,
            allowed_authors=obj.allowed_authors or [],
            enabled=obj.enabled,
            has_github_token=bool(obj.github_token),
            has_ci_api_key=bool(obj.ci_api_key_enc),
            has_webhook_secret=False,
            created_at=obj.created_at,
            updated_at=obj.updated_at,
        )


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("", status_code=status.HTTP_201_CREATED, response_model=CIIntegrationResponse)
async def register_integration(body: CIIntegrationCreate):
    """Register a repo for CI auto-fix. Idempotent — re-registering updates the config."""
    async with get_db() as session:
        # Upsert: update if already exists
        result = await session.execute(
            select(CIIntegration).where(CIIntegration.repo_full_name == body.repo_full_name)
        )
        integration = result.scalar_one_or_none()

        if integration:
            # Update existing
            if body.github_token is not None:
                integration.github_token = body.github_token
            if body.ci_api_key is not None:
                integration.ci_api_key_enc = body.ci_api_key
            integration.ci_provider = body.ci_provider
            integration.max_attempts = body.max_attempts
            integration.auto_commit = body.auto_commit
            integration.allowed_authors = body.allowed_authors
            integration.enabled = True
            integration.updated_at = datetime.now(UTC)
            log.info("ci_integration.updated", repo=body.repo_full_name)
        else:
            integration = CIIntegration(
                repo_full_name=body.repo_full_name,
                ci_provider=body.ci_provider,
                github_token=body.github_token,
                ci_api_key_enc=body.ci_api_key,
                max_attempts=body.max_attempts,
                auto_commit=body.auto_commit,
                allowed_authors=body.allowed_authors,
                enabled=True,
            )
            session.add(integration)
            log.info("ci_integration.created", repo=body.repo_full_name)

        await session.commit()
        await session.refresh(integration)
        return CIIntegrationResponse.from_orm(integration)


@router.get("", response_model=list[CIIntegrationResponse])
async def list_integrations():
    """List all registered CI integrations."""
    async with get_db() as session:
        result = await session.execute(
            select(CIIntegration).order_by(CIIntegration.created_at.desc())
        )
        return [CIIntegrationResponse.from_orm(i) for i in result.scalars()]


@router.get("/{integration_id}", response_model=CIIntegrationResponse)
async def get_integration(integration_id: str):
    async with get_db() as session:
        integration = await session.get(CIIntegration, integration_id)
        if not integration:
            raise HTTPException(status_code=404, detail="Integration not found")
        return CIIntegrationResponse.from_orm(integration)


@router.patch("/{integration_id}", response_model=CIIntegrationResponse)
async def update_integration(integration_id: str, body: CIIntegrationUpdate):
    """Update token, disable/enable, or change max_attempts."""
    async with get_db() as session:
        integration = await session.get(CIIntegration, integration_id)
        if not integration:
            raise HTTPException(status_code=404, detail="Integration not found")

        if body.github_token is not None:
            integration.github_token = body.github_token
        if body.ci_api_key is not None:
            integration.ci_api_key_enc = body.ci_api_key
        if body.max_attempts is not None:
            integration.max_attempts = body.max_attempts
        if body.auto_commit is not None:
            integration.auto_commit = body.auto_commit
        if body.allowed_authors is not None:
            integration.allowed_authors = body.allowed_authors
        if body.enabled is not None:
            integration.enabled = body.enabled
        integration.updated_at = datetime.now(UTC)

        await session.commit()
        await session.refresh(integration)
        log.info("ci_integration.patched", id=integration_id, repo=integration.repo_full_name)
        return CIIntegrationResponse.from_orm(integration)


@router.delete("/{integration_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_integration(integration_id: str):
    """Remove a CI integration. Does not delete historical CIFixRun records."""
    async with get_db() as session:
        integration = await session.get(CIIntegration, integration_id)
        if not integration:
            raise HTTPException(status_code=404, detail="Integration not found")
        await session.delete(integration)
        await session.commit()
        log.info("ci_integration.deleted", id=integration_id, repo=integration.repo_full_name)
