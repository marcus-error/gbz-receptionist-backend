from typing import Optional
from sqlalchemy.orm import Session

from src.models.settings import AppSettings


class SettingsRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_or_create(self, organization_id: Optional[int] = 1) -> AppSettings:
        org_id = organization_id or 1
        settings = self.db.query(AppSettings).filter(AppSettings.organization_id == org_id).first()
        if not settings:
            settings = AppSettings(organization_id=org_id)
            self.db.add(settings)
            self.db.commit()
            self.db.refresh(settings)
        return settings

    def update(self, settings: AppSettings, data: dict) -> AppSettings:
        for key, value in data.items():
            if value is not None:
                setattr(settings, key, value)
        self.db.commit()
        self.db.refresh(settings)
        return settings
