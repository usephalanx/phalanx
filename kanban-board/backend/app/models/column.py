"""Column model — a vertical lane within a board."""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Column(Base):
    """Represents a column (e.g. To Do, In Progress, Done) on a board."""

    __tablename__ = "column"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    board_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("board.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    color: Mapped[str | None] = mapped_column(String(50), nullable=True)
    wip_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    board: Mapped["Board"] = relationship(back_populates="columns", lazy="selectin")
    cards: Mapped[list["Card"]] = relationship(
        back_populates="column", lazy="selectin", cascade="all, delete-orphan",
        order_by="Card.position",
    )


from app.models.board import Board  # noqa: E402
from app.models.card import Card  # noqa: E402
