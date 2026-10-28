from typing import List, Optional
from fastapi import Depends, File, Form, UploadFile, HTTPException, Query
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.services.knowledge_service import KnowledgeService
from src.validators.knowledge_validator import KnowledgeDocCreate, KnowledgeDocOut, KnowledgeDocUpdate
from fastapi.responses import FileResponse


from src.middleware.auth_middleware import get_current_user
from src.models.user import User


def _get_org_id(user: Optional[User]) -> Optional[int]:
    """Returns the effective organization_id for the current user.
    Super Admin with no org => None (superadmin_assistant scope).
    Regular users => their organization_id.
    """
    if not user:
        return None
    if user.role == "super_admin" and user.organization_id is None:
        return None
    return user.organization_id


def _is_super_admin(user: Optional[User]) -> bool:
    return user is not None and user.role == "super_admin"


def list_docs(
    scope: Optional[str] = Query(None),
    document_type: Optional[str] = Query(None),
    target_org_id: Optional[int] = Query(None, alias="target_org_id"),
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[KnowledgeDocOut]:
    is_sa = _is_super_admin(current_user)
    if is_sa and target_org_id is not None:
        # Super Admin viewing a specific org's documents
        return KnowledgeService(db).list_docs(
            organization_id=target_org_id,
            scope=scope,
            document_type=document_type,
            super_admin_view=True,
        )
    
    org_id = _get_org_id(current_user)
    if is_sa and target_org_id is None:
        if scope == "superadmin_assistant":
            return KnowledgeService(db).list_docs(
                organization_id=None,
                scope="superadmin_assistant",
                document_type=document_type,
                super_admin_view=True,
            )
        else:
            return KnowledgeService(db).list_docs(
                organization_id=org_id,
                scope=scope,
                document_type=document_type,
                super_admin_view=True,
            )

    # Regular organization admin
    return KnowledgeService(db).list_docs(
        organization_id=org_id,
        scope=scope,
        document_type=document_type,
        super_admin_view=False,
    )


def get_doc(
    doc_id: int,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> KnowledgeDocOut:
    is_sa = _is_super_admin(current_user)
    org_id = _get_org_id(current_user)
    return KnowledgeService(db).get_doc(doc_id, organization_id=org_id, super_admin_view=is_sa)


def create_doc(
    payload: KnowledgeDocCreate,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> KnowledgeDocOut:
    is_sa = _is_super_admin(current_user)
    org_id = _get_org_id(current_user)

    # Non-super-admin users cannot set cross-org parameters
    if not is_sa and payload.organization_id is not None and payload.organization_id != org_id:
        raise HTTPException(status_code=403, detail="Forbidden: cannot create documents for another organization.")

    uploaded_by = current_user.id if current_user else None
    return KnowledgeService(db).create_doc(
        payload,
        organization_id=org_id,
        uploaded_by=uploaded_by,
        super_admin_mode=is_sa,
    )


def upload_doc(
    file: UploadFile = File(...),
    scope: Optional[str] = Form(None),
    document_type: Optional[str] = Form(None),
    target_org_id: Optional[int] = Form(None),
    agent_id: Optional[str] = Form(None),
    assistant_id: Optional[str] = Form(None),
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> KnowledgeDocOut:
    is_sa = _is_super_admin(current_user)
    org_id = _get_org_id(current_user)

    if is_sa:
        if target_org_id is not None:
            effective_org_id = target_org_id
        elif scope == "superadmin_assistant":
            effective_org_id = None
        else:
            effective_org_id = org_id
    else:
        # Non-super-admin cannot change target org
        if target_org_id is not None and target_org_id != org_id:
            raise HTTPException(status_code=403, detail="Forbidden: cannot upload documents for another organization.")
        effective_org_id = org_id

    uploaded_by = current_user.id if current_user else None
    return KnowledgeService(db).upload_file(
        file,
        organization_id=effective_org_id,
        scope=scope,
        document_type=document_type,
        agent_id=agent_id,
        assistant_id=assistant_id,
        uploaded_by=uploaded_by,
        super_admin_mode=is_sa,
    )


def search_docs(
    q: Optional[str] = "",
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[KnowledgeDocOut]:
    org_id = _get_org_id(current_user)
    return KnowledgeService(db).search_docs(q, organization_id=org_id)


def update_doc(
    doc_id: int,
    payload: KnowledgeDocUpdate,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> KnowledgeDocOut:
    is_sa = _is_super_admin(current_user)
    org_id = _get_org_id(current_user)
    return KnowledgeService(db).update_doc(doc_id, payload, organization_id=org_id, super_admin_view=is_sa)


def delete_doc(
    doc_id: int,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    is_sa = _is_super_admin(current_user)
    org_id = _get_org_id(current_user)
    KnowledgeService(db).delete_doc(doc_id, organization_id=org_id, super_admin_view=is_sa)
    return {"message": "Knowledge document deleted"}


def get_doc_file(
    doc_id: int,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    is_sa = _is_super_admin(current_user)
    org_id = _get_org_id(current_user)
    return KnowledgeService(db).get_doc_file(doc_id, organization_id=org_id, super_admin_view=is_sa or (current_user is None))


def query_rag(
    payload: dict,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> dict:
    from src.services.rag_service import rag_service
    q = payload.get("query", "")
    is_sa = _is_super_admin(current_user)
    user_org_id = _get_org_id(current_user)

    if is_sa:
        org_id = payload.get("organization_id")
    else:
        org_id = user_org_id

    scope = payload.get("scope", None)
    ctx = rag_service.retrieve_context(q, organization_id=org_id, scope=scope)
    return {
        "query": q,
        "organization_id": org_id,
        "scope": scope,
        "context": ctx,
        "found": bool(ctx.strip())
    }


def reindex_all(db: Session = Depends(get_db)) -> dict:
    count = KnowledgeService(db).reindex_all_docs()
    return {"message": f"Successfully re-indexed {count} documents", "count": count}
