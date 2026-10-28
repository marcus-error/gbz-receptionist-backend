"""
Analytics API Routes for AI Receptionist.

Provides comprehensive analytics on:
- Conversation volume (Voice vs Chat channel breakdown)
- Sentiment and Emotion distribution
- Intent frequency and resolution rates
- Conversion metrics (Appointments booked, callbacks requested, handoffs)
- Paginated conversation session logs
"""

import logging
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, distinct, case

from src.config.database import get_db
from src.models.conversation_message import ConversationMessage
from src.models.call_log import CallLog
from src.models.appointment import Appointment

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analytics", tags=["Conversation & Receptionist Analytics"])


@router.get("/overview")
def get_analytics_overview(
    organization_id: int = 1,
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db)
):
    """
    Returns executive overview metrics:
    - Total sessions (Voice vs Chat)
    - Total messages
    - Sentiment distribution (Positive / Neutral / Negative)
    - Appointments booked & conversion rate
    - Callbacks and human handoffs requested
    """
    cutoff = datetime.utcnow() - timedelta(days=days)

    # 1. Total Messages and Channel Breakdown
    msg_query = db.query(ConversationMessage).filter(
        ConversationMessage.created_at >= cutoff
    )

    total_messages = msg_query.count()

    # Distinct sessions count by channel
    sessions_by_channel = (
        db.query(
            ConversationMessage.channel,
            func.count(distinct(ConversationMessage.session_id)).label("session_count")
        )
        .filter(ConversationMessage.created_at >= cutoff)
        .group_by(ConversationMessage.channel)
        .all()
    )

    voice_sessions = 0
    chat_sessions = 0
    for row in sessions_by_channel:
        ch = (row[0] or "chat").lower()
        if "voice" in ch:
            voice_sessions += row[1]
        else:
            chat_sessions += row[1]

    total_sessions = voice_sessions + chat_sessions

    # 2. Sentiment Breakdown
    sentiment_counts = (
        db.query(
            ConversationMessage.sentiment,
            func.count(ConversationMessage.id)
        )
        .filter(
            ConversationMessage.created_at >= cutoff,
            ConversationMessage.sentiment.isnot(None)
        )
        .group_by(ConversationMessage.sentiment)
        .all()
    )

    sentiments = {"Positive": 0, "Neutral": 0, "Negative": 0}
    for s_name, count in sentiment_counts:
        if s_name in sentiments:
            sentiments[s_name] = count
        elif s_name:
            sentiments["Neutral"] += count

    # 3. Appointments Booked (from conversation confirmed flags + appointment table)
    confirmed_in_chat = (
        db.query(func.count(distinct(ConversationMessage.appointment_id)))
        .filter(
            ConversationMessage.created_at >= cutoff,
            ConversationMessage.booking_confirmed == True,
            ConversationMessage.appointment_id.isnot(None)
        )
        .scalar() or 0
    )

    total_appointments = (
        db.query(func.count(Appointment.id))
        .filter(
            Appointment.organization_id == organization_id,
            Appointment.created_at >= cutoff
        )
        .scalar() or 0
    )

    # 4. Handoffs and Callbacks
    handoffs_count = (
        db.query(func.count(distinct(ConversationMessage.session_id)))
        .filter(
            ConversationMessage.created_at >= cutoff,
            ConversationMessage.intent.in_(["transfer request", "human_handoff", "human_transfer"])
        )
        .scalar() or 0
    )

    callbacks_count = (
        db.query(func.count(distinct(ConversationMessage.session_id)))
        .filter(
            ConversationMessage.created_at >= cutoff,
            ConversationMessage.intent == "callback"
        )
        .scalar() or 0
    )

    return {
        "period_days": days,
        "total_sessions": total_sessions,
        "voice_sessions": voice_sessions,
        "chat_sessions": chat_sessions,
        "total_messages": total_messages,
        "sentiment_distribution": sentiments,
        "appointments_booked": max(confirmed_in_chat, total_appointments),
        "handoffs_requested": handoffs_count,
        "callbacks_requested": callbacks_count,
        "conversion_rate_pct": round((max(confirmed_in_chat, total_appointments) / max(1, total_sessions)) * 100, 1)
    }


@router.get("/intents")
def get_top_intents(
    organization_id: int = 1,
    limit: int = Query(10, ge=1, le=50),
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db)
):
    """Returns the most frequently triggered intents with their counts."""
    cutoff = datetime.utcnow() - timedelta(days=days)

    results = (
        db.query(
            ConversationMessage.intent,
            func.count(ConversationMessage.id).label("count")
        )
        .filter(
            ConversationMessage.created_at >= cutoff,
            ConversationMessage.intent.isnot(None),
            ConversationMessage.intent != ""
        )
        .group_by(ConversationMessage.intent)
        .order_by(func.count(ConversationMessage.id).desc())
        .limit(limit)
        .all()
    )

    return [{"intent": r[0], "count": r[1]} for r in results]


@router.get("/conversations")
def get_conversations_list(
    channel: Optional[str] = None,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db)
):
    """
    Returns aggregated conversation sessions with turn count, dominant intent,
    sentiment, latest message snippet, and timestamps.
    """
    # Group messages by session_id
    subq = (
        db.query(
            ConversationMessage.session_id,
            func.max(ConversationMessage.channel).label("channel"),
            func.count(ConversationMessage.id).label("message_count"),
            func.max(ConversationMessage.sentiment).label("sentiment"),
            func.max(ConversationMessage.created_at).label("last_activity"),
            func.min(ConversationMessage.created_at).label("started_at")
        )
        .group_by(ConversationMessage.session_id)
    )

    if channel:
        subq = subq.filter(ConversationMessage.channel == channel)

    subq = subq.order_by(func.max(ConversationMessage.created_at).desc()).limit(limit).offset(offset).all()

    sessions_data = []
    for row in subq:
        sess_id = row[0]
        # Fetch last message in this session
        last_msg = (
            db.query(ConversationMessage)
            .filter(ConversationMessage.session_id == sess_id)
            .order_by(ConversationMessage.created_at.desc())
            .first()
        )
        # Fetch primary intent
        primary_intent = last_msg.intent if last_msg else "general"

        sessions_data.append({
            "session_id": sess_id,
            "channel": row[1] or "chat",
            "message_count": row[2],
            "sentiment": row[3] or "Neutral",
            "primary_intent": primary_intent,
            "last_message": last_msg.message if last_msg else "",
            "started_at": row[5].isoformat() if row[5] else None,
            "last_activity": row[4].isoformat() if row[4] else None,
        })

    return {
        "total": len(sessions_data),
        "limit": limit,
        "offset": offset,
        "conversations": sessions_data
    }
