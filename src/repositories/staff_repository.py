from typing import List, Optional

from sqlalchemy.orm import Session

from src.models.staff import Staff


class StaffRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_all(self, organization_id: Optional[int] = None) -> List[Staff]:
        query = self.db.query(Staff)
        if organization_id is not None:
            query = query.filter(Staff.organization_id == organization_id)
        return query.order_by(Staff.id.asc()).all()

    def get_by_id(self, staff_id: int, organization_id: Optional[int] = None) -> Optional[Staff]:
        query = self.db.query(Staff).filter(Staff.id == staff_id)
        if organization_id is not None:
            query = query.filter(Staff.organization_id == organization_id)
        return query.first()

    def create(self, data: dict) -> Staff:
        staff = Staff(**data)
        self.db.add(staff)
        self.db.commit()
        self.db.refresh(staff)
        return staff

    def update(self, staff: Staff, data: dict) -> Staff:
        for key, value in data.items():
            if value is not None:
                setattr(staff, key, value)
        self.db.commit()
        self.db.refresh(staff)
        return staff

    def delete(self, staff: Staff) -> None:
        self.db.delete(staff)
        self.db.commit()
