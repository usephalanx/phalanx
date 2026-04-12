"""Tests for Pydantic schemas — validation and serialization."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.auth import (
    LoginRequest,
    RefreshResponse,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.schemas.board import BoardCreate, BoardResponse, BoardUpdate
from app.schemas.card import CardCreate, CardMove, CardResponse, CardUpdate
from app.schemas.column import ColumnCreate, ColumnReorder, ColumnResponse, ColumnUpdate
from app.schemas.workspace import (
    MemberAdd,
    MemberResponse,
    WorkspaceCreate,
    WorkspaceResponse,
    WorkspaceUpdate,
)


# --- Auth schemas ---


def test_register_request_valid() -> None:
    """Valid registration data passes validation."""
    req = RegisterRequest(
        email="test@example.com", password="secret123", display_name="Test User"
    )
    assert req.email == "test@example.com"
    assert req.display_name == "Test User"


def test_register_request_short_password() -> None:
    """Password shorter than 6 chars fails validation."""
    with pytest.raises(ValidationError):
        RegisterRequest(email="a@b.com", password="short", display_name="Name")


def test_register_request_invalid_email() -> None:
    """Invalid email fails validation."""
    with pytest.raises(ValidationError):
        RegisterRequest(email="not-an-email", password="secret123", display_name="Name")


def test_register_request_empty_display_name() -> None:
    """Empty display_name fails validation."""
    with pytest.raises(ValidationError):
        RegisterRequest(email="a@b.com", password="secret123", display_name="")


def test_login_request_valid() -> None:
    """Valid login data passes validation."""
    req = LoginRequest(email="test@example.com", password="mypassword")
    assert req.email == "test@example.com"


def test_user_response_from_attributes() -> None:
    """UserResponse can be created from a dict simulating ORM attributes."""
    now = datetime.now(timezone.utc)
    resp = UserResponse(
        id=1, email="u@test.com", display_name="User", avatar_url=None, created_at=now
    )
    assert resp.id == 1
    assert resp.avatar_url is None


def test_token_response() -> None:
    """TokenResponse includes user and token fields."""
    now = datetime.now(timezone.utc)
    user = UserResponse(
        id=1, email="u@test.com", display_name="User", created_at=now
    )
    resp = TokenResponse(
        access_token="jwt-token-here", refresh_token="refresh-jwt", user=user
    )
    assert resp.token_type == "bearer"
    assert resp.access_token == "jwt-token-here"
    assert resp.refresh_token == "refresh-jwt"


def test_refresh_response() -> None:
    """RefreshResponse has the expected structure."""
    resp = RefreshResponse(access_token="new-jwt")
    assert resp.token_type == "bearer"


# --- Workspace schemas ---


def test_workspace_create_valid() -> None:
    """Valid workspace creation data passes validation."""
    ws = WorkspaceCreate(name="My Workspace", slug="my-workspace")
    assert ws.slug == "my-workspace"


def test_workspace_create_invalid_slug() -> None:
    """Slug with uppercase or spaces fails validation."""
    with pytest.raises(ValidationError):
        WorkspaceCreate(name="WS", slug="Invalid Slug!")


def test_workspace_update_optional() -> None:
    """WorkspaceUpdate allows all fields to be None."""
    update = WorkspaceUpdate()
    assert update.name is None


def test_workspace_response() -> None:
    """WorkspaceResponse serializes correctly."""
    now = datetime.now(timezone.utc)
    resp = WorkspaceResponse(id=1, name="Team", owner_id=5, created_at=now)
    assert resp.owner_id == 5


def test_member_add_default_role() -> None:
    """MemberAdd defaults to 'member' role."""
    m = MemberAdd(email="new@test.com")
    assert m.role == "member"


def test_member_add_explicit_role() -> None:
    """MemberAdd accepts explicit role."""
    m = MemberAdd(email="new@test.com", role="admin")
    assert m.role == "admin"


def test_member_response() -> None:
    """MemberResponse serializes correctly."""
    resp = MemberResponse(
        user_id=1, email="m@test.com", display_name="Member", role="member"
    )
    assert resp.display_name == "Member"


# --- Board schemas ---


def test_board_create_valid() -> None:
    """Valid board creation data passes validation."""
    b = BoardCreate(name="Sprint Board", description="Q1 Sprint")
    assert b.name == "Sprint Board"


def test_board_create_no_description() -> None:
    """Board can be created without description."""
    b = BoardCreate(name="Board")
    assert b.description is None


def test_board_update_optional() -> None:
    """BoardUpdate allows all fields to be None."""
    update = BoardUpdate()
    assert update.name is None
    assert update.description is None


def test_board_response() -> None:
    """BoardResponse serializes correctly."""
    now = datetime.now(timezone.utc)
    resp = BoardResponse(
        id=1, workspace_id=2, name="Board", description=None, created_at=now
    )
    assert resp.workspace_id == 2


# --- Column schemas ---


def test_column_create_valid() -> None:
    """Valid column creation data passes validation."""
    c = ColumnCreate(name="To Do", color="#FF0000", wip_limit=5)
    assert c.wip_limit == 5


def test_column_create_minimal() -> None:
    """Column can be created with just a name."""
    c = ColumnCreate(name="Backlog")
    assert c.color is None
    assert c.wip_limit is None


def test_column_update_optional() -> None:
    """ColumnUpdate allows all fields to be None."""
    update = ColumnUpdate()
    assert update.name is None


def test_column_reorder() -> None:
    """ColumnReorder accepts a float position."""
    r = ColumnReorder(position=1536.0)
    assert r.position == 1536.0


def test_column_response() -> None:
    """ColumnResponse serializes correctly."""
    resp = ColumnResponse(
        id=1, board_id=2, name="Done", color="#00FF00", wip_limit=None, position=1024.0
    )
    assert resp.board_id == 2


# --- Card schemas ---


def test_card_create_valid() -> None:
    """Valid card creation data passes validation."""
    c = CardCreate(title="Fix bug", description="Fix the login bug", assignee_id=3)
    assert c.assignee_id == 3


def test_card_create_minimal() -> None:
    """Card can be created with just a title."""
    c = CardCreate(title="Task")
    assert c.description is None
    assert c.assignee_id is None


def test_card_create_empty_title() -> None:
    """Empty card title fails validation."""
    with pytest.raises(ValidationError):
        CardCreate(title="")


def test_card_update_optional() -> None:
    """CardUpdate allows all fields to be None."""
    update = CardUpdate()
    assert update.title is None


def test_card_move() -> None:
    """CardMove has column_id and position."""
    m = CardMove(column_id=5, position=2048.0)
    assert m.column_id == 5


def test_card_response() -> None:
    """CardResponse serializes correctly."""
    now = datetime.now(timezone.utc)
    resp = CardResponse(
        id=1,
        column_id=2,
        title="Card",
        position=1024.0,
        created_at=now,
        updated_at=now,
    )
    assert resp.assignee_id is None
