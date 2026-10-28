"""
AI Chat Receptionist API Routes.

Provides endpoints for the CallZenza-style AI Chat Receptionist:
- POST /chat/message: Process a conversational message through AgentEngine.
- GET  /chat/greeting: Retrieve initial receptionist greeting and quick actions.
- GET  /chat/history/{session_id}: Fetch message history for the session.
- POST /chat/reset/{session_id}: Reset conversation session memory.
- GET  /chat/config: Retrieve active receptionist branding, hours, and quick actions.
"""

import time
import logging
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.models.conversation_message import ConversationMessage
from src.models.settings import AppSettings
from src.models.calendar_config import CalendarConfig
from src.models.organization import Organization
from src.services.agent_engine import agent_engine, DEFAULT_QUICK_ACTIONS
from src.services.booking_state_service import booking_state_service
from src.services.rag_service import rag_service
from src.services.tts_service import tts_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["AI Chat Receptionist"])

# Short-lived in-memory deduplication cache: (session_id, message_id) -> (timestamp, result)
_DEDUP_CACHE: Dict[str, Any] = {}
_DEDUP_WINDOW_SECONDS: float = 5.0


class ChatMessageRequest(BaseModel):
    session_id: str = Field(..., description="Unique conversation session ID")
    message_id: Optional[str] = Field(None, description="Client-generated unique message ID for deduplication")
    text: str = Field(..., description="User message text or action command")
    language: Optional[str] = Field("en", description="Conversation language code (e.g., en, ta, hi)")
    organization_id: Optional[int] = Field(1, description="Tenant organization ID")
    scope: Optional[str] = Field("organization", description="Scope of the chat (e.g., organization, superadmin_assistant)")
    action: Optional[str] = Field(None, description="Optional structured action (e.g., reschedule, cancellation, handoff)")


class ChatMessageResponse(BaseModel):
    response: str
    intent: Optional[str] = None
    emotion: Optional[str] = None
    language: Optional[str] = "en"
    stage: Optional[str] = None
    next_action: Optional[str] = None
    quick_actions: List[str] = []
    collected_fields: Dict[str, Any] = {}
    missing_fields: List[str] = []
    active_rag_title: Optional[str] = None
    session_id: str
    telemetry: Optional[Dict[str, Any]] = None
    handoff: Optional[Dict[str, Any]] = None
    callback: Optional[Dict[str, Any]] = None


@router.post("/message", response_model=ChatMessageResponse)
async def process_chat_message(payload: ChatMessageRequest):
    """
    Process an incoming user message through the shared AgentEngine.
    Enforces deduplication to prevent double-sends from UI double-clicks or effects.
    """
    session_id = payload.session_id.strip()
    user_text = payload.text.strip()
    msg_id = payload.message_id or f"msg_{int(time.time()*1000)}"

    if not user_text and not payload.action:
        raise HTTPException(status_code=400, detail="Message text or action is required.")

    # Deduplication check
    dedup_key = f"{session_id}:{msg_id}"
    now = time.time()

    # Clean old dedup keys (older than 10 seconds)
    stale_keys = [k for k, v in _DEDUP_CACHE.items() if now - v.get("time", 0) > 10.0]
    for k in stale_keys:
        _DEDUP_CACHE.pop(k, None)

    if dedup_key in _DEDUP_CACHE:
        entry = _DEDUP_CACHE[dedup_key]
        if now - entry.get("time", 0) < _DEDUP_WINDOW_SECONDS:
            logger.info(f"[CHAT_MESSAGE] Dropped duplicate message {dedup_key} within {_DEDUP_WINDOW_SECONDS}s window.")
            return entry.get("result")

    # Process through AgentEngine
    result = await agent_engine.process_turn(
        user_text=user_text,
        session_id=session_id,
        organization_id=payload.organization_id or 1,
        conversation_language=payload.language or "en",
        utterance_id=msg_id,
        channel="chat",
        client_action=payload.action,
        scope=payload.scope
    )

    response_data = ChatMessageResponse(
        response=result["response"],
        intent=result.get("intent"),
        emotion=result.get("emotion"),
        language=result.get("language", "en"),
        stage=result.get("stage"),
        next_action=result.get("next_action"),
        quick_actions=result.get("quick_actions", DEFAULT_QUICK_ACTIONS),
        collected_fields=result.get("collected_fields", {}),
        missing_fields=result.get("missing_fields", []),
        active_rag_title=result.get("active_rag_title"),
        session_id=session_id,
        telemetry=result.get("telemetry"),
        handoff=result.get("handoff"),
        callback=result.get("callback")
    )

    _DEDUP_CACHE[dedup_key] = {"time": time.time(), "result": response_data}
    return response_data


@router.get("/greeting")
def get_chat_greeting(
    organization_id: int = 1,
    session_id: Optional[str] = None,
    language: str = "en",
    scope: str = "organization"
):
    """
    Retrieve initial receptionist greeting and contextual quick action suggestions.
    """
    sess_id = session_id or f"chat_{int(time.time()*1000)}"
    greeting_data = agent_engine.get_opening_greeting(
        organization_id=organization_id,
        session_id=sess_id,
        language=language,
        scope=scope
    )
    greeting_data["session_id"] = sess_id
    return greeting_data


