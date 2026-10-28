import enum
from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT

from sqlalchemy.sql import func

from src.config.database import Base


class CallType(str, enum.Enum):
    INBOUND = "Inbound"
    OUTBOUND = "Outbound"
    VOICE_TEST = "Voice Test"


class CallLog(Base):
    __tablename__ = "call_logs"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, default=1)
    caller_phone = Column(String(50), nullable=True)

    caller_name = Column(String(150), default="Unknown")
    call_type = Column(Enum(CallType), default=CallType.INBOUND)

    # Channel: distinguishes voice (Twilio/SIP/WebRTC) from web-chat interactions
    channel = Column(String(20), default="voice", nullable=True)  # "voice" | "chat" | "twilio" | "sip"

    status = Column(String(100), default="INBOUND CALL • CONNECTED")
    duration = Column(String(50), default="02:14")

    caller_said = Column(Text().with_variant(LONGTEXT, "mysql"), nullable=True)
    ai_response = Column(Text().with_variant(LONGTEXT, "mysql"), nullable=True)

    # Telephony / Recording specifics
    call_sid = Column(String(100), nullable=True)
    recording_url = Column(String(255), nullable=True)
    recording_status = Column(String(50), default="NOT_RECORDED", nullable=True)  # RECORDING, PROCESSING, COMPLETED, FAILED, NOT_RECORDED
    recording_file_size = Column(Integer, nullable=True)  # in bytes
    recording_duration = Column(Integer, nullable=True)  # in seconds
    recording_error = Column(Text, nullable=True)

    intent = Column(String(150), default="Appointment Booking")
    emotion = Column(String(100), default="Neutral — 91%")
    language = Column(String(50), default="English")
    rag_source = Column(String(255), default="legal-services.pdf")
    receptionist_state = Column(Text, nullable=True)  # JSON or comma-separated steps
    messages = Column(Text().with_variant(LONGTEXT, "mysql"), default="[]")  # Store conversation history as JSON string

    # Link to a specific lead and script for automated outbound calls
    lead_id = Column(Integer, ForeignKey("leads.id", ondelete="SET NULL"), nullable=True)
    outbound_script = Column(Text().with_variant(LONGTEXT, "mysql"), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    @property
    def customer_number(self):
        return self.caller_phone

    @property
    def caller_number(self):
        return self.caller_phone

    @property
    def customer_name(self):
        return self.caller_name
