"""
Unified AgentEngine Service for AI Receptionist.

Acts as the single source of conversational intelligence across BOTH
Voice (WebSocket / Telephony) and Chat (REST / WebSocket) channels.

Ensures:
- 1:1 parity between Chat and Voice AI behavior.
- Shared RAG retrieval with strict organization and ready-document scoping.
- Shared multi-turn booking, reschedule, cancellation, callback, and handoff state machine.
- Single-pass intent and emotion detection.
- Fast-path sub-50ms rule responses for greetings, pleasantries, and identity queries.
- Consistent structured logging and persistent conversation message tracking.
"""

import asyncio
import logging
import re
import time
from typing import Dict, Any, List, Optional

from src.config.database import SessionLocal
from src.models.knowledge_doc import KnowledgeDocument, KnowledgeDocStatus
from src.services.ai_service import ai_service
from src.services.rag_service import rag_service
from src.services.nlp_service import nlp_service
from src.services.emotion_service import emotion_service
from src.services.booking_state_service import (
    booking_state_service,
    BookingSession,
    BookingState,
    NextAction
)
from src.services.call_service import CallService
from src.services.language_service import language_service
from src.services.receptionist_tools import cancel_appointment, reschedule_appointment
from src.services.calendar_verification_service import calendar_verification_service
from src.utils.latency_tracker import LatencyTracker

logger = logging.getLogger(__name__)

FAST_PATH_INTENTS = {
    "EMERGENCY", "MEDICAL_ADVICE", "GREETING", "GOODBYE", "THANK_YOU",
    "CONFIRMATION", "REJECTION", "YES", "NO", "ACKNOWLEDGEMENT",
    "LANGUAGE_SWITCH",
}

DEFAULT_QUICK_ACTIONS = [
    "Book Appointment",
    "Check Availability",
    "Services",
    "Working Hours",
    "Talk to Human",
    "Request Callback"
]


