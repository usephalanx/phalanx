"""FastAPI dependency injection for authentication and RBAC.

The canonical ``get_current_user`` implementation lives in
:mod:`app.auth.dependencies`.  This module re-exports it so that
existing import paths continue to work, and adds workspace-level
RBAC helpers.
"""

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, oauth2_scheme  # noqa: F401
from app.database import get_db
from app.models.user import User
from app.models.workspace_member import WorkspaceMember


async def require_workspace_member(
    workspace_id: int,
    user: User,
    db: AsyncSession,
) -> WorkspaceMember:
    """Verify the user is a member of the workspace, raising 403 if not.

    Args:
        workspace_id: The workspace to check membership for.
        user: The authenticated user.
        db: The async database session.

    Returns:
        The ``WorkspaceMember`` record for the user.

    Raises:
        HTTPException: 403 if the user is not a member of the workspace.
    """
    result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user.id,
        )
    )
    member = result.scalar_one_or_none()

    if member is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not a member of this workspace",
        )
    return member


async def require_workspace_admin(
    workspace_id: int,
    user: User,
    db: AsyncSession,
) -> WorkspaceMember:
    """Verify the user is an admin or owner of the workspace, raising 403 if not.

    Args:
        workspace_id: The workspace to check admin rights for.
        user: The authenticated user.
        db: The async database session.

    Returns:
        The ``WorkspaceMember`` record for the user.

    Raises:
        HTTPException: 403 if the user is not an admin or owner.
    """
    member = await require_workspace_member(workspace_id, user, db)

    if member.role not in ("owner", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin or owner role required",
        )
    return member


class WorkspaceMembershipChecker:
    """Reusable FastAPI dependency that verifies workspace membership.

    Use as a dependency in route handlers that receive a ``workspace_id``
    path parameter.  It reads the workspace ID from the path, validates
    the current user is a member, and returns the ``WorkspaceMember``
    record so downstream code can inspect the role.

    Example::

        @router.get("/workspaces/{workspace_id}/settings")
        async def get_settings(
            workspace_id: int,
            membership: WorkspaceMember = Depends(check_workspace_membership),
            db: AsyncSession = Depends(get_db),
        ):
            ...  # membership.role is available here
    """

    def __init__(self, *, min_role: str | None = None) -> None:
        """Initialise the checker with an optional minimum role.

        Args:
            min_role: If provided, the user must hold at least this role.
                Accepted values: ``"viewer"``, ``"member"``, ``"admin"``,
                ``"owner"``.  When ``None``, any membership suffices.
        """
        self._min_role = min_role
        self._role_hierarchy = {
            "viewer": 0,
            "member": 1,
            "admin": 2,
            "owner": 3,
        }

    async def __call__(
        self,
        workspace_id: int,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> WorkspaceMember:
        """Check that the current user is a workspace member.

        Args:
            workspace_id: Path parameter — the workspace to check.
            current_user: The authenticated user (injected).
            db: The async database session (injected).

        Returns:
            The ``WorkspaceMember`` record.

        Raises:
            HTTPException: 403 if the user is not a member or lacks the
                required role.
        """
        member = await require_workspace_member(workspace_id, current_user, db)

        if self._min_role is not None:
            required_level = self._role_hierarchy.get(self._min_role, 0)
            actual_level = self._role_hierarchy.get(member.role, 0)
            if actual_level < required_level:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"{self._min_role} role or higher required",
                )

        return member


# Pre-built dependency instances for common access levels.
check_workspace_membership = WorkspaceMembershipChecker()
check_workspace_admin = WorkspaceMembershipChecker(min_role="admin")
check_workspace_owner = WorkspaceMembershipChecker(min_role="owner")
