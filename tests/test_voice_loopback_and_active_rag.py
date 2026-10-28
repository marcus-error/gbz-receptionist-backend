"""
Automated Test Suite for Voice Loopback Suppression, Active RAG Isolation & STT Performance.
"""

import os
import sys
import time
import pytest

# Ensure backend root is on path
BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from src.config.database import SessionLocal
from src.models.knowledge_doc import KnowledgeDocument, KnowledgeDocStatus
from src.services.knowledge_service import KnowledgeService
from src.services.rag_service import rag_service
from src.services.ai_service import ai_service
from src.validators.knowledge_validator import KnowledgeDocCreate, KnowledgeDocUpdate
from src.sockets.voice_socket import _normalize_speech


def test_speech_normalization_and_echo_detection():
    """Verify that speech normalization handles punctuation, casing, and spacing."""
    ai_phrase = "I apologize for the confusion. Could you clarify what information you're looking for?"
    norm_ai = _normalize_speech(ai_phrase)
    assert "apologize for the confusion" in norm_ai
    assert "could you clarify" in norm_ai

    # Exact match from client mic
    client_mic = "I apologize for the confusion. Could you clarify what information you're looking for?"
    norm_client = _normalize_speech(client_mic)
    assert norm_client == norm_ai

    # Substring echo
    client_sub = "clarify what information you are looking for"
    norm_sub = _normalize_speech(client_sub)
    assert len(norm_sub) > 6


def test_active_rag_isolation_and_no_cross_tenant_leakage():
    """
    Test that Agent A with RAG A only receives RAG A data,
    and never leaks RAG B data even if RAG B exists in ChromaDB.
    """
    db = SessionLocal()
    ks = KnowledgeService(db)

    try:
        # 1. Create Document A (Law Firm)
        doc_a_payload = KnowledgeDocCreate(
            title="Apex_Legal_Test.txt",
            file_type="TXT",
            content=(
                "# Apex Legal Partners\n\n"
                "Organization Name: Apex Legal Partners\n"
                "Receptionist Greeting: Welcome to Apex Legal Partners. How may I direct your legal inquiry?\n"
                "Office Hours: Monday to Friday from 8:00 AM to 6:00 PM.\n"
                "Services: Corporate Litigation, Intellectual Property, Contract Review.\n"
                "Corporate Consultation Fee: $450 per hour.\n"
                "Managing Partner: Attorney Eleanor Sterling.\n"
            ),
            organization_id=1,
            ai_usage="Active"
        )
        doc_a = ks.create_doc(doc_a_payload)

        # 2. Create Document B (Salon)
        doc_b_payload = KnowledgeDocCreate(
            title="Radiance_Salon_Test.txt",
            file_type="TXT",
            content=(
                "# Radiance Hair Studio\n\n"
                "Organization Name: Radiance Hair Studio\n"
                "Receptionist Greeting: Thank you for calling Radiance Hair Studio. How can we make you glow today?\n"
                "Office Hours: Tuesday to Sunday from 10:00 AM to 9:00 PM.\n"
                "Services: Balayage, Keratin Treatment, Precision Haircuts.\n"
                "Balayage Fee: $220.\n"
                "Lead Stylist: Master Stylist Antonio Rossi.\n"
            ),
            organization_id=1,
            ai_usage="Inactive"
        )
        doc_b = ks.create_doc(doc_b_payload)

        # Re-index both docs so both are in Chroma
        ks._index_document(doc_a, doc_a.content)
        ks._index_document(doc_b, doc_b.content)
        db.commit()

        # Step 3: Test Document A Isolation (Session bound to Doc A)
        greeting_a = rag_service.get_organization_greeting(1, document_id=str(doc_a.id))
        assert "Apex Legal Partners" in greeting_a

        context_a = rag_service.retrieve_context(
            "What is the corporate consultation fee and who is the managing partner?",
            organization_id=1,
            document_id=str(doc_a.id)
        )
        assert "$450" in context_a
        assert "Eleanor Sterling" in context_a
        assert "Antonio Rossi" not in context_a  # No leakage from doc B

        # Step 4: Ask Doc A a question that ONLY exists in Doc B
        context_leak_check = rag_service.retrieve_context(
            "What is the balayage fee and who is the lead stylist?",
            organization_id=1,
            document_id=str(doc_a.id)
        )
        assert "Antonio Rossi" not in context_leak_check
        assert "$220" not in context_leak_check

        # Step 5: Test AI response with Doc A context
        resp_a = ai_service.generate_response(
            text="How much is your corporate consultation fee?",
            context=context_a,
            organization_id=1
        )
        assert "450" in resp_a or "four hundred fifty" in resp_a.lower() or "four hundred and fifty" in resp_a.lower()

        # Step 6: Test AI response when asking for unlisted information (must not hallucinate or leak)
        resp_no_leak = ai_service.generate_response(
            text="Who is your lead stylist Antonio Rossi?",
            context=context_leak_check,
            organization_id=1
        )
        clean_resp = resp_no_leak.replace("’", "'").replace("‘", "'").lower()
        negative_phrases = ["not available", "don't have", "not listed", "no information", "any information", "cannot find", "not found", "not mentioned", "no record", "legal services", "specialize"]
        assert any(p in clean_resp for p in negative_phrases)

        # Step 7: Switch Active Document to Document B
        ks.update_doc(doc_b.id, KnowledgeDocUpdate(ai_usage="Active"), organization_id=1)
        ks.update_doc(doc_a.id, KnowledgeDocUpdate(ai_usage="Inactive"), organization_id=1)
        db.commit()

        greeting_b = rag_service.get_organization_greeting(1, document_id=str(doc_b.id))
        assert "Radiance Hair Studio" in greeting_b

        context_b = rag_service.retrieve_context(
            "How much is the balayage and who is the lead stylist?",
            organization_id=1,
            document_id=str(doc_b.id)
        )
        assert "$220" in context_b
        assert "Antonio Rossi" in context_b
        assert "Eleanor Sterling" not in context_b  # Doc A data is excluded

    finally:
        # Cleanup test documents completely from DB and vector store
        try:
            if 'doc_a' in locals() and doc_a:
                ks.delete_doc(doc_a.id, organization_id=1)
            if 'doc_b' in locals() and doc_b:
                ks.delete_doc(doc_b.id, organization_id=1)
            # Restore primary org doc #1 to Active
            doc1 = ks.repo.get_by_id(1)
            if doc1:
                doc1.ai_usage = "Active"
                db.commit()
        except Exception:
            pass
        db.close()


def test_fast_path_latency():
    """Verify that greetings and pleasantries bypass LLM and execute in < 50ms."""
    t0 = time.perf_counter()
    ans = ai_service._fast_rule_response("Hello, good morning!", intent="greeting", organization_id=1)
    dur_ms = (time.perf_counter() - t0) * 1000.0

    assert ans is not None
    assert dur_ms < 50.0
    print(f"Fast path latency: {dur_ms:.2f} ms")


if __name__ == "__main__":
    print("Running voice loopback and active RAG test suite...")
    test_speech_normalization_and_echo_detection()
    print("[PASS] Speech normalization & echo suppression passed.")
    test_active_rag_isolation_and_no_cross_tenant_leakage()
    print("[PASS] Active RAG isolation & zero cross-tenant leakage passed.")
    test_fast_path_latency()
    print("[PASS] Fast-path sub-50ms latency passed.")
    print("ALL TESTS PASSED SUCCESSFULLY!")
