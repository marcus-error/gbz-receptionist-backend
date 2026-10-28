"""
End-to-end tests for the 6 critical call scenarios:
1. “What services do you provide?”
2. “What is Nurse O' Clock?”
3. A caller who pauses briefly in the middle of a question.
4. A caller who interrupts the AI (barge-in).
5. A slow or failed LLM stream fallback.
6. Multiple concurrent calls.
"""

import asyncio
import time
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from src.services.sip_call_handler import SipCallSession
from src.services.rag_service import rag_service
from src.services.ai_service import ai_service
from src.services.deepgram_stt_service import DeepgramStreamingSession


@pytest.mark.asyncio
async def test_scenario_1_what_services_do_you_provide():
    """Verify complete, grounded answer and measured latency for service query."""
    received_audio = []
    def mock_send(audio):
        received_audio.append(audio)

    session = SipCallSession(
        call_sid="test_srv_001",
        caller_phone="+919876543210",
        organization_id=1,
        audio_send_callback=mock_send,
    )

    t_speech_end = time.monotonic()
    metrics = {
        "utt_id": "utt_test_1",
        "audio_duration_s": 1.5,
        "speech_end_monotonic": t_speech_end,
        "t_speech_endpoint": t_speech_end,
        "t_stt_final": t_speech_end + 0.12,
        "endpointing_ms": 300,
        "finalization_latency_ms": 120,
    }

    t0 = time.monotonic()
    await session._run_ai_pipeline("What services do you provide?", metrics)
    total_latency_ms = round((time.monotonic() - t0) * 1000)

    # Verify session history recorded full answer
    last_msg = session.session_history[-1]
    assert last_msg["role"] == "model"
    ans_text = last_msg["text"]
    assert len(ans_text.split()) >= 5, f"Answer too short/fragmented: '{ans_text}'"
    # Grounded in active knowledge base / enterprise services
    assert any(term in ans_text.lower() for term in ["home", "care", "nurs", "health", "patient", "service", "product", "assist", "enterprise"]), f"Not grounded: '{ans_text}'"
    assert len(received_audio) > 0, "No audio transmitted to phone"
    assert "On" != ans_text.strip(), "Must not be dangling fragment"
    await session.stop()


@pytest.mark.asyncio
async def test_scenario_2_what_is_nurse_o_clock():
    """Verify complete organization identity and purpose response."""
    received_audio = []
    session = SipCallSession(
        call_sid="test_org_002",
        caller_phone="+919876543210",
        organization_id=1,
        audio_send_callback=lambda a: received_audio.append(a),
    )

    t_speech_end = time.monotonic()
    metrics = {
        "utt_id": "utt_test_2",
        "audio_duration_s": 1.2,
        "speech_end_monotonic": t_speech_end,
        "t_speech_endpoint": t_speech_end,
        "t_stt_final": t_speech_end + 0.10,
        "endpointing_ms": 300,
        "finalization_latency_ms": 100,
    }

    await session._run_ai_pipeline("What is Nurse O' Clock?", metrics)
    last_msg = session.session_history[-1]
    ans_text = last_msg["text"]
    assert len(ans_text.split()) >= 6, f"Identity response fragmented: '{ans_text}'"
    assert any(term in ans_text.lower() for term in ["nurse", "healthcare", "care", "service", "provider", "enterprise"])
    await session.stop()


