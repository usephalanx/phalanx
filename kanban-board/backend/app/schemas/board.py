"""Pydantic schemas for board CRUD."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class BoardCreate(BaseModel):
    """Schema for creating a new board."""

    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class BoardUpdate(BaseModel):
    """Schema for updating an existing board."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None


class BoardResponse(BaseModel):
    """Board data returned by the API."""

    id: int
    workspace_id: int
    name: str
    description: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BoardDetailResponse(BoardResponse):
    """Board data with nested columns and cards."""

    columns: list["ColumnWithCardsResponse"] = []


# Import here to allow forward reference resolution
from app.schemas.column import ColumnWithCardsResponse  # noqa: E402

BoardDetailResponse.model_rebuild()
