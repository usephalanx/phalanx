"""Pydantic schemas for card CRUD and move operations."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CardCreate(BaseModel):
    """Schema for creating a new card in a column."""

    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    assignee_id: int | None = None


class CardUpdate(BaseModel):
    """Schema for updating an existing card."""

    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    assignee_id: int | None = None


class CardMoveRequest(BaseModel):
    """Schema for moving a card to a different column and/or position.

    The ``position`` field is a zero-based index indicating where the card
    should be inserted in the target column's ordered list of cards.  All
    cards in both the source and destination columns have their positions
    recalculated atomically within a single transaction.
    """

    target_column_id: int = Field(description="Destination column ID")
    position: int = Field(
        ge=0,
        description="Zero-based index where the card should be placed in the target column",
    )


class CardResponse(BaseModel):
    """Card data returned by the API."""

    id: int
    column_id: int
    title: str
    description: str | None = None
    position: float
    assignee_id: int | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