@router.get("/history/{session_id}")
def get_chat_history(session_id: str, db: Session = Depends(get_db)):
    """
    Retrieve persisted conversation history for a given chat session.
    """
    messages = (
        db.query(ConversationMessage)
        .filter(ConversationMessage.session_id == session_id)
        .order_by(ConversationMessage.created_at.asc())
        .all()
    )
    if not messages:
        from src.services.booking_state_service import booking_state_service
        session = booking_state_service.get_session(session_id)
        if session:
            history_items = getattr(session, "history", None) or []
            if history_items:
                return [
                    {
                        "id": idx + 1,
                        "speaker": item.get("role", "user"),
                        "role": item.get("role", "user"),
                        "message": item.get("text", ""),
                        "detected_intent": item.get("intent", "general"),
                        "intent": item.get("intent", "general"),
                        "created_at": None
                    }
                    for idx, item in enumerate(history_items)
                ]
    return [
        {
            "id": msg.id,
            "speaker": msg.role,
            "role": msg.role,
            "message": msg.message,
            "detected_intent": msg.intent,
            "intent": msg.intent,
            "created_at": msg.created_at.isoformat() if msg.created_at else None
        }
        for msg in messages
    ]


@router.post("/reset/{session_id}")
def reset_chat_session(session_id: str, organization_id: int = 1, language: str = "en"):
    """
    Reset conversational session memory and return a fresh greeting.
    """
    booking_state_service.remove_session(session_id)
    logger.info(f"[CHAT_SESSION] Session {session_id} reset cleanly.")
    data = agent_engine.get_opening_greeting(
        organization_id=organization_id,
        session_id=session_id,
        language=language
    )
    data["session_id"] = session_id
    data["status"] = "reset"
    return data


@router.get("/config")
def get_chat_config(organization_id: int = 1, db: Session = Depends(get_db)):
    """Retrieve active receptionist branding, working hours, and configured quick actions."""
    active_doc_id = rag_service._resolve_active_document_id(organization_id)
    org_name = rag_service.get_organization_name(organization_id, document_id=active_doc_id)
    if not org_name or org_name == "AI Receptionist":
        org = db.query(Organization).filter(Organization.id == organization_id).first()
        if org and org.name:
            org_name = org.name

    settings = db.query(AppSettings).filter(AppSettings.organization_id == organization_id).first()
    calendar_cfg = db.query(CalendarConfig).filter(CalendarConfig.organization_id == organization_id).first()

    working_hours = "9:00 AM - 6:00 PM"
    if calendar_cfg and calendar_cfg.start_time and calendar_cfg.end_time:
        working_hours = f"{calendar_cfg.start_time} - {calendar_cfg.end_time}"

    assistant_name = settings.receptionist_name if (settings and settings.receptionist_name) else (org_name or "Ava")

    return {
        "organization_name": org_name or "AI Receptionist",
        "assistant_name": assistant_name,
        "working_hours": working_hours,
        "quick_actions": DEFAULT_QUICK_ACTIONS,
        "active_rag_id": active_doc_id
    }


@router.get("/tts")
async def get_tts_audio(text: str, language: str = "en", voice: Optional[str] = None):
    """Synthesizes text to speech audio bytes via Murf AI (Voice: en-US-natalie, Model: GEN2)."""
    if not text or not text.strip():
        raise HTTPException(status_code=400, detail="Text is required for TTS synthesis.")

    t0 = time.time()
    audio_bytes = await tts_service.synthesize_stream_chunk_async(text.strip(), language=language, voice_id=voice)
    elapsed_ms = int((time.time() - t0) * 1000)
    logger.info(f"[TTS_API] Synthesized {len(audio_bytes) if audio_bytes else 0} bytes in {elapsed_ms}ms")

    if not audio_bytes:
        raise HTTPException(status_code=502, detail="Speech synthesis failed. Please check TTS provider configuration.")

    # Detect audio format: MP3 starts with 0xFF 0xFB or ID3; WAV starts with RIFF
    if audio_bytes[:4] == b"RIFF":
        media_type = "audio/wav"
    elif audio_bytes[:3] == b"ID3" or (len(audio_bytes) >= 2 and audio_bytes[0] == 0xFF and (audio_bytes[1] & 0xE0) == 0xE0):
        media_type = "audio/mpeg"
    else:
        media_type = "audio/mpeg"  # Default to MP3 (Murf default)

    # Cache audio in browser for 1 hour — repeated phrases (greetings, common answers) are instant
    return Response(
        content=audio_bytes,
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-TTS-Latency-Ms": str(elapsed_ms)
        }
    )


class SessionSummaryRequest(BaseModel):
    session_id: str = Field(..., description="Session ID to generate post-call/chat intelligence summary for")
    organization_id: Optional[int] = Field(1, description="Tenant organization ID")


@router.post("/summary")
def get_session_summary(payload: SessionSummaryRequest):
    """
    Generate post-session intelligence summary (customer name, intent, sentiment, outcome, next action)
    using LLM-grounded analysis of all conversation turns.
    """
    summary = agent_engine.generate_session_summary(payload.session_id, payload.organization_id or 1)
    return summary


@router.get("/quick-actions")
def get_quick_actions(session_id: Optional[str] = None):
    """
    Retrieve contextual quick action chips based on the current booking session state.
    """
    if session_id:
        booking_session = booking_state_service.get_session(session_id)
        return {"quick_actions": agent_engine.get_quick_actions(booking_session)}
    return {"quick_actions": DEFAULT_QUICK_ACTIONS}

