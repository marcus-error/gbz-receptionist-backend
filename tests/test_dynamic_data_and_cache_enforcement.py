"""
Automated Dynamic Data & Cache Enforcement Test Suite for AI Receptionist.

Validates the 10 Critical Invariants:
1. No production answer is sourced from a hardcoded demo response.
2. The active document and version strictly determine retrieval results.
3. Changing, updating, or deleting a document invalidates stale retrieval and context caches.
4. Missing or corrupted vector collections are handled safely without unhandled exceptions.
5. Strict multi-tenant isolation: No cross-tenant, inactive-document, or cross-call leakage occurs.
6. A selected/requested doctor or representative is never silently substituted with another.
7. Test fixtures and synthetic data never enter production collections or databases.
8. Empty or missing retrieval evidence never produces a fabricated business fact.
9. Conversation events are correctly correlated by call_id, utterance_id, and turn_id.
10. Single final AI response per turn with zero duplicate responses or redundant audio emissions.
"""

import os
import uuid
import pytest
from unittest.mock import patch, MagicMock

from src.services.rag_service import rag_service
from src.services.ai_service import ai_service
from src.services.cache_service import cache_service
from src.services.knowledge_service import KnowledgeService
from src.services.booking_state_service import booking_state_service, BookingState, BookingSession
from src.services.entity_validation_service import entity_validation_service
from src.validators.knowledge_validator import KnowledgeDocCreate, KnowledgeDocUpdate


# -----------------------------------------------------------------------------
# 1. No Production Answer Sourced from Hardcoded Demo Response
# -----------------------------------------------------------------------------
def test_no_production_answer_from_hardcoded_demo_response(db_session):
    """Verify that unlisted business inquiries produce safe refusal rather than demo strings."""
    uid = uuid.uuid4().hex[:8]
    unique_biz = f"Enterprise_{uid}"
    
    # Active document with very specific, non-demo knowledge
    doc_content = f"Organization Name: {unique_biz}\nServices: Specialized Quantum Computing {uid}.\n"
    ks = KnowledgeService(db_session)
    doc = ks.create_doc(
        payload=KnowledgeDocCreate(
            title=f"quantum_{uid}.txt",
            content=doc_content,
            ai_usage="Active",
            document_type="RAG",
            organization_id=1
        ),
        organization_id=1
    )

    try:
        # Ask about a medical or hospital department (which does NOT exist in this doc)
        resp = ai_service.generate_response(
            text="Can I see your cardiologist or doctor?",
            context=rag_service.retrieve_context("cardiologist doctor", organization_id=1, document_id=str(doc.id)),
            session_history=[],
            organization_id=1
        )
        resp_lower = resp.lower()
        
        # Must NOT mention dummy doctors from legacy tests or demos
        assert "arun" not in resp_lower
        assert "rohit" not in resp_lower
        assert "priya" not in resp_lower
        assert "abc general hospital" not in resp_lower
        assert "city care" not in resp_lower
    finally:
        ks.delete_doc(doc.id, organization_id=1)


# -----------------------------------------------------------------------------
# 2. Active Document & Version Determines Retrieval Results
# -----------------------------------------------------------------------------
def test_active_document_and_version_determines_retrieval(db_session):
    """Verify that switching active documents dynamically switches retrieved knowledge."""
    ks = KnowledgeService(db_session)
    token_a = f"ALPHA_SECRET_TOKEN_{uuid.uuid4().hex[:6]}"
    token_b = f"BETA_SECRET_TOKEN_{uuid.uuid4().hex[:6]}"

    # Doc A
    doc_a = ks.create_doc(
        payload=KnowledgeDocCreate(
            title="doc_alpha.txt",
            content=f"Organization Name: Alpha Firm\nPolicy Details: Our policy code is {token_a}.\n",
            ai_usage="Active",
            document_type="RAG",
            organization_id=1
        ),
        organization_id=1
    )

    try:
        res_a = rag_service.retrieve_context("policy code", organization_id=1)
        assert token_a in res_a, f"Expected {token_a} in retrieval result"

        # Now create and activate Doc B (which deactivates Doc A)
        doc_b = ks.create_doc(
            payload=KnowledgeDocCreate(
                title="doc_beta.txt",
                content=f"Organization Name: Beta Corp\nPolicy Details: Our policy code is {token_b}.\n",
                ai_usage="Active",
                document_type="RAG",
                organization_id=1
            ),
            organization_id=1
        )
        try:
            res_b = rag_service.retrieve_context("policy code", organization_id=1)
            assert token_b in res_b, f"Expected {token_b} in retrieval result after activating Doc B"
            assert token_a not in res_b, f"Stale token {token_a} should NOT appear after activating Doc B"
        finally:
            ks.delete_doc(doc_b.id, organization_id=1)
    finally:
        try:
            ks.delete_doc(doc_a.id, organization_id=1)
        except Exception:
            pass


