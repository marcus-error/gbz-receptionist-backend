from typing import Optional
from fastapi import Depends
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.middleware.auth_middleware import get_current_user
from src.models.user import User
from src.services.dashboard_service import DashboardService


def _get_org_id(user: Optional[User]) -> Optional[int]:
    if not user:
        return 1
    if user.role == "super_admin" and user.organization_id is None:
        return None
    return user.organization_id


def get_stats(
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> dict:
    org_id = _get_org_id(current_user)
    return DashboardService(db).get_stats(organization_id=org_id)


def get_recent_calls(
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> list:
    org_id = _get_org_id(current_user)
    return DashboardService(db).get_recent_calls(organization_id=org_id)


def get_status(
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> dict:
    org_id = _get_org_id(current_user)
    return DashboardService(db).get_status(organization_id=org_id)


def get_insights(
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> dict:
    org_id = _get_org_id(current_user)
    return DashboardService(db).get_insights(organization_id=org_id)


def get_overview(
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> dict:
    org_id = _get_org_id(current_user)
    return DashboardService(db).get_overview(organization_id=org_id)


def get_upcoming_schedule(
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> list:
    org_id = _get_org_id(current_user)
    return DashboardService(db).get_upcoming_schedule(organization_id=org_id)


def get_leads_summary(
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> list:
    org_id = _get_org_id(current_user)
    return DashboardService(db).get_leads_summary(organization_id=org_id)


def get_staff_performance(
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> list:
    org_id = _get_org_id(current_user)
    return DashboardService(db).get_staff_performance(organization_id=org_id)

