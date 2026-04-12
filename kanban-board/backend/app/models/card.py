"""Card model — a task card within a column."""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Card(Base):
    """Represents a task card on a Kanban board."""

    __tablename__ = "card"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    column_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("column.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    assignee_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    column: Mapped["Column"] = relationship(back_populates="cards", lazy="selectin")
    assignee: Mapped["User | None"] = relationship(
        back_populates="assigned_cards", lazy="selectin"
    )


from app.models.column import Column  # noqa: E402
from app.models.user import User  # noqa: E402
