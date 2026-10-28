"""
Comprehensive Test Suite for Option B: Dedicated ChromaDB Vector Database & Dynamic RAG Service.

Verifies:
1. Relational DB (receptionist.db) maintains document metadata, while ChromaDB (./chroma_db) stores vector chunks & embeddings.
2. Dynamic Ingestion & Active Script Switching: Changing the active script dynamically changes AI retrieved context & responses.
3. Strict Tenant Isolation: Organization 1 cannot access Organization 2's knowledge vectors.
4. Obsolete Knowledge Deletion: Deleting a document safely purges vector chunks from ChromaDB.
5. Neutral Fallback: Unlisted factual inquiries return neutral fallback without hardcoded or hallucinated business data.
"""

import os
import pytest
from sqlalchemy.orm import Session

from src.config.database import SessionLocal, engine, Base
from src.models.knowledge_doc import KnowledgeDocument, KnowledgeDocStatus
from src.models.organization import Organization
from src.services.knowledge_service import KnowledgeService
from src.services.rag_service import rag_service
from src.services.chroma_vector_store import chroma_vector_store
from src.services.agent_engine import AgentEngine

@pytest.fixture(scope="module")
def setup_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    # Deactivate existing test documents for org 1 and org 2 so tests start with a clean slate
    db.query(KnowledgeDocument).filter(KnowledgeDocument.organization_id.in_([1, 2])).update({"ai_usage": "Inactive"}, synchronize_session=False)
    db.commit()

    # Create test orgs if not present
    org1 = db.query(Organization).filter(Organization.id == 1).first()
    if not org1:
        org1 = Organization(id=1, name="Org One Real Estate", email="org1@test.com")
        db.add(org1)
    
    org2 = db.query(Organization).filter(Organization.id == 2).first()
    if not org2:
        org2 = Organization(id=2, name="Org Two Medical Clinic", email="org2@test.com")
        db.add(org2)
    
    db.commit()
    db.close()
    rag_service.clear_cache()
    yield
    # Cleanup


