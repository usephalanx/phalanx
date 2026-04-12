"""Tests for permission dependencies — auth and RBAC."""

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.services.permissions import require_workspace_admin, require_workspace_member


@pytest.mark.asyncio
async def test_require_workspace_member_success(db_session: AsyncSession) -> None:
    """Returns WorkspaceMember when the user is a member."""
    user = User(email="member@test.com", hashed_password="hash", display_name="Member")
    db_session.add(user)
    await db_session.flush()

    ws = Workspace(name="WS", owner_id=user.id)
    db_session.add(ws)
    await db_session.flush()

    wm = WorkspaceMember(user_id=user.id, workspace_id=ws.id, role="member")
    db_session.add(wm)
    await db_session.flush()

    result = await require_workspace_member(ws.id, user, db_session)
    assert result.user_id == user.id
    assert result.role == "member"


@pytest.mark.asyncio
async def test_require_workspace_member_not_found(db_session: AsyncSession) -> None:
    """Raises 403 when the user is not a workspace member."""
    user = User(email="outsider@test.com", hashed_password="hash", display_name="Out")
    db_session.add(user)
    await db_session.flush()

    owner = User(email="owner@test.com", hashed_password="hash", display_name="Own")
    db_session.add(owner)
    await db_session.flush()

    ws = Workspace(name="WS", owner_id=owner.id)
    db_session.add(ws)
    await db_session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await require_workspace_member(ws.id, user, db_session)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_workspace_admin_success(db_session: AsyncSession) -> None:
    """Returns WorkspaceMember when the user has admin role."""
    user = User(email="admin@test.com", hashed_password="hash", display_name="Admin")
    db_session.add(user)
    await db_session.flush()

    ws = Workspace(name="WS", owner_id=user.id)
    db_session.add(ws)
    await db_session.flush()

    wm = WorkspaceMember(user_id=user.id, workspace_id=ws.id, role="admin")
    db_session.add(wm)
    await db_session.flush()

    result = await require_workspace_admin(ws.id, user, db_session)
    assert result.role == "admin"


@pytest.mark.asyncio
async def test_require_workspace_admin_owner_allowed(db_session: AsyncSession) -> None:
    """Owners also pass the admin check."""
    user = User(email="own@test.com", hashed_password="hash", display_name="Own")
    db_session.add(user)
    await db_session.flush()

    ws = Workspace(name="WS", owner_id=user.id)
    db_session.add(ws)
    await db_session.flush()

    wm = WorkspaceMember(user_id=user.id, workspace_id=ws.id, role="owner")
    db_session.add(wm)
    await db_session.flush()

    result = await require_workspace_admin(ws.id, user, db_session)
    assert result.role == "owner"


@pytest.mark.asyncio
async def test_require_workspace_admin_member_denied(db_session: AsyncSession) -> None:
    """Regular members are denied admin access."""
    user = User(email="reg@test.com", hashed_password="hash", display_name="Reg")
    db_session.add(user)
    await db_session.flush()

    ws = Workspace(name="WS", owner_id=user.id)
    db_session.add(ws)
    await db_session.flush()

    wm = WorkspaceMember(user_id=user.id, workspace_id=ws.id, role="member")
    db_session.add(wm)
    await db_session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await require_workspace_admin(ws.id, user, db_session)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_workspace_admin_viewer_denied(db_session: AsyncSession) -> None:
    """Viewers are denied admin access."""
    user = User(email="viewer@test.com", hashed_password="hash", display_name="View")
    db_session.add(user)
    await db_session.flush()

    ws = Workspace(name="WS", owner_id=user.id)
    db_session.add(ws)
    await db_session.flush()

    wm = WorkspaceMember(user_id=user.id, workspace_id=ws.id, role="viewer")
    db_session.add(wm)
    await db_session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await require_workspace_admin(ws.id, user, db_session)
    assert exc_info.value.status_code == 403
