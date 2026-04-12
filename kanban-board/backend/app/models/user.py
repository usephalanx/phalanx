"""User model for authentication and identity."""

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class User(Base):
    """Represents a registered user in the system."""

    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(
        String(100), nullable=False, server_default=""
    )
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    owned_workspaces: Mapped[list["Workspace"]] = relationship(
        back_populates="owner", lazy="selectin"
    )
    memberships: Mapped[list["WorkspaceMember"]] = relationship(
        back_populates="user", lazy="selectin"
    )
    assigned_cards: Mapped[list["Card"]] = relationship(
        back_populates="assignee", lazy="selectin"
    )


# Avoid circular import issues with TYPE_CHECKING
from app.models.card import Card  # noqa: E402
from app.models.workspace import Workspace  # noqa: E402
from app.models.workspace_member import WorkspaceMember  # noqa: E402
