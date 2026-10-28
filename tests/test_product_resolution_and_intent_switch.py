import pytest
from unittest.mock import patch, MagicMock

from src.services.booking_state_service import (
    booking_state_service,
    BookingSession,
    BookingState,
)
from src.services.entity_validation_service import entity_validation_service, EntityType
from src.services.calendar_verification_service import CalendarVerificationResult
from src.config.database import SessionLocal
from src.models.knowledge_doc import KnowledgeDocument, KnowledgeDocStatus
from src.services.rag_service import rag_service
from src.services.cache_service import cache_service


@pytest.fixture(autouse=True)
def setup_test_knowledge_base():
    """
    Seeds dynamic product suite document in the isolated test database for Organization 1.
    Ensures active RAG extracts Call Zenza, Galaxy Chat, AI Receptionist, and Aminoid AI.
    """
    rag_service.clear_cache()
    cache_service.clear_all()

    with SessionLocal() as db:
        db.query(KnowledgeDocument).filter(KnowledgeDocument.organization_id == 1).delete()

        doc = KnowledgeDocument(
            id=101,
            organization_id=1,
            title="Aminoid_AI_Product_Suite.pdf",
            document_type="RAG",
            scope="inbound",
            ai_usage="Active",
            status=KnowledgeDocStatus.INDEXED,
            content=(
                "Aminoid AI Enterprise Knowledge Base\n\n"
                "# PRODUCT SUITE\n"
                "- Call Zenza: Intelligent Voice Agent for Inbound & Outbound Calling\n"
                "- Galaxy Chat: Enterprise Omnichannel Conversational Platform\n"
                "- AI Receptionist: Automated Medical and Enterprise Receptionist\n"
                "- Aminoid AI: Core Multi-Agent Orchestration Engine\n"
            ),
        )
        db.add(doc)
        db.commit()

    rag_service.clear_cache()
    yield

    with SessionLocal() as db:
        db.query(KnowledgeDocument).filter(KnowledgeDocument.organization_id == 1).delete()
        db.commit()
    rag_service.clear_cache()
    cache_service.clear_all()


