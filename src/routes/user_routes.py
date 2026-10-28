from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session
import bcrypt
from src.config.database import get_db
from src.middleware.auth_middleware import get_current_user, require_permission, require_role
from src.models.user import User
from src.models.permission import UserPermission

def hash_password(password: str) -> str:
    pwd_bytes = password.encode("utf-8")[:72]
    return bcrypt.hashpw(pwd_bytes, bcrypt.gensalt()).decode("utf-8")


router = APIRouter(prefix="/users", tags=["Users"])


class CreateUserRequest(BaseModel):
    name: str
    email: EmailStr
    password: str
    role: str = "admin"  # admin, staff (super_admin can only be created by existing super_admin)
    organization_id: Optional[int] = None
    permissions: List[str] = []


class UpdatePermissionsRequest(BaseModel):
    permissions: List[str]  # List of enabled permission names


class UserOut(BaseModel):
    id: int
    name: str
    email: str
    role: str
    organization_id: Optional[int]
    is_active: bool
    permissions: List[str] = []

    class Config:
        from_attributes = True


ALL_PERMISSIONS = [
    "dashboard", "calls", "recordings", "live_calls", "reports",
    "appointments", "calendar", "staff", "knowledge_base",
    "ai_config", "telephony", "whatsapp", "email", "crm",
    "integrations", "settings", "user_management"
]


@router.get("", response_model=List[UserOut])
def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("user_management"))
):
    """List users within current user's organization (or all users if Super Admin)."""
    if current_user.role == "super_admin" and current_user.organization_id is None:
        users = db.query(User).all()
    else:
        users = db.query(User).filter(User.organization_id == current_user.organization_id).all()

    for u in users:
        if u.role == "super_admin":
            u.permissions = ALL_PERMISSIONS + ["organization_management"]
        else:
            perms = db.query(UserPermission.permission_name).filter(
                UserPermission.user_id == u.id,
                UserPermission.enabled == 1
            ).all()
            u.permissions = [p[0] for p in perms]
    return users


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: CreateUserRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("user_management"))
):
    """Create a user and assign permissions."""
    # Prevent unauthorized Super Admin self-creation
    if payload.role == "super_admin" and current_user.role != "super_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only existing Super Admins can provision a Super Admin account"
        )

    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=409, detail="Email is already registered")

    org_id = payload.organization_id if (current_user.role == "super_admin" and payload.organization_id) else current_user.organization_id

    new_user = User(
        name=payload.name,
        email=payload.email,
        password_hash=hash_password(payload.password),
        role=payload.role,
        organization_id=org_id,
        is_active=True
    )

    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    # Assign permissions
    assigned_perms = payload.permissions if payload.permissions else ALL_PERMISSIONS
    for p_name in assigned_perms:
        db.add(UserPermission(user_id=new_user.id, permission_name=p_name, enabled=1))
    db.commit()

    new_user.permissions = assigned_perms
    return new_user


@router.put("/{user_id}/permissions")
def update_user_permissions(
    user_id: int,
    payload: UpdatePermissionsRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("user_management"))
):
    """Super Admin or authorized Admin updates permissions for a user."""
    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    if current_user.role != "super_admin" and target_user.organization_id != current_user.organization_id:
        raise HTTPException(status_code=403, detail="Cannot edit users from another organization")

    # Clear existing permissions
    db.query(UserPermission).filter(UserPermission.user_id == user_id).delete()
    db.commit()

    # Add new enabled permissions
    for p_name in payload.permissions:
        db.add(UserPermission(user_id=user_id, permission_name=p_name, enabled=1))
    db.commit()

    return {"status": "success", "message": f"Permissions updated for user {target_user.name}", "permissions": payload.permissions}