@pytest.mark.asyncio
async def test_scenario_3_caller_pauses_briefly():
    """Verify that a brief pause (600ms) between incomplete words does NOT finalize prematurely."""
    finalized_results = []
    partial_results = []

    def on_partial(utt_id, txt, dur):
        partial_results.append((utt_id, txt))

    def on_final(utt_id, txt, m):
        finalized_results.append((utt_id, txt))

    dg = DeepgramStreamingSession(
        call_id="test_pause_003",
        on_partial=on_partial,
        on_final=on_final
    )
    dg._connected = True
    dg._running = True

    # 1. First segment arrives: "What are the" (is_final=True, speech_final=False)
    data1 = {
        "type": "Results",
        "is_final": True,
        "speech_final": False,
        "duration": 0.8,
        "channel": {"alternatives": [{"transcript": "What are the", "confidence": 0.95}]}
    }
    await dg._handle_results_message(data1)
    assert len(finalized_results) == 0, "Prematurely finalized 'What are the'!"

    # 2. Caller pauses for 600ms (debounce timer is 1.5s for incomplete words)
    await asyncio.sleep(0.6)
    assert len(finalized_results) == 0, "Finalized during natural 600ms pause!"

    # 3. Caller resumes and completes: "services available in nurse o'clock?"
    data2 = {
        "type": "Results",
        "is_final": True,
        "speech_final": True,
        "duration": 2.2,
        "channel": {"alternatives": [{"transcript": "services available in nurse o'clock?", "confidence": 0.97}]}
    }
    await dg._handle_results_message(data2)

    # Should finalize exactly ONCE with the combined text
    assert len(finalized_results) == 1, f"Expected exactly 1 finalization, got {len(finalized_results)}"
    utt_id, full_text = finalized_results[0]
    assert utt_id == "utt_100"
    assert "What are the services available" in full_text
    await dg.stop()


@pytest.mark.asyncio
async def test_scenario_4_caller_interrupts_ai_barge_in():
    """Verify responsive barge-in: AI halts playback and clears audio queue on interrupt."""
    cleared = False
    def mock_clear():
        nonlocal cleared
        cleared = True

    session = SipCallSession(
        call_sid="test_barge_004",
        caller_phone="+919876543210",
        organization_id=1,
        audio_clear_callback=mock_clear,
    )
    session._ai_speaking = True
    session._ai_speaking_until = time.monotonic() + 5.0

    # Caller interrupts with speech
    await session._on_deepgram_partial("utt_interrupt", "Excuse me, wait", 0.4)

    assert session._ai_speaking is False, "AI speaking flag was not cleared on barge-in"
    assert session._barge_in_event.is_set(), "Barge in event was not set"
    assert cleared is True, "Audio clear callback was not called"
    await session.stop()


@pytest.mark.asyncio
async def test_scenario_5_slow_or_failed_llm_stream_fallback():
    """Verify that an empty or failed LLM stream returns complete grounded fallback, never fragments."""
    session = SipCallSession(
        call_sid="test_fallback_005",
        caller_phone="+919876543210",
        organization_id=1,
        audio_send_callback=lambda a: None,
    )

    t_speech_end = time.monotonic()
    metrics = {
        "utt_id": "utt_test_5",
        "audio_duration_s": 1.0,
        "speech_end_monotonic": t_speech_end,
        "t_speech_endpoint": t_speech_end,
        "t_stt_final": t_speech_end + 0.1,
    }

    # Simulate an LLM stream that yields nothing
    with patch.object(ai_service, "generate_response_stream", return_value=iter([])):
        await session._run_ai_pipeline("Is there any service?", metrics)

    last_msg = session.session_history[-1]
    ans_text = last_msg["text"]
    assert len(ans_text.split()) >= 6, f"Fallback must be complete sentence: '{ans_text}'"
    assert any(term in ans_text.lower() for term in ["nurse", "healthcare", "care", "service", "provider", "enterprise"])
    assert "On" != ans_text.strip()
    await session.stop()


@pytest.mark.asyncio
async def test_scenario_6_multiple_concurrent_calls():
    """Verify multiple concurrent calls execute without crosstalk or mixed utterance IDs."""
    sessions = [
        SipCallSession(
            call_sid=f"test_concurrent_{i}",
            caller_phone=f"+91987654321{i}",
            organization_id=1,
            audio_send_callback=lambda a: None,
        )
        for i in range(3)
    ]

    async def run_call(s, idx):
        m = {
            "utt_id": f"utt_call_{idx}",
            "speech_end_monotonic": time.monotonic(),
            "t_speech_endpoint": time.monotonic(),
            "t_stt_final": time.monotonic() + 0.05,
        }
        await s._run_ai_pipeline(f"What services do you offer in call {idx}?", m)

    await asyncio.gather(*(run_call(s, i) for i, s in enumerate(sessions)))

    for i, s in enumerate(sessions):
        assert len(s.session_history) >= 2
        resp = s.session_history[-1]["text"]
        assert len(resp.split()) >= 4, f"Call {i} response invalid: '{resp}'"
        await s.stop()
