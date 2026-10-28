from typing import Optional
from pydantic import BaseModel, ConfigDict


class SettingsUpdate(BaseModel):
    receptionist_name: Optional[str] = None
    default_greeting: Optional[str] = None
    after_hours_message: Optional[str] = None
    ai_inbound_calling: Optional[bool] = None
    rag_responses: Optional[bool] = None
    appointment_booking: Optional[bool] = None
    human_escalation: Optional[bool] = None
    call_recording: Optional[bool] = None


class SettingsOut(BaseModel):
    id: int
    receptionist_name: str
    default_greeting: str
    after_hours_message: str
    ai_inbound_calling: bool
    rag_responses: bool
    appointment_booking: bool
    human_escalation: bool
    call_recording: bool

    model_config = ConfigDict(from_attributes=True)
