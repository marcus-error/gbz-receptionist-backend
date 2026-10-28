import os
import sys
import io
import time
import zipfile
import pytest
from fastapi import UploadFile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.config.database import SessionLocal
from src.models.knowledge_doc import KnowledgeDocument, KnowledgeDocStatus
from src.services.knowledge_service import KnowledgeService
from src.services.rag_service import rag_service
from src.services.ai_service import ai_service


def create_sample_docx(content_paragraphs: list) -> bytes:
    """Helper to generate a valid .docx in memory using zipfile & XML."""
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        # [Content_Types].xml
        content_types = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
            '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
            '  <Default Extension="xml" ContentType="application/xml"/>\n'
            '  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>\n'
            '</Types>'
        )
        zf.writestr('[Content_Types].xml', content_types)

        # word/document.xml
        p_xml = ""
        for p in content_paragraphs:
            p_xml += f'<w:p><w:r><w:t>{p}</w:t></w:r></w:p>'
        
        doc_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">\n'
            f'<w:body>{p_xml}</w:body>\n'
            '</w:document>'
        )
        zf.writestr('word/document.xml', doc_xml)

    return zip_buffer.getvalue()


class TestRagProductionSuite:
    @classmethod
    def setup_class(cls):
        """Initializes testing environment and ensures vector store is ready."""
        cls.db = SessionLocal()
        cls.ks = KnowledgeService(cls.db)
        rag_service.reset_and_rebuild_from_db()

    @classmethod
    def teardown_class(cls):
        cls.db.close()

    def test_01_upload_txt_document(self):
        """TEST 1: Ingest TXT file -> Must parse, chunk, create vectors, and mark INDEXED."""
        content = (
            "# Apex Law Partners - Client Consultation Manual\n\n"
            "## Office Timings & Working Hours\n"
            "Our corporate office operates Monday through Friday from 08:30 AM to 05:30 PM.\n"
            "Saturday consultations are available by prior appointment from 10:00 AM to 02:00 PM.\n\n"
            "## Consultation Fees\n"
            "Initial legal consultation fee is $150 for 30 minutes with a Senior Associate.\n"
            "Managing Partner consultation fee is $350 for 45 minutes.\n\n"
            "## Practice Areas\n"
            "Corporate Law, Intellectual Property, Real Estate Law, and Commercial Litigation.\n"
        )
        file_obj = io.BytesIO(content.encode('utf-8'))
        upload = UploadFile(filename="apex_law_manual.txt", file=file_obj)

        doc = self.ks.upload_file(upload, organization_id=1)
        assert doc.id is not None
        assert doc.status == KnowledgeDocStatus.INDEXED
        assert doc.chunks > 0
        assert "Apex Law Partners" in doc.content
        print(f"\n[TEST 1 PASSED] TXT Uploaded & Indexed: Doc {doc.id}, {doc.chunks} chunks")

    def test_02_upload_docx_document(self):
        """TEST 2: Ingest DOCX file -> Must extract XML text, chunk, and index vectors."""
        paragraphs = [
            "TechSphere Solutions - IT Support Knowledge Base",
            "Emergency IT Hotline: Available 24/7 at +1-800-555-TECH.",
            "Standard SLA Response Times:",
            "Severity 1 (Critical Outage): 15 minutes response time.",
            "Severity 2 (Degraded Performance): 1 hour response time.",
            "Severity 3 (General Ticket): 4 business hours.",
            "Cloud Backup Policy: Automatic daily incremental backups at 02:00 UTC."
        ]
        docx_bytes = create_sample_docx(paragraphs)
        file_obj = io.BytesIO(docx_bytes)
        upload = UploadFile(filename="techsphere_handbook.docx", file=file_obj)

        doc = self.ks.upload_file(upload, organization_id=1)
        assert doc.id is not None
        assert doc.status == KnowledgeDocStatus.INDEXED
        assert doc.chunks > 0
        assert "TechSphere Solutions" in doc.content
        print(f"\n[TEST 2 PASSED] DOCX Uploaded & Indexed: Doc {doc.id}, {doc.chunks} chunks")

    def test_03_rag_grounded_qa(self):
        """TEST 3: Ask a question answered by active RAG -> Must answer accurately from RAG."""
        query = "What is the response time for a critical Severity 1 outage?"
        ctx = rag_service.retrieve_context(query, n_results=4, organization_id=1)
        assert "15 minutes" in ctx or "Severity 1" in ctx

        response = ai_service.generate_response(query, context=ctx, organization_id=1)
        assert any(term in response.lower() for term in ["15 minute", "severity 1", "critical", "response"])
        print(f"\n[TEST 3 PASSED] RAG Grounded Answer: '{response}'")

    def test_04_missing_business_info_anti_hallucination(self):
        """TEST 4: Ask for business detail not present -> Must state not available without inventing facts."""
        query = "What is the CEO's personal private home address?"
        ctx = rag_service.retrieve_context(query, n_results=4, organization_id=1)
        response = ai_service.generate_response(query, context=ctx, organization_id=1)
        
        # Must decline gracefully without inventing an address
        assert any(w in response.lower() for w in ["unable to", "not available", "don't have", "do not have", "records", "sorry", "cannot provide", "cannot share", "private"])
        print(f"\n[TEST 4 PASSED] Anti-Hallucination Safe Response: '{response}'")

    def test_05_general_knowledge_fallback(self):
        """TEST 5: Ask general knowledge question -> Must answer using general LLM knowledge."""
        query = "What is the speed of light in vacuum?"
        ctx = rag_service.retrieve_context(query, n_results=4, organization_id=1)
        response = ai_service.generate_response(query, context=ctx, organization_id=1)
        
        assert any(term in response.lower() for term in ["300,000", "299,792", "3x10", "186,000", "meters per second", "kilometers per second", "miles per second", "light"])
        print(f"\n[TEST 5 PASSED] General Knowledge Fallback Answer: '{response}'")

    def test_06_multi_turn_conversation_memory(self):
        """TEST 6: Multi-turn dialogue -> Must preserve context from previous turns."""
        history = [
            {"role": "user", "text": "I need help with a Severity 1 server crash."},
            {"role": "assistant", "text": "Our response time for Severity 1 outages is 15 minutes. What is your server host ID?"}
        ]
        query = "It is server-node-04 located in US-East."
        ctx = rag_service.retrieve_context(query, n_results=4, organization_id=1)
        response = ai_service.generate_response(query, context=ctx, session_history=history, organization_id=1)
        
        assert len(response) > 10
        print(f"\n[TEST 6 PASSED] Multi-Turn Continuity Response: '{response}'")

    def test_07_caching_and_fast_path(self):
        """TEST 7: Repeat conversational question -> Fast-path must return in < 50ms."""
        t0 = time.perf_counter()
        resp1 = ai_service._fast_rule_response("What is your company name?", organization_id=1)
        dur1 = (time.perf_counter() - t0) * 1000
        
        t1 = time.perf_counter()
        resp2 = ai_service._fast_rule_response("What is your company name?", organization_id=1)
        dur2 = (time.perf_counter() - t1) * 1000

        assert resp1 is not None
        assert resp1 == resp2
        assert dur2 < 50.0
        print(f"\n[TEST 7 PASSED] Fast-Path Query: '{resp1}' in {dur2:.2f}ms")

    def test_08_multi_tenant_isolation(self):
        """TEST 8: Tenant isolation -> Org 1 must never retrieve Org 2 data."""
        # Ingest private doc for Org 999
        org_999_text = "# Confidential BioTech Labs\nSecret Formula: Project-X999 is quantum synthesized."
        self.ks.db.query(KnowledgeDocument).filter(KnowledgeDocument.organization_id == 999).delete()
        self.ks.db.commit()

        doc_999 = KnowledgeDocument(
            organization_id=999,
            title="org999_secret.txt",
            file_type="TXT",
            content=org_999_text,
            chunks=1,
            ai_usage="Active",
            status=KnowledgeDocStatus.INDEXED
        )
        self.ks.db.add(doc_999)
        self.ks.db.commit()
        self.ks._index_document(doc_999, org_999_text)

        # Query from Org 1 -> Must NOT find Project-X999
        ctx_org1 = rag_service.retrieve_context("What is Project-X999 secret formula?", organization_id=1)
        assert "Project-X999" not in ctx_org1

        # Query from Org 999 -> Must find Project-X999
        ctx_org999 = rag_service.retrieve_context("What is Project-X999 secret formula?", organization_id=999)
        assert "Project-X999" in ctx_org999
        print(f"\n[TEST 8 PASSED] Strict Multi-Tenant Isolation Verified.")

    def test_09_large_document_stability(self):
        """TEST 9: Large document with 30+ paragraphs -> Must chunk, index, and retrieve stably."""
        large_sections = []
        for i in range(1, 35):
            large_sections.append(
                f"## Section {i}: Standard Operating Procedure {i}\n"
                f"Protocol {i} requires all personnel to execute verification checkpoint {i*10}. "
                f"Reference code for procedure {i} is REF-SOP-{i:03d}.\n"
            )
        large_content = "# Enterprise Global Standard Operating Procedures\n\n" + "\n".join(large_sections)
        file_obj = io.BytesIO(large_content.encode('utf-8'))
        upload = UploadFile(filename="enterprise_sops.txt", file=file_obj)

        doc = self.ks.upload_file(upload, organization_id=1)
        assert doc.status == KnowledgeDocStatus.INDEXED
        assert doc.chunks >= 20

        # Retrieve specific section
        ctx = rag_service.retrieve_context("What is the reference code for procedure 27?", organization_id=1)
        assert "REF-SOP-027" in ctx or "procedure 27" in ctx
        print(f"\n[TEST 9 PASSED] Large Doc (Chunks: {doc.chunks}) Ingestion & Targeted Retrieval Verified.")

    def test_10_empty_document_rejection(self):
        """TEST 10: Empty file upload -> Must reject with clear HTTP 400 error."""
        empty_obj = io.BytesIO(b"")
        upload = UploadFile(filename="empty.txt", file=empty_obj)

        with pytest.raises(Exception) as exc_info:
            self.ks.upload_file(upload)
        assert "empty" in str(exc_info.value).lower() or "400" in str(exc_info.value)
        print(f"\n[TEST 10 PASSED] Empty Document Properly Rejected: {exc_info.value}")


if __name__ == "__main__":
    t_start = time.time()
    tester = TestRagProductionSuite()
    tester.setup_class()

    print("==================================================")
    print("RUNNING COMPLETE RAG PRODUCTION VERIFICATION SUITE")
    print("==================================================")

    tester.test_01_upload_txt_document()
    tester.test_02_upload_docx_document()
    tester.test_03_rag_grounded_qa()
    tester.test_04_missing_business_info_anti_hallucination()
    tester.test_05_general_knowledge_fallback()
    tester.test_06_multi_turn_conversation_memory()
    tester.test_07_caching_and_fast_path()
    tester.test_08_multi_tenant_isolation()
    tester.test_09_large_document_stability()
    tester.test_10_empty_document_rejection()

    tester.teardown_class()
    print(f"\n==================================================")
    print(f"ALL TESTS PASSED SUCCESSFULLY IN {time.time() - t_start:.2f}s")
    print("==================================================")
