"""Workspace model for multi-tenant organization."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Workspace(Base):
    """Represents a workspace that contains boards."""

    __tablename__ = "workspace"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True
    )
    owner_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    owner: Mapped["User"] = relationship(
        back_populates="owned_workspaces", lazy="selectin"
    )
    members: Mapped[list["WorkspaceMember"]] = relationship(
        back_populates="workspace", lazy="selectin", cascade="all, delete-orphan"
    )
    boards: Mapped[list["Board"]] = relationship(
        back_populates="workspace", lazy="selectin", cascade="all, delete-orphan"
    )


from app.models.board import Board  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.workspace_member import WorkspaceMember  # noqa: E402
