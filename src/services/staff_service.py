from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.models.staff import Staff
from src.repositories.staff_repository import StaffRepository
from src.validators.staff_validator import StaffCreate, StaffUpdate


class StaffService:
    def __init__(self, db: Session):
        self.repo = StaffRepository(db)

    def list_staff(self, organization_id: Optional[int] = None) -> List[Staff]:
        return self.repo.get_all(organization_id=organization_id)

    def get_staff(self, staff_id: int, organization_id: Optional[int] = None) -> Staff:
        staff = self.repo.get_by_id(staff_id, organization_id=organization_id)
        if not staff:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Staff member not found")
        return staff

    def create_staff(self, payload: StaffCreate, organization_id: Optional[int] = None) -> Staff:
        data = payload.model_dump()
        if organization_id is not None:
            data["organization_id"] = organization_id
        return self.repo.create(data)

    def update_staff(self, staff_id: int, payload: StaffUpdate, organization_id: Optional[int] = None) -> Staff:
        staff = self.get_staff(staff_id, organization_id=organization_id)
        return self.repo.update(staff, payload.model_dump(exclude_unset=True))

    def delete_staff(self, staff_id: int, organization_id: Optional[int] = None) -> None:
        staff = self.get_staff(staff_id, organization_id=organization_id)
        self.repo.delete(staff)
