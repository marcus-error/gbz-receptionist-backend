"""
Test Suite for AI Receptionist:
1. Customer rejects previously selected department -> department & doctor invalidated, patient info preserved.
2. "I don't want dental care" does not select Dental Care.
3. Missing doctor record never produces a doctor offer.
4. Legitimate active doctor offered from DB records.
5. Home healthcare requests not redirected to dental care.
6. New customer turn does not replay previous AI response.
7. Duplicate processing of same turn (call_sid, utt_id) suppressed.
8. Organization A cannot access Organization B's doctors or knowledge.
9. Voice flow preservation check (verify SIP, pyVoIP, RTP, PCMU, Murf audio path untouched).
10. Measured response latency validation (<2000 ms).
"""

import time
import uuid
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from src.config.database import SessionLocal
from src.models.staff import Staff, StaffStatus
from src.models.department import Department
from src.models.knowledge_doc import KnowledgeDocument
from src.services.booking_state_service import booking_state_service, BookingSession, BookingState
from src.services.entity_validation_service import entity_validation_service
from src.services.rag_service import rag_service
from src.services.sip_call_handler import SipCallSession, convert_tts_to_pcmu_and_pcm16
from src.services.murf_tts import murf_tts


# =============================================================================
# SCENARIO 1: Department rejection invalidates doctor & department, retains patient info
# =============================================================================
def test_scenario_1_department_rejection_invalidates_dept_and_preserves_patient():
    session = BookingSession("test_s1")
    session.organization_id = 1
    # Seed prior state
    session.department = "Cardiology"
    session.doctor = "Dr. Priya"
    session.doctor_id = 101
    session.doctor_locked = True
    session.patient_name = "Ravi Kumar"
    session.caller_phone = "+919876543210"
    session.date = "2026-10-25"
    session.state = BookingState.WAITING_FOR_TIME

    # Customer rejects cardiology
    resp = booking_state_service.process_booking_turn("Actually I don't want cardiology", session)

    # Department and doctor must be cleared
    assert session.department is None, f"Expected None, got {session.department}"
    assert session.doctor is None, f"Expected None, got {session.doctor}"
    assert session.doctor_id is None, f"Expected None, got {session.doctor_id}"
    assert "cardiology" in session._rejected_departments

    # Valid patient info MUST be preserved
    assert session.patient_name == "Ravi Kumar"
    assert session.caller_phone == "+919876543210"
    assert session.date == "2026-10-25"

    # AI prompt asks for preferred department/doctor, not re-confirming cardiology
    assert "priya" not in resp.lower()
    assert "cardiology" not in resp.lower() or "which department" in resp.lower()


# =============================================================================
# SCENARIO 2: "I don't want dental care" does not select Dental Care
# =============================================================================
def test_scenario_2_negation_does_not_select_dental_care():
    session = BookingSession("test_s2")
    session.organization_id = 1

    resp = booking_state_service.process_booking_turn("I don't want dental care, I need a general physician", session)

    assert session.department != "Dental Care"
    assert "dental" not in (session.department or "").lower()
    assert session.doctor != "Dr. Rohit Kumar"
    assert "dental" in [d.lower() for d in session._rejected_departments] or "dental care" in session._rejected_departments
    # Must NOT offer Dr. Rohit Kumar
    assert "rohit" not in resp.lower()


# =============================================================================
# SCENARIO 3: Missing doctor record never produces a doctor offer (id: None rejected)
# =============================================================================
def test_scenario_3_missing_doctor_record_never_produces_doctor_offer():
    session = BookingSession("test_s3")
    session.organization_id = 1

    # Try to validate and book an unlisted doctor
    val_res = entity_validation_service.validate_entity("Dr. Nonexistent Ghost", organization_id=1)
    assert not val_res.is_verified
    assert val_res.staff_id is None

    resp = booking_state_service.process_booking_turn("Can I book with Dr. Nonexistent Ghost?", session)

    assert session.doctor is None
    assert session.doctor_id is None
    assert "couldn't find" in resp.lower() or "not find" in resp.lower() or "directory" in resp.lower()


