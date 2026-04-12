"""CRUD endpoints for service management."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.service import Service
from app.schemas.service import (
    ServiceCreate,
    ServiceListResponse,
    ServiceResponse,
    ServiceUpdate,
)

router = APIRouter(prefix="/api/services", tags=["services"])

DB = Annotated[AsyncSession, Depends(get_db)]


@router.get("", response_model=ServiceListResponse)
async def list_services(
    db: DB,
    category: str | None = Query(None),
    active: bool | None = Query(None),
) -> ServiceListResponse:
    """List services, optionally filtered by category and/or active status."""
    stmt = select(Service)
    count_stmt = select(func.count(Service.id))

    if category is not None:
        stmt = stmt.where(Service.category == category)
        count_stmt = count_stmt.where(Service.category == category)
    if active is not None:
        stmt = stmt.where(Service.active == active)
        count_stmt = count_stmt.where(Service.active == active)

    stmt = stmt.order_by(Service.category, Service.name)
    result = await db.execute(stmt)
    services = result.scalars().all()
    count_result = await db.execute(count_stmt)
    total = count_result.scalar() or 0

    return ServiceListResponse(
        items=[ServiceResponse.model_validate(s) for s in services],
        total=total,
    )


@router.post("", response_model=ServiceResponse, status_code=status.HTTP_201_CREATED)
async def create_service(db: DB, payload: ServiceCreate) -> ServiceResponse:
    """Create a new service."""
    service = Service(
        name=payload.name,
        description=payload.description,
        duration_minutes=payload.duration_minutes,
        price=payload.price,
        category=payload.category,
    )
    db.add(service)
    await db.flush()
    await db.refresh(service)
    return ServiceResponse.model_validate(service)


@router.get("/{service_id}", response_model=ServiceResponse)
async def get_service(db: DB, service_id: int) -> ServiceResponse:
    """Get a single service by ID."""
    service = await db.get(Service, service_id)
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    return ServiceResponse.model_validate(service)


@router.put("/{service_id}", response_model=ServiceResponse)
async def update_service(
    db: DB, service_id: int, payload: ServiceUpdate
) -> ServiceResponse:
    """Update an existing service."""
    service = await db.get(Service, service_id)
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(service, field, value)

    await db.flush()
    await db.refresh(service)
    return ServiceResponse.model_validate(service)


@router.delete("/{service_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_service(db: DB, service_id: int) -> None:
    """Soft-delete a service by setting active=false."""
    service = await db.get(Service, service_id)
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    service.active = False
    await db.flush()
