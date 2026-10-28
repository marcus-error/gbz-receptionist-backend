import enum

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.sql import func

from src.config.database import Base


class AppointmentStatus(str, enum.Enum):
    PENDING = "Pending"
    CONFIRMED = "Confirmed"
    RESCHEDULED = "Rescheduled"
    CANCELLED = "Cancelled"
    COMPLETED = "Completed"


class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, default=1)
    customer = Column(String(150), nullable=False)
    customer_id = Column(String(50), nullable=True, index=True)

    phone = Column(String(50), nullable=True)
    email = Column(String(150), nullable=True)
    department = Column(String(100), nullable=False)
    staff = Column(String(150), nullable=False)
    service = Column(String(200), nullable=True)  # Optional service/procedure name
    appointment_date = Column(String(50), nullable=False)  # e.g. "04 Sep 2026"
    appointment_time = Column(String(50), nullable=False)  # e.g. "03:00 PM"
    duration = Column(String(50), default="30 Minutes", nullable=True)
    notes = Column(String(500), nullable=True)
    reminder = Column(String(100), default="WhatsApp + Email")
    status = Column(Enum(AppointmentStatus), default=AppointmentStatus.PENDING)
    calendar_event_id = Column(String(255), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
