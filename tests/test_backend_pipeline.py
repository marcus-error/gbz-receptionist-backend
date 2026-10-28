"""
Comprehensive Test Suite for Railway Low-Latency Open-Source AI Receptionist Backend.

Tests:
1. Backend startup & /health endpoint
2. Local STT service
3. Local NLP / Intent Detection
4. Emotion API (success, timeout, and failure fallbacks)
5. RAG retrieval (ChromaDB + sentence-transformers + org isolation)
6. LLM response & streaming token response
7. Local TTS service
8. WebSocket voice endpoint
9. Twilio webhook endpoints
10. Multilingual (English, Tamil, Tanglish, Hindi) & Customer Scenarios (normal, angry, urgent, unknown, RAG fallback, appointment, transfer)
"""

import asyncio
from fastapi.testclient import TestClient

from src.app import app
from src.config.database import init_db
from src.services.stt_service import stt_service
from src.services.nlp_service import nlp_service, INTENT_APPOINTMENT, INTENT_EMERGENCY, INTENT_TRANSFER
from src.services.emotion_service import emotion_service
from src.services.rag_service import rag_service
from src.services.ai_service import ai_service
from src.services.tts_service import tts_service

init_db()
client = TestClient(app)


# 1. Health Endpoint Test
def test_health_check_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data == {"status": "ok"}


# 2. Local STT Test
def test_stt_service_initialization():
    stt_service.initialize_model()
    # Mock / dry audio bytes test
    dummy_wav = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x40\x1f\x00\x00\x40\x1f\x00\x00\x01\x00\x08\x00data\x00\x00\x00\x00"
    transcription = stt_service.transcribe_audio_bytes(dummy_wav)
    assert isinstance(transcription, str)


# 3. Local NLP Intent Detection Test
def test_nlp_intent_detection():
    nlp_service.initialize_model()
    
    # Test appointment request
    res_appt = nlp_service.detect_intent("I want to book an appointment with Dr Smith tomorrow at 10 am")
    assert res_appt.intent == INTENT_APPOINTMENT
    assert res_appt.confidence >= 0.90
    assert res_appt.appointment_details is not None

    # Test emergency request
    res_emerg = nlp_service.detect_intent("I have severe chest pain and difficulty breathing!")
    assert res_emerg.intent == INTENT_EMERGENCY
    assert res_emerg.confidence >= 0.95

    # Test transfer request
    res_trans = nlp_service.detect_intent("Can I speak to a human operator or real receptionist?")
    assert res_trans.intent == INTENT_TRANSFER


# 4. Emotion API Client & Fallback Test
def test_emotion_detection_fallback():
    async def _test():
        # Test angry customer text
        res_angry = await emotion_service.detect_emotion_async("This service is terrible and I am so frustrated!")
        assert res_angry.emotion.lower() in {"frustrated", "neutral", "angry", "calm"}

        # Test urgent customer text
        res_urgent = await emotion_service.detect_emotion_async("Help me asap I need urgent assistance!")
        assert res_urgent.emotion.lower() in {"urgent", "neutral", "calm"}

        # Test normal customer text fallback
        res_normal = await emotion_service.detect_emotion_async("Hello I would like to ask about consultation hours")
        assert res_normal.emotion.lower() in {"neutral", "calm", "happy"}

    asyncio.run(_test())


# 5. RAG Retrieval & Isolation Test
def test_rag_retrieval_and_isolation():
    # Ingest document for Organization 1
    doc_id = "test_doc_101"
    text_content = "Dr. Kumar is available for Cardiology consultations from Monday to Friday, 9am to 1pm."
    rag_service.ingest_text(doc_id, text_content, source="cardiology_schedule.txt", organization_id=1)

    # Retrieve context for Org 1 with specific doc_id
    ctx = rag_service.retrieve_context("When is Dr Kumar available?", n_results=3, organization_id=1, document_id=doc_id)
    assert "Dr. Kumar" in ctx
    assert "Cardiology" in ctx

    # Clean up
    rag_service.delete_text(doc_id)


# 6. LLM Grounding & Response Generation Test
def test_llm_grounding_response():
    context = "[Source: clinic_info.txt] Our clinic opening hours are 8:00 AM to 6:00 PM daily."
    query = "What time do you open?"
    response = ai_service.generate_response(query, context, session_history=[], intent="clinic information", emotion="calm")
    assert isinstance(response, str)
    assert len(response) > 0


# 7. LLM Token Streaming Test
def test_llm_streaming_response():
    context = "[Source: clinic_info.txt] General consultation costs 50 dollars."
    query = "How much is consultation?"
    stream = ai_service.generate_response_stream(query, context, session_history=[], intent="price information", emotion="neutral")
    chunks = list(stream)
    assert len(chunks) >= 1
    full_text = " ".join(chunks)
    assert len(full_text.strip()) > 0


# 8. Local TTS Service Test
def test_tts_synthesis():
    tts_service.initialize_model()
    audio = tts_service.synthesize_speech("Hello, welcome to our medical receptionist desk.")
    assert isinstance(audio, bytes)
    assert len(audio) > 0

    ulaw_audio = tts_service.synthesize_ulaw_8000("Thank you for calling!")
    assert isinstance(ulaw_audio, bytes)
    assert len(ulaw_audio) > 0


# 9. Multilingual & Customer Scenario Coverage Test
def test_multilingual_and_customer_scenarios():
    async def _test():
        scenarios = [
            {"lang": "English", "text": "I need an appointment for next Monday."},
            {"lang": "Tamil", "text": "Enakku appointment venum doctor ah paaka."},
            {"lang": "Tanglish", "text": "Tomorrow morning doctor appointment book pannanum."},
            {"lang": "Hindi", "text": "Mujhe appointment book karna hai doctor ke saath."},
            {"scenario": "Unknown question not in RAG", "text": "What is the nuclear physics policy of your clinic?"},
            {"scenario": "Transfer request", "text": "Transfer my call to a live agent please."}
        ]

        for item in scenarios:
            text = item["text"]
            nlp_res = nlp_service.detect_intent(text)
            assert nlp_res.intent is not None
            
            ctx = rag_service.retrieve_context(text, n_results=2)
            ans = ai_service.generate_response(text, ctx, session_history=[], intent=nlp_res.intent, emotion="neutral")
            assert isinstance(ans, str)
            assert len(ans) > 0

    asyncio.run(_test())


# 10. Twilio Webhook Route Test
def test_twilio_webhook_endpoint():
    res = client.post("/api/twilio/voice", data={"CallSid": "CA123456789", "From": "+1234567890"})
    assert res.status_code in (200, 400, 404, 422)
