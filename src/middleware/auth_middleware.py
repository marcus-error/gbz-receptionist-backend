from typing import List, Optional
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.config.jwt import decode_access_token
from src.models.user import User
from src.models.permission import UserPermission
from src.models.subscription import Subscription
from src.models.organization import Organization

security_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(security_scheme),
    db: Session = Depends(get_db)
) -> User:
    user = None
    if credentials and credentials.credentials:
        token = credentials.credentials
        payload = decode_access_token(token)
        if payload and "sub" in payload:
            user_id = payload.get("sub")
            try:
                user = db.query(User).filter(User.id == int(user_id), User.is_active == 1).first()
            except Exception:
                user = None

    # Fallback to active super_admin user if no valid token is provided (local dashboard access)
    if not user:
        user = db.query(User).filter(User.role == "super_admin", User.is_active == 1).first()
        if not user:
            user = db.query(User).filter(User.is_active == 1).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token required or no active user account found"
        )

    # Attach permissions
    if user.role == "super_admin":
        user.permissions = [
            "dashboard", "calls", "recordings", "live_calls", "reports",
            "appointments", "calendar", "staff", "knowledge_base",
            "ai_config", "telephony", "whatsapp", "email", "crm",
            "integrations", "settings", "user_management", "organization_management"
        ]
        user.organization_name = "Super Admin"
    else:
        perms = db.query(UserPermission.permission_name).filter(
            UserPermission.user_id == user.id,
            UserPermission.enabled == 1
        ).all()
        user.permissions = [p[0] for p in perms]
        
        if user.organization_id:
            org = db.query(Organization).filter(Organization.id == user.organization_id).first()
            if org:
                user.organization_name = org.name
            else:
                user.organization_name = None
        else:
            user.organization_name = None

    return user


def get_optional_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(security_scheme),
    db: Session = Depends(get_db)
) -> Optional[User]:
    if not credentials or not credentials.credentials:
        return None
    try:
        token = credentials.credentials
        payload = decode_access_token(token)
        if not payload or "sub" not in payload:
            return None
        user_id = payload.get("sub")
        user = db.query(User).filter(User.id == int(user_id)).first()
        if not user or not user.is_active:
            return None
        return user
    except Exception:
        return None


def require_role(allowed_roles: List[str]):
    def role_checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Requires one of roles: {', '.join(allowed_roles)}"
            )
        return current_user
    return role_checker


def require_permission(permission_name: str):
    def permission_checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role == "super_admin":
            return current_user
        if permission_name not in current_user.permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied. You lack '{permission_name}' permission."
            )
        return current_user
    return permission_checker


def require_subscription(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> User:
    if current_user.role == "super_admin":
        return current_user
    
    if not current_user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not associated with an organization"
        )

    sub = db.query(Subscription).filter(
        Subscription.organization_id == current_user.organization_id
    ).first()

    if not sub or sub.status not in {"active", "trial"}:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Organization subscription is expired, suspended, or inactive."
        )
    return current_user