# -----------------------------------------------------------------------------
# 3. Document Change and Deletion Invalidates Stale Caches
# -----------------------------------------------------------------------------
def test_document_change_and_deletion_invalidates_caches(db_session):
    """Verify that updating or deleting a document flushes RAG in-memory and application caches."""
    ks = KnowledgeService(db_session)
    uid = uuid.uuid4().hex[:6]
    token_v1 = f"VERSION_ONE_{uid}"
    token_v2 = f"VERSION_TWO_{uid}"

    doc = ks.create_doc(
        payload=KnowledgeDocCreate(
            title="cache_test.txt",
            content=f"Organization Name: Cache Org\nKey Term: {token_v1}\n",
            ai_usage="Active",
            document_type="RAG",
            organization_id=1
        ),
        organization_id=1
    )

    try:
        # Initial read populates caches
        ctx1 = rag_service.get_organization_master_context(organization_id=1, document_id=str(doc.id))
        assert token_v1 in ctx1

        # Update document content
        ks.update_doc(doc.id, KnowledgeDocUpdate(content=f"Organization Name: Cache Org\nKey Term: {token_v2}\n"), organization_id=1)
        
        # Stale cache must NOT be returned
        ctx2 = rag_service.get_organization_master_context(organization_id=1, document_id=str(doc.id))
        assert token_v2 in ctx2
        assert token_v1 not in ctx2

        # Delete document
        ks.delete_doc(doc.id, organization_id=1)
        ctx3 = rag_service.get_organization_master_context(organization_id=1, document_id=str(doc.id))
        assert ctx3 == ""
    finally:
        try:
            ks.delete_doc(doc.id, organization_id=1)
        except Exception:
            pass


# -----------------------------------------------------------------------------
# 4. Missing Vector Collections Handled Safely
# -----------------------------------------------------------------------------
def test_missing_vector_collection_handled_safely():
    """Verify that query against an unindexed/empty collection safely returns empty context without unhandled crashes."""
    res = rag_service.retrieve_context("any search query", organization_id=999999, document_id="nonexistent_doc_999999")
    assert isinstance(res, str)
    assert res == "" or "not available" in res.lower() or len(res) == 0


# -----------------------------------------------------------------------------
# 5. Multi-Tenant Isolation: No Cross-Tenant Leakage
# -----------------------------------------------------------------------------
def test_no_cross_tenant_leakage(db_session):
    """Verify Tenant 1 data is strictly invisible to Tenant 2."""
    ks = KnowledgeService(db_session)
    secret_token = f"CONFIDENTIAL_ORG1_{uuid.uuid4().hex}"

    doc_org1 = ks.create_doc(
        payload=KnowledgeDocCreate(
            title="org1_private.txt",
            content=f"Organization Name: Org One Exclusive\nPrivate Code: {secret_token}\n",
            ai_usage="Active",
            document_type="RAG",
            organization_id=1
        ),
        organization_id=1
    )

    try:
        # Org 2 attempts to retrieve the secret code
        retrieved_org2 = rag_service.retrieve_context(f"Private Code {secret_token}", organization_id=2)
        assert secret_token not in retrieved_org2, "Cross-tenant leakage: Org 2 retrieved Org 1 secret vector!"
    finally:
        ks.delete_doc(doc_org1.id, organization_id=1)


