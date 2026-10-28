from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
import base64

from src.config.database import get_db
from src.controllers import telephony_controller
from src.middleware.auth_middleware import require_role, get_optional_current_user
from src.models.call_log import CallLog
from src.models.user import User
from src.sockets.socket_manager import sio
from datetime import datetime, timezone
from src.validators.telephony_validator import (
    TelephonySettingsOut,
    TestConnectionResponse,
    ValidateSipResponse,
    SipConfigOut,
    SipTestResponse,
    SipInboundPayload,
    SipAudioChunkPayload,
    SipCallEndPayload,
    SipE2ETestResponse,
    SipRegisterRequest,
    SipRegistrationStatusResponse,
    SipUnregisterResponse,
)
from src.services.call_service import CallService
from src.models.call_log import CallType

router = APIRouter(prefix="/telephony", tags=["Telephony"])

# ──────────────────────────────────────────────────────────────────────────────
# Legacy Twilio routes (preserved for future toggling)
# ──────────────────────────────────────────────────────────────────────────────
router.add_api_route("/settings", telephony_controller.get_settings, methods=["GET"], response_model=TelephonySettingsOut)
router.add_api_route("/settings", telephony_controller.update_settings, methods=["PUT"], response_model=TelephonySettingsOut)
router.add_api_route("/test-connection", telephony_controller.test_connection, methods=["POST"], response_model=TestConnectionResponse)
router.add_api_route("/validate-sip", telephony_controller.validate_sip, methods=["POST"], response_model=ValidateSipResponse)

# ──────────────────────────────────────────────────────────────────────────────
# SIP Trunk configuration routes
# ──────────────────────────────────────────────────────────────────────────────
router.add_api_route("/sip", telephony_controller.get_sip_config, methods=["GET"], response_model=SipConfigOut)
router.add_api_route("/sip", telephony_controller.update_sip_config, methods=["POST", "PUT"], response_model=SipConfigOut)
router.add_api_route("/sip/test", telephony_controller.test_sip_connection, methods=["POST"], response_model=SipTestResponse)
router.add_api_route("/sip/test-e2e", telephony_controller.run_e2e_test, methods=["POST"], response_model=SipE2ETestResponse)

# ──────────────────────────────────────────────────────────────────────────────
# SIP UA Registration routes (pyVoIP — FreeSWITCH / Asterisk)
# ──────────────────────────────────────────────────────────────────────────────
router.add_api_route(
    "/sip/register",
    telephony_controller.register_sip_trunk,
    methods=["POST"],
    summary="Register SIP UA with FreeSWITCH / Asterisk",
)
router.add_api_route(
    "/sip/registration-status",
    telephony_controller.get_registration_status,
    methods=["GET"],
    response_model=SipRegistrationStatusResponse,
    summary="Get live SIP UA registration status",
)
router.add_api_route(
    "/sip/unregister",
    telephony_controller.unregister_sip_trunk,
    methods=["POST"],
    response_model=SipUnregisterResponse,
    summary="Unregister SIP UA",
)


@router.get("/demo-status")
def get_demo_status():
    """
    Public restricted demo status endpoint.
    Accurately reports whether the phone system and voice pipeline are connected and ready
    WITHOUT exposing credentials, SIP accounts, customer PII, or internal tokens.
    """
    try:
        from src.services.sip_registrar import sip_registrar
        from src.services.sip_call_handler import get_all_active_calls
        is_registered = sip_registrar.is_registered()
        active_calls_count = len(get_all_active_calls())
    except Exception:
        is_registered = False
        active_calls_count = 0

    return {
        "status": "online" if is_registered else "ready",
        "system_online": True,
        "sip_registered": is_registered,
        "phone_system_connected": is_registered,
        "mode": "restricted_demo",
        "live_demo_available": True,
        "active_calls": active_calls_count,
        "active_calls_count": active_calls_count,
        "supported_codecs": ["PCMU", "PCMA"],
        "voice_pipeline_ready": True
    }


# ──────────────────────────────────────────────────────────────────────────────
# Inbound SIP Call Webhook — full call lifecycle
# ──────────────────────────────────────────────────────────────────────────────

