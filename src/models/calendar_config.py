from sqlalchemy import Column, DateTime, Integer, String, Boolean, ForeignKey
from sqlalchemy.sql import func

from src.config.database import Base

class CalendarConfig(Base):
    __tablename__ = "calendar_configs"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, default=1)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, default=1, index=True)
    provider = Column(String(50), default="Google Calendar")
    email = Column(String(255), nullable=True)
    access_token = Column(String(500), nullable=True)
    refresh_token = Column(String(500), nullable=True)
    connected = Column(String(20), default="disconnected")

    # Working Hours & Schedule Settings
    working_days = Column(String(255), default="Monday,Tuesday,Wednesday,Thursday,Friday")
    start_time = Column(String(10), default="09:00")
    end_time = Column(String(10), default="17:00")
    break_start = Column(String(10), default="13:00")
    break_end = Column(String(10), default="14:00")
    
    # Booking & Duration Settings
    default_appointment_duration = Column(Integer, default=30)
    staff_appointment_duration = Column(Integer, default=30)
    services_handled_by_staff = Column(Boolean, default=True)
    staff_availability = Column(Boolean, default=True)
    leave_management = Column(Boolean, default=True)
    holiday_blocking = Column(Boolean, default=True)
    online_booking = Column(Boolean, default=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
