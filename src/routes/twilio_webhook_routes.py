import json
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response, Depends, HTTPException
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.services.call_service import CallService
from src.sockets.socket_manager import sio
from src.models.call_log import CallType

router = APIRouter(prefix="/webhooks/twilio", tags=["Twilio Webhooks"])


def _iso_now():
    return datetime.now(timezone.utc).isoformat()


async def _emit_call_update(event: str, payload: dict):
    """Broadcast a real-time call event to all connected receptionist UIs."""
    await sio.emit(event, payload)
    # Keep the older event name for existing frontend listeners.
    if event == "incoming-call":
        await sio.emit("incoming_call", payload)


@router.post("/incoming")
async def incoming_call(request: Request, db: Session = Depends(get_db)):
    """Twilio incoming-call webhook: create a call session and start the media stream."""
    form_data = await request.form()

    call_sid = form_data.get("CallSid")
    caller_phone = form_data.get("From", "Unknown")
    caller_name = form_data.get("CallerName") or "Unknown"

    if not call_sid:
        raise HTTPException(status_code=400, detail="Twilio CallSid is required")

    call_service = CallService(db)
    existing = call_service.repo.get_by_sid(call_sid)

    if existing:
        call = existing
    else:
        # This database record is the live call session.
        call = call_service.create_from_twilio({
            "call_sid": call_sid,
            "caller_phone": caller_phone,
            "caller_name": caller_name,
            "call_type": CallType.INBOUND,
            "status": "ringing",
            "duration": "00:00",
            "messages": "[]",
            "receptionist_state": "incoming-call",
        })

    event_payload = {
        "call_id": call.id,
        "call_sid": call_sid,
        "caller_phone": caller_phone,
        "caller_name": caller_name,
        "status": "ringing",
        "event": "incoming-call",
        "timestamp": _iso_now(),
    }
    await _emit_call_update("incoming-call", event_payload)

    host = request.headers.get("host")
    if not host:
        raise HTTPException(status_code=400, detail="Request host is required")

    # Start Twilio recording and stream live audio to the existing media-stream endpoint.
    twiml_response = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Start>
        <Recording
            recordingStatusCallback="https://{host}/api/webhooks/twilio/recording"
            recordingStatusCallbackMethod="POST"
            recordingStatusCallbackEvent="completed" />
    </Start>
    <Say>Connecting you to the AI receptionist.</Say>
    <Connect>
        <Stream url="wss://{host}/api/ws/twilio-stream" />
    </Connect>
</Response>'''
    return Response(content=twiml_response, media_type="application/xml")


@router.post("/status")
async def call_status(request: Request, db: Session = Depends(get_db)):
    """Persist Twilio lifecycle status and broadcast the change to the Live Call UI."""
    form_data = await request.form()
    call_sid = form_data.get("CallSid")
    call_status = form_data.get("CallStatus")
    duration = form_data.get("CallDuration")

    if not call_sid:
        raise HTTPException(status_code=400, detail="Twilio CallSid is required")

    call_service = CallService(db)
    update_data = {"status": call_status or "unknown"}
    if duration:
        total_seconds = int(duration)
        update_data["duration"] = f"{total_seconds // 60:02d}:{total_seconds % 60:02d}"

    call = call_service.update_from_twilio(call_sid, update_data)

    await sio.emit("call_status_update", {
        "call_id": call.id if call else None,
        "call_sid": call_sid,
        "status": update_data["status"],
        "duration": update_data.get("duration", "00:00"),
        "timestamp": _iso_now(),
    })
    return {"status": "success"}


@router.post("/transcript")
async def save_transcript(request: Request, db: Session = Depends(get_db)):
    """Save a transcript/message and broadcast the new Live Call transcript entry."""
    body = await request.json()
    call_sid = body.get("call_sid") or body.get("CallSid")
    if not call_sid:
        raise HTTPException(status_code=400, detail="call_sid is required")

    message = {
        "timestamp": body.get("timestamp") or _iso_now(),
        "speaker": body.get("speaker") or body.get("role") or "user",
        "text": body.get("text") or body.get("transcript") or "",
    }

    call_service = CallService(db)
    call = call_service.repo.get_by_sid(call_sid)
    if not call:
        raise HTTPException(status_code=404, detail="Call session not found")

    try:
        messages = json.loads(call.messages or "[]")
        if not isinstance(messages, list):
            messages = []
    except (TypeError, json.JSONDecodeError):
        messages = []

    messages.append(message)
    update = {
        "messages": json.dumps(messages),
        # Keep the plain transcript field useful to existing UI/API consumers.
        "caller_said": "\n".join(
            f"{m.get('speaker', 'user')}: {m.get('text', '')}" for m in messages
        ),
    }
    updated = call_service.update_from_twilio(call_sid, update)

    await sio.emit("transcript_update", {
        "call_id": updated.id,
        "call_sid": call_sid,
        "message": message,
        "messages": messages,
        "timestamp": _iso_now(),
    })
    return {"status": "success", "call_id": updated.id, "messages": messages}


@router.post("/recording")
async def call_recording(request: Request, db: Session = Depends(get_db)):
    """Persist the completed Twilio recording URL and notify the Live Call UI."""
    form_data = await request.form()
    call_sid = form_data.get("CallSid")
    recording_url = form_data.get("RecordingUrl")
    recording_status = form_data.get("RecordingStatus")

    if not call_sid:
        raise HTTPException(status_code=400, detail="Twilio CallSid is required")

    call_service = CallService(db)
    call = call_service.update_from_twilio(call_sid, {
        "recording_url": recording_url,
    })

    await sio.emit("recording_update", {
        "call_id": call.id if call else None,
        "call_sid": call_sid,
        "recording_url": recording_url,
        "recording_status": recording_status or "completed",
        "timestamp": _iso_now(),
    })
    return {"status": "success"}
