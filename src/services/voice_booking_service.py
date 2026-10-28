import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from src.models.appointment import Appointment, AppointmentStatus
from src.models.staff import Staff


class VoiceBookingService:
    """
    Deterministic appointment-booking state machine for the live voice AI.

    The AI extracts intent and caller answers; this service owns the unsafe-to-
    guess parts: required fields, real availability, final conflict check,
    DB booking, and calendar creation.
    """

    # Fields required before a booking can be committed.
    # NOTE: The question text for each field is generated dynamically by
    # BookingStateService._get_target_entity_question() (for "staff_id") and
    # the other state-specific logic in process_booking_turn().
    # Do NOT add hardcoded question strings here.
    REQUIRED = (
        "department",
        "staff_id",
        "date",
        "service",
        "time",
    )

    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def detect_appointment_intent(intent: str, text: str = "") -> bool:
        value = f"{intent} {text}".upper()
        return (
            "BOOK_APPOINTMENT" in value
            or "CHECK_AVAILABILITY" in value
            or bool(re.search(r"\b(book|appointment|schedule|available slot)\b", value))
        )

    def get_staff(self, staff_id: int) -> Staff:
        staff = self.db.query(Staff).filter(Staff.id == staff_id).first()
        if not staff:
            raise HTTPException(404, "Staff not found")
        return staff

    def collect(
        self,
        state: Dict[str, Any],
        *,
        department: Optional[str] = None,
        staff_id: Optional[int] = None,
        appointment_date: Optional[str] = None,
        service: Optional[str] = None,
        time: Optional[str] = None,
        duration_minutes: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Merge caller answers into booking state and return the next required
        field name. Questions are generated dynamically by the state machine.
        """
        state = dict(state or {})
        if department:
            state["department"] = department
        if staff_id:
            state["staff_id"] = staff_id
        if appointment_date:
            state["date"] = appointment_date
        if service:
            state["service"] = service
        if time:
            state["time"] = time
        if duration_minutes:
            state["duration_minutes"] = duration_minutes

        missing = [key for key in self.REQUIRED if not state.get(key)]
        return {
            "state": state,
            "complete": not missing,
            "missing": missing,
            "next_field": missing[0] if missing else None,
        }

    def available_slots(
        self,
        staff_id: int,
        appointment_date: str,
        service: Optional[str] = None,
        duration_minutes: Optional[int] = None,
    ):
        from src.routes.availability_routes import get_available_slots

        result = get_available_slots(
            staff_id=staff_id,
            selected_date=date.fromisoformat(appointment_date),
            service=service,
            duration_minutes=duration_minutes,
            db=self.db,
        )
        return result["available_slots"]

    def confirm_selected_slot(
        self,
        staff_id: int,
        appointment_date: str,
        selected_time: str,
        service: Optional[str] = None,
        duration_minutes: Optional[int] = None,
    ) -> Dict[str, Any]:
        slots = self.available_slots(
            staff_id, appointment_date, service, duration_minutes
        )
        selected = next(
            (s for s in slots if s["start"] == selected_time),
            None,
        )
        if not selected:
            raise HTTPException(
                409,
                "The selected slot is no longer available. Please choose another slot.",
            )
        return selected

    def book(
        self,
        *,
        customer: str,
        phone: Optional[str],
        email: Optional[str],
        department: str,
        staff_id: int,
        appointment_date: str,
        service: str,
        selected_time: str,
        duration_minutes: Optional[int] = None,
        notes: Optional[str] = None,
        calendar_id: str = "primary",
    ) -> Dict[str, Any]:
        staff = self.get_staff(staff_id)

        # First availability validation.
        slot = self.confirm_selected_slot(
            staff_id,
            appointment_date,
            selected_time,
            service,
            duration_minutes,
        )

        duration = duration_minutes or int(re.search(r"\d+", staff.appointment_duration or "30").group())

        # Second/final availability validation immediately before DB insert.
        final_slot = self.confirm_selected_slot(
            staff_id,
            appointment_date,
            selected_time,
            service,
            duration,
        )

        appointment = Appointment(
            customer=customer,
            phone=phone,
            email=email,
            department=department,
            staff=staff.name,
            appointment_date=datetime.strptime(
                appointment_date, "%Y-%m-%d"
            ).strftime("%d %b %Y"),
            appointment_time=datetime.strptime(
                selected_time, "%H:%M"
            ).strftime("%I:%M %p"),
            duration=f"{duration} Minutes",
            notes=notes,
            status=AppointmentStatus.CONFIRMED,
        )

        self.db.add(appointment)
        self.db.commit()
        self.db.refresh(appointment)

        # Synchronize appointment with Leads Management module
        try:
            from src.services.lead_service import LeadService
            LeadService(self.db).link_appointment(appointment)
        except Exception as lead_err:
            pass

        calendar_event = None
        calendar_error = None
        try:
            from src.services.calendar_service import CalendarService
            start = datetime.fromisoformat(final_slot["start_datetime"])
            end = datetime.fromisoformat(final_slot["end_datetime"])
            calendar_event = CalendarService(self.db).create_event(
                summary=f"{service} - {customer}",
                start=start,
                end=end,
                description=(
                    f"Customer: {customer}\n"
                    f"Phone: {phone or ''}\n"
                    f"Staff: {staff.name}\n"
                    f"Service: {service}\n"
                    f"Appointment ID: {appointment.id}"
                ),
                calendar_id=calendar_id,
            )
        except Exception as exc:
            calendar_error = str(exc)

        # Trigger real-time Email & WhatsApp notifications
        try:
            from src.services.notification_service import send_realtime_appointment_notification
            send_realtime_appointment_notification(self.db, appointment, "created")
        except Exception:
            pass

        if calendar_error:
            return {
                "success": True,
                "calendar_synced": False,
                "calendar_error": calendar_error,
                "appointment": appointment,
                "confirmation": self._confirmation(
                    appointment, staff, service, calendar_event
                ),
            }

        return {
            "success": True,
            "calendar_synced": True,
            "appointment": appointment,
            "calendar_event": calendar_event,
            "confirmation": self._confirmation(
                appointment, staff, service, calendar_event
            ),
        }

    @staticmethod
    def _confirmation(
        appointment: Appointment,
        staff: Staff,
        service: str,
        calendar_event: Optional[dict],
    ) -> str:
        confirmation = (
            f"Your {service} appointment is confirmed for "
            f"{appointment.appointment_date} at {appointment.appointment_time} "
            f"with {staff.name}."
        )
        if calendar_event:
            confirmation += " The appointment has also been added to the calendar."
        return confirmation
