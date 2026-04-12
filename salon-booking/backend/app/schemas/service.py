"""Pydantic schemas for service endpoints."""

from datetime import datetime

from pydantic import BaseModel, Field


class ServiceCreate(BaseModel):
    """Schema for creating a new service."""

    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = None
    duration_minutes: int = Field(..., gt=0)
    price: float = Field(..., ge=0)
    category: str = Field(..., min_length=1, max_length=50)


class ServiceUpdate(BaseModel):
    """Schema for updating an existing service."""

    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = None
    duration_minutes: int | None = Field(None, gt=0)
    price: float | None = Field(None, ge=0)
    category: str | None = Field(None, max_length=50)
    active: bool | None = None


class ServiceResponse(BaseModel):
    """Schema for service response."""

    id: int
    name: str
    description: str | None
    duration_minutes: int
    price: float
    category: str
    active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class ServiceListResponse(BaseModel):
    """Schema for listing services."""

    items: list[ServiceResponse]
    total: int
