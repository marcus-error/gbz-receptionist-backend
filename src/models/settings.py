from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.sql import func

from src.config.database import Base


class AppSettings(Base):
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, default=1)
    receptionist_name = Column(String(100), default="Ava")
    default_greeting = Column(
        Text,
        default="Hello! Welcome to our company. I'm Ava, your AI receptionist. How may I help you today?",
    )
    after_hours_message = Column(
        Text,
        default="Our office is currently closed. I can still help you with general questions or schedule an appointment for the next available time.",
    )

    ai_inbound_calling = Column(Boolean, default=True)
    rag_responses = Column(Boolean, default=True)
    appointment_booking = Column(Boolean, default=True)
    human_escalation = Column(Boolean, default=True)
    call_recording = Column(Boolean, default=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Twilio Programmable Voice Settings
    twilio_account_sid = Column(String(100), nullable=True)
    twilio_auth_token = Column(String(100), nullable=True)
    twilio_phone_number = Column(String(50), nullable=True)
    twilio_phone_number_sid = Column(String(100), nullable=True)
    twilio_webhook_url = Column(String(255), nullable=True)

    # SIP settings
    sip_domain = Column(String(255), nullable=True)
    sip_credential_list_sid = Column(String(100), nullable=True)
    sip_ip_access_control_list_sid = Column(String(100), nullable=True)
