import json
import base64
import logging
import asyncio
import time
import hashlib
import re
from typing import List, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.config.database import SessionLocal
from src.models.knowledge_doc import KnowledgeDocument, KnowledgeDocStatus
from src.models.user import User
from src.services.ai_service import ai_service
from src.services.rag_service import rag_service
from src.services.stt_service import stt_service
from src.services.nlp_service import nlp_service
from src.services.emotion_service import emotion_service
from src.services.tts_service import tts_service
from src.services.booking_state_service import booking_state_service, NextAction, BookingState
from src.services.call_service import CallService
from src.services.language_service import language_service
from src.services.agent_engine import agent_engine
from src.utils.latency_tracker import LatencyTracker
from src.utils.text_sanitizer import clean_markdown_for_voice

router = APIRouter()
logger = logging.getLogger(__name__)

FAST_PATH_INTENTS = {
    "EMERGENCY", "MEDICAL_ADVICE", "GREETING", "GOODBYE", "THANK_YOU",
    "CONFIRMATION", "REJECTION", "YES", "NO", "ACKNOWLEDGEMENT",
    "LANGUAGE_SWITCH",  # language-switch requests skip ALL RAG/NLP/booking
}

# How long (ms) to wait before playing a 'please hold' TTS during slow backend ops
_SLOW_FETCH_THRESHOLD_MS: float = 700.0


async def _handle_post_response_state(websocket: WebSocket, booking_session, session_id: str, current_lang: str, recent_ai_utterances: list):
    """
    Evaluates state immediately after TTS completes, emits state to client, and triggers automatic actions if ready.
    """
    logger.info(
        f"\n[STATE]\nprevious=RESPONDING\ncurrent=TTS\n"
        f"[TTS]\nstarted\ncompleted"
    )
    post_eval = booking_state_service.evaluate_post_response(booking_session)
    logger.info(
        f"\n[POST_RESPONSE_CHECK]\n"
        f"conversation_id={booking_session.conversation_id}\n"
        f"call_sid={booking_session.call_sid}\n"
        f"intent={booking_session.current_intent}\n"
        f"stage={booking_session.current_stage}\n"
        f"missing_fields={booking_session.missing_fields}\n"
        f"[NEXT_ACTION]\n"
        f"{post_eval['next_action']}"
    )

    await websocket.send_json({
        "type": "state",
        "state": post_eval["state"],
        "next_action": post_eval["next_action"],
        "stage": booking_session.current_stage,
        "missing_fields": booking_session.missing_fields
    })

    if post_eval["next_action"] == NextAction.AUTOMATIC_ACTION_REQUIRED:
        logger.info(f"[AUTOMATIC ACTION] Triggering auto-execution for session {booking_session.session_id}")
        auto_response = booking_state_service.process_booking_turn("", booking_session)
        norm_ans = _normalize_speech(auto_response)
        if norm_ans:
            recent_ai_utterances.append(norm_ans)

        await websocket.send_json({
            "type": "response",
            "text": auto_response
        })
        audio_response = await tts_service.synthesize_stream_chunk_async(auto_response, language=current_lang)
        if audio_response and len(audio_response) > 100:
            audio_base64 = base64.b64encode(audio_response).decode('utf-8')
            await websocket.send_json({
                "type": "audio",
                "audio": audio_base64
            })
        _log_turn(session_id, "[AUTO_ACTION]", auto_response, booking_session.current_intent, booking_session)

        # Follow-up evaluation after auto execution
        next_eval = booking_state_service.evaluate_post_response(booking_session)
        await websocket.send_json({
            "type": "state",
            "state": next_eval["state"],
            "next_action": next_eval["next_action"],
            "stage": booking_session.current_stage,
            "missing_fields": booking_session.missing_fields
        })
    elif post_eval["next_action"] in (NextAction.USER_INPUT_REQUIRED, NextAction.CONFIRMATION_REQUIRED):
        logger.info(f"\n[LISTENING]\nwaiting_for_caller=true")


def _log_turn(session_id: str, user_text: str, assistant_text: str, intent: str, booking_session=None, emotion: Optional[str] = None):
    try:
        db = SessionLocal()
        try:
            svc = CallService(db)
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
                channel="voice",
                emotion=emotion,
                sentiment=sentiment,
            )
            svc.log_conversation_message(
                session_id=session_id,
                role="assistant",
                message=assistant_text,
                intent=intent,
                channel="voice",
                emotion=emotion,
                sentiment=sentiment,
                doctor_name=booking_session.doctor if booking_session else None,
                appointment_date=booking_session.date if booking_session else None,
                appointment_time=booking_session.time if booking_session else None,
                availability_verified=booking_session.availability_verified if booking_session else False,
                booking_confirmed=booking_session.booking_confirmed if booking_session else False,
                appointment_id=booking_session.appointment_id if booking_session else None
            )
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Failed to log conversation turn to DB: {e}")


