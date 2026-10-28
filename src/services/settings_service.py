from typing import Optional
from sqlalchemy.orm import Session

from src.models.settings import AppSettings
from src.repositories.settings_repository import SettingsRepository
from src.validators.settings_validator import SettingsUpdate


class SettingsService:
    def __init__(self, db: Session):
        self.repo = SettingsRepository(db)

    def get_settings(self, organization_id: Optional[int] = 1) -> AppSettings:
        return self.repo.get_or_create(organization_id=organization_id)

    def update_settings(self, payload: SettingsUpdate, organization_id: Optional[int] = 1) -> AppSettings:
        settings = self.get_settings(organization_id=organization_id)
        return self.repo.update(settings, payload.model_dump(exclude_unset=True))