class TestProductResolutionAndIntentSwitch:
    """
    Regression test suite for dynamic product/service resolution, negation handling,
    ambiguity confirmation, and seamless intent switching between booking and general inquiries.
    """

    def test_direct_product_request_resolves_and_prompts_for_date(self):
        """
        Caller directly requests a dynamic product: 'I want to book Galaxy Chat'.
        Should resolve entity as PRODUCT without requiring a doctor staff_id,
        set session.service = 'Galaxy Chat', and advance state to WAITING_FOR_DATE.
        """
        session = BookingSession("test-direct-product", organization_id=1)
        resp = booking_state_service.process_booking_turn("I want to book Galaxy Chat", session)

        assert session.service == "Galaxy Chat"
        assert session.state == BookingState.WAITING_FOR_DATE
        assert "date" in resp.lower() or "when" in resp.lower()
        # Ensure it does NOT ask for a doctor or echo stale phrases
        assert "doctor" not in resp.lower()
        assert "which galaxy chat" not in resp.lower()

    def test_negation_and_correction_in_single_turn(self):
        """
        Caller states: 'I don't want Galaxy Jet; I want Galaxy Chat'.
        Should reject 'Galaxy Jet', verify and bind 'Galaxy Chat', and prompt for date.
        """
        session = BookingSession("test-negation-turn", organization_id=1)
        resp = booking_state_service.process_booking_turn(
            "I don't want Galaxy Jet; I want Galaxy Chat", session
        )

        assert "galaxy jet" in session._rejected_services
        assert session.service == "Galaxy Chat"
        assert session.state == BookingState.WAITING_FOR_DATE
        assert "galaxy jet" not in resp.lower()
        assert "Galaxy Chat" in resp

    def test_ambiguous_entity_prompts_confirmation_without_stale_echo(self):
        """
        Caller states: 'I want to book Galaxy Jet'.
        Since Galaxy Jet is not in the directory but fuzzy matches Galaxy Chat (similarity ~0.73),
        the system should identify it as ambiguous and ask: 'Did you mean Galaxy Chat?'.
        Crucially, it MUST NEVER echo: 'Which galaxy jet would you like to book?'.
        """
        session = BookingSession("test-ambiguous-entity", organization_id=1)
        resp = booking_state_service.process_booking_turn("I want to book Galaxy Jet", session)

        assert "which galaxy jet" not in resp.lower()
        assert "did you mean" in resp.lower()
        assert "Galaxy Chat" in resp
        assert session.booking_data.get("pending_confirmation_entity") == "Galaxy Chat"

    def test_affirmative_response_confirms_suggested_entity(self):
        """
        Following ambiguity clarification ('Did you mean Galaxy Chat?'),
        if caller answers 'Yes', the system confirms Galaxy Chat and advances to WAITING_FOR_DATE.
        """
        session = BookingSession("test-confirm-suggested", organization_id=1)
        # Turn 1: ambiguous request
        booking_state_service.process_booking_turn("I want to book Galaxy Jet", session)
        assert session.booking_data.get("pending_confirmation_entity") == "Galaxy Chat"

        # Turn 2: caller confirms
        resp2 = booking_state_service.process_booking_turn("Yes, that's right", session)
        assert session.service == "Galaxy Chat"
        assert session.state == BookingState.WAITING_FOR_DATE
        assert "Galaxy Chat" in resp2
        assert "date" in resp2.lower() or "when" in resp2.lower()

    def test_product_info_inquiry_switches_intent_during_booking(self):
        """
        Caller changes topic to product inquiry during booking:
        'No, I don't want Galaxy Jet. I want to know about Galaxy Chat.'
        check_and_handle_intent_switch should return True, clear obsolete booking slots,
        and save last_inquired_product.
        """
        session = BookingSession("test-info-switch", organization_id=1)
        session.state = BookingState.WAITING_FOR_DOCTOR
        session.booking_data["user_requested_entity_term"] = "galaxy jet"

        text = "No, I don't want Galaxy Jet. I want to know about Galaxy Chat."
        switched = booking_state_service.check_and_handle_intent_switch(session, "general_question", text)

        assert switched is True
        assert "galaxy jet" in session._rejected_services
        assert session.booking_data.get("last_inquired_product") == "Galaxy Chat"
        assert session.booking_data.get("user_requested_entity_term") is None
        assert session.booking_data.get("unverified_entity_requested") is None

    def test_switching_back_to_booking_binds_inquired_product(self):
        """
        After asking about Galaxy Chat, caller switches back to booking:
        'Yeah, I want to book an appointment today'.
        System should adopt the inquired product ('Galaxy Chat'), extract date ('today'),
        and ask for time without asking for a doctor.
        """
        session = BookingSession("test-switch-back", organization_id=1)
        session.booking_data["last_inquired_product"] = "Galaxy Chat"
        session.state = BookingState.WAITING_FOR_DOCTOR

        text = "Yeah, I want to book an appointment today"
        switched = booking_state_service.check_and_handle_intent_switch(session, "appointment_booking", text)
        assert switched is False

        resp = booking_state_service.process_booking_turn(text, session)
        assert session.service == "Galaxy Chat"
        assert session.date is not None
        assert session.state == BookingState.WAITING_FOR_TIME
        assert "doctor" not in resp.lower()

    def test_full_call_231_conversational_lifecycle(self):
        """
        End-to-end replay of CallLog 231 conversational flow:
        Turn 1: Greeting
        Turn 2: 'I want to book Galaxy Jet' -> Ambiguity confirmation (Did you mean Galaxy Chat?)
        Turn 3: 'No, I don't want Galaxy Jet. I want to know about Galaxy Chat.' -> Intent switch to RAG
        Turn 4: 'Yeah, I want to book an appointment today' -> Resumes booking with Galaxy Chat
        Turn 5: '10 AM' -> Live verification and prompts for patient name
        """
        session = BookingSession("test-call-231-e2e", organization_id=1)

        # Turn 1
        sw1 = booking_state_service.check_and_handle_intent_switch(session, "greeting", "Hello, good morning.")
        assert sw1 is False

        # Turn 2
        sw2 = booking_state_service.check_and_handle_intent_switch(session, "appointment_booking", "I want to book Galaxy Jet")
        assert sw2 is False
        r2 = booking_state_service.process_booking_turn("I want to book Galaxy Jet", session)
        assert "did you mean 'galaxy chat'" in r2.lower()
        assert "which galaxy jet" not in r2.lower()

        # Turn 3
        t3 = "No, I don't want Galaxy Jet. I want to know about Galaxy Chat."
        sw3 = booking_state_service.check_and_handle_intent_switch(session, "general_question", t3)
        assert sw3 is True
        assert "galaxy jet" in session._rejected_services
        assert session.booking_data.get("last_inquired_product") == "Galaxy Chat"

        # Turn 4
        t4 = "Yeah, I want to book an appointment today"
        sw4 = booking_state_service.check_and_handle_intent_switch(session, "appointment_booking", t4)
        assert sw4 is False
        r4 = booking_state_service.process_booking_turn(t4, session)
        assert session.service == "Galaxy Chat"
        assert session.date is not None
        assert "galaxy jet" not in r4.lower()

        # Turn 5
        with patch("src.services.calendar_verification_service.calendar_verification_service.verify_availability") as mock_cal:
            mock_cal.return_value = CalendarVerificationResult(is_checked=True, is_available=True)
            r5 = booking_state_service.process_booking_turn("10 AM", session)
            assert session.time == "10:00 AM"
            assert session.state == BookingState.COLLECTING_PATIENT_NAME
            assert "name" in r5.lower()
            assert "Galaxy Chat" in r5
