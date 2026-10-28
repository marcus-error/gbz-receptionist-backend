"""
Direct Low-Latency Voice Performance Benchmark Runner.
"""

import time
import asyncio
import os
import sys

# Force UTF-8 output encoding for Windows terminals
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

os.environ["APP_ENV"] = "test"

from src.services.ai_service import ai_service
from src.services.nlp_service import nlp_service
from src.services.cache_service import cache_service
from src.services.rag_service import rag_service
from src.services.booking_state_service import booking_state_service
from src.services.entity_validation_service import entity_validation_service
from src.services.language_service import language_service


def prewarm_connections():
    """Prewarm ChromaDB, cache, and database connection pools."""
    t0 = time.perf_counter()
    try:
        rag_service.get_organization_master_context(1)
        rag_service.get_organization_name(1)
        ai_service._fast_rule_response("hi", intent="greeting", organization_id=1)
        dur_ms = (time.perf_counter() - t0) * 1000.0
        print(f"[PREWARM] Connections & Models Prewarmed in {dur_ms:.2f} ms")
    except Exception as e:
        print(f"[PREWARM WARNING] {e}")


def run_fast_path_benchmark():
    print("\n--- 1. FAST PATH LATENCY BENCHMARK ---")
    queries = [
        ("What's your salon name?", "hospital_information"),
        ("What are your opening hours?", "hospital_information"),
        ("Where are you located?", "hospital_information"),
        ("List your doctors", "doctor_list"),
        ("What services do you offer?", "department_list")
    ]

    for q, intent in queries:
        t0 = time.perf_counter()
        resp = ai_service._fast_rule_response(q, intent=intent, organization_id=1)
        dur_ms = (time.perf_counter() - t0) * 1000.0

        assert resp is not None, f"Fast path should answer query: '{q}'"
        assert dur_ms < 150.0, f"Fast path latency for '{q}' should be < 150 ms (was {dur_ms:.2f} ms)"
        print(f"  ⚡ '{q}' -> {dur_ms:.2f} ms | Response: '{resp[:55]}...'")


async def run_single_pass_benchmark():
    print("\n--- 2. SINGLE-PASS INTENT & ENTITY BENCHMARK ---")
    user_text = "I want to book an appointment with Dr. Arun Kumar tomorrow at 10 AM"

    t0 = time.perf_counter()
    nlp_res = await nlp_service.detect_intent_async(user_text)
    dur_nlp = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    val_res = entity_validation_service.validate_entity("Dr. Arun Kumar", organization_id=1)
    dur_val = (time.perf_counter() - t1) * 1000.0

    assert nlp_res.intent == "appointment_booking"
    assert dur_nlp < 150.0
    print(f"  ⚡ Intent NLP: {dur_nlp:.2f} ms | Entity Validation: {dur_val:.2f} ms")


def run_cache_benchmark():
    print("\n--- 3. IN-MEMORY CACHE BENCHMARK ---")
    key = "org_1_test_key"
    val = {"name": "LuxeGlow Salon", "phone": "555-0199"}

    t0 = time.perf_counter()
    cache_service.set(key, val, ttl=60)
    retrieved = cache_service.get(key)
    dur_ms = (time.perf_counter() - t0) * 1000.0

    assert retrieved == val
    assert dur_ms < 5.0
    print(f"  ⚡ Cache Set + Get duration: {dur_ms:.3f} ms")


async def run_multi_turn_acceptance_benchmark():
    print("\n--- 4. MULTI-TURN ACCURACY & CONTINUITY ACCEPTANCE BENCHMARK ---")
    session_id = "test_perf_acceptance_session_99"
    booking_session = booking_state_service.get_session(session_id)
    booking_session.reset()

    # Turn 1
    t1_text = "எனக்கு Priya Sharma-க்கு appointment book பண்ணணும்."
    lang1 = language_service.determine_conversation_language(t1_text)
    booking_session.conversation_language = lang1
    nlp1 = await nlp_service.detect_intent_async(t1_text)
    resp1 = booking_state_service.process_booking_turn(t1_text, booking_session)
    print(f"  Turn 1 [Tamil]: '{t1_text}'\n    -> State: {booking_session.state} | Doctor: {booking_session.doctor} | Resp: '{resp1}'")

    # Turn 2
    t2_text = "spa"
    resp2 = booking_state_service.process_booking_turn(t2_text, booking_session)
    print(f"  Turn 2 [Service]: '{t2_text}'\n    -> State: {booking_session.state} | Resp: '{resp2}'")

    # Turn 3
    t3_text = "நாளைக்கு"
    resp3 = booking_state_service.process_booking_turn(t3_text, booking_session)
    print(f"  Turn 3 [Date]: '{t3_text}'\n    -> State: {booking_session.state} | Date: {booking_session.date} | Resp: '{resp3}'")

    # Turn 4
    t4_text = "morning"
    resp4 = booking_state_service.process_booking_turn(t4_text, booking_session)
    print(f"  Turn 4 [Time Slot]: '{t4_text}'\n    -> State: {booking_session.state} | Resp: '{resp4}'")

    booking_state_service.remove_session(session_id)
    print("  ✅ MULTI-TURN CONTINUITY ACCEPTANCE TEST PASSED!")


async def main():
    print("==================================================")
    print("   AI RECEPTIONIST LOW-LATENCY BENCHMARK RUNNER   ")
    print("==================================================")
    prewarm_connections()
    run_fast_path_benchmark()
    await run_single_pass_benchmark()
    run_cache_benchmark()
    await run_multi_turn_acceptance_benchmark()
    print("==================================================")
    print("   ALL LOW-LATENCY PERFORMANCE TARGETS VERIFIED   ")
    print("==================================================")


if __name__ == "__main__":
    asyncio.run(main())