def _get_loading_message(lang: str, booking_session=None) -> str:
    """
    Returns a natural 'please hold' message in the correct language.
    Called when a backend operation (RAG/DB) exceeds the slow-fetch threshold.
    Never mentions 'loading', 'processing', or technical terms.
    """
    _MESSAGES = {
        "ta":        "ஒரு நிமிடம் please.",
        "tanglish":  "Oru nimisham wait pannunga.",
        "ta_mix":    "Oru nimisham wait pannunga.",
        "hi":        "Ek pal ruke, main check karta hoon.",
        "ml":        "ഒരു നിമിഷം, ഞാൻ നോക്കട്ടെ.",
        "te":        "ఒక్క నిమిషం, చూస్తాను.",
        "kn":        "ಒಂದು ನಿಮಿಷ, ನೋಡ್ತೇನೆ.",
        "en":        "Just one moment, please.",
    }
    return _MESSAGES.get(lang, "Just one moment, please.")


def _normalize_speech(text: str) -> str:
    """Strips punctuation and extra spaces for acoustic echo comparison."""
    return re.sub(r'[^a-zA-Z0-9\s]', '', (text or "").lower()).strip()


@router.websocket("/ws/voice")
@router.websocket("/ws/voice-stream")
async def voice_endpoint(websocket: WebSocket):
    await websocket.accept()
    session_id = f"ws_{id(websocket)}"
    session_history = []
    socket_lock = asyncio.Lock()
    last_processed_hash = None
    last_processed_time = 0.0

    # Resolve tenant organization from token query parameter or explicit organization_id
    token = websocket.query_params.get("token")
    query_org_id = websocket.query_params.get("organization_id")
    resolved_org_id: Optional[int] = None

    if token:
        try:
            clean_token = token.strip().strip('"\'')
            if clean_token.lower().startswith("bearer "):
                clean_token = clean_token[7:].strip()

            from src.config.jwt import decode_access_token
            payload = decode_access_token(clean_token)
            if payload and "sub" in payload:
                user_id = payload.get("sub")
                db_auth = SessionLocal()
                try:
                    user = db_auth.query(User).filter(User.id == int(user_id), User.is_active == 1).first()
                    if user:
                        if user.role == "super_admin":
                            if query_org_id:
                                try:
                                    resolved_org_id = int(query_org_id)
                                except (ValueError, TypeError):
                                    resolved_org_id = None
                            else:
                                resolved_org_id = user.organization_id
                        else:
                            resolved_org_id = user.organization_id
                finally:
                    db_auth.close()
            elif payload and "organization_id" in payload:
                resolved_org_id = payload.get("organization_id")
        except Exception as auth_err:
            logger.warning(f"[VOICE WS AUTH] Token validation failed: {auth_err}")

    # Session-bound Active RAG snapshot
    booking_session = booking_state_service.get_session(session_id)
    org_id = resolved_org_id or getattr(booking_session, "organization_id", None)
    booking_session.organization_id = org_id

    active_doc_id: Optional[str] = rag_service._resolve_active_document_id(org_id) if org_id is not None else None
    active_doc_title: str = "No Active Document"

    if active_doc_id and org_id is not None:
        db = SessionLocal()
        try:
            active_doc = db.query(KnowledgeDocument).filter(
                KnowledgeDocument.id == int(active_doc_id),
                KnowledgeDocument.organization_id == org_id,
                KnowledgeDocument.ai_usage == "Active",
                KnowledgeDocument.status.in_([KnowledgeDocStatus.INDEXED, "Indexed"])
            ).first()
            if active_doc:
                active_doc_title = active_doc.title
            else:
                active_doc_id = None
                active_doc_title = "No Active Document"
        except Exception as db_err:
            logger.warning(f"Active RAG lookup warning: {db_err}")
            active_doc_id = None
            active_doc_title = "No Active Document"
        finally:
            db.close()

    # Inform client of session-bound active RAG
    await websocket.send_json({
        "type": "rag_bound",
        "title": active_doc_title if (active_doc_id and active_doc_title != "None") else "No Active Document",
        "document_id": active_doc_id
    })

    # Server-Side Acoustic Echo Tracker
    recent_ai_utterances: List[str] = []
    last_ai_speech_time: float = 0.0

    # Send automatic opening greeting from active script immediately on connection
    try:
        opening_greeting = rag_service.get_organization_greeting(org_id, document_id=active_doc_id)
        if opening_greeting:
            booking_session.greeting_sent = True
            booking_session.conversation_started = True
            clean_norm = _normalize_speech(opening_greeting)
            if clean_norm:
                recent_ai_utterances.append(clean_norm)
            last_ai_speech_time = time.time()

            await websocket.send_json({
                "type": "response",
                "text": opening_greeting
            })
            audio_response = await tts_service.synthesize_stream_chunk_async(opening_greeting, language="en")
            if audio_response and len(audio_response) > 100:
                audio_base64 = base64.b64encode(audio_response).decode('utf-8')
                await websocket.send_json({
                    "type": "audio",
                    "audio": audio_base64
                })
            _log_turn(session_id, "[CALL_START]", opening_greeting, "greeting", booking_session)
            await _handle_post_response_state(websocket, booking_session, session_id, "en", recent_ai_utterances)
    except (asyncio.CancelledError, WebSocketDisconnect):
        logger.info(f"Opening greeting cancelled or disconnected for session {session_id}")
        return
    except Exception as exc:
        logger.warning(f"Opening greeting warning: {exc}")

    is_on_hold = False
    last_processed_utterance_id = None
    last_processed_hash = None
    last_processed_time = 0.0

    try:
        while True:
            data = await websocket.receive()

            async with socket_lock:
                tracker = LatencyTracker(call_sid=session_id)
                tracker.mark("audio_received_at")
                transcription = ""
                utterance_id = None

                if 'bytes' in data:
                    if is_on_hold:
                        logger.info(f"[RECEPTIONIST_SESSION] session_id={session_id} on hold — ignoring audio bytes.")
                        continue

                    audio_bytes = data['bytes']
                    logger.info(f"[STT] session_id={session_id} received audio packet of size {len(audio_bytes)}")

                    audio_hash = hashlib.md5(audio_bytes).hexdigest()
                    now = time.time()
                    if audio_hash == last_processed_hash and (now - last_processed_time) < 0.75:
                        logger.info(f"[VOICE_UTTERANCE] session_id={session_id} skipping duplicate audio packet (<750ms).")
                        continue
                    last_processed_hash = audio_hash
                    last_processed_time = now

                    # 1. Speech-to-Text
                    t0 = time.perf_counter()
                    tracker.mark("stt_started_at")
                    try:
                        transcription = await stt_service.transcribe_async(audio_bytes)
                    except Exception as e:
                        logger.error(f"[STT] session_id={session_id} transcription failed: {e}")
                        await websocket.send_json({"type": "error", "message": "Failed to transcribe audio"})
                        continue
                    t_stt_dur = (time.perf_counter() - t0) * 1000.0
                    tracker.record("stt", t_stt_dur)
                    tracker.mark("stt_completed_at")

                    if not transcription or transcription.strip() in {"Demo transcription", ""}:
                        continue

                    utterance_id = f"aud_{int(now*1000)}"

                elif 'text' in data:
                    try:
                        text_data = json.loads(data['text'])
                        msg_type = text_data.get('type')

                        if msg_type == 'auth':
                            auth_tok = text_data.get('token')
                            if auth_tok:
                                clean_tok = auth_tok.strip().strip('"\'')
                                if clean_tok.lower().startswith('bearer '):
                                    clean_tok = clean_tok[7:].strip()
                                from src.config.jwt import decode_access_token
                                payload = decode_access_token(clean_tok)
                                if payload and "sub" in payload:
                                    db_auth = SessionLocal()
                                    try:
                                        u = db_auth.query(User).filter(User.id == int(payload["sub"]), User.is_active == 1).first()
                                        if u and u.role != "super_admin":
                                            org_id = u.organization_id
                                            booking_session.organization_id = org_id
                                            active_doc_id = rag_service._resolve_active_document_id(org_id)
                                            active_doc_title = "No Active Document"
                                            if active_doc_id:
                                                active_doc = db_auth.query(KnowledgeDocument).filter(
                                                    KnowledgeDocument.id == int(active_doc_id),
                                                    KnowledgeDocument.organization_id == org_id,
                                                    KnowledgeDocument.ai_usage == "Active",
                                                    KnowledgeDocument.status.in_([KnowledgeDocStatus.INDEXED, "Indexed"])
                                                ).first()
                                                if active_doc:
                                                    active_doc_title = active_doc.title
                                            await websocket.send_json({
                                                "type": "rag_bound",
                                                "title": active_doc_title if (active_doc_id and active_doc_title != "None") else "No Active Document",
                                                "document_id": active_doc_id
                                            })
                                    finally:
                                        db_auth.close()
                            continue

                        if msg_type == 'interrupt':
                            logger.info(f"[VOICE_UTTERANCE] [BARGE-IN] session_id={session_id} received client voice interrupt signal.")
                            continue

                        if msg_type == 'hold':
                            is_on_hold = True
                            booking_session.state = "ON_HOLD"
                            logger.info(f"[RECEPTIONIST_SESSION] session_id={session_id} put ON_HOLD.")
                            await websocket.send_json({"type": "state", "state": "ON_HOLD"})
                            continue

                        if msg_type == 'resume':
                            is_on_hold = False
                            booking_session.state = "LISTENING"
                            logger.info(f"[RECEPTIONIST_SESSION] session_id={session_id} RESUMED.")
                            await websocket.send_json({"type": "state", "state": "LISTENING"})
                            continue

                        if msg_type in ('reschedule', 'cancellation', 'callback', 'handoff', 'human_handoff'):
                            target_intent = {
                                'reschedule': 'reschedule',
                                'cancellation': 'cancellation',
                                'callback': 'callback',
                                'handoff': 'human_handoff',
                                'human_handoff': 'human_handoff'
                            }.get(msg_type, msg_type)
                            booking_session.current_intent = target_intent
                            logger.info(f"[RECEPTIONIST_SESSION] session_id={session_id} explicit signal received for action '{msg_type}'. Setting intent={target_intent}.")
                            if not text_data.get('text'):
                                text_data['text'] = f"I would like to request {msg_type}."

                        if is_on_hold:
                            logger.info(f"[RECEPTIONIST_SESSION] session_id={session_id} on hold — ignoring text message.")
                            continue

                        transcription = text_data.get('text', '').strip()
                        utterance_id = text_data.get('utterance_id')
                    except Exception:
                        transcription = ""

                    if not transcription:
                        continue

                    now = time.time()
                    if utterance_id and utterance_id == last_processed_utterance_id and (now - last_processed_time) < 0.75:
                        logger.info(f"[VOICE_UTTERANCE] session_id={session_id} utterance_id={utterance_id} dropped duplicate (<750ms).")
                        continue

                    msg_hash = hashlib.md5(f"{transcription}_{utterance_id or ''}".encode('utf-8')).hexdigest()
                    if msg_hash == last_processed_hash and (now - last_processed_time) < 0.75:
                        logger.info(f"[VOICE_UTTERANCE] session_id={session_id} dropped duplicate text packet (<750ms).")
                        continue

                    last_processed_hash = msg_hash
                    last_processed_utterance_id = utterance_id
                    last_processed_time = now

                if not transcription:
                    continue

                # PRECISE SERVER-SIDE ACOUSTIC ECHO REJECTION
                norm_user = _normalize_speech(transcription)
                if norm_user:
                    is_echo = False
                    time_since_speech = time.time() - last_ai_speech_time

                    if time_since_speech < 2.0:
                        for recent_utt in recent_ai_utterances[-3:]:
                            if norm_user == recent_utt:
                                is_echo = True
                                break
                            user_words = set(norm_user.split())
                            ai_words = set(recent_utt.split())
                            if len(user_words) >= 4 and len(ai_words) >= 4:
                                jaccard = len(user_words & ai_words) / float(len(user_words | ai_words))
                                if jaccard > 0.85:
                                    is_echo = True
                                    break

                    if is_echo:
                        logger.warning(
                            f"[SERVER ACOUSTIC ECHO SUPPRESSED] Dropped incoming transcript matching recent AI output ({time_since_speech:.2f}s ago): '{transcription}'"
                        )
                        continue

                logger.info(
                    f"[STT_FINAL]\n"
                    f"session_id={session_id}\n"
                    f"call_sid={booking_session.call_sid}\n"
                    f"utterance_id={utterance_id}\n"
                    f"transcript='{transcription}' (len={len(transcription)})\n"
                    f"language={booking_session.conversation_language}\n"
                    f"stage={booking_session.state}"
                )
                await websocket.send_json({
                    "type": "transcription",
                    "text": transcription,
                    "utterance_id": utterance_id
                })

                # ============================================================
                # LANGUAGE-SWITCH FAST PATH — must fire BEFORE NLP, RAG,
                # booking state, and entity extraction.
                # ============================================================
                target_lang, is_lang_switch = language_service.check_explicit_language_switch(transcription)
                if is_lang_switch and target_lang:
                    prev_lang = booking_session.conversation_language
                    booking_session.conversation_language = target_lang
                    current_lang = target_lang

                    logger.info(
                        f"[AGENT] [LANGUAGE SWITCH] '{prev_lang}' → '{target_lang}' "
                        f"session={session_id} stage={booking_session.state}"
                    )

                    lang_resp = language_service.get_language_switch_confirmation(
                        target_lang, booking_session=booking_session
                    )

                    norm_ans = _normalize_speech(lang_resp)
                    if norm_ans:
                        recent_ai_utterances.append(norm_ans)
                    last_ai_speech_time = time.time()

                    await websocket.send_json({"type": "analysis", "intent": "LANGUAGE_SWITCH",
                                               "emotion": "Neutral - 100%", "language": target_lang})
                    await websocket.send_json({"type": "response", "text": lang_resp})

                    audio_response = await tts_service.synthesize_stream_chunk_async(lang_resp, language=target_lang)
                    if audio_response and len(audio_response) > 100:
                        audio_base64 = base64.b64encode(audio_response).decode('utf-8')
                        await websocket.send_json({"type": "audio", "audio": audio_base64})

                    _log_turn(session_id, transcription, lang_resp, "LANGUAGE_SWITCH", booking_session)
                    await _handle_post_response_state(websocket, booking_session, session_id, current_lang, recent_ai_utterances)
                    continue
                # ============================================================

                # 2. Language Lock & Single-Pass Intent / Emotion Understanding
                t_n_start = time.perf_counter()
                booking_session.conversation_language = language_service.determine_conversation_language(
                    transcription, current_lang=booking_session.conversation_language
                )
                current_lang = booking_session.conversation_language
                tracker.mark("language_detected_at")

                nlp_task = nlp_service.detect_intent_async(transcription)
                emotion_task = emotion_service.detect_emotion_async(transcription)

                nlp_res, emotion_res = await asyncio.gather(nlp_task, emotion_task)
                intent_name = nlp_res.intent
                emotion = f"{emotion_res.emotion.title()} - {int(emotion_res.confidence * 100)}%"
                t_nlp_dur = (time.perf_counter() - t_n_start) * 1000.0
                tracker.record("intent_nlp", t_nlp_dur)
                tracker.mark("intent_detected_at")

                logger.info(
                    f"[AGENT] session_id={session_id} utterance_id={utterance_id} intent={intent_name} emotion={emotion} lang={current_lang}"
                )

                await websocket.send_json({
                    "type": "analysis",
                    "intent": intent_name,
                    "emotion": emotion,
                    "language": current_lang
                })

                org_id = getattr(booking_session, "organization_id", None)

                # -------------------------------------------------------------
                # HUMAN HANDOFF HANDLING
                # -------------------------------------------------------------
                if intent_name == "transfer request":
                    handoff_msg = "I am transferring you to our human representative right away. Please stay on the line."
                    if current_lang in ("ta", "tanglish", "ta_mix"):
                        handoff_msg = "உங்களை எங்கள் human representative-க்கு transfer செய்கிறேன். தயவுசெய்து line-ல் இருங்கள்."
                    elif current_lang == "hi":
                        handoff_msg = "Main aapko hamare human representative ke paas transfer kar raha hoon. Kripya line par bane rahein."

                    logger.info(f"[HANDOFF] [RECEPTIONIST_SESSION] session_id={session_id} initiated human handoff.")
                    await websocket.send_json({
                        "type": "handoff",
                        "status": "initiated",
                        "customer": booking_session.full_name,
                        "phone": booking_session.phone_number,
                        "reason": "Caller requested human agent"
                    })
                    await websocket.send_json({"type": "response", "text": handoff_msg})
                    handoff_audio = await tts_service.synthesize_speech_async(clean_markdown_for_voice(handoff_msg), language=current_lang)
                    if handoff_audio and len(handoff_audio) > 100:
                        await websocket.send_json({"type": "audio", "audio": base64.b64encode(handoff_audio).decode('utf-8')})
                    _log_turn(session_id, transcription, handoff_msg, "transfer request", booking_session)
                    await _handle_post_response_state(websocket, booking_session, session_id, current_lang, recent_ai_utterances)
                    continue

                # -------------------------------------------------------------
                # CALLBACK HANDLING
                # -------------------------------------------------------------
                if intent_name == "callback":
                    cb_phone = booking_session.phone_number or "your number"
                    cb_msg = f"I have registered your callback request for {cb_phone}. Our team will contact you shortly."
                    if current_lang in ("ta", "tanglish", "ta_mix"):
                        cb_msg = f"உங்க callback request register பண்ணியாச்சு. எங்கள் team உங்களை விரைவில் தொடர்புகொள்வார்கள்."
                    elif current_lang == "hi":
                        cb_msg = f"Aapka callback request note kar liya gaya hai. Hamari team aapse jaldi sampark karegi."

                    logger.info(f"[WORKFLOW] [CALLBACK] session_id={session_id} registered callback.")
                    await websocket.send_json({"type": "response", "text": cb_msg})
                    cb_audio = await tts_service.synthesize_speech_async(clean_markdown_for_voice(cb_msg), language=current_lang)
                    if cb_audio and len(cb_audio) > 100:
                        await websocket.send_json({"type": "audio", "audio": base64.b64encode(cb_audio).decode('utf-8')})
                    _log_turn(session_id, transcription, cb_msg, "callback", booking_session)
                    await _handle_post_response_state(websocket, booking_session, session_id, current_lang, recent_ai_utterances)
                    continue

                # -------------------------------------------------------------
                # APPOINTMENT CANCELLATION HANDLING
                # -------------------------------------------------------------
                if intent_name == "appointment_cancellation":
                    from src.services.receptionist_tools import cancel_appointment
                    appt_id_match = re.search(r'\b(?:id|appointment\s+id|appointment\s*#|#)\s*(\d+)\b', transcription, re.IGNORECASE)
                    target_id = int(appt_id_match.group(1)) if appt_id_match else booking_session.appointment_id
                    if target_id:
                        res = cancel_appointment(target_id)
                        cancel_msg = res.get("message", f"Appointment #{target_id} has been cancelled.")
                    else:
                        cancel_msg = "Please provide your appointment ID or registered phone number so I can cancel your booking."

                    logger.info(f"[WORKFLOW] [APPOINTMENT_CANCEL] session_id={session_id} target_id={target_id}")
                    await websocket.send_json({"type": "response", "text": cancel_msg})
                    cancel_audio = await tts_service.synthesize_speech_async(clean_markdown_for_voice(cancel_msg), language=current_lang)
                    if cancel_audio and len(cancel_audio) > 100:
                        await websocket.send_json({"type": "audio", "audio": base64.b64encode(cancel_audio).decode('utf-8')})
                    _log_turn(session_id, transcription, cancel_msg, "appointment_cancellation", booking_session)
                    await _handle_post_response_state(websocket, booking_session, session_id, current_lang, recent_ai_utterances)
                    continue

                # -------------------------------------------------------------
                # APPOINTMENT RESCHEDULING HANDLING
                # -------------------------------------------------------------
                if intent_name == "appointment_rescheduling":
                    from src.services.receptionist_tools import reschedule_appointment
                    appt_id_match = re.search(r'\b(?:id|appointment\s+id|appointment\s*#|#)\s*(\d+)\b', transcription, re.IGNORECASE)
                    target_id = int(appt_id_match.group(1)) if appt_id_match else booking_session.appointment_id

                    # Check if new date and time are provided in the message
                    booking_state_service.extract_slots(transcription, booking_session)
                    if target_id and booking_session.date and booking_session.time:
                        res = reschedule_appointment(target_id, booking_session.date, booking_session.time, org_id)
                        reschedule_msg = res.get("message", f"Appointment #{target_id} rescheduled.")
                    else:
                        reschedule_msg = "Sure, I can help you reschedule. What new date and time would you prefer?"

                    logger.info(f"[WORKFLOW] [APPOINTMENT_RESCHEDULE] session_id={session_id} target_id={target_id}")
                    await websocket.send_json({"type": "response", "text": reschedule_msg})
                    resched_audio = await tts_service.synthesize_speech_async(clean_markdown_for_voice(reschedule_msg), language=current_lang)
                    if resched_audio and len(resched_audio) > 100:
                        await websocket.send_json({"type": "audio", "audio": base64.b64encode(resched_audio).decode('utf-8')})
                    _log_turn(session_id, transcription, reschedule_msg, "appointment_rescheduling", booking_session)
                    await _handle_post_response_state(websocket, booking_session, session_id, current_lang, recent_ai_utterances)
                    continue

                is_ongoing_turn = bool(booking_session.conversation_started or booking_session.greeting_sent or booking_session.turn_count > 0)
                fast_rule_answer = ai_service._fast_rule_response(
                    transcription, intent_name,
                    organization_id=org_id,
                    is_ongoing=is_ongoing_turn,
                    booking_session=booking_session
                )

                if fast_rule_answer:
                    logger.info(f"[AGENT] [FAST PATH ROUTE] Satisfied '{transcription}' in < 50ms without LLM/RAG.")
                    t_tts_start = time.perf_counter()
                    tracker.mark("tts_started_at")

                    # Track for echo suppression
                    norm_ans = _normalize_speech(fast_rule_answer)
                    if norm_ans:
                        recent_ai_utterances.append(norm_ans)
                    last_ai_speech_time = time.time()

                    await websocket.send_json({
                        "type": "response",
                        "text": fast_rule_answer
                    })

                    audio_response = await tts_service.synthesize_speech_async(clean_markdown_for_voice(fast_rule_answer), language=current_lang)
                    if audio_response and len(audio_response) > 100:
                        tracker.mark("first_audio_at")
                        audio_base64 = base64.b64encode(audio_response).decode('utf-8')
                        await websocket.send_json({
                            "type": "audio",
                            "audio": audio_base64
                        })
                    tracker.record("tts_total", (time.perf_counter() - t_tts_start) * 1000.0)
                    tracker.mark("tts_completed_at")

                    telemetry = tracker.log_summary()
                    await websocket.send_json({"type": "telemetry", "data": telemetry})
                    _log_turn(session_id, transcription, fast_rule_answer, intent_name, booking_session)
                    await _handle_post_response_state(websocket, booking_session, session_id, current_lang, recent_ai_utterances)
                    continue

                # Check if intent switched to informational query
                is_intent_switched = booking_state_service.check_and_handle_intent_switch(booking_session, intent_name, transcription)

                # 3. BOOKING STATE MACHINE ROUTING
                if not is_intent_switched and (booking_session.is_active() or intent_name in ("appointment_booking", "appointment_availability")):
                    if intent_name == "appointment_booking" and booking_session.is_completed:
                        booking_session.reset()

                    response_text = booking_state_service.process_booking_turn(transcription, booking_session)

                    norm_ans = _normalize_speech(response_text)
                    if norm_ans:
                        recent_ai_utterances.append(norm_ans)
                    last_ai_speech_time = time.time()

                    await websocket.send_json({
                        "type": "response",
                        "text": response_text
                    })

                    t_tts = time.perf_counter()
                    tracker.mark("tts_started_at")
                    try:
                        audio_response = await tts_service.synthesize_speech_async(clean_markdown_for_voice(response_text), language=current_lang)
                        if audio_response and len(audio_response) > 100:
                            tracker.mark("first_audio_at")
                            audio_base64 = base64.b64encode(audio_response).decode('utf-8')
                            await websocket.send_json({
                                "type": "audio",
                                "audio": audio_base64
                            })
                    except Exception as e:
                        logger.error(f"Booking state TTS generation failed: {e}")

                    tracker.record("tts_total", (time.perf_counter() - t_tts) * 1000.0)
                    tracker.mark("tts_completed_at")
                    telemetry = tracker.log_summary()
                    await websocket.send_json({"type": "telemetry", "data": telemetry})
                    _log_turn(session_id, transcription, response_text, intent_name, booking_session)
                    await _handle_post_response_state(websocket, booking_session, session_id, current_lang, recent_ai_utterances)
                    continue

                # 4. INFORMATIONAL / STREAMING RAG ROUTING (STRICT ACTIVE DOCUMENT ISOLATION)
                booking_session.conversation_started = True
                booking_session.turn_count += 1
                context = ""
                response_text = ""

                if intent_name not in FAST_PATH_INTENTS:
                    t_rag = time.perf_counter()
                    tracker.mark("retrieval_started_at")
                    try:
                        current_doc_id = rag_service._resolve_active_document_id(org_id)
                        rag_task = asyncio.create_task(
                            asyncio.to_thread(
                                rag_service.retrieve_context,
                                transcription, 5, org_id, intent_name, current_doc_id
                            )
                        )
                        context = await rag_task

                        if context:
                            doc_title_to_send = active_doc_title
                            if current_doc_id and org_id is not None:
                                try:
                                    _d = SessionLocal()
                                    doc_obj = _d.query(KnowledgeDocument).filter(
                                        KnowledgeDocument.id == int(current_doc_id),
                                        KnowledgeDocument.organization_id == org_id
                                    ).first()
                                    if doc_obj:
                                        doc_title_to_send = doc_obj.title
                                    _d.close()
                                except Exception:
                                    pass
                            await websocket.send_json({
                                "type": "rag",
                                "source": f"{doc_title_to_send} (ID: {current_doc_id})" if (current_doc_id and doc_title_to_send not in ("None", "No Active Document")) else "Active Knowledge Base"
                            })
                    except Exception as e:
                        logger.error(f"RAG retrieval error: {e}")
                        context = ""
                    tracker.record("retrieval_ms", (time.perf_counter() - t_rag) * 1000.0)
                    tracker.mark("retrieval_completed_at")

                # Live Calendar Verification if availability query
                if intent_name in ("appointment_availability", "appointment_booking"):
                    try:
                        from src.services.calendar_verification_service import calendar_verification_service
                        cal_v = calendar_verification_service.verify_availability(transcription, session_history)
                        if cal_v and cal_v.is_checked:
                            await websocket.send_json({
                                "type": "calendar_verification",
                                "verified": cal_v.is_available,
                                "doctor": cal_v.doctor_name,
                                "date": cal_v.requested_date,
                                "time": cal_v.requested_time,
                                "reason": cal_v.reason,
                                "alternative_slots": cal_v.alternative_slots
                            })
                    except Exception as e:
                        logger.warning(f"Voice WS calendar verification error: {e}")

                # 5. STREAMING LLM & STREAMING TTS PIPELINE
                t_llm_start = time.perf_counter()
                tracker.mark("llm_started_at")
                session_history.append({"role": "user", "text": transcription})
                full_response_text = ""

                first_token_recorded = False
                first_audio_recorded = False

                try:
                    token_stream = ai_service.generate_response_stream(
                        text=transcription,
                        context=context,
                        session_history=session_history,
                        intent=intent_name,
                        emotion=emotion,
                        conversation_language=current_lang,
                        organization_id=org_id,
                        conversation_state=booking_session.get_state_dict()
                    )

                    for token_chunk in token_stream:
                        if not token_chunk:
                            continue

                        if not first_token_recorded:
                            t_first_tok = (time.perf_counter() - t_llm_start) * 1000.0
                            tracker.record("llm_first_token_ms", t_first_tok)
                            tracker.mark("first_token_at")
                            first_token_recorded = True

                        full_response_text += token_chunk
                        # Stream text chunk to UI so user sees real-time visual transcription/typing
                        await websocket.send_json({
                            "type": "text_chunk",
                            "text": token_chunk
                        })

                    response_text = full_response_text.strip()
                    session_history.append({"role": "model", "text": response_text})

                except Exception as e:
                    logger.error(f"LLM streaming pipeline failed: {e}")
                    response_text = "Thank you for contacting our reception desk. How may I assist you?"

                tracker.record("llm_total_ms", (time.perf_counter() - t_llm_start) * 1000.0)
                tracker.mark("llm_completed_at")

                norm_full = _normalize_speech(response_text)
                if norm_full:
                    recent_ai_utterances.append(norm_full)
                    if len(recent_ai_utterances) > 15:
                        recent_ai_utterances = recent_ai_utterances[-15:]
                last_ai_speech_time = time.time()

                # Send final complete text to UI
                await websocket.send_json({
                    "type": "response",
                    "text": response_text
                })

                # Synthesize the complete response as ONE continuous natural audio response via Murf TTS
                t_tts_start = time.perf_counter()
                tracker.mark("tts_started_at")
                clean_voice_text = clean_markdown_for_voice(response_text)
                if clean_voice_text:
                    try:
                        audio_resp = await tts_service.synthesize_speech_async(clean_voice_text, language=current_lang)
                        if audio_resp and len(audio_resp) > 100:
                            tracker.record("tts_total", (time.perf_counter() - t_tts_start) * 1000.0)
                            tracker.record("tts_first_audio_ms", (time.perf_counter() - t_llm_start) * 1000.0)
                            tracker.mark("first_audio_at")
                            tracker.mark("tts_completed_at")
                            audio_b64 = base64.b64encode(audio_resp).decode('utf-8')
                            await websocket.send_json({
                                "type": "audio",
                                "audio": audio_b64,
                                "text": response_text
                            })
                    except Exception as e:
                        logger.error(f"Voice continuous TTS synthesis failed: {e}")

                telemetry = tracker.log_summary()
                await websocket.send_json({"type": "telemetry", "data": telemetry})
                _log_turn(session_id, transcription, response_text, intent_name, booking_session, emotion=emotion)
                await _handle_post_response_state(websocket, booking_session, session_id, current_lang, recent_ai_utterances)

    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError) as e:
        logger.info(f"Voice WebSocket disconnected ({e})")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        try:
            org_id = getattr(booking_session, "organization_id", None) if 'booking_session' in locals() and booking_session else None
            if org_id is not None:
                asyncio.create_task(asyncio.to_thread(agent_engine.generate_session_summary, session_id, org_id))
        except Exception as _e:
            logger.warning(f"Could not trigger post-call summary: {_e}")
        booking_state_service.remove_session(session_id)
