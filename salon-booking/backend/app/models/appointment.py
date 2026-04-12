"""Appointment model for salon bookings."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.service import Service
from app.models.staff import Staff


class Appointment(Base):
    """Represents a booked appointment at the salon."""

    __tablename__ = "appointment"

    __table_args__ = (
        CheckConstraint(
            "status IN ('BOOKED', 'CONFIRMED', 'COMPLETED', 'CANCELLED')",
            name="ck_appointment_status",
        ),
        Index("idx_appointment_staff_time", "staff_id", "start_time", "end_time"),
        Index("idx_appointment_status", "status"),
        Index("idx_appointment_date", "start_time"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_name: Mapped[str] = mapped_column(String(100), nullable=False)
    customer_email: Mapped[str] = mapped_column(String(255), nullable=False)
    customer_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    staff_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("staff.id"), nullable=False
    )
    service_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("service.id"), nullable=False
    )
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="BOOKED"
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    staff: Mapped["Staff"] = relationship(back_populates="appointments", lazy="selectin")
    service: Mapped["Service"] = relationship(
        back_populates="appointments", lazy="selectin"
    )
