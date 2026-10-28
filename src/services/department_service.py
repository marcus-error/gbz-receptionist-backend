from typing import List
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.models.department import Department
from src.repositories.department_repository import DepartmentRepository
from src.validators.department_validator import DepartmentCreate, DepartmentUpdate


class DepartmentService:
    def __init__(self, db: Session):
        self.db = db
        self.repo = DepartmentRepository(db)

    def list_departments(self, organization_id: int) -> List[Department]:
        return self.repo.get_all(organization_id=organization_id)

    def get_department(self, dept_id: int, organization_id: int) -> Department:
        dept = self.repo.get_by_id(dept_id, organization_id=organization_id)
        if not dept:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Department with ID {dept_id} not found in your organization"
            )
        return dept

    def create_department(self, payload: DepartmentCreate, organization_id: int) -> Department:
        data = payload.model_dump()
        data["organization_id"] = organization_id
        
        # Check duplicate name within the same organization
        existing = self.repo.get_by_name(payload.name, organization_id=organization_id)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"A department named '{payload.name}' already exists in your organization."
            )
        return self.repo.create(data)

    def update_department(self, dept_id: int, payload: DepartmentUpdate, organization_id: int) -> Department:
        dept = self.get_department(dept_id, organization_id=organization_id)
        data = payload.model_dump(exclude_unset=True)
        
        if "name" in data and data["name"] != dept.name:
            existing = self.repo.get_by_name(data["name"], organization_id=organization_id)
            if existing and existing.id != dept_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"A department named '{data['name']}' already exists in your organization."
                )
        return self.repo.update(dept, data)

    def delete_department(self, dept_id: int, organization_id: int) -> None:
        dept = self.get_department(dept_id, organization_id=organization_id)
        self.repo.delete(dept)
