"""
Targeted Regression Test Suite for CallZenza Appointment Booking Flow & Doctor Resolution.

Verifies:
1. Doctor exists in the active staff directory and customer requests a 10 AM appointment.
2. Doctor is mentioned with a genuine STT spelling variation.
3. Doctor is absent from the active directory.
4. Customer explicitly says they do not want a product or service and wants to meet a doctor.
5. Customer speaks an incomplete sentence such as "Before you say the".
6. Customer changes their requested doctor.
7. Customer asks a question during WAITING_FOR_DOCTOR.
8. Organization isolation prevents selecting another tenant's staff.
9. Existing inbound/outbound calls and voice flow remain functional.
"""

import pytest
import re
from unittest.mock import MagicMock, patch

from src.config.database import SessionLocal, engine, Base
from src.models.organization import Organization
from src.models.knowledge_doc import KnowledgeDocument, KnowledgeDocStatus
from src.models.staff import Staff, StaffStatus
from src.models.appointment import Appointment, AppointmentStatus
from src.services.booking_state_service import (
    BookingStateService,
    BookingState,
    BookingSession,
)
from src.services.entity_validation_service import entity_validation_service, EntityType
from src.services.sip_call_handler import SipCallSession
from src.services.rag_service import rag_service

SARANS_SCRIPT = """
SARANS HOSPITALS - RAG-STRUCTURED INBOUND AI SCRIPT
Purpose: Patient enquiries, hospital information, treatments, doctors, services, branches, consultations, appointments.

DOCTORS & CONSULTANTS
Listed Doctors
Dr. A. Kulandai Theres
Siddha Consultant
Dr. A. P. Latha Kumari
Naturopathy & Yoga Consultant
Dr. G. Merlyn Pricilla
Siddha Consultant
Dr. K. P. Nandhini
Naturopathy & Yoga Consultant
Dr. M. Anitha
Siddha Consultant
Dr. D. Indhumathi
Siddha Consultant
Dr. P. Akhila Gladis
Ayurvedic Consultant
Dr. T. Indiradevi
Naturopathy & Yoga Consultant
Dr. M. Shiba Shree
Siddha Consultant
Dr. T. Niveditha Bharathi
Siddha Consultant

SERVICES & DEPARTMENTS
- Ayurveda
- Siddha
- Naturopathy & Yoga
- Acupuncture
- General Medicine
"""

@pytest.fixture(autouse=True)
def setup_test_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        # Ensure org 1 and org 2 exist
        org1 = db.query(Organization).filter(Organization.id == 1).first()
        if not org1:
            org1 = Organization(id=1, name="Sarans Hospitals", is_active=True)
            db.add(org1)

        org2 = db.query(Organization).filter(Organization.id == 2).first()
        if not org2:
            org2 = Organization(id=2, name="Zenza Salon & Spa", is_active=True)
            db.add(org2)

        # Seed KnowledgeDocument for Org 1 with Sarans Hospitals script
        doc1 = db.query(KnowledgeDocument).filter(KnowledgeDocument.organization_id == 1).first()
        if not doc1:
            doc1 = KnowledgeDocument(
                organization_id=1,
                title="SARANS_HOSPITALS_RAG_Inbound_Script_Fine_Tuned.pdf",
                document_type="SCRIPT",
                content=SARANS_SCRIPT,
                status=KnowledgeDocStatus.INDEXED
            )
            db.add(doc1)
        else:
            doc1.content = SARANS_SCRIPT
            doc1.status = KnowledgeDocStatus.INDEXED

        # Seed KnowledgeDocument for Org 2 with Fatima Zahra
        doc2 = db.query(KnowledgeDocument).filter(KnowledgeDocument.organization_id == 2).first()
        if not doc2:
            doc2 = KnowledgeDocument(
                organization_id=2,
                title="Zenza_Salon_Staff.pdf",
                document_type="SCRIPT",
                content="Staff Directory:\nDr. Fatima Zahra: Dermatology Consultant",
                status=KnowledgeDocStatus.INDEXED
            )
            db.add(doc2)
        else:
            doc2.content = "Staff Directory:\nDr. Fatima Zahra: Dermatology Consultant"
            doc2.status = KnowledgeDocStatus.INDEXED

        db.commit()
    finally:
        db.close()
    rag_service.clear_cache()
    yield


