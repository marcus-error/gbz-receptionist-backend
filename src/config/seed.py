"""
Initial seed data for development / demo environments.

Called once from init_db() when the database tables are empty.
Only seeds if there are no existing rows – never overwrites production data.
"""

from __future__ import annotations
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
import bcrypt


def hash_password(password: str) -> str:
    pwd_bytes = password.encode("utf-8")[:72]
    return bcrypt.hashpw(pwd_bytes, bcrypt.gensalt()).decode("utf-8")


def seed_if_empty(db: Session) -> None:
    """Seed default organization, roles, permissions, subscriptions, and super admin."""
    from src.models.organization import Organization
    from src.models.subscription import Subscription
    from src.models.user import User
    from src.models.permission import Permission, UserPermission
    from src.models.settings import AppSettings

    # 1. Default Organization
    default_org = db.query(Organization).filter(Organization.id == 1).first()
    if not default_org:
        default_org = Organization(
            id=1,
            name="Default Organization",
            organization_type="General",
            status="active"
        )
        db.add(default_org)
        db.commit()
        db.refresh(default_org)

    # 2. Default Subscription
    sub = db.query(Subscription).filter(Subscription.organization_id == default_org.id).first()
    if not sub:
        sub = Subscription(
            organization_id=default_org.id,
            plan="Enterprise",
            status="active",
            start_date=datetime.now(timezone.utc),
            expiry_date=datetime.now(timezone.utc) + timedelta(days=365)
        )
        db.add(sub)
        db.commit()

    # 3. Super Admin & Admin Users
    super_admin = db.query(User).filter(User.role == "super_admin").first()
    if not super_admin:
        hashed_password = hash_password("SuperAdmin123!")
        super_admin = User(
            name="Platform Super Admin",
            email="superadmin@receptionist.ai",
            password_hash=hashed_password,
            role="super_admin",
            organization_id=None,
            is_active=True
        )
        db.add(super_admin)
        db.commit()

    admin_user = db.query(User).filter(User.email == "admin@receptionist.ai").first()
    if not admin_user:
        hashed_password = hash_password("Admin123!")
        admin_user = User(
            name="Organization Admin",
            email="admin@receptionist.ai",
            password_hash=hashed_password,
            role="admin",
            organization_id=default_org.id,
            is_active=True
        )
        db.add(admin_user)
        db.commit()
        db.refresh(admin_user)

        # Assign all permissions to initial Admin user
        permissions_list = [
            "dashboard", "calls", "recordings", "live_calls", "reports",
            "appointments", "calendar", "staff", "knowledge_base",
            "ai_config", "telephony", "whatsapp", "email", "crm",
            "integrations", "settings", "user_management"
        ]
        for perm in permissions_list:
            db.add(UserPermission(user_id=admin_user.id, permission_name=perm, enabled=1))
        db.commit()

    # 4. App Settings (ensure one row exists)
    if db.query(AppSettings).count() == 0:
        db.add(AppSettings())

    db.commit()
