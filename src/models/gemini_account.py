from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.sql import func

from src.config.database import Base

class GeminiAccount(Base):
    __tablename__ = "gemini_accounts"

    id = Column(Integer, primary_key=True, index=True)
    account = Column(String(255), nullable=False)
    status = Column(String(50), default="Active")
    requests = Column(String(50), default="0")
    quota = Column(String(50), default="0%")
    api_key = Column(String(500), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
