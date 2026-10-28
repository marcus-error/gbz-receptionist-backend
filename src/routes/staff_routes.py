from fastapi import APIRouter

from src.controllers import staff_controller
from src.validators.staff_validator import StaffOut

router = APIRouter(prefix="/staff", tags=["Staff"])

router.add_api_route("", staff_controller.list_staff, methods=["GET"], response_model=list[StaffOut])
router.add_api_route("", staff_controller.create_staff, methods=["POST"], response_model=StaffOut, status_code=201)
router.add_api_route("/{staff_id}", staff_controller.get_staff, methods=["GET"], response_model=StaffOut)
router.add_api_route("/{staff_id}", staff_controller.update_staff, methods=["PUT"], response_model=StaffOut)
router.add_api_route("/{staff_id}", staff_controller.delete_staff, methods=["DELETE"])
