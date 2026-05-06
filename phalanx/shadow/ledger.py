"""ShadowLedger CRUD — thin async wrappers used by the runner + CLI.

v1.7.3 append-mode — every shadow run for the same (repo,
workflow_run_id) appends a new row with the next free
attempt_number. Prior evidence is never overwritten.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from phalanx.db.models import ShadowLedger


async def _next_attempt_number(
    session: AsyncSession,
    *,
    repo: str,
    workflow_run_id: int,
) -> int:
    """SELECT MAX(attempt_number)+1 for (repo, workflow_run_id), or 1 if
    no prior attempt exists. Race window is bounded by the unique
    constraint — concurrent invocations serialize cleanly: one
    succeeds, the other gets a unique-violation and would re-call this
    helper. Callers handle the IntegrityError if needed.
    """
    result = await session.execute(
        select(func.max(ShadowLedger.attempt_number)).where(
            ShadowLedger.repo == repo,
            ShadowLedger.workflow_run_id == workflow_run_id,
        )
    )
    current_max = result.scalar_one_or_none()
    return (current_max or 0) + 1


async def create_pending(
    session: AsyncSession,
    *,
    repo: str,
    workflow_run_id: int,
    pr_number: int | None,
    failing_commit_sha: str | None,
    phalanx_run_id: str | None = None,
) -> ShadowLedger:
    """Append a pending ledger row before the Phalanx run starts.

    v1.7.3 append-mode — always creates a new row. The attempt_number
    is the next free integer for the (repo, workflow_run_id) pair:
    first call → 1; every retry on the same workflow → 2, 3, …

    Prior pending or completed rows are NEVER mutated.
    """
    next_attempt = await _next_attempt_number(
        session, repo=repo, workflow_run_id=workflow_run_id,
    )
    row = ShadowLedger(
        repo=repo,
        workflow_run_id=workflow_run_id,
        attempt_number=next_attempt,
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


async def list_attempts_for_workflow(
    session: AsyncSession,
    *,
    repo: str,
    workflow_run_id: int,
) -> list[ShadowLedger]:
    """Return every attempt against (repo, workflow_run_id), in
    attempt-number order (oldest → newest)."""
    result = await session.execute(
        select(ShadowLedger)
        .where(
            ShadowLedger.repo == repo,
            ShadowLedger.workflow_run_id == workflow_run_id,
        )
        .order_by(ShadowLedger.attempt_number.asc())
    )
    return list(result.scalars().all())


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
    """Return ledger rows newest-first. Includes ALL attempts (one row
    per attempt). For per-workflow grouping see latest_per_workflow."""
    stmt = (
        select(ShadowLedger)
        .order_by(
            ShadowLedger.created_at.desc(),
            ShadowLedger.attempt_number.desc(),
        )
        .limit(limit)
    )
    if repo:
        stmt = stmt.where(ShadowLedger.repo == repo)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def latest_per_workflow(
    session: AsyncSession, *, repo: str | None = None
) -> list[ShadowLedger]:
    """Return one row per (repo, workflow_run_id) tuple — the LATEST
    attempt only. Used by metrics --by-workflow which dedups against
    the unique workflow_run_id rather than counting every retry."""
    # Subquery: max attempt_number per (repo, wfid).
    sub = (
        select(
            ShadowLedger.repo.label("r"),
            ShadowLedger.workflow_run_id.label("w"),
            func.max(ShadowLedger.attempt_number).label("max_att"),
        )
        .group_by(ShadowLedger.repo, ShadowLedger.workflow_run_id)
        .subquery()
    )
    stmt = (
        select(ShadowLedger)
        .join(
            sub,
            (ShadowLedger.repo == sub.c.r)
            & (ShadowLedger.workflow_run_id == sub.c.w)
            & (ShadowLedger.attempt_number == sub.c.max_att),
        )
        .order_by(ShadowLedger.created_at.desc())
    )
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
        "attempt_number": row.attempt_number,
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
