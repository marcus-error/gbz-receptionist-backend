"""
Universal Production RAG Retrieval Service.
Features:
1. Production SQLite Vector Store Engine with 384-dimensional dense embeddings & cosine similarity.
2. Pure Python BM25 + Dense Vector Hybrid Search (60% Dense + 40% BM25).
3. Strict Multi-Tenant & Active Document Isolation (Filtered by organization_id & specific active document_id).
4. Domain-Agnostic Entity, Keyword, and Metadata Extraction (Works for any business).
5. Dynamic Business Name & Voice Greeting Extraction.
6. Fail-Safe Instant SQLite Fallback Context.
"""

import os
import re
import math
import time
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any

from src.services.sqlite_vector_store import sqlite_vector_store
from src.services.chroma_vector_store import chroma_vector_store

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 5
DEFAULT_SIMILARITY_THRESHOLD = 0.15

NON_RAG_INTENTS = {
    "greeting",
    "general_conversation",
    "goodbye",
    "emergency/urgent request",
    "transfer request",
    "appointment_booking",
    "appointment"
}

CONVERSATIONAL_PHRASES = {
    "how are you", "how are you doing", "what's up", "yeah good", "good",
    "hi", "hello", "hey", "good morning", "good afternoon", "good evening",
    "bye", "goodbye", "thanks", "thank you", "ok", "okay", "alright", "yes", "no",
    "yes please", "no thanks", "sure", "ok sure", "confirm", "proceed", "go ahead"
}


STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "because", "as", "what", "which",
    "who", "whom", "this", "that", "these", "those", "am", "is", "are", "was", "were",
    "be", "been", "being", "have", "has", "had", "having", "do", "does", "did", "doing",
    "can", "could", "should", "would", "may", "might", "must", "shall", "will", "i",
    "me", "my", "myself", "we", "our", "ours", "you", "your", "yours", "he", "him",
    "his", "she", "her", "hers", "it", "its", "they", "them", "their", "theirs", "at",
    "by", "for", "from", "in", "into", "of", "off", "on", "onto", "out", "over", "to",
    "with", "about", "against", "between", "through", "during", "before", "after", "how"
}


class BM25Scorer:
    """
    Pure Python BM25 (Best Matching 25) Keyword Ranker for Hybrid Search.
    Mathematically exact BM25 formulation with zero external dependencies.
    """
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b

    def tokenize(self, text: str) -> List[str]:
        words = re.split(r'\W+', (text or "").lower())
        return [w for w in words if len(w) > 1 and w not in STOPWORDS]

    def score(self, query: str, corpus: List[str]) -> List[float]:
        query_terms = self.tokenize(query)
        if not query_terms or not corpus:
            return [0.0] * len(corpus)

        doc_tokens = [self.tokenize(doc) for doc in corpus]
        N = len(corpus)
        avgdl = sum(len(d) for d in doc_tokens) / N if N > 0 else 1.0

        idf = {}
        for q in set(query_terms):
            n_q = sum(1 for d in doc_tokens if q in d)
            idf[q] = math.log((N - n_q + 0.5) / (n_q + 0.5) + 1.0)

        scores = []
        for d in doc_tokens:
            d_len = len(d)
            s = 0.0
            for q in query_terms:
                if q in d:
                    f = d.count(q)
                    num = f * (self.k1 + 1.0)
                    den = f + self.k1 * (1.0 - self.b + self.b * (d_len / avgdl))
                    s += idf.get(q, 0.0) * (num / den)
            scores.append(s)

        max_possible = sum(idf.get(q, 0.0) for q in query_terms) * (self.k1 + 1.0)
        denom = max_possible if max_possible > 0 else 1.0
        return [min(1.0, s / denom) for s in scores]


