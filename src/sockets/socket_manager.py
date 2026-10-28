import socketio

# Create a globally accessible Socket.IO server
# async_mode='asgi' is used to integrate with FastAPI
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')

# The ASGI application that will be mounted in FastAPI
socket_app = socketio.ASGIApp(sio)

import logging
logger = logging.getLogger(__name__)

@sio.event
async def connect(sid, environ):
    print(f"Socket.IO client connected: {sid}")

@sio.event
async def disconnect(sid):
    print(f"Socket.IO client disconnected: {sid}")

@sio.on("HANGUP_CALL")
async def handle_hangup_call(sid, data):
    call_id = None
    if isinstance(data, dict):
        call_id = data.get("call_id") or data.get("call_sid")
    elif isinstance(data, str):
        call_id = data

    print(f"[CALL_HANGUP_REQUESTED] call_id={call_id} source=WEB_UI sid={sid}")
    await sio.emit("HANGUP_ACCEPTED", {"type": "HANGUP_ACCEPTED", "call_id": call_id}, room=sid)
    print(f"[CALL_HANGUP_ACCEPTED] call_id={call_id}")
    if call_id:
        from src.services.sip_call_handler import terminate_call
        await terminate_call(call_id, reason="LOCAL_HANGUP")


@sio.on("hangup_call")
async def handle_hangup_call_lower(sid, data):
    await handle_hangup_call(sid, data)


@sio.event
async def accept_call(sid, data):
    call_sid = data.get("call_sid") if isinstance(data, dict) else None
    logger.info(f"Socket.IO client {sid} accepted call: {call_sid}")
    if call_sid:
        try:
            from src.services.sip_call_handler import get_active_call
            session = get_active_call(call_sid)
            if session:
                session.human_attended = True
                logger.info(f"[SIP CALL] Call {call_sid} marked as human_attended = True.")
        except Exception as exc:
            logger.warning(f"Error updating session human_attended state: {exc}")
    await sio.emit("call_status_update", {"call_sid": call_sid, "status": "in-progress"})


@sio.event
async def decline_call(sid, data):
    call_sid = data.get("call_sid") if isinstance(data, dict) else None
    logger.info(f"Socket.IO client {sid} declined/ended call: {call_sid}")
    # Use the authoritative terminate_call() so SIP BYE is sent to the customer's phone
    from src.services.sip_call_handler import terminate_call
    await terminate_call(call_sid, reason="LOCAL_HANGUP")
    await sio.emit("call_status_update", {"call_sid": call_sid, "status": "completed"}, room=sid)


@sio.event
async def user_mic_chunk(sid, data):
    """Relay browser microphone audio to active SIP call session RTP stream."""
    if isinstance(data, dict):
        call_sid = data.get("call_sid")
        audio_b64 = data.get("audio")
        if call_sid and audio_b64:
            try:
                from src.services.sip_call_handler import get_active_call, convert_tts_to_pyvoip_pcm8, write_audio_paced
                import base64
                import threading
                session = get_active_call(call_sid)
                if session and session.audio_send_callback:
                    audio_bytes = base64.b64decode(audio_b64)
                    pcm8_bytes = convert_tts_to_pyvoip_pcm8(audio_bytes)
                    if pcm8_bytes:
                        threading.Thread(
                            target=write_audio_paced,
                            args=(session.audio_send_callback, pcm8_bytes),
                            daemon=True,
                        ).start()
            except Exception as exc:
                logger.warning(f"Error processing browser mic chunk: {exc}")
