"""Appointment conflict detection service."""

from datetime import datetime

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appointment import Appointment


async def check_conflict(
    db: AsyncSession,
    staff_id: int,
    start_time: datetime,
    end_time: datetime,
    exclude_appointment_id: int | None = None,
) -> Appointment | None:
    """Check if a staff member has an overlapping appointment.

    Returns the conflicting appointment if one exists, otherwise None.
    Two intervals [s1, e1) and [s2, e2) overlap when s1 < e2 AND s2 < e1.
    """
    conditions = [
        Appointment.staff_id == staff_id,
        Appointment.start_time < end_time,
        Appointment.end_time > start_time,
        Appointment.status.notin_(["CANCELLED"]),
    ]
    if exclude_appointment_id is not None:
        conditions.append(Appointment.id != exclude_appointment_id)

    stmt = select(Appointment).where(and_(*conditions)).limit(1)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()