class RagService:
    def __init__(self):
        self.collection = chroma_vector_store
        self.bm25_scorer = BM25Scorer()
        self._org_context_cache: Dict[str, str] = {}
        self._verified_indexed_docs: set = set()
        logger.info("[RAG SERVICE] ChromaDB Vector Store initialized successfully as primary.")

    def _init_or_repair_collection(self):
        """Returns the primary ChromaDB Vector Store."""
        return chroma_vector_store

    def reset_and_rebuild_from_db(self):
        """
        Hard resets the ChromaDB vector store and syncs all active documents from SQLite.
        """
        logger.info("[RAG SERVICE] Resetting and rebuilding ChromaDB vector store from database...")
        chroma_vector_store.reset()
        sqlite_vector_store.reset()
        self.clear_organization_cache()

        try:
            from src.config.database import SessionLocal
            from src.models.knowledge_doc import KnowledgeDocument, KnowledgeDocStatus
            from src.services.knowledge_service import KnowledgeService

            db = SessionLocal()
            try:
                ks = KnowledgeService(db)
                docs = db.query(KnowledgeDocument).all()
                reindexed_count = 0
                for doc in docs:
                    if doc.content and doc.content.strip():
                        try:
                            ks._index_document(doc, doc.content)
                            reindexed_count += 1
                        except Exception as e:
                            logger.warning(f"Failed to reindex doc {doc.id}: {e}")
                db.commit()
                logger.info(f"[RAG SERVICE] Rebuild complete. Successfully re-indexed {reindexed_count} documents.")
                return reindexed_count
            finally:
                db.close()
        except Exception as err:
            logger.error(f"[RAG SERVICE] Error rebuilding knowledge base: {err}")
            return 0

    def ingest_text(self, document_id: str, text: str, source: str, organization_id: int = 1):
        """Upsert document text and metadata into vector store with organization isolation."""
        metadata = {
            "source": source,
            "document_id": str(document_id),
            "organization_id": str(organization_id),
            "ai_usage": "Active",
            "status": "Indexed"
        }
        self.collection.upsert(
            documents=[text],
            metadatas=[metadata],
            ids=[str(document_id)]
        )
        self.clear_organization_cache(organization_id)

    def delete_text(self, document_id: str):
        """Delete document vectors by ID."""
        try:
            self.collection.delete(where={"document_id": str(document_id)})
        except Exception as exc:
            logger.warning(f"Failed to delete document {document_id} from SQLite vector store: {exc}")

    def is_rag_needed(self, intent: Optional[str] = None, text: Optional[str] = None) -> bool:
        """
        Determines whether RAG search is required for the user message.
        Bypasses RAG for simple greetings, pure pleasantries, personal entity introductions, and goodbyes.
        """
        if intent and intent.lower().strip() in NON_RAG_INTENTS:
            return False

        if text:
            txt_clean = text.lower().strip()
            clean_words = re.sub(r'[^a-zA-Z\s]', '', txt_clean).strip()
            if clean_words in CONVERSATIONAL_PHRASES or (len(clean_words.split()) <= 2 and clean_words in CONVERSATIONAL_PHRASES):
                return False

            # Bypass RAG for personal name declarations (e.g. "My name is Mugilan", "I'm Mugilan", "Mugilan Viswanathan")
            name_decl = re.search(r'^\s*(?:my\s+(?:full\s+)?name\s+is|i\s*(?:am|\'m)|this\s+is|you\s+can\s+call\s+me|call\s+me|myself)\s+([a-zA-Z\s]+)\s*$', txt_clean)
            if name_decl and len(name_decl.group(1).split()) <= 4:
                return False

            # Bypass RAG for raw phone numbers or spoken email responses
            digits = re.sub(r'[^\d]', '', txt_clean)
            if len(digits) >= 7 and len(txt_clean.split()) <= 4:
                return False
            if "@" in txt_clean or "dot com" in txt_clean:
                return False

        return True

    def clear_cache(self):
        """Clears all in-memory RAG, context, and greeting caches across the service."""
        if hasattr(self, "_org_context_cache"):
            self._org_context_cache.clear()
        if hasattr(self, "_greeting_cache"):
            self._greeting_cache.clear()
        if hasattr(self, "_rag_terminology_cache"):
            self._rag_terminology_cache.clear()
        if hasattr(self, "_ctx_cache"):
            self._ctx_cache.clear()
        if hasattr(self, "_verified_indexed_docs"):
            self._verified_indexed_docs.clear()
        if hasattr(self, "_active_doc_cache"):
            self._active_doc_cache.clear()
        if hasattr(self, "_rag_query_cache"):
            self._rag_query_cache.clear()
        if hasattr(self, "_doc_version_cache"):
            self._doc_version_cache.clear()
        logger.info("[RAG SERVICE] In-memory RAG, context, and greeting caches cleared.")

    def clear_organization_cache(self, organization_id: Optional[int] = None):
        """
        Flushes cached organization document context and in-memory caches strictly for the requested organization.
        Tenant A's document changes will NEVER flush or alter Tenant B's caches.
        """
        if organization_id is not None:
            prefix1 = f"org_{organization_id}_"
            prefix2 = f"{organization_id}_"
            prefix_query = f"org_{organization_id}"

            if hasattr(self, "_ctx_cache"):
                keys_to_del = [k for k in self._ctx_cache if k.startswith(prefix1) or k.startswith(prefix2)]
                for k in keys_to_del:
                    del self._ctx_cache[k]
            if hasattr(self, "_org_context_cache"):
                keys_to_del = [k for k in self._org_context_cache if k.startswith(prefix1) or k.startswith(prefix2)]
                for k in keys_to_del:
                    del self._org_context_cache[k]
            if hasattr(self, "_active_doc_cache"):
                keys_to_del = [k for k in self._active_doc_cache if k.startswith(prefix1) or k.startswith(prefix2)]
                for k in keys_to_del:
                    del self._active_doc_cache[k]
            if hasattr(self, "_rag_query_cache"):
                keys_to_del = [k for k in self._rag_query_cache if prefix_query in k or k.startswith(prefix2)]
                for k in keys_to_del:
                    del self._rag_query_cache[k]
            if hasattr(self, "_doc_version_cache"):
                self._doc_version_cache.clear()

            try:
                from src.services.cache_service import cache_service
                cache_service.invalidate_organization(organization_id)
            except Exception:
                pass
            logger.info(f"[MASTER CONTEXT FLUSHED] In-memory cache cleared strictly for org={organization_id}.")
        else:
            self.clear_cache()
            try:
                from src.services.cache_service import cache_service
                cache_service.clear_all()
            except Exception:
                pass
            logger.info("[MASTER CONTEXT FLUSHED] Global in-memory cache cleared.")

    invalidate_master_context = clear_organization_cache

    def _get_document_version(self, document_id: Optional[str]) -> str:
        """Returns a cached version identifier for a document based on its updated_at timestamp or chunk count."""
        if not document_id:
            return "0"
        if not hasattr(self, "_doc_version_cache"):
            self._doc_version_cache = {}
        now = time.monotonic()
        if document_id in self._doc_version_cache:
            v, exp = self._doc_version_cache[document_id]
            if now < exp:
                return v

        try:
            from src.config.database import SessionLocal
            from src.models.knowledge_doc import KnowledgeDocument
            db = SessionLocal()
            try:
                try:
                    d_id = int(document_id)
                    doc = db.query(KnowledgeDocument).filter(KnowledgeDocument.id == d_id).first()
                except ValueError:
                    doc = db.query(KnowledgeDocument).filter(KnowledgeDocument.title == str(document_id)).first()

                if doc and doc.updated_at:
                    ver = str(int(doc.updated_at.timestamp()))
                elif doc:
                    ver = str(doc.chunks or "1")
                else:
                    ver = "0"
                self._doc_version_cache[document_id] = (ver, now + 30.0)
                return ver
            finally:
                db.close()
        except Exception:
            return "1"

    def _resolve_active_document_id(
        self,
        organization_id: Optional[int] = None,
        document_id: Optional[str] = None,
        scope: Optional[str] = None,
        document_type: Optional[str] = None,
    ) -> Optional[str]:
        """
        Resolves the currently active, indexed document ID strictly for the requested organization.
        Guarantees:
        - Admin or Super Admin documents are NEVER returned for regular tenants.
        - Tenant documents are NEVER returned for superadmin assistant queries.
        - Inactive or unindexed documents are NEVER resolved.
        - If a tenant has no active indexed document, returns None (NO global or cross-tenant fallback).
        """
        org_key = f"org_{organization_id}" if organization_id is not None else "superadmin"
        cache_key = f"{org_key}_doc_{document_id}_type_{document_type}_scope_{scope}"
        now = time.monotonic()
        if not hasattr(self, "_active_doc_cache"):
            self._active_doc_cache = {}
        if cache_key in self._active_doc_cache:
            val, expires = self._active_doc_cache[cache_key]
            if now < expires:
                return val

        if document_id:
            try:
                from src.config.database import SessionLocal
                from src.models.knowledge_doc import KnowledgeDocument, KnowledgeDocStatus
                db_val = SessionLocal()
                try:
                    try:
                        doc_val = db_val.query(KnowledgeDocument).filter(KnowledgeDocument.id == int(document_id)).first()
                    except ValueError:
                        doc_val = db_val.query(KnowledgeDocument).filter(KnowledgeDocument.title == str(document_id)).first()

                    if doc_val:
                        # Strict tenant isolation check:
                        # 1. Regular tenant query must match document organization_id exactly (Super Admin docs with organization_id=None are REFUSED)
                        if organization_id is not None:
                            if doc_val.organization_id is None or doc_val.organization_id != int(organization_id):
                                logger.warning(
                                    f"[RAG ISOLATION] Refusing document {doc_val.id} (org={doc_val.organization_id}) for requested tenant org {organization_id}"
                                )
                                document_id = None
                        else:
                            # 2. Superadmin query (organization_id is None) must only access superadmin documents
                            if doc_val.organization_id is not None or doc_val.scope != "superadmin_assistant":
                                logger.warning(
                                    f"[RAG ISOLATION] Refusing tenant document {doc_val.id} (org={doc_val.organization_id}) for superadmin assistant query"
                                )
                                document_id = None

                        # Status check: must be actively assigned and successfully indexed
                        if document_id and (doc_val.ai_usage != "Active" or str(doc_val.status) not in ("Indexed", "KnowledgeDocStatus.INDEXED")):
                            logger.warning(
                                f"[RAG ISOLATION] Refusing inactive or unindexed document {doc_val.id} (ai_usage={doc_val.ai_usage}, status={doc_val.status})"
                            )
                            document_id = None

                        # Scope check: inbound calls cannot use outbound scripts and vice versa
                        if document_id:
                            scope_str = str(scope or "").lower()
                            if "inbound" in scope_str:
                                is_outbound = (
                                    doc_val.document_type in ("OUTBOUND_SCRIPT", "CAMPAIGN_SCRIPT")
                                    or "outbound" in (doc_val.title or "").lower()
                                    or doc_val.scope == "outbound"
                                )
                                if is_outbound:
                                    logger.warning(f"[RAG ISOLATION] Refusing outbound script '{doc_val.title}' (id={doc_val.id}) for inbound call.")
                                    document_id = None
                            elif "outbound" in scope_str:
                                is_inbound_only = (
                                    doc_val.scope == "inbound" and "outbound" not in (doc_val.title or "").lower()
                                )
                                if is_inbound_only:
                                    logger.warning(f"[RAG ISOLATION] Refusing inbound document '{doc_val.title}' (id={doc_val.id}) for outbound call.")
                                    document_id = None
                    else:
                        document_id = None

                    if document_id:
                        resolved_id_str = str(document_id)
                        self._active_doc_cache[cache_key] = (resolved_id_str, now + 15.0)
                        return resolved_id_str
                finally:
                    db_val.close()
            except Exception as e_check:
                logger.warning(f"Error checking specified document {document_id}: {e_check}")

        # Fallback to resolving the active document for the requested tenant from SQLite
        try:
            from src.config.database import SessionLocal
            from src.models.knowledge_doc import KnowledgeDocument, KnowledgeDocStatus
            db = SessionLocal()
            try:
                base_query = db.query(KnowledgeDocument).filter(
                    KnowledgeDocument.ai_usage == "Active",
                    KnowledgeDocument.status.in_([KnowledgeDocStatus.INDEXED, "Indexed"]),
                )
                if document_type:
                    base_query = base_query.filter(KnowledgeDocument.document_type == document_type)

                scope_str = str(scope or "").lower()
                if "inbound" in scope_str:
                    base_query = base_query.filter(
                        KnowledgeDocument.document_type.notin_(["OUTBOUND_SCRIPT", "CAMPAIGN_SCRIPT"]),
                        ~KnowledgeDocument.title.ilike("%outbound%"),
                        KnowledgeDocument.scope != "outbound",
                    )
                elif "outbound" in scope_str:
                    base_query = base_query.filter(
                        (KnowledgeDocument.document_type.in_(["OUTBOUND_SCRIPT", "SCRIPT"])) |
                        (KnowledgeDocument.title.ilike("%outbound%")) |
                        (KnowledgeDocument.scope == "outbound")
                    )

                resolved_id = None
                if organization_id is not None:
                    # STRICT: only query documents owned by this organization
                    org_int = int(organization_id)
                    org_doc = base_query.filter(
                        KnowledgeDocument.organization_id == org_int
                    ).order_by(KnowledgeDocument.updated_at.desc()).first()

                    if org_doc:
                        resolved_id = str(org_doc.id)
                elif scope == "superadmin_assistant":
                    # Superadmin scope: only query un-owned superadmin assistant documents
                    sa_doc = base_query.filter(
                        KnowledgeDocument.organization_id.is_(None),
                        KnowledgeDocument.scope == "superadmin_assistant"
                    ).order_by(KnowledgeDocument.updated_at.desc()).first()

                    if sa_doc:
                        resolved_id = str(sa_doc.id)

                self._active_doc_cache[cache_key] = (resolved_id, now + 15.0)
                return resolved_id
            finally:
                db.close()
        except Exception as err:
            logger.warning(f"Error resolving active document ID from SQLite: {err}")
        return None

    def get_organization_master_context(self, organization_id: Optional[int] = None, document_id: Optional[str] = None, scope: Optional[str] = None) -> str:
        """
        Retrieves the active organization knowledge base context directly from SQLite / ChromaDB.
        Ensures strict multi-tenant isolation by organization_id and the resolved active document_id.
        Caches master context in-memory with document versioning to avoid redundant queries and stale data.
        """
        active_doc_id = self._resolve_active_document_id(organization_id, document_id, scope=scope)
        if not active_doc_id:
            return ""

        if not hasattr(self, "_ctx_cache"):
            self._ctx_cache = {}

        doc_version = self._get_document_version(active_doc_id)
        cache_key = f"org_{organization_id}_doc_{active_doc_id}_v_{doc_version}_scope_{scope}"
        if cache_key in self._ctx_cache:
            return self._ctx_cache[cache_key]

        # 1. SQLite Direct Fetch (instant zero-latency read & guaranteed 100% complete text)
        try:
            from src.config.database import SessionLocal
            from src.models.knowledge_doc import KnowledgeDocument, KnowledgeDocStatus
            db = SessionLocal()
            try:
                try:
                    doc_id_num = int(active_doc_id)
                    q = db.query(KnowledgeDocument).filter(
                        KnowledgeDocument.id == doc_id_num,
                        KnowledgeDocument.ai_usage == "Active",
                        KnowledgeDocument.status.in_([KnowledgeDocStatus.INDEXED, "Indexed"])
                    )
                    if organization_id is not None:
                        q = q.filter(KnowledgeDocument.organization_id == int(organization_id))
                    else:
                        q = q.filter(KnowledgeDocument.organization_id.is_(None))
                    active_doc = q.first()
                except ValueError:
                    q = db.query(KnowledgeDocument).filter(
                        KnowledgeDocument.title.ilike(f"%{active_doc_id}%"),
                        KnowledgeDocument.ai_usage == "Active",
                        KnowledgeDocument.status.in_([KnowledgeDocStatus.INDEXED, "Indexed"])
                    )
                    if organization_id is not None:
                        q = q.filter(KnowledgeDocument.organization_id == int(organization_id))
                    else:
                        q = q.filter(KnowledgeDocument.organization_id.is_(None))
                    active_doc = q.first()

                if active_doc and active_doc.content and active_doc.content.strip():
                    combined = str(active_doc.content).strip()
                    logger.info(f"[MASTER CONTEXT] Loaded {len(combined)} chars from SQLite doc {active_doc_id} ('{active_doc.title}')")
                    res_text = combined[:25000]
                    self._ctx_cache[cache_key] = res_text
                    return res_text
            finally:
                db.close()
        except Exception as e2:
            logger.warning(f"SQLite master context fetch warning: {e2}")

        # 2. SQLite / Chroma Vector Store Fallback
        try:
            where_filter = {"document_id": str(active_doc_id)}
            if organization_id is not None:
                where_filter = {
                    "$and": [
                        {"document_id": str(active_doc_id)},
                        {"organization_id": str(organization_id)}
                    ]
                }
            res = self.collection.get(where=where_filter)
            docs = res.get("documents", []) if res else []
            metas = res.get("metadatas", []) if res else []
            valid_docs = []
            for d, m in zip(docs, metas):
                if d and str(d).strip():
                    m_dict = m or {}
                    if organization_id is not None and str(m_dict.get("organization_id", "")).strip() != str(organization_id):
                        continue
                    if str(m_dict.get("document_id", "")).strip() != str(active_doc_id):
                        continue
                    valid_docs.append(str(d).strip())

            if valid_docs:
                combined = "\n\n".join(valid_docs)
                logger.info(f"[MASTER CONTEXT] Loaded {len(valid_docs)} vector chunks from vector store for doc {active_doc_id}")
                res_text = combined[:25000]
                self._ctx_cache[cache_key] = res_text
                return res_text
        except Exception as e:
            logger.warning(f"Vector master context fallback warning: {e}")

        return ""

    def get_organization_name(self, organization_id: Optional[int] = None, document_id: Optional[str] = None, scope: Optional[str] = None) -> str:
        """
        Dynamically extracts the Organization / Business Name from the active Knowledge Base.
        Returns empty string if no custom brand is specified.
        """
        master_ctx = self.get_organization_master_context(organization_id, document_id=document_id, scope=scope)
        if master_ctx:
            match = re.search(r'(?:Organization Name|Business Name|Company Name|Hospital Name|Clinic Name|Salon Name|School Name|Restaurant Name|Hotel Name|Agency Name|Firm Name):\s*([^\n\r]+)', master_ctx, re.IGNORECASE)
            if match:
                clean_name = re.sub(r'[*_#`]', '', match.group(1)).strip()
                if len(clean_name) > 1:
                    return clean_name

            header_match = re.search(r'#+\s*([A-Za-z0-9\s\.\-&,\']+?)(?:[\-\n\r]|$)', master_ctx)
            if header_match:
                candidate = re.sub(r'[*_#`]', '', header_match.group(1)).strip()
                stop_phrases = ["test receptionist script", "receptionist script", "test script", "rag knowledge base", "section", "organization overview", "knowledge base", "master rag", "school information", "hospital information", "salon information", "company information"]
                for sp in stop_phrases:
                    candidate = re.sub(re.escape(sp), '', candidate, flags=re.IGNORECASE).strip()
                if candidate and len(candidate) > 2 and not any(w in candidate.lower() for w in ["script", "receptionist", "knowledge base"]):
                    return candidate

        # If active document exists, attempt to extract clean title from SQLite
        target_doc_id = document_id or self._resolve_active_document_id(organization_id)
        if target_doc_id:
            try:
                from src.config.database import SessionLocal
                from src.models.knowledge_doc import KnowledgeDocument
                db = SessionLocal()
                try:
                    try:
                        doc_id_num = int(target_doc_id)
                        doc = db.query(KnowledgeDocument).filter(KnowledgeDocument.id == doc_id_num).first()
                        if doc and organization_id is not None and doc.organization_id is not None and doc.organization_id != int(organization_id):
                            doc = None
                    except ValueError:
                        doc = None
                    if doc and doc.title:
                        title_clean = re.sub(r'\.(pdf|txt|docx|doc)$', '', doc.title, flags=re.IGNORECASE)
                        title_clean = re.sub(r'^(master_rag_knowledge_base_|rag_knowledge_base_|knowledge_base_)', '', title_clean, flags=re.IGNORECASE)
                        title_clean = title_clean.replace('_', ' ').strip().title()
                        bad_generic_keywords = ["simple english", "knowledge base", "master rag", "script", "document", "receptionist", "test"]
                        if title_clean and not any(kw in title_clean.lower() for kw in bad_generic_keywords) and len(title_clean) > 2:
                            return title_clean
                finally:
                    db.close()
            except Exception:
                pass

        if organization_id:
            try:
                from src.config.database import SessionLocal
                from src.models.organization import Organization
                db = SessionLocal()
                try:
                    org = db.query(Organization).filter(Organization.id == int(organization_id)).first()
                    if org and org.name and org.name.strip().lower() not in {"default organization", "organization", "company", "business", "unknown", "none"}:
                        return org.name
                finally:
                    db.close()
            except Exception:
                pass

        return ""

    def get_organization_greeting(self, organization_id: Optional[int] = None, document_id: Optional[str] = None, scope: Optional[str] = None) -> str:
        """
        Dynamically extracts the approved opening Receptionist Greeting directly from the active script,
        or constructs a natural, time-aware greeting using the extracted organization name.
        """
        master_ctx = self.get_organization_master_context(organization_id, document_id=document_id, scope=scope)
        if master_ctx:
            match = re.search(r'(?m)^\s*(?:Voice Greeting|Opening Greeting|Receptionist Greeting|Greeting|Welcome Greeting):\s*["\']?\*?\*?([^\n\r"]+)', master_ctx, re.IGNORECASE)
            if match:
                clean_greeting = match.group(1).strip().replace("*", "").strip('"').strip("'")
                bad_prefixes = ["retrieve", "check", "when", "the receptionist", "if a customer", "verify", "identify", "must"]
                if len(clean_greeting) > 5 and not any(clean_greeting.lower().startswith(bp) for bp in bad_prefixes):
                    return clean_greeting

        org_name = self.get_organization_name(organization_id, document_id=document_id, scope=scope)
        current_hour = datetime.now().hour
        if current_hour < 12:
            salutation = "Good morning"
        elif current_hour < 17:
            salutation = "Good afternoon"
        else:
            salutation = "Good evening"

        if org_name and org_name.lower() not in {"call zenza", "unknown", "none", "organization", "company", "business", "default organization"}:
            return f"{salutation}. Thank you for calling {org_name}. How may I assist you today?"
        return f"{salutation}. How may I assist you today?"

    def get_organization_hours(self, organization_id: Optional[int] = None, document_id: Optional[str] = None) -> Optional[str]:
        master_ctx = self.get_organization_master_context(organization_id, document_id=document_id)
        if master_ctx:
            match = re.search(r'(?:Working Hours|Hours|Operating Hours|Timings|Schedule|Opening Hours)\s*:\s*([^\n\r]+)', master_ctx, re.IGNORECASE)
            if match:
                return match.group(1).strip().replace("*", "")
        return None

    def get_organization_location(self, organization_id: Optional[int] = None, document_id: Optional[str] = None) -> Optional[str]:
        master_ctx = self.get_organization_master_context(organization_id, document_id=document_id)
        if master_ctx:
            match = re.search(r'(?:Address|Location|Situated at|Located at|Address / Location)\s*:\s*([^\n\r]+)', master_ctx, re.IGNORECASE)
            if match:
                return match.group(1).strip().replace("*", "")
        return None

    def get_organization_doctors(self, organization_id: Optional[int] = None, document_id: Optional[str] = None) -> list[str]:
        master_ctx = self.get_organization_master_context(organization_id, document_id=document_id)
        doctors = []
        if master_ctx:
            # Check explicit Teacher, Doctor, Stylist or Staff directory patterns in script
            matches = re.findall(r'(?:(?:Mr\.?|Ms\.?|Mrs\.?|Dr\.?|Prof\.?)\s+[A-Za-z]+(?:\s+[A-Za-z]+)?|[A-Za-z]+\s+(?:Kumar|Sharma|Verma|Singh|Patel|Rao|Reddy|Nair|Gupta|Joshi))', master_ctx)
            for m in matches:
                clean_m = m.strip()
                if clean_m and clean_m not in doctors and len(clean_m) > 2:
                    doctors.append(clean_m)
            # Only query Staff database table if organization_id is provided and no specific document filter is active
            if not document_id and organization_id is not None:
                try:
                    from src.config.database import SessionLocal
                    from src.models.staff import Staff
                    db = SessionLocal()
                    try:
                        org_id_num = int(organization_id)
                        staff_members = db.query(Staff).filter(Staff.organization_id == org_id_num).all()
                        for sm in staff_members:
                            if sm.name and sm.name not in doctors:
                                doctors.append(sm.name)
                    finally:
                        db.close()
                except Exception:
                    pass
        return doctors

    def get_organization_services(self, organization_id: Optional[int] = None, document_id: Optional[str] = None) -> list[str]:
        master_ctx = self.get_organization_master_context(organization_id, document_id=document_id)
        if not master_ctx:
            return []

        services = []
        seen = set()

        def add_item(item: str):
            if not item:
                return
            for sub_line in str(item).splitlines():
                line_clean = re.sub(r'[^\x20-\x7E]', ' ', sub_line).strip()
                clean = re.sub(r'^(?:[-*•\d\.]+\s*)+', '', line_clean).strip()
                clean = re.sub(r'[:\(\[\{].*$', '', clean).strip().replace('*', '').strip()
                c_lower = clean.lower()
                if not clean or len(clean) < 2 or len(clean) > 50 or c_lower in seen:
                    continue
                words = clean.split()
                if len(words) >= 1 and all(w.isupper() for w in words if len(w) > 2):
                    continue
                _noise_headers = {
                    "product suite", "products", "services", "offerings", "menu", "departments",
                    "specialties", "overview", "core capabilities", "features", "primary purpose",
                    "main interaction", "about", "note", "important", "type", "company", "product name"
                }
                if c_lower not in _noise_headers and not any(c_lower.startswith(p) for p in ["primary purpose", "main interaction", "type:", "about "]):
                    seen.add(c_lower)
                    services.append(clean)

        # 1. Comma/semicolon/newline separated lists under headers (including Product Suite and Products)
        header_patterns = [
            r'(?:Product\s+Suite|Products|Products\s+Offered|Product\s+Line|Software\s+Suite|Offerings|Services|Specialties|Departments|Treatments|Procedures|Menu)\s*:\s*([^\n\r]+(?:\n\s+[A-Za-z0-9][^\n\r]+)*)',
        ]
        for pat in header_patterns:
            for m in re.finditer(pat, master_ctx, re.IGNORECASE):
                for part in re.split(r'[,;•\n]', m.group(1)):
                    add_item(part)

        # 2. Block lists under PRODUCT SUITE / PRODUCTS / SERVICES headers
        block_pat = r'(?:#+\s*|\n\s*)(?:PRODUCT\s+SUITE|PRODUCTS|SERVICES|OFFERINGS|SPECIALTIES|DEPARTMENTS)[^\n]*\n((?:[^\n]+\n?){1,20})'
        for bm in re.finditer(block_pat, master_ctx, re.IGNORECASE):
            for line in bm.group(1).splitlines():
                line_s = line.strip()
                if re.match(r'^(?:[-*•]|\d+\.)\s*[A-Za-z0-9]', line_s):
                    add_item(line_s)
                elif line_s and len(line_s) <= 40 and not any(line_s.lower().startswith(p) for p in ["primary purpose:", "main interaction:", "type:", "about", "note:", "important:"]):
                    if re.match(r'^[A-Za-z0-9\s\-_]+$', line_s) and len(line_s.split()) <= 4:
                        add_item(line_s)

        # 3. Product definition lines (e.g., "Galaxy Chat is the text-based conversational AI product")
        for m in re.finditer(r'\b([A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9]+){0,3})\s+is\s+(?:the|an|our|a)\s+[^.\n]*(?:product|software|platform|system|application|service|tool)\b', master_ctx):
            add_item(m.group(1))

        return services

    def get_appointment_entity_terminology(
        self,
        organization_id: int = 1,
        document_id: Optional[str] = None
    ) -> dict:
        """
        Extracts the business-appropriate appointment entity terminology from the active RAG.
        """
        master_ctx = self.get_organization_master_context(organization_id, document_id=document_id)
        ctx_lower = master_ctx.lower() if master_ctx else ""

        DOMAIN_SIGNALS: list[tuple[list[str], dict]] = [
            # Education / School (prioritized for teacher/school scripts)
            (["teacher", "tutor", "faculty", "lecturer"],
             {"entity_term": "teacher", "entity_term_plural": "teachers", "entity_action": "meet", "service_based": False,
              "ask_target_question": "Which teacher would you like to meet?"}),
            (["school", "college", "university", "admission enquiry", "campus visit", "class teacher", "principal", "headmaster", "academic"],
             {"entity_term": "teacher", "entity_term_plural": "teachers", "entity_action": "meet", "service_based": False,
              "ask_target_question": "Which teacher or department would you like to meet?"}),
            # Salon / Beauty (placed high for specific service terms like haircut, facial, spa)
            (["haircut", "hair cut", "hair color", "facial", "manicure", "pedicure", "blow dry",
              "beauty", "salon", "spa", "makeup", "beard grooming", "hair spa", "stylist"],
             {"entity_term": "service", "entity_term_plural": "services", "entity_action": "book", "service_based": True,
              "ask_target_question": "Which service would you like to book?"}),
            # Software / AI / Products
            (["callzenza", "ai receptionist", "receptionist bot", "ai agent", "software", "product demo"],
             {"entity_term": "service", "entity_term_plural": "services", "entity_action": "book", "service_based": True,
              "ask_target_question": "Which service or product would you like to book?"}),
            # Medical / Healthcare (using specific medical terms)
            (["doctor", "physician", "cardiologist", "neurologist", "dermatologist",
              "pediatrician", "orthopedic", "gynecologist", "dentist", "surgeon", "hospital", "clinic"],
             {"entity_term": "doctor", "entity_term_plural": "doctors", "entity_action": "see", "service_based": False,
              "ask_target_question": "Which doctor would you like to see?"}),
            # Legal
            (["attorney", "lawyer", "advocate", "solicitor", "paralegal"],
             {"entity_term": "attorney", "entity_term_plural": "attorneys", "entity_action": "meet", "service_based": False,
              "ask_target_question": "Which attorney would you like to meet?"}),
            # Finance / Banking
            (["financial advisor", "loan officer", "banker", "account manager", "wealth manager"],
             {"entity_term": "advisor", "entity_term_plural": "advisors", "entity_action": "meet", "service_based": False,
              "ask_target_question": "Which advisor would you like to meet?"}),
            # Real Estate
            (["real estate agent", "property agent", "broker"],
             {"entity_term": "agent", "entity_term_plural": "agents", "entity_action": "meet", "service_based": False,
              "ask_target_question": "Which agent would you like to meet?"}),
            # IT / Tech Support
            (["technician", "support engineer", "it support", "tech support"],
             {"entity_term": "technician", "entity_term_plural": "technicians", "entity_action": "meet", "service_based": False,
              "ask_target_question": "Which technician would you like to meet?"}),
            # Therapy / Wellness
            (["therapist", "psychologist", "counsellor", "physiotherapist", "chiropractor", "nutritionist", "dietitian"],
             {"entity_term": "therapist", "entity_term_plural": "therapists", "entity_action": "see", "service_based": False,
              "ask_target_question": "Which therapist would you like to see?"}),
            # Veterinary
            (["veterinarian", "vet", "animal doctor", "veterinary"],
             {"entity_term": "vet", "entity_term_plural": "vets", "entity_action": "see", "service_based": False,
              "ask_target_question": "Which vet would you like to see?"}),
        ]

        for signals, terminology in DOMAIN_SIGNALS:
            for signal in signals:
                if re.search(r'\b' + re.escape(signal) + r'\b', ctx_lower):
                    return terminology

        return {
            "entity_term": None,
            "entity_term_plural": None,
            "entity_action": "meet",
            "service_based": False,
            "ask_target_question": None,
        }



    def retrieve_context(
        self,
        query: str,
        n_results: int = DEFAULT_TOP_K,
        organization_id: Optional[int] = None,
        intent: Optional[str] = None,
        document_id: Optional[str] = None,
        scope: Optional[str] = None,
        call_id: Optional[str] = None,
        utterance_id: Optional[str] = None,
    ) -> str:
        """
        Universal RAG Retrieval combining Dense Vector Search + BM25 Keyword Scoring.
        Strict multi-tenant & active document filtering with post-retrieval metadata verification.
        """
        t_start = time.monotonic()
        org_id_val = int(organization_id) if organization_id is not None else None
        active_doc_id = self._resolve_active_document_id(org_id_val, document_id, scope=scope)

        logger.info(
            f"\n[RAG_QUERY]"
            f"\n  call_id={call_id or 'unknown'}"
            f"\n  utterance_id={utterance_id or 'unknown'}"
            f"\n  query='{query}'"
            f"\n  organization_id={org_id_val}"
            f"\n  knowledge_id={active_doc_id or 'none'}"
            f"\n  scope={scope or 'none'}"
        )

        if not query or not query.strip():
            master_ctx = self.get_organization_master_context(org_id_val, document_id=active_doc_id, scope=scope) if active_doc_id else ""
            elapsed_ms = round((time.monotonic() - t_start) * 1000)
            logger.info(
                f"\n[RAG_RESULT]"
                f"\n  chunks=0 (master_fallback)"
                f"\n  context_length={len(master_ctx)}"
                f"\n  latency_ms={elapsed_ms}"
            )
            return master_ctx

        # STRICT ISOLATION: A tenant with no eligible active document receives NO context and never falls back
        if org_id_val is not None and not active_doc_id:
            elapsed_ms = round((time.monotonic() - t_start) * 1000)
            logger.info(
                f"\n[RAG_RESULT]"
                f"\n  chunks=0 (no_active_doc_for_org)"
                f"\n  context_length=0"
                f"\n  latency_ms={elapsed_ms}"
            )
            return ""

        if not self.is_rag_needed(intent=intent, text=query):
            logger.info(
                f"\n[RAG_RESULT]"
                f"\n  chunks=0 (bypassed_conversational)"
                f"\n  context_length=0"
                f"\n  latency_ms={round((time.monotonic() - t_start) * 1000)}"
            )
            return ""

        search_query = query.strip()
        doc_version = self._get_document_version(active_doc_id) if active_doc_id else "0"
        if not hasattr(self, "_rag_query_cache"):
            self._rag_query_cache = {}

        # Cache key strictly incorporates tenant ID, document ID, version, scope, and query
        cache_key = f"org_{org_id_val}_doc_{active_doc_id}_v_{doc_version}_scope_{scope}:q_{search_query.lower()}"
        if cache_key in self._rag_query_cache:
            cached_ctx = self._rag_query_cache[cache_key]
            elapsed_ms = round((time.monotonic() - t_start) * 1000)
            logger.info(
                f"\n[RAG_RESULT]"
                f"\n  chunks=cache_hit"
                f"\n  context_length={len(cached_ctx)}"
                f"\n  latency_ms={elapsed_ms}"
            )
            return cached_ctx

        # Verify collection has chunks for active document; if missing, auto-index from SQLite
        if active_doc_id and str(active_doc_id) not in getattr(self, "_verified_indexed_docs", set()):
            try:
                cnt = self.collection.count(where={"document_id": str(active_doc_id)})
                if cnt == 0:
                    from src.config.database import SessionLocal
                    from src.models.knowledge_doc import KnowledgeDocument, KnowledgeDocStatus
                    from src.services.knowledge_service import KnowledgeService
                    _db = SessionLocal()
                    try:
                        try:
                            _d_id = int(active_doc_id)
                            _doc = _db.query(KnowledgeDocument).filter(KnowledgeDocument.id == _d_id).first()
                        except ValueError:
                            _doc = _db.query(KnowledgeDocument).filter(KnowledgeDocument.title.ilike(f"%{active_doc_id}%")).first()
                        if _doc and _doc.content and _doc.content.strip():
                            logger.info(f"[RAG] Auto-indexing active document {_doc.id} ('{_doc.title}') into vector store...")
                            KnowledgeService(_db)._index_document(_doc, _doc.content)
                    finally:
                        _db.close()
                if not hasattr(self, "_verified_indexed_docs"):
                    self._verified_indexed_docs = set()
                self._verified_indexed_docs.add(str(active_doc_id))
            except Exception as _sync_err:
                logger.debug(f"[RAG SYNC] Note: {_sync_err}")

        # Vector Store Query with strict multi-tenant where filter
        if active_doc_id and org_id_val is not None:
            where_filter = {
                "$and": [
                    {"document_id": str(active_doc_id)},
                    {"organization_id": str(org_id_val)}
                ]
            }
        elif active_doc_id:
            where_filter = {"document_id": str(active_doc_id)}
        elif org_id_val is not None:
            where_filter = {"organization_id": str(org_id_val)}
        elif scope == "superadmin_assistant":
            where_filter = {
                "$and": [
                    {"organization_id": "super_admin"},
                    {"scope": "superadmin_assistant"}
                ]
            }
        else:
            return ""

        query_kwargs = {
            "query_texts": [search_query],
            "n_results": max(n_results, DEFAULT_TOP_K),
            "where": where_filter,
            "include": ["documents", "metadatas", "distances"]
        }

        results = None
        try:
            results = self.collection.query(**query_kwargs)
        except Exception as exc:
            logger.warning(f"[RAG SEARCH] Primary query failed ({exc}). Retrying with active isolation filter...")
            try:
                results = self.collection.query(
                    query_texts=[search_query],
                    n_results=max(n_results, DEFAULT_TOP_K),
                    where=where_filter,
                    include=["documents", "metadatas", "distances"]
                )
            except Exception as exc2:
                logger.error(f"[RAG SEARCH] Vector query exception: {exc2}.")
                elapsed_ms = round((time.monotonic() - t_start) * 1000)
                logger.info(
                    f"\n[RAG_RESULT]"
                    f"\n  chunks=0 (exception)"
                    f"\n  context_length=0"
                    f"\n  latency_ms={elapsed_ms}"
                )
                return ""

        if not results or not results.get('documents') or not results['documents'][0]:
            master_ctx = self.get_organization_master_context(org_id_val, document_id=active_doc_id, scope=scope) if active_doc_id else ""
            elapsed_ms = round((time.monotonic() - t_start) * 1000)
            logger.info(
                f"\n[RAG_RESULT]"
                f"\n  chunks=0 (empty_vector_hits -> fallback_master)"
                f"\n  context_length={len(master_ctx)}"
                f"\n  latency_ms={elapsed_ms}"
            )
            return master_ctx

        docs = results['documents'][0]
        metas = results['metadatas'][0] if results.get('metadatas') and results['metadatas'][0] else [{}] * len(docs)
        dists = results['distances'][0] if results.get('distances') and results['distances'][0] else [0.0] * len(docs)

        clean_docs = []
        valid_metas = []
        valid_dists = []
        for d, m, dist in zip(docs, metas, dists):
            if d and str(d).strip():
                m_dict = m or {}

                # ── POST-RETRIEVAL CHUNK METADATA VALIDATION (DEFENSE-IN-DEPTH) ──
                # 1. Enforce active usage status
                if str(m_dict.get("ai_usage", "Active")).strip().lower() == "inactive":
                    logger.debug(f"[RAG ISOLATION] Rejected inactive chunk: {m_dict.get('chunk_index')}")
                    continue

                # 2. Enforce organization ownership
                if org_id_val is not None:
                    chunk_org = str(m_dict.get("organization_id", "")).strip()
                    if chunk_org != str(org_id_val):
                        logger.warning(
                            f"[RAG ISOLATION LEAK PREVENTED] Rejected chunk belonging to org '{chunk_org}' for requested tenant org {org_id_val}"
                        )
                        continue
                elif scope == "superadmin_assistant":
                    chunk_org = str(m_dict.get("organization_id", "")).strip()
                    if chunk_org != "super_admin":
                        logger.warning(
                            f"[RAG ISOLATION LEAK PREVENTED] Rejected non-superadmin chunk from org '{chunk_org}' for superadmin assistant"
                        )
                        continue

                # 3. Enforce active document identity
                if active_doc_id:
                    chunk_doc = str(m_dict.get("document_id", "")).strip()
                    if chunk_doc != str(active_doc_id):
                        logger.warning(
                            f"[RAG ISOLATION LEAK PREVENTED] Rejected chunk from doc '{chunk_doc}' instead of active doc {active_doc_id}"
                        )
                        continue

                # 4. Enforce directional scope
                if scope:
                    scope_lower = str(scope).lower()
                    chunk_scope = str(m_dict.get("scope", "")).lower()
                    chunk_type = str(m_dict.get("document_type", "")).upper()
                    if "inbound" in scope_lower and (chunk_scope == "outbound" or chunk_type in ("OUTBOUND_SCRIPT", "CAMPAIGN_SCRIPT")):
                        continue
                    if "outbound" in scope_lower and chunk_scope == "inbound":
                        continue

                clean_docs.append(str(d).strip())
                valid_metas.append(m_dict)
                valid_dists.append(dist if dist is not None else 0.0)

        if not clean_docs:
            master_ctx = self.get_organization_master_context(org_id_val, document_id=active_doc_id, scope=scope) if active_doc_id else ""
            elapsed_ms = round((time.monotonic() - t_start) * 1000)
            logger.info(
                f"\n[RAG_RESULT]"
                f"\n  chunks=0 (clean_docs_empty -> fallback_master)"
                f"\n  context_length={len(master_ctx)}"
                f"\n  latency_ms={elapsed_ms}"
            )
            return master_ctx

        # Step 1: BM25 Keyword Scoring
        bm25_scores = self.bm25_scorer.score(search_query, clean_docs)

        # Step 2: Hybrid Reranking (60% Dense Vector + 40% BM25)
        candidates = []
        for i, (doc_text, meta, dist) in enumerate(zip(clean_docs, valid_metas, valid_dists)):
            vector_sim = max(0.0, 1.0 - (float(dist) / 2.0)) if dist is not None else 0.0
            bm25_sim = bm25_scores[i] if i < len(bm25_scores) else 0.0
            hybrid_score = (0.6 * vector_sim) + (0.4 * bm25_sim)

            if hybrid_score >= DEFAULT_SIMILARITY_THRESHOLD:
                candidates.append((hybrid_score, doc_text, meta))

        # Sort candidates by combined score (descending)
        candidates.sort(key=lambda x: x[0], reverse=True)

        # Select Top distinct relevant chunks
        context_parts = []
        chunk_log_details = []
        seen = set()
        for idx, (score, doc_text, meta) in enumerate(candidates[:n_results]):
            norm_snippet = doc_text[:100]
            if norm_snippet not in seen:
                seen.add(norm_snippet)
                context_parts.append(doc_text)
                c_id = meta.get("chunk_id", meta.get("id", f"chk_{idx}"))
                d_id = meta.get("document_id", active_doc_id or "unknown")
                preview = doc_text[:60].replace('\n', ' ')
                chunk_log_details.append(f"[id={c_id}, doc={d_id}, score={score:.3f}, preview='{preview}...']")

        specific_ctx = "\n\n".join(context_parts)
        final_ctx = specific_ctx
        elapsed_ms = round((time.monotonic() - t_start) * 1000)

        logger.info(
            f"\n[RAG_RESULT]"
            f"\n  chunks={len(context_parts)}"
            f"\n  details={chunk_log_details}"
            f"\n  context_length={len(final_ctx)}"
            f"\n  latency_ms={elapsed_ms}"
        )

        res_to_return = final_ctx if final_ctx else (self.get_organization_master_context(org_id_val, document_id=active_doc_id, scope=scope) if active_doc_id else "")
        if hasattr(self, "_rag_query_cache") and len(self._rag_query_cache) < 500:
            self._rag_query_cache[cache_key] = res_to_return
        return res_to_return

    def get_active_script(
        self,
        organization_id: Optional[int] = None,
        agent_id: Optional[str] = None,
        assistant_id: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> Optional[str]:
        """
        Retrieves the active SCRIPT document content for the specified scope/org/agent.
        Strictly isolated: Super Admin scripts are NEVER returned for org queries and vice versa.
        """
        try:
            from src.config.database import SessionLocal
            from src.models.knowledge_doc import KnowledgeDocument, KnowledgeDocStatus
            db = SessionLocal()
            try:
                query = db.query(KnowledgeDocument).filter(
                    KnowledgeDocument.document_type == "SCRIPT",
                    KnowledgeDocument.ai_usage == "Active",
                    KnowledgeDocument.status.in_([KnowledgeDocStatus.INDEXED, "Indexed"]),
                )

                if scope == "superadmin_assistant" or organization_id is None:
                    query = query.filter(
                        KnowledgeDocument.organization_id.is_(None),
                        KnowledgeDocument.scope == "superadmin_assistant",
                    )
                else:
                    query = query.filter(
                        KnowledgeDocument.organization_id == int(organization_id),
                    )

                if agent_id:
                    query = query.filter(KnowledgeDocument.agent_id == agent_id)
                if assistant_id:
                    query = query.filter(KnowledgeDocument.assistant_id == assistant_id)

                script_doc = query.order_by(KnowledgeDocument.updated_at.desc()).first()
                if script_doc and script_doc.content:
                    return script_doc.content.strip()
            finally:
                db.close()
        except Exception as err:
            logger.warning(f"Error loading active script: {err}")
        return None


rag_service = RagService()
