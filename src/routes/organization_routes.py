from datetime import datetime, timezone, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.middleware.auth_middleware import get_current_user, require_role
from src.models.organization import Organization
from src.models.subscription import Subscription
from src.models.user import User
from src.models.permission import UserPermission
from src.services.auth_service import AuthService
from src.validators.auth_validator import RegisterRequest

router = APIRouter(prefix="/organizations", tags=["Organizations"])


class CompanySignupRequest(BaseModel):
    name: str
    organization_type: str
    admin_name: str
    admin_email: EmailStr
    admin_password: str

    # Explicit KYC Fields
    official_email: Optional[str] = None
    contact_person: Optional[str] = None
    gst_number: Optional[str] = None
    full_address: Optional[str] = None
    tax_id: Optional[str] = None
    pan_number: Optional[str] = None
    contact_phone: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    identity_proof_type: Optional[str] = None
    identity_proof_number: Optional[str] = None


class OrganizationOut(BaseModel):
    id: int
    name: str
    organization_type: str
    status: str
    official_email: Optional[str] = None
    contact_person: Optional[str] = None
    gst_number: Optional[str] = None
    full_address: Optional[str] = None
    tax_id: Optional[str] = None
    pan_number: Optional[str] = None
    contact_phone: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    identity_proof_type: Optional[str] = None
    identity_proof_number: Optional[str] = None
    kyc_status: Optional[str] = "verified"
    created_at: datetime

    class Config:
        from_attributes = True


@router.post("/signup", response_model=dict, status_code=status.HTTP_201_CREATED)
def company_signup(payload: CompanySignupRequest, db: Session = Depends(get_db)):
    """Public Company/Organization signup endpoint with KYC fields."""
    # Check if admin email already exists
    existing = db.query(User).filter(User.email == payload.admin_email).first()
    if existing:
        raise HTTPException(status_code=409, detail="Email is already registered")

    # 1. Create Organization with KYC Data
    org = Organization(
        name=payload.name,
        organization_type=payload.organization_type or "Other",
        status="active",
        official_email=payload.official_email or payload.admin_email,
        contact_person=payload.contact_person or payload.admin_name,
        gst_number=payload.gst_number or payload.tax_id,
        full_address=payload.full_address or payload.address,
        tax_id=payload.tax_id or payload.gst_number,
        pan_number=payload.pan_number,
        contact_phone=payload.contact_phone,
        address=payload.address or payload.full_address,
        city=payload.city,
        state=payload.state,
        postal_code=payload.postal_code,
        identity_proof_type=payload.identity_proof_type,
        identity_proof_number=payload.identity_proof_number,
        kyc_status="verified"
    )
    db.add(org)
    db.commit()
    db.refresh(org)

    # 2. Create Trial/Enterprise Subscription for Organization
    sub = Subscription(
        organization_id=org.id,
        plan="Trial",
        status="active",
        start_date=datetime.now(timezone.utc),
        expiry_date=datetime.now(timezone.utc) + timedelta(days=30)
    )
    db.add(sub)
    db.commit()

    # 3. Create initial Admin user for the Organization
    auth_service = AuthService(db)
    token_resp = auth_service.register(
        payload=RegisterRequest(
            name=payload.admin_name,
            email=payload.admin_email,
            password=payload.admin_password
        ),
        organization_id=org.id,
        role="admin"
    )

    # Assign all standard admin permissions
    all_perms = [
        "dashboard", "calls", "recordings", "live_calls", "reports",
        "appointments", "calendar", "staff", "knowledge_base",
        "ai_config", "telephony", "whatsapp", "email", "crm",
        "integrations", "settings", "user_management"
    ]
    for p_name in all_perms:
        db.add(UserPermission(user_id=token_resp.user.id, permission_name=p_name, enabled=1))
    db.commit()

    return {
        "status": "success",
        "message": "Organization created successfully",
        "organization": {
            "id": org.id,
            "name": org.name,
            "organization_type": org.organization_type,
            "kyc_status": org.kyc_status
        },
        "access_token": token_resp.access_token
    }


@router.get("", response_model=List[OrganizationOut])
def list_organizations(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["super_admin"]))
):
    """List all organizations (Super Admin only)."""
    return db.query(Organization).all()


@router.put("/{org_id}/status")
def update_organization_status(
    org_id: int,
    status_value: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["super_admin"]))
):
    """Enable, suspend, or reactivate an organization (Super Admin only)."""
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    org.status = status_value
    db.commit()
    return {"status": "success", "message": f"Organization status updated to {status_value}"}


@router.put("/{org_id}/kyc-status")
def update_organization_kyc_status(
    org_id: int,
    kyc_status: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["super_admin"]))
):
    """Update KYC verification status for an organization (Super Admin only)."""
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    org.kyc_status = kyc_status
    db.commit()
    return {"status": "success", "message": f"Organization KYC status updated to {kyc_status}"}
