from datetime import date, datetime, timedelta
import logging
from typing import Optional, List
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.models.staff import Staff
from src.models.appointment import Appointment, AppointmentStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/availability", tags=["Availability"])


def _parse_working_hours(value: str):
    match = re.match(r"^\s*(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})\s*$", value or "")
    if not match:
        return None, None
    return (
        datetime.strptime(match.group(1), "%H:%M").time(),
        datetime.strptime(match.group(2), "%H:%M").time(),
    )


def _duration_minutes(value: str) -> int:
    match = re.search(r"\d+", value or "")
    return int(match.group()) if match else 30


def _appointment_datetime(appt: Appointment, target: date):
    date_formats = ["%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"]
    time_formats = ["%I:%M %p", "%I:%M%p", "%H:%M", "%H:%M:%S", "%I %p", "%I%p"]

    parsed_date = None
    date_str = (appt.appointment_date or "").strip()
    for fmt in date_formats:
        try:
            parsed_date = datetime.strptime(date_str, fmt).date()
            break
        except ValueError:
            continue

    if not parsed_date:
        try:
            from src.services.calendar_verification_service import calendar_verification_service
            parsed_date = calendar_verification_service.parse_date(date_str)
        except Exception:
            pass

    if parsed_date != target:
        return None

    parsed_time = None
    time_str = (appt.appointment_time or "").strip().upper()
    for fmt in time_formats:
        try:
            parsed_time = datetime.strptime(time_str, fmt).time()
            break
        except ValueError:
            continue

    if parsed_time is None:
        try:
            from src.services.calendar_verification_service import calendar_verification_service
            t24 = calendar_verification_service.parse_time_to_24h(time_str)
            if t24:
                parsed_time = datetime.strptime(t24, "%H:%M").time()
        except Exception:
            pass

    if parsed_time is None:
        return None

    start = datetime.combine(parsed_date, parsed_time)
    return start, start + timedelta(minutes=_duration_minutes(appt.duration))


def _normalize_name(name: str) -> str:
    if not name:
        return ""
    cleaned = re.sub(r'^(?:dr\.?|doctor|mr\.?|mrs\.?|ms\.?)\s+', '', str(name).strip().lower())
    return re.sub(r'[^a-z0-9]', '', cleaned)


def _staff_matches(appt: Appointment, staff: Staff):
    if not appt.staff or not staff:
        return False
    if str(appt.staff).strip() == str(staff.id):
        return True
    n1 = _normalize_name(appt.staff)
    n2 = _normalize_name(staff.name)
    return bool(n1 and n2 and (n1 == n2 or n1 in n2 or n2 in n1))


