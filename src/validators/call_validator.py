from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict

from src.models.call_log import CallType


class CallLogCreate(BaseModel):
    caller_phone: Optional[str] = None
    caller_name: Optional[str] = "Unknown"
    customer_number: Optional[str] = None
    caller_number: Optional[str] = None
    customer_name: Optional[str] = None
    call_type: Optional[CallType] = CallType.INBOUND
    status: Optional[str] = "INBOUND CALL • CONNECTED"
    duration: Optional[str] = "02:14"
    caller_said: Optional[str] = None
    ai_response: Optional[str] = None
    intent: Optional[str] = "Appointment Booking"
    emotion: Optional[str] = "Neutral — 91%"
    language: Optional[str] = "English"
    rag_source: Optional[str] = "legal-services.pdf"
    receptionist_state: Optional[str] = None


class CallLogOut(BaseModel):
    id: int
    organization_id: Optional[int] = None
    caller_phone: Optional[str] = None
    caller_name: Optional[str] = "Unknown"
    customer_number: Optional[str] = None
    caller_number: Optional[str] = None
    customer_name: Optional[str] = None
    call_type: Optional[CallType] = CallType.INBOUND
    channel: Optional[str] = None
    status: Optional[str] = None
    duration: Optional[str] = "00:00"
    caller_said: Optional[str] = None
    ai_response: Optional[str] = None
    call_sid: Optional[str] = None
    recording_url: Optional[str] = None
    intent: Optional[str] = None
    emotion: Optional[str] = None
    language: Optional[str] = None
    rag_source: Optional[str] = None
    receptionist_state: Optional[str] = None
    messages: Optional[str] = None
    lead_id: Optional[int] = None
    outbound_script: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)