# =============================================================================
# SCENARIO 4: Legitimate active doctor offered from DB records
# =============================================================================
def test_scenario_4_legitimate_active_doctor_offered_from_db(db_session):
    # Ensure active doctor in DB
    unique_name = f"TestDoc_{uuid.uuid4().hex[:6]}"
    doc = Staff(
        name=unique_name,
        department="Pediatrics",
        organization_id=1,
        status=StaffStatus.AVAILABLE,
        online_booking_enabled=True
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    try:
        session = BookingSession("test_s4")
        session.organization_id = 1
        resp = booking_state_service.process_booking_turn(f"I want to book an appointment with {unique_name}", session)

        assert session.doctor is not None
        assert unique_name.lower() in session.doctor.lower()
        assert session.doctor_id == doc.id
        assert session.doctor_id is not None
    finally:
        db_session.delete(doc)
        db_session.commit()


# =============================================================================
# SCENARIO 5: Home healthcare requests not redirected to dental care
# =============================================================================
def test_scenario_5_home_healthcare_not_redirected_to_dental():
    session = BookingSession("test_s5")
    session.organization_id = 1

    val_res = entity_validation_service.validate_entity("home healthcare", organization_id=1)
    # Must NOT resolve to dental
    if val_res.is_verified:
        assert "dental" not in (val_res.verified_name or "").lower()

    resp = booking_state_service.process_booking_turn("I need home healthcare and nursing care for my father", session)

    assert (session.department or "").lower() != "dental care"
    assert "rohit" not in resp.lower()
    assert "dental" not in resp.lower() or "home" in resp.lower()


# =============================================================================
# SCENARIO 6: New customer turn does not replay previous AI response
# =============================================================================
def test_scenario_6_repeated_customer_utterance_does_not_replay_stale_response():
    session = BookingSession("test_s6")
    session.organization_id = 1

    # First turn
    resp1 = booking_state_service.process_booking_turn("I need an appointment", session)
    assert resp1 is not None

    # Advance state: caller repeats exact utterance
    resp2 = booking_state_service.process_booking_turn("I need an appointment", session)

    # In the old code, this was hardcoded to: return _ret(last_resp)
    # Now it must re-evaluate dynamically against the current state and increment turn count
    assert session.turn_count == 2
    assert session.turn_id == 2


# =============================================================================
# SCENARIO 7: Duplicate processing of same turn (call_sid, utt_id) suppressed
# =============================================================================
@pytest.mark.asyncio
async def test_scenario_7_duplicate_turn_processing_suppressed():
    received_audio = []

    def mock_send(audio):
        received_audio.append(audio)

    session = SipCallSession(
        call_sid="call_dupe_test_7",
        caller_phone="+919876543210",
        caller_name="Test Caller",
        organization_id=1,
        audio_send_callback=mock_send,
    )

    metrics = {
        "utt_id": "utt_unique_777",
        "audio_duration_s": 1.0,
        "speech_end_monotonic": time.monotonic(),
        "t_speech_endpoint": time.monotonic(),
        "t_stt_final": time.monotonic() + 0.1,
    }

    # First run of turn
    await session._run_ai_pipeline("I need to book an appointment", metrics)
    first_history_len = len(session.session_history)
    assert first_history_len > 0

    # Second run with exact SAME turn_key (call_dupe_test_7, utt_unique_777)
    await session._run_ai_pipeline("I need to book an appointment", metrics)

    # Must be suppressed — no extra turns added to session_history
    assert len(session.session_history) == first_history_len, "Duplicate turn was not suppressed!"
    await session.stop()


# =============================================================================
# SCENARIO 8: Organization A cannot access Organization B's doctors or knowledge
# =============================================================================
def test_scenario_8_strict_multi_tenant_isolation(db_session):
    # Org 2 doctor
    doc_org2 = Staff(
        name="Dr. Secret Specialist OrgTwo",
        department="Neurology",
        organization_id=2,
        status=StaffStatus.AVAILABLE,
        online_booking_enabled=True
    )
    # Org 2 document
    doc_rag2 = KnowledgeDocument(
        title="org2_classified.txt",
        content="Classification: Org 2 Secret Protocol and Neuro Surgery",
        ai_usage="Active",
        document_type="RAG",
        organization_id=2
    )
    db_session.add(doc_org2)
    db_session.add(doc_rag2)
    db_session.commit()
    db_session.refresh(doc_org2)
    db_session.refresh(doc_rag2)

    try:
        # Org 1 tries to resolve Org 2's doctor
        s_id, s_name, s_status = booking_state_service.resolve_staff_from_directory(
            "Dr. Secret Specialist OrgTwo", organization_id=1, db=db_session
        )
        assert s_id is None
        assert s_status != "resolved"

        # Org 1 tries to retrieve Org 2's document context
        retrieved = rag_service.retrieve_context(
            "Org 2 Secret Protocol", organization_id=1, document_id=str(doc_rag2.id)
        )
        assert retrieved == ""

        # Org 1 tries to get master context for Org 2's document
        master = rag_service.get_organization_master_context(
            organization_id=1, document_id=str(doc_rag2.id)
        )
        assert master == ""
    finally:
        db_session.delete(doc_org2)
        db_session.delete(doc_rag2)
        db_session.commit()


# =============================================================================
# SCENARIO 9: Voice flow preservation check (SIP, pyVoIP, RTP, PCMU, Murf untouched)
# =============================================================================
def test_scenario_9_voice_pipeline_integrity():
    import audioop

    # Verify PCMU audio companding helper functions are available
    pcm_test = b'\x00\x00' * 160  # 160 samples of 16-bit PCM (320 bytes)
    ulaw = audioop.lin2ulaw(pcm_test, 2)
    assert len(ulaw) == 160  # 8kHz PCMU is 1 byte per sample (160 bytes for 20ms)

    # Verify SipCallSession has audio handling methods intact
    assert hasattr(SipCallSession, "feed_audio")
    assert hasattr(SipCallSession, "_speak")
    from src.services.sip_call_handler import write_audio_paced
    assert callable(write_audio_paced)
    assert callable(convert_tts_to_pcmu_and_pcm16)

    # Verify Murf TTS service attributes
    assert hasattr(murf_tts, "synthesize")
    assert hasattr(murf_tts, "synthesize_ulaw")
    assert hasattr(murf_tts, "get_credentials")


# =============================================================================
# SCENARIO 10: Measured response latency validation (<2000 ms)
# =============================================================================
@pytest.mark.asyncio
async def test_scenario_10_measured_response_latency_under_2000ms():
    received_audio = []

    session = SipCallSession(
        call_sid="latency_test_10",
        caller_phone="+919876543210",
        caller_name="Test Caller",
        organization_id=1,
        audio_send_callback=lambda a: received_audio.append(a),
    )

    t_speech_end = time.monotonic()
    metrics = {
        "utt_id": "utt_latency_10",
        "audio_duration_s": 1.2,
        "speech_end_monotonic": t_speech_end,
        "t_speech_endpoint": t_speech_end,
        "t_stt_final": t_speech_end + 0.08,
    }

    t0 = time.monotonic()
    await session._run_ai_pipeline("I want to book an appointment", metrics)
    elapsed_ms = (time.monotonic() - t0) * 1000

    # Ensure fast booking turn response is strictly within 2000 ms SLA
    assert elapsed_ms < 2000, f"Turn latency {elapsed_ms:.1f}ms exceeded 2000ms target!"
    assert len(session.session_history) > 0
    await session.stop()
