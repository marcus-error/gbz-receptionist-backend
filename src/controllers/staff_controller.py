from typing import List, Optional

from fastapi import Depends
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.middleware.auth_middleware import get_current_user
from src.models.user import User
from src.services.staff_service import StaffService
from src.validators.staff_validator import StaffCreate, StaffOut, StaffUpdate


def _get_org_id(user: Optional[User]) -> Optional[int]:
    if not user:
        return 1
    if user.role == "super_admin" and user.organization_id is None:
        return None
    return user.organization_id


def list_staff(
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> List[StaffOut]:
    org_id = _get_org_id(current_user)
    return StaffService(db).list_staff(organization_id=org_id)


def get_staff(
    staff_id: int,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> StaffOut:
    org_id = _get_org_id(current_user)
    return StaffService(db).get_staff(staff_id, organization_id=org_id)


def create_staff(
    payload: StaffCreate,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> StaffOut:
    org_id = _get_org_id(current_user)
    return StaffService(db).create_staff(payload, organization_id=org_id)


def update_staff(
    staff_id: int,
    payload: StaffUpdate,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> StaffOut:
    org_id = _get_org_id(current_user)
    return StaffService(db).update_staff(staff_id, payload, organization_id=org_id)


def delete_staff(
    staff_id: int,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> dict:
    org_id = _get_org_id(current_user)
    StaffService(db).delete_staff(staff_id, organization_id=org_id)
    return {"message": "Staff member deleted"}
