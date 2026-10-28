from typing import List, Optional
from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.middleware.auth_middleware import get_current_user
from src.models.user import User
from src.services.department_service import DepartmentService
from src.validators.department_validator import DepartmentCreate, DepartmentOut, DepartmentUpdate


def _get_org_id(user: Optional[User]) -> int:
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication token required")
    if user.organization_id is not None:
        return user.organization_id
    if user.role == "super_admin":
        return 1
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User is not assigned to an organization")


def list_departments(
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> List[DepartmentOut]:
    org_id = _get_org_id(current_user)
    return DepartmentService(db).list_departments(organization_id=org_id)


def get_department(
    dept_id: int,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> DepartmentOut:
    org_id = _get_org_id(current_user)
    return DepartmentService(db).get_department(dept_id, organization_id=org_id)


def create_department(
    payload: DepartmentCreate,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> DepartmentOut:
    org_id = _get_org_id(current_user)
    return DepartmentService(db).create_department(payload, organization_id=org_id)


def update_department(
    dept_id: int,
    payload: DepartmentUpdate,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> DepartmentOut:
    org_id = _get_org_id(current_user)
    return DepartmentService(db).update_department(dept_id, payload, organization_id=org_id)


def delete_department(
    dept_id: int,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> dict:
    org_id = _get_org_id(current_user)
    DepartmentService(db).delete_department(dept_id, organization_id=org_id)
    return {"message": "Department deleted successfully"}
