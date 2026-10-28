from typing import Optional
import bcrypt
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.config.jwt import create_access_token
from src.repositories.auth_repository import AuthRepository
from src.models.user import User
from src.models.organization import Organization
from src.models.subscription import Subscription
from src.models.permission import UserPermission
from src.validators.auth_validator import LoginRequest, RegisterRequest, TokenResponse


def hash_password(password: str) -> str:
    pwd_bytes = password.encode("utf-8")[:72]
    return bcrypt.hashpw(pwd_bytes, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        pwd_bytes = plain_password.encode("utf-8")[:72]
        return bcrypt.checkpw(pwd_bytes, hashed_password.encode("utf-8"))
    except Exception:
        return False


class AuthService:
    def __init__(self, db: Session):
        self.repo = AuthRepository(db)
        self.db = db

    def register(self, payload: RegisterRequest, organization_id: Optional[int] = None, role: str = "admin") -> TokenResponse:
        from datetime import datetime, timezone, timedelta
        if self.repo.get_by_email(payload.email):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

        if not organization_id:
            org_name = getattr(payload, "company_name", None) or f"{payload.name}'s Organization"
            org = Organization(
                name=org_name,
                organization_type="General",
                status="active"
            )
            self.db.add(org)
            self.db.commit()
            self.db.refresh(org)

            sub = Subscription(
                organization_id=org.id,
                plan="Trial",
                status="active",
                start_date=datetime.now(timezone.utc),
                expiry_date=datetime.now(timezone.utc) + timedelta(days=30)
            )
            self.db.add(sub)
            self.db.commit()
            organization_id = org.id

        user = self.repo.create(
            {
                "name": payload.name,
                "email": payload.email,
                "password_hash": hash_password(payload.password),
                "organization_id": organization_id,
                "role": role,
            }
        )

        all_perms = [
            "dashboard", "calls", "recordings", "live_calls", "reports",
            "appointments", "calendar", "staff", "knowledge_base",
            "ai_config", "telephony", "whatsapp", "email", "crm",
            "integrations", "settings", "user_management"
        ]
        for p_name in all_perms:
            self.db.add(UserPermission(user_id=user.id, permission_name=p_name, enabled=1))
        self.db.commit()
        user.permissions = all_perms

        token = create_access_token({
            "sub": str(user.id),
            "email": user.email,
            "role": user.role,
            "organization_id": user.organization_id
        })
        return TokenResponse(access_token=token, user=user)

    def login(self, payload: LoginRequest) -> TokenResponse:
        user = self.repo.get_by_email(payload.email)
        if not user or not verify_password(payload.password, user.password_hash):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

        # Fetch permissions
        if user.role == "super_admin":
            permissions = [
                "dashboard", "calls", "recordings", "live_calls", "reports",
                "appointments", "calendar", "staff", "knowledge_base",
                "ai_config", "telephony", "whatsapp", "email", "crm",
                "integrations", "settings", "user_management", "organization_management"
            ]
        else:
            perms = self.db.query(UserPermission.permission_name).filter(
                UserPermission.user_id == user.id,
                UserPermission.enabled == 1
            ).all()
            permissions = [p[0] for p in perms]

        user.permissions = permissions

        token = create_access_token({
            "sub": str(user.id),
            "email": user.email,
            "role": user.role,
            "organization_id": user.organization_id
        })
        return TokenResponse(access_token=token, user=user)
