import logging
from datetime import date, datetime
from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.models.appointment import Appointment, AppointmentStatus
from src.repositories.appointment_repository import AppointmentRepository
from src.validators.appointment_validator import AppointmentCreate, AppointmentUpdate

logger = logging.getLogger(__name__)


class AppointmentService:
    """
    Appointment business layer.

    Integrates real availability checks and Google/Microsoft calendar events
    while retaining the existing Appointment model/schema.
    """

    def __init__(self, db: Session):
        self.db = db
        self.repo = AppointmentRepository(db)

    def list_appointments(self, organization_id: Optional[int] = None) -> List[Appointment]:
        return self.repo.get_all(organization_id=organization_id)

    def get_appointment(self, appointment_id: int, organization_id: Optional[int] = None) -> Appointment:
        appointment = self.repo.get_by_id(appointment_id, organization_id=organization_id)
        if not appointment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Appointment not found",
            )
        return appointment

    @staticmethod
    def _staff_id(staff_name: str):
        from src.models.staff import Staff
        return Staff.id

    def _find_staff(self, staff_name: str):
        from src.models.staff import Staff
        staff = self.db.query(Staff).filter(Staff.name == staff_name).first()
        return staff

    @staticmethod
    def _parse_datetime(appointment: Appointment):
        date_str = (appointment.appointment_date or "").strip()
        d = None
        for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
            try:
                d = datetime.strptime(date_str, fmt).date()
                break
            except ValueError:
                pass

        if not d:
            try:
                from src.services.calendar_verification_service import calendar_verification_service
                d = calendar_verification_service.parse_date(date_str)
            except Exception:
                d = None

        if not d:
            raise HTTPException(400, f"Invalid appointment date: {date_str}")

        time_str = (appointment.appointment_time or "").strip().upper()
        t = None
        for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M", "%H:%M:%S", "%I %p", "%I%p"):
            try:
                t = datetime.strptime(time_str, fmt).time()
                break
            except ValueError:
                pass

        if not t:
            try:
                from src.services.calendar_verification_service import calendar_verification_service
                t24 = calendar_verification_service.parse_time_to_24h(time_str)
                if t24:
                    t = datetime.strptime(t24, "%H:%M").time()
            except Exception:
                t = None

        if not t:
            raise HTTPException(400, f"Invalid appointment time: {time_str}")

        minutes = 30
        import re
        match = re.search(r"\d+", appointment.duration or "")
        if match:
            minutes = int(match.group())

        start = datetime.combine(d, t)
        return start, start + __import__("datetime").timedelta(minutes=minutes)

    def _calendar_event_id(self, appointment):
        if getattr(appointment, "calendar_event_id", None):
            return appointment.calendar_event_id
        import re
        match = re.search(r"\[calendar_event_id:([^\]]+)\]", appointment.notes or "")
        return match.group(1) if match else None

    def _set_calendar_event_id(self, appointment, event_id):
        appointment.calendar_event_id = event_id
        import re
        notes = appointment.notes or ""
        notes = re.sub(r"\s*\[calendar_event_id:[^\]]+\]", "", notes).rstrip()
        appointment.notes = f"{notes} [calendar_event_id:{event_id}]".strip()

    def _check_real_availability(self, appointment, exclude_id=None):
        staff = self._find_staff(appointment.staff)
        if not staff:
            raise HTTPException(404, f"Staff '{appointment.staff}' not found")

        from src.routes.availability_routes import get_available_slots

        start, _ = self._parse_datetime(appointment)
        result = get_available_slots(
            staff_id=staff.id,
            selected_date=start.date(),
            service=appointment.department,
            duration_minutes=None,
            db=self.db,
        )
        selected = start.strftime("%H:%M")
        if not any(slot["start"] == selected for slot in result["available_slots"]):
            raise HTTPException(
                409,
                "Selected appointment time is no longer available",
            )

    def _find_duplicate(self, payload: AppointmentCreate) -> Optional[Appointment]:
        """Check if an identical active appointment already exists to prevent duplicates."""
        try:
            candidates = self.db.query(Appointment).filter(
                Appointment.staff.ilike(payload.staff.strip()),
                Appointment.appointment_date == payload.appointment_date.strip(),
                Appointment.appointment_time == payload.appointment_time.strip(),
                Appointment.status != AppointmentStatus.CANCELLED
            ).all()
            for cand in candidates:
                if payload.phone and cand.phone and payload.phone.strip() == cand.phone.strip():
                    return cand
                if payload.customer and cand.customer and payload.customer.strip().lower() == cand.customer.strip().lower():
                    return cand
        except Exception as e:
            logger.warning(f"Error checking duplicate appointment: {e}")
        return None

    def _sync_appointment_to_calendar(self, appointment: Appointment, user_id: Optional[int] = None) -> Optional[str]:
        """Syncs an individual appointment to Google Calendar and stores event id."""
        try:
            start, end = self._parse_datetime(appointment)
            from src.services.calendar_service import CalendarService
            cal_svc = CalendarService(self.db, user_id=user_id)
            if not cal_svc.get_connected_config():
                return None

            event = cal_svc.create_event(
                summary=f"{appointment.department} - {appointment.customer}",
                start=start,
                end=end,
                description=(
                    f"Customer: {appointment.customer}\n"
                    f"Phone: {appointment.phone or ''}\n"
                    f"Staff: {appointment.staff}\n"
                    f"Service: {appointment.department}\n"
                    f"Appointment ID: {appointment.id}"
                ),
            )
            if event and event.get("id"):
                ev_id = event["id"]
                self._set_calendar_event_id(appointment, ev_id)
                try:
                    from src.services.lead_service import LeadService
                    LeadService(self.db).link_appointment(appointment)
                except Exception:
                    pass
                self.db.commit()
                self.db.refresh(appointment)
                logger.info(f"Appointment #{appointment.id} successfully synced to Google Calendar event {ev_id}")
                return ev_id
        except HTTPException as he:
            if he.status_code == 409:
                logger.warning(f"Google Calendar conflict for appointment #{appointment.id}: {he.detail}")
                raise
            logger.warning(f"Google Calendar sync notice for appointment #{appointment.id}: {he}")
            raise
        except Exception as e:
            logger.warning(f"Google Calendar sync notice for appointment #{appointment.id}: {e}")
            raise
        return None

    def _sync_appointment_to_calendar_async(self, appointment_id: int, user_id: Optional[int] = None):
        """Asynchronously syncs an individual appointment to Google Calendar in a background thread."""
        def _worker():
            from src.config.database import SessionLocal
            db = SessionLocal()
            try:
                appt = db.query(Appointment).filter(Appointment.id == appointment_id).first()
                if appt:
                    svc = AppointmentService(db)
                    svc._sync_appointment_to_calendar(appt, user_id=user_id)
            except Exception as e:
                logger.debug(f"Background calendar sync notice for #{appointment_id}: {e}")
            finally:
                db.close()
        import threading
        threading.Thread(target=_worker, daemon=True).start()

    def _generate_customer_id(self, phone: Optional[str] = None, customer_name: Optional[str] = None) -> str:
        """
        Generate or reuse a unique customer ID (e.g. CUST-84920).
        Reuses existing customer ID if the caller phone matches an existing customer.
        """
        import random
        phone_clean = (phone or "").strip()
        if phone_clean:
            existing = self.db.query(Appointment).filter(
                Appointment.phone == phone_clean,
                Appointment.customer_id != None,
                Appointment.customer_id != ""
            ).first()
            if existing and existing.customer_id:
                return existing.customer_id

        # Generate new unique customer_id
        for _ in range(10):
            cid = f"CUST-{random.randint(10000, 99999)}"
            conflict = self.db.query(Appointment).filter(Appointment.customer_id == cid).first()
            if not conflict:
                return cid

        return f"CUST-{random.randint(100000, 999999)}"

    def create_appointment(
        self,
        payload: AppointmentCreate,
        user_id: Optional[int] = None,
        organization_id: Optional[int] = None,
        sync_calendar_sync: bool = False
    ) -> Appointment:
        # 1. Prevent duplicate bookings
        duplicate = self._find_duplicate(payload)
        if duplicate:
            logger.info(f"Duplicate appointment detected: returning existing #{duplicate.id} for {payload.customer}")
            if not duplicate.customer_id:
                duplicate.customer_id = self._generate_customer_id(duplicate.phone, duplicate.customer)
                self.db.commit()
                self.db.refresh(duplicate)
            if not self._calendar_event_id(duplicate):
                if sync_calendar_sync:
                    self._sync_appointment_to_calendar(duplicate, user_id=user_id)
                else:
                    self._sync_appointment_to_calendar_async(duplicate.id, user_id=user_id)
            return duplicate

        data = payload.model_dump()
        if organization_id is not None:
            data["organization_id"] = organization_id

        # Ensure customer_id is assigned
        if not data.get("customer_id"):
            data["customer_id"] = self._generate_customer_id(data.get("phone"), data.get("customer"))

        appointment = self.repo.create(data)

        # Synchronize appointment with Leads Management module
        try:
            from src.services.lead_service import LeadService
            LeadService(self.db).link_appointment(appointment)
        except Exception:
            pass

        # Sync to Google Calendar
        if sync_calendar_sync:
            self._sync_appointment_to_calendar(appointment, user_id=user_id)
        else:
            self._sync_appointment_to_calendar_async(appointment.id, user_id=user_id)

        # Trigger real-time Email & WhatsApp notifications (async daemon)
        try:
            from src.services.notification_service import send_realtime_appointment_notification_async
            send_realtime_appointment_notification_async(appointment.id, "created")
        except Exception:
            pass

        # Prepare payload for background socket & webhook broadcast
        payload_data = {
            "id": appointment.id,
            "customer": appointment.customer,
            "customer_id": appointment.customer_id,
            "phone": appointment.phone,
            "staff": appointment.staff,
            "department": appointment.department,
            "appointment_date": appointment.appointment_date,
            "appointment_time": appointment.appointment_time,
            "duration": appointment.duration,
            "status": str(appointment.status.value if hasattr(appointment.status, "value") else appointment.status),
            "notes": appointment.notes,
        }

        # Dispatch reminders, socket events, and webhooks asynchronously in background daemon thread
        def _post_create_background_workers(appt_id: int, pdata: dict, org_id: Optional[int]):
            from src.config.database import SessionLocal
            bg_db = SessionLocal()
            try:
                from src.services.reminder_service import ReminderService
                bg_appt = bg_db.query(Appointment).filter(Appointment.id == appt_id).first()
                if bg_appt:
                    try:
                        ReminderService(bg_db).create_for_appointment(bg_appt)
                    except Exception as err:
                        logger.debug(f"Could not queue reminders for #{appt_id}: {err}")
            except Exception as e:
                logger.debug(f"Reminder background error: {e}")
            finally:
                bg_db.close()

            # Emit real-time Socket.IO notification
            try:
                from src.sockets.socket_manager import sio
                import asyncio
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.run_coroutine_threadsafe(sio.emit("appointment_created", pdata), loop)
                    else:
                        loop.run_until_complete(sio.emit("appointment_created", pdata))
                except Exception:
                    pass
            except Exception as e:
                logger.debug(f"Could not emit appointment_created socket event: {e}")

            # Dispatch Webhook Event
            try:
                from src.services.webhook_dispatcher import dispatch_event
                target_org_id = org_id or 1
                dispatch_event("appointment.created", target_org_id, pdata)
            except Exception as wh_err:
                logger.debug(f"Could not dispatch appointment.created webhook: {wh_err}")

        import threading
        threading.Thread(
            target=_post_create_background_workers,
            args=(appointment.id, payload_data, appointment.organization_id or organization_id or 1),
            daemon=True
        ).start()

        return appointment


    def update_appointment(
        self, appointment_id: int, payload: AppointmentUpdate, user_id: Optional[int] = None, organization_id: Optional[int] = None
    ) -> Appointment:
        appointment = self.get_appointment(appointment_id, organization_id=organization_id)
        old_event_id = self._calendar_event_id(appointment)

        changes = payload.model_dump(exclude_unset=True)
        candidate = {k: v for k, v in changes.items() if v is not None}

        # Apply changes to the appointment row.
        for key, value in candidate.items():
            setattr(appointment, key, value)

        # Soft availability check (non-fatal for manual reschedules).
        if appointment.status != AppointmentStatus.CANCELLED:
            try:
                self._check_real_availability(appointment, appointment_id)
            except Exception:
                pass  # Availability warnings never block updates

        # Commit the DB change first so the booking is always persisted.
        self.db.commit()
        self.db.refresh(appointment)

        # Best-effort calendar sync.
        try:
            start, end = self._parse_datetime(appointment)
            if appointment.status == AppointmentStatus.CANCELLED:
                if old_event_id:
                    self._delete_calendar_event(old_event_id, user_id=user_id)
            else:
                if old_event_id:
                    self._update_calendar_event(old_event_id, appointment, start, end, user_id=user_id)
                else:
                    event = self._create_calendar_event(appointment, start, end, user_id=user_id)
                    if event and event.get("id"):
                        self._set_calendar_event_id(appointment, event["id"])
                        self.db.commit()
                        self.db.refresh(appointment)
        except Exception:
            pass  # Calendar not configured — skip silently

        # Synchronize appointment update with Leads Management module
        try:
            from src.models.lead import Lead
            lead = self.db.query(Lead).filter(Lead.appointment_id == appointment.id).first()
            if not lead and appointment.phone:
                d10 = appointment.phone[-10:] if len(appointment.phone) >= 10 else appointment.phone
                lead = self.db.query(Lead).filter(
                    Lead.organization_id == (appointment.organization_id or 1),
                    Lead.phone.like(f"%{d10}")
                ).first()
            if lead:
                st_val = str(appointment.status.value if hasattr(appointment.status, "value") else appointment.status)
                lead.appointment_status = st_val
                if st_val.lower() == "cancelled":
                    lead.lead_status = "Lost"
                elif st_val.lower() in ("confirmed", "completed"):
                    lead.lead_status = "Converted"
                self.db.commit()
                from src.services.lead_service import LeadService
                LeadService(self.db).add_activity(
                    lead_id=lead.id,
                    organization_id=lead.organization_id,
                    event_type="appointment_updated",
                    title=f"Appointment Status Updated to {st_val}",
                    description=f"Appointment date: {appointment.appointment_date} at {appointment.appointment_time} with {appointment.staff}",
                    metadata={"appointment_id": appointment.id, "status": st_val}
                )
        except Exception:
            pass
            pass  # Calendar not configured — skip silently

        # Trigger real-time Email & WhatsApp notifications (best-effort async)
        try:
            from src.services.notification_service import send_realtime_appointment_notification_async
            action = "cancelled" if appointment.status == AppointmentStatus.CANCELLED else "rescheduled"
            send_realtime_appointment_notification_async(appointment.id, action)
        except Exception:
            pass

        # Dispatch Webhook Event (best-effort async)
        try:
            from src.services.webhook_dispatcher import dispatch_event
            org_id = appointment.organization_id or organization_id or 1
            ev_name = "appointment.cancelled" if appointment.status == AppointmentStatus.CANCELLED else "appointment.updated"
            dispatch_event(ev_name, org_id, {
                "id": appointment.id,
                "customer": appointment.customer,
                "phone": appointment.phone,
                "staff": appointment.staff,
                "department": appointment.department,
                "appointment_date": appointment.appointment_date,
                "appointment_time": appointment.appointment_time,
                "status": str(appointment.status.value if hasattr(appointment.status, "value") else appointment.status),
            })
        except Exception as wh_err:
            logger.debug(f"Could not dispatch appointment update webhook: {wh_err}")

        return appointment

    def delete_appointment(self, appointment_id: int, user_id: Optional[int] = None, organization_id: Optional[int] = None) -> None:
        appointment = self.get_appointment(appointment_id, organization_id=organization_id)
        event_id = self._calendar_event_id(appointment)

        if event_id:
            self._delete_calendar_event(event_id, user_id=user_id)

        # Soft-cancel so appointment history remains available.
        appointment.status = AppointmentStatus.CANCELLED
        self.db.commit()

        # Update linked lead to Cancelled
        try:
            from src.models.lead import Lead
            lead = self.db.query(Lead).filter(Lead.appointment_id == appointment.id).first()
            if lead:
                lead.appointment_status = "Cancelled"
                self.db.commit()
                from src.services.lead_service import LeadService
                LeadService(self.db).add_activity(
                    lead_id=lead.id,
                    organization_id=lead.organization_id,
                    event_type="appointment_cancelled",
                    title="Appointment Cancelled",
                    description=f"Appointment #{appointment.id} was cancelled.",
                    metadata={"appointment_id": appointment.id}
                )
        except Exception:
            pass

        # Trigger real-time Email & WhatsApp notifications (best-effort async)
        try:
            from src.services.notification_service import send_realtime_appointment_notification_async
            send_realtime_appointment_notification_async(appointment.id, "cancelled")
        except Exception:
            pass

        # Dispatch Webhook Event (best-effort async)
        try:
            from src.services.webhook_dispatcher import dispatch_event
            org_id = appointment.organization_id or organization_id or 1
            dispatch_event("appointment.cancelled", org_id, {
                "id": appointment.id,
                "customer": appointment.customer,
                "phone": appointment.phone,
                "status": "Cancelled",
            })
        except Exception as wh_err:
            logger.debug(f"Could not dispatch appointment.cancelled webhook: {wh_err}")

    def _create_calendar_event(self, appointment, start, end, user_id: Optional[int] = None):
        from src.services.calendar_service import CalendarService
        try:
            return CalendarService(self.db, user_id=user_id).create_event(
                summary=f"{appointment.department} - {appointment.customer}",
                start=start,
                end=end,
                description=f"Staff: {appointment.staff}\nAppointment ID: {appointment.id}",
            )
        except Exception:
            from src.routes.microsoft_calendar_routes import create_event, EventPayload
            return create_event(
                EventPayload(
                    subject=f"{appointment.department} - {appointment.customer}",
                    start=start,
                    end=end,
                    body=f"Staff: {appointment.staff}\nAppointment ID: {appointment.id}",
                ),
                self.db,
            )

    def _update_calendar_event(self, event_id, appointment, start, end, user_id: Optional[int] = None):
        try:
            from src.services.calendar_service import CalendarService
            return CalendarService(self.db, user_id=user_id).update_event(
                event_id,
                summary=f"{appointment.department} - {appointment.customer}",
                start=start,
                end=end,
                description=f"Staff: {appointment.staff}\nAppointment ID: {appointment.id}",
            )
        except Exception:
            from src.routes.microsoft_calendar_routes import update_event, EventUpdatePayload
            return update_event(
                event_id,
                EventUpdatePayload(
                    subject=f"{appointment.department} - {appointment.customer}",
                    start=start,
                    end=end,
                    body=f"Staff: {appointment.staff}\nAppointment ID: {appointment.id}",
                ),
                self.db,
            )

    def _delete_calendar_event(self, event_id, user_id: Optional[int] = None):
        try:
            from src.services.calendar_service import CalendarService
            return CalendarService(self.db, user_id=user_id).delete_event(event_id)
        except Exception:
            from src.routes.microsoft_calendar_routes import cancel_event
            return cancel_event(event_id, "default", self.db)

    def sync_with_google_calendar(self, user_id: Optional[int] = None) -> dict:
        """
        Bidirectional synchronization between Google Calendar and database appointments.
        1. Finds un-synced DB appointments and pushes them to Google Calendar.
        2. Retrieves Google Calendar events and synchronizes updates and cancellations.
        """
        from src.services.calendar_service import CalendarService
        import datetime as dt

        cal_svc = CalendarService(self.db, user_id=user_id)
        config = cal_svc.get_connected_config()
        if not config:
            return {"status": "skipped", "message": "Google Calendar is not connected."}

        pushed = 0
        updated = 0
        now = datetime.now()
        start_window = now - dt.timedelta(days=14)
        end_window = now + dt.timedelta(days=90)

        # 1. Push un-synced active appointments to Google Calendar
        active_appts = self.db.query(Appointment).filter(
            Appointment.status != AppointmentStatus.CANCELLED
        ).all()

        for appt in active_appts:
            event_id = self._calendar_event_id(appt)
            if not event_id:
                eid = self._sync_appointment_to_calendar(appt, user_id=user_id)
                if eid:
                    pushed += 1

        # 2. Reconcile from Google Calendar
        try:
            events = cal_svc.list_events(start=start_window, end=end_window)
            for ev in events:
                ev_id = ev.get("id")
                if not ev_id:
                    continue
                ev_status = ev.get("status")
                matched_appt = next(
                    (a for a in active_appts if self._calendar_event_id(a) == ev_id),
                    None
                )
                if matched_appt:
                    if ev_status == "cancelled" and matched_appt.status != AppointmentStatus.CANCELLED:
                        matched_appt.status = AppointmentStatus.CANCELLED
                        self.db.commit()
                        updated += 1
                    elif ev.get("start"):
                        try:
                            ev_start = datetime.fromisoformat(ev["start"].replace("Z", "+00:00")).replace(tzinfo=None)
                            date_fmt = ev_start.strftime("%d %b %Y")
                            time_fmt = ev_start.strftime("%I:%M %p")
                            if matched_appt.appointment_date != date_fmt or matched_appt.appointment_time != time_fmt:
                                matched_appt.appointment_date = date_fmt
                                matched_appt.appointment_time = time_fmt
                                self.db.commit()
                                updated += 1
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(f"Error during Google Calendar reconciliation: {e}")

        return {
            "status": "success",
            "calendar_account": config.email or "Google Calendar",
            "pushed_to_calendar": pushed,
            "reconciled_from_calendar": updated,
            "total_active_appointments": len(active_appts),
        }


