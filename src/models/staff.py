import enum
from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.sql import func
from src.config.database import Base


class StaffStatus(str, enum.Enum):
    AVAILABLE = "Available"
    BUSY = "Busy"
    OFFLINE = "Offline"


class Staff(Base):
    __tablename__ = "staff"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, default=1)
    name = Column(String(150), nullable=False)

    department = Column(String(100), nullable=False)
    email = Column(String(200), nullable=True)
    phone = Column(String(50), nullable=True)

    # Existing compatibility field.
    working_hours = Column(String(100), nullable=False, default="09:00-18:00")

    appointment_duration = Column(String(50), default="30 min")
    status = Column(Enum(StaffStatus), default=StaffStatus.AVAILABLE)

    # Availability configuration.
    working_days = Column(Text, nullable=False, default='["Monday","Tuesday","Wednesday","Thursday","Friday"]')
    start_time = Column(String(10), nullable=False, default="09:00")
    end_time = Column(String(10), nullable=False, default="18:00")
    break_start = Column(String(10), nullable=True)
    break_end = Column(String(10), nullable=True)
    services = Column(Text, nullable=False, default="[]")
    leave_dates = Column(Text, nullable=False, default="[]")
    holiday_blocked_dates = Column(Text, nullable=False, default="[]")
    online_booking_enabled = Column(Boolean, default=True, nullable=False)
    calendar_connected = Column(Boolean, default=False, nullable=False)
    calendar_provider = Column(String(50), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
