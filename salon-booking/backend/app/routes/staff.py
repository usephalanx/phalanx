"""CRUD endpoints for staff management."""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.staff import Staff
from app.schemas.staff import (
    StaffCreate,
    StaffDetailResponse,
    StaffListResponse,
    StaffResponse,
    StaffUpdate,
)

router = APIRouter(prefix="/api/staff", tags=["staff"])

DB = Annotated[AsyncSession, Depends(get_db)]


def _staff_to_response(staff: Staff) -> StaffResponse:
    """Convert a Staff model to a StaffResponse, parsing specialties JSON."""
    specialties = None
    if staff.specialties:
        try:
            specialties = json.loads(staff.specialties)
        except (json.JSONDecodeError, TypeError):
            specialties = None
    return StaffResponse(
        id=staff.id,
        name=staff.name,
        email=staff.email,
        phone=staff.phone,
        role=staff.role,
        specialties=specialties,
        active=staff.active,
        created_at=staff.created_at,
    )


@router.get("", response_model=StaffListResponse)
async def list_staff(
    db: DB,
    active: bool | None = Query(None),
) -> StaffListResponse:
    """List staff members, optionally filtered by active status."""
    stmt = select(Staff)
    count_stmt = select(func.count(Staff.id))
    if active is not None:
        stmt = stmt.where(Staff.active == active)
        count_stmt = count_stmt.where(Staff.active == active)
    stmt = stmt.order_by(Staff.name)

    result = await db.execute(stmt)
    staff_list = result.scalars().all()
    count_result = await db.execute(count_stmt)
    total = count_result.scalar() or 0

    return StaffListResponse(
        items=[_staff_to_response(s) for s in staff_list],
        total=total,
    )


@router.post("", response_model=StaffResponse, status_code=status.HTTP_201_CREATED)
async def create_staff(db: DB, payload: StaffCreate) -> StaffResponse:
    """Create a new staff member."""
    specialties_json = (
        json.dumps(payload.specialties) if payload.specialties else None
    )
    staff = Staff(
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        role=payload.role,
        specialties=specialties_json,
    )
    db.add(staff)
    await db.flush()
    await db.refresh(staff)
    return _staff_to_response(staff)


@router.get("/{staff_id}", response_model=StaffDetailResponse)
async def get_staff(db: DB, staff_id: int) -> StaffDetailResponse:
    """Get a single staff member with their schedule."""
    staff = await db.get(Staff, staff_id)
    if not staff:
        raise HTTPException(status_code=404, detail="Staff member not found")

    resp = _staff_to_response(staff)
    return StaffDetailResponse(
        **resp.model_dump(),
        schedules=[
            {
                "id": s.id,
                "day_of_week": s.day_of_week,
                "start_time": s.start_time,
                "end_time": s.end_time,
                "active": s.active,
            }
            for s in staff.schedules
        ],
    )


@router.put("/{staff_id}", response_model=StaffResponse)
async def update_staff(
    db: DB, staff_id: int, payload: StaffUpdate
) -> StaffResponse:
    """Update an existing staff member."""
    staff = await db.get(Staff, staff_id)
    if not staff:
        raise HTTPException(status_code=404, detail="Staff member not found")

    update_data = payload.model_dump(exclude_unset=True)
    if "specialties" in update_data:
        val = update_data.pop("specialties")
        staff.specialties = json.dumps(val) if val is not None else None
    for field, value in update_data.items():
        setattr(staff, field, value)

    await db.flush()
    await db.refresh(staff)
    return _staff_to_response(staff)


@router.delete("/{staff_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_staff(db: DB, staff_id: int) -> None:
    """Soft-delete a staff member by setting active=false."""
    staff = await db.get(Staff, staff_id)
    if not staff:
        raise HTTPException(status_code=404, detail="Staff member not found")
    staff.active = False
    await db.flush()
