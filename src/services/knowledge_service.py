import os
import re
import zipfile
import xml.etree.ElementTree as ET
import logging
from typing import List, Dict, Any, Optional
from fastapi import HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from src.models.knowledge_doc import KnowledgeDocument, KnowledgeDocStatus
from src.repositories.knowledge_repository import KnowledgeRepository
from src.validators.knowledge_validator import KnowledgeDocCreate, KnowledgeDocUpdate

logger = logging.getLogger(__name__)

UPLOAD_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "uploads", "knowledge"
)
CHUNK_SIZE = 800
CHUNK_OVERLAP = 120


class KnowledgeService:
    """
    Universal Knowledge-Base Ingestion & Indexing Service.

    Pipeline:
        File -> Multi-Format Text Extraction -> Semantic Chunking -> Vector Embeddings -> SQLite Vector Store Indexing

    Supports: PDF, DOCX, TXT, MD, CSV, JSON, HTML, ZIP.
    Works for ANY business domain (hospital, clinic, salon, school, restaurant, IT, legal, hotel, etc.).
    """

    def __init__(self, db: Session):
        self.repo = KnowledgeRepository(db)
        self.db = db

    def list_docs(
        self,
        organization_id: Optional[int] = None,
        scope: Optional[str] = None,
        document_type: Optional[str] = None,
        agent_id: Optional[str] = None,
        super_admin_view: bool = False,
    ) -> List[KnowledgeDocument]:
        return self.repo.get_all(
            organization_id=organization_id,
            scope=scope,
            document_type=document_type,
            agent_id=agent_id,
            super_admin_view=super_admin_view,
        )

    def get_doc(
        self,
        doc_id: int,
        organization_id: Optional[int] = None,
        super_admin_view: bool = False,
    ) -> KnowledgeDocument:
        doc = self.repo.get_by_id(doc_id, organization_id=organization_id, super_admin_view=super_admin_view)
        if not doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Knowledge document with ID {doc_id} not found or access denied",
            )
        return doc

    def _deactivate_conflicting(
        self,
        organization_id: Optional[int],
        scope: str,
        document_type: str,
        agent_id: Optional[str] = None,
        exclude_doc_id: Optional[int] = None,
    ) -> None:
        """Deactivate existing active documents strictly within the SAME scope, organization, and agent."""
        query = self.db.query(KnowledgeDocument).filter(
            KnowledgeDocument.scope == scope,
            KnowledgeDocument.ai_usage == "Active",
        )
        if organization_id is not None:
            query = query.filter(KnowledgeDocument.organization_id == organization_id)
        else:
            query = query.filter(KnowledgeDocument.organization_id.is_(None))

        if agent_id:
            query = query.filter(KnowledgeDocument.agent_id == agent_id)
        if exclude_doc_id is not None:
            query = query.filter(KnowledgeDocument.id != exclude_doc_id)

        conflicting_docs = query.all()
        for d in conflicting_docs:
            d.ai_usage = "Inactive"
            try:
                from src.services.rag_service import rag_service
                res = rag_service.collection.get(where={"document_id": str(d.id)})
                if res and res.get("ids"):
                    updated_metas = []
                    for m in res.get("metadatas", []):
                        m_copy = dict(m)
                        m_copy["ai_usage"] = "Inactive"
                        updated_metas.append(m_copy)
                    rag_service.collection.update(ids=res["ids"], metadatas=updated_metas)
            except Exception as meta_err:
                logger.debug(f"SQLite vector metadata update note: {meta_err}")
        self.db.commit()
        try:
            from src.services.rag_service import rag_service
            from src.services.cache_service import cache_service
            rag_service.clear_organization_cache(organization_id)
            if organization_id is not None:
                cache_service.invalidate_organization(organization_id)
            else:
                cache_service.clear_all()
        except Exception:
            pass

    def create_doc(
        self,
        payload: KnowledgeDocCreate,
        organization_id: Optional[int] = None,
        uploaded_by: Optional[int] = None,
        super_admin_mode: bool = False,
    ) -> KnowledgeDocument:
        """
        Creates a knowledge document from payload and immediately indexes its content if present.
        Strictly preserves ownership and scope boundaries.
        """
        logger.info(f"[DOCUMENT CREATE START] Title: '{payload.title}'")
        data = payload.model_dump()

        if super_admin_mode:
            if payload.scope == "superadmin_assistant" or payload.organization_id is None:
                data["organization_id"] = None
                data["scope"] = "superadmin_assistant"
                data["visibility"] = payload.visibility or "superadmin_assistant"
            else:
                data["organization_id"] = payload.organization_id
                data["scope"] = payload.scope or "organization"
                data["visibility"] = payload.visibility or "organization"
        else:
            data["organization_id"] = organization_id if organization_id is not None else payload.organization_id
            data["scope"] = "organization"
            data["visibility"] = "organization"

        if uploaded_by is not None:
            data["uploaded_by"] = uploaded_by

        doc_type = data.get("document_type") or "RAG"
        data["document_type"] = doc_type

        # Deactivate previous active documents for the same scope and type
        if data.get("ai_usage") == "Active":
            self._deactivate_conflicting(
                organization_id=data.get("organization_id"),
                scope=data.get("scope", "organization"),
                document_type=doc_type,
                agent_id=data.get("agent_id")
            )

        doc = self.repo.create(data)

        if doc.content and doc.content.strip():
            try:
                self._index_document(doc, doc.content)
                self.db.commit()
                self.db.refresh(doc)
            except Exception as e:
                logger.error(f"[DOCUMENT CREATE ERROR] Indexing failed: {e}")
                doc.status = KnowledgeDocStatus.FAILED
                self.db.commit()
                self.db.refresh(doc)
        else:
            try:
                from src.services.rag_service import rag_service
                from src.services.cache_service import cache_service
                rag_service.clear_organization_cache(doc.organization_id)
                if doc.organization_id is not None:
                    cache_service.invalidate_organization(doc.organization_id)
                else:
                    cache_service.clear_all()
            except Exception:
                pass

        return doc

    @staticmethod
    def _clean_extracted_text(text: str) -> str:
        """Strips running header/footer lines and cleans noise from extracted PDF/document text."""
        if not text:
            return ""
        # Strip running headers like "Dream Caatchers Properties | Outbound RAG Script \n Page 1"
        text = re.sub(r'(?m)^[^\n]*\|\s*(?:Outbound|Inbound|RAG|Script)[^\n]*$', '', text)
        text = re.sub(r'(?m)^Page\s+\d+\s*$', '', text)
        # Strip test disclaimers if present
        disclaimer_pattern = r'(?i)(?:DEVELOPMENT\s*/\s*TEST\s+DATA\s+(?:ONLY)?|They\s+are\s+not\s+real\s+business\s+information\.?|All\s+.*dummy data|These\s+slots\s+are\s+synthetic\s+development/test\s+data\.?)'
        text = re.sub(disclaimer_pattern, '', text)
        # Normalize non-printable/corrupted bullet symbols and ASCII control characters like \x7f
        text = re.sub(r'[\u25a0\u25a1\ufffd]', '• ', text)
        text = re.sub(r'[\x7f\x00-\x08\x0b\x0c\x0e-\x1f]', ' ', text)
        return text

    @staticmethod
    def _sub_split_text(text: str, max_len: int) -> List[str]:
        """Sub-splits an oversized text block into coherent segments <= max_len respecting sentence and word boundaries."""
        if not text or len(text) <= max_len:
            return [text] if text else []
        sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]
        if len(sentences) <= 1:
            sentences = [l.strip() for l in text.split('\n') if l.strip()]

        chunks = []
        accum = ""
        for s in sentences:
            candidate = f"{accum} {s}".strip() if accum else s
            if len(candidate) <= max_len:
                accum = candidate
            else:
                if accum:
                    chunks.append(accum)
                if len(s) > max_len:
                    words = s.split()
                    w_accum = ""
                    for w in words:
                        w_cand = f"{w_accum} {w}".strip() if w_accum else w
                        if len(w_cand) <= max_len:
                            w_accum = w_cand
                        else:
                            if w_accum:
                                chunks.append(w_accum)
                            w_accum = w
                    accum = w_accum
                else:
                    accum = s
        if accum:
            chunks.append(accum)
        return chunks

    @staticmethod
    def _chunk_text_with_metadata(text: str, source_title: str = "", max_chunk_size: int = 900, min_chunk_size: int = 80, overlap: int = 150) -> List[Dict[str, Any]]:
        """
        Universal Domain-Agnostic Structure-Aware Dynamic Chunker.

        Dynamically detects section boundaries, directory records, Q&A blocks, and paragraph breaks.
        Keeps related facts, locations, and rules together.
        Ensures chunks fit optimal embedding sizes (max 900 chars) with context headers prepended.
        """
        text = KnowledgeService._clean_extracted_text(text).strip()
        if not text:
            return []

        lines = [l.rstrip() for l in text.split('\n')]
        sections = []
        curr_header = "General Information"
        curr_lines = []

        def is_heading_line(line: str) -> bool:
            s = line.strip()
            if not s:
                return False
            # Markdown heading or visual divider
            if re.match(r'^(?:#{1,6}\s+|===+\s*|---+\s*|\*{3,}\s*)', s):
                return True
            # Key section title in ALL-CAPS (3 to 55 chars) without colons or URLs
            if s.isupper() and 3 <= len(s) <= 55 and not s.startswith(('HTTP', 'WWW', 'COMPANY', 'AGENT', 'CUSTOMER')) and ':' not in s:
                return True
            # Structural labels (SECTION, MODULE, CHAPTER, PART, DIRECTORY, RULES, FAQS, POLICY)
            if re.match(r'^(?:SECTION\b|MODULE\b|CHAPTER\b|PART\b|OVERVIEW\b|DIRECTORY\b|RULES\b|FAQS?\b|POLICIES?\b)', s, re.IGNORECASE):
                return True
            return False

        for line in lines:
            if is_heading_line(line) and curr_lines:
                content = '\n'.join(curr_lines).strip()
                if content:
                    sections.append((curr_header, content))
                curr_header = re.sub(r'^[#=:\-\*\s]+', '', line.strip()).strip()
                curr_lines = []
            else:
                curr_lines.append(line)

        content = '\n'.join(curr_lines).strip()
        if content:
            sections.append((curr_header, content))

        raw_chunks = []

        for header, scontent in sections:
            # Detect sub-records (e.g., Project entries: Title followed by Location/Address/Details/Rules)
            record_blocks = re.split(r'\n(?=[A-Z0-9\s/&\-\'\"]+\n(?:Location|Address|Specialty|Department|Services|Example information|Outbound rule|Rules?|Price|Details):)', scontent)

            # Detect Q&A pairs (Q: ... or Question: ... or Customer: ...)
            qa_blocks = re.split(r'\n(?=(?:Q|Question|FAQ)\s*:|\bCustomer\s*:)', scontent)

            if len(record_blocks) > 1:
                blocks = record_blocks
            elif len(qa_blocks) > 1:
                blocks = qa_blocks
            else:
                blocks = [scontent]

            for block in blocks:
                btext = block.strip()
                if not btext:
                    continue

                prefix = f"[{header}]\n" if header and header != "General Information" and not btext.startswith(header) else ""

                if len(prefix + btext) <= max_chunk_size:
                    raw_chunks.append({
                        "text": prefix + btext,
                        "section": header
                    })
                else:
                    # Sub-split oversized blocks at paragraph double newlines or line breaks
                    paras = [p.strip() for p in btext.split('\n\n') if p.strip()]
                    if len(paras) <= 1:
                        paras = [lline.strip() for lline in btext.split('\n') if lline.strip()]

                    # Sub-split any individual paragraph that is still larger than max allowed
                    safe_limit = max(100, max_chunk_size - len(prefix))
                    fine_paras = []
                    for p in paras:
                        if len(p) <= safe_limit:
                            fine_paras.append(p)
                        else:
                            fine_paras.extend(KnowledgeService._sub_split_text(p, safe_limit))

                    accum = ""
                    for p in fine_paras:
                        candidate = f"{accum}\n\n{p}".strip() if accum else p
                        if len(prefix + candidate) <= max_chunk_size:
                            accum = candidate
                        else:
                            if accum:
                                raw_chunks.append({
                                    "text": prefix + accum,
                                    "section": header
                                })
                            accum = p
                    if accum:
                        raw_chunks.append({
                            "text": prefix + accum,
                            "section": header
                        })

        # Merge undersized chunks (< min_chunk_size) within same section
        merged = []
        for item in raw_chunks:
            if merged and len(item["text"]) < min_chunk_size and merged[-1]["section"] == item["section"]:
                if len(merged[-1]["text"]) + len(item["text"]) + 2 <= max_chunk_size:
                    merged[-1]["text"] = f"{merged[-1]['text']}\n\n{item['text']}".strip()
                    continue
            merged.append(item)

        total_cnt = len(merged)
        for i, item in enumerate(merged):
            item["chunk_index"] = i
            item["total_chunks"] = total_cnt

        return merged

    @staticmethod
    def _chunk_text(text: str) -> List[str]:
        items = KnowledgeService._chunk_text_with_metadata(text)
        return [it["text"] for it in items]

    def _extract_text(self, file_path: str, file_bytes: bytes, ext: str) -> str:
        """
        Extracts clean plain text across multiple document formats:
        - PDF: via pypdf
        - DOCX: via zip XML parsing of word/document.xml (zero external docx library dependency)
        - TXT, MD, CSV, JSON, HTML: UTF-8 with fallback decoding
        - ZIP: Unpacks and extracts contained documents
        """
        try:
            # 1. Plain text and markup
            if ext in ["TXT", "MD", "CSV", "JSON", "HTML"]:
                try:
                    return file_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    return file_bytes.decode("latin-1", errors="ignore")

            # 2. PDF Documents
            if ext == "PDF":
                try:
                    import pypdf
                except ImportError:
                    raise HTTPException(
                        status_code=500,
                        detail="PDF parsing library 'pypdf' is missing.",
                    )

                with open(file_path, "rb") as pdf_file:
                    reader = pypdf.PdfReader(pdf_file)
                    extracted_pages = []
                    for idx, page in enumerate(reader.pages):
                        page_text = page.extract_text()
                        if page_text:
                            extracted_pages.append(page_text.strip())
                    return "\n\n".join(extracted_pages)

            # 3. Microsoft Word (.DOCX) Documents
            if ext == "DOCX":
                try:
                    with zipfile.ZipFile(file_path, 'r') as zf:
                        xml_content = zf.read('word/document.xml')
                        tree = ET.fromstring(xml_content)
                        # Extract all w:t text nodes within Word XML namespace
                        paragraphs = []
                        for p in tree.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'):
                            texts = [t.text for t in p.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t') if t.text]
                            if texts:
                                paragraphs.append("".join(texts).strip())
                        return "\n\n".join(paragraphs)
                except Exception as docx_err:
                    logger.warning(f"DOCX XML extraction error: {docx_err}. Attempting fallback string extraction.")
                    # Fallback string extraction for older or atypical docx structures
                    clean_strings = re.findall(r'[A-Za-z0-9\s.,!?:;\'"()\-_/\$\€\₹]{4,}', file_bytes.decode('latin-1', errors='ignore'))
                    return "\n".join(clean_strings)

            # 4. Old Microsoft Word (.DOC) Binary
            if ext == "DOC":
                clean_strings = re.findall(r'[A-Za-z0-9\s.,!?:;\'"()\-_/\$\€\₹]{4,}', file_bytes.decode('latin-1', errors='ignore'))
                return "\n".join(clean_strings)

            # 5. ZIP Archive of Documents
            if ext == "ZIP":
                import io
                extracted_texts = []
                with zipfile.ZipFile(file_path, 'r') as zf:
                    for name in zf.namelist():
                        if name.endswith("/") or name.startswith("__MACOSX") or name.split("/")[-1].startswith("."):
                            continue

                        sub_ext = name.split(".")[-1].upper() if "." in name else "TXT"
                        try:
                            file_data = zf.read(name)
                            if sub_ext in ["TXT", "MD", "CSV", "JSON", "HTML"]:
                                extracted_texts.append(f"--- Document: {name} ---\n" + file_data.decode("utf-8", errors="ignore"))
                            elif sub_ext == "PDF":
                                import pypdf
                                reader = pypdf.PdfReader(io.BytesIO(file_data))
                                pdf_text = "\n".join(p.extract_text() or "" for p in reader.pages)
                                extracted_texts.append(f"--- Document: {name} ---\n" + pdf_text)
                            elif sub_ext == "DOCX":
                                with zipfile.ZipFile(io.BytesIO(file_data), 'r') as sub_zf:
                                    xml_content = sub_zf.read('word/document.xml')
                                    tree = ET.fromstring(xml_content)
                                    paragraphs = []
                                    for p in tree.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'):
                                        texts = [t.text for t in p.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t') if t.text]
                                        if texts:
                                            paragraphs.append("".join(texts).strip())
                                    extracted_texts.append(f"--- Document: {name} ---\n" + "\n\n".join(paragraphs))
                        except Exception as e:
                            logger.warning(f"Skipping {name} in zip: {e}")

                return "\n\n".join(extracted_texts)

            # Default fallback
            return file_bytes.decode("utf-8", errors="ignore")

        except HTTPException:
            raise
        except Exception as exc:
            logger.error(f"[TEXT EXTRACTION ERROR] File: {file_path}, Error: {exc}")
            raise HTTPException(
                status_code=400,
                detail=f"Failed to process document content: {exc}",
            )

    def _index_document(self, doc: KnowledgeDocument, text: str) -> int:
        """
        Chunks the document text and upserts all chunks into ChromaDB Vector Store with full multi-tenant isolation metadata.
        """
        from src.services.rag_service import rag_service
        from src.services.chroma_vector_store import chroma_vector_store
        from src.services.sqlite_vector_store import sqlite_vector_store

        logger.info(f"[DOCUMENT INDEXING START] Document ID: {doc.id}, Title: '{doc.title}'")
        structured_chunks = self._chunk_text_with_metadata(text, doc.title)
        if not structured_chunks:
            raise ValueError("Document contains no indexable text content.")

        logger.info(f"[CHUNKS CREATED] Generated {len(structured_chunks)} semantic chunks for doc {doc.id}")

        # Remove any previous vector chunks for this document across stores
        try:
            chroma_vector_store.delete(where={"document_id": str(doc.id)})
            sqlite_vector_store.delete(where={"document_id": str(doc.id)})
        except Exception as del_err:
            logger.debug(f"Previous chunk cleanup note for doc {doc.id}: {del_err}")

        ids = [f"doc-{doc.id}-chunk-{i}" for i in range(len(structured_chunks))]
        documents = [item["text"] for item in structured_chunks]

        # Build isolation-aware metadata for every chunk
        org_id_str = str(doc.organization_id) if doc.organization_id is not None else "super_admin"
        doc_scope = getattr(doc, "scope", None) or "organization"
        doc_type = getattr(doc, "document_type", None) or "RAG"
        doc_visibility = getattr(doc, "visibility", None) or "organization"
        doc_agent_id = str(getattr(doc, "agent_id", None) or "")
        doc_assistant_id = str(getattr(doc, "assistant_id", None) or "")

        metadatas = [
            {
                "source": doc.title,
                "document_id": str(doc.id),
                "organization_id": org_id_str,
                "scope": doc_scope,
                "document_type": doc_type,
                "visibility": doc_visibility,
                "agent_id": doc_agent_id,
                "assistant_id": doc_assistant_id,
                "section": item["section"],
                "chunk_index": item["chunk_index"],
                "total_chunks": len(structured_chunks),
                "ai_usage": getattr(doc, "ai_usage", "Active") or "Active",
            }
            for item in structured_chunks
        ]

        logger.info(f"[EMBEDDING START] Inserting {len(documents)} vectors into ChromaDB Vector Store (org={org_id_str}, scope={doc_scope}, type={doc_type})...")
        chroma_vector_store.upsert(
            ids=ids,
            documents=documents,
            metadatas=metadatas,
        )
        sqlite_vector_store.upsert(
            ids=ids,
            documents=documents,
            metadatas=metadatas,
        )

        indexed_count = chroma_vector_store.count(where={"document_id": str(doc.id)})
        logger.info(f"[VECTOR INSERT SUCCESS] Successfully inserted and verified {indexed_count} vectors for doc {doc.id} in ChromaDB.")

        rag_service.clear_organization_cache(doc.organization_id)

        doc.chunks = len(structured_chunks)
        doc.status = KnowledgeDocStatus.INDEXED
        logger.info(f"[DOCUMENT INDEXING COMPLETE] Document {doc.id} marked as INDEXED.")
        return len(structured_chunks)

    def upload_file(
        self,
        file: UploadFile,
        organization_id: Optional[int] = None,
        scope: Optional[str] = None,
        document_type: Optional[str] = None,
        agent_id: Optional[str] = None,
        assistant_id: Optional[str] = None,
        uploaded_by: Optional[int] = None,
        super_admin_mode: bool = False,
    ) -> KnowledgeDocument:
        """
        Full Document Ingestion Pipeline with strict multi-tenant isolation:
        Upload -> Save -> Extract Text -> Validate -> Chunk -> Generate Embeddings -> ChromaDB Insert -> Update DB Status
        """
        logger.info(f"[DOCUMENT UPLOAD START] Filename: '{file.filename}'")
        os.makedirs(UPLOAD_DIR, exist_ok=True)

        safe_name = os.path.basename(file.filename or "document.txt")
        file_path = os.path.join(UPLOAD_DIR, safe_name)
        file_bytes = file.file.read()

        if not file_bytes:
            raise HTTPException(
                status_code=400,
                detail="Uploaded file is empty.",
            )

        with open(file_path, "wb") as f:
            f.write(file_bytes)

        ext = safe_name.split(".")[-1].upper() if "." in safe_name else "TXT"

        # Determine scope & ownership
        effective_scope = scope or "organization"
        effective_doc_type = document_type or "RAG"
        effective_visibility = effective_scope

        if super_admin_mode and organization_id is None and scope == "superadmin_assistant":
            effective_scope = "superadmin_assistant"
            effective_visibility = "superadmin_assistant"

        # Create the DB record first in PROCESSING status
        doc_data = {
            "organization_id": organization_id,  # None for super admin, int for org
            "title": safe_name,
            "file_type": ext,
            "file_path": file_path,
            "content": "",
            "chunks": 0,
            "ai_usage": "Active",
            "status": KnowledgeDocStatus.PROCESSING,
            "scope": effective_scope,
            "document_type": effective_doc_type,
            "visibility": effective_visibility,
            "agent_id": agent_id,
            "assistant_id": assistant_id,
            "uploaded_by": uploaded_by,
        }

        # Scope-aware deactivation: only deactivate docs within the SAME scope/org/type
        self._deactivate_conflicting(
            organization_id=organization_id,
            scope=effective_scope,
            document_type=effective_doc_type,
            agent_id=agent_id,
        )

        doc = self.repo.create(doc_data)

        try:
            text_content = self._extract_text(file_path, file_bytes, ext)
            if not text_content or not text_content.strip():
                raise ValueError("No readable text could be extracted from this document.")

            logger.info(f"[TEXT EXTRACTION SUCCESS] Extracted {len(text_content)} characters from '{safe_name}'")
            doc.content = text_content
            try:
                self.db.commit()
                self.db.refresh(doc)
            except Exception as commit_err:
                err_str = str(commit_err).lower()
                if "1406" in err_str or "data too long" in err_str:
                    logger.warning("[MYSQL DATA_TOO_LONG] Detected 1406 error on knowledge_documents.content. Upgrading column to LONGTEXT...")
                    self.db.rollback()
                    from sqlalchemy import text
                    try:
                        self.db.execute(text("ALTER TABLE knowledge_documents MODIFY COLUMN content LONGTEXT"))
                        self.db.commit()
                        doc = self.get_doc(doc.id, organization_id=organization_id, super_admin_view=super_admin_mode)
                        doc.content = text_content
                        self.db.commit()
                        self.db.refresh(doc)
                        logger.info("[MYSQL AUTO-MIGRATION] Successfully modified content to LONGTEXT and saved full document.")
                    except Exception as alt_err:
                        logger.warning(f"[MYSQL AUTO-MIGRATION FAILED] Could not alter column: {alt_err}. Truncating content to 60000 chars for DB record.")
                        self.db.rollback()
                        doc = self.get_doc(doc.id, organization_id=organization_id, super_admin_view=super_admin_mode)
                        doc.content = text_content[:60000]
                        self.db.commit()
                        self.db.refresh(doc)
                else:
                    raise

            self._index_document(doc, text_content)
            self.db.commit()
            self.db.refresh(doc)

        except Exception as exc:
            self.db.rollback()
            doc = self.get_doc(doc.id, organization_id=organization_id, super_admin_view=super_admin_mode)
            doc.status = KnowledgeDocStatus.FAILED
            doc.chunks = 0
            self.db.commit()
            self.db.refresh(doc)

            logger.error(f"[DOCUMENT UPLOAD FAILED] Indexing failed for doc {doc.id}: {exc}")
            raise HTTPException(
                status_code=500,
                detail=f"Document indexing failed: {exc}",
            )

        return doc

    def reindex_doc(self, doc_id: int, organization_id: Optional[int] = None) -> KnowledgeDocument:
        """Deletes previous vectors and rebuilds chunks/embeddings for a specific document."""
        doc = self.get_doc(doc_id, organization_id=organization_id)
        doc.status = KnowledgeDocStatus.PROCESSING
        self.db.commit()

        try:
            self._index_document(doc, doc.content or "")
            self.db.commit()
            self.db.refresh(doc)
            return doc
        except Exception as exc:
            self.db.rollback()
            doc = self.get_doc(doc_id, organization_id=organization_id)
            doc.status = KnowledgeDocStatus.FAILED
            self.db.commit()
            raise HTTPException(
                status_code=500,
                detail=f"Document re-indexing failed: {exc}",
            )

    def search_docs(self, query: str, organization_id: Optional[int] = None) -> List[KnowledgeDocument]:
        all_docs = self.list_docs(organization_id=organization_id)
        if not query:
            return all_docs

        q = query.lower()
        return [
            doc for doc in all_docs
            if q in doc.title.lower()
            or (doc.content and q in doc.content.lower())
        ]

    def update_doc(
        self,
        doc_id: int,
        payload: KnowledgeDocUpdate,
        organization_id: Optional[int] = None,
        super_admin_view: bool = False,
    ) -> KnowledgeDocument:
        doc = self.get_doc(doc_id, organization_id=organization_id, super_admin_view=super_admin_view)
        changes = payload.model_dump(exclude_unset=True) if hasattr(payload, "model_dump") else dict(payload)

        if "ai_usage" in changes:
            new_usage = changes["ai_usage"]
            if new_usage == "Active":
                # Scope-aware deactivation
                self._deactivate_conflicting(
                    organization_id=doc.organization_id,
                    scope=doc.scope or "organization",
                    document_type=doc.document_type or "RAG",
                    agent_id=doc.agent_id,
                    exclude_doc_id=doc.id,
                )

        doc = self.repo.update(doc, changes)

        from src.services.rag_service import rag_service
        from src.services.cache_service import cache_service
        rag_service.clear_organization_cache(doc.organization_id)
        if doc.organization_id is not None:
            cache_service.invalidate_organization(doc.organization_id)
        else:
            cache_service.clear_all()

        if "content" in changes:
            return self.reindex_doc(doc_id, organization_id=organization_id)

        return doc

    def delete_doc(self, doc_id: int, organization_id: Optional[int] = None, super_admin_view: bool = False) -> None:
        doc = self.get_doc(doc_id, organization_id=organization_id, super_admin_view=super_admin_view)

        if doc.file_path and os.path.exists(doc.file_path):
            try:
                os.remove(doc.file_path)
            except Exception:
                pass

        try:
            from src.services.rag_service import rag_service
            from src.services.cache_service import cache_service
            rag_service.delete_text(str(doc_id))
            rag_service.clear_organization_cache(doc.organization_id)
            if doc.organization_id is not None:
                cache_service.invalidate_organization(doc.organization_id)
            else:
                cache_service.clear_all()
        except Exception:
            pass

        self.repo.delete(doc)

    def get_doc_file(self, doc_id: int, organization_id: Optional[int] = None, super_admin_view: bool = False):
        from fastapi.responses import FileResponse, Response

        doc = self.get_doc(doc_id, organization_id=organization_id, super_admin_view=super_admin_view)
        if doc.file_path and os.path.exists(doc.file_path):
            return FileResponse(
                doc.file_path,
                filename=doc.title,
                headers={"Content-Disposition": f'attachment; filename="{doc.title}"'}
            )
        elif doc.content:
            ext = (doc.file_type or "txt").lower()
            filename = doc.title if doc.title.endswith(f".{ext}") else f"{doc.title}.{ext}"
            return Response(
                content=doc.content.encode("utf-8"),
                media_type="text/plain; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'}
            )
        else:
            raise HTTPException(status_code=404, detail="File content not found on disk or database")

    def reindex_all_docs(self) -> int:
        """Re-indexes all database documents into SQLite Vector Store."""
        from src.services.rag_service import rag_service

        docs = self.list_docs(super_admin_view=True)
        count = 0
        for doc in docs:
            if doc.content and doc.content.strip():
                try:
                    self._index_document(doc, doc.content)
                    count += 1
                except Exception as e:
                    logger.error(f"Error reindexing doc {doc.id}: {e}")
        self.db.commit()
        return count
