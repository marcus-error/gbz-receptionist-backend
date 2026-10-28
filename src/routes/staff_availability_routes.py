import json
from datetime import date, datetime, time, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.models.staff import Staff

router = APIRouter(prefix="/staff", tags=["Staff Availability"])


class AvailabilityUpdate(BaseModel):
    working_days: list[str] = Field(default_factory=lambda: [
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"
    ])
    start_time: str = "09:00"
    end_time: str = "18:00"
    break_start: Optional[str] = None
    break_end: Optional[str] = None
    services: list[str] = Field(default_factory=list)
    appointment_duration: str = "30 min"
    leave_dates: list[str] = Field(default_factory=list)
    holiday_blocked_dates: list[str] = Field(default_factory=list)
    online_booking_enabled: bool = True
    calendar_connected: bool = False
    calendar_provider: Optional[str] = None


def _json(value, fallback):
    try:
        return json.loads(value or "")
    except Exception:
        return fallback


def _out(staff: Staff):
    return {
        "staff_id": staff.id,
        "name": staff.name,
        "department": staff.department,
        "working_days": _json(staff.working_days, []),
        "start_time": staff.start_time,
        "end_time": staff.end_time,
        "break_start": staff.break_start,
        "break_end": staff.break_end,
        "services": _json(staff.services, []),
        "appointment_duration": staff.appointment_duration,
        "leave_dates": _json(staff.leave_dates, []),
        "holiday_blocked_dates": _json(staff.holiday_blocked_dates, []),
        "online_booking_enabled": staff.online_booking_enabled,
        "calendar_connected": staff.calendar_connected,
        "calendar_provider": staff.calendar_provider,
        "availability_enabled": staff.status.value == "Available",
    }


def _clock(value: str) -> time:
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError:
        raise HTTPException(400, f"Invalid time '{value}'. Use HH:MM.")


@router.get("/{staff_id}/availability")
def get_staff_availability(staff_id: int, db: Session = Depends(get_db)):
    staff = db.query(Staff).filter(Staff.id == staff_id).first()
    if not staff:
        raise HTTPException(404, "Staff not found")
    return _out(staff)


@router.put("/{staff_id}/availability")
def update_staff_availability(
    staff_id: int, payload: AvailabilityUpdate, db: Session = Depends(get_db)
):
    staff = db.query(Staff).filter(Staff.id == staff_id).first()
    if not staff:
        raise HTTPException(404, "Staff not found")

    start, end = _clock(payload.start_time), _clock(payload.end_time)
    if start >= end:
        raise HTTPException(400, "Start time must be before end time")

    if payload.break_start and payload.break_end:
        bs, be = _clock(payload.break_start), _clock(payload.break_end)
        if bs >= be or bs < start or be > end:
            raise HTTPException(400, "Break must be inside working hours")

    allowed_days = {
        "Monday", "Tuesday", "Wednesday", "Thursday",
        "Friday", "Saturday", "Sunday"
    }
    if any(day not in allowed_days for day in payload.working_days):
        raise HTTPException(400, "Invalid working day")

    staff.working_days = json.dumps(payload.working_days)
    staff.start_time = payload.start_time
    staff.end_time = payload.end_time
    staff.break_start = payload.break_start
    staff.break_end = payload.break_end
    staff.services = json.dumps(payload.services)
    staff.appointment_duration = payload.appointment_duration
    staff.leave_dates = json.dumps(payload.leave_dates)
    staff.holiday_blocked_dates = json.dumps(payload.holiday_blocked_dates)
    staff.online_booking_enabled = payload.online_booking_enabled
    staff.calendar_connected = payload.calendar_connected
    staff.calendar_provider = payload.calendar_provider
    staff.working_hours = f"{payload.start_time}-{payload.end_time}"

    db.commit()
    db.refresh(staff)
    return _out(staff)


@router.get("/{staff_id}/availability/check")
def check_staff_availability(
    staff_id: int,
    requested_date: str,
    requested_time: Optional[str] = None,
    service: Optional[str] = None,
    db: Session = Depends(get_db),
):
    staff = db.query(Staff).filter(Staff.id == staff_id).first()
    if not staff:
        raise HTTPException(404, "Staff not found")

    try:
        target = date.fromisoformat(requested_date)
    except ValueError:
        raise HTTPException(400, "requested_date must be YYYY-MM-DD")

    reasons = []
    working_days = _json(staff.working_days, [])
    leaves = set(_json(staff.leave_dates, []))
    blocked = set(_json(staff.holiday_blocked_dates, []))

    if not staff.online_booking_enabled:
        reasons.append("Online booking disabled")
    if target.strftime("%A") not in working_days:
        reasons.append("Staff does not work on this day")
    if requested_date in leaves:
        reasons.append("Staff is on leave")
    if requested_date in blocked:
        reasons.append("Date is holiday/blocked")
    if service and service not in _json(staff.services, []):
        reasons.append("Service is not handled by this staff member")

    if requested_time and not reasons:
        try:
            requested = _clock(requested_time)
            start, end = _clock(staff.start_time), _clock(staff.end_time)
            if not (start <= requested < end):
                reasons.append("Outside working hours")
            if staff.break_start and staff.break_end:
                bs, be = _clock(staff.break_start), _clock(staff.break_end)
                if bs <= requested < be:
                    reasons.append("During staff break")
        except HTTPException:
            raise

    return {
        "staff_id": staff.id,
        "date": requested_date,
        "time": requested_time,
        "available": not reasons,
        "reasons": reasons,
        "appointment_duration": staff.appointment_duration,
        "calendar_connected": staff.calendar_connected,
        "calendar_provider": staff.calendar_provider,
    }
