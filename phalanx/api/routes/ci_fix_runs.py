"""
CI Fix Runs API — inspect the state of multi-agent CI fix pipeline runs.

Endpoints:
  GET /v1/ci-fix-runs/{run_id}/context  — full CIFixContext pipeline state
  GET /v1/ci-fix-runs/{run_id}          — CIFixRun record summary
  GET /v1/ci-fix-runs                   — list runs (filtered by repo/branch/status)
"""

from __future__ import annotations

import json

import structlog
from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from phalanx.ci_fixer.context import CIFixContext
from phalanx.db.models import CIFixRun
from phalanx.db.session import get_db

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/ci-fix-runs", tags=["ci-fix-runs"])


@router.get("/{run_id}/context")
async def get_fix_run_context(run_id: str) -> dict:
    """
    Return the full CIFixContext pipeline state for a CI fix run.

    This is the shared state object written by each agent as the pipeline
    progresses. Use this to inspect exactly what each agent produced,
    which stage the pipeline is at, and what the final outcome was.

    Returns 404 if the run does not exist.
    Returns the raw context dict if pipeline_context_json is not yet
    populated (run is too old or not yet started).
    """
    async with get_db() as session:
        result = await session.execute(select(CIFixRun).where(CIFixRun.id == run_id))
        ci_run = result.scalar_one_or_none()

    if ci_run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"CIFixRun {run_id} not found",
        )

    if not ci_run.pipeline_context_json:
        # Run exists but was created before Phase 1 — return basic info
        return {
            "ci_fix_run_id": str(ci_run.id),
            "repo": ci_run.repo_full_name,
            "branch": ci_run.branch,
            "commit_sha": ci_run.commit_sha,
            "original_build_id": ci_run.ci_build_id,
            "status": ci_run.status,
            "final_status": "unknown",
            "current_stage": "unknown",
            "_note": "This run predates the multi-agent pipeline context. No detailed state available.",
        }

    try:
        ctx_dict = json.loads(ci_run.pipeline_context_json)
        ctx = CIFixContext.from_dict(ctx_dict)
        return {
            **ctx.to_dict(),
            "current_stage": ctx.current_stage,
        }
    except Exception as exc:
        log.warning("ci_fix_runs.context_parse_error", run_id=run_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to parse pipeline context",
        ) from exc


@router.get("/{run_id}")
async def get_fix_run(run_id: str) -> dict:
    """Return a summary of a CI fix run record."""
    async with get_db() as session:
        result = await session.execute(select(CIFixRun).where(CIFixRun.id == run_id))
        ci_run = result.scalar_one_or_none()

    if ci_run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"CIFixRun {run_id} not found",
        )

    return {
        "id": str(ci_run.id),
        "repo": ci_run.repo_full_name,
        "branch": ci_run.branch,
        "commit_sha": ci_run.commit_sha,
        "ci_provider": ci_run.ci_provider,
        "ci_build_id": ci_run.ci_build_id,
        "status": ci_run.status,
        "fix_branch": ci_run.fix_branch,
        "fix_pr_number": ci_run.fix_pr_number,
        "fix_commit_sha": ci_run.fix_commit_sha,
        "fingerprint_hash": ci_run.fingerprint_hash,
        "error": ci_run.error,
        "created_at": ci_run.created_at.isoformat() if ci_run.created_at else None,
        "completed_at": ci_run.completed_at.isoformat() if ci_run.completed_at else None,
        "has_context": ci_run.pipeline_context_json is not None,
    }


@router.get("")
async def list_fix_runs(
    repo: str | None = Query(None, description="Filter by repo (owner/repo)"),
    branch: str | None = Query(None, description="Filter by branch"),
    run_status: str | None = Query(
        None, alias="status", description="Filter by status: PENDING, FIXED, FAILED"
    ),
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    """List CI fix runs with optional filters."""
    async with get_db() as session:
        q = select(CIFixRun).order_by(CIFixRun.created_at.desc()).limit(limit)
        if repo:
            q = q.where(CIFixRun.repo_full_name == repo)
        if branch:
            q = q.where(CIFixRun.branch == branch)
        if run_status:
            q = q.where(CIFixRun.status == run_status.upper())

        result = await session.execute(q)
        runs = result.scalars().all()

    return {
        "runs": [
            {
                "id": str(r.id),
                "repo": r.repo_full_name,
                "branch": r.branch,
                "status": r.status,
                "fix_pr_number": r.fix_pr_number,
                "error": r.error,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "has_context": r.pipeline_context_json is not None,
            }
            for r in runs
        ],
        "count": len(runs),
    }
