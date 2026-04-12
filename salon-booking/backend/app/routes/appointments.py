"""CRUD endpoints for appointment management."""

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.appointment import Appointment
from app.models.service import Service
from app.models.staff import Staff
from app.schemas.appointment import (
    AppointmentCreate,
    AppointmentListResponse,
    AppointmentReschedule,
    AppointmentResponse,
)
from app.services.conflict import check_conflict

router = APIRouter(prefix="/api/appointments", tags=["appointments"])

DB = Annotated[AsyncSession, Depends(get_db)]


@router.get("", response_model=AppointmentListResponse)
async def list_appointments(
    db: DB,
    staff_id: int | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    date: str | None = Query(None, description="Filter by date (YYYY-MM-DD)"),
) -> AppointmentListResponse:
    """List appointments with optional filters."""
    stmt = select(Appointment)
    count_stmt = select(func.count(Appointment.id))

    if staff_id is not None:
        stmt = stmt.where(Appointment.staff_id == staff_id)
        count_stmt = count_stmt.where(Appointment.staff_id == staff_id)
    if status_filter is not None:
        stmt = stmt.where(Appointment.status == status_filter)
        count_stmt = count_stmt.where(Appointment.status == status_filter)
    if date is not None:
        from datetime import datetime

        try:
            day_start = datetime.fromisoformat(f"{date}T00:00:00")
            day_end = day_start + timedelta(days=1)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
        stmt = stmt.where(
            Appointment.start_time >= day_start,
            Appointment.start_time < day_end,
        )
        count_stmt = count_stmt.where(
            Appointment.start_time >= day_start,
            Appointment.start_time < day_end,
        )

    stmt = stmt.order_by(Appointment.start_time)
    result = await db.execute(stmt)
    appointments = result.scalars().all()
    count_result = await db.execute(count_stmt)
    total = count_result.scalar() or 0

    return AppointmentListResponse(
        items=[AppointmentResponse.model_validate(a) for a in appointments],
        total=total,
    )


@router.post("", response_model=AppointmentResponse, status_code=status.HTTP_201_CREATED)
async def create_appointment(db: DB, payload: AppointmentCreate) -> AppointmentResponse:
    """Book a new appointment with conflict validation."""
    # Validate staff exists and is active
    staff = await db.get(Staff, payload.staff_id)
    if not staff or not staff.active:
        raise HTTPException(status_code=404, detail="Staff member not found or inactive")

    # Validate service exists and is active
    service = await db.get(Service, payload.service_id)
    if not service or not service.active:
        raise HTTPException(status_code=404, detail="Service not found or inactive")

    # Compute end time from service duration
    end_time = payload.start_time + timedelta(minutes=service.duration_minutes)

    # Check for conflicts
    conflict = await check_conflict(db, payload.staff_id, payload.start_time, end_time)
    if conflict:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Time slot conflict: {staff.name} already has an appointment "
                f"from {conflict.start_time.isoformat()} to {conflict.end_time.isoformat()}"
            ),
        )

    appointment = Appointment(
        customer_name=payload.customer_name,
        customer_email=payload.customer_email,
        customer_phone=payload.customer_phone,
        staff_id=payload.staff_id,
        service_id=payload.service_id,
        start_time=payload.start_time,
        end_time=end_time,
        notes=payload.notes,
    )
    db.add(appointment)
    await db.flush()
    await db.refresh(appointment)
    return AppointmentResponse.model_validate(appointment)


@router.get("/{appointment_id}", response_model=AppointmentResponse)
async def get_appointment(db: DB, appointment_id: int) -> AppointmentResponse:
    """Get a single appointment by ID."""
    appointment = await db.get(Appointment, appointment_id)
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    return AppointmentResponse.model_validate(appointment)


@router.put("/{appointment_id}", response_model=AppointmentResponse)
async def reschedule_appointment(
    db: DB,
    appointment_id: int,
    payload: AppointmentReschedule,
) -> AppointmentResponse:
    """Reschedule an existing appointment to a new time."""
    appointment = await db.get(Appointment, appointment_id)
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    if appointment.status == "CANCELLED":
        raise HTTPException(status_code=400, detail="Cannot reschedule a cancelled appointment")

    # Compute new end time based on service duration
    service = await db.get(Service, appointment.service_id)
    duration = service.duration_minutes if service else 60
    new_end = payload.start_time + timedelta(minutes=duration)

    # Check for conflicts (exclude self)
    conflict = await check_conflict(
        db, appointment.staff_id, payload.start_time, new_end,
        exclude_appointment_id=appointment_id,
    )
    if conflict:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Time slot conflict with existing appointment "
                f"from {conflict.start_time.isoformat()} to {conflict.end_time.isoformat()}"
            ),
        )

    appointment.start_time = payload.start_time
    appointment.end_time = new_end
    await db.flush()
    await db.refresh(appointment)
    return AppointmentResponse.model_validate(appointment)


@router.patch("/{appointment_id}/cancel", response_model=AppointmentResponse)
async def cancel_appointment(db: DB, appointment_id: int) -> AppointmentResponse:
    """Cancel an appointment."""
    appointment = await db.get(Appointment, appointment_id)
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    if appointment.status == "CANCELLED":
        raise HTTPException(status_code=400, detail="Appointment is already cancelled")

    appointment.status = "CANCELLED"
    await db.flush()
    await db.refresh(appointment)
    return AppointmentResponse.model_validate(appointment)
