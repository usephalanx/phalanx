"""
Work Orders API — CRUD for WorkOrder entities.

These routes are used by internal tools and the admin UI (future).
The primary entry point for creating work orders is the Slack gateway — not this API.
(AP-001: Slack is the single human entry point.)

Routes:
  GET  /work-orders               — list open work orders for a project
  GET  /work-orders/{id}          — get a single work order
  POST /work-orders               — create (internal tools / testing only)
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.db.models import WorkOrder
from forge.db.session import get_db

router = APIRouter(prefix="/work-orders", tags=["work-orders"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class WorkOrderOut(BaseModel):
    id: str
    project_id: str
    title: str
    description: str
    raw_command: str
    status: str
    priority: int
    requested_by: str
    created_at: str

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm(cls, wo: WorkOrder) -> "WorkOrderOut":
        return cls(
            id=wo.id,
            project_id=wo.project_id,
            title=wo.title,
            description=wo.description,
            raw_command=wo.raw_command,
            status=wo.status,
            priority=wo.priority,
            requested_by=wo.requested_by,
            created_at=wo.created_at.isoformat(),
        )


class CreateWorkOrderRequest(BaseModel):
    project_id: str
    title: str = Field(..., max_length=500)
    description: str
    raw_command: str
    requested_by: str
    priority: int = Field(default=50, ge=0, le=100)


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[WorkOrderOut])
async def list_work_orders(
    project_id: str = Query(..., description="Filter by project ID"),
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(default=50, le=200),
):
    """List work orders for a project, newest first."""
    async with get_db() as session:
        stmt = (
            select(WorkOrder)
            .where(WorkOrder.project_id == project_id)
            .order_by(WorkOrder.created_at.desc())
            .limit(limit)
        )
        if status_filter:
            stmt = stmt.where(WorkOrder.status == status_filter)

        result = await session.execute(stmt)
        wos = list(result.scalars())
        return [WorkOrderOut.from_orm(wo) for wo in wos]


@router.get("/{work_order_id}", response_model=WorkOrderOut)
async def get_work_order(work_order_id: str):
    """Get a single work order by ID."""
    async with get_db() as session:
        result = await session.execute(
            select(WorkOrder).where(WorkOrder.id == work_order_id)
        )
        wo = result.scalar_one_or_none()
        if wo is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"WorkOrder {work_order_id!r} not found",
            )
        return WorkOrderOut.from_orm(wo)


@router.post("", response_model=WorkOrderOut, status_code=status.HTTP_201_CREATED)
async def create_work_order(body: CreateWorkOrderRequest):
    """
    Create a work order directly (internal tools / integration tests).
    In production, work orders are created by the Slack gateway.
    """
    async with get_db() as session:
        wo = WorkOrder(
            project_id=body.project_id,
            title=body.title,
            description=body.description,
            raw_command=body.raw_command,
            requested_by=body.requested_by,
            priority=body.priority,
            status="OPEN",
        )
        session.add(wo)
        await session.commit()
        await session.refresh(wo)
        return WorkOrderOut.from_orm(wo)
