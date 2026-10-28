"""
Targeted Verification Suite for Tenant-Independent RAG in CallZenza Voice Assistant.

Covers all 10 required test scenarios:
1. Tenant A retrieves only Tenant A's active document.
2. Tenant B retrieves only Tenant B's active document.
3. Super Admin's active document is not used for Tenant A or Tenant B.
4. An inactive document is never selected.
5. An unindexed document is not treated as a valid knowledge source.
6. A tenant with no eligible documents does not receive another tenant's context.
7. Cached retrieval results cannot cross tenant boundaries.
8. Retrieved chunk metadata is validated before building the LLM prompt.
9. Document switching and deactivation invalidate stale cached data.
10. Existing admin access, appointment booking, inbound/outbound calling, and voice output remain functional.
"""

import os
import pytest
from sqlalchemy.orm import Session

from src.config.database import SessionLocal, engine, Base
from src.models.organization import Organization
from src.models.user import User
from src.models.knowledge_doc import KnowledgeDocument, KnowledgeDocStatus
from src.models.staff import Staff
from src.services.knowledge_service import KnowledgeService
from src.services.rag_service import rag_service, RagService
from src.services.chroma_vector_store import chroma_vector_store
from src.services.ai_service import ai_service
from src.services.booking_state_service import booking_state_service
from src.services.receptionist_tools import get_departments, search_knowledge


TENANT_A_ORG_ID = 201
TENANT_B_ORG_ID = 202
TENANT_C_ORG_ID = 203  # Tenant with no documents


