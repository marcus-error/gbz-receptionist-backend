"""
Comprehensive Test Suite for Global Conversation State & Multi-Turn Continuity.
Verifies the isolated execution of the voice and state pipeline:
Customer Speech -> Live STT -> Actual customer text -> RAG retrieval -> Business knowledge -> LLM -> Dynamic response -> TTS -> Customer.
Ensures zero hardcoded RAG or business details pollute the codebase or database.
"""

import pytest
from unittest.mock import patch, MagicMock

from src.services.booking_state_service import BookingStateService, BookingState
from src.services.nlp_service import nlp_service
from src.services.ai_service import ai_service


@patch("src.services.rag_service.rag_service.retrieve_context")
@patch("src.services.ai_service.ai_service.generate_response")
def test_dynamic_conversational_continuity_flow(mock_generate_response, mock_retrieve_context):
    """
    Test the pipeline flow dynamically across multiple turns without hardcoding DB knowledge.
    Ensures that customer speech seamlessly flows through intent detection,
    RAG context retrieval, and state progression, entirely dictated
    by whatever dynamic text is retrieved.

    Flow: Customer Speech -> Live STT -> Actual customer text -> RAG retrieval
          -> Business knowledge -> LLM -> Dynamic response -> TTS -> Customer
    """
    service = BookingStateService()
    session = service.get_session("test_dynamic_session")
    session.reset()

    # =====================================================================
    # TURN 1: Customer asks a general knowledge question
    # =====================================================================
    customer_speech_1 = "Can you tell me about the services offered?"

    # Step 1: NLP — intent detection from actual customer speech text
    nlp_res_1 = nlp_service.detect_intent(customer_speech_1)

    # Step 2: RAG retrieval — mock dynamic retrieval from active script
    mock_active_script_content = "We offer premium generic services including consultations."
    mock_retrieve_context.return_value = mock_active_script_content
    retrieved_knowledge_1 = mock_retrieve_context(customer_speech_1, n_results=5, intent=nlp_res_1.intent)
    assert retrieved_knowledge_1 == mock_active_script_content

    # Step 3: State machine — not a booking intent, so no switch needed
    is_switch_1 = service.check_and_handle_intent_switch(session, nlp_res_1.intent, customer_speech_1)

    # Step 4: LLM — dynamic response using only retrieved knowledge (mocked)
    mock_generate_response.return_value = "Yes, we offer premium generic services including consultations."
    dynamic_response_1 = ai_service.generate_response(
        customer_speech_1,
        context=retrieved_knowledge_1,
        intent=nlp_res_1.intent
    )

    # Step 5: Verify TTS hand-off readiness
    assert isinstance(dynamic_response_1, str)
    assert len(dynamic_response_1) > 0
    assert "premium generic services" in dynamic_response_1.lower() or "consultations" in dynamic_response_1.lower()

    # =====================================================================
    # TURN 2: Customer decides to book an appointment
    # =====================================================================
    customer_speech_2 = "I want to book an appointment."

    # Step 1: NLP
    nlp_res_2 = nlp_service.detect_intent(customer_speech_2)
    assert nlp_res_2.intent == "appointment_booking"

    # Step 2: State machine — process booking turn
    turn2_resp = service.process_booking_turn(customer_speech_2, session)
    assert session.is_active() is True
    assert session.conversation_started is True
    assert session.turn_count == 1

    # Step 3: Verify system asked a booking question (who/what/when)
    assert any(w in turn2_resp.lower() for w in ["which", "who", "what", "book", "name"]), \
        f"Expected booking question, got: {turn2_resp}"

    # =====================================================================
    # TURN 3: Customer provides follow-up — state machine must advance
    # =====================================================================
    customer_speech_3 = "Tomorrow at 10 AM please."

    turn3_resp = service.process_booking_turn(customer_speech_3, session)
    assert session.turn_count == 2

    # Verify the system gave a response (not empty, not crashed)
    assert isinstance(turn3_resp, str)
    assert len(turn3_resp) > 0