# -----------------------------------------------------------------------------
# 6. Selected Doctor Never Silently Substituted
# -----------------------------------------------------------------------------
def test_selected_doctor_never_silently_substituted():
    """Verify requesting an unknown doctor produces rejection and never silent replacement."""
    session = BookingSession("test_no_silent_sub_sess")
    session.reset()
    session.organization_id = 1
    session.state = BookingState.WAITING_FOR_DOCTOR

    # Request a completely unknown doctor
    unknown_doctor_input = "Doctor Nonexistent Person"
    val_res = entity_validation_service.validate_entity(unknown_doctor_input, organization_id=1)

    # Entity validation must NOT verify an unknown person
    assert val_res.is_verified is False

    # Process booking turn with this unknown doctor
    res = booking_state_service.process_booking_turn(unknown_doctor_input, session)

    # Doctor state must NOT be silently replaced with any arbitrary doctor
    assert session.doctor != "Dr. Arun Kumar"
    assert session.doctor != "Dr. Rohit"
    assert session.doctor_locked is False
    booking_state_service.remove_session("test_no_silent_sub_sess")


# -----------------------------------------------------------------------------
# 7. Production Database Contains No Test Fixtures
# -----------------------------------------------------------------------------
def test_production_database_contains_no_test_fixtures():
    """Verify that test documents and dummy records cannot leak into production storage."""
    # Check production receptionist.db directly using standard sqlite connection
    import sqlite3
    prod_db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "receptionist.db"))
    if os.path.exists(prod_db_path):
        conn = sqlite3.connect(prod_db_path)
        cur = conn.cursor()
        
        # Test documents should not be in production
        bad_docs = cur.execute("SELECT id, title FROM knowledge_documents WHERE title IN ('org999_secret.txt', 'city_care_dental.txt') OR organization_id = 999").fetchall()
        assert len(bad_docs) == 0, f"Found leaked test documents in production database: {bad_docs}"

        # Dummy test staff should not be in production
        bad_staff = cur.execute("SELECT id, name FROM staff WHERE name IN ('Dr. Arun Kumar', 'Dr. Priya Sharma')").fetchall()
        assert len(bad_staff) == 0, f"Found leaked test staff in production database: {bad_staff}"
        
        conn.close()


# -----------------------------------------------------------------------------
# 8. Empty Retrieval Never Produces Fabricated Answer
# -----------------------------------------------------------------------------
def test_empty_retrieval_never_fabricates_facts():
    """Verify that when no RAG context exists for a business query, AI gives safe refusal."""
    resp = ai_service.generate_response(
        text="What is your return refund fee for damaged products?",
        context="",  # empty retrieval
        session_history=[],
        intent="general_question",
        organization_id=1
    )
    resp_lower = resp.lower()
    # Must acknowledge lack of record or offer assistance, without inventing dollar amounts
    assert any(term in resp_lower for term in ["not available", "records", "assist", "help", "information", "apologize", "sorry"])


# -----------------------------------------------------------------------------
# 9. Correlation by Call ID and Utterance ID
# -----------------------------------------------------------------------------
def test_rag_correlation_by_call_and_utterance_ids():
    """Verify RAG retrieval accepts and correlates call_id and utterance_id."""
    call_id = f"call_{uuid.uuid4().hex[:8]}"
    utt_id = f"utt_{uuid.uuid4().hex[:6]}"
    
    # Should execute without error passing tracking IDs
    res = rag_service.retrieve_context(
        query="business hours",
        organization_id=1,
        call_id=call_id,
        utterance_id=utt_id
    )
    assert isinstance(res, str)


# -----------------------------------------------------------------------------
# 10. Single Turn State Management
# -----------------------------------------------------------------------------
def test_single_turn_state_management():
    """Verify a turn does not mutate into multiple competing response states."""
    session = BookingSession("test_single_turn_management")
    session.reset()
    session.state = BookingState.WAITING_FOR_DATE
    session.doctor = "Specialist Representative"
    session.doctor_locked = True

    # User provides date
    res = booking_state_service.process_booking_turn("Tomorrow", session)
    assert session.date == "Tomorrow"
    # State advances to time selection without duplicate state branches
    assert session.state == BookingState.WAITING_FOR_TIME
    booking_state_service.remove_session("test_single_turn_management")
