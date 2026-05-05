"""ShadowLedger CRUD — thin async wrappers used by the runner + CLI."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from phalanx.db.models import ShadowLedger


async def create_pending(
    session: AsyncSession,
    *,
    repo: str,
    workflow_run_id: int,
    pr_number: int | None,
    failing_commit_sha: str | None,
    phalanx_run_id: str | None = None,
) -> ShadowLedger:
    """Insert a pending ledger row before the Phalanx run starts.

    Idempotent on (repo, workflow_run_id) — returns the existing row if
    we've already shadowed this workflow run.
    """
    existing = await session.execute(
        select(ShadowLedger).where(
            ShadowLedger.repo == repo,
            ShadowLedger.workflow_run_id == workflow_run_id,
        )
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        return row
    row = ShadowLedger(
        repo=repo,
        workflow_run_id=workflow_run_id,
        pr_number=pr_number,
        failing_commit_sha=failing_commit_sha,
        phalanx_run_id=phalanx_run_id,
        phalanx_verdict="PENDING",
        ground_truth_status="pending",
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def update_with_results(
    session: AsyncSession,
    *,
    ledger_id: str,
    verdict: str,
    confidence: float | None,
    proposed_patch: str | None,
    root_cause: str | None,
    affected_files: list[str] | None,
    iterations: int | None,
    tool_calls: int | None,
    cost_usd: float | None,
    run_seconds: int | None,
    failure_class: str | None = None,
    notes: str | None = None,
) -> ShadowLedger:
    """Update a pending row with terminal-state results."""
    row = await get(session, ledger_id)
    if row is None:
        raise ValueError(f"shadow_ledger row {ledger_id} not found")
    row.phalanx_verdict = verdict
    row.phalanx_confidence = confidence
    row.phalanx_proposed_patch = proposed_patch
    row.phalanx_root_cause = root_cause
    row.phalanx_affected_files = affected_files
    row.phalanx_iterations = iterations
    row.phalanx_tool_calls = tool_calls
    row.phalanx_cost_usd = cost_usd
    row.phalanx_run_seconds = run_seconds
    if failure_class is not None:
        row.failure_class = failure_class
    if notes is not None:
        row.notes = notes
    await session.commit()
    await session.refresh(row)
    return row


async def get(session: AsyncSession, ledger_id: str) -> ShadowLedger | None:
    result = await session.execute(
        select(ShadowLedger).where(ShadowLedger.id == ledger_id)
    )
    return result.scalar_one_or_none()


async def list_all(
    session: AsyncSession, *, repo: str | None = None, limit: int = 500
) -> list[ShadowLedger]:
    stmt = select(ShadowLedger).order_by(ShadowLedger.created_at.desc()).limit(limit)
    if repo:
        stmt = stmt.where(ShadowLedger.repo == repo)
    result = await session.execute(stmt)
    return list(result.scalars().all())


def to_dict(row: ShadowLedger) -> dict[str, Any]:
    """JSON-serializable view of a ledger row."""

    def _iso(v: datetime | None) -> str | None:
        return v.isoformat() if v is not None else None

    return {
        "id": row.id,
        "repo": row.repo,
        "workflow_run_id": row.workflow_run_id,
        "pr_number": row.pr_number,
        "failing_commit_sha": row.failing_commit_sha,
        "failure_class": row.failure_class,
        "phalanx_run_id": row.phalanx_run_id,
        "phalanx_verdict": row.phalanx_verdict,
        "phalanx_confidence": row.phalanx_confidence,
        "phalanx_proposed_patch": row.phalanx_proposed_patch,
        "phalanx_root_cause": row.phalanx_root_cause,
        "phalanx_affected_files": row.phalanx_affected_files,
        "phalanx_iterations": row.phalanx_iterations,
        "phalanx_tool_calls": row.phalanx_tool_calls,
        "phalanx_cost_usd": row.phalanx_cost_usd,
        "phalanx_run_seconds": row.phalanx_run_seconds,
        "ground_truth_status": row.ground_truth_status,
        "maintainer_fix_commit_sha": row.maintainer_fix_commit_sha,
        "maintainer_actual_patch": row.maintainer_actual_patch,
        "notes": row.notes,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }
