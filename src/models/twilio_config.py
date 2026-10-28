from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.sql import func
from src.config.database import Base


class TwilioConfig(Base):
    __tablename__ = "twilio_configs"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, default=1, index=True)
    account_sid = Column(String(255), nullable=True)
    auth_token = Column(String(255), nullable=True)
    twilio_number = Column(String(50), nullable=True)
    webhook_url = Column(String(255), nullable=True)
    call_status_url = Column(String(255), nullable=True)
    recording_status_url = Column(String(255), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
