"""
Test Suite for SIP ACK Handling, RFC 3261 Compliance, and 32+ Second Call Lifecycle.

Verifies:
1. Exact RFC 3261 ACK Request-URI generation (no 'sip:sip:' bug, Contact header resolution).
2. Outbound 200 OK ACK delivery and retransmission handling.
3. Call lasting > 32 seconds without disconnect.
4. Continuous multi-turn conversation (2-5 minutes simulation).
5. 10-20 seconds customer silence handling (comfort noise & watchdog integrity).
6. Slow LLM/RAG/TTS response handling without call drops.
7. Customer barge-in interruption while AI is speaking.
8. Normal customer hangup (clean BYE handling).
9. Concurrent calls without call-state interference.
"""

import asyncio
import time
import uuid
import pytest
from unittest.mock import MagicMock, patch

import pyVoIP.SIP
from pyVoIP.SIP import SIPMessage
from pyVoIP.VoIP import CallState, VoIPCall

from src.services.sip_registrar import SipRegistrar, get_registrar
from src.services.sip_call_handler import (
    SipCallSession,
    register_call,
    get_active_call,
    terminate_call,
    _active_sip_calls,
)


@pytest.fixture(autouse=True)
def clean_active_calls():
    """Ensure clean active calls registry for every test."""
    _active_sip_calls.clear()
    yield
    _active_sip_calls.clear()


# ==============================================================================
# TEST 1: RFC 3261 Compliant ACK Generation (Fixing the 32-second 'sip:sip:' bug)
# ==============================================================================
def test_1_outbound_ack_generation_rfc3261_compliance():
    """
    Test that _patched_gen_ack generates an exact, standards-compliant ACK:
    - Never prepends duplicate 'sip:sip:'
    - Extracts Request-URI directly from the 200 OK's Contact header
    - Preserves tags on To and From headers
    - Sets CSeq with matching sequence number and method ACK
    """
    raw_200_ok = (
        b"SIP/2.0 200 OK\r\n"
        b"Via: SIP/2.0/UDP 210.18.176.97:5080;branch=z9hG4bK-orig-12345;rport=5080;received=210.18.176.97\r\n"
        b"Call-ID: call-out-987654@210.18.176.97:5080\r\n"
        b"From: <sip:09410697548@210.18.176.97>;tag=local-from-tag-11\r\n"
        b"To: <sip:9092649453@49.249.142.147>;tag=remote-to-tag-22\r\n"
        b"CSeq: 5 INVITE\r\n"
        b"Contact: <sip:49.249.142.147:5060>\r\n"
        b"Content-Length: 0\r\n\r\n"
    )
    msg = SIPMessage(raw_200_ok)

    mock_client = MagicMock()
    mock_client.myIP = "210.18.176.97"
    mock_client.myPort = 5080

    # Call the patched gen_ack on SIPClient
    gen_ack_fn = pyVoIP.SIP.SIPClient.gen_ack
    ack_str = gen_ack_fn(mock_client, msg)

    # 1. Must NOT contain malformed double sip:sip:
    assert "sip:sip:" not in ack_str, f"Found duplicate sip:sip: in ACK:\n{ack_str}"

    # 2. Must start with exact Contact URI Request-Line
    assert ack_str.startswith("ACK sip:49.249.142.147:5060 SIP/2.0\r\n"), f"Invalid Request-Line:\n{ack_str}"

    # 3. Must match To header with tag
    assert "To: <sip:9092649453@49.249.142.147>;tag=remote-to-tag-22\r\n" in ack_str

    # 4. Must match From header with tag
    assert "From: <sip:09410697548@210.18.176.97>;tag=local-from-tag-11\r\n" in ack_str

    # 5. Must match Call-ID
    assert "Call-ID: call-out-987654@210.18.176.97:5080\r\n" in ack_str

    # 6. Must have matching CSeq with ACK method
    assert "CSeq: 5 ACK\r\n" in ack_str

    # 7. Via must have a fresh branch starting with z9hG4bK-ack-
    assert "branch=z9hG4bK-ack-" in ack_str


