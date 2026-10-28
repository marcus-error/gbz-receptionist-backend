from fastapi import Depends
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.services.settings_service import SettingsService
from src.validators.settings_validator import SettingsOut, SettingsUpdate


from typing import Optional
from src.middleware.auth_middleware import get_current_user
from src.models.user import User


def _get_org_id(user: Optional[User]) -> int:
    if not user:
        return 1
    return user.organization_id or 1


def get_settings(
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> SettingsOut:
    org_id = _get_org_id(current_user)
    return SettingsService(db).get_settings(organization_id=org_id)


def update_settings(
    payload: SettingsUpdate,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> SettingsOut:
    org_id = _get_org_id(current_user)
    return SettingsService(db).update_settings(payload, organization_id=org_id)