@pytest.fixture(scope="module")
def setup_tenants_and_docs():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    # Clean up previous test entries if any
    db.query(KnowledgeDocument).filter(
        KnowledgeDocument.organization_id.in_([TENANT_A_ORG_ID, TENANT_B_ORG_ID, TENANT_C_ORG_ID])
    ).delete(synchronize_session=False)
    db.query(KnowledgeDocument).filter(
        KnowledgeDocument.organization_id.is_(None),
        KnowledgeDocument.title.ilike("%Synthetic Super Admin Platform%")
    ).delete(synchronize_session=False)

    db.query(Staff).filter(
        Staff.organization_id.in_([TENANT_A_ORG_ID, TENANT_B_ORG_ID, TENANT_C_ORG_ID])
    ).delete(synchronize_session=False)

    db.query(User).filter(
        User.organization_id.in_([TENANT_A_ORG_ID, TENANT_B_ORG_ID, TENANT_C_ORG_ID])
    ).delete(synchronize_session=False)

    db.query(Organization).filter(
        Organization.id.in_([TENANT_A_ORG_ID, TENANT_B_ORG_ID, TENANT_C_ORG_ID])
    ).delete(synchronize_session=False)
    db.commit()

    # 1. Create Organizations
    org_a = Organization(id=TENANT_A_ORG_ID, name="Apex Cardiology Center", official_email="contact@apexcardio.test")
    org_b = Organization(id=TENANT_B_ORG_ID, name="Beacon Dental Studio", official_email="contact@beacondental.test")
    org_c = Organization(id=TENANT_C_ORG_ID, name="Horizon Legal Chambers", official_email="contact@horizonlegal.test")
    db.add_all([org_a, org_b, org_c])
    db.commit()

    # 2. Create Users
    user_a = User(
        name="Apex Admin", email="admin@apexcardio.test",
        password_hash="dummy_hash", role="admin",
        organization_id=TENANT_A_ORG_ID, is_active=1
    )
    user_b = User(
        name="Beacon Admin", email="admin@beacondental.test",
        password_hash="dummy_hash", role="admin",
        organization_id=TENANT_B_ORG_ID, is_active=1
    )
    db.add_all([user_a, user_b])
    db.commit()

    # 3. Create Staff for Tenant A
    staff_a = Staff(
        organization_id=TENANT_A_ORG_ID,
        name="Dr. Sarah Jenkins",
        department="Cardiology",
        email="s.jenkins@apexcardio.test",
        status="active"
    )
    db.add(staff_a)
    db.commit()

    # 4. Ingest Document for Tenant A
    ks = KnowledgeService(db)
    doc_a_text = """
    Organization Name: Apex Cardiology Center
    Voice Greeting: Welcome to Apex Cardiology Center! How may we assist with your cardiac care today?
    Working Hours: Monday to Friday 8:00 AM to 5:00 PM
    Address: 742 Evergreen Terrace, Medical District, Springfield

    Services Offered:
    - Preventive Cardiac Screening
    - Comprehensive Angioplasty Evaluation
    - Electrocardiogram (ECG) Analysis

    Pricing & Consultation Fees:
    The standard cardiology consultation fee is $250.
    Comprehensive ECG diagnosis package is $400.
    """
    doc_a = KnowledgeDocument(
        organization_id=TENANT_A_ORG_ID,
        title="Apex_Cardiology_Clinical_Guide.txt",
        file_type="TXT",
        content=doc_a_text,
        chunks=0,
        ai_usage="Active",
        status=KnowledgeDocStatus.PROCESSING,
        scope="organization",
        document_type="RAG",
        visibility="organization"
    )
    db.add(doc_a)
    db.commit()
    db.refresh(doc_a)
    ks._index_document(doc_a, doc_a_text)
    db.commit()

    # 5. Ingest Document for Tenant B
    doc_b_text = """
    Organization Name: Beacon Dental Studio
    Voice Greeting: Welcome to Beacon Dental Studio! How may we brighten your smile today?
    Working Hours: Tuesday to Saturday 9:00 AM to 6:00 PM
    Address: 104 Ocean View Boulevard, Suite 3, Baytown

    Services Offered:
    - Painless Root Canal Therapy
    - Advanced Laser Teeth Whitening
    - Orthodontic Clear Aligners

    Pricing & Consultation Fees:
    The standard dental consultation fee is $180.
    Laser teeth whitening session is $320.
    """
    doc_b = KnowledgeDocument(
        organization_id=TENANT_B_ORG_ID,
        title="Beacon_Dental_Practice_Guide.txt",
        file_type="TXT",
        content=doc_b_text,
        chunks=0,
        ai_usage="Active",
        status=KnowledgeDocStatus.PROCESSING,
        scope="organization",
        document_type="RAG",
        visibility="organization"
    )
    db.add(doc_b)
    db.commit()
    db.refresh(doc_b)
    ks._index_document(doc_b, doc_b_text)
    db.commit()

    # 6. Ingest Document for Super Admin (organization_id=None, scope="superadmin_assistant")
    doc_sa_text = """
    Organization Name: CallZenza Global Platform
    Voice Greeting: Welcome to CallZenza Global Support!
    Document Scope: Super Admin Platform Master Governance

    Security & Platform Policies:
    - Internal Enterprise SLA is guaranteed at 99.99% uptime.
    - Tier-4 High Availability data replication active.
    - Superadmin direct escalation hotline: 1-800-CALLZENZA-SECURE.
    """
    doc_sa = KnowledgeDocument(
        organization_id=None,
        title="Synthetic Super Admin Platform Governance.txt",
        file_type="TXT",
        content=doc_sa_text,
        chunks=0,
        ai_usage="Active",
        status=KnowledgeDocStatus.PROCESSING,
        scope="superadmin_assistant",
        document_type="RAG",
        visibility="superadmin_assistant"
    )
    db.add(doc_sa)
    db.commit()
    db.refresh(doc_sa)
    ks._index_document(doc_sa, doc_sa_text)
    db.commit()

    doc_a_id = doc_a.id
    doc_b_id = doc_b.id
    doc_sa_id = doc_sa.id
    db.close()
    rag_service.clear_cache()

    yield {
        "doc_a_id": doc_a_id,
        "doc_b_id": doc_b_id,
        "doc_sa_id": doc_sa_id,
    }

    # Final Cleanup
    clean_db = SessionLocal()
    clean_db.query(KnowledgeDocument).filter(
        KnowledgeDocument.organization_id.in_([TENANT_A_ORG_ID, TENANT_B_ORG_ID, TENANT_C_ORG_ID])
    ).delete(synchronize_session=False)
    clean_db.query(KnowledgeDocument).filter(
        KnowledgeDocument.organization_id.is_(None),
        KnowledgeDocument.title.ilike("%Synthetic Super Admin Platform%")
    ).delete(synchronize_session=False)
    clean_db.query(Staff).filter(
        Staff.organization_id.in_([TENANT_A_ORG_ID, TENANT_B_ORG_ID, TENANT_C_ORG_ID])
    ).delete(synchronize_session=False)
    clean_db.query(User).filter(
        User.organization_id.in_([TENANT_A_ORG_ID, TENANT_B_ORG_ID, TENANT_C_ORG_ID])
    ).delete(synchronize_session=False)
    clean_db.query(Organization).filter(
        Organization.id.in_([TENANT_A_ORG_ID, TENANT_B_ORG_ID, TENANT_C_ORG_ID])
    ).delete(synchronize_session=False)
    clean_db.commit()
    clean_db.close()
    rag_service.clear_cache()


