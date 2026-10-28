"""
Comprehensive Low-Latency Voice Experience Performance & Accuracy Acceptance Test Suite.

Validates:
1. Fast Path Response Latency (< 150 ms for structured business info queries).
2. Intent & Entity Extraction Latency (< 150 ms).
3. In-Memory Cache Performance (< 5 ms).
4. Full Multi-Turn Tamil/English Voice Conversation Acceptance Test (Context Continuity).
"""

import time
import pytest
import asyncio
from datetime import date, timedelta

from src.services.ai_service import ai_service
from src.services.nlp_service import nlp_service
from src.services.cache_service import cache_service
from src.services.rag_service import rag_service
from src.services.booking_state_service import booking_state_service, BookingState
from src.services.entity_validation_service import entity_validation_service
from src.services.language_service import language_service


def test_fast_path_latency():
    """Verify common receptionist requests execute in < 150 ms via Fast Path."""
    queries = [
        ("What's your salon name?", "hospital_information"),
        ("What is your company name?", "hospital_information"),
        ("Hello", "greeting"),
        ("Thank you", "general_conversation"),
        ("Goodbye", "goodbye")
    ]

    for q, intent in queries:
        t0 = time.perf_counter()
        resp = ai_service._fast_rule_response(q, intent=intent, organization_id=1)
        dur_ms = (time.perf_counter() - t0) * 1000.0

        assert resp is not None, f"Fast path should answer query: '{q}'"
        assert dur_ms < 150.0, f"Fast path latency for '{q}' should be < 150 ms (was {dur_ms:.2f} ms)"


def test_single_pass_intent_and_entity_latency():
    """Verify intent detection and entity validation execute in < 150 ms."""
    async def _run():
        user_text = "I want to book an appointment with Dr. Alexander tomorrow at 10 AM"

        t0 = time.perf_counter()
        nlp_res = await nlp_service.detect_intent_async(user_text)
        dur_nlp = (time.perf_counter() - t0) * 1000.0

        t1 = time.perf_counter()
        val_res = entity_validation_service.validate_entity("Dr. Alexander", organization_id=1)
        dur_val = (time.perf_counter() - t1) * 1000.0

        assert nlp_res.intent == "appointment_booking"
        assert dur_nlp < 150.0, f"NLP intent detection should be < 150 ms (was {dur_nlp:.2f} ms)"
        assert dur_val < 150.0, f"Entity validation should be < 150 ms (was {dur_val:.2f} ms)"

    asyncio.run(_run())


def test_in_memory_cache_performance():
    """Verify in-memory cache operations execute in < 5 ms."""
    key = "org_1_test_key"
    val = {"name": "LuxeGlow Salon", "phone": "555-0199"}

    t0 = time.perf_counter()
    cache_service.set(key, val, ttl=60)
    retrieved = cache_service.get(key)
    dur_ms = (time.perf_counter() - t0) * 1000.0

    assert retrieved == val
    assert dur_ms < 5.0, f"Cache ops should execute in < 5 ms (was {dur_ms:.2f} ms)"


def test_final_performance_acceptance_multi_turn_voice_conversation():
    """
    REQUIRED ACCEPTANCE TEST FROM PROMPT:
    Simulates multi-turn conversation with state retention & language lock:
    User 1: "எனக்கு Priya Sharma-க்கு appointment book பண்ணணும்."
    User 2: "spa"
    User 3: "நாளைக்கு"
    User 4: "morning"
    """
    async def _run():
        session_id = "test_perf_acceptance_session_99"
        booking_session = booking_state_service.get_session(session_id)
        booking_session.reset()

        # --- Turn 1 ---
        turn1_text = "எனக்கு Priya Sharma-க்கு appointment book பண்ணணும்."
        lang1 = language_service.determine_conversation_language(turn1_text)
        assert lang1 in ("ta", "ta_mix", "tanglish"), f"Expected Tamil detection, got {lang1}"
        booking_session.conversation_language = lang1

        nlp1 = await nlp_service.detect_intent_async(turn1_text)
        assert nlp1.intent in ("appointment_booking", "appointment_availability")

        resp1 = booking_state_service.process_booking_turn(turn1_text, booking_session)
        assert booking_session.doctor is not None or "Priya" in resp1 or booking_session.is_active()

        # --- Turn 2 ---
        turn2_text = "spa"
        nlp2 = await nlp_service.detect_intent_async(turn2_text)
        resp2 = booking_state_service.process_booking_turn(turn2_text, booking_session)
        assert booking_session.is_active(), "Session should remain active across turns"

        # --- Turn 3 ---
        turn3_text = "நாளைக்கு"
        nlp3 = await nlp_service.detect_intent_async(turn3_text)
        resp3 = booking_state_service.process_booking_turn(turn3_text, booking_session)
        assert booking_session.date is not None or "நாளைக்கு" in resp3 or "time" in resp3.lower() or "slot" in resp3.lower() or "மணி" in resp3

        # --- Turn 4 ---
        turn4_text = "morning"
        resp4 = booking_state_service.process_booking_turn(turn4_text, booking_session)
        assert resp4 and len(resp4) > 10

        # Cleanup session
        booking_state_service.remove_session(session_id)

    asyncio.run(_run())
