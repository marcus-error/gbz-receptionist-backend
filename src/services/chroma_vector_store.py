"""
Dedicated ChromaDB Vector Store & RAG Knowledge Repository (Option B).

Features:
- Stores RAG chunks, 384-dimensional dense embeddings, and vector indexes in ChromaDB persistent database.
- Keeps relational application records and metadata in the primary database (receptionist.db).
- Full multi-tenant isolation by organization_id, scope, document_type, agent_id.
- Native ChromaDB persistent client storage at `./chroma_db`.
- Drop-in interface compatible with RAG retrieval workflows.
"""

import os
import re
import json
import math
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

try:
    import chromadb
    from chromadb.config import Settings
except ImportError:
    chromadb = None

try:
    from src.config.env import settings
    custom_path = getattr(settings, "CHROMA_PERSIST_DIRECTORY", "./chroma_db")
    if not os.path.isabs(custom_path):
        CHROMA_DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", custom_path))
    else:
        CHROMA_DB_PATH = custom_path
except Exception:
    CHROMA_DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "chroma_db"))


class EmbeddingEngine:
    """
    384-dimensional dense embedding generator.
    Uses standard all-MiniLM-L6-v2 embedding model with in-memory caching.
    """
    def __init__(self):
        self._model = None
        self._cache: Dict[str, List[float]] = {}

    def _get_model(self):
        if self._model is None:
            try:
                from chromadb.utils import embedding_functions
                self._model = embedding_functions.DefaultEmbeddingFunction()
            except Exception as e:
                try:
                    from sentence_transformers import SentenceTransformer
                    self._model = SentenceTransformer("all-MiniLM-L6-v2")
                except Exception as e2:
                    logger.warning(f"[EMBEDDING ENGINE] Embedding loader fallback: {e} / {e2}")
                    self._model = None
        return self._model

    def generate_embeddings(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []

        # Check in-memory query embedding cache
        results: List[Optional[List[float]]] = [None] * len(texts)
        missing_indices: List[int] = []
        missing_texts: List[str] = []

        for i, text in enumerate(texts):
            clean_k = text.strip().lower()
            if clean_k in self._cache:
                results[i] = self._cache[clean_k]
            else:
                missing_indices.append(i)
                missing_texts.append(text)

        if not missing_texts:
            return [r for r in results if r is not None]

        computed_embs: List[List[float]] = []
        model = self._get_model()
        if model:
            try:
                embs = model(missing_texts)
                computed_embs = [[float(x) for x in emb] for emb in embs]
            except Exception as err:
                logger.warning(f"[EMBEDDING ENGINE] Embedding calculation error: {err}")

        # Deterministic semantic hash fallback if model unavailable
        if not computed_embs or len(computed_embs) != len(missing_texts):
            computed_embs = []
            for text in missing_texts:
                words = re.findall(r'\w+', text.lower())
                vec = [0.0] * 384
                for i, w in enumerate(words):
                    h = hash(w)
                    idx = abs(h) % 384
                    vec[idx] += 1.0 / (1.0 + (i * 0.05))
                norm = math.sqrt(sum(x * x for x in vec)) or 1.0
                computed_embs.append([x / norm for x in vec])

        # Cache results and assemble return array
        for idx, emb, text in zip(missing_indices, computed_embs, missing_texts):
            clean_k = text.strip().lower()
            if len(self._cache) < 1000:
                self._cache[clean_k] = emb
            results[idx] = emb

        return [r for r in results if r is not None]


def _clean_where(where_dict: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not where_dict:
        return None
    cleaned = {}
    for k, v in where_dict.items():
        if k in ("$and", "$or") and isinstance(v, list):
            cleaned[k] = [_clean_where(item) for item in v if isinstance(item, dict)]
        elif isinstance(v, (int, float, str)):
            cleaned[k] = str(v)
        elif isinstance(v, bool):
            cleaned[k] = v
        elif isinstance(v, dict):
            cleaned[k] = {sub_k: (str(sub_v) if not isinstance(sub_v, bool) else sub_v) for sub_k, sub_v in v.items()}
        else:
            cleaned[k] = str(v)
    return cleaned


class ChromaVectorStore:
    """
    Production ChromaDB-backed Vector Store for RAG chunks and vector embeddings.
    Option B: Dedicated Vector DB for embeddings/chunks, Relational DB for application metadata.
    """
    def __init__(self, db_path: Optional[str] = None, collection_name: str = "knowledge_chunks"):
        self.db_path = db_path or CHROMA_DB_PATH
        self.collection_name = collection_name
        self.embedding_engine = EmbeddingEngine()
        self.client = None
        self.collection = None
        self._init_chroma()

    def _init_chroma(self):
        os.makedirs(self.db_path, exist_ok=True)
        if chromadb:
            try:
                self.client = chromadb.PersistentClient(path=self.db_path)
                self.collection = self.client.get_or_create_collection(name=self.collection_name)
                logger.info(f"[CHROMA VECTOR STORE] Initialized at: {self.db_path} (Collection: {self.collection_name}, Count: {self.collection.count()})")
            except Exception as err:
                logger.error(f"[CHROMA VECTOR STORE] Failed to initialize ChromaDB PersistentClient: {err}")
                self.client = None
                self.collection = None
        else:
            logger.error("[CHROMA VECTOR STORE] chromadb library is not installed.")

    def _ensure_collection(self):
        """Ensures ChromaDB persistent client and collection exist and are active."""
        if not chromadb:
            return None
        if not self.client:
            try:
                self.client = chromadb.PersistentClient(path=self.db_path)
            except Exception as err:
                logger.error(f"[CHROMA VECTOR STORE] Failed to re-init PersistentClient: {err}")
                return None
        if self.client:
            try:
                if self.collection is None:
                    self.collection = self.client.get_or_create_collection(name=self.collection_name)
                else:
                    _ = self.collection.count()
            except Exception as col_err:
                logger.warning(f"[CHROMA VECTOR STORE] Collection error ({col_err}). Re-creating collection '{self.collection_name}'...")
                try:
                    self.collection = self.client.get_or_create_collection(name=self.collection_name)
                except Exception as col_err2:
                    logger.error(f"[CHROMA VECTOR STORE] Failed to recover collection '{self.collection_name}': {col_err2}")
                    self.collection = None
        return self.collection

    def upsert(
        self,
        ids: List[str],
        documents: List[str],
        metadatas: List[Dict[str, Any]],
        embeddings: Optional[List[List[float]]] = None,
    ) -> int:
        if not self._ensure_collection() or not ids or not documents:
            return 0

        if embeddings is None or len(embeddings) != len(documents):
            embeddings = self.embedding_engine.generate_embeddings(documents)

        # Sanitize metadata for ChromaDB (ensure string values for metadata query filtering)
        clean_metadatas = []
        for meta in metadatas:
            m = meta or {}
            raw_org = m.get("organization_id")
            org_str = str(raw_org) if raw_org is not None and str(raw_org).strip() else "super_admin"
            clean_m = {
                "document_id": str(m.get("document_id", "")),
                "organization_id": org_str,
                "scope": str(m.get("scope", "organization")),
                "document_type": str(m.get("document_type", "RAG")),
                "visibility": str(m.get("visibility", "organization")),
                "agent_id": str(m.get("agent_id", "") or ""),
                "assistant_id": str(m.get("assistant_id", "") or ""),
                "section": str(m.get("section", "general")),
                "chunk_index": int(m.get("chunk_index", 0)),
                "total_chunks": int(m.get("total_chunks", 1)),
                "ai_usage": str(m.get("ai_usage", "Active")),
                "source": str(m.get("source", "")),
            }
            clean_metadatas.append(clean_m)

        batch_size = 100
        total_upserted = 0
        try:
            for i in range(0, len(ids), batch_size):
                b_ids = ids[i:i + batch_size]
                b_docs = documents[i:i + batch_size]
                b_metas = clean_metadatas[i:i + batch_size]
                b_embs = embeddings[i:i + batch_size] if embeddings else None
                self.collection.upsert(
                    ids=b_ids,
                    documents=b_docs,
                    metadatas=b_metas,
                    embeddings=b_embs,
                )
                total_upserted += len(b_ids)
            logger.info(f"[CHROMA VECTOR STORE] Upserted {total_upserted} vector chunks into ChromaDB.")
            return total_upserted
        except Exception as err:
            logger.error(f"[CHROMA VECTOR STORE] Upsert error: {err}")
            return total_upserted


    def delete(self, where: Optional[Dict[str, Any]] = None, ids: Optional[List[str]] = None) -> int:
        if not self._ensure_collection():
            return 0

        try:
            if ids:
                self.collection.delete(ids=ids)
                logger.info(f"[CHROMA VECTOR STORE] Deleted {len(ids)} chunks by ID.")
                return len(ids)
            elif where:
                clean_where = _clean_where(where)
                self.collection.delete(where=clean_where)
                logger.info(f"[CHROMA VECTOR STORE] Deleted chunks matching filter {clean_where}.")
                return 1
            return 0
        except Exception as err:
            logger.warning(f"[CHROMA VECTOR STORE] Delete warning: {err}")
            return 0

    def get(
        self,
        where: Optional[Dict[str, Any]] = None,
        ids: Optional[List[str]] = None,
        include: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        if not self._ensure_collection():
            return {"ids": [], "documents": [], "metadatas": []}

        try:
            kwargs = {}
            if ids:
                kwargs["ids"] = ids
            if where:
                kwargs["where"] = _clean_where(where)
            if include:
                kwargs["include"] = include
            else:
                kwargs["include"] = ["documents", "metadatas"]

            res = self.collection.get(**kwargs)
            return {
                "ids": res.get("ids", []),
                "documents": res.get("documents", []),
                "metadatas": res.get("metadatas", []),
                "embeddings": res.get("embeddings", None),
            }
        except Exception as err:
            logger.warning(f"[CHROMA VECTOR STORE] Get error: {err}")
            return {"ids": [], "documents": [], "metadatas": []}

    def query(
        self,
        query_texts: List[str],
        n_results: int = 5,
        where: Optional[Dict[str, Any]] = None,
        query_embeddings: Optional[List[List[float]]] = None,
        include: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        if not self._ensure_collection() or not (query_texts or query_embeddings):
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}

        if query_embeddings is None:
            query_embeddings = self.embedding_engine.generate_embeddings(query_texts)

        try:
            query_kwargs = {
                "query_embeddings": query_embeddings,
                "n_results": n_results,
                "include": include or ["documents", "metadatas", "distances"]
            }
            if where:
                query_kwargs["where"] = _clean_where(where)

            res = self.collection.query(**query_kwargs)
            return {
                "ids": res.get("ids", [[]]),
                "documents": res.get("documents", [[]]),
                "metadatas": res.get("metadatas", [[]]),
                "distances": res.get("distances", [[]]),
            }
        except Exception as err:
            logger.warning(f"[CHROMA VECTOR STORE] Query error: {err}")
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}

    def count(self, where: Optional[Dict[str, Any]] = None) -> int:
        if not self._ensure_collection():
            return 0
        try:
            if where:
                res = self.get(where=where)
                return len(res.get("ids", []))
            return self.collection.count()
        except Exception:
            return 0

    def update(
        self,
        ids: List[str],
        documents: Optional[List[str]] = None,
        metadatas: Optional[List[Dict[str, Any]]] = None,
    ) -> int:
        if not self._ensure_collection() or not ids:
            return 0
        try:
            kwargs = {"ids": ids}
            if documents:
                kwargs["documents"] = documents
            if metadatas:
                clean_metas = [{k: str(v) for k, v in m.items()} for m in metadatas]
                kwargs["metadatas"] = clean_metas
            self.collection.update(**kwargs)
            return len(ids)
        except Exception as err:
            logger.warning(f"[CHROMA VECTOR STORE] Update warning: {err}")
            return 0

    def reset(self):
        if self._ensure_collection():
            try:
                self.client.delete_collection(name=self.collection_name)
            except Exception:
                pass
            try:
                self.collection = self.client.get_or_create_collection(name=self.collection_name)
            except Exception:
                self.collection = None
            logger.info("[CHROMA VECTOR STORE] Reset and cleared all ChromaDB vectors.")


chroma_vector_store = ChromaVectorStore()

