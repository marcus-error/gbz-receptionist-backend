import os
import sys

# Ensure backend root in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.config.database import SessionLocal
from src.models.knowledge_doc import KnowledgeDocument
from src.services.rag_service import rag_service
from src.services.ai_service import ai_service
from src.services.cache_service import cache_service

def run():
    print("=== TESTING ACTIVE RAG SYSTEM ===")
    active_id = rag_service._resolve_active_document_id(1)
    print(f"Active Doc ID: {active_id}")

    db = SessionLocal()
    try:
        active_doc = db.query(KnowledgeDocument).get(int(active_id)) if active_id else None
        print(f"Active Doc Title: {active_doc.title if active_doc else 'None'}")
        print(f"Active Doc Usage: {active_doc.ai_usage if active_doc else 'None'}")
    finally:
        db.close()

    org_name = rag_service.get_organization_name(1)
    print(f"Extracted Org Name: {org_name}")

    greeting = rag_service.get_organization_greeting(1)
    print(f"Extracted Greeting: {greeting}")

    print("\n--- Testing Retrieval ---")
    queries = [
        "hello",
        "who are all the staff available",
        "what services do you offer",
        "what is the price for haircut",
        "who are the doctors",
    ]

    for q in queries:
        print(f"\n[QUERY]: {q}")
        ctx = rag_service.retrieve_context(q, n_results=3, organization_id=1)
        print(f"[RAG CONTEXT (First 200 chars)]:\n{ctx[:200]}...")
        resp = ai_service.generate_response(q, context=ctx, organization_id=1)
        print(f"[AI RESPONSE]: {resp}")

if __name__ == "__main__":
    run()
