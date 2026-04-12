"""WorkspaceMember model for RBAC within workspaces."""

import enum
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class WorkspaceRole(str, enum.Enum):
    """Valid roles for workspace membership."""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class WorkspaceMember(Base):
    """Associates a user with a workspace and assigns a role."""

    __tablename__ = "workspace_member"

    __table_args__ = (
        CheckConstraint(
            "role IN ('owner', 'admin', 'member', 'viewer')",
            name="ck_workspace_member_role",
        ),
    )

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id"), primary_key=True
    )
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspace.id"), primary_key=True
    )
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, default="member"
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="memberships", lazy="selectin")
    workspace: Mapped["Workspace"] = relationship(
        back_populates="members", lazy="selectin"
    )


from app.models.user import User  # noqa: E402
from app.models.workspace import Workspace  # noqa: E402
