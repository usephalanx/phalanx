"""Pydantic schemas for the dashboard endpoint."""

from pydantic import BaseModel

from app.schemas.appointment import AppointmentResponse


class HourBlock(BaseModel):
    """A single hour block with its appointments."""

    hour: str  # e.g. "09:00"
    appointments: list[AppointmentResponse]


class DashboardResponse(BaseModel):
    """Dashboard response: today's appointments grouped by hour."""

    date: str  # ISO date string
    total_appointments: int
    hour_blocks: list[HourBlock]
