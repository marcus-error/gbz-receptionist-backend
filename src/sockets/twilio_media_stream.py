"""
Twilio <Stream> live AI receptionist pipeline.

Handles:
- Real greeting from active RAG / AppSettings
- Twilio Media Stream audio (inbound track only) -> Speech-to-Text
- Intent detection & parallel emotion analysis
- Strict session-bound Active RAG retrieval
- Gemini AI response generation & streaming
- ElevenLabs TTS (Twilio-compatible mulaw/8000)
- Live conversation history & Socket.IO live UI updates
- Automatic goodbye + Twilio hangup
"""

import asyncio
import base64
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
try:
    from twilio.rest import Client as TwilioClient
except ImportError:
    TwilioClient = None

from src.config.database import SessionLocal
from src.models.call_log import CallLog
from src.models.settings import AppSettings
from src.models.knowledge_doc import KnowledgeDocument
from src.services.ai_service import ai_service
from src.services.rag_service import rag_service
from src.services.stt_service import stt_service
from src.services.nlp_service import nlp_service
from src.services.emotion_service import emotion_service
from src.services.tts_service import tts_service
from src.services.call_service import CallService
from src.services.booking_state_service import booking_state_service, NextAction, BookingState, BookingSession
from src.sockets.socket_manager import sio
from src.utils.latency_tracker import LatencyTracker
import time

router = APIRouter()
logger = logging.getLogger(__name__)