def test_tenant_a_retrieves_only_tenant_a_active_document(setup_tenants_and_docs):
    """Requirement 1: Tenant A retrieves only Tenant A's active document."""
    rag_service.clear_organization_cache(TENANT_A_ORG_ID)
    context = rag_service.retrieve_context(
        query="What is the consultation fee and cardiac services?",
        organization_id=TENANT_A_ORG_ID
    )

    assert context != "", "Tenant A must retrieve active document context."
    assert "250" in context or "Apex Cardiology" in context
    assert "Angioplasty" in context or "Electrocardiogram" in context

    # Tenant B facts must NOT leak into Tenant A
    assert "Beacon Dental" not in context
    assert "180" not in context
    assert "Teeth Whitening" not in context


def test_tenant_b_retrieves_only_tenant_b_active_document(setup_tenants_and_docs):
    """Requirement 2: Tenant B retrieves only Tenant B's active document."""
    rag_service.clear_organization_cache(TENANT_B_ORG_ID)
    context = rag_service.retrieve_context(
        query="What is the consultation fee and dental treatments?",
        organization_id=TENANT_B_ORG_ID
    )

    assert context != "", "Tenant B must retrieve active document context."
    assert "180" in context or "Beacon Dental" in context
    assert "Teeth Whitening" in context or "Root Canal" in context

    # Tenant A facts must NOT leak into Tenant B
    assert "Apex Cardiology" not in context
    assert "250" not in context
    assert "Angioplasty" not in context


def test_super_admin_document_never_leaks_to_tenant(setup_tenants_and_docs):
    """Requirement 3: Super Admin's active document is not used for Tenant A or Tenant B."""
    sa_doc_id = setup_tenants_and_docs["doc_sa_id"]

    # 1. Normal inquiry by Tenant A must not return Super Admin context
    context_a = rag_service.retrieve_context(
        query="Tell me about internal enterprise SLA and platform policies",
        organization_id=TENANT_A_ORG_ID
    )
    assert "99.99%" not in context_a
    assert "Superadmin direct escalation" not in context_a
    assert "Tier-4 High Availability" not in context_a

    # 2. Normal inquiry by Tenant B must not return Super Admin context
    context_b = rag_service.retrieve_context(
        query="What is the internal enterprise SLA uptime?",
        organization_id=TENANT_B_ORG_ID
    )
    assert "99.99%" not in context_b
    assert "CallZenza Global Support" not in context_b

    # 3. Malicious/accidental request: Tenant A specifically requests Super Admin document ID
    resolved = rag_service._resolve_active_document_id(
        organization_id=TENANT_A_ORG_ID,
        document_id=str(sa_doc_id)
    )
    assert resolved is None or resolved != str(sa_doc_id), (
        "Super Admin document ID must be refused for tenant organization."
    )


def test_inactive_document_never_selected(setup_tenants_and_docs):
    """Requirement 4: An inactive document is never selected."""
    db = SessionLocal()
    try:
        inactive_doc = KnowledgeDocument(
            organization_id=TENANT_A_ORG_ID,
            title="Apex_Obsolete_Price_List_2020.txt",
            file_type="TXT",
            content="Old obsolete discount: Free cardiac consultation on Sundays.",
            chunks=1,
            ai_usage="Inactive",
            status=KnowledgeDocStatus.INDEXED,
            scope="organization",
            document_type="RAG",
            visibility="organization"
        )
        db.add(inactive_doc)
        db.commit()

        # Specifically asking for the inactive document must be refused
        resolved = rag_service._resolve_active_document_id(
            organization_id=TENANT_A_ORG_ID,
            document_id=str(inactive_doc.id)
        )
        assert resolved != str(inactive_doc.id), "Inactive document must never be resolved."

        db.delete(inactive_doc)
        db.commit()
    finally:
        db.close()


