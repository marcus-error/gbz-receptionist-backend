from datetime import datetime, timezone
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.sql import func

from src.config.database import Base


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    plan = Column(String(50), default="Enterprise", nullable=False)  # Trial, Pro, Enterprise
    status = Column(String(50), default="active", nullable=False)  # trial, active, expired, cancelled, suspended
    
    start_date = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    expiry_date = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
