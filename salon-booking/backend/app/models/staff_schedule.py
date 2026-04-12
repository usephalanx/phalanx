"""Staff schedule model for working hours."""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.staff import Staff


class StaffSchedule(Base):
    """Represents a staff member's working hours for a given day of the week."""

    __tablename__ = "staff_schedule"

    __table_args__ = (
        UniqueConstraint("staff_id", "day_of_week", name="uq_staff_day"),
        CheckConstraint(
            "day_of_week >= 0 AND day_of_week <= 6",
            name="ck_schedule_day_of_week",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    staff_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("staff.id"), nullable=False
    )
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[str] = mapped_column(
        String(5), nullable=False, default="09:00"
    )
    end_time: Mapped[str] = mapped_column(
        String(5), nullable=False, default="17:00"
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    staff: Mapped["Staff"] = relationship(back_populates="schedules", lazy="selectin")