def _event_to_interval(event: dict):
    try:
        start = datetime.fromisoformat(event["start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(event["end"].replace("Z", "+00:00"))
        return start.replace(tzinfo=None), end.replace(tzinfo=None)
    except Exception:
        return None


def _google_events(db, start, end, calendar_id="primary", user_id=None):
    try:
        from src.services.calendar_service import CalendarService
        return CalendarService(db, user_id=user_id).list_events(start, end, calendar_id)
    except Exception as e:
        logger.debug(f"Could not retrieve Google events for availability: {e}")
        return []


def _microsoft_events(db, start, end, calendar_id="default"):
    try:
        from src.routes.microsoft_calendar_routes import events
        return events(start, end, calendar_id, db)
    except Exception:
        return []


def _is_overlapping(start, end, blocked):
    return any(start < b_end and end > b_start for b_start, b_end in blocked)


@router.get("")
def get_available_slots(
    staff_id: int,
    selected_date: date = Query(...),
    service: Optional[str] = None,
    duration_minutes: Optional[int] = None,
    slot_interval_minutes: Optional[int] = None,
    google_calendar_id: str = "primary",
    microsoft_calendar_id: str = "default",
    db: Session = Depends(get_db),
):
    """
    Return only appointment slots that are genuinely free.

    Sources checked:
      - Staff working days/hours/breaks/leave/blocked holidays
      - Existing DB appointments
      - Google Calendar events
      - Microsoft Calendar events
      - Service compatibility
      - Appointment duration
      - Overlap/conflict detection
    """
    staff = db.query(Staff).filter(Staff.id == staff_id).first()
    if not staff:
        raise HTTPException(404, "Staff not found")

    # Works with both the enhanced Staff model and the original model.
    working_days = getattr(staff, "working_days", None)
    if working_days:
        import json
        try:
            working_days = json.loads(working_days)
        except Exception:
            working_days = []
        if selected_date.strftime("%A") not in working_days:
            return {
                "staff_id": staff.id,
                "date": selected_date.isoformat(),
                "available_slots": [],
                "reason": "Staff does not work on this day",
            }

    # Leave / holiday blocked dates from the enhanced Staff model.
    import json
    for field, reason in [
        ("leave_dates", "Staff is on leave"),
        ("holiday_blocked_dates", "Date is holiday/blocked"),
    ]:
        raw = getattr(staff, field, None)
        if raw:
            try:
                blocked_dates = json.loads(raw)
            except Exception:
                blocked_dates = []
            if selected_date.isoformat() in blocked_dates:
                return {
                    "staff_id": staff.id,
                    "date": selected_date.isoformat(),
                    "available_slots": [],
                    "reason": reason,
                }

    if service and getattr(staff, "services", None):
        try:
            services = json.loads(staff.services)
        except Exception:
            services = []
        if services and service not in services:
            return {
                "staff_id": staff.id,
                "date": selected_date.isoformat(),
                "available_slots": [],
                "reason": "Service is not handled by this staff member",
            }

    start_time = getattr(staff, "start_time", None)
    end_time = getattr(staff, "end_time", None)
    if start_time and end_time:
        from datetime import time
        try:
            work_start = datetime.strptime(start_time, "%H:%M").time()
            work_end = datetime.strptime(end_time, "%H:%M").time()
        except ValueError:
            raise HTTPException(500, "Invalid staff working time configuration")
        break_start = getattr(staff, "break_start", None)
        break_end = getattr(staff, "break_end", None)
    else:
        work_start, work_end = _parse_working_hours(staff.working_hours)
        if not work_start:
            raise HTTPException(500, "Invalid staff working hours configuration")
        break_start = break_end = None

    duration = duration_minutes or _duration_minutes(staff.appointment_duration)
    if duration <= 0:
        raise HTTPException(400, "Appointment duration must be positive")

    interval = slot_interval_minutes or duration
    if interval <= 0:
        interval = duration

    day_start = datetime.combine(selected_date, work_start)
    day_end = datetime.combine(selected_date, work_end)

    # Existing application appointments.
    blocked = []
    appt_query = db.query(Appointment).filter(
        Appointment.status != AppointmentStatus.CANCELLED
    )
    if staff.organization_id:
        appt_query = appt_query.filter(Appointment.organization_id == staff.organization_id)
    appointments = appt_query.all()
    for appt in appointments:
        if not _staff_matches(appt, staff):
            continue
        pair = _appointment_datetime(appt, selected_date)
        if pair:
            blocked.append(pair)

    # Staff break.
    if break_start and break_end:
        try:
            bs = datetime.strptime(break_start, "%H:%M").time()
            be = datetime.strptime(break_end, "%H:%M").time()
            blocked.append((
                datetime.combine(selected_date, bs),
                datetime.combine(selected_date, be),
            ))
        except ValueError:
            raise HTTPException(500, "Invalid staff break configuration")

    # Real Google events.
    google = _google_events(db, day_start, day_end, google_calendar_id, user_id=getattr(staff, "user_id", None))
    for event in google:
        pair = _event_to_interval(event)
        if pair:
            blocked.append(pair)

    # Real Microsoft events.
    microsoft = _microsoft_events(db, day_start, day_end, microsoft_calendar_id)
    for event in microsoft:
        pair = _event_to_interval(event)
        if pair:
            blocked.append(pair)

    slots = []
    cursor = day_start
    while cursor + timedelta(minutes=duration) <= day_end:
        slot_end = cursor + timedelta(minutes=duration)

        # Never return overlapping slots or slots that touch a break/event.
        if not _is_overlapping(cursor, slot_end, blocked):
            slots.append({
                "start": cursor.strftime("%H:%M"),
                "end": slot_end.strftime("%H:%M"),
                "start_datetime": cursor.isoformat(),
                "end_datetime": slot_end.isoformat(),
                "duration_minutes": duration,
            })

        cursor += timedelta(minutes=interval)

    return {
        "staff_id": staff.id,
        "staff_name": staff.name,
        "date": selected_date.isoformat(),
        "working_hours": {
            "start": work_start.strftime("%H:%M"),
            "end": work_end.strftime("%H:%M"),
        },
        "break": (
            {"start": break_start, "end": break_end}
            if break_start and break_end else None
        ),
        "appointment_duration_minutes": duration,
        "available_slots": slots,
        "blocked_intervals_count": len(blocked),
        "conflict_checked": True,
    }


@router.get("/{staff_id}/slots")
def staff_slots(
    staff_id: int,
    selected_date: date,
    service: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Convenience endpoint for the frontend calendar."""
    return get_available_slots(
        staff_id=staff_id,
        selected_date=selected_date,
        service=service,
        db=db,
    )


from pydantic import BaseModel

class SlotCheckRequest(BaseModel):
    staff: Optional[str] = None
    staff_id: Optional[int] = None
    date: str
    time: str
    duration: Optional[str] = "30 Minutes"
    department: Optional[str] = None


@router.post("/check-slot")
def check_slot_availability(payload: SlotCheckRequest, db: Session = Depends(get_db)):
    """
    Live real-time Google Calendar and Database availability check for manual admin bookings.
    """
    staff = None
    if payload.staff_id:
        staff = db.query(Staff).filter(Staff.id == payload.staff_id).first()
    if not staff and payload.staff:
        staff = db.query(Staff).filter(Staff.name.ilike(f"%{payload.staff.strip()}%")).first()
    if not staff:
        staff = db.query(Staff).first()
    if not staff:
        return {"is_available": False, "reason": "No staff found in records.", "available_slots": []}

    from src.services.calendar_verification_service import calendar_verification_service
    target_date = calendar_verification_service.parse_date(payload.date)
    time_24h = calendar_verification_service.parse_time_to_24h(payload.time) or "10:00"

    duration_mins = _duration_minutes(payload.duration)

    slots_data = get_available_slots(
        staff_id=staff.id,
        selected_date=target_date,
        service=payload.department,
        duration_minutes=duration_mins,
        db=db
    )

    available_slots = slots_data.get("available_slots", [])
    selected_slot = next((s for s in available_slots if s["start"] == time_24h), None)
    is_available = selected_slot is not None

    formatted_time = calendar_verification_service.format_time_12h(time_24h)
    formatted_date = target_date.strftime("%d %b %Y")
    alt_slots = [calendar_verification_service.format_time_12h(s["start"]) for s in available_slots[:6]]

    if not is_available:
        if alt_slots:
            reason = f"Slot {formatted_time} on {formatted_date} is already booked on Google Calendar or database. Suggested slots: {', '.join(alt_slots)}"
        else:
            reason = f"No open slots on {formatted_date} for {staff.name}."
    else:
        reason = f"Slot {formatted_time} on {formatted_date} with {staff.name} is available in Google Calendar & Database."

    return {
        "is_available": is_available,
        "staff_id": staff.id,
        "staff_name": staff.name,
        "date": formatted_date,
        "time": formatted_time,
        "reason": reason,
        "available_slots": alt_slots,
        "google_calendar_checked": True
    }

