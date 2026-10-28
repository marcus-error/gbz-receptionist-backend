from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.sql import func

from src.config.database import Base

class EmailConfig(Base):
    __tablename__ = "email_configs"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, default=1)
    provider = Column(String(50), default="Resend")  # Resend, SendGrid, SMTP
    api_key = Column(String(255), nullable=True)
    from_email = Column(String(255), nullable=True)
    
    smtp_host = Column(String(255), default="smtp.gmail.com")
    smtp_port = Column(String(50), default="587")
    email_address = Column(String(255), nullable=True)
    password = Column(String(255), nullable=True)
    sender_name = Column(String(255), default="AI Receptionist")
    encryption = Column(String(50), default="TLS")
    template = Column(Text, nullable=True)
    staff_template = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
