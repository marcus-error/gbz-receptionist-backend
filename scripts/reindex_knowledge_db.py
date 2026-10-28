"""
CLI Script to Re-Index All Knowledge Base Documents with Structure-Aware Semantic Chunking.

Usage:
python -m scripts.reindex_knowledge_db
"""

import sys
import os

# Ensure backend root is on PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config.database import SessionLocal
from src.services.knowledge_service import KnowledgeService


def main():
    print("Starting Knowledge Base Re-Indexing with Structure-Aware Semantic Chunking...")
    db = SessionLocal()
    try:
        svc = KnowledgeService(db)
        count = svc.reindex_all_docs()
        print(f"Successfully re-indexed {count} knowledge base documents in ChromaDB.")
    except Exception as e:
        print(f"Failed to re-index knowledge base: {e}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
