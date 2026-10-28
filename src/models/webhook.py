import enum
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, JSON, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from src.config.database import Base


class WebhookType(str, enum.Enum):
    OUTGOING = "outgoing"
    INCOMING = "incoming"


class WebhookDeliveryStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    DELIVERED = "delivered"
    FAILED = "failed"
    RETRYING = "retrying"


class Webhook(Base):
    __tablename__ = "webhooks"

    id = Column(Integer, primary_key=True, index=True)
    webhook_uuid = Column(String(64), unique=True, index=True, nullable=True)  # Public secure identifier
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, default=1, index=True)
    name = Column(String(255), nullable=False)
    type = Column(String(50), nullable=False, default="outgoing")  # "outgoing" or "incoming"
    url = Column(String(1024), nullable=True)  # Destination URL for outgoing webhooks
    secret = Column(String(255), nullable=False)  # Cryptographically secure HMAC secret
    events = Column(JSON, nullable=False)  # List of subscribed events e.g. ["appointment.created", "call.completed"]
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    
    # Metadata & timestamps
    description = Column(String(500), nullable=True)
    last_triggered_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationship to deliveries
    deliveries = relationship("WebhookDelivery", back_populates="webhook", cascade="all, delete-orphan", lazy="dynamic")

    __table_args__ = (
        Index("ix_webhooks_org_active", "organization_id", "is_active"),
        Index("ix_webhooks_org_type", "organization_id", "type"),
    )


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id = Column(Integer, primary_key=True, index=True)
    webhook_id = Column(Integer, ForeignKey("webhooks.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, default=1, index=True)
    
    event = Column(String(100), nullable=False, index=True)
    event_id = Column(String(100), nullable=False, index=True)
    direction = Column(String(20), nullable=False, default="outgoing")  # "outgoing" or "incoming"
    
    request_url = Column(String(1024), nullable=True)
    request_headers = Column(JSON, nullable=True)
    request_payload = Column(JSON, nullable=True)
    
    response_status = Column(Integer, nullable=True)
    response_headers = Column(JSON, nullable=True)
    response_body = Column(Text, nullable=True)
    
    attempt = Column(Integer, default=1, nullable=False)
    max_attempts = Column(Integer, default=5, nullable=False)
    status = Column(String(50), default="pending", nullable=False, index=True)  # pending, processing, delivered, failed, retrying
    duration_ms = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationship
    webhook = relationship("Webhook", back_populates="deliveries")

    __table_args__ = (
        Index("ix_wh_deliveries_org_status", "organization_id", "status"),
        Index("ix_wh_deliveries_wh_created", "webhook_id", "created_at"),
        Index("ix_wh_deliveries_event_id", "event_id"),
    )
