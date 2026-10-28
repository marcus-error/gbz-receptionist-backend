from sqlalchemy import Column, DateTime, Integer, String, Boolean, Text
from sqlalchemy.sql import func
from src.config.database import Base

class Workflow(Base):
    __tablename__ = "workflows"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, index=True, nullable=True)
    name = Column(String(150), nullable=False)
    description = Column(String(255), nullable=True)
    trigger_event = Column(String(100), nullable=False)  # call_ended, appointment_created, missed_call, emergency_call, lead_captured
    action_type = Column(String(100), nullable=False)    # send_whatsapp, send_email, notify_staff, transfer_call, sync_crm
    action_config = Column(Text, nullable=True)          # JSON string config
    is_active = Column(Boolean, default=True, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class WorkflowExecutionLog(Base):
    __tablename__ = "workflow_logs"

    id = Column(Integer, primary_key=True, index=True)
    workflow_id = Column(Integer, index=True, nullable=True)
    organization_id = Column(Integer, index=True, nullable=True)
    workflow_name = Column(String(150), nullable=True)
    trigger_event = Column(String(100), nullable=False)
    action_type = Column(String(100), nullable=False)
    status = Column(String(50), default="success", nullable=False)  # success, failed
    details = Column(Text, nullable=True)

    executed_at = Column(DateTime(timezone=True), server_default=func.now())