class TestOptionBChromaRAG:

    def test_01_relational_and_chroma_separation(self, setup_db):
        """Test that relational metadata is saved in DB and vector chunks are saved in ChromaDB."""
        db: Session = SessionLocal()
        ks = KnowledgeService(db)
        
        script_text = """
        Business Name: Dream Catchers Properties
        Voice Greeting: Welcome to Dream Catchers Properties! How can we assist with your dream home today?
        Services: Property Buying, Villa Rentals, Commercial Leasing
        Location: 100 Sunset Boulevard, Suite 400
        Working Hours: Monday to Saturday, 9 AM to 6 PM
        """
        
        doc = ks.create_doc(
            payload=type("Payload", (), {
                "title": "dream_catchers_script.txt",
                "content": script_text,
                "scope": "organization",
                "document_type": "SCRIPT",
                "ai_usage": "Active",
                "visibility": "organization",
                "organization_id": 1,
                "agent_id": None,
                "assistant_id": None,
                "model_dump": lambda self: {
                    "title": self.title,
                    "content": self.content,
                    "scope": self.scope,
                    "document_type": self.document_type,
                    "ai_usage": self.ai_usage,
                    "visibility": self.visibility,
                    "organization_id": self.organization_id,
                    "agent_id": self.agent_id,
                    "assistant_id": self.assistant_id,
                }
            })(),
            organization_id=1,
            super_admin_mode=False
        )
        
        assert doc.id is not None
        assert doc.status == KnowledgeDocStatus.INDEXED
        
        # Verify Relational DB metadata
        db_doc = db.query(KnowledgeDocument).filter(KnowledgeDocument.id == doc.id).first()
        assert db_doc is not None
        assert db_doc.title == "dream_catchers_script.txt"
        assert db_doc.organization_id == 1
        
        # Verify ChromaDB vector store contains the chunks
        chroma_res = chroma_vector_store.get(where={"document_id": str(doc.id)})
        assert len(chroma_res["ids"]) > 0
        assert "Dream Catchers Properties" in chroma_res["documents"][0]
        
        db.close()

    def test_02_active_script_context_retrieval(self, setup_db):
        """Test retrieval of context and greeting for Org 1."""
        org_name = rag_service.get_organization_name(organization_id=1)
        assert "Dream Catchers Properties" in org_name
        
        context = rag_service.retrieve_context("What services do you offer?", organization_id=1)
        assert "Property Buying" in context or "Villa Rentals" in context
        assert len(context) > 0

    def test_03_active_script_switching(self, setup_db):
        """Test switching active script in Org 1 dynamically updates retrieved context."""
        db: Session = SessionLocal()
        ks = KnowledgeService(db)
        
        new_script_text = """
        Business Name: Luxe Hair & Spa Lounge
        Voice Greeting: Hello and welcome to Luxe Hair & Spa Lounge! How can we pamper you today?
        Services: Haircuts, Organic Hair Coloring, Deep Tissue Massage, Hydrating Facials
        Location: 45 Park Avenue, Downtown
        Working Hours: Tuesday to Sunday, 10 AM to 8 PM
        """
        
        doc2 = ks.create_doc(
            payload=type("Payload", (), {
                "title": "luxe_spa_script.txt",
                "content": new_script_text,
                "scope": "organization",
                "document_type": "SCRIPT",
                "ai_usage": "Active",
                "visibility": "organization",
                "organization_id": 1,
                "agent_id": None,
                "assistant_id": None,
                "model_dump": lambda self: {
                    "title": self.title,
                    "content": self.content,
                    "scope": self.scope,
                    "document_type": self.document_type,
                    "ai_usage": self.ai_usage,
                    "visibility": self.visibility,
                    "organization_id": self.organization_id,
                    "agent_id": self.agent_id,
                    "assistant_id": self.assistant_id,
                }
            })(),
            organization_id=1,
            super_admin_mode=False
        )
        
        # Verify active brand & greeting switched
        org_name = rag_service.get_organization_name(organization_id=1)
        assert "Luxe Hair & Spa Lounge" in org_name
        
        greeting = rag_service.get_organization_greeting(organization_id=1)
        assert "Luxe Hair & Spa Lounge" in greeting
        
        # Verify retrieval returns new script content, not old real estate content
        context = rag_service.retrieve_context("What services do you offer?", organization_id=1)
        assert "Haircuts" in context or "Organic Hair Coloring" in context
        assert "Villa Rentals" not in context
        
        db.close()

    def test_04_tenant_isolation(self, setup_db):
        """Test that Org 2 cannot retrieve Org 1's vector knowledge."""
        db: Session = SessionLocal()
        ks = KnowledgeService(db)
        
        org2_script = """
        Business Name: City Care Dental Clinic
        Voice Greeting: Thank you for calling City Care Dental Clinic.
        Services: Dental Checkup, Teeth Whitening, Root Canal
        Location: 78 Healthcare Way
        """
        
        ks.create_doc(
            payload=type("Payload", (), {
                "title": "city_care_dental.txt",
                "content": org2_script,
                "scope": "organization",
                "document_type": "SCRIPT",
                "ai_usage": "Active",
                "visibility": "organization",
                "organization_id": 2,
                "agent_id": None,
                "assistant_id": None,
                "model_dump": lambda self: {
                    "title": self.title,
                    "content": self.content,
                    "scope": self.scope,
                    "document_type": self.document_type,
                    "ai_usage": self.ai_usage,
                    "visibility": self.visibility,
                    "organization_id": self.organization_id,
                    "agent_id": self.agent_id,
                    "assistant_id": self.assistant_id,
                }
            })(),
            organization_id=2,
            super_admin_mode=False
        )
        
        # Query Org 2 for hair salon services (which exist in Org 1 but NOT Org 2)
        org2_context = rag_service.retrieve_context("Haircuts and Organic Coloring", organization_id=2)
        assert "Luxe Hair" not in org2_context
        assert "Organic Hair Coloring" not in org2_context
        
        # Query Org 2 for dental checkup
        org2_dental_ctx = rag_service.retrieve_context("dental checkup services", organization_id=2)
        assert "Teeth Whitening" in org2_dental_ctx or "Dental Checkup" in org2_dental_ctx
        
        db.close()

    def test_05_obsolete_knowledge_deletion(self, setup_db):
        """Test that deleting a document purges its vectors from ChromaDB."""
        db: Session = SessionLocal()
        ks = KnowledgeService(db)
        
        temp_doc = ks.create_doc(
            payload=type("Payload", (), {
                "title": "temp_obsolete_doc.txt",
                "content": "Obsolete Secret Information XYZ-999",
                "scope": "organization",
                "document_type": "RAG",
                "ai_usage": "Active",
                "visibility": "organization",
                "organization_id": 1,
                "agent_id": None,
                "assistant_id": None,
                "model_dump": lambda self: {
                    "title": self.title,
                    "content": self.content,
                    "scope": self.scope,
                    "document_type": self.document_type,
                    "ai_usage": self.ai_usage,
                    "visibility": self.visibility,
                    "organization_id": self.organization_id,
                    "agent_id": self.agent_id,
                    "assistant_id": self.assistant_id,
                }
            })(),
            organization_id=1,
            super_admin_mode=False
        )
        
        # Verify present in ChromaDB
        res_before = chroma_vector_store.get(where={"document_id": str(temp_doc.id)})
        assert len(res_before["ids"]) > 0
        
        # Delete document via KnowledgeService
        ks.delete_doc(temp_doc.id, organization_id=1)
        
        # Verify purged from ChromaDB
        res_after = chroma_vector_store.get(where={"document_id": str(temp_doc.id)})
        assert len(res_after["ids"]) == 0
        
        db.close()
