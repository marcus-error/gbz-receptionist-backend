from typing import List, Optional
from sqlalchemy.orm import Session

from src.models.knowledge_doc import KnowledgeDocument


class KnowledgeRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_all(
        self,
        organization_id: Optional[int] = None,
        scope: Optional[str] = None,
        document_type: Optional[str] = None,
        agent_id: Optional[str] = None,
        super_admin_view: bool = False,
    ) -> List[KnowledgeDocument]:
        query = self.db.query(KnowledgeDocument)

        if super_admin_view:
            # Super Admin explicitly viewing — apply optional filters
            if organization_id is not None:
                query = query.filter(KnowledgeDocument.organization_id == organization_id)
            if scope:
                query = query.filter(KnowledgeDocument.scope == scope)
            # If no org_id AND no scope, return all (for reindex_all_docs only)
        else:
            # STRICT: Non-super-admin MUST have org_id
            if organization_id is not None:
                query = query.filter(KnowledgeDocument.organization_id == organization_id)
            else:
                # No org_id and not super_admin_view = return nothing (safety)
                query = query.filter(KnowledgeDocument.organization_id.is_(None))
                query = query.filter(KnowledgeDocument.scope == "superadmin_assistant")

        if document_type:
            query = query.filter(KnowledgeDocument.document_type == document_type)
        if agent_id:
            query = query.filter(KnowledgeDocument.agent_id == agent_id)

        return query.order_by(KnowledgeDocument.id.desc()).all()

    def get_by_id(
        self,
        doc_id: int,
        organization_id: Optional[int] = None,
        super_admin_view: bool = False,
    ) -> Optional[KnowledgeDocument]:
        query = self.db.query(KnowledgeDocument).filter(KnowledgeDocument.id == doc_id)
        if not super_admin_view:
            if organization_id is not None:
                query = query.filter(KnowledgeDocument.organization_id == organization_id)
            else:
                query = query.filter(KnowledgeDocument.scope == "superadmin_assistant")
        return query.first()

    def create(self, data: dict) -> KnowledgeDocument:
        doc = KnowledgeDocument(**data)
        self.db.add(doc)
        self.db.commit()
        self.db.refresh(doc)
        return doc

    def update(self, doc: KnowledgeDocument, data: dict) -> KnowledgeDocument:
        for key, value in data.items():
            if value is not None:
                setattr(doc, key, value)
        self.db.commit()
        self.db.refresh(doc)
        return doc

    def delete(self, doc: KnowledgeDocument) -> None:
        self.db.delete(doc)
        self.db.commit()
