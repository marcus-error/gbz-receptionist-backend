from typing import List, Optional

from fastapi import Depends
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.middleware.auth_middleware import get_current_user
from src.models.user import User
from src.services.appointment_service import AppointmentService
from src.validators.appointment_validator import AppointmentCreate, AppointmentOut, AppointmentUpdate


def _get_org_id(user: Optional[User]) -> Optional[int]:
    if not user:
        return 1
    if user.role == "super_admin" and user.organization_id is None:
        return None
    return user.organization_id


def list_appointments(
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> List[AppointmentOut]:
    org_id = _get_org_id(current_user)
    return AppointmentService(db).list_appointments(organization_id=org_id)


def get_appointment(
    appointment_id: int,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> AppointmentOut:
    org_id = _get_org_id(current_user)
    return AppointmentService(db).get_appointment(appointment_id, organization_id=org_id)


def create_appointment(
    payload: AppointmentCreate,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> AppointmentOut:
    user_id = current_user.id if current_user else None
    org_id = _get_org_id(current_user)
    return AppointmentService(db).create_appointment(payload, user_id=user_id, organization_id=org_id)


def update_appointment(
    appointment_id: int,
    payload: AppointmentUpdate,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> AppointmentOut:
    user_id = current_user.id if current_user else None
    org_id = _get_org_id(current_user)
    return AppointmentService(db).update_appointment(appointment_id, payload, user_id=user_id, organization_id=org_id)


def delete_appointment(
    appointment_id: int,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> dict:
    user_id = current_user.id if current_user else None
    org_id = _get_org_id(current_user)
    AppointmentService(db).delete_appointment(appointment_id, user_id=user_id, organization_id=org_id)
    return {"message": "Appointment deleted"}


def sync_calendar(
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> dict:
    user_id = current_user.id if current_user else None
    return AppointmentService(db).sync_with_google_calendar(user_id=user_id)


