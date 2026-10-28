from typing import List, Optional
from fastapi import Depends, Query
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.models.call_log import CallType
from src.services.call_service import CallService
from src.validators.call_validator import CallLogCreate, CallLogOut


from src.middleware.auth_middleware import get_current_user
from src.models.user import User


def _get_target_org_id(user: Optional[User], requested_org_id: Optional[int] = None) -> Optional[int]:
    if not user:
        return 1
    if user.role == "super_admin":
        return requested_org_id
    return user.organization_id


def list_calls(
    organization_id: Optional[int] = Query(None),
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> List[CallLogOut]:
    org_id = _get_target_org_id(current_user, organization_id)
    return CallService(db).list_calls(organization_id=org_id)


def get_latest_call(
    call_type: Optional[CallType] = Query(None),
    organization_id: Optional[int] = Query(None),
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Optional[CallLogOut]:
    org_id = _get_target_org_id(current_user, organization_id)
    return CallService(db).get_latest_call(call_type, organization_id=org_id)


def create_call(
    payload: CallLogCreate,
    organization_id: Optional[int] = Query(None),
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> CallLogOut:
    org_id = _get_target_org_id(current_user, organization_id)
    return CallService(db).create_call(payload, organization_id=org_id)
