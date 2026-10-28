import io
import os
import pytest
from fastapi import UploadFile
from sqlalchemy import text
from src.config.database import SessionLocal, engine
from src.models.knowledge_doc import KnowledgeDocument, KnowledgeDocStatus
from src.services.knowledge_service import KnowledgeService
from src.services.chroma_vector_store import chroma_vector_store


class TestLargeDocumentUploadAndChunking:
    def test_dynamic_chunking_oversized_text(self):
        """Verify that large texts with 150k+ chars are converted into proper bounded chunks."""
        # Create a structured document of ~160k characters with multiple sections, FAQs, and long paragraphs
        sections = []
        for i in range(1, 25):
            sec_text = (
                f"SECTION {i}: PRODUCT {i} SUITE OVERVIEW\n"
                f"Product {i} provides end-to-end automated customer operations, AI speech processing, "
                f"and omni-channel integration. \x7f Key highlights include 99.9% uptime and zero latency.\n\n"
                f"RULES AND GUIDELINES FOR PRODUCT {i}:\n"
                f"All customer interactions must be logged with accurate metadata and session tracking. "
                f"In case of escalation, transfer to Tier-2 technical support.\n\n"
                f"Q: What is the primary benefit of Product {i}?\n"
                f"A: Product {i} automates workflows without requiring manual interventions.\n\n"
            )
            # Add an oversized single paragraph of ~6000 characters
            oversized_paragraph = f"Technical specifications for Product {i} architecture: " + (
                f"Component {i} operates in high-throughput distributed mode with resilient clustering. "
                * 70
            ) + "\n\n"
            sections.append(sec_text + oversized_paragraph)

        full_content = "\n".join(sections)
        assert len(full_content) > 150000, f"Expected >150k characters, got {len(full_content)}"

        # Chunk the text
        chunks = KnowledgeService._chunk_text_with_metadata(full_content, source_title="Aminoid_AI_Large_Doc")

        assert len(chunks) > 150, f"Expected >150 chunks for large document, got {len(chunks)}"

        # Verify strict size bounds: no chunk exceeds max_chunk_size (900 chars)
        for idx, chunk in enumerate(chunks):
            txt = chunk["text"]
            assert len(txt) <= 950, f"Chunk {idx} exceeded max length: {len(txt)} chars"
            assert "\x7f" not in txt, f"Control character \x7f was not cleaned from chunk {idx}"
            assert "section" in chunk
            assert "chunk_index" in chunk
            assert chunk["total_chunks"] == len(chunks)

    def test_clean_control_characters(self):
        """Verify that control characters like DEL (\x7f) and non-printables are normalized."""
        sample = "AMINOID AI \x7f INBOUND CALL SCRIPTS\x00\x08Playbook \u25a0 Feature 1"
        cleaned = KnowledgeService._clean_extracted_text(sample)
        assert "\x7f" not in cleaned
        assert "\x00" not in cleaned
        assert "\x08" not in cleaned
        assert "AMINOID AI" in cleaned
        assert "INBOUND CALL SCRIPTS" in cleaned
        assert "Playbook" in cleaned
        assert "•" in cleaned
        assert "Feature 1" in cleaned

    def test_upload_large_file_and_batch_upsert(self):
        """Verify full upload pipeline for a large file (>150k characters)."""
        db = SessionLocal()
        ks = KnowledgeService(db)

        # Generate large content
        parts = []
        for i in range(1, 20):
            parts.append(
                f"# MODULE {i}: ENTERPRISE PLATFORM SERVICES\n"
                f"Enterprise Platform Service {i} manages enterprise billing, provisioning, and telephony.\n"
                f"Details for service {i}: " + ("High availability cloud deployment with auto-scaling. " * 35) + "\n"
                f"Q: How to configure service {i}?\n"
                f"A: Access the settings dashboard under enterprise integrations.\n\n"
            )
        large_text = "\n".join(parts)
        file_bytes = large_text.encode("utf-8")

        upload_file = UploadFile(
            filename="large_enterprise_catalog.txt",
            file=io.BytesIO(file_bytes)
        )

        try:
            doc = ks.upload_file(
                upload_file,
                organization_id=1,
                scope="organization",
                document_type="RAG",
                uploaded_by=1,
            )

            assert doc.id is not None
            assert doc.status == KnowledgeDocStatus.INDEXED
            assert doc.chunks > 50
            assert len(doc.content) == len(large_text)

            # Verify vectors were inserted into ChromaDB
            count = chroma_vector_store.count(where={"document_id": str(doc.id)})
            assert count == doc.chunks

            # Clean up
            ks.delete_doc(doc.id, organization_id=1)
        finally:
            db.close()

    def test_mysql_data_too_long_auto_recovery_simulation(self):
        """Verify that when 1406 Data too long is simulated on content save, the auto-recovery mechanism handles it."""
        from unittest.mock import MagicMock
        db = MagicMock()
        ks = KnowledgeService(db)

        mock_doc = MagicMock()
        mock_doc.id = 999
        mock_doc.content = None

        has_failed = {"failed": False}
        def mock_commit():
            # Only trigger 1406 error once when doc.content is set
            if mock_doc.content is not None and not has_failed["failed"]:
                has_failed["failed"] = True
                raise Exception("(pymysql.err.DataError) (1406, \"Data too long for column 'content' at row 1\")")
            return None

        db.commit.side_effect = mock_commit

        ks.repo = MagicMock()
        ks.repo.create.return_value = mock_doc
        ks.get_doc = MagicMock(return_value=mock_doc)
        ks._index_document = MagicMock(return_value=10)

        file_content = "Test content " * 1000
        upload_file = UploadFile(
            filename="test_recovery.txt",
            file=io.BytesIO(file_content.encode("utf-8"))
        )

        doc = ks.upload_file(
            upload_file,
            organization_id=1,
            scope="organization",
            document_type="RAG"
        )

        assert doc is not None
        # Verify ALTER TABLE was attempted
        db.execute.assert_called()
        executed_sql = str(db.execute.call_args[0][0])
        assert "ALTER TABLE knowledge_documents MODIFY COLUMN content LONGTEXT" in executed_sql
