import enum
from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.sql import func

from src.config.database import Base


class KnowledgeDocStatus(str, enum.Enum):
    INDEXED = "Indexed"
    PROCESSING = "Processing"
    FAILED = "Failed"


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)
    receptionist_id = Column(String(100), nullable=True, default="default")
    agent_id = Column(String(100), nullable=True, index=True)
    assistant_id = Column(String(100), nullable=True, index=True)
    uploaded_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    title = Column(String(255), nullable=False)
    document_type = Column(String(50), nullable=False, default="RAG")  # "RAG", "SCRIPT", "POLICY"
    source_type = Column(String(50), nullable=False, default="file")    # "file", "text", "url"
    visibility = Column(String(50), nullable=False, default="organization")  # "superadmin_assistant", "organization", "agent_private", "global"
    scope = Column(String(50), nullable=False, default="organization")       # "superadmin_assistant", "organization", "agent"

    file_type = Column(String(50), nullable=False, default="PDF")
    file_path = Column(String(500), nullable=True)
    content = Column(Text().with_variant(LONGTEXT, "mysql"), nullable=True)
    chunks = Column(Integer, default=0)
    ai_usage = Column(String(50), default="Active")
    status = Column(Enum(KnowledgeDocStatus), default=KnowledgeDocStatus.INDEXED)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