async def _handle_twilio_post_response(websocket: WebSocket, stream_sid: str, call_sid: str, booking_session: BookingSession):
    """
    Evaluates conversation state after TTS output completes on Twilio stream,
    emits state over WebSocket/Socket.IO, and executes automatic actions if ready.
    """
    logger.info(
        f"\n[STATE]\nprevious=RESPONDING\ncurrent=TTS\n"
        f"[TTS]\nstarted\ncompleted"
    )
    post_eval = booking_state_service.evaluate_post_response(booking_session)
    logger.info(
        f"\n[POST_RESPONSE_CHECK]\n"
        f"conversation_id={booking_session.conversation_id}\n"
        f"call_sid={call_sid}\n"
        f"intent={booking_session.current_intent}\n"
        f"stage={booking_session.current_stage}\n"
        f"missing_fields={booking_session.missing_fields}\n"
        f"[NEXT_ACTION]\n"
        f"{post_eval['next_action']}"
    )
    await _emit("live_call_update", {
        "call_sid": call_sid,
        "type": "state",
        "state": post_eval["state"],
        "next_action": post_eval["next_action"],
        "stage": booking_session.current_stage,
        "missing_fields": booking_session.missing_fields
    })

    if post_eval["next_action"] == NextAction.AUTOMATIC_ACTION_REQUIRED:
        logger.info(f"[TWILIO AUTOMATIC ACTION] Triggering auto-execution for call {call_sid}")
        auto_response = booking_state_service.process_booking_turn("", booking_session)
        if auto_response:
            sess = sessions.get(stream_sid)
            if sess:
                sess["messages"].append({
                    "role": "assistant",
                    "text": auto_response,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            await _emit("live_call_update", {
                "call_sid": call_sid,
                "type": "ai_response",
                "text": auto_response,
                "history": sess["messages"] if sess else [],
                "intent": booking_session.current_intent,
            })
            await _play_text(websocket, stream_sid, auto_response)
            next_eval = booking_state_service.evaluate_post_response(booking_session)
            await _emit("live_call_update", {
                "call_sid": call_sid,
                "type": "state",
                "state": next_eval["state"],
                "next_action": next_eval["next_action"],
                "stage": booking_session.current_stage,
                "missing_fields": booking_session.missing_fields
            })
    elif post_eval["next_action"] in (NextAction.USER_INPUT_REQUIRED, NextAction.CONFIRMATION_REQUIRED):
        logger.info(f"\n[LISTENING]\nwaiting_for_caller=true")

# Twilio sends 20 ms media packets at 8 kHz, 8-bit μ-law mono = 160 bytes/packet.
SAMPLE_RATE = 8000
BYTES_PER_SECOND = SAMPLE_RATE
UTTERANCE_SECONDS = 1.5  # Reduced to 1.5s for faster speech responsiveness
UTTERANCE_BYTES = int(BYTES_PER_SECOND * UTTERANCE_SECONDS)

sessions = {}


def _settings():
    db = SessionLocal()
    try:
        row = db.query(AppSettings).first()
        return row
    finally:
        db.close()


def _save_call(call_sid: str, values: dict):
    db = SessionLocal()
    try:
        CallService(db).update_from_twilio(call_sid, values)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not save call update")
    finally:
        db.close()


async def _emit(event: str, payload: dict):
    try:
        await sio.emit(event, payload)
    except Exception:
        logger.exception("Socket event failed: %s", event)


def _twilio_client():
    s = _settings()
    if not s or not s.twilio_account_sid or not s.twilio_auth_token:
        return None
    return TwilioClient(s.twilio_account_sid, s.twilio_auth_token)


def _twilio_hangup(call_sid: str):
    client = _twilio_client()
    if not client:
        return
    try:
        client.calls(call_sid).update(status="completed")
    except Exception:
        logger.exception("Twilio hangup failed for %s", call_sid)


def _calculate_frame_rms(ulaw_frame: bytes) -> int:
    """Calculate RMS volume of an 8kHz 8-bit mulaw audio frame."""
    if not ulaw_frame:
        return 0
    try:
        import audioop
        pcm = audioop.ulaw2lin(ulaw_frame, 2)
        return audioop.rms(pcm, 2)
    except Exception:
        total = 0
        for b in ulaw_frame:
            val = abs((b ^ 0xFF) - 128)
            total += val * val
        return int((total / len(ulaw_frame)) ** 0.5) * 8


async def _play_text(websocket: WebSocket, stream_sid: str, text: str):
    if not text or not text.strip():
        return
    if stream_sid in sessions:
        sessions[stream_sid]["is_ai_speaking"] = True
    try:
        try:
            audio = await tts_service.synthesize_ulaw_async(text)
        except Exception:
            # Fallback to direct synchronous call in thread
            from src.sockets.twilio_media_stream import _tts_ulaw_8000
            audio = await asyncio.to_thread(_tts_ulaw_8000, text)

        # Twilio accepts base64 μ-law/8000 audio in media events.
        chunk_size = 160
        for i in range(0, len(audio), chunk_size):
            payload = base64.b64encode(audio[i:i + chunk_size]).decode("ascii")
            await websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": payload},
            }))

        await _emit("ai_audio_playing", {
            "call_sid": sessions.get(stream_sid, {}).get("call_sid"),
            "text": text,
        })
    finally:
        if stream_sid in sessions:
            sessions[stream_sid]["is_ai_speaking"] = False
            sessions[stream_sid]["last_ai_speech_time"] = time.time()


def _tts_ulaw_8000(text: str) -> bytes:
    import requests
    from src.config.env import settings

    if not settings.ELEVENLABS_API_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY is not configured")

    app = _settings()
    voice_id = getattr(app, "voice_selection", None) or settings.ELEVENLABS_VOICE_ID
    model_id = getattr(app, "tts_model_id", None) or settings.ELEVENLABS_MODEL_ID
    speech_speed = getattr(app, "speech_speed", None)
    try:
        speech_speed = float(speech_speed) if speech_speed is not None else 1.0
    except (TypeError, ValueError):
        speech_speed = 1.0
    speech_speed = max(0.5, min(2.0, speech_speed))
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"

    response = requests.post(
        url,
        params={"output_format": "ulaw_8000"},
        headers={
            "xi-api-key": settings.ELEVENLABS_API_KEY,
            "accept": "audio/basic",
            "content-type": "application/json",
        },
        json={
            "text": text,
            "model_id": model_id,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.5, "speed": speech_speed},
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"ElevenLabs TTS failed: {response.status_code} {response.text}"
        )
    return response.content


