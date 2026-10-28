"""
Global Pytest Configuration and Test Environment Isolation for AI Receptionist.

Guarantees:
1. Complete isolation from production databases (receptionist.db, rag.sqlite).
2. Complete isolation from production vector stores (./chroma_db).
3. Zero test fixtures or dummy data leak into production.
4. Temporary sandbox created per test session and purged on teardown.
"""

import os
import sys
import shutil
import tempfile
import uuid
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

# Ensure backend root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import src.config.database as db_mod
import src.services.chroma_vector_store as chroma_mod
import src.services.rag_service as rag_mod
from src.services.cache_service import cache_service


# Set up isolated temporary test directory
TEMP_TEST_DIR = tempfile.mkdtemp(prefix="ai_receptionist_test_sandbox_")
TEST_DB_PATH = os.path.join(TEMP_TEST_DIR, "test_receptionist.db")
TEST_CHROMA_PATH = os.path.join(TEMP_TEST_DIR, "test_chroma_db")
os.makedirs(TEST_CHROMA_PATH, exist_ok=True)

# 1. Create Isolated Test Database Engine & Session
TEST_DB_URL = f"sqlite:///{TEST_DB_PATH}"
test_connect_args = {"check_same_thread": False, "timeout": 30}
test_engine = create_engine(TEST_DB_URL, connect_args=test_connect_args, echo=False)

@event.listens_for(test_engine, "connect")
def _set_test_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()

TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

# Rebind database module immediately upon conftest import
orig_engine = db_mod.engine
orig_SessionLocal = db_mod.SessionLocal
db_mod.engine = test_engine
db_mod.SessionLocal = TestSessionLocal

# Import models so they are registered on Base.metadata before create_all
from src.models import (
    appointment, call_log, knowledge_doc, staff, user,
    organization, permission, subscription, department,
    twilio_config, sip_config, email_config, whatsapp_config,
    calendar_config, gemini_account, workflow, lead, webhook
)
from src.models import settings as settings_model

# Create tables in test database
db_mod.Base.metadata.create_all(bind=test_engine)

# 2. Rebind Chroma Vector Store to Isolated Temporary Directory
orig_chroma_store = chroma_mod.chroma_vector_store
test_chroma_store = chroma_mod.ChromaVectorStore(db_path=TEST_CHROMA_PATH, collection_name="test_knowledge_chunks")
chroma_mod.chroma_vector_store = test_chroma_store
rag_mod.rag_service.collection = test_chroma_store

try:
    from src.services.sqlite_vector_store import sqlite_vector_store
    sqlite_vector_store._store = test_chroma_store
except Exception:
    pass


@pytest.fixture(scope="session", autouse=True)
def isolate_test_environment():
    """Session-scoped fixture ensuring all tests run strictly against the test sandbox."""
    # Seed baseline minimal organizations for tests (with zero dummy doctors or test docs)
    with TestSessionLocal() as session:
        from src.models.organization import Organization
        from src.models.subscription import Subscription
        from src.models.settings import AppSettings

        if not session.query(Organization).filter(Organization.id == 1).first():
            session.add(Organization(id=1, name="Synthetic Test Enterprise", organization_type="General", status="active"))
        if not session.query(Organization).filter(Organization.id == 2).first():
            session.add(Organization(id=2, name="Synthetic Second Tenant", organization_type="General", status="active"))
        if not session.query(AppSettings).first():
            session.add(AppSettings())
        session.commit()

    rag_mod.rag_service.clear_cache()
    cache_service.clear_all()

    yield {
        "temp_dir": TEMP_TEST_DIR,
        "db_path": TEST_DB_PATH,
        "chroma_path": TEST_CHROMA_PATH,
        "engine": test_engine,
        "SessionLocal": TestSessionLocal,
        "chroma_store": test_chroma_store,
    }

    # Teardown: Revert modules and purge sandbox
    db_mod.engine = orig_engine
    db_mod.SessionLocal = orig_SessionLocal
    chroma_mod.chroma_vector_store = orig_chroma_store
    rag_mod.rag_service.collection = orig_chroma_store
    try:
        from src.services.sqlite_vector_store import sqlite_vector_store
        sqlite_vector_store._store = orig_chroma_store
    except Exception:
        pass

    rag_mod.rag_service.clear_cache()
    cache_service.clear_all()

    try:
        shutil.rmtree(TEMP_TEST_DIR, ignore_errors=True)
    except Exception:
        pass


@pytest.fixture
def db_session():
    """Provides a fresh isolated database session for a test function."""
    session = TestSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def dynamic_doc_factory(db_session):
    """
    Factory fixture to dynamically create, index, and tear down synthetic test documents.
    Prevents any hardcoded hospital or business names from entering test knowledge.
    """
    created_docs = []

    def _create(
        organization_id: int = 1,
        title: str = None,
        content: str = None,
        ai_usage: str = "Active",
        document_type: str = "RAG"
    ):
        from src.services.knowledge_service import KnowledgeService
        from src.validators.knowledge_validator import KnowledgeDocCreate

        uid = uuid.uuid4().hex[:6]
        doc_title = title or f"synthetic_test_doc_{uid}.txt"
        doc_content = content or (
            f"Organization Name: Synthetic Corp {uid}\n"
            f"Services: Consulting {uid}, Analytics {uid}\n"
            f"Operating Hours: 09:00 AM - 05:00 PM\n"
        )

        payload = KnowledgeDocCreate(
            title=doc_title,
            document_type=document_type,
            ai_usage=ai_usage,
            content=doc_content,
            organization_id=organization_id,
            scope="organization",
            visibility="organization"
        )
        ks = KnowledgeService(db_session)
        doc = ks.create_doc(payload=payload, organization_id=organization_id)
        created_docs.append(doc.id)
        return doc

    yield _create

    # Teardown created docs
    from src.services.knowledge_service import KnowledgeService
    ks = KnowledgeService(db_session)
    for doc_id in created_docs:
        try:
            ks.delete_doc(doc_id, organization_id=None, super_admin_view=True)
        except Exception:
            pass
