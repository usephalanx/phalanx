"""Pydantic schemas for column CRUD and reorder."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.card import CardResponse


class ColumnCreate(BaseModel):
    """Schema for creating a new column."""

    name: str = Field(min_length=1, max_length=200)
    color: str | None = Field(default=None, max_length=7, description="Hex color code")
    wip_limit: int | None = Field(default=None, ge=0, description="Work-in-progress limit")


class ColumnUpdate(BaseModel):
    """Schema for updating an existing column."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    color: str | None = None
    wip_limit: int | None = Field(default=None, ge=0)


class ColumnReorderRequest(BaseModel):
    """Schema for bulk reordering columns — list of column IDs in desired order."""

    column_ids: list[int] = Field(min_length=1)


class ColumnResponse(BaseModel):
    """Column data returned by the API."""

    id: int
    board_id: int
    name: str
    color: str | None = None
    wip_limit: int | None = None
    position: float
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ColumnWithCardsResponse(ColumnResponse):
    """Column data with nested cards."""

    cards: list[CardResponse] = []
