"""Pydantic schemas for request/response validation."""

from app.schemas.staff import (
    StaffCreate,
    StaffDetailResponse,
    StaffListResponse,
    StaffResponse,
    StaffUpdate,
)
from app.schemas.service import (
    ServiceCreate,
    ServiceListResponse,
    ServiceResponse,
    ServiceUpdate,
)
from app.schemas.appointment import (
    AppointmentCancel,
    AppointmentCreate,
    AppointmentListResponse,
    AppointmentReschedule,
    AppointmentResponse,
)
from app.schemas.dashboard import DashboardResponse, HourBlock

__all__ = [
    "StaffCreate",
    "StaffUpdate",
    "StaffResponse",
    "StaffDetailResponse",
    "StaffListResponse",
    "ServiceCreate",
    "ServiceUpdate",
    "ServiceResponse",
    "ServiceListResponse",
    "AppointmentCreate",
    "AppointmentReschedule",
    "AppointmentCancel",
    "AppointmentResponse",
    "AppointmentListResponse",
    "DashboardResponse",
    "HourBlock",
]