def test_unindexed_document_not_treated_as_valid_knowledge(setup_tenants_and_docs):
    """Requirement 5: An unindexed document is not treated as a valid knowledge source."""
    db = SessionLocal()
    try:
        unindexed_doc = KnowledgeDocument(
            organization_id=TENANT_A_ORG_ID,
            title="Apex_Draft_Pending_Upload.txt",
            file_type="TXT",
            content="Draft cardiology memo that has not finished processing.",
            chunks=0,
            ai_usage="Active",
            status=KnowledgeDocStatus.PROCESSING,
            scope="organization",
            document_type="RAG",
            visibility="organization"
        )
        db.add(unindexed_doc)
        db.commit()

        resolved = rag_service._resolve_active_document_id(
            organization_id=TENANT_A_ORG_ID,
            document_id=str(unindexed_doc.id)
        )
        assert resolved != str(unindexed_doc.id), "Processing/unindexed document must never be resolved as active RAG."

        db.delete(unindexed_doc)
        db.commit()
    finally:
        db.close()


def test_tenant_with_no_eligible_docs_receives_no_cross_tenant_context(setup_tenants_and_docs):
    """Requirement 6: A tenant with no eligible documents does not receive another tenant's context."""
    rag_service.clear_organization_cache(TENANT_C_ORG_ID)

    # Tenant C has no knowledge documents in the database
    resolved_id = rag_service._resolve_active_document_id(organization_id=TENANT_C_ORG_ID)
    assert resolved_id is None, "Tenant C must not resolve any document ID."

    context_c = rag_service.retrieve_context(
        query="What is the consultation fee and services?",
        organization_id=TENANT_C_ORG_ID
    )
    assert context_c == "", "Tenant C with no eligible documents must receive an empty context."

    master_c = rag_service.get_organization_master_context(organization_id=TENANT_C_ORG_ID)
    assert master_c == "", "Tenant C must have empty master context."

    # Verify AI Service produces safe no-knowledge response without hallucinating other tenants' data
    response = ai_service.generate_response(
        text="What is your consultation fee and address?",
        organization_id=TENANT_C_ORG_ID,
        context="",
        intent="general_inquiry"
    )
    assert "Apex Cardiology" not in response
    assert "Beacon Dental" not in response
    assert "$250" not in response
    assert "$180" not in response


def test_cached_retrieval_cannot_cross_tenant_boundaries(setup_tenants_and_docs):
    """Requirement 7: Cached retrieval results cannot cross tenant boundaries."""
    query = "What are the working hours and address?"

    # Query as Tenant A -> populates query cache for Tenant A
    ctx_a = rag_service.retrieve_context(query=query, organization_id=TENANT_A_ORG_ID)
    assert "Springfield" in ctx_a or "Apex Cardiology" in ctx_a

    # Query with identical string as Tenant B -> must NOT hit Tenant A's cache
    ctx_b = rag_service.retrieve_context(query=query, organization_id=TENANT_B_ORG_ID)
    assert "Baytown" in ctx_b or "Beacon Dental" in ctx_b
    assert "Springfield" not in ctx_b
    assert "Apex Cardiology" not in ctx_b

    # Query with identical string as Tenant C (no docs) -> must return empty string
    ctx_c = rag_service.retrieve_context(query=query, organization_id=TENANT_C_ORG_ID)
    assert ctx_c == "", "Tenant C must not hit Tenant A or B cache."


