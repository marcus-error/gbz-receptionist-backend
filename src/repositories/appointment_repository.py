"""
Data-access layer for appointments. Nothing here knows about HTTP -
it only talks to the DB session. Services call into this layer instead
of touching SQLAlchemy models directly.
"""
from typing import List, Optional

from sqlalchemy.orm import Session

from src.models.appointment import Appointment


class AppointmentRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_all(self, organization_id: Optional[int] = None) -> List[Appointment]:
        query = self.db.query(Appointment)
        if organization_id is not None:
            query = query.filter(Appointment.organization_id == organization_id)
        return query.order_by(Appointment.id.desc()).all()

    def get_by_id(self, appointment_id: int, organization_id: Optional[int] = None) -> Optional[Appointment]:
        query = self.db.query(Appointment).filter(Appointment.id == appointment_id)
        if organization_id is not None:
            query = query.filter(Appointment.organization_id == organization_id)
        return query.first()

    def create(self, data: dict) -> Appointment:
        appointment = Appointment(**data)
        self.db.add(appointment)
        self.db.commit()
        self.db.refresh(appointment)
        return appointment

    def update(self, appointment: Appointment, data: dict) -> Appointment:
        for key, value in data.items():
            if value is not None:
                setattr(appointment, key, value)
        self.db.commit()
        self.db.refresh(appointment)
        return appointment

    def delete(self, appointment: Appointment) -> None:
        self.db.delete(appointment)
        self.db.commit()
