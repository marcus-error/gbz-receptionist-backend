import json
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.models.call_log import CallLog, CallType
from src.repositories.call_repository import CallRepository
from src.validators.call_validator import CallLogCreate


class CallService:
    """Persistent service for REAL Twilio calls only."""

    def __init__(self, db: Session):
        self.db = db
        self.repo = CallRepository(db)

    def list_calls(self, organization_id: Optional[int] = None) -> List[CallLog]:
        return self.repo.get_all(organization_id=organization_id)

    def get_latest_call(self, call_type: Optional[CallType] = None, organization_id: Optional[int] = None) -> Optional[CallLog]:
        if call_type:
            return self.repo.get_latest_by_type(call_type, organization_id=organization_id)
        calls = self.repo.get_all(organization_id=organization_id)
        return calls[0] if calls else None

    def create_call(self, payload: CallLogCreate, organization_id: Optional[int] = None) -> CallLog:
        data = payload.model_dump()
        if organization_id is not None:
            data["organization_id"] = organization_id
        return self.repo.create(data)

    def create_from_twilio(self, data: dict) -> CallLog:
        """Create a call session from Twilio's real webhook event."""
        if not data.get("call_sid"):
            raise HTTPException(400, "Twilio CallSid is required")

        existing = self.repo.get_by_sid(data["call_sid"])
        if existing:
            return existing

        data.setdefault("call_type", CallType.INBOUND)
        data.setdefault("status", "ringing")
        data.setdefault("duration", "00:00")
        data.setdefault("caller_name", "Unknown")
        data.setdefault("messages", "[]")
        return self.repo.create(data)

    def update_from_twilio(self, call_sid: str, data: dict) -> Optional[CallLog]:
        """Update lifecycle fields from real Twilio callbacks."""
        call = self.repo.update_by_sid(call_sid, data)
        if call:
            self.db.commit()
            self.db.refresh(call)
        return call

    def append_conversation(
        self,
        call_sid: str,
        caller_text: Optional[str] = None,
        ai_text: Optional[str] = None,
        intent: Optional[str] = None,
        rag_sources: Optional[list] = None,
        appointment_id: Optional[int] = None,
    ) -> Optional[CallLog]:
        """Persist live caller/AI turns and RAG sources for the real call."""
        call = self.repo.get_by_sid(call_sid)
        if not call:
            return None

        try:
            history = json.loads(call.messages or "[]")
            if not isinstance(history, list):
                history = []
        except (TypeError, ValueError):
            history = []

        if caller_text:
            call.caller_said = caller_text
            history.append({
                "role": "caller",
                "content": caller_text,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        if ai_text:
            call.ai_response = ai_text
            history.append({
                "role": "assistant",
                "content": ai_text,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        if intent:
            call.intent = intent

        if rag_sources is not None:
            call.rag_source = ", ".join(str(x) for x in rag_sources)

        if appointment_id is not None:
            # The existing schema has no appointment_id column, so retain the
            # relationship in receptionist_state without changing the model.
            try:
                state = json.loads(call.receptionist_state or "{}")
                if not isinstance(state, dict):
                    state = {}
            except (TypeError, ValueError):
                state = {}
            state["appointment_id"] = appointment_id
            call.receptionist_state = json.dumps(state)

        call.messages = json.dumps(history, ensure_ascii=False)
        call.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(call)
        return call

    def set_call_outcome(self, call_sid: str, outcome: str) -> Optional[CallLog]:
        call = self.repo.get_by_sid(call_sid)
        if not call:
            return None

        try:
            state = json.loads(call.receptionist_state or "{}")
            if not isinstance(state, dict):
                state = {}
        except (TypeError, ValueError):
            state = {}

        state["call_outcome"] = outcome
        call.receptionist_state = json.dumps(state)
        call.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(call)
        return call

    def log_conversation_message(
        self,
        session_id: str,
        role: str,
        message: str,
        language: str = "English",
        intent: Optional[str] = None,
        channel: Optional[str] = "chat",
        emotion: Optional[str] = None,
        sentiment: Optional[str] = None,
        doctor_id: Optional[int] = None,
        doctor_name: Optional[str] = None,
        department: Optional[str] = None,
        appointment_date: Optional[str] = None,
        appointment_time: Optional[str] = None,
        availability_verified: bool = False,
        booking_confirmed: bool = False,
        appointment_id: Optional[int] = None,
        tool_name: Optional[str] = None,
        tool_status: Optional[str] = None,
        customer_id: Optional[str] = None,
    ):
        """Persist structured conversation message turn into conversation_messages table."""
        from src.models.conversation_message import ConversationMessage
        record = ConversationMessage(
            session_id=session_id,
            customer_id=customer_id,
            role=role,
            message=message,
            language=language,
            intent=intent,
            channel=channel,
            emotion=emotion,
            sentiment=sentiment,
            doctor_id=doctor_id,
            doctor_name=doctor_name,
            department=department,
            appointment_date=appointment_date,
            appointment_time=appointment_time,
            availability_verified=availability_verified,
            booking_confirmed=booking_confirmed,
            appointment_id=appointment_id,
            tool_name=tool_name,
            tool_status=tool_status
        )
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