async def _process_utterance(websocket: WebSocket, stream_sid: str, audio_bytes: bytes):
    session = sessions.get(stream_sid)
    if not session:
        return

    call_sid = session["call_sid"]
    active_doc_id = session.get("active_doc_id")
    tracker = LatencyTracker(call_sid=call_sid)

    # Wrap Twilio μ-law in WAV container
    import io
    import wave

    wav = io.BytesIO()
    with wave.open(wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio_bytes)
    wav.seek(0)
    wav_bytes = wav.read()

    # 1. Local STT
    t0 = time.perf_counter()
    try:
        transcription = await stt_service.transcribe_async(wav_bytes)
    except Exception:
        logger.exception("STT failed")
        return
    tracker.record("stt", (time.perf_counter() - t0) * 1000.0)

    if not transcription or transcription.strip().lower() in {
        "demo transcription", "none", "null", ""
    }:
        return

    logger.info(
        f"[CALLER SPEECH VERIFIED]\n"
        f"stream_sid={stream_sid}\n"
        f"call_sid={call_sid}\n"
        f"transcript='{transcription}' (len={len(transcription)})\n"
        f"language={session.get('language', 'en')}"
    )

    session["messages"].append({
        "role": "user",
        "text": transcription,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    await _emit("live_call_update", {
        "call_sid": call_sid,
        "type": "transcript",
        "speaker": "caller",
        "text": transcription,
        "history": session["messages"],
    })

    # 2. CONCURRENT NLP + EMOTION
    t0 = time.perf_counter()
    nlp_task = nlp_service.detect_intent_async(transcription)
    emotion_task = emotion_service.detect_emotion_async(transcription)

    nlp_res, emotion_res = await asyncio.gather(nlp_task, emotion_task)
    intent_name = nlp_res.intent
    emotion = f"{emotion_res.emotion.title()} - {int(emotion_res.confidence * 100)}%"
    tracker.record("nlp_emotion_parallel", (time.perf_counter() - t0) * 1000.0)

    await _emit("live_call_update", {
        "call_sid": call_sid,
        "type": "analysis",
        "intent": intent_name,
        "emotion": emotion,
    })

    # BOOKING STATE MACHINE ROUTING
    session_id = f"twilio_{stream_sid}"
    booking_session = booking_state_service.get_session(session_id)
    if not booking_session.contact_number or booking_session.contact_number == "Unknown":
        db_check = SessionLocal()
        try:
            call_rec = CallService(db_check).repo.get_by_sid(call_sid)
            if call_rec and call_rec.caller_phone and call_rec.caller_phone != "Unknown":
                booking_session.contact_number = call_rec.caller_phone
        except Exception:
            pass
        finally:
            db_check.close()

    is_intent_switched = booking_state_service.check_and_handle_intent_switch(booking_session, intent_name, transcription)

    if not is_intent_switched and (booking_session.is_active() or intent_name in ("appointment_booking", "appointment_availability")):
        if intent_name == "appointment_booking" and booking_session.is_completed:
            booking_session.reset()

        response_text = booking_state_service.process_booking_turn(transcription, booking_session)

        session["messages"].append({
            "role": "assistant",
            "text": response_text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        await _emit("live_call_update", {
            "call_sid": call_sid,
            "type": "ai_response",
            "text": response_text,
            "history": session["messages"],
            "intent": intent_name,
        })

        _save_call(call_sid, {
            "caller_said": transcription,
            "ai_response": response_text,
            "intent": intent_name,
            "emotion": emotion,
            "messages": json.dumps(session["messages"]),
            "receptionist_state": f"BOOKING TURN -> {booking_session.state}",
        })

        db = SessionLocal()
        try:
            CallService(db).log_conversation_message(
                session_id=call_sid,
                role="user",
                message=transcription,
                intent=intent_name
            )
            CallService(db).log_conversation_message(
                session_id=call_sid,
                role="assistant",
                message=response_text,
                intent=intent_name,
                doctor_name=booking_session.doctor,
                appointment_date=booking_session.date,
                appointment_time=booking_session.time,
                availability_verified=booking_session.availability_verified,
                booking_confirmed=booking_session.booking_confirmed,
                appointment_id=booking_session.appointment_id
            )
        finally:
            db.close()

        try:
            await _play_text(websocket, stream_sid, response_text)
            await _handle_twilio_post_response(websocket, stream_sid, call_sid, booking_session)
        except Exception:
            logger.exception("TTS/playback failed")
        tracker.log_summary()
        return

    booking_session.conversation_started = True
    booking_session.turn_count += 1
    context = ""
    sources = []
    fast_intents = {"EMERGENCY", "MEDICAL_ADVICE", "GREETING", "GOODBYE", "THANK_YOU", "CONFIRMATION", "REJECTION", "YES", "NO"}

    # 3. STRICT ACTIVE RAG RETRIEVAL
    if intent_name not in fast_intents:
        t_rag = time.perf_counter()
        tracker.mark("retrieval_started_at")
        try:
            org_id = session.get("organization_id")
            active_doc_id = session.get("active_doc_id")
            context = await asyncio.to_thread(
                rag_service.retrieve_context,
                transcription,
                5,
                org_id,
                intent_name,
                active_doc_id
            )
            sources = [f"{session.get('active_doc_title', 'Knowledge Document')} (ID: {active_doc_id})"] if (context and active_doc_id) else []
        except Exception as e:
            logger.error(f"Twilio RAG retrieval error: {e}")
        tracker.record("retrieval_ms", (time.perf_counter() - t_rag) * 1000.0)
        tracker.mark("retrieval_completed_at")

        if sources:
            await _emit("live_call_update", {
                "call_sid": call_sid,
                "type": "rag",
                "sources": sources,
            })

    # Live Calendar & DB Verification Socket Notification
    try:
        from src.services.calendar_verification_service import calendar_verification_service
        cal_verif = calendar_verification_service.verify_availability(transcription, session["messages"])
        if cal_verif and cal_verif.is_checked:
            await _emit("live_call_update", {
                "call_sid": call_sid,
                "type": "calendar_verification",
                "verified": cal_verif.is_available,
                "doctor": cal_verif.doctor_name,
                "date": cal_verif.requested_date,
                "time": cal_verif.requested_time,
                "reason": cal_verif.reason,
                "alternative_slots": cal_verif.alternative_slots,
            })
    except Exception as exc:
        logger.warning(f"Calendar verification socket emit warning: {exc}")

    # 4. STREAMING LLM & STREAMING TTS
    t_llm_start = time.perf_counter()
    first_token_recorded = False
    first_audio_recorded = False
    full_response_text = ""

    try:
        stream = ai_service.generate_response_stream(
            text=transcription,
            context=context,
            session_history=session["messages"],
            intent=intent_name,
            emotion=emotion,
            conversation_state=booking_session.get_state_dict()
        )
        for chunk in stream:
            if not chunk:
                continue
            if not first_token_recorded:
                tracker.record("llm_first_token", (time.perf_counter() - t_llm_start) * 1000.0)
                first_token_recorded = True

            full_response_text += chunk

        if full_response_text.strip():
            t_tts = time.perf_counter()
            try:
                await _play_text(websocket, stream_sid, full_response_text.strip())
                if not first_audio_recorded:
                    tracker.record("tts_first_audio", (time.perf_counter() - t_tts) * 1000.0)
                    first_audio_recorded = True
            except Exception:
                logger.exception("TTS playback failed")

    except Exception:
        logger.exception("AI response generation failed")
        full_response_text = "I'm sorry, I had trouble processing that. Could you please repeat it?"
        await _play_text(websocket, stream_sid, full_response_text)

    response_text = full_response_text.strip()
    tracker.record("llm_total", (time.perf_counter() - t_llm_start) * 1000.0)

    session["messages"].append({
        "role": "assistant",
        "text": response_text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    await _emit("live_call_update", {
        "call_sid": call_sid,
        "type": "ai_response",
        "text": response_text,
        "history": session["messages"],
        "intent": intent_name,
        "rag_sources": sources,
    })

    _save_call(call_sid, {
        "caller_said": transcription,
        "ai_response": response_text,
        "intent": intent_name,
        "emotion": emotion,
        "rag_source": ", ".join(sources) if sources else None,
        "messages": json.dumps(session["messages"]),
        "receptionist_state": "STT → NLP/Emotion(Parallel) → Active RAG → Stream LLM → Stream TTS",
    })

    tracker.log_summary()

    # Goodbye intent
    goodbye = (
        intent_name == "GOODBYE"
        or transcription.strip().lower() in {
            "bye", "goodbye", "thank you bye", "thanks bye"
        }
    )

    if not goodbye:
        await _handle_twilio_post_response(websocket, stream_sid, call_sid, booking_session)

    if goodbye:
        settings = _settings()
        goodbye_text = (
            getattr(settings, "goodbye_message", None)
            if settings else None
        ) or "Thank you for calling. Goodbye!"

        session["messages"].append({
            "role": "assistant",
            "text": goodbye_text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        await _emit("live_call_update", {
            "call_sid": call_sid,
            "type": "goodbye",
            "text": goodbye_text,
        })

        try:
            await _play_text(websocket, stream_sid, goodbye_text)
        finally:
            _save_call(call_sid, {
                "status": "completed",
                "messages": json.dumps(session["messages"]),
                "receptionist_state": "GOODBYE → CALL TERMINATED",
            })
            await asyncio.to_thread(_twilio_hangup, call_sid)
        return


@router.websocket("/ws/twilio-stream")
async def twilio_voice_stream(websocket: WebSocket):
    await websocket.accept()

    stream_sid: Optional[str] = None
    
    # Non-blocking VAD audio buffer state (8 kHz μ-law 160 bytes = 20ms)
    FRAME_SIZE = 160
    ENERGY_THRESHOLD = 350
    SILENCE_FRAMES_THRESHOLD = 28  # ~560ms of silence ends speech
    MIN_SPEECH_FRAMES = 12         # ~240ms minimum speech length
    MAX_SPEECH_FRAMES = 400        # ~8.0s maximum utterance buffer before forced STT

    pre_speech_buffer = deque(maxlen=10) # 200ms pre-speech rolling window
    speech_buffer = bytearray()
    in_speech = False
    silence_count = 0

    try:
        while True:
            message = json.loads(await websocket.receive_text())
            event = message.get("event")

            if event == "start":
                start = message.get("start", {})
                stream_sid = start.get("streamSid")
                call_sid = start.get("callSid") or start.get("customParameters", {}).get("callSid")

                if not call_sid:
                    call_sid = start.get("call_sid", "unknown")

                # Resolve tenant organization from custom parameters, query params, or call_log
                custom_params = start.get("customParameters", {})
                resolved_org_id = None
                if custom_params.get("organization_id"):
                    try:
                        resolved_org_id = int(custom_params.get("organization_id"))
                    except (ValueError, TypeError):
                        resolved_org_id = None
                if resolved_org_id is None and websocket.query_params.get("organization_id"):
                    try:
                        resolved_org_id = int(websocket.query_params.get("organization_id"))
                    except (ValueError, TypeError):
                        resolved_org_id = None

                db = SessionLocal()
                try:
                    if resolved_org_id is None and call_sid and call_sid != "unknown":
                        from src.models.call_log import CallLog
                        c_log = db.query(CallLog).filter(CallLog.call_sid == call_sid).first()
                        if c_log and c_log.organization_id:
                            resolved_org_id = c_log.organization_id

                    active_doc_id: Optional[str] = rag_service._resolve_active_document_id(resolved_org_id) if resolved_org_id is not None else None
                    active_doc_title: str = "No Active Document"
                    if active_doc_id and resolved_org_id is not None:
                        from src.models.knowledge_doc import KnowledgeDocStatus
                        active_doc = db.query(KnowledgeDocument).filter(
                            KnowledgeDocument.id == int(active_doc_id),
                            KnowledgeDocument.organization_id == resolved_org_id,
                            KnowledgeDocument.ai_usage == "Active",
                            KnowledgeDocument.status.in_([KnowledgeDocStatus.INDEXED, "Indexed"])
                        ).first()
                        if active_doc:
                            active_doc_title = active_doc.title
                        else:
                            active_doc_id = None
                finally:
                    db.close()

                greeting = rag_service.get_organization_greeting(resolved_org_id, document_id=active_doc_id)

                sessions[stream_sid] = {
                    "call_sid": call_sid,
                    "organization_id": resolved_org_id,
                    "active_doc_id": active_doc_id,
                    "active_doc_title": active_doc_title,
                    "messages": [],
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "is_ai_speaking": False,
                    "is_processing": False,
                    "last_ai_speech_time": 0.0,
                }

                await _emit("live_call_started", {
                    "call_sid": call_sid,
                    "stream_sid": stream_sid,
                    "status": "connected",
                    "active_rag": active_doc_title,
                })

                _save_call(call_sid, {
                    "status": "in-progress",
                    "messages": json.dumps([]),
                    "receptionist_state": f"CALL STARTED → ACTIVE RAG ({active_doc_title})",
                })

                sessions[stream_sid]["messages"].append({
                    "role": "assistant",
                    "text": greeting,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

                await _emit("live_call_update", {
                    "call_sid": call_sid,
                    "type": "greeting",
                    "text": greeting,
                })

                await _play_text(websocket, stream_sid, greeting)
                booking_sess = booking_state_service.get_session(f"twilio_{stream_sid}", call_sid=call_sid)
                booking_sess.greeting_sent = True
                booking_sess.conversation_started = True
                await _handle_twilio_post_response(websocket, stream_sid, call_sid, booking_sess)

            elif event == "media":
                media_obj = message.get("media", {})
                track = media_obj.get("track")
                # CRITICAL: Inbound caller audio ONLY (never process outbound AI speech as user input)
                if track and track != "inbound":
                    continue

                payload = media_obj.get("payload")
                if not payload or not stream_sid:
                    continue

                raw_frame = base64.b64decode(payload)
                sess = sessions.get(stream_sid)
                if not sess:
                    continue

                # Anti-loopback guard: Discard incoming audio during AI speech
                if sess.get("is_ai_speaking"):
                    continue

                # Calculate frame volume
                rms = _calculate_frame_rms(raw_frame)

                if not in_speech:
                    if rms >= ENERGY_THRESHOLD:
                        in_speech = True
                        silence_count = 0
                        speech_buffer.clear()
                        for f in pre_speech_buffer:
                            speech_buffer.extend(f)
                        speech_buffer.extend(raw_frame)
                    else:
                        pre_speech_buffer.append(raw_frame)
                else:
                    speech_buffer.extend(raw_frame)
                    if rms >= ENERGY_THRESHOLD:
                        silence_count = 0
                    else:
                        silence_count += 1

                    # Trigger STT on detected end of speech or max buffer reached
                    speech_ended = (silence_count >= SILENCE_FRAMES_THRESHOLD)
                    max_reached = (len(speech_buffer) >= MAX_SPEECH_FRAMES * FRAME_SIZE)

                    if speech_ended or max_reached:
                        if len(speech_buffer) >= MIN_SPEECH_FRAMES * FRAME_SIZE:
                            utterance_bytes = bytes(speech_buffer)
                            # Non-blocking async task execution preserves stream responsiveness
                            asyncio.create_task(_process_utterance(websocket, stream_sid, utterance_bytes))
                        speech_buffer.clear()
                        in_speech = False
                        silence_count = 0

            elif event == "stop":
                if stream_sid and stream_sid in sessions:
                    call_sid = sessions[stream_sid]["call_sid"]
                    _save_call(call_sid, {
                        "status": "completed",
                        "messages": json.dumps(sessions[stream_sid]["messages"]),
                        "receptionist_state": "CALL ENDED",
                    })
                    await _emit("live_call_ended", {
                        "call_sid": call_sid,
                        "status": "completed",
                        "history": sessions[stream_sid]["messages"],
                    })
                    sessions.pop(stream_sid, None)
                break

    except (WebSocketDisconnect, asyncio.CancelledError):
        logger.info("Twilio stream disconnected: %s", stream_sid)
    except Exception:
        logger.exception("Twilio stream error: %s", stream_sid)
    finally:
        if stream_sid:
            booking_state_service.remove_session(f"twilio_{stream_sid}")
            session = sessions.pop(stream_sid, None)
            if session:
                _save_call(session["call_sid"], {
                    "status": "completed",
                    "messages": json.dumps(session["messages"]),
                    "receptionist_state": "STREAM DISCONNECTED",
                })

