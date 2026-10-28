import os
import pytest
from src.config.database import SessionLocal
from src.models.knowledge_doc import KnowledgeDocument, KnowledgeDocStatus
from src.services.knowledge_service import KnowledgeService
from src.services.rag_service import rag_service

PDF_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "src", "uploads", "knowledge",
    "Dream_Caatchers_Properties_Outbound_RAG_Script_Salon_Format.pdf"
)

def test_pdf_chunking_dynamic_count_and_size_bounds():
    """Verify PDF extraction and dynamic chunking produce structured chunks within size bounds."""
    if not os.path.exists(PDF_PATH):
        pytest.skip(f"Target PDF missing at {PDF_PATH}")
    
    db = SessionLocal()
    try:
        ks = KnowledgeService(db)
        with open(PDF_PATH, "rb") as f:
            file_bytes = f.read()

        extracted_text = ks._extract_text(PDF_PATH, file_bytes, "PDF")
        assert len(extracted_text) > 10000, f"Expected >10k characters, got {len(extracted_text)}"

        chunks = ks._chunk_text_with_metadata(extracted_text, "Dream_Caatchers")
        
        # Verify chunk count is dynamic (e.g. >25 chunks, not hardcoded 2)
        assert len(chunks) >= 25, f"Expected >=25 dynamic chunks, got {len(chunks)}"

        # Verify max chunk size bounds (all chunks <= 1000 chars)
        for idx, item in enumerate(chunks):
            txt = item["text"]
            assert len(txt) <= 1000, f"Chunk {idx} exceeds 1000 chars limit ({len(txt)} chars)"
            assert "section" in item
            assert "chunk_index" in item
            assert item["total_chunks"] == len(chunks)
    finally:
        db.close()


def test_property_directory_fact_and_rule_cohesion():
    """Verify property records preserve name, location, and outbound rules in the same chunk."""
    if not os.path.exists(PDF_PATH):
        pytest.skip(f"Target PDF missing at {PDF_PATH}")
    db = SessionLocal()
    try:
        ks = KnowledgeService(db)
        with open(PDF_PATH, "rb") as f:
            file_bytes = f.read()

        extracted_text = ks._extract_text(PDF_PATH, file_bytes, "PDF")
        chunks = ks._chunk_text_with_metadata(extracted_text, "Dream_Caatchers")

        chunk_texts = [c["text"] for c in chunks]

        # Check Elephantine Enormous chunk cohesion
        elephantine_chunks = [t for t in chunk_texts if "Elephantine Enormous" in t]
        assert len(elephantine_chunks) >= 1
        assert any("Mambakkam" in t and "Outbound rule" in t for t in elephantine_chunks)

        # Check VARAM Padappai Premium Plots chunk cohesion
        varam_chunks = [t for t in chunk_texts if "VARAM Padappai" in t]
        assert len(varam_chunks) >= 1
        assert any("Padappai" in t and ("Outbound rule" in t or "Verify" in t) for t in varam_chunks)
    finally:
        db.close()


def test_dynamic_rag_retrieval_grounded_qa():
    """Verify RAG retrieval returns accurate, grounded context for document queries."""
    if not os.path.exists(PDF_PATH):
        pytest.skip(f"Target PDF missing at {PDF_PATH}")
    db = SessionLocal()
    try:
        # Deactivate existing org 1 documents to isolate test
        db.query(KnowledgeDocument).filter(KnowledgeDocument.organization_id == 1).update({"ai_usage": "Inactive"})
        db.commit()

        ks = KnowledgeService(db)
        with open(PDF_PATH, "rb") as f:
            file_bytes = f.read()

        text = ks._extract_text(PDF_PATH, file_bytes, "PDF")

        doc = KnowledgeDocument(
            organization_id=1,
            title="Dream_Caatchers_Properties_Outbound_RAG_Script_Salon_Format.pdf",
            file_type="PDF",
            file_path=PDF_PATH,
            content=text,
            chunks=0,
            ai_usage="Active",
            status=KnowledgeDocStatus.INDEXED,
            document_type="SCRIPT",
            scope="organization"
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)

        indexed_cnt = ks._index_document(doc, text)
        db.commit()
        db.refresh(doc)

        assert doc.chunks == indexed_cnt
        assert doc.chunks >= 25

        # Query 1: Location of Elephantine Enormous
        ctx1 = rag_service.retrieve_context("Where is Elephantine Enormous located?", n_results=3, organization_id=1, document_id=str(doc.id))
        assert "Mambakkam" in ctx1

        # Query 2: Property categories
        ctx2 = rag_service.retrieve_context("What property categories do you handle?", n_results=3, organization_id=1, document_id=str(doc.id))
        assert "plots" in ctx2.lower() or "apartments" in ctx2.lower()

        # Query 3: Site visit scheduling rule
        ctx3 = rag_service.retrieve_context("How are site visits confirmed?", n_results=3, organization_id=1, document_id=str(doc.id))
        assert "confirmed" in ctx3.lower() or "visit" in ctx3.lower()

        # Cleanup
        db.delete(doc)
        db.commit()
    finally:
        db.close()
