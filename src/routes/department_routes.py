from fastapi import APIRouter

from src.controllers import department_controller
from src.validators.department_validator import DepartmentOut

router = APIRouter(prefix="/departments", tags=["Departments"])

router.add_api_route("", department_controller.list_departments, methods=["GET"], response_model=list[DepartmentOut])
router.add_api_route("", department_controller.create_department, methods=["POST"], response_model=DepartmentOut, status_code=201)
router.add_api_route("/{dept_id}", department_controller.get_department, methods=["GET"], response_model=DepartmentOut)
router.add_api_route("/{dept_id}", department_controller.update_department, methods=["PUT"], response_model=DepartmentOut)
router.add_api_route("/{dept_id}", department_controller.delete_department, methods=["DELETE"])
