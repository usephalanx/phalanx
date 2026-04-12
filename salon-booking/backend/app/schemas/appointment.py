"""Pydantic schemas for appointment endpoints."""

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.schemas.service import ServiceResponse
from app.schemas.staff import StaffResponse


class AppointmentCreate(BaseModel):
    """Schema for booking a new appointment."""

    customer_name: str = Field(..., min_length=1, max_length=100)
    customer_email: EmailStr
    customer_phone: str | None = Field(None, max_length=20)
    staff_id: int
    service_id: int
    start_time: datetime
    notes: str | None = None


class AppointmentReschedule(BaseModel):
    """Schema for rescheduling an appointment."""

    start_time: datetime


class AppointmentCancel(BaseModel):
    """Schema for cancelling an appointment (body is optional but allows notes)."""

    notes: str | None = None


class AppointmentResponse(BaseModel):
    """Schema for appointment response."""

    id: int
    customer_name: str
    customer_email: str
    customer_phone: str | None
    staff_id: int
    service_id: int
    start_time: datetime
    end_time: datetime
    status: str
    notes: str | None
    created_at: datetime
    staff: StaffResponse | None = None
    service: ServiceResponse | None = None

    model_config = {"from_attributes": True}


class AppointmentListResponse(BaseModel):
    """Schema for listing appointments."""

    items: list[AppointmentResponse]
    total: int
