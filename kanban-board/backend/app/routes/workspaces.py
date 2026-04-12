"""Workspace router — CRUD endpoints and member management."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.schemas.workspace import (
    MemberAdd,
    MemberResponse,
    WorkspaceCreate,
    WorkspaceListResponse,
    WorkspaceResponse,
    WorkspaceUpdate,
)
from app.services.permissions import (
    get_current_user,
    require_workspace_admin,
    require_workspace_member,
)

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


@router.post("", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    body: WorkspaceCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceResponse:
    """Create a new workspace and add the creator as owner.

    Only ``name`` is required — if ``slug`` is omitted it will be
    auto-generated from the name.  The creator is automatically added
    as an OWNER member of the new workspace.

    Args:
        body: Workspace creation data including name and optional slug.
        current_user: The authenticated user creating the workspace.
        db: The async database session.

    Returns:
        The created workspace data.

    Raises:
        HTTPException: If the slug is already taken.
    """
    result = await db.execute(
        select(Workspace).where(Workspace.slug == body.slug)
    )
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workspace slug already taken",
        )

    workspace = Workspace(
        name=body.name,
        slug=body.slug,
        owner_id=current_user.id,
    )
    db.add(workspace)
    await db.flush()

    member = WorkspaceMember(
        user_id=current_user.id,
        workspace_id=workspace.id,
        role="owner",
    )
    db.add(member)
    await db.flush()

    return WorkspaceResponse.model_validate(workspace)


@router.get("", response_model=WorkspaceListResponse)
async def list_workspaces(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceListResponse:
    """List all workspaces the current user is a member of.

    Returns a wrapped response containing the workspace list and a count.

    Args:
        current_user: The authenticated user.
        db: The async database session.

    Returns:
        A ``WorkspaceListResponse`` with the user's workspaces and count.
    """
    result = await db.execute(
        select(Workspace)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == current_user.id)
        .order_by(Workspace.created_at.desc())
    )
    workspaces = result.scalars().all()
    items = [WorkspaceResponse.model_validate(ws) for ws in workspaces]
    return WorkspaceListResponse(workspaces=items, count=len(items))


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace(
    workspace_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceResponse:
    """Get a single workspace by ID.

    Only members of the workspace may view it.

    Args:
        workspace_id: The workspace database ID.
        current_user: The authenticated user.
        db: The async database session.

    Returns:
        The workspace data.

    Raises:
        HTTPException: If the workspace does not exist or user is not a member.
    """
    workspace = await _get_workspace_or_404(workspace_id, db)
    await require_workspace_member(workspace_id, current_user, db)
    return WorkspaceResponse.model_validate(workspace)


@router.put("/{workspace_id}", response_model=WorkspaceResponse)
async def update_workspace(
    workspace_id: int,
    body: WorkspaceUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceResponse:
    """Update a workspace. Only admins and owners can update.

    Args:
        workspace_id: The workspace database ID.
        body: Fields to update.
        current_user: The authenticated user.
        db: The async database session.

    Returns:
        The updated workspace data.

    Raises:
        HTTPException: If the workspace does not exist or user lacks permission.
    """
    workspace = await _get_workspace_or_404(workspace_id, db)
    await require_workspace_admin(workspace_id, current_user, db)

    if body.name is not None:
        workspace.name = body.name

    await db.flush()
    await db.refresh(workspace)
    return WorkspaceResponse.model_validate(workspace)


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    workspace_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a workspace. Only the owner can delete.

    Args:
        workspace_id: The workspace database ID.
        current_user: The authenticated user.
        db: The async database session.

    Raises:
        HTTPException: If the workspace does not exist or user is not the owner.
    """
    workspace = await _get_workspace_or_404(workspace_id, db)

    if workspace.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the workspace owner can delete the workspace",
        )

    await db.delete(workspace)
    await db.flush()


# ── Member management ────────────────────────────────────────────────────────


@router.post(
    "/{workspace_id}/members",
    response_model=MemberResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_member(
    workspace_id: int,
    body: MemberAdd,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MemberResponse:
    """Invite a user to a workspace by email. Only owner/admin can invite.

    Args:
        workspace_id: The workspace database ID.
        body: Member invitation data with email and role.
        current_user: The authenticated user (must be owner or admin).
        db: The async database session.

    Returns:
        The created member record.

    Raises:
        HTTPException: If the user is not found, already a member, or caller lacks permission.
    """
    await _get_workspace_or_404(workspace_id, db)
    await require_workspace_admin(workspace_id, current_user, db)

    result = await db.execute(select(User).where(User.email == body.email))
    target_user = result.scalar_one_or_none()

    if target_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found with that email",
        )

    result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == target_user.id,
        )
    )
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User is already a member of this workspace",
        )

    member = WorkspaceMember(
        user_id=target_user.id,
        workspace_id=workspace_id,
        role=body.role,
    )
    db.add(member)
    await db.flush()
    await db.refresh(member)

    return MemberResponse(
        user_id=target_user.id,
        email=target_user.email,
        display_name=target_user.display_name,
        role=member.role,
        joined_at=member.joined_at,
    )


@router.get("/{workspace_id}/members", response_model=list[MemberResponse])
async def list_members(
    workspace_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MemberResponse]:
    """List all members of a workspace. Any member can view.

    Args:
        workspace_id: The workspace database ID.
        current_user: The authenticated user (must be a member).
        db: The async database session.

    Returns:
        A list of workspace members.
    """
    await _get_workspace_or_404(workspace_id, db)
    await require_workspace_member(workspace_id, current_user, db)

    result = await db.execute(
        select(WorkspaceMember)
        .where(WorkspaceMember.workspace_id == workspace_id)
    )
    members = result.scalars().all()

    return [
        MemberResponse(
            user_id=m.user_id,
            email=m.user.email,
            display_name=m.user.display_name,
            role=m.role,
            joined_at=m.joined_at,
        )
        for m in members
    ]


@router.delete(
    "/{workspace_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_member(
    workspace_id: int,
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a member from a workspace. Only owner can remove members.

    The owner cannot be removed.

    Args:
        workspace_id: The workspace database ID.
        user_id: The ID of the user to remove.
        current_user: The authenticated user (must be the owner).
        db: The async database session.

    Raises:
        HTTPException: If workspace not found, caller is not owner, or target is the owner.
    """
    workspace = await _get_workspace_or_404(workspace_id, db)

    if workspace.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the workspace owner can remove members",
        )

    if user_id == workspace.owner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot remove the workspace owner",
        )

    result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()

    if member is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found in this workspace",
        )

    await db.delete(member)
    await db.flush()


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _get_workspace_or_404(
    workspace_id: int,
    db: AsyncSession,
) -> Workspace:
    """Fetch a workspace by ID or raise 404.

    Args:
        workspace_id: The workspace database ID.
        db: The async database session.

    Returns:
        The Workspace ORM instance.

    Raises:
        HTTPException: If the workspace does not exist.
    """
    result = await db.execute(
        select(Workspace).where(Workspace.id == workspace_id)
    )
    workspace = result.scalar_one_or_none()

    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found",
        )
    return workspace
