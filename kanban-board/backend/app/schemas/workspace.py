"""Pydantic schemas for workspace CRUD and member management."""

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _slugify(text: str) -> str:
    """Convert a string to a URL-safe slug.

    Lowercases the text, replaces non-alphanumeric characters with hyphens,
    collapses consecutive hyphens, and strips leading/trailing hyphens.

    Args:
        text: The string to slugify.

    Returns:
        A lowercase, hyphen-separated slug.
    """
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    slug = slug.strip("-")
    return slug


class WorkspaceCreate(BaseModel):
    """Schema for creating a new workspace.

    Only ``name`` is required.  If ``slug`` is omitted it will be
    auto-generated from the name (lowercased, non-alphanumeric characters
    replaced with hyphens).
    """

    name: str = Field(min_length=1, max_length=200)
    slug: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9-]+$",
        description="URL-safe slug (lowercase alphanumeric and hyphens). Auto-generated from name if omitted.",
    )

    @model_validator(mode="after")
    def _generate_slug(self) -> "WorkspaceCreate":
        """Auto-generate a slug from the workspace name when not provided."""
        if self.slug is None:
            self.slug = _slugify(self.name)
        return self


class WorkspaceUpdate(BaseModel):
    """Schema for updating an existing workspace."""

    name: str | None = Field(default=None, min_length=1, max_length=200)


class WorkspaceResponse(BaseModel):
    """Workspace data returned by the API."""

    id: int
    name: str
    slug: str
    owner_id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WorkspaceListResponse(BaseModel):
    """Paginated list response for workspaces.

    Wraps a list of workspaces with a total count for client convenience.
    """

    workspaces: list[WorkspaceResponse]
    count: int = Field(description="Total number of workspaces in the list")


class MemberAdd(BaseModel):
    """Schema for adding a member to a workspace."""

    email: str = Field(description="Email of the user to add")
    role: Literal["admin", "member", "viewer"] = "member"


class MemberResponse(BaseModel):
    """Workspace member data returned by the API."""

    user_id: int
    email: str
    display_name: str
    role: str
    joined_at: datetime

    model_config = ConfigDict(from_attributes=True)
