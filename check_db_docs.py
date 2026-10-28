import sys
import os

sys.path.insert(0, os.path.abspath("."))
from src.config.database import SessionLocal
from src.models.knowledge_doc import KnowledgeDocument
from src.models.organization import Organization
from src.models.sip_config import SipConfig

db = SessionLocal()
orgs = db.query(Organization).all()
print(f"Total Organizations: {len(orgs)}")
for o in orgs:
    print(f"  Org ID: {o.id}, Name: {o.name}")

sips = db.query(SipConfig).all()
print(f"\nTotal SIP Configs: {len(sips)}")
for s in sips:
    print(f"  SIP ID: {s.id}, Org ID: {s.organization_id}, User: {s.sip_username}, Trunk: {s.did_number}")

docs = db.query(KnowledgeDocument).all()
print(f"\nTotal Knowledge Documents: {len(docs)}")
for d in docs:
    print(f"  Doc ID: {d.id}, Title: {d.title}, Org: {d.organization_id}, Type: {d.document_type}, AI Usage: {d.ai_usage}, Status: {d.status}, Scope: {d.scope}, Len: {len(d.content or '')}")
    print(f"  Preview: {(d.content or '')[:160]}...\n")

db.close()
