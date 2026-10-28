from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.sql import func
from src.config.database import Base


class NotificationLog(Base):
    __tablename__ = "notification_logs"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, default=1)
    appointment_id = Column(Integer, nullable=True, index=True)

    recipient_type = Column(String(50), nullable=False, default="customer")  # "customer" or "staff"
    recipient_name = Column(String(150), nullable=True)
    recipient_contact = Column(String(200), nullable=True)  # email address or phone number

    channel = Column(String(50), nullable=False)  # "email" or "whatsapp"
    action = Column(String(50), nullable=False)  # "created", "rescheduled", "cancelled", "reminder_24h", "reminder_1h"

    subject = Column(String(255), nullable=True)
    message_body = Column(Text, nullable=False)

    status = Column(String(50), nullable=False, default="sent")  # "sent", "failed", "queued"
    error_reason = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