# ==============================================================================
# TEST 1: Doctor exists in active staff directory + 10 AM requested
# ==============================================================================
def test_doctor_exists_in_directory_with_10am_request():
    service = BookingStateService()
    session = service.get_session("test_case_1_session")
    session.reset()
    session.organization_id = 1
    session.state = BookingState.WAITING_FOR_DOCTOR
    session.last_asked_field = "doctor"
    session.date = "2026-10-26"

    resp = service.process_booking_turn("I want to meet Dr. Akhila Gladis at 10 AM", session)

    assert session.doctor is not None
    assert "Akhila Gladis" in session.doctor
    assert session.doctor_id is not None
    assert session.time == "10:00 AM"
    assert session.state == BookingState.COLLECTING_PATIENT_NAME
    assert "Which product or service" not in resp
    assert "Cardiology" not in resp
    assert "name" in resp.lower()


# ==============================================================================
# TEST 2: Doctor mentioned with genuine STT spelling variation
# ==============================================================================
def test_doctor_stt_spelling_variation():
    service = BookingStateService()
    session = service.get_session("test_case_2_session")
    session.reset()
    session.organization_id = 1
    session.state = BookingState.WAITING_FOR_DOCTOR
    session.last_asked_field = "doctor"

    # Spoken variation without title, 'Akila' instead of 'Akhila', 'Gladys' instead of 'Gladis'
    resp = service.process_booking_turn("I want to book an appointment with doctor Akila Gladys", session)

    assert session.doctor is not None
    assert "Akhila Gladis" in session.doctor
    assert session.doctor_id is not None
    assert "Which product or service" not in resp


# ==============================================================================
# TEST 3: Doctor absent from active directory
# ==============================================================================
def test_doctor_absent_from_directory():
    service = BookingStateService()
    session = service.get_session("test_case_3_session")
    session.reset()
    session.organization_id = 1
    session.state = BookingState.WAITING_FOR_DOCTOR
    session.last_asked_field = "doctor"

    resp = service.process_booking_turn("I want to see doctor Montgomery Montgomery", session)

    assert session.doctor is None
    assert session.doctor_id is None
    assert session.state == BookingState.WAITING_FOR_DOCTOR
    # Must NOT redirect to unrelated products or services
    assert "Which product or service would you like to book?" not in resp
    assert "Cardiology" not in resp
    # Explains doctor directory issue and asks for clarification
    assert "doctor" in resp.lower() or "clarify" in resp.lower() or "directory" in resp.lower()


# ==============================================================================
# TEST 4: Customer rejects products/services and requests doctor
# ==============================================================================
def test_customer_rejects_product_and_requests_doctor():
    service = BookingStateService()
    session = service.get_session("test_case_4_session")
    session.reset()
    session.organization_id = 1
    session.state = BookingState.WAITING_FOR_DOCTOR
    session.last_asked_field = "doctor"
    session.date = "2026-10-26"
    session.time = "10:00 AM"

    resp = service.process_booking_turn(
        "I don't want any product or service. I want to meet doctor Akila Gladys",
        session
    )

    assert session.doctor is not None
    assert "Akhila Gladis" in session.doctor
    assert session.doctor_id is not None
    assert session.time == "10:00 AM"
    assert session.date == "2026-10-26"
    assert "Which product or service would you like to book?" not in resp
    assert session.state == BookingState.COLLECTING_PATIENT_NAME
    assert "full name" in resp.lower()


