from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.sql import func
from src.config.database import Base

class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, default=1, index=True)
    
    # Generated unique reference, e.g. "LEAD-1001"
    lead_number = Column(String(50), nullable=True, index=True)

    # Customer Information
    name = Column(String(150), nullable=True, default=None)
    phone = Column(String(50), nullable=False, index=True)
    email = Column(String(150), nullable=True)

    # Call & Campaign Information
    call_type = Column(String(50), nullable=True, default="Inbound")  # "Inbound" or "Outbound"
    call_time = Column(DateTime(timezone=True), nullable=True, index=True)
    script = Column(Text, nullable=True)
    
    # Backward compatibility with automated dialer: "pending", "calling", "completed", "failed"
    status = Column(String(50), nullable=False, default="pending", index=True)
    
    # Standard Lead Status: "New", "Contacted", "Qualified", "Converted", "Lost"
    lead_status = Column(String(50), nullable=False, default="New", index=True)

    # Appointment Status: "Not Booked", "Pending", "Booked", "Completed", "Cancelled"
    appointment_status = Column(String(50), nullable=False, default="Not Booked", index=True)

    # Product / Service discussed or booked
    product_service = Column(String(255), nullable=True)
    service_discussed = Column(String(255), nullable=True)
    service_booked = Column(String(255), nullable=True)

    # Follow-up & Notes
    interest_notes = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    follow_up_required = Column(Boolean, default=False, nullable=False)
    follow_up_date = Column(DateTime(timezone=True), nullable=True)
    lead_source = Column(String(100), nullable=True, default="Voice Call")

    # Linked Entities
    call_log_id = Column(Integer, ForeignKey("call_logs.id", ondelete="SET NULL"), nullable=True)
    appointment_id = Column(Integer, ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True)
    calendar_event_id = Column(String(255), nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class LeadActivity(Base):
    __tablename__ = "lead_activities"

    id = Column(Integer, primary_key=True, index=True)
    lead_id = Column(Integer, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, default=1, index=True)

    # Event types: "lead_created", "call_received", "call_initiated", "call_completed",
    # "customer_details_collected", "product_discussed", "appointment_booked",
    # "appointment_rescheduled", "appointment_cancelled", "follow_up_scheduled", "status_changed", "note_added"
    event_type = Column(String(100), nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    metadata_json = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
