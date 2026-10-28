from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field

from src.models.knowledge_doc import KnowledgeDocStatus


class KnowledgeDocCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    file_type: Optional[str] = "PDF"
    file_path: Optional[str] = None
    content: Optional[str] = None
    chunks: Optional[int] = 0
    ai_usage: Optional[str] = "Active"
    status: Optional[KnowledgeDocStatus] = KnowledgeDocStatus.INDEXED
    organization_id: Optional[int] = None
    agent_id: Optional[str] = None
    assistant_id: Optional[str] = None
    uploaded_by: Optional[int] = None
    document_type: Optional[str] = "RAG"
    source_type: Optional[str] = "file"
    visibility: Optional[str] = "organization"
    scope: Optional[str] = "organization"


class KnowledgeDocUpdate(BaseModel):
    title: Optional[str] = None
    file_type: Optional[str] = None
    file_path: Optional[str] = None
    content: Optional[str] = None
    chunks: Optional[int] = None
    ai_usage: Optional[str] = None
    status: Optional[KnowledgeDocStatus] = None
    agent_id: Optional[str] = None
    assistant_id: Optional[str] = None
    document_type: Optional[str] = None
    source_type: Optional[str] = None
    visibility: Optional[str] = None
    scope: Optional[str] = None


class KnowledgeDocOut(BaseModel):
    id: int
    title: str
    file_type: str
    file_path: Optional[str] = None
    content: Optional[str] = None
    chunks: int
    ai_usage: str
    status: KnowledgeDocStatus
    organization_id: Optional[int] = None
    agent_id: Optional[str] = None
    assistant_id: Optional[str] = None
    uploaded_by: Optional[int] = None
    document_type: Optional[str] = "RAG"
    source_type: Optional[str] = "file"
    visibility: Optional[str] = "organization"
    scope: Optional[str] = "organization"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)