# ==============================================================================
# TEST 5: Customer speaks incomplete sentence ("Before you say the")
# ==============================================================================
def test_incomplete_sentence_not_extracted_as_entity():
    service = BookingStateService()
    session = service.get_session("test_case_5_session")
    session.reset()
    session.organization_id = 1
    session.state = BookingState.WAITING_FOR_DOCTOR
    session.last_asked_field = "doctor"
    session.date = "2026-10-26"
    session.time = "10:00 AM"

    # Turn 1: Customer picks doctor
    resp1 = service.process_booking_turn(
        "I don't want any product or service. I want to meet doctor Akila Gladys",
        session
    )
    assert session.doctor is not None
    assert session.state == BookingState.COLLECTING_PATIENT_NAME

    # Turn 2: Customer speaks incomplete sentence / conversational interruption
    resp2 = service.process_booking_turn("Before you say the", session)

    assert "Before" not in (session.doctor or "")
    assert session.patient_name is None
    assert session.doctor is not None
    assert "Akhila Gladis" in session.doctor
    assert session.time == "10:00 AM"
    assert session.state == BookingState.COLLECTING_PATIENT_NAME
    assert "Which product or service would you like to book?" not in resp2
    assert "Cardiology" not in resp2
    assert "time" in resp2.lower() or "ready" in resp2.lower() or "name" in resp2.lower()


# ==============================================================================
# TEST 6: Customer changes their requested doctor
# ==============================================================================
def test_customer_changes_requested_doctor():
    service = BookingStateService()
    session = service.get_session("test_case_6_session")
    session.reset()
    session.organization_id = 1
    session.state = BookingState.WAITING_FOR_DOCTOR
    session.last_asked_field = "doctor"
    session.date = "2026-10-26"

    # Initially request Dr. Kulandai Theres
    resp1 = service.process_booking_turn("I want to see Dr. Kulandai Theres", session)
    doc1 = session.doctor
    id1 = session.doctor_id

    # Now change to Dr. Akhila Gladis
    resp2 = service.process_booking_turn("Actually, change doctor to Dr. Akhila Gladis", session)

    assert session.doctor is not None
    assert "Akhila Gladis" in session.doctor
    assert session.doctor != doc1
    assert session.doctor_id != id1


# ==============================================================================
# TEST 7: Customer asks an informational question during WAITING_FOR_DOCTOR
# ==============================================================================
def test_informational_question_during_waiting_for_doctor():
    service = BookingStateService()
    session = service.get_session("test_case_7_session")
    session.reset()
    session.organization_id = 1
    session.state = BookingState.WAITING_FOR_DOCTOR
    session.last_asked_field = "doctor"
    session.time = "10:00 AM"

    # Customer asks about hospital timings
    is_switch = service.check_and_handle_intent_switch(
        session,
        current_intent="hospital_information",
        text="What are your hospital working hours?"
    )

    assert is_switch is True
    # Session state fields must remain preserved for return
    assert session.time == "10:00 AM"
    assert session.state == BookingState.WAITING_FOR_DOCTOR


# ==============================================================================
# TEST 8: Organization isolation prevents selecting another tenant's staff
# ==============================================================================
def test_organization_isolation_staff_resolution():
    service = BookingStateService()
    db = SessionLocal()
    try:
        # Organization 1 (Sarans Hospitals) lookup for Dr. Akhila Gladis -> should resolve
        s_id_1, s_name_1, status_1 = service.resolve_staff_from_directory(
            "Dr. Akhila Gladis", organization_id=1, db=db
        )
        assert status_1 == "resolved"
        assert s_id_1 is not None

        # Organization 2 (Zenza Salon) lookup for Dr. Akhila Gladis -> must NOT resolve
        s_id_2, s_name_2, status_2 = service.resolve_staff_from_directory(
            "Dr. Akhila Gladis", organization_id=2, db=db
        )
        assert status_2 != "resolved"
        assert s_id_2 is None
    finally:
        db.close()


# ==============================================================================
# TEST 9: Existing inbound/outbound calls and voice flow remain functional
# ==============================================================================
def test_voice_and_sip_session_intact():
    session = SipCallSession(call_sid="TEST_CALLZENZA_VOICE_001")
    assert session.ended is False
    assert session.call_sid == "TEST_CALLZENZA_VOICE_001"
    assert hasattr(session, "feed_audio")
    assert hasattr(session, "process_audio_loop")