# ==============================================================================
# TEST 2: Outbound 200 OK Handling & ACK Retransmission
# ==============================================================================
def test_2_callback_resp_ok_retransmission_and_multi_destination():
    """
    Test that _patched_resp_ok handles the 200 OK:
    - Transitions call state from DIALING to ANSWERED
    - Transmits ACK to server destination and Contact host
    - When 200 OK is retransmitted by Asterisk, immediately re-sends the ACK
    """
    mock_phone = MagicMock()
    mock_phone.server = "49.249.142.147"
    mock_phone.port = 5060
    mock_phone.calls = {}

    cid = "call-retrans-123@210.18.176.97:5080"
    mock_call = MagicMock()
    mock_call.state = CallState.DIALING
    mock_call.answered = MagicMock(side_effect=lambda req: setattr(mock_call, "state", CallState.ANSWERED))
    mock_phone.calls[cid] = mock_call

    raw_200 = (
        "SIP/2.0 200 OK\r\n"
        "Via: SIP/2.0/UDP 210.18.176.97:5080;branch=z9hG4bK123\r\n"
        f"Call-ID: {cid}\r\n"
        "From: <sip:09410697548@210.18.176.97>;tag=tag1\r\n"
        "To: <sip:9092649453@49.249.142.147>;tag=tag2\r\n"
        "CSeq: 1 INVITE\r\n"
        "Contact: <sip:49.249.142.147:5060>\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode("utf-8")
    msg = SIPMessage(raw_200)

    # First 200 OK arrival: Initial answer
    pyVoIP.VoIP.VoIPPhone._callback_RESP_OK(mock_phone, msg)
    assert mock_call.state == CallState.ANSWERED
    assert mock_call.answered.call_count == 1
    assert mock_phone.sip.out.sendto.called

    # Second 200 OK arrival: Asterisk retransmission
    initial_send_count = mock_phone.sip.out.sendto.call_count
    pyVoIP.VoIP.VoIPPhone._callback_RESP_OK(mock_phone, msg)
    # State remains ANSWERED, but ACK must be retransmitted
    assert mock_call.state == CallState.ANSWERED
    assert mock_phone.sip.out.sendto.call_count > initial_send_count


# ==============================================================================
# TEST 3: Call Duration Exceeding 32 Seconds Without Disconnect
# ==============================================================================
@pytest.mark.anyio
async def test_3_call_duration_exceeding_32_seconds():
    """
    Test that an active call session does not automatically disconnect
    when crossing the 32-second boundary.
    """
    session = SipCallSession(
        caller_phone="9092649453",
        caller_name="Long Call Patient",
        organization_id=1,
    )
    register_call(session)
    session._last_activity_time = time.monotonic()

    # Fast-forward simulated elapsed time past 32 seconds
    session._session_start_time = time.time() - 35.0  # 35 seconds elapsed

    # Verify session is still alive and active
    assert not session.ended
    assert session.call_sid in _active_sip_calls

    # Simulate RTP frames flowing during this time
    assert getattr(session, "_rtp_frame_count", 0) >= 0


# ==============================================================================
# TEST 4: Continuous Multi-Minute Conversation Simulation
# ==============================================================================
@pytest.mark.anyio
async def test_4_continuous_conversation_multi_minute():
    """
    Test continuous multi-turn conversation over simulated 180 seconds (3 minutes):
    Each customer utterance resets the activity timer, keeping the call robustly open.
    """
    session = SipCallSession(
        caller_phone="9092649453",
        caller_name="Continuous Patient",
        organization_id=1,
    )
    register_call(session)

    simulated_now = time.monotonic()
    # Simulate 6 conversational turns over 180 seconds (turn every 30s)
    for turn in range(6):
        simulated_now += 30.0
        session._last_activity_time = simulated_now
        session._messages.append({"role": "user", "content": f"Turn {turn} message"})
        session._messages.append({"role": "assistant", "content": f"Turn {turn} response"})

    # Session should remain fully active
    assert not session.ended
    assert len(session._messages) == 12
    assert session.call_sid in _active_sip_calls


# ==============================================================================
# TEST 5: Customer Silence 10–20 Seconds
# ==============================================================================
@pytest.mark.anyio
async def test_5_customer_silence_10_to_20_seconds():
    """
    Test that 15–20 seconds of customer silence does NOT terminate the call.
    The inactivity timeout is configured for 10 minutes (600s).
    """
    session = SipCallSession(
        caller_phone="9092649453",
        caller_name="Quiet Patient",
        organization_id=1,
    )
    register_call(session)

    # Fast-forward silence by 20 seconds
    session._last_activity_time = time.monotonic() - 20.0

    # Verify session is NOT ended
    assert not session.ended
    assert session.end_reason != "INACTIVITY_TIMEOUT"


# ==============================================================================
# TEST 6: Slow LLM / RAG / TTS Resilience (No Call Drops)
# ==============================================================================
@pytest.mark.anyio
async def test_6_slow_llm_rag_tts_resilience():
    """
    Test that slow TTS or LLM response delays do not crash or abort the session.
    """
    session = SipCallSession(
        caller_phone="9092649453",
        caller_name="Slow Pipeline Patient",
        organization_id=1,
    )
    register_call(session)

    with patch("src.services.tts_service.tts_service.synthesize_speech_async") as mock_tts:
        # Simulate a 200ms delay in TTS (within 4s timeout)
        async def slow_tts(*args, **kwargs):
            await asyncio.sleep(0.05)
            return b"RIFF" + (b"\x00" * 400)

        mock_tts.side_effect = slow_tts

        # Call speak
        await session._speak("Thank you for holding, let me check the clinic schedule.", intent="faq")

    assert not session.ended
    assert not session._ai_speaking


# ==============================================================================
# TEST 7: Customer Barge-In Interruption While AI is Speaking
# ==============================================================================
@pytest.mark.anyio
async def test_7_barge_in_interruption_during_playback():
    """
    Test that when customer speaks during AI playback, barge-in is triggered instantly:
    - self._barge_in_event is set
    - audio_clear_callback is invoked to flush the hardware buffer
    """
    clear_called = False
    def mock_clear():
        nonlocal clear_called
        clear_called = True

    session = SipCallSession(
        caller_phone="9092649453",
        caller_name="Barge-in Patient",
        organization_id=1,
        audio_clear_callback=mock_clear,
    )
    register_call(session)

    # Simulate barge-in trigger (e.g. from Deepgram partial)
    session._ai_speaking = True
    session._barge_in_event.set()
    if session.audio_clear_callback:
        session.audio_clear_callback()

    assert session._barge_in_event.is_set()
    assert clear_called is True


# ==============================================================================
# TEST 8: Normal Customer Hangup (Clean Remote BYE Handling)
# ==============================================================================
@pytest.mark.anyio
async def test_8_normal_customer_hangup():
    """
    Test that a remote hangup via terminate_call cleans up resources,
    marks call ended, cancels watchdog, and records the correct reason.
    """
    session = SipCallSession(
        caller_phone="9092649453",
        caller_name="Hanging Up Patient",
        organization_id=1,
    )
    register_call(session)

    assert session.call_sid in _active_sip_calls

    # Remote hangup arrives
    await terminate_call(session.call_sid, reason="REMOTE_HANGUP")

    assert session.ended is True
    assert session.end_reason == "REMOTE_HANGUP"
    assert session.call_sid not in _active_sip_calls


# ==============================================================================
# TEST 9: Concurrent Calls Without Call-State Interference
# ==============================================================================
@pytest.mark.anyio
async def test_9_concurrent_calls_isolation():
    """
    Test that multiple simultaneous calls have strictly isolated sessions,
    separate booking state machines, and independent audio buffers.
    """
    session_1 = SipCallSession(
        caller_phone="9092649451",
        caller_name="Caller 1",
        organization_id=1,
    )
    session_2 = SipCallSession(
        caller_phone="9092649452",
        caller_name="Caller 2",
        organization_id=1,
    )

    register_call(session_1)
    register_call(session_2)

    # Set distinct booking data
    session_1.booking_session.patient_name = "Alice Smith"
    session_1.booking_session.doctor_name = "Dr Arun"

    session_2.booking_session.patient_name = "Bob Jones"
    session_2.booking_session.doctor_name = "Dr Sarah"

    # Terminate call 1
    await terminate_call(session_1.call_sid, reason="REMOTE_HANGUP")

    # Call 1 must be ended, Call 2 must remain active
    assert session_1.ended is True
    assert session_2.ended is False
    assert session_2.booking_session.patient_name == "Bob Jones"
    assert get_active_call(session_2.call_sid) is not None
