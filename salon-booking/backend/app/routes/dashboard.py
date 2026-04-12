"""Dashboard endpoint returning today's appointments grouped by hour."""

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.appointment import Appointment
from app.schemas.appointment import AppointmentResponse
from app.schemas.dashboard import DashboardResponse, HourBlock

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

DB = Annotated[AsyncSession, Depends(get_db)]


@router.get("", response_model=DashboardResponse)
async def get_dashboard(
    db: DB,
    date: str | None = Query(None, description="Date in YYYY-MM-DD format, defaults to today"),
) -> DashboardResponse:
    """Return appointments for a given day grouped by hour with staff/service details."""
    if date:
        try:
            target_date = datetime.fromisoformat(date)
        except ValueError:
            from fastapi import HTTPException

            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
    else:
        target_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    day_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    stmt = (
        select(Appointment)
        .where(
            Appointment.start_time >= day_start,
            Appointment.start_time < day_end,
            Appointment.status != "CANCELLED",
        )
        .order_by(Appointment.start_time)
    )
    result = await db.execute(stmt)
    appointments = result.scalars().all()

    # Group by hour
    hour_map: dict[str, list[AppointmentResponse]] = defaultdict(list)
    for appt in appointments:
        hour_key = appt.start_time.strftime("%H:00")
        hour_map[hour_key].append(AppointmentResponse.model_validate(appt))

    # Build hour blocks for working hours (08:00–19:00)
    hour_blocks = []
    for h in range(8, 20):
        hour_key = f"{h:02d}:00"
        hour_blocks.append(
            HourBlock(hour=hour_key, appointments=hour_map.get(hour_key, []))
        )

    return DashboardResponse(
        date=day_start.strftime("%Y-%m-%d"),
        total_appointments=len(appointments),
        hour_blocks=hour_blocks,
    )
