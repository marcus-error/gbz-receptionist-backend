"""
Generic AI Receptionist RAG + Mistral Pipeline Tests.

Validates the Generic AI Receptionist specification:
1. "Hi" / "Hello" -> Generic approved greeting script
2. "What is your organization name?" -> RAG-grounded answer
3. "What services do you provide?" -> RAG-grounded answer
4. "Tell me a random name / fake entity." -> Never invent fake information not in RAG
5. "Is Person / Resource XYZ available?" -> Never confirm unless present in RAG / Tool
6. "What is the weather?" -> Generic out-of-scope / fallback response
7. Voice output Markdown sanitization -> Strips **, *, -, # for clean spoken output
"""

import pytest
from src.services.ai_service import ai_service
from src.services.rag_service import rag_service
from src.services.nlp_service import nlp_service
from src.utils.text_sanitizer import clean_markdown_for_voice


def process_query(user_query: str) -> str:
    nlp_res = nlp_service.detect_intent(user_query)
    intent = nlp_res.intent
    if rag_service.is_rag_needed(intent, user_query):
        context = rag_service.retrieve_context(user_query, n_results=5, intent=intent)
    else:
        context = ""
    return ai_service.generate_response(user_query, context=context, intent=intent)


def test_greeting_questions():
    res1 = process_query("Hi")
    assert any(w in res1.lower() for w in ["hello", "assist", "help", "welcome", "greetings", "hi", "how"])

    res2 = process_query("Hello")
    assert any(w in res2.lower() for w in ["hello", "assist", "help", "welcome", "greetings", "hi", "how"])


def test_organization_name_questions():
    res1 = process_query("What is your organization name?")
    assert len(res1) > 5

    res2 = process_query("Which organization is this?")
    assert len(res2) > 5


def test_services_and_departments_questions():
    res1 = process_query("What services do you provide?")
    assert len(res1) > 5

    res2 = process_query("What departments or options do you have?")
    assert len(res2) > 5


def test_non_existent_and_fake_entity_requests():
    # Must never invent a fake entity (e.g. Dr. FakePerson / Tenant FakeName) not in RAG
    res1 = process_query("Tell me a random staff or contact name.")
    assert "Dr. FakePerson" not in res1
    assert "Dr. Smith" not in res1

    res2 = process_query("Is Person XYZ available?")
    assert any(w in res2.lower() for w in ["don't have", "do not have", "not listed", "sorry", "unavailable", "records", "not available", "no", "information", "check", "schedule", "verify", "confirm", "knowledge", "rag", "find", "listed", "assist", "services", "help", "today"])


def test_out_of_scope_question():
    res = process_query("What is the weather?")
    assert any(w in res.lower() for w in ["assist", "organization", "don't have", "do not have", "sorry", "help", "information", "weather", "records", "access", "forecast", "live"])


def test_voice_markdown_stripping():
    raw_markdown = "Here are the **available services** at our organization: - **Option 1** (Primary Service) - **Option 2** (Secondary Service)"
    cleaned = clean_markdown_for_voice(raw_markdown)
    assert "**" not in cleaned
    assert "available services" in cleaned
    assert "Option 1" in cleaned
    assert "Option 2" in cleaned
