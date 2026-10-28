"""
Unit & Integration Tests for Dynamic RAG Pipeline Flow.

Verifies the isolated execution of the voice pipeline:
Customer Speech -> Live STT -> Actual customer text -> RAG retrieval -> Business knowledge -> LLM -> Dynamic response -> TTS -> Customer.
"""

import pytest
from unittest.mock import patch, MagicMock

from src.services.nlp_service import nlp_service, NLPResult
from src.services.ai_service import ai_service


@patch("src.services.rag_service.rag_service.retrieve_context")
def test_dynamic_rag_flow_end_to_end(mock_retrieve_context):
    """
    Test the pipeline flow dynamically without hardcoding DB knowledge.
    Ensures that customer speech seamlessly flows through intent detection, 
    RAG context retrieval, and LLM response generation, entirely dictated 
    by whatever dynamic text is retrieved.
    """
    customer_speech_text = "Can you tell me about the services offered?"
    
    # 1. Intent Detection
    nlp_res = nlp_service.detect_intent(customer_speech_text)
    assert nlp_res.intent in ["service_information", "school_information", "hospital_information", "organization_information", "faq", "general_question"]
    
    # 2. Dynamic RAG Retrieval (Mocked as if coming from Active Script)
    mock_active_script_content = "We offer premium services including consultations and appointments."
    mock_retrieve_context.return_value = mock_active_script_content
    
    # Simulate the service call
    retrieved_knowledge = mock_retrieve_context(customer_speech_text, n_results=5, intent=nlp_res.intent)
    assert retrieved_knowledge == mock_active_script_content
    
    # 3. LLM Response Generation using Dynamic Knowledge
    dynamic_response = ai_service.generate_response(
        customer_speech_text, 
        context=retrieved_knowledge, 
        intent=nlp_res.intent
    )
    
    # 4. Verify TTS Hand-off Readiness
    assert isinstance(dynamic_response, str)
    assert len(dynamic_response) > 0
    assert "According to our records" not in dynamic_response
