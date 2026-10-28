from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.sql import func

from src.config.database import Base

class WhatsAppConfig(Base):
    __tablename__ = "whatsapp_configs"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, default=1)
    provider = Column(String(255), default="Meta WhatsApp Cloud API")
    phone_number = Column(String(50), nullable=True)
    phone_number_id = Column(String(100), nullable=True)
    waba_id = Column(String(100), nullable=True)
    access_token = Column(String(500), nullable=True)
    template = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