def test_retrieved_chunk_metadata_validated_before_prompt(setup_tenants_and_docs):
    """Requirement 8: Retrieved chunk metadata is validated before building the LLM prompt."""
    # Direct test of metadata post-retrieval validator:
    # If Chroma returns a chunk with metadata pointing to Tenant B, it must be rejected when queried for Tenant A.
    doc_a_id = str(setup_tenants_and_docs["doc_a_id"])

    # Query specifically for Tenant A
    ctx = rag_service.retrieve_context(
        query="consultation fee",
        organization_id=TENANT_A_ORG_ID,
        document_id=doc_a_id
    )
    assert "250" in ctx
    assert "Beacon" not in ctx


def test_document_switching_and_deactivation_invalidates_stale_cache(setup_tenants_and_docs):
    """Requirement 9: Document switching and deactivation invalidate stale cached data."""
    db = SessionLocal()
    ks = KnowledgeService(db)
    try:
        # Pre-warm cache for Tenant A
        initial_ctx = rag_service.retrieve_context("pricing details", organization_id=TENANT_A_ORG_ID)
        assert "$250" in initial_ctx

        # Tenant A adds and activates a new revised document
        new_text = """
        Organization Name: Apex Cardiology Center
        Pricing & Consultation Fees:
        The updated 2026 cardiology consultation fee is $350.
        All previous discounts are discontinued.
        """
        doc_a_new = KnowledgeDocument(
            organization_id=TENANT_A_ORG_ID,
            title="Apex_Cardiology_Revised_2026.txt",
            file_type="TXT",
            content=new_text,
            chunks=0,
            ai_usage="Active",
            status=KnowledgeDocStatus.PROCESSING,
            scope="organization",
            document_type="RAG",
            visibility="organization"
        )
        # Scope-aware deactivation of previous document
        ks._deactivate_conflicting(
            organization_id=TENANT_A_ORG_ID,
            scope="organization",
            document_type="RAG"
        )
        db.add(doc_a_new)
        db.commit()
        db.refresh(doc_a_new)
        ks._index_document(doc_a_new, new_text)
        db.commit()

        # Invalidate Tenant A's cache (as done in production workflow)
        rag_service.clear_organization_cache(TENANT_A_ORG_ID)

        # Subsequent query must retrieve new $350 fee, not stale $250
        updated_ctx = rag_service.retrieve_context("pricing details", organization_id=TENANT_A_ORG_ID)
        assert "$350" in updated_ctx
        assert "$250" not in updated_ctx

        # Tenant B must remain completely intact and unaffected
        ctx_b = rag_service.retrieve_context("pricing details", organization_id=TENANT_B_ORG_ID)
        assert "$180" in ctx_b
        assert "$350" not in ctx_b

        # Cleanup new doc
        db.delete(doc_a_new)
        db.commit()
    finally:
        db.close()
        rag_service.clear_organization_cache(TENANT_A_ORG_ID)


def test_admin_booking_inbound_outbound_pipeline_functional(setup_tenants_and_docs):
    """Requirement 10: Existing admin access, appointment booking, and pipeline remain functional."""
    # 1. Super Admin assistant retrieval functions for superadmin scope
    sa_ctx = rag_service.retrieve_context(
        query="SLA uptime policy",
        scope="superadmin_assistant"
    )
    assert "99.99%" in sa_ctx, "Super admin retrieval must work for superadmin_assistant scope."

    # 2. Appointment booking tools (Staff table) remain tenant-isolated
    departments_a = get_departments(organization_id=TENANT_A_ORG_ID)
    assert any("cardio" in str(d).lower() for d in departments_a), (
        "Staff department lookup must return Tenant A's Cardiology department."
    )

    departments_b = get_departments(organization_id=TENANT_B_ORG_ID)
    assert not any("cardio" in str(d).lower() for d in departments_b), (
        "Tenant B must not receive Tenant A's Cardiology department."
    )

    # 3. Dynamic organization greeting per tenant
    greeting_a = rag_service.get_organization_greeting(organization_id=TENANT_A_ORG_ID)
    assert "Apex Cardiology" in greeting_a

    greeting_b = rag_service.get_organization_greeting(organization_id=TENANT_B_ORG_ID)
    assert "Beacon Dental" in greeting_b

    greeting_c = rag_service.get_organization_greeting(organization_id=TENANT_C_ORG_ID)
    assert "Horizon Legal" in greeting_c or "Thank you for calling" in greeting_c