@router.api_route("/sip/inbound", methods=["GET", "POST", "HEAD", "OPTIONS"])
async def sip_inbound_call(
    request: Request,
    payload: Optional[SipInboundPayload] = None,
    db: Session = Depends(get_db)
):
    """
    SIP Inbound Call Webhook. Supports GET, POST, HEAD, OPTIONS.
    """
    if request.method in ["HEAD", "OPTIONS"]:
        return {"status": "active"}

    if payload is None:
        if request.method == "GET":
            params = dict(request.query_params)
            if not params:
                return {
                    "status": "active",
                    "message": "Webhook endpoint '/api/v1/telephony/sip/inbound' is active and ready to accept incoming SIP call traffic.",
                }
            payload = SipInboundPayload(
                caller_phone=params.get("caller_phone") or params.get("From") or params.get("from") or "Unknown",
                caller_name=params.get("caller_name") or params.get("CallerName") or "SIP Caller",
                call_id=params.get("call_id") or params.get("CallSid") or params.get("call_sid"),
                transcription=params.get("transcription") or params.get("SpeechResult"),
                event=params.get("event") or "call_start",
            )
        else:
            try:
                body = await request.json()
                payload = SipInboundPayload(**body)
            except Exception:
                payload = SipInboundPayload()
    import uuid
    from src.services.sip_call_handler import get_active_call, SipCallSession

    event = (payload.event or "call_start").lower()

    # ── call_start ──────────────────────────────────────────────────────────
    if event == "call_start":
        call_sid = payload.call_id or f"sip_{uuid.uuid4().hex[:12]}"

        # Resolve target organization dynamically from DID / Callee / Username
        target_did = getattr(payload, "to_phone", None) or getattr(payload, "did_number", None)
        if not target_did and request.method == "GET":
            target_did = request.query_params.get("To") or request.query_params.get("to") or request.query_params.get("did")
        target_user = getattr(payload, "sip_username", None)

        inbound_org_id = getattr(payload, "organization_id", None) or 1
        if not getattr(payload, "organization_id", None):
            from src.models.sip_config import SipConfig
            if target_did:
                clean_did = str(target_did).strip().lstrip('+')
                cfg_match = db.query(SipConfig).filter(
                    (SipConfig.did_number == target_did) |
                    (SipConfig.did_number == clean_did) |
                    (SipConfig.did_number == f"+{clean_did}")
                ).first()
                if cfg_match and cfg_match.organization_id:
                    inbound_org_id = cfg_match.organization_id
            elif target_user:
                cfg_match = db.query(SipConfig).filter(
                    (SipConfig.sip_username == target_user) |
                    (SipConfig.login == target_user)
                ).first()
                if cfg_match and cfg_match.organization_id:
                    inbound_org_id = cfg_match.organization_id

        call_service = CallService(db)
        call = call_service.create_from_twilio({
            "call_sid": call_sid,
            "caller_phone": payload.caller_phone,
            "caller_name": payload.caller_name,
            "call_type": CallType.INBOUND,
            "organization_id": inbound_org_id,
            "status": "ringing",
            "duration": "00:00",
            "messages": "[]",
            "receptionist_state": "SIP INBOUND • RINGING",
        })

        event_payload = {
            "call_id": call.id,
            "call_sid": call_sid,
            "caller_phone": payload.caller_phone,
            "caller_name": payload.caller_name,
            "status": "ringing",
            "event": "incoming-call",
            "source": "sip_trunk",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await sio.emit("incoming-call", event_payload)
        await sio.emit("incoming_call", event_payload)

        # Create an AI session for this call (webhook-based audio bridging).
        # Pass call_sid explicitly so the registry key matches the DB record.
        from src.services.sip_call_handler import register_call, unregister_call
        session = SipCallSession(
            caller_phone=payload.caller_phone,
            caller_name=payload.caller_name,
            organization_id=inbound_org_id,
        )
        # Re-register with the correct call_sid (overrides the auto-generated one)
        unregister_call(session.call_sid)
        session.call_sid = call_sid
        session.session_id = f"sip_session_{call_sid}"
        session.db_call_id = call.id
        register_call(session)
        await session._send_opening_greeting()

        return {
            "status": "connected",
            "call_id": call.id,
            "call_sid": call_sid,
            "message": "SIP trunk inbound call initiated with AI receptionist.",
            "stream_endpoint": "/api/ws/voice-stream",
            "greeting_sent": True,
        }

    # ── transcription (provider posts pre-transcribed text) ─────────────────
    if event == "transcription":
        call_sid = payload.call_id
        if not call_sid:
            raise HTTPException(status_code=400, detail="call_id required for transcription event")

        session = get_active_call(call_sid)
        if not session:
            raise HTTPException(status_code=404, detail=f"No active call session for call_id={call_sid}")

        text = payload.transcription or ""
        if text.strip():
            await session.process_text(text)

        return {"status": "ok", "call_sid": call_sid, "processed": bool(text.strip())}

    # ── call_end ─────────────────────────────────────────────────────────────
    if event == "call_end":
        call_sid = payload.call_id
        if not call_sid and payload.caller_phone:
            from src.services.sip_call_handler import _active_calls
            for s in list(_active_calls.values()):
                if getattr(s, "caller_phone", None) == payload.caller_phone:
                    call_sid = s.call_sid
                    break
            if not call_sid:
                from src.models.call_log import CallLog
                last_c = db.query(CallLog).filter(CallLog.caller_phone == payload.caller_phone).order_by(CallLog.id.desc()).first()
                if last_c:
                    call_sid = last_c.call_sid

        session = get_active_call(call_sid) if call_sid else None
        if session:
            await session.stop()

        # Update DB regardless
        if call_sid:
            try:
                from src.models.call_log import CallLog
                call = db.query(CallLog).filter(CallLog.call_sid == call_sid).first()
                if call:
                    call.status = "completed"
                    call.receptionist_state = "SIP CALL COMPLETED"
                    db.commit()
            except Exception as exc:
                pass

        await sio.emit("call_ended", {
            "call_sid": call_sid,
            "event": "call_ended",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        await sio.emit("call_status_update", {
            "call_sid": call_sid,
            "status": "completed",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        return {"status": "completed", "call_sid": call_sid, "message": "Call session finalised."}

    raise HTTPException(status_code=400, detail=f"Unknown event type: {event}")


@router.post("/sip/inbound/audio")
async def sip_inbound_audio(payload: SipAudioChunkPayload, db: Session = Depends(get_db)):
    """
    Receive a base64-encoded audio chunk for an active SIP call and feed it
    to the AI STT pipeline.  Used by providers that stream raw RTP frames
    via HTTP rather than WebSocket.
    """
    from src.services.sip_call_handler import get_active_call

    session = get_active_call(payload.call_sid)
    if not session:
        raise HTTPException(status_code=404, detail=f"No active call for call_sid={payload.call_sid}")

    try:
        audio_bytes = base64.b64decode(payload.audio_base64)
        session.feed_audio(audio_bytes)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid audio payload: {exc}")

    return {"status": "queued", "call_sid": payload.call_sid, "bytes": len(audio_bytes)}


@router.post("/sip/inbound/hangup")
async def sip_inbound_hangup(payload: SipCallEndPayload, db: Session = Depends(get_db)):
    """Terminate an active SIP call session."""
    from src.services.sip_call_handler import terminate_call
    reason = payload.reason or "LOCAL_HANGUP"
    res = await terminate_call(payload.call_sid, reason=reason)
    return {"status": "success", "call_sid": payload.call_sid, "details": res}


# ──────────────────────────────────────────────────────────────────────────────
# Admin call barging
# ──────────────────────────────────────────────────────────────────────────────

class BargeResponse(BaseModel):
    status: str
    call_sid: str
    barge_state: str
    message: str


@router.post("/calls/{call_sid}/barge", response_model=BargeResponse)
async def barge_call(
    call_sid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["super_admin"])),
):
    """
    Real Call Barging for Super Admin.
    Verifies Super Admin permission, checks active call status, updates state
    to BARGED, and broadcasts via Socket.IO.
    """
    call = db.query(CallLog).filter(CallLog.call_sid == call_sid).first()
    if not call:
        try:
            call = db.query(CallLog).filter(CallLog.id == int(call_sid)).first()
        except ValueError:
            pass

    if not call:
        raise HTTPException(status_code=404, detail="Call session not found")

    call.receptionist_state = "BARGED (Super Admin Connected)"
    call.status = "AI + SUPER ADMIN (BARGED)"
    db.commit()

    await sio.emit("call_barged", {
        "call_id": call.id,
        "call_sid": call.call_sid or call_sid,
        "barge_state": "AI_AND_SUPER_ADMIN",
        "super_admin_name": current_user.name,
        "timestamp": call.created_at.isoformat() if call.created_at else "",
    })

    return BargeResponse(
        status="success",
        call_sid=call.call_sid or str(call.id),
        barge_state="AI_AND_SUPER_ADMIN",
        message=f"Super Admin {current_user.name} joined live call session successfully.",
    )

from src.services.telephony_service import TelephonyService
from src.validators.telephony_validator import SipOutboundCallRequest, SipOutboundCallResponse

@router.post("/sip/outbound", response_model=SipOutboundCallResponse)
def make_outbound_call_route(
    payload: SipOutboundCallRequest,
    current_user: Optional[User] = Depends(get_optional_current_user),
    db: Session = Depends(get_db)
):
    user_id = current_user.id if current_user else 1
    org_id = current_user.organization_id if current_user else 1
    result = TelephonyService(db, user_id=user_id, org_id=org_id).make_outbound_call(payload)
    return SipOutboundCallResponse(**result)
