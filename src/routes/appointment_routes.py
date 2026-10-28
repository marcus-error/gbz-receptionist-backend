from fastapi import APIRouter

from src.controllers import appointment_controller
from src.validators.appointment_validator import AppointmentOut

router = APIRouter(prefix="/appointments", tags=["Appointments"])

router.add_api_route("", appointment_controller.list_appointments, methods=["GET"], response_model=list[AppointmentOut])
router.add_api_route("", appointment_controller.create_appointment, methods=["POST"], response_model=AppointmentOut, status_code=201)
router.add_api_route("/sync-calendar", appointment_controller.sync_calendar, methods=["POST"])
router.add_api_route("/{appointment_id}", appointment_controller.get_appointment, methods=["GET"], response_model=AppointmentOut)
router.add_api_route("/{appointment_id}", appointment_controller.update_appointment, methods=["PUT"], response_model=AppointmentOut)
router.add_api_route("/{appointment_id}", appointment_controller.delete_appointment, methods=["DELETE"])

