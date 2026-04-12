"""Board model — a Kanban board within a workspace."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Board(Base):
    """Represents a Kanban board belonging to a workspace."""

    __tablename__ = "board"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    workspace: Mapped["Workspace"] = relationship(
        back_populates="boards", lazy="selectin"
    )
    columns: Mapped[list["Column"]] = relationship(
        back_populates="board", lazy="selectin", cascade="all, delete-orphan",
        order_by="Column.position",
    )


from app.models.column import Column  # noqa: E402
from app.models.workspace import Workspace  # noqa: E402
