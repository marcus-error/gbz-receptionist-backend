from typing import List, Optional
from sqlalchemy.orm import Session
from src.models.department import Department


class DepartmentRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_all(self, organization_id: int) -> List[Department]:
        return (
            self.db.query(Department)
            .filter(Department.organization_id == organization_id)
            .order_by(Department.id.asc())
            .all()
        )

    def get_by_id(self, dept_id: int, organization_id: int) -> Optional[Department]:
        return (
            self.db.query(Department)
            .filter(Department.id == dept_id, Department.organization_id == organization_id)
            .first()
        )

    def get_by_name(self, name: str, organization_id: int) -> Optional[Department]:
        return (
            self.db.query(Department)
            .filter(Department.name.ilike(name), Department.organization_id == organization_id)
            .first()
        )

    def create(self, data: dict) -> Department:
        dept = Department(**data)
        self.db.add(dept)
        self.db.commit()
        self.db.refresh(dept)
        return dept

    def update(self, dept: Department, data: dict) -> Department:
        for key, value in data.items():
            if value is not None:
                setattr(dept, key, value)
        self.db.commit()
        self.db.refresh(dept)
        return dept

    def delete(self, dept: Department) -> None:
        self.db.delete(dept)
        self.db.commit()
