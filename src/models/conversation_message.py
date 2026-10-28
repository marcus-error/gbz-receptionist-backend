from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.sql import func
from src.config.database import Base


class ConversationMessage(Base):
    """
    Conversation Message record storage model for AI Receptionist interactions.
    Stores user and assistant turns with intent, language, emotion, channel,
    verified appointment details, tool execution metrics, and status.
    """
    __tablename__ = "conversation_messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(100), index=True, nullable=False)
    customer_id = Column(String(100), nullable=True)
    role = Column(String(20), nullable=False)  # "user" | "assistant" | "system" | "tool"
    message = Column(Text, nullable=False)
    language = Column(String(50), default="English")
    intent = Column(String(100), nullable=True)

    # Channel distinguishes web-chat from voice (browser WebRTC / Twilio / SIP)
    channel = Column(String(20), default="voice", nullable=True)  # "voice" | "chat" | "twilio" | "sip"

    # Emotion & sentiment detected by EmotionService at response generation time
    emotion = Column(String(100), nullable=True)    # e.g. "Neutral", "Happy", "Frustrated"
    sentiment = Column(String(50), nullable=True)   # e.g. "Positive", "Negative", "Neutral"

    doctor_id = Column(Integer, nullable=True)
    doctor_name = Column(String(150), nullable=True)
    department = Column(String(100), nullable=True)
    appointment_date = Column(String(50), nullable=True)
    appointment_time = Column(String(50), nullable=True)
    availability_verified = Column(Boolean, default=False)
    booking_confirmed = Column(Boolean, default=False)
    appointment_id = Column(Integer, nullable=True)

    tool_name = Column(String(100), nullable=True)
    tool_status = Column(String(50), nullable=True)
    error_status = Column(String(255), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
