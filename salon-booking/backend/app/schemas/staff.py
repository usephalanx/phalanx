"""Pydantic schemas for staff endpoints."""

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class StaffCreate(BaseModel):
    """Schema for creating a new staff member."""

    name: str = Field(..., min_length=1, max_length=100)
    email: EmailStr
    phone: str | None = Field(None, max_length=20)
    role: str = Field("stylist", max_length=50)
    specialties: list[str] | None = None


class StaffUpdate(BaseModel):
    """Schema for updating an existing staff member."""

    name: str | None = Field(None, min_length=1, max_length=100)
    email: EmailStr | None = None
    phone: str | None = Field(None, max_length=20)
    role: str | None = Field(None, max_length=50)
    specialties: list[str] | None = None
    active: bool | None = None


class ScheduleResponse(BaseModel):
    """Schema for a staff schedule entry."""

    id: int
    day_of_week: int
    start_time: str
    end_time: str
    active: bool

    model_config = {"from_attributes": True}


class StaffResponse(BaseModel):
    """Schema for staff response."""

    id: int
    name: str
    email: str
    phone: str | None
    role: str
    specialties: list[str] | None
    active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class StaffDetailResponse(StaffResponse):
    """Schema for detailed staff response including schedules."""

    schedules: list[ScheduleResponse] = []


class StaffListResponse(BaseModel):
    """Schema for listing staff members."""

    items: list[StaffResponse]
    total: int
