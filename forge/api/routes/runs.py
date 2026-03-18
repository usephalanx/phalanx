"""
Runs API — read-only views into Run state, plus cancel action.

The Run lifecycle is managed by the Commander + WorkflowOrchestrator.
The API provides visibility and the cancel action for humans.

Routes:
  GET  /runs                        — list runs for a project
  GET  /runs/{id}                   — get a single run
  GET  /runs/{id}/tasks             — get tasks for a run
  POST /runs/{id}/cancel            — cancel an active run
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from forge.db.models import Run, Task
from forge.db.session import get_db
from forge.workflow.state_machine import TERMINAL_STATES, RunStatus, validate_transition

router = APIRouter(prefix="/runs", tags=["runs"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class TaskOut(BaseModel):
    id: str
    sequence_num: int
    title: str
    description: str
    agent_role: str
    assigned_agent_id: Optional[str]
    status: str
    estimated_complexity: int
    error: Optional[str]
    created_at: str
    started_at: Optional[str]
    completed_at: Optional[str]

    @classmethod
    def from_orm(cls, t: Task) -> "TaskOut":
        return cls(
            id=t.id,
            sequence_num=t.sequence_num,
            title=t.title,
            description=t.description,
            agent_role=t.agent_role,
            assigned_agent_id=t.assigned_agent_id,
            status=t.status,
            estimated_complexity=t.estimated_complexity,
            error=t.error,
            created_at=t.created_at.isoformat(),
            started_at=t.started_at.isoformat() if t.started_at else None,
            completed_at=t.completed_at.isoformat() if t.completed_at else None,
        )


class RunOut(BaseModel):
    id: str
    work_order_id: str
    project_id: str
    run_number: int
    status: str
    active_branch: Optional[str]
    pr_url: Optional[str]
    pr_number: Optional[int]
    error_message: Optional[str]
    token_count: int
    estimated_cost_usd: float
    created_at: str
    updated_at: str
    started_at: Optional[str]
    completed_at: Optional[str]

    @classmethod
    def from_orm(cls, r: Run) -> "RunOut":
        return cls(
            id=r.id,
            work_order_id=r.work_order_id,
            project_id=r.project_id,
            run_number=r.run_number,
            status=r.status,
            active_branch=r.active_branch,
            pr_url=r.pr_url,
            pr_number=r.pr_number,
            error_message=r.error_message,
            token_count=r.token_count,
            estimated_cost_usd=r.estimated_cost_usd,
            created_at=r.created_at.isoformat(),
            updated_at=r.updated_at.isoformat(),
            started_at=r.started_at.isoformat() if r.started_at else None,
            completed_at=r.completed_at.isoformat() if r.completed_at else None,
        )


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[RunOut])
async def list_runs(
    project_id: str = Query(..., description="Filter by project ID"),
    active_only: bool = Query(default=False),
    limit: int = Query(default=50, le=200),
):
    """List runs for a project, newest first."""
    async with get_db() as session:
        stmt = (
            select(Run)
            .where(Run.project_id == project_id)
            .order_by(Run.created_at.desc())
            .limit(limit)
        )
        if active_only:
            terminal = [s.value for s in TERMINAL_STATES]
            stmt = stmt.where(Run.status.notin_(terminal))

        result = await session.execute(stmt)
        runs = list(result.scalars())
        return [RunOut.from_orm(r) for r in runs]


@router.get("/{run_id}", response_model=RunOut)
async def get_run(run_id: str):
    """Get a single run by ID."""
    async with get_db() as session:
        result = await session.execute(select(Run).where(Run.id == run_id))
        run = result.scalar_one_or_none()
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Run {run_id!r} not found",
            )
        return RunOut.from_orm(run)


@router.get("/{run_id}/tasks", response_model=list[TaskOut])
async def get_run_tasks(run_id: str):
    """Get all tasks for a run, ordered by sequence_num."""
    async with get_db() as session:
        # Verify run exists
        run_check = await session.execute(select(Run.id).where(Run.id == run_id))
        if run_check.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Run {run_id!r} not found",
            )

        result = await session.execute(
            select(Task)
            .where(Task.run_id == run_id)
            .order_by(Task.sequence_num)
        )
        tasks = list(result.scalars())
        return [TaskOut.from_orm(t) for t in tasks]


@router.post("/{run_id}/cancel", response_model=RunOut)
async def cancel_run(run_id: str):
    """
    Cancel an active run.

    Only valid for non-terminal runs. Validates the transition via state machine.
    The Commander Celery task will detect the CANCELLED status and stop.
    """
    async with get_db() as session:
        result = await session.execute(select(Run).where(Run.id == run_id))
        run = result.scalar_one_or_none()

        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Run {run_id!r} not found",
            )

        try:
            validate_transition(RunStatus(run.status), RunStatus.CANCELLED)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot cancel run in status {run.status!r}: {exc}",
            )

        await session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(
                status="CANCELLED",
                updated_at=datetime.now(UTC),
                error_message="Cancelled via API",
            )
        )
        await session.commit()
        await session.refresh(run)
        return RunOut.from_orm(run)
