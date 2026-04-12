"""Tests for workspace Pydantic schemas and slug generation."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.workspace import (
    WorkspaceCreate,
    WorkspaceListResponse,
    WorkspaceResponse,
    WorkspaceUpdate,
    _slugify,
)


class TestSlugify:
    """Tests for the _slugify helper function."""

    def test_simple_name(self) -> None:
        """Lowercase words are joined by hyphens."""
        assert _slugify("My Workspace") == "my-workspace"

    def test_special_characters_stripped(self) -> None:
        """Non-alphanumeric characters are replaced with hyphens."""
        assert _slugify("Hello!! World @#$") == "hello-world"

    def test_consecutive_hyphens_collapsed(self) -> None:
        """Multiple consecutive hyphens are collapsed to one."""
        assert _slugify("a---b") == "a-b"

    def test_leading_trailing_hyphens_stripped(self) -> None:
        """Leading and trailing hyphens are stripped."""
        assert _slugify("  --hello--  ") == "hello"

    def test_already_valid_slug(self) -> None:
        """A string that is already a valid slug passes through."""
        assert _slugify("valid-slug-123") == "valid-slug-123"

    def test_empty_after_strip(self) -> None:
        """A string of only special characters becomes empty."""
        assert _slugify("!!!") == ""

    def test_numbers_preserved(self) -> None:
        """Numeric characters are preserved in the slug."""
        assert _slugify("Project 2024") == "project-2024"


class TestWorkspaceCreate:
    """Tests for WorkspaceCreate schema."""

    def test_name_and_slug(self) -> None:
        """Both name and slug can be provided explicitly."""
        schema = WorkspaceCreate(name="Test", slug="test-slug")
        assert schema.name == "Test"
        assert schema.slug == "test-slug"

    def test_name_only_generates_slug(self) -> None:
        """Omitting slug auto-generates it from the name."""
        schema = WorkspaceCreate(name="My Cool Project")
        assert schema.slug == "my-cool-project"

    def test_name_min_length(self) -> None:
        """Empty name is rejected."""
        with pytest.raises(ValidationError):
            WorkspaceCreate(name="")

    def test_name_max_length(self) -> None:
        """Name exceeding 200 characters is rejected."""
        with pytest.raises(ValidationError):
            WorkspaceCreate(name="x" * 201)

    def test_slug_invalid_pattern(self) -> None:
        """Slug with uppercase or special characters is rejected."""
        with pytest.raises(ValidationError):
            WorkspaceCreate(name="Fine", slug="Has Spaces!")

    def test_slug_max_length(self) -> None:
        """Slug exceeding 100 characters is rejected."""
        with pytest.raises(ValidationError):
            WorkspaceCreate(name="OK", slug="a" * 101)

    def test_auto_slug_special_chars(self) -> None:
        """Auto-generated slug handles names with special characters."""
        schema = WorkspaceCreate(name="Hello!! World  @#$  Test")
        assert schema.slug == "hello-world-test"


class TestWorkspaceUpdate:
    """Tests for WorkspaceUpdate schema."""

    def test_name_update(self) -> None:
        """Name field can be set."""
        schema = WorkspaceUpdate(name="New Name")
        assert schema.name == "New Name"

    def test_name_optional(self) -> None:
        """Name defaults to None when not provided."""
        schema = WorkspaceUpdate()
        assert schema.name is None

    def test_name_empty_rejected(self) -> None:
        """Empty name string is rejected by min_length."""
        with pytest.raises(ValidationError):
            WorkspaceUpdate(name="")


class TestWorkspaceResponse:
    """Tests for WorkspaceResponse schema."""

    def test_from_dict(self) -> None:
        """Response can be constructed from a dictionary."""
        now = datetime.now(timezone.utc)
        resp = WorkspaceResponse(
            id=1, name="WS", slug="ws", owner_id=42, created_at=now
        )
        assert resp.id == 1
        assert resp.slug == "ws"
        assert resp.owner_id == 42

    def test_from_attributes(self) -> None:
        """Response can be constructed from an object with matching attributes."""

        class FakeWorkspace:
            """Mock workspace object for testing from_attributes."""

            id = 1
            name = "Test"
            slug = "test"
            owner_id = 5
            created_at = datetime.now(timezone.utc)

        resp = WorkspaceResponse.model_validate(FakeWorkspace())
        assert resp.id == 1
        assert resp.name == "Test"


class TestWorkspaceListResponse:
    """Tests for WorkspaceListResponse schema."""

    def test_empty_list(self) -> None:
        """An empty workspace list has count 0."""
        resp = WorkspaceListResponse(workspaces=[], count=0)
        assert resp.workspaces == []
        assert resp.count == 0

    def test_populated_list(self) -> None:
        """List response contains workspaces and matching count."""
        now = datetime.now(timezone.utc)
        ws = WorkspaceResponse(
            id=1, name="WS", slug="ws", owner_id=1, created_at=now
        )
        resp = WorkspaceListResponse(workspaces=[ws], count=1)
        assert len(resp.workspaces) == 1
        assert resp.count == 1
        assert resp.workspaces[0].name == "WS"

    def test_count_field_required(self) -> None:
        """Count is required in the list response."""
        with pytest.raises(ValidationError):
            WorkspaceListResponse(workspaces=[])  # type: ignore[call-arg]
