"""
Script to clean up stale/orphaned vectors from ChromaDB that are not present in the SQL database.
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config.database import SessionLocal
from src.models.knowledge_doc import KnowledgeDocument, KnowledgeDocStatus
from src.services.rag_service import rag_service


def cleanup_chromadb():
    db = SessionLocal()
    try:
        active_docs = db.query(KnowledgeDocument).filter(
            KnowledgeDocument.status == KnowledgeDocStatus.INDEXED
        ).all()
        valid_doc_ids = {str(d.id) for d in active_docs}

        all_data = rag_service.collection.get()
        ids = all_data.get("ids", [])
        metadatas = all_data.get("metadatas", [])

        to_delete = []
        for v_id, meta in zip(ids, metadatas):
            doc_id = str(meta.get("document_id", "")) if meta else ""
            if not doc_id or doc_id not in valid_doc_ids:
                to_delete.append(v_id)

        if to_delete:
            print(f"Deleting {len(to_delete)} orphaned vectors from ChromaDB: {to_delete}")
            rag_service.collection.delete(ids=to_delete)
            print("Successfully deleted orphaned vectors.")
        else:
            print("No orphaned vectors found in ChromaDB.")

        # Re-check remaining count
        remaining = rag_service.collection.count()
        print(f"Remaining active vectors in ChromaDB: {remaining}")
    finally:
        db.close()


if __name__ == "__main__":
    cleanup_chromadb()
