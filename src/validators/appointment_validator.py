from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from src.models.appointment import AppointmentStatus


class AppointmentCreate(BaseModel):
    customer: str = Field(..., min_length=1, max_length=150)
    customer_id: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    department: str = Field(..., min_length=1, max_length=100)
    staff: str = Field(..., min_length=1, max_length=150)
    service: Optional[str] = Field(default=None, max_length=200)
    appointment_date: str = Field(..., min_length=1, max_length=50)
    appointment_time: str = Field(..., min_length=1, max_length=50)
    duration: Optional[str] = "30 Minutes"
    notes: Optional[str] = None
    reminder: Optional[str] = "WhatsApp + Email"
    status: Optional[AppointmentStatus] = AppointmentStatus.PENDING


class AppointmentUpdate(BaseModel):
    customer: Optional[str] = None
    customer_id: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    department: Optional[str] = None
    staff: Optional[str] = None
    service: Optional[str] = Field(default=None, max_length=200)
    appointment_date: Optional[str] = None
    appointment_time: Optional[str] = None
    duration: Optional[str] = None
    notes: Optional[str] = None
    reminder: Optional[str] = None
    status: Optional[AppointmentStatus] = None


class AppointmentOut(BaseModel):
    id: int
    customer: str
    customer_id: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    department: str
    staff: str
    service: Optional[str] = None
    appointment_date: str
    appointment_time: str
    duration: Optional[str] = None
    notes: Optional[str] = None
    reminder: Optional[str] = "WhatsApp + Email"
    status: AppointmentStatus

    model_config = ConfigDict(from_attributes=True)

