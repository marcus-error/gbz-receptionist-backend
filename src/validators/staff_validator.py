import re
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.models.staff import StaffStatus

# Accepts:
#   E.164           : +<1-3 digit country code><6-14 digits>   e.g. +919876543210
#   Local formats   : optional leading +, digits / spaces / dashes / dots / parens
#                     total 6-15 digits (after stripping non-digit chars)
_PHONE_RE = re.compile(
    r"^\+?[\d\s\-().]{6,20}$"
)
_MIN_DIGITS = 6
_MAX_DIGITS = 15


def _validate_phone(value: Optional[str]) -> Optional[str]:
    """Shared phone validator – returns cleaned value or raises ValueError."""
    if value is None:
        return value
    value = value.strip()
    if not value:
        return None
    if not _PHONE_RE.match(value):
        raise ValueError(
            "Invalid phone number. Use E.164 format (e.g. +919876543210) "
            "or a local format with digits, spaces, dashes, or parentheses."
        )
    digit_count = len(re.sub(r"\D", "", value))
    if digit_count < _MIN_DIGITS:
        raise ValueError(f"Phone number must contain at least {_MIN_DIGITS} digits.")
    if digit_count > _MAX_DIGITS:
        raise ValueError(f"Phone number must not exceed {_MAX_DIGITS} digits.")
    return value


class StaffCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    department: str = Field(..., min_length=1, max_length=100)
    working_hours: str = Field(default="09:00-18:00", min_length=1, max_length=100)
    email: Optional[str] = None
    phone: Optional[str] = None
    appointment_duration: Optional[str] = "30 min"
    status: Optional[StaffStatus] = StaffStatus.AVAILABLE

    @field_validator("phone", mode="before")
    @classmethod
    def validate_phone(cls, v):
        return _validate_phone(v)


class StaffUpdate(BaseModel):
    name: Optional[str] = None
    department: Optional[str] = None
    working_hours: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    appointment_duration: Optional[str] = None
    status: Optional[StaffStatus] = None

    @field_validator("phone", mode="before")
    @classmethod
    def validate_phone(cls, v):
        return _validate_phone(v)


class StaffOut(BaseModel):
    id: int
    name: str
    department: str
    email: Optional[str] = None
    phone: Optional[str] = None
    working_hours: str
    appointment_duration: str
    status: StaffStatus

    model_config = ConfigDict(from_attributes=True)

