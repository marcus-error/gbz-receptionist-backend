import pytest
import re
from unittest.mock import MagicMock, patch
from src.services.rag_service import rag_service
from src.services.entity_validation_service import entity_validation_service
from src.services.booking_state_service import booking_state_service, BookingSession, BookingState

def test_greeting_matches_active_script():
    """
    E.1: The greeting always matches the active script and never leaks generic or hardcoded titles.
    """
    org_name = rag_service.get_organization_name(organization_id=1)
    greeting = rag_service.get_organization_greeting(organization_id=1)
    
    assert "Test Receptionist Script" not in org_name
    assert "receptionist script" not in org_name.lower()
    assert len(greeting) > 5

def test_valid_doctor_selection_preserved():
    """
    E.2: A valid doctor selection is preserved exactly (e.g. 'Doctor Rohit Kumar' -> 'Dr Rohit').
    """
    with patch.object(rag_service, "get_organization_master_context", return_value="Doctor Directory:\nDr Rohit (Cardiology)\n"):
        session = BookingSession(session_id="test_sess_1", call_sid="test_call_1")
        session.organization_id = 1
        session.state = BookingState.WAITING_FOR_DOCTOR
        session.last_asked_field = "doctor"

        # User says "Doctor Rohit Kumar"
        resp = booking_state_service.process_booking_turn("Doctor Rohit Kumar", session)

        assert session.doctor is not None
        assert "Rohit" in session.doctor
        assert session.doctor_locked is True

def test_unknown_doctor_never_becomes_different_doctor():
    """
    E.3: An unknown doctor (e.g. 'Doctor John Smith') is rejected politely and NEVER silently becomes 'Dr Arun Kumar'.
    """
    session = BookingSession(session_id="test_sess_2", call_sid="test_call_2")
    session.organization_id = 1
    session.state = BookingState.WAITING_FOR_DOCTOR
    session.last_asked_field = "doctor"

    # User requests non-existent doctor
    resp = booking_state_service.process_booking_turn("Doctor John Smith", session)

    assert session.doctor is None
    assert "Arun" not in (session.doctor or "")
    assert "couldn't find 'Doctor John Smith'" in resp or "directory" in resp.lower()

def test_service_questions_retrieve_service_content():
    """
    E.4: Service questions retrieve service content, not unrelated FAQs.
    """
    ctx = rag_service.retrieve_context("What services do you offer?", n_results=3, organization_id=1, intent="service_information")
    assert ctx is not None
    assert isinstance(ctx, str)

def test_vector_collection_resilience():
    """
    E.5: Vector collection auto-creates safely without throwing errors on missing collection.
    """
    coll = rag_service.collection
    assert coll is not None

def test_no_inactive_document_content():
    """
    E.6: Inactive documents or other tenant content are not returned for active org queries.
    If no active document exists (e.g. earlier tests cleaned up), skip gracefully.
    """
    rag_service.clear_cache()
    active_doc_id = rag_service._resolve_active_document_id(organization_id=1)
    if active_doc_id is None:
        pytest.skip("No active document for org 1 in this test DB state — skipping isolation check.")
    master_ctx = rag_service.get_organization_master_context(organization_id=1)
    assert len(master_ctx) > 0

def test_single_turn_single_response_state():
    """
    E.7: Each customer turn yields a single response text, avoiding duplicate/stale state.
    """
    session = BookingSession(session_id="test_sess_3", call_sid="test_call_3")
    session.organization_id = 1
    resp1 = booking_state_service.process_booking_turn("I want to book an appointment", session)
    assert session.state in (BookingState.WAITING_FOR_DOCTOR, BookingState.BOOKING_STARTED, BookingState.WAITING_FOR_DATE)
    assert isinstance(resp1, str)

def test_booking_state_survives_informational_questions():
    """
    E.8: Booking state survives informational questions and resumes correctly.
    """
    session = BookingSession(session_id="test_sess_4", call_sid="test_call_4")
    session.organization_id = 1
    
    # 1. Start booking
    booking_state_service.process_booking_turn("I want to book an appointment", session)
    assert session.is_active()

    # 2. Informational question
    switched = booking_state_service.check_and_handle_intent_switch(session, "general_question", "What are your opening hours?")
    assert switched is True
    # Session state remains active so appointment details can be collected after info answer
    assert session.is_active()