class AgentEngine:
    """
    Centralized conversational brain for AI Receptionist.
    Processes user utterances from either Voice or Chat and returns
    rich, structured turn outcomes.
    """

    def __init__(self):
        pass

    def get_quick_actions(self, session: BookingSession) -> List[str]:
        """Generate dynamic, context-aware quick actions based on current conversation stage."""
        stage = getattr(session, "current_stage", BookingState.IDLE)
        if stage == BookingState.WAITING_FOR_DATE:
            return ["Today", "Tomorrow", "Next Monday", "Check Availability", "Cancel"]
        elif stage == BookingState.WAITING_FOR_TIME:
            return ["Morning", "Afternoon", "10:00 AM", "02:00 PM", "04:30 PM", "Cancel"]
        elif stage == BookingState.WAITING_FOR_CONFIRMATION:
            return ["Yes, Confirm", "Change Time", "Change Date", "Cancel"]
        elif stage in (BookingState.BOOKED, BookingState.COMPLETED):
            return ["Book Another", "Our Services", "Working Hours", "Thank You"]
        elif stage == BookingState.WAITING_FOR_PATIENT_NAME:
            return ["Skip", "Cancel Booking"]
        elif stage == BookingState.WAITING_FOR_CONTACT:
            return ["Cancel Booking"]
        return DEFAULT_QUICK_ACTIONS

    def get_opening_greeting(self, organization_id: int = 1, session_id: Optional[str] = None, language: str = "en", scope: Optional[str] = None) -> Dict[str, Any]:
        """
        Retrieves the initial welcome greeting for a new conversation session.
        Uses active RAG document / organization script configuration.
        """
        active_doc_id = rag_service._resolve_active_document_id(organization_id, scope=scope)
        org_name = rag_service.get_organization_name(organization_id, document_id=active_doc_id, scope=scope)
        greeting = rag_service.get_organization_greeting(organization_id, document_id=active_doc_id, scope=scope)

        session = None
        if session_id:
            session = booking_state_service.get_session(session_id)
            session.greeting_sent = True
            session.conversation_started = True
            session.organization_id = organization_id

        return {
            "response": greeting,
            "organization_name": org_name or "AI Receptionist",
            "intent": "GREETING",
            "emotion": "Neutral - 100%",
            "language": language,
            "stage": BookingState.IDLE,
            "next_action": NextAction.USER_INPUT_REQUIRED,
            "quick_actions": DEFAULT_QUICK_ACTIONS,
            "collected_fields": {},
            "missing_fields": [],
            "active_rag": active_doc_id
        }

    async def process_turn(
        self,
        user_text: str,
        session_id: str,
        organization_id: Optional[int] = None,
        conversation_language: Optional[str] = None,
        utterance_id: Optional[str] = None,
        channel: str = "chat",
        session_history: Optional[List[Dict[str, str]]] = None,
        client_action: Optional[str] = None,
        scope: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Executes a single conversational turn through the central AgentEngine pipeline.

        Returns:
            Dict containing:
                - response: AI text response
                - intent: Detected intent
                - emotion: Detected emotion with confidence
                - language: Conversation language code
                - stage: Current booking/receptionist stage
                - next_action: Next required action
                - quick_actions: Contextual quick actions
                - collected_fields: Verified booking fields
                - missing_fields: Remaining fields to collect
                - active_rag_title: Title of active RAG document
                - telemetry: Latency breakdown
                - handoff: Optional handoff metadata
                - callback: Optional callback metadata
        """
        tracker = LatencyTracker(call_sid=session_id)
        tracker.mark("turn_started_at")
        t_start = time.perf_counter()

        booking_session = booking_state_service.get_session(session_id)
        if organization_id is not None:
            booking_session.organization_id = organization_id
        else:
            organization_id = getattr(booking_session, "organization_id", None)
        if conversation_language:
            booking_session.conversation_language = conversation_language

        history = session_history if session_history is not None else booking_session.history

        active_doc_id = rag_service._resolve_active_document_id(organization_id, scope=scope)
        active_doc_title = "None"
        if active_doc_id:
            try:
                db = SessionLocal()
                q = db.query(KnowledgeDocument).filter(
                    KnowledgeDocument.id == int(active_doc_id),
                    KnowledgeDocument.ai_usage == "Active"
                )
                if organization_id is not None:
                    q = q.filter(KnowledgeDocument.organization_id == int(organization_id))
                doc = q.first()
                if doc:
                    active_doc_title = doc.title
                else:
                    active_doc_id = None
                db.close()
            except Exception:
                pass

        # 1. LANGUAGE DETECTION & EXPLICIT SWITCH
        target_lang, is_lang_switch = language_service.check_explicit_language_switch(user_text)
        if is_lang_switch and target_lang:
            prev_lang = booking_session.conversation_language
            booking_session.conversation_language = target_lang
            current_lang = target_lang

            logger.info(
                f"[AGENT] [LANGUAGE SWITCH] '{prev_lang}' -> '{target_lang}' "
                f"session={session_id} channel={channel}"
            )
            lang_resp = language_service.get_language_switch_confirmation(
                target_lang, booking_session=booking_session
            )
            self._log_turn(session_id, user_text, lang_resp, "LANGUAGE_SWITCH", booking_session, channel=channel, emotion="Neutral - 100%")
            post_eval = booking_state_service.evaluate_post_response(booking_session)

            return {
                "response": lang_resp,
                "intent": "LANGUAGE_SWITCH",
                "emotion": "Neutral - 100%",
                "language": target_lang,
                "stage": booking_session.current_stage,
                "next_action": post_eval.get("next_action", NextAction.USER_INPUT_REQUIRED),
                "quick_actions": self.get_quick_actions(booking_session),
                "collected_fields": booking_session.collected_fields,
                "missing_fields": booking_session.missing_fields,
                "active_rag_title": active_doc_title,
                "telemetry": tracker.log_summary()
            }

        # Determine conversation language
        current_lang = language_service.determine_conversation_language(
            user_text, current_lang=booking_session.conversation_language
        )
        booking_session.conversation_language = current_lang
        tracker.mark("language_detected_at")

        # 2. INTENT & EMOTION DETECTION
        t_nlp_start = time.perf_counter()
        nlp_task = nlp_service.detect_intent_async(user_text)
        emotion_task = emotion_service.detect_emotion_async(user_text)
        nlp_res, emotion_res = await asyncio.gather(nlp_task, emotion_task)

        intent_name = nlp_res.intent
        emotion = f"{emotion_res.emotion.title()} - {int(emotion_res.confidence * 100)}%"
        tracker.record("intent_nlp", (time.perf_counter() - t_nlp_start) * 1000.0)
        tracker.mark("intent_detected_at")

        # Handle explicit client action overrides if provided
        if client_action:
            action_map = {
                "reschedule": "appointment_rescheduling",
                "cancellation": "appointment_cancellation",
                "callback": "callback",
                "handoff": "transfer request",
                "human_handoff": "transfer request"
            }
            if client_action in action_map:
                intent_name = action_map[client_action]

        logger.info(
            f"[AGENT] session_id={session_id} utterance_id={utterance_id} "
            f"channel={channel} intent={intent_name} emotion={emotion} lang={current_lang}"
        )

        # 3. ACTION SIGNALS (HANDOFF, CALLBACK, CANCEL, RESCHEDULE)
        handoff_data = None
        callback_data = None

        # Human Handoff
        # Guard: NEVER trigger handoff for informational questions.
        # e.g. "can you tell me about a receptionist" must NOT trigger handoff.
        _is_informational_question = any(phrase in user_text.lower() for phrase in [
            "tell me about", "what is", "what are", "explain", "describe",
            "how does", "how do", "can you tell", "what do you", "give me information",
            "information about", "what's a", "what is a", "how is a", "about a"
        ])

        _is_explicit_handoff_request = (
            not _is_informational_question
            and (
                intent_name in ("transfer request", "human_handoff", "human_transfer")
                or any(w in user_text.lower() for w in [
                    "speak to a human", "speak with a human", "speak to human", "speak with human",
                    "real person", "speak to agent", "speak with agent",
                    "talk to human", "talk to a human", "talk to a person",
                    "human representative", "connect me to a human", "connect me with a human"
                ])
            )
        )

        if _is_explicit_handoff_request:
            handoff_msg = "I am connecting you to our human representative right away. Please wait a moment."
            if current_lang in ("ta", "tanglish", "ta_mix"):
                handoff_msg = "உங்களை எங்கள் human representative-க்கு transfer செய்கிறேன். ஒரு நிமிடம் காத்திருங்கள்."
            elif current_lang == "hi":
                handoff_msg = "Main aapko hamare human representative se connect kar raha hoon. Kripya ek pal intezar karein."

            handoff_data = {
                "status": "initiated",
                "customer": booking_session.full_name or "Customer",
                "phone": booking_session.phone_number or "Not provided",
                "reason": "Caller requested human assistance"
            }
            self._log_turn(session_id, user_text, handoff_msg, "transfer request", booking_session, channel=channel, emotion=emotion)
            post_eval = booking_state_service.evaluate_post_response(booking_session)

            return {
                "response": handoff_msg,
                "intent": "transfer request",
                "emotion": emotion,
                "language": current_lang,
                "stage": booking_session.current_stage,
                "next_action": post_eval.get("next_action", NextAction.COMPLETED),
                "quick_actions": self.get_quick_actions(booking_session),
                "collected_fields": booking_session.collected_fields,
                "missing_fields": booking_session.missing_fields,
                "active_rag_title": active_doc_title,
                "handoff": handoff_data,
                "telemetry": tracker.log_summary()
            }



        # Callback Request
        if intent_name == "callback":
            cb_phone = booking_session.phone_number or "your phone number"
            cb_msg = f"I have registered your callback request for {cb_phone}. Our team will contact you shortly."
            if current_lang in ("ta", "tanglish", "ta_mix"):
                cb_msg = f"உங்க callback request register பண்ணியாச்சு. எங்கள் team உங்களை விரைவில் தொடர்புகொள்வார்கள்."
            elif current_lang == "hi":
                cb_msg = f"Aapka callback request note kar liya gaya hai. Hamari team aapse jaldi sampark karegi."

            callback_data = {
                "phone": booking_session.phone_number,
                "customer": booking_session.full_name,
                "status": "registered"
            }
            self._log_turn(session_id, user_text, cb_msg, "callback", booking_session, channel=channel, emotion=emotion)
            post_eval = booking_state_service.evaluate_post_response(booking_session)

            return {
                "response": cb_msg,
                "intent": "callback",
                "emotion": emotion,
                "language": current_lang,
                "stage": booking_session.current_stage,
                "next_action": post_eval.get("next_action", NextAction.USER_INPUT_REQUIRED),
                "quick_actions": self.get_quick_actions(booking_session),
                "collected_fields": booking_session.collected_fields,
                "missing_fields": booking_session.missing_fields,
                "active_rag_title": active_doc_title,
                "callback": callback_data,
                "telemetry": tracker.log_summary()
            }

        # Appointment Cancellation
        if intent_name == "appointment_cancellation":
            appt_id_match = re.search(r'\b(?:id|appointment\s+id|appointment\s*#|#)\s*(\d+)\b', user_text, re.IGNORECASE)
            target_id = int(appt_id_match.group(1)) if appt_id_match else booking_session.appointment_id
            if target_id:
                res = cancel_appointment(target_id)
                cancel_msg = res.get("message", f"Appointment #{target_id} has been cancelled.")
            else:
                cancel_msg = "Please provide your appointment ID so I can cancel your booking."

            self._log_turn(session_id, user_text, cancel_msg, "appointment_cancellation", booking_session, channel=channel, emotion=emotion)
            post_eval = booking_state_service.evaluate_post_response(booking_session)

            return {
                "response": cancel_msg,
                "intent": "appointment_cancellation",
                "emotion": emotion,
                "language": current_lang,
                "stage": booking_session.current_stage,
                "next_action": post_eval.get("next_action", NextAction.USER_INPUT_REQUIRED),
                "quick_actions": self.get_quick_actions(booking_session),
                "collected_fields": booking_session.collected_fields,
                "missing_fields": booking_session.missing_fields,
                "active_rag_title": active_doc_title,
                "telemetry": tracker.log_summary()
            }

        # Appointment Rescheduling
        if intent_name == "appointment_rescheduling":
            appt_id_match = re.search(r'\b(?:id|appointment\s+id|appointment\s*#|#)\s*(\d+)\b', user_text, re.IGNORECASE)
            target_id = int(appt_id_match.group(1)) if appt_id_match else booking_session.appointment_id

            booking_state_service.extract_slots(user_text, booking_session)
            if target_id and booking_session.date and booking_session.time:
                res = reschedule_appointment(target_id, booking_session.date, booking_session.time, organization_id)
                reschedule_msg = res.get("message", f"Appointment #{target_id} rescheduled.")
            else:
                reschedule_msg = "Sure, I can help you reschedule. What new date and time would you prefer?"

            self._log_turn(session_id, user_text, reschedule_msg, "appointment_rescheduling", booking_session, channel=channel, emotion=emotion)
            post_eval = booking_state_service.evaluate_post_response(booking_session)

            return {
                "response": reschedule_msg,
                "intent": "appointment_rescheduling",
                "emotion": emotion,
                "language": current_lang,
                "stage": booking_session.current_stage,
                "next_action": post_eval.get("next_action", NextAction.USER_INPUT_REQUIRED),
                "quick_actions": self.get_quick_actions(booking_session),
                "collected_fields": booking_session.collected_fields,
                "missing_fields": booking_session.missing_fields,
                "active_rag_title": active_doc_title,
                "telemetry": tracker.log_summary()
            }

        # 4. FAST-PATH RULE RESPONSE (<50ms)
        is_ongoing_turn = bool(
            booking_session.conversation_started or
            booking_session.greeting_sent or
            booking_session.turn_count > 0 or
            len(history) > 0
        )
        fast_rule_answer = ai_service._fast_rule_response(
            user_text, intent_name,
            organization_id=organization_id,
            is_ongoing=is_ongoing_turn,
            booking_session=booking_session
        )

        if fast_rule_answer:
            logger.info(f"[AGENT] [FAST PATH] Satisfied '{user_text}' in <50ms without LLM/RAG.")
            self._log_turn(session_id, user_text, fast_rule_answer, intent_name, booking_session, channel=channel, emotion=emotion)
            post_eval = booking_state_service.evaluate_post_response(booking_session)

            return {
                "response": fast_rule_answer,
                "intent": intent_name,
                "emotion": emotion,
                "language": current_lang,
                "stage": booking_session.current_stage,
                "next_action": post_eval.get("next_action", NextAction.USER_INPUT_REQUIRED),
                "quick_actions": self.get_quick_actions(booking_session),
                "collected_fields": booking_session.collected_fields,
                "missing_fields": booking_session.missing_fields,
                "active_rag_title": active_doc_title,
                "telemetry": tracker.log_summary()
            }

        # Check for intent switch
        is_intent_switched = booking_state_service.check_and_handle_intent_switch(
            booking_session, intent_name, user_text
        )

        # 5. MULTI-TURN BOOKING STATE MACHINE
        if not is_intent_switched and (
            booking_session.is_active() or
            intent_name in ("appointment_booking", "appointment_availability")
        ):
            if intent_name == "appointment_booking" and booking_session.is_completed:
                booking_session.reset()

            response_text = booking_state_service.process_booking_turn(user_text, booking_session)
            self._log_turn(session_id, user_text, response_text, intent_name, booking_session, channel=channel, emotion=emotion)
            post_eval = booking_state_service.evaluate_post_response(booking_session)

            # Auto action resolution if required
            if post_eval.get("next_action") == NextAction.AUTOMATIC_ACTION_REQUIRED:
                auto_resp = booking_state_service.process_booking_turn("", booking_session)
                if auto_resp:
                    response_text += f" {auto_resp}"
                post_eval = booking_state_service.evaluate_post_response(booking_session)

            return {
                "response": response_text,
                "intent": intent_name,
                "emotion": emotion,
                "language": current_lang,
                "stage": booking_session.current_stage,
                "next_action": post_eval.get("next_action", NextAction.USER_INPUT_REQUIRED),
                "quick_actions": self.get_quick_actions(booking_session),
                "collected_fields": booking_session.collected_fields,
                "missing_fields": booking_session.missing_fields,
                "active_rag_title": active_doc_title,
                "telemetry": tracker.log_summary()
            }

        # 6. RAG RETRIEVAL & GROUNDED LLM PIPELINE
        booking_session.conversation_started = True
        booking_session.turn_count += 1
        context = ""

        if intent_name not in FAST_PATH_INTENTS:
            t_rag = time.perf_counter()
            tracker.mark("retrieval_started_at")
            try:
                context = await asyncio.to_thread(
                    rag_service.retrieve_context,
                    user_text, 5, organization_id, intent_name, active_doc_id, scope, session_id, utterance_id
                )
            except Exception as e:
                logger.error(f"[AGENT] RAG retrieval error: {e}")
                context = ""
            tracker.record("retrieval_ms", (time.perf_counter() - t_rag) * 1000.0)
            tracker.mark("retrieval_completed_at")

        # Live Calendar verification if availability
        if intent_name in ("appointment_availability", "appointment_booking"):
            try:
                cal_v = calendar_verification_service.verify_availability(user_text, history)
                if cal_v and cal_v.is_checked and not cal_v.is_available:
                    alt_slots = ", ".join(cal_v.alternative_slots) if cal_v.alternative_slots else "another date"
                    context += f"\n[CALENDAR AVAILABILITY NOTE: Requested slot is full. Available slots: {alt_slots}]"
            except Exception as e:
                logger.warning(f"[AGENT] Calendar verification error: {e}")

        # 7. LLM RESPONSE GENERATION
        t_llm = time.perf_counter()
        tracker.mark("llm_started_at")
        history.append({"role": "user", "text": user_text})

        try:
            response_text = await asyncio.to_thread(
                ai_service.generate_response,
                text=user_text,
                context=context,
                session_history=history,
                intent=intent_name,
                emotion=emotion,
                conversation_language=current_lang,
                organization_id=organization_id,
                conversation_state=booking_session.get_state_dict()
            )
        except Exception as e:
            logger.error(f"[AGENT] LLM generation error: {e}")
            response_text = "I apologize, but I am having trouble processing that right now. How else may I assist you?"

        history.append({"role": "assistant", "text": response_text})
        tracker.record("llm_total_ms", (time.perf_counter() - t_llm) * 1000.0)
        tracker.mark("llm_completed_at")

        self._log_turn(session_id, user_text, response_text, intent_name, booking_session, channel=channel, emotion=emotion)
        post_eval = booking_state_service.evaluate_post_response(booking_session)

        telemetry = tracker.log_summary()

        return {
            "response": response_text,
            "intent": intent_name,
            "emotion": emotion,
            "language": current_lang,
            "stage": booking_session.current_stage,
            "next_action": post_eval.get("next_action", NextAction.USER_INPUT_REQUIRED),
            "quick_actions": self.get_quick_actions(booking_session),
            "collected_fields": booking_session.collected_fields,
            "missing_fields": booking_session.missing_fields,
            "active_rag_title": active_doc_title,
            "telemetry": telemetry
        }

    def _log_turn(
        self,
        session_id: str,
        user_text: str,
        assistant_text: str,
        intent: str,
        booking_session: Optional[BookingSession] = None,
        channel: str = "chat",
        emotion: Optional[str] = None,
    ):
        if booking_session:
            booking_session.history.append({"role": "user", "text": user_text, "intent": intent})
            booking_session.history.append({"role": "assistant", "text": assistant_text, "intent": intent})
            booking_session.turn_count += 1

        try:
            db = SessionLocal()
            try:
                svc = CallService(db)
                # Derive sentiment from emotion label for analytics grouping
                sentiment = "Neutral"
                if emotion:
                    e_lower = emotion.lower()
                    if any(w in e_lower for w in ["happy", "satisfied", "positive"]):
                        sentiment = "Positive"
                    elif any(w in e_lower for w in ["frustrated", "angry", "upset", "negative", "urgent"]):
                        sentiment = "Negative"

                svc.log_conversation_message(
                    session_id=session_id,
                    role="user",
                    message=user_text,
                    intent=intent,
                    channel=channel,
                    emotion=emotion,
                    sentiment=sentiment,
                )
                svc.log_conversation_message(
                    session_id=session_id,
                    role="assistant",
                    message=assistant_text,
                    intent=intent,
                    channel=channel,
                    emotion=emotion,
                    sentiment=sentiment,
                    doctor_name=booking_session.doctor if booking_session else None,
                    appointment_date=booking_session.date if booking_session else None,
                    appointment_time=booking_session.time if booking_session else None,
                    availability_verified=booking_session.availability_verified if booking_session else False,
                    booking_confirmed=booking_session.booking_confirmed if booking_session else False,
                    appointment_id=booking_session.appointment_id if booking_session else None,
                )
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"[AGENT] Failed to log conversation turn to DB: {e}")

    def generate_session_summary(self, session_id: str, organization_id: int = 1) -> Dict[str, Any]:
        """
        Generates a structured post-conversation summary using Gemini.
        Reads all conversation_messages for the session and produces:
          - summary: brief plain-text summary of the interaction
          - customer_name: detected customer name
          - primary_intent: dominant intent in the conversation
          - sentiment: overall sentiment (Positive / Neutral / Negative)
          - outcome: what was accomplished (e.g. Appointment Booked, Callback Registered)
          - next_action: recommended follow-up action
        """
        try:
            db = SessionLocal()
            from src.models.conversation_message import ConversationMessage
            msgs = (
                db.query(ConversationMessage)
                .filter(ConversationMessage.session_id == session_id)
                .order_by(ConversationMessage.created_at.asc())
                .all()
            )
            db.close()

            if not msgs:
                return {"summary": "No conversation recorded.", "outcome": "unknown"}

            # Build transcript for LLM
            transcript_lines = []
            for m in msgs:
                prefix = "User" if m.role == "user" else "AI"
                transcript_lines.append(f"{prefix}: {m.message}")
            transcript = "\n".join(transcript_lines[-60:])  # limit to last 60 turns

            prompt = (
                "You are an AI Receptionist intelligence analyst. "
                "Analyze the following call transcript and reply ONLY with valid JSON (no markdown, no code fences).\n\n"
                f"Transcript:\n{transcript}\n\n"
                "Return JSON with these exact keys:\n"
                '{ "summary": "...", "customer_name": "...", "primary_intent": "...", '
                '"sentiment": "Positive|Neutral|Negative", "outcome": "...", "next_action": "..." }'
            )

            raw = ai_service.generate_response(
                text=prompt,
                context="",
                session_history=[],
                intent="post_session_summary",
                emotion="Neutral",
                conversation_language="en",
                organization_id=organization_id,
                conversation_state={}
            )

            import json as _json
            # Strip markdown fences if present
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("```")[1]
                if cleaned.startswith("json"):
                    cleaned = cleaned[4:]
            result = _json.loads(cleaned.strip())
            logger.info(f"[AGENT] Post-session summary generated for {session_id}: {result.get('outcome')}")
            return result
        except Exception as e:
            logger.warning(f"[AGENT] Failed to generate session summary for {session_id}: {e}")
            return {
                "summary": "Summary could not be generated.",
                "customer_name": "Unknown",
                "primary_intent": "unknown",
                "sentiment": "Neutral",
                "outcome": "unknown",
                "next_action": "Review conversation manually"
            }


# Global singleton instance
agent_engine = AgentEngine()
