"""
SQLite Vector Store & RAG Knowledge Repository.

Replaces ChromaDB with an ultra-lightweight, zero-external-service SQLite vector store.
Features:
- Pure SQLite storage in `rag.sqlite`
- Exact 384-dimensional vector embedding storage & cosine similarity search
- Full multi-tenant isolation by organization_id, scope, document_type, agent_id
- Complete metadata preservation & filtering (ai_usage, document_id, section, etc.)
- Drop-in interface compatible with RAG retrieval workflows
"""

import os
import re
import json
import math
import struct
import sqlite3
import logging
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from src.config.env import settings
    custom_path = getattr(settings, "RAG_DATABASE_PATH", "./rag.sqlite")
    if not os.path.isabs(custom_path):
        RAG_DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", custom_path))
    else:
        RAG_DB_PATH = custom_path
except Exception:
    RAG_DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "rag.sqlite"))


class EmbeddingEngine:
    """
    Lightweight 384-dimensional dense embedding generator.
    Uses standard all-MiniLM-L6-v2 embedding model.
    """
    def __init__(self):
        self._model = None

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
        model = self._get_model()
        if model:
            try:
                embs = model(texts)
                # Ensure float lists
                return [[float(x) for x in emb] for emb in embs]
            except Exception as err:
                logger.warning(f"[EMBEDDING ENGINE] Embedding calculation error: {err}")

        # Deterministic semantic hash fallback if embedding model is unavailable
        results = []
        for text in texts:
            words = re.findall(r'\w+', text.lower())
            vec = [0.0] * 384
            for i, w in enumerate(words):
                h = hash(w)
                idx = abs(h) % 384
                vec[idx] += 1.0 / (1.0 + (i * 0.05))
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            results.append([x / norm for x in vec])
        return results


def cosine_similarity(vec_a: Any, vec_b: Any) -> float:
    """Compute cosine similarity between two float vectors."""
    if vec_a is None or vec_b is None:
        return 0.0
    if len(vec_a) != len(vec_b) or len(vec_a) == 0:
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for a, b in zip(vec_a, vec_b):
        dot += a * b
        norm_a += a * a
        norm_b += b * b
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


class SQLiteVectorStore:
    """
    Vector Store Wrapper using ChromaDB Persistent Client (Option B).
    Delegates all vector store operations to ChromaVectorStore.
    """
    def __init__(self, db_path: Optional[str] = None):
        from src.services.chroma_vector_store import chroma_vector_store
        self._store = chroma_vector_store
        self.embedding_engine = chroma_vector_store.embedding_engine

    def upsert(self, ids: List[str], documents: List[str], metadatas: List[Dict[str, Any]], embeddings: Optional[List[List[float]]] = None) -> int:
        return self._store.upsert(ids, documents, metadatas, embeddings)

    def delete(self, where: Optional[Dict[str, Any]] = None, ids: Optional[List[str]] = None) -> int:
        return self._store.delete(where=where, ids=ids)

    def get(self, where: Optional[Dict[str, Any]] = None, ids: Optional[List[str]] = None, include: Optional[List[str]] = None) -> Dict[str, Any]:
        return self._store.get(where=where, ids=ids, include=include)

    def query(self, query_texts: List[str], n_results: int = 5, where: Optional[Dict[str, Any]] = None, query_embeddings: Optional[List[List[float]]] = None, include: Optional[List[str]] = None, **kwargs: Any) -> Dict[str, Any]:
        return self._store.query(query_texts=query_texts, n_results=n_results, where=where, query_embeddings=query_embeddings, include=include, **kwargs)

    def count(self, where: Optional[Dict[str, Any]] = None) -> int:
        return self._store.count(where=where)

    def update(self, ids: List[str], documents: Optional[List[str]] = None, metadatas: Optional[List[Dict[str, Any]]] = None) -> int:
        return self._store.update(ids=ids, documents=documents, metadatas=metadatas)

    def reset(self):
        return self._store.reset()


sqlite_vector_store = SQLiteVectorStore()

